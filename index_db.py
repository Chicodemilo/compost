#!/usr/bin/env python3
"""COMPOST — Phase 2 index.

Builds ~/compost/compost.db from the files. The DB is disposable: `rebuild`
wipes and reconstructs it from meta/ + transcripts/ alone, and that must
always produce identical results.

Usage:
    index_db.py rebuild
"""

import json
import sqlite3
import sys
from pathlib import Path

COMPOST = Path.home() / "compost"
DB = COMPOST / "compost.db"

# Classes are DB-NATIVE — a deliberate exception to "files are truth".
# Rebuild carries them across from the previous db; these defaults only seed
# a db built from nothing. Customize the roster from the search page.
DEFAULT_CLASSES = {
    "song": "singing, humming, melodies, lyric fragments (garbled transcripts of singing are still songs)",
    "idea": "a clearly articulated concept, invention, or plan",
    "work": "day-job or professional matters",
    "compost": ("THE DEFAULT. Anything unclear, mixed, mundane, or where you are not confident "
                "belongs here. Bias hard toward compost — a wrong confident label is worse than punting."),
}

SCHEMA = """
CREATE TABLE hunks (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT,
    class       TEXT NOT NULL,
    transcript  TEXT,
    notes       TEXT,
    audio_path  TEXT,
    lat         REAL,
    lon         REAL,
    place       TEXT,
    duration    REAL,
    status      TEXT DEFAULT 'raw',
    archived      INTEGER DEFAULT 0,
    indexed_at  TEXT
);
CREATE TABLE tags (
    hunk_id    TEXT NOT NULL REFERENCES hunks(id),
    tag         TEXT NOT NULL,
    PRIMARY KEY (hunk_id, tag)
);
-- notes goes last: transcript stays column 1 for anything addressing it by index
CREATE VIRTUAL TABLE hunks_fts USING fts5(
    id UNINDEXED, transcript, title, notes, tokenize='porter'
);
CREATE TABLE classes (
    name        TEXT PRIMARY KEY,
    writeup     TEXT NOT NULL
);
"""


def rebuild() -> int:
    from datetime import datetime, timezone
    tmp = DB.with_suffix(".db.new")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for mp in sorted((COMPOST / "meta").rglob("*.json")):
        try:
            m = json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        stem = mp.relative_to(COMPOST / "meta")
        tp = COMPOST / "transcripts" / stem.with_suffix(".txt")
        transcript = tp.read_text().strip() if tp.exists() else ""
        # notes are the reader's own writing, kept beside the transcript and
        # never merged into it — see §9. Same stem, so it rebuilds like the rest.
        dp = COMPOST / "notes" / stem.with_suffix(".md")
        notes = dp.read_text().strip() if dp.exists() else ""
        conn.execute(
            "INSERT OR REPLACE INTO hunks (id, created_at, source, title, class, transcript,"
            " notes, audio_path, lat, lon, place, duration, status, archived, indexed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m["created_at"], m.get("source", "vm"), m.get("title"),
             m.get("class", "compost"), transcript, notes, m.get("audio_path"), m.get("lat"),
             m.get("lon"), m.get("place"), m.get("duration"), m.get("status", "raw"),
             int(m.get("archived") or 0), now),
        )
        conn.execute("INSERT INTO hunks_fts (id, transcript, title, notes) VALUES (?,?,?,?)",
                     (m["id"], transcript, m.get("title") or "", notes))
        for t in m.get("tags", []):
            conn.execute("INSERT OR IGNORE INTO tags VALUES (?,?)", (m["id"], t))
        n += 1
    try:
        old = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        classes = dict(old.execute("SELECT name, writeup FROM classes").fetchall())
        old.close()
    except sqlite3.Error:
        classes = {}
    conn.executemany("INSERT INTO classes VALUES (?,?)", (classes or DEFAULT_CLASSES).items())
    conn.commit()
    conn.close()
    tmp.replace(DB)  # atomic swap — searchers never see a half-built index
    return n


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rebuild":
        print(f"indexed {rebuild()} hunks -> {DB}")
    else:
        sys.exit(__doc__.strip())
