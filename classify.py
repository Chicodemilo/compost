#!/usr/bin/env python3
"""COMPOST — Phase 3 classify + tag.

Tool surface is exactly three verbs (spec §7): classify, tag, route.
The LLM backend is headless Claude Code (`claude -p`) behind one function —
swap _llm() to change providers.

Usage:
    classify.py backfill        classify+tag every entry not yet classified
    classify.py backfill N      ... at most N entries (for sampling)
    classify.py one <id>        classify+tag a single entry by id
"""

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

COMPOST = Path.home() / "compost"
META = COMPOST / "meta"
TRANSCRIPTS = COMPOST / "transcripts"
DB = COMPOST / "compost.db"
CLAUDE = "/opt/homebrew/bin/claude"
MODEL = "haiku"          # cheap and fast; classification needs no heavyweight
MAX_CHARS = 6000         # an hour-long ramble classifies fine from its first stretch


def load_classes() -> dict[str, str]:
    """Class roster is db-native (the one deliberate exception to files-are-truth)
    and CRUD'd from the search page. Edits fall forward only: they shape future
    filing, never already-filed entries."""
    from index_db import DEFAULT_CLASSES
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        classes = dict(conn.execute("SELECT name, writeup FROM classes").fetchall())
        conn.close()
    except sqlite3.Error:
        classes = {}
    if not classes:
        classes = dict(DEFAULT_CLASSES)
    classes["compost"] = classes.get("compost") or DEFAULT_CLASSES["compost"]  # the punt bucket is load-bearing
    return classes


PROMPT = """You are the filing clerk for COMPOST, a personal voice-memo pile. \
Classify the transcript below and extract tags. Reply with ONLY a JSON object, \
no other text: {{"class": "...", "tags": ["...", "..."]}}

class — exactly one of:
{class_lines}

tags — up to 7 lowercase keywords lifted from the content itself: concrete nouns, \
names, places, subjects. Never filler words (you, that, well, thanks). If the \
transcript is too thin for meaningful tags, return fewer or an empty list — \
junk tags are worse than none. No invention, no summarizing, no titles.

Transcript:
---
{transcript}
---"""

_cache: dict[str, dict] = {}


def _llm(transcript: str) -> dict:
    """One call, one JSON result. Anything that goes wrong routes to compost."""
    if transcript in _cache:
        return _cache[transcript]
    fallback = {"class": "compost", "tags": []}
    classes = load_classes()  # fresh each call so roster edits apply without restart
    lines = "\n".join(f"- {n}: {w}" for n, w in classes.items() if n != "compost")
    lines += f"\n- compost: {classes['compost']}"
    try:
        out = subprocess.run(
            [CLAUDE, "-p", "--model", MODEL],
            input=PROMPT.format(class_lines=lines, transcript=transcript[:MAX_CHARS]),
            capture_output=True, text=True, timeout=180,
        ).stdout
        m = re.search(r"\{.*\}", out, re.DOTALL)
        raw = json.loads(m.group(0)) if m else {}
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        raw = {}
    cls = raw.get("class")
    tags = raw.get("tags")
    result = fallback if cls not in classes or not isinstance(tags, list) else {
        "class": cls,
        "tags": [t.strip().lower() for t in tags if isinstance(t, str) and t.strip()][:7],
    }
    _cache[transcript] = result
    return result


def classify(transcript: str) -> str:
    return _llm(transcript)["class"]


def tag(transcript: str) -> list[str]:
    return _llm(transcript)["tags"]


def _meta_path(entry_id: str) -> Path:
    return META / entry_id[:4] / entry_id[4:6] / f"{entry_id}.json"


def route(entry_id: str, cls: str) -> None:
    """Write class to meta + db. Meta is truth; the db update just keeps search fresh."""
    mp = _meta_path(entry_id)
    m = json.loads(mp.read_text())
    m["class"] = cls
    mp.write_text(json.dumps(m, indent=2))
    try:
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE entries SET class = ? WHERE id = ?", (cls, entry_id))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass  # db is disposable; next index rebuild picks it up from meta


def apply(entry_id: str) -> dict:
    """Classify+tag one entry: meta gets class, tags, classified_at; db follows."""
    tp = TRANSCRIPTS / entry_id[:4] / entry_id[4:6] / f"{entry_id}.txt"
    transcript = tp.read_text().strip() if tp.exists() else ""
    result = _llm(transcript) if transcript else {"class": "compost", "tags": []}
    route(entry_id, result["class"])
    mp = _meta_path(entry_id)
    m = json.loads(mp.read_text())
    m["tags"] = result["tags"]
    m["classified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mp.write_text(json.dumps(m, indent=2))
    try:
        conn = sqlite3.connect(DB)
        conn.execute("DELETE FROM tags WHERE entry_id = ?", (entry_id,))
        conn.executemany("INSERT OR IGNORE INTO tags VALUES (?,?)",
                         [(entry_id, t) for t in result["tags"]])
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    return result


def backfill(limit: int | None = None) -> None:
    todo = []
    for mp in sorted(META.rglob("*.json")):
        try:
            m = json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "classified_at" not in m:
            todo.append(m["id"])
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} entries to classify")
    for i, eid in enumerate(todo, 1):
        r = apply(eid)
        print(f"[{i}/{len(todo)}] {eid} -> {r['class']} {r['tags']}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif len(sys.argv) > 2 and sys.argv[1] == "one":
        print(apply(sys.argv[2]))
    else:
        sys.exit(__doc__.strip())
