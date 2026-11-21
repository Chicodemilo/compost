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

Written for one person's archive, a decade-plus deep. Published because
the problem — years of voice memos in one unsearchable list — is common enough
that the plumbing might save someone the evening it takes to build.

## How it's put together

- `ingest.py` — watches the Voice Memos directory (launchd WatchPaths plus a
  30-minute timer as backstop). New recordings are copied out, transcribed
  with whisper.cpp large-v3, and written as three files sharing one id:
  audio, transcript, meta JSON. The recording's own name travels with it —
  read straight out of the MP4 atom, no ffmpeg — and `ingest.py titles`
  backfills it onto anything filed before that was kept. Idempotent; re-running is a no-op. Failures
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
  Search covers transcripts and titles both — Voice Memos names a recording
  after where it was made, so the name of a bar finds the night you were in
  it, not just the words you said.
- `server.py` — stdlib-only server on `localhost:8771`. Full-text search
  (porter-stemmed), a year-by-year collapsible browse view, class filter
  chips, inline audio playback with seeking, and a panel for editing the
  class list. Every row carries a mark — `[ ]` until you flag it, `[•]` after —
  meaning only *there is something here*. It is a third axis, not a tag and not
  a rating: one bit, one direction, no scale and no opposite, written only by
  you and filtered on its own. It lands in the entry's meta JSON, so it
  survives `rebuild` like everything else that matters. A dark-orange `[x]`
  beside it puts an entry out of view in one click, and it leaves the pile
  entirely: out of search, out of the year counts, out of the header total,
  never drawn by `random`. Nothing moves on disk and nothing is deleted; the
  `hidden` chip shows that pile, where `[+]` puts an entry back in one tap. Class edits apply to future filing only; already-filed entries
  keep their labels. An active-post box sits above the search: `random`
  pulls one entry out of the pile (honoring whatever class chips are lit),
  clicking any entry's date promotes it, and the box holds it — whole
  transcript untruncated in a scrollable pane, every column the schema
  carries, and a clickable audio path that reveals the file in Finder.
  The active post also carries a **draft** — a self-saving text box where you
  work an idea out. It sits beside the transcript and never overwrites it: the
  transcript is what the machine heard, the draft is what you made of it, and
  only keeping them apart lets you re-transcribe later without losing your
  thinking. Drafts are plain `.md` files under `drafts/`, indexed alongside
  transcripts so search finds your writing too. Under it, a one-line field
  with `↑ top` / `↓ end` adds a dated line without touching what is already
  there. That makes the fast path additive, though the box stays editable, so
  it is a habit rather than a guarantee. For a real guarantee, drafts are
  plain files: `git init ~/compost` with `audio/` ignored costs nothing.
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
