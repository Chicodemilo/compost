#!/usr/bin/env python3
"""COMPOST — Phase 2 search server. Stdlib only, localhost only.

GET /                    the search page
GET /api/search?q=...    FTS5 search across all years
                         all search routes take &mark=1 (only marked) and
                         &archived=1 (only archived; otherwise archived are excluded)
GET /api/search?year=Y   one year's hunks (browse view loads years lazily)
GET /api/search          per-year counts for the folded browse view
GET /audio/<rel>         audio with Range support so <audio> can seek
GET /api/hunk?id=...    one hunk, whole transcript, every column + tags
GET /api/random          one hunk at random (honors ?cls= like search does)
POST /api/reveal         {"path": audio_path} — reveal that file in Finder
POST /api/mark           {"id": ..., "mark": true|false} — flag "something here"
POST /api/archive           {"id": ..., "archive": true|false} — out of view, or back
POST /api/notes          {"id": ..., "text": ...} — the reader's own writing
GET /api/classes         the class roster (name -> writeup)
POST /api/classes        create or update a class {"name": ..., "writeup": ...}
DELETE /api/classes/<name>  retire a class — falls forward only: already-filed
                            hunks keep it; the clerk just stops offering it
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
NOTES = COMPOST / "notes"
DB = COMPOST / "compost.db"
STATIC = Path(__file__).parent / "static"
PORT = 8771
CLASS_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
HUNK_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")  # a path is built from this

AUDIO_TYPES = {".m4a": "audio/mp4", ".qta": "audio/mp4", ".wav": "audio/wav"}


TAGS_SUB = "(SELECT group_concat(tag, ',') FROM tags WHERE hunk_id = e.id) AS tags"


def class_filter(conn, cls: str):
    """Honors classes actually worn by hunks, not just the live roster —
    retired classes never vanish from history, so never from here."""
    roster = [r[0] for r in conn.execute("SELECT name FROM classes ORDER BY rowid")]
    present = {r[0] for r in conn.execute("SELECT DISTINCT class FROM hunks")}
    selected = [c for c in cls.split(",") if c in (set(roster) | present)]  # unknown names ignored
    return roster, present, selected


def row_filter(selected: list, marked: bool, archived: bool = False) -> tuple[list, list]:
    """Conditions for the filters, ANDed. Class, mark and archived are separate
    axes — a marked hunk keeps whatever class it wears, compost included.

    Archived is always pinned one way or the other, never left open: the default
    pile excludes it, and asking for it shows that and nothing else. There is
    no view that mixes the two, so something put out of sight stays out of
    sight until you go looking."""
    conds = ["e.archived = 1" if archived else "e.archived = 0"]
    params = []
    if selected:
        conds.append(f"e.class IN ({','.join('?' * len(selected))})")
        params += selected
    if marked:
        conds.append("e.status = 'promoted'")
    return conds, params


FLAGS = {"status", "archived"}   # the only flag columns the page may write
# A knowable ceiling the page can show a count against — keep it in step with
# MAX_NOTES in index.html. Generous for notes on a memo; raise it if it bites.
MAX_NOTES = 10_000


def set_notes(hunk_id: str, text: str) -> dict | None:
    """Save the reader's own writing for one hunk.

    It lives in notes/<id>.md, a sibling of the transcript, and never touches
    it. The transcript is what the machine heard; the notes are what you made
    of it, and keeping them apart is what lets you re-transcribe later without
    destroying your thinking — and what lets you still tell what you actually
    said from what you decided afterwards you meant.
    """
    if not (META / hunk_id[:4] / hunk_id[4:6] / f"{hunk_id}.json").is_file():
        return None
    text = text.strip()
    dp = NOTES / hunk_id[:4] / hunk_id[4:6] / f"{hunk_id}.md"
    if text:
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(text)
    elif dp.is_file():
        dp.unlink()   # cleared by hand: don't leave an empty file behind
    try:
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE hunks SET notes = ? WHERE id = ?", (text, hunk_id))
        row = conn.execute(
            "SELECT transcript, title FROM hunks WHERE id = ?", (hunk_id,)).fetchone()
        conn.execute("DELETE FROM hunks_fts WHERE id = ?", (hunk_id,))
        conn.execute("INSERT INTO hunks_fts (id, transcript, title, notes) VALUES (?,?,?,?)",
                     (hunk_id, row[0] or "", row[1] or "", text))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # db is disposable; the file is written, so rebuild agrees
    return load_hunk(hunk_id)


def set_flag(hunk_id: str, field: str, value) -> dict | None:
    """Write one flag to the hunk's meta json, then the db.

    Meta is truth: the json is what survives a rebuild, the db write only
    keeps the open page honest. Both flags are the reader's, not the clerk's,
    and neither touches anything the pipeline produced — not the transcript,
    not the class, not the tags. Nothing moves on disk either, which is what
    keeps ingest's idempotency (meta/*.json alone) intact.
    """
    if field not in FLAGS:
        return None
    mp = META / hunk_id[:4] / hunk_id[4:6] / f"{hunk_id}.json"
    if not mp.is_file():
        return None
    m = json.loads(mp.read_text())
    m[field] = value
    mp.write_text(json.dumps(m, indent=2))
    try:
        conn = sqlite3.connect(DB)
        conn.execute(f"UPDATE hunks SET {field} = ? WHERE id = ?", (value, hunk_id))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # db is disposable; the next rebuild picks it up from meta
    return load_hunk(hunk_id)


def load_hunk(hunk_id: str = "", cls: str = "", marked: bool = False,
               archived: bool = False):
    """One whole hunk — full transcript, every column, tags joined.
    No hunk_id means pick at random from whatever the filters allow."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if hunk_id:
        row = conn.execute(
            f"SELECT e.*, {TAGS_SUB} FROM hunks e WHERE e.id = ?", (hunk_id,)).fetchone()
    else:
        _, _, selected = class_filter(conn, cls)
        # random is random: the filters narrow the pile, nothing weights the draw
        conds, params = row_filter(selected, marked, archived)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        row = conn.execute(
            f"SELECT e.*, {TAGS_SUB} FROM hunks e{where} ORDER BY RANDOM() LIMIT 1",
            params).fetchone()
    conn.close()
    return dict(row) if row else None


def query_db(q: str, year: str = "", cls: str = "", marked: bool = False,
             archived: bool = False, limit: int = 100):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = {}
    roster, present, selected = class_filter(conn, cls)
    conds, fparams = row_filter(selected, marked, archived)
    cfilter = "".join(f" AND {c}" for c in conds)
    tags_sub = TAGS_SUB
    if q.strip():
        # quote each term so user input can't break FTS5 syntax
        match = " ".join(f'"{t}"' for t in re.findall(r"[^\s\"]+", q))
        rows = conn.execute(
            f"""SELECT e.*, snippet(hunks_fts, -1, '<mark>', '</mark>', '…', 24) AS snip, {tags_sub}
               FROM hunks_fts f JOIN hunks e ON e.id = f.id
               WHERE hunks_fts MATCH ?{cfilter} ORDER BY e.created_at DESC LIMIT ?""",
            (match, *fparams, limit)).fetchall()
        out["results"] = [dict(r) for r in rows]
    elif year:
        rows = conn.execute(
            f"SELECT e.*, substr(COALESCE(NULLIF(e.transcript, ''), e.notes, ''), 1, 220) AS snip,"
            f" {tags_sub} FROM hunks e "
            f"WHERE substr(e.created_at, 1, 4) = ?{cfilter} ORDER BY e.created_at DESC",
            (year, *fparams)).fetchall()
        out["results"] = [dict(r) for r in rows]
    else:
        # FILTER keeps zero-count years in the list — the archive's shape stays visible
        filt = f" FILTER (WHERE {' AND '.join(conds)})" if conds else ""
        counts = conn.execute(
            f"SELECT substr(e.created_at, 1, 4), COUNT(*){filt} "
            f"FROM hunks e GROUP BY 1 ORDER BY 1 DESC", fparams)
        out["years"] = [{"year": y, "count": c} for y, c in counts]
        out["classes"] = [c for c in roster if c in present] + sorted(present - set(roster))
    total = conn.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM hunks e "
        f"WHERE {conds[0]}").fetchone()   # conds[0] is always the archived pin
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
                                       qs.get("mark", [""])[0] == "1",
                                       qs.get("archived", [""])[0] == "1")).encode()
            self._send(200, "application/json", body)
        elif url.path in ("/api/hunk", "/api/random"):
            qs = urllib.parse.parse_qs(url.query)
            hunk = load_hunk(qs.get("id", [""])[0] if url.path == "/api/hunk" else "",
                               qs.get("cls", [""])[0], qs.get("mark", [""])[0] == "1",
                               qs.get("archived", [""])[0] == "1")
            if hunk is None:
                self._send(404, "text/plain", b"no such hunk")
                return
            self._send(200, "application/json", json.dumps(hunk).encode())
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

    def _flag(self, path: str):
        """Everything the page may write to an hunk. Mark says there is
        something here, archived says keep it out of my view, notes are the
        reader's own writing. None of the three touches what the pipeline
        produced — not the transcript, not the class, not the tags."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            hunk_id = str(body.get("id", ""))
        except (json.JSONDecodeError, ValueError):
            hunk_id, body = "", {}
        if not HUNK_ID_RE.match(hunk_id):
            self._send(400, "text/plain", b"bad hunk id")
            return
        if path == "/api/notes":
            text = body.get("text", "")
            if not isinstance(text, str) or len(text) > MAX_NOTES:
                self._send(400, "text/plain",
                           f"notes must be text under {MAX_NOTES} chars".encode())
                return
            hunk = set_notes(hunk_id, text)
        elif path == "/api/archive":
            hunk = set_flag(hunk_id, "archived", 1 if body.get("archive") else 0)
        else:
            hunk = set_flag(hunk_id, "status",
                             "promoted" if body.get("mark") else "raw")
        if hunk is None:
            self._send(404, "text/plain", b"no such hunk")
            return
        self._send(200, "application/json", json.dumps(hunk).encode())

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/reveal":
            self._reveal()
            return
        if path in ("/api/mark", "/api/archive", "/api/notes"):
            self._flag(path)
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
        del classes[name]  # falls forward only — filed hunks keep their label
        save_classes(classes)
        self._send(200, "application/json", json.dumps(classes).encode())


if __name__ == "__main__":
    print(f"compost on http://127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
