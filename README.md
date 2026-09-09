# COMPOST

A local pipeline that turns Apple Voice Memos into a searchable text archive.

When a new memo syncs to the Mac via iCloud, a launchd agent copies it,
transcribes it with whisper.cpp, assigns it a class and a few keyword tags,
and indexes it for full-text search. A small local web page searches and
plays the archive. Everything runs on the machine; no network calls except
the optional tagging step.

```
Voice Memos (iCloud) → watch → copy → whisper.cpp → classify+tag → SQLite FTS5 → search page
```

Written for one person's archive (460 memos, 2013–present). Published because
the problem — years of voice memos in one unsearchable list — is common enough
that the plumbing might save someone the evening it takes to build.

## How it's put together

- `ingest.py` — watches the Voice Memos directory (launchd WatchPaths plus a
  30-minute timer as backstop). New recordings are copied out, transcribed
  with whisper.cpp large-v3, and written as three files sharing one id:
  audio, transcript, meta JSON. Idempotent; re-running is a no-op. Failures
  quarantine to `~/compost/inbox_failed/`. Zero-byte iCloud placeholders
  (recordings deleted on the phone) are given two chances, then checked with
  a single cheap stat per run. `ingest.py doctor` checks the installation.
- `classify.py` — one LLM call per memo assigns a class and up to 7 keyword
  tags lifted from the transcript. Backed by headless Claude Code
  (`claude -p`); the backend is one function, swap it for whatever you have.
  Low confidence routes to the default class rather than guessing.
  `classify.py backfill` processes an existing archive. Optional — everything
  else works without it.
- `index_db.py` — builds the SQLite FTS5 index from the files. The database
  is disposable: delete it and `rebuild` reconstructs it from the folder.
- `server.py` — stdlib-only server on `localhost:8771`. Full-text search
  (porter-stemmed), a year-by-year collapsible browse view, class filter
  chips, inline audio playback with seeking, and a panel for editing the
  class list. Class edits apply to future filing only; already-filed entries
  keep their labels.
- `static/index.html` — the page. One file, vanilla JS, no build step.

Everything lands in `~/compost/` — audio, transcripts, meta, index, logs.
Backing up that one folder backs up the archive.

Design decisions are written down in
[`compost-rev1-spec.md`](compost-rev1-spec.md), including the ones that
constrain the code: files are the source of truth, the classifier defaults
to an unsorted pile rather than guessing, nothing is ever deleted.

## Requirements

- A Mac with Voice Memos syncing from a phone via iCloud
- Homebrew Python 3.12+ (needs a framework build; Homebrew's is)
- `brew install whisper-cpp`, plus the model:
  [`ggml-large-v3.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main)
  in `~/compost/models/`
- For the optional classify/tag step: [Claude Code](https://claude.com/claude-code)

No Docker, no Node, no cloud services, no accounts. Python stdlib throughout.

## Install

```sh
git clone <this repo> && cd compost
./install.sh
```

The script renders the launchd templates for your paths and loads both
agents, then prints the one step it can't do: granting Full Disk Access to
Python.app. The Voice Memos folder is TCC-protected, the grant has to go on
the app bundle, and the agent has to launch the bundle's inner binary —
plain `python3` doesn't inherit the grant. The plist templates and
`install.sh` handle the details; the README-visible consequence is one trip
to System Settings.

Verify with `python3 ingest.py doctor`, record a memo on the phone, and watch
`~/compost/logs/ingest.log`. The page is at <http://127.0.0.1:8771>.

## License

MIT
