# COMPOST

A local pipeline that turns Apple Voice Memos into a searchable archive, and
a place to work the good ones into something.

When a new memo syncs to the Mac via iCloud, a launchd agent copies it,
transcribes it with whisper.cpp, assigns it a class and a few keyword tags,
and indexes it for full-text search. A small local web page searches and
plays the archive, and opens any recording into a writing surface with a
chord sheet and a circle of fifths in it.

```
Voice Memos (iCloud) → watch → copy → whisper.cpp → classify+tag → SQLite FTS5 → search page
```

Written for one person's archive, a decade-plus deep. Published because
the problem — years of voice memos in one unsearchable list — is common enough
that the plumbing might save someone the evening it takes to build.

## What things are called

A **hunk** is one unit of compost: a recording, its transcript, and whatever
you have written about it. Not an entry — a hunk has mass, which is what you
are hunting for in the pile, and a half-sung fragment really is a hunk of a
song that does not exist yet.

The **pile** is all of them. **compost** is the default class and the most
interesting one: the unresolved heap, not a failure bucket.

## The pipeline

- `ingest.py` — watches the Voice Memos directory (launchd WatchPaths plus a
  30-minute timer as backstop). New recordings are copied out, transcribed
  with whisper.cpp large-v3, and written as three files sharing one id:
  audio, transcript, meta JSON. The recording's own name travels with it —
  read straight out of the MP4 atom, no ffmpeg — and `ingest.py titles`
  backfills it onto anything filed before that was kept. Idempotent;
  re-running is a no-op. Failures quarantine to `~/compost/inbox_failed/`.
  Zero-byte iCloud placeholders (recordings deleted on the phone) get two
  chances, then one cheap stat per run. `ingest.py doctor` checks the install.
- `classify.py` — one LLM call per memo assigns a class and up to 7 keyword
  tags lifted from the transcript. Backed by headless Claude Code
  (`claude -p`); the backend is one function, swap it for whatever you have.
  Low confidence routes to `compost` rather than guessing. `classify.py
  backfill` processes an existing archive. Optional — everything else works
  without it.
- `index_db.py` — builds the SQLite FTS5 index from the files. The database
  is disposable: delete it and `rebuild` reconstructs it from the folder.
  Search covers transcripts, titles and your own notes. Voice Memos names a
  recording after where it was made, so the name of a bar finds the night you
  were in it, not just the words you said.
- `server.py` — stdlib-only server on `localhost:8771`.
- `static/index.html` — the page. One file, vanilla JS, no build step.

Everything lands in `~/compost/` — audio, transcripts, notes, meta, index,
logs. Backing up that one folder backs up the archive.

## The page

Full-text search (porter-stemmed), a year-by-year collapsible browse view,
class filter chips, inline audio with seeking, and a panel for editing the
class list. Class edits apply to future filing only; already-filed hunks keep
their labels.

Every row carries two controls at its right edge:

- **`[ ]` → `[•]` the mark.** One bit meaning *there is something here*. Not a
  tag and not a rating: one direction, no scale, no opposite, written only by
  you and filtered on its own chip. Absence of the mark is the only negative
  signal there is.
- **`[archive]`** puts a hunk out of view in one click. It leaves the pile
  completely — out of search, out of the year counts, out of the header total,
  never drawn by `random`. Nothing moves on disk and nothing is deleted: the
  `archived` chip shows that pile, where `[back]` returns a hunk in one tap.

`random` pulls one hunk out of the pile, honoring whatever chips are lit.
Random is random — the filters narrow the pile and nothing weights the draw.

## The active hunk

Clicking a hunk's date makes it active, in a box above the search that holds
it until you clear it. Whole transcript untruncated, every column the schema
carries, and a clickable audio path that reveals the file in Finder.
`[<->]` fills the screen with it; `[x]` puts it away; escape leaves the big
view without losing your place.

It also carries **notes** — a self-saving text box where you work an idea
out, capped at 10,000 characters with the count beside the label. Notes sit
*beside* the transcript and never overwrite it: the transcript is what the
machine heard, the notes are what you made of it, and only keeping them apart
lets you re-transcribe with a better model later without destroying your
thinking. They are plain `.md` files under `notes/`, indexed alongside
transcripts so search finds your writing too.

## The ribbon

In the big view, a strip of tools along the bottom, in tabs:

```
/songs\__/text\__/file\______________________________________
|  chord rows  [show]  [clear]  |  circle of 5ths  [open]  |
```

**songs** — `[show]` opens a **chord row** above every line of words. Not
another line of text: a lane, with chords in it as chips you drag left and
right over the syllables. Anything with a space between it is its own chip,
so a bridge is a run of them. A chip's position is its character column,
which is why the file stays an ordinary chord sheet — dragging a chord is
changing how many spaces precede it. `[clear]` takes out every chord row and
leaves the words alone.

`circle of 5ths` slides a wheel into the right of the sheet: scale degrees
outermost, majors inside them, relative minors within that. Changing key
rotates both rings while `I / IV / V` stay put — the circle is the maths, so
there is no lookup table. With the chord rows open too, the wheel becomes a
picker: put the cursor in a chord row and click a chord to drop it there.

**text** — `[bold]` and `[italic]` on the highlighted words. Notes are `.md`,
so these write the markdown the file already speaks, and applying a mark that
is already there takes it off.

**file** — `[open as pdf]` opens a printable sheet and calls print, so *Save
as PDF* is one dialog away; chord rows print bold. `[copy as markdown]` puts
the sheet on the clipboard with a title and date. `[reveal in finder]` shows
the recording.

Playback lives with the player rather than the ribbon: `[play]`, `[-5s]`,
`[loop]`, and a rate cycle through 1x / 0.75x / 0.5x for catching a mumbled
line.

## Design notes

Decisions are written down in
[`compost-rev1-spec.md`](compost-rev1-spec.md), including the ones that
constrain the code: files are the source of truth and the database is a
disposable index; the classifier defaults to an unsorted pile rather than
guessing; nothing is ever deleted. §11 lists the known gotchas, including the
one the wheel cannot avoid — each position carries a single spelling, so far
round the circle you will read `Ab` where the theory wants `G#`.

For history on your writing, the notes are plain files: `git init ~/compost`
with `audio/` ignored costs nothing.

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
