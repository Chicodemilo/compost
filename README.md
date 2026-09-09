# COMPOST 🌱

**Your voice memos, transcribed, searchable, and tagged — and nothing ever leaves your machine.**

Ideas arrive constantly and unpredictably. Capture works fine (press-and-hold →
Voice Memos). Retrieval is broken: everything lands in one undifferentiated list
you can only scroll. Most entries are bad, and the good ones are buried behind
them. COMPOST fixes retrieval without touching capture.

Record a memo on your phone. iCloud syncs it to your Mac. COMPOST notices,
transcribes it locally with whisper.cpp, files it under a class with a few
keyword tags, and indexes it for full-text search — hands-free, minutes later,
with the audio playable inline next to its transcript.

```
Voice Memos (iCloud) → watch → copy → whisper.cpp → classify+tag → SQLite FTS5 → search page
```

## Design principles

1. **Capture is disposable, the pipeline is permanent.** Swap capture hardware
   any time; the pipeline doesn't care what made the audio file.
2. **Never sort at capture time.** One gesture, no decisions. The machine sorts
   downstream.
3. **Files are the source of truth; the database is an index.** Delete
   `compost.db` and rebuild it from the folder at any time.
4. **The AI sorts. It does not have opinions.** No rewriting, summarizing, or
   "improving" your material.
5. **Uncertainty routes to `compost`** — the default class and the honest
   unsorted pile, which is the most interesting part of the system.
6. **Nothing is ever deleted.**

The full design document is in [`compost-rev1-spec.md`](compost-rev1-spec.md).

## What's in the box

- `ingest.py` — the pipeline: watches the Voice Memos dir (launchd WatchPaths +
  a 30-minute backstop), copies new recordings, transcribes with whisper.cpp
  large-v3, writes transcript + meta files. Idempotent; failures quarantine to
  `~/compost/inbox_failed/`; iCloud ghost placeholders get a strike system so
  they don't burn cycles forever. `ingest.py doctor` checks the installation.
- `classify.py` — the filing clerk: one LLM call per memo assigns a class and
  up to 7 content-lifted tags. Backed by headless Claude Code (`claude -p`),
  swappable behind one function. Biases hard toward the `compost` class —
  a wrong confident label is worse than punting. `classify.py backfill`
  catches up an existing archive. Entirely optional: everything else works
  untagged.
- `index_db.py` — builds the SQLite FTS5 index from the files. Atomic swap;
  disposable by design.
- `server.py` — stdlib-only search/browse server on `localhost:8771`: full-text
  search across all years, a collapsible year-by-year browse view (entries lazy-
  load per year), inline audio with seek support, and a CRUD panel for the
  class roster (edits fall forward — refiling history is your call, not a side
  effect).
- `static/index.html` — the search page. One file, vanilla JS, no build step.

**Explicitly not used:** Docker, Node, React, any cloud service, any hosted DB,
any auth layer. Python stdlib + two `brew` installs.

## Requirements

- Apple-silicon Mac with Voice Memos syncing via iCloud
- Homebrew Python 3.12+ (a framework build — the stock Homebrew python is)
- `brew install whisper-cpp`
- The whisper model: download
  [`ggml-large-v3.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main)
  to `~/compost/models/`
- Optional, for auto-classify/tag: [Claude Code](https://claude.com/claude-code)
  (`brew install claude`)

## Install

```sh
git clone <this repo> && cd compost
./install.sh
```

The installer renders the launchd templates for your machine, loads both
agents, and prints the one step it can't do for you: granting **Full Disk
Access** to Python.app (the Voice Memos folder is TCC-protected; the grant
must go on the app bundle, and the agent launches its inner binary — plain
`python3` won't inherit it).

Then verify with `python3 ingest.py doctor`, record a memo on your phone, and
watch `~/compost/logs/ingest.log`. The search page is at
<http://127.0.0.1:8771>.

Everything lives in `~/compost/` — audio copies, transcripts, meta, the index,
logs. Back that folder up and you've backed up everything.

## License

MIT
