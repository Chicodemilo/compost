#!/usr/bin/env python3
"""COMPOST — Phase 2 search server. Stdlib only, localhost only.

GET /                    the search page
GET /api/search?q=...    FTS5 search across all years
                         all search routes take &mark=1 — only marked entries
GET /api/search?year=Y   one year's entries (browse view loads years lazily)
GET /api/search          per-year counts for the folded browse view
GET /audio/<rel>         audio with Range support so <audio> can seek
GET /api/entry?id=...    one entry, whole transcript, every column + tags
GET /api/random          one entry at random (honors ?cls= like search does)
POST /api/reveal         {"path": audio_path} — reveal that file in Finder
POST /api/mark           {"id": ..., "mark": true|false} — flag "something here"
POST /api/remove         {"id": ...} — take one entry out of the pile
GET /api/classes         the class roster (name -> writeup)
POST /api/classes        create or update a class {"name": ..., "writeup": ...}
DELETE /api/classes/<name>  retire a class — falls forward only: already-filed
                            entries keep it; the clerk just stops offering it
"""

import json
import re
import sqlite3
import subprocess
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

COMPOST = Path.home() / "compost"
META = COMPOST / "meta"
REMOVED = COMPOST / "removed"
DB = COMPOST / "compost.db"
STATIC = Path(__file__).parent / "static"
PORT = 8771
CLASS_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
ENTRY_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")  # a path is built from this

AUDIO_TYPES = {".m4a": "audio/mp4", ".qta": "audio/mp4", ".wav": "audio/wav"}


TAGS_SUB = "(SELECT group_concat(tag, ',') FROM tags WHERE entry_id = e.id) AS tags"


def class_filter(conn, cls: str):
    """Honors classes actually worn by entries, not just the live roster —
    retired classes never vanish from history, so never from here."""
    roster = [r[0] for r in conn.execute("SELECT name FROM classes ORDER BY rowid")]
    present = {r[0] for r in conn.execute("SELECT DISTINCT class FROM entries")}
    selected = [c for c in cls.split(",") if c in (set(roster) | present)]  # unknown names ignored
    return roster, present, selected


def row_filter(selected: list, marked: bool) -> tuple[list, list]:
    """Conditions for the two filters. Class and mark are different axes —
    a marked entry keeps whatever class it wears, compost included."""
    conds, params = [], []
    if selected:
        conds.append(f"e.class IN ({','.join('?' * len(selected))})")
        params += selected
    if marked:
        conds.append("e.status = 'promoted'")
    return conds, params


def set_mark(entry_id: str, marked: bool) -> dict | None:
    """Flip one entry's mark. Meta is truth — the json write is what survives a
    rebuild; the db update just keeps the open page honest. Carries no opinion
    and no scale: either there is something here or nobody has said so yet."""
    mp = META / entry_id[:4] / entry_id[4:6] / f"{entry_id}.json"
    if not mp.is_file():
        return None
    m = json.loads(mp.read_text())
    m["status"] = "promoted" if marked else "raw"
    mp.write_text(json.dumps(m, indent=2))
    try:
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE entries SET status = ? WHERE id = ?", (m["status"], entry_id))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # db is disposable; the next rebuild picks it up from meta
    return load_entry(entry_id)


def remove_entry(entry_id: str) -> dict | None:
    """Take one entry out of the pile.

    The files move to ~/compost/removed/ keeping their layout; they are not
    unlinked. Two reasons, both load-bearing. Rebuild reconstructs the index
    from files, so unlinking would leave nothing to recover from — a slip
    would be final. And ingest derives idempotency from meta/*.json alone
    (ingest.py:76), so a vanished meta file reads as "never processed" and
    launchd re-ingests the original memo on its next fire. The moved meta is
    the tombstone that stops it coming back.

    Emptying ~/compost/removed/ is a deletion only the person at the keyboard
    performs. This function never does it.
    """
    entry = load_entry(entry_id)
    if entry is None:
        return None
    ym = f"{entry_id[:4]}/{entry_id[4:6]}"
    targets = [COMPOST / "transcripts" / ym / f"{entry_id}.txt",
               COMPOST / "meta" / ym / f"{entry_id}.json"]
    if entry.get("audio_path"):
        audio = (COMPOST / entry["audio_path"]).resolve()
        if audio.is_relative_to((COMPOST / "audio").resolve()):
            targets.insert(0, audio)
    moved = []
    for src in targets:
        if not src.is_file():
            continue
        dst = REMOVED / src.relative_to(COMPOST)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        moved.append(str(dst.relative_to(COMPOST)))
    try:
        conn = sqlite3.connect(DB)
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.execute("DELETE FROM tags WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM entries_fts WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # db is disposable; the files already left, so rebuild agrees
    return {"id": entry_id, "moved": moved}


def load_entry(entry_id: str = "", cls: str = "", marked: bool = False):
    """One whole entry — full transcript, every column, tags joined.
    No entry_id means pick at random from whatever the filters allow."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if entry_id:
        row = conn.execute(
            f"SELECT e.*, {TAGS_SUB} FROM entries e WHERE e.id = ?", (entry_id,)).fetchone()
    else:
        _, _, selected = class_filter(conn, cls)
        # random is random: the filters narrow the pile, nothing weights the draw
        conds, params = row_filter(selected, marked)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        row = conn.execute(
            f"SELECT e.*, {TAGS_SUB} FROM entries e{where} ORDER BY RANDOM() LIMIT 1",
            params).fetchone()
    conn.close()
    return dict(row) if row else None


def query_db(q: str, year: str = "", cls: str = "", marked: bool = False, limit: int = 100):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = {}
    roster, present, selected = class_filter(conn, cls)
    conds, fparams = row_filter(selected, marked)
    cfilter = "".join(f" AND {c}" for c in conds)
    tags_sub = TAGS_SUB
    if q.strip():
        # quote each term so user input can't break FTS5 syntax
        match = " ".join(f'"{t}"' for t in re.findall(r"[^\s\"]+", q))
        rows = conn.execute(
            f"""SELECT e.*, snippet(entries_fts, 1, '<mark>', '</mark>', '…', 24) AS snip, {tags_sub}
               FROM entries_fts f JOIN entries e ON e.id = f.id
               WHERE entries_fts MATCH ?{cfilter} ORDER BY e.created_at DESC LIMIT ?""",
            (match, *fparams, limit)).fetchall()
        out["results"] = [dict(r) for r in rows]
    elif year:
        rows = conn.execute(
            f"SELECT e.*, substr(e.transcript, 1, 220) AS snip, {tags_sub} FROM entries e "
            f"WHERE substr(e.created_at, 1, 4) = ?{cfilter} ORDER BY e.created_at DESC",
            (year, *fparams)).fetchall()
        out["results"] = [dict(r) for r in rows]
    else:
        # FILTER keeps zero-count years in the list — the archive's shape stays visible
        filt = f" FILTER (WHERE {' AND '.join(conds)})" if conds else ""
        counts = conn.execute(
            f"SELECT substr(e.created_at, 1, 4), COUNT(*){filt} "
            f"FROM entries e GROUP BY 1 ORDER BY 1 DESC", fparams)
        out["years"] = [{"year": y, "count": c} for y, c in counts]
        out["classes"] = [c for c in roster if c in present] + sorted(present - set(roster))
    total = conn.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM entries").fetchone()
    conn.close()
    out.update(total=total[0], oldest=total[1], newest=total[2])
    return out


def load_classes() -> dict:
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        classes = dict(conn.execute("SELECT name, writeup FROM classes").fetchall())
        conn.close()
        return classes
    except sqlite3.Error:
        return {}


def save_classes(classes: dict) -> None:
    """Classes are db-native (the one deliberate exception to files-are-truth);
    rebuild carries this table across instead of reconstructing it."""
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS classes (name TEXT PRIMARY KEY, writeup TEXT NOT NULL)")
    conn.execute("DELETE FROM classes")
    conn.executemany("INSERT INTO classes VALUES (?,?)", classes.items())
    conn.commit()
    conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, ctype, body, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser aborted (audio seek, page close) — not an error

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self._send(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
        elif url.path == "/api/search":
            qs = urllib.parse.parse_qs(url.query)
            body = json.dumps(query_db(qs.get("q", [""])[0], qs.get("year", [""])[0],
                                       qs.get("cls", [""])[0],
                                       qs.get("mark", [""])[0] == "1")).encode()
            self._send(200, "application/json", body)
        elif url.path in ("/api/entry", "/api/random"):
            qs = urllib.parse.parse_qs(url.query)
            entry = load_entry(qs.get("id", [""])[0] if url.path == "/api/entry" else "",
                               qs.get("cls", [""])[0], qs.get("mark", [""])[0] == "1")
            if entry is None:
                self._send(404, "text/plain", b"no such entry")
                return
            self._send(200, "application/json", json.dumps(entry).encode())
        elif url.path == "/api/classes":
            self._send(200, "application/json", json.dumps(load_classes()).encode())
        elif url.path.startswith("/audio/"):
            rel = urllib.parse.unquote(url.path[len("/audio/"):])
            f = (COMPOST / "audio" / rel).resolve()
            if not f.is_relative_to((COMPOST / "audio").resolve()) or not f.is_file():
                self._send(404, "text/plain", b"not found")
                return
            data = f.read_bytes()
            ctype = AUDIO_TYPES.get(f.suffix, "application/octet-stream")
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else len(data) - 1
                chunk = data[start:end + 1]
                self._send(206, ctype, chunk, {
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    "Accept-Ranges": "bytes"})
            else:
                self._send(200, ctype, data, {"Accept-Ranges": "bytes"})
        else:
            self._send(404, "text/plain", b"not found")

    def _audio_file(self, rel: str):
        """Resolve a stored audio_path to a real file under ~/compost/audio,
        or None. Same containment check the /audio/ route uses."""
        rel = rel[len("audio/"):] if rel.startswith("audio/") else rel
        f = (COMPOST / "audio" / rel).resolve()
        if not f.is_relative_to((COMPOST / "audio").resolve()) or not f.is_file():
            return None
        return f

    def _reveal(self):
        """Open a Finder window with the audio file selected. Localhost-only
        server, so this is a local convenience, not a remote exec surface —
        the path still has to land inside ~/compost/audio."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            f = self._audio_file(str(body.get("path", "")))
        except (json.JSONDecodeError, ValueError):
            f = None
        if f is None:
            self._send(400, "text/plain", b"no such audio file")
            return
        subprocess.run(["open", "-R", str(f)], check=False)
        self._send(200, "application/json", b'{"revealed": true}')

    def _mark(self, removing: bool = False):
        """Mark toggles the one bit that means there is something here — not a
        rating, not a tag, a third axis. Remove takes the entry out entirely."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            entry_id = str(body.get("id", ""))
        except (json.JSONDecodeError, ValueError):
            entry_id, body = "", {}
        if not ENTRY_ID_RE.match(entry_id):
            self._send(400, "text/plain", b"bad entry id")
            return
        entry = remove_entry(entry_id) if removing else set_mark(entry_id, bool(body.get("mark")))
        if entry is None:
            self._send(404, "text/plain", b"no such entry")
            return
        self._send(200, "application/json", json.dumps(entry).encode())

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/reveal":
            self._reveal()
            return
        if path in ("/api/mark", "/api/remove"):
            self._mark(path == "/api/remove")
            return
        if path != "/api/classes":
            self._send(404, "text/plain", b"not found")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            name, writeup = body["name"].strip(), body["writeup"].strip()
        except (json.JSONDecodeError, KeyError, AttributeError, ValueError):
            self._send(400, "text/plain", b"bad body")
            return
        if not CLASS_NAME_RE.match(name) or not writeup or len(writeup) > 500:
            self._send(400, "text/plain", b"name must be a short lowercase slug; writeup 1-500 chars")
            return
        classes = load_classes()
        classes[name] = writeup
        save_classes(classes)
        self._send(200, "application/json", json.dumps(classes).encode())

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/classes/"):
            self._send(404, "text/plain", b"not found")
            return
        name = urllib.parse.unquote(path[len("/api/classes/"):])
        if name == "compost":
            self._send(400, "text/plain", b"compost is load-bearing; it cannot be retired")
            return
        classes = load_classes()
        if name not in classes:
            self._send(404, "text/plain", b"no such class")
            return
        del classes[name]  # falls forward only — filed entries keep their label
        save_classes(classes)
        self._send(200, "application/json", json.dumps(classes).encode())


if __name__ == "__main__":
    print(f"compost on http://127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
