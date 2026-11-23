# COMPOST — Rev 1 Spec

**Personal idea capture & retrieval system**
Target: Apple-silicon Mac, recent macOS
Date: 2026-09-02

---

## 1. The problem

Ideas arrive constantly and unpredictably — song fragments, app concepts,
bits for [WHTC](https://thewitchinghourtone.club), a
[show](https://www.youtube.com/@TheWitchingHourToneClub) about old forgotten 78s
and new under-heard songs, and work thoughts. Existing capture works fine
(press-and-hold → Voice Memos). **Retrieval is broken.**
Everything lands in one undifferentiated list that has to be scrolled linearly. Most entries are
bad. The good ones are unreachable because they're buried behind the bad ones.

Rev 1 solves retrieval. It does not touch capture.

---

## 2. Design principles

These are load-bearing. Do not optimize them away.

1. **Capture is disposable, the pipeline is permanent.** The system must not care what created
   the audio file. Swapping capture hardware later must require zero pipeline changes.
2. **Never sort at capture time.** One intake, one gesture, no decisions. Classification happens
   downstream, by machine.
3. **Files are the source of truth. The database is an index.** Deleting the DB and rebuilding it
   from the folder must always be a valid recovery path.
4. **The AI sorts. It does not have opinions.** No rewriting, summarizing, or "improving"
   content. The reader may write as much as they like — in their notes (§9), which are a
   separate field. Machine output and human output never share a box.
5. **Uncertainty routes to `compost`.** An honest unsorted pile beats a confident wrong folder.
6. **Nothing is ever deleted.** No `dead` state, no trash, no expiry, no grade that condemns
   an entry. Material either gets used or sits. A person may put an entry **out of view** (§9),
   which is a different act entirely: a flag, reversible in one click, with every file left
   exactly where it was. Nothing in this repo unlinks an entry.

---

## 3. Architecture

```
iPhone (press-and-hold → Voice Memos)
        │
        │  iCloud sync (already working, no code)
        ▼
   ~/Library/Group Containers/group.com.apple.VoiceMemos/Recordings/
        │
        ▼
   [1] WATCHER          launchd WatchPaths → detects new .m4a
        │
        ▼
   [2] INGEST           copy to compost store, generate id, extract mtime
        │
        ▼
   [3] TRANSCRIBE       whisper.cpp large-v3, local, offline
        │
        ▼
   [4] CLASSIFY+TAG     LLM, 3 verbs only, defaults to compost
        │
        ▼
   [5] INDEX            SQLite + FTS5
        │
        ▼
   [6] SEARCH UI        static HTML on localhost, audio playback inline
```

---

## 4. Stack

| Component | Choice | Notes |
|---|---|---|
| Watcher | `launchd` WatchPaths plist | Preferred over `fswatch` — no daemon to babysit, survives reboot |
| Transcription | `whisper.cpp`, `large-v3` | Local. 48GB RAM handles it easily. No API cost, works offline |
| Classifier | LLM call | Local model or API — implementer's choice, must be swappable behind one function |
| Index | SQLite + FTS5 | Bundled with macOS. FTS5 gives full-text search for free |
| UI | Single static HTML + vanilla JS | Served by `python3 -m http.server` or equivalent. No framework, no build step, no auth, no deploy |
| Language | Python 3 | Everything except the plist |

**Explicitly not using:** Docker, Node, React, any cloud service, any hosted DB, any auth layer.

---

## 5. Storage layout

```
~/compost/
├── audio/              # copies of source audio, never modified
│   └── 2026/09/
│       └── 20260902-143312-a3f9.m4a
├── transcripts/        # one .txt per entry, same stem as audio — machine output
│   └── 2026/09/
│       └── 20260902-143312-a3f9.txt
├── notes/              # one .md per entry — the reader's own writing (§9)
│   └── 2026/09/        #   absent until you write something
│       └── 20260902-143312-a3f9.md
├── meta/               # one .json per entry — tags, class, location, source
│   └── 2026/09/
│       └── 20260902-143312-a3f9.json
├── compost.db          # SQLite index — DISPOSABLE, rebuildable
├── logs/
└── inbox_failed/       # anything the pipeline choked on, with error log
```

**ID format:** `YYYYMMDD-HHMMSS-<4 hex>`. Sortable, human-readable, collision-safe.

Audio, transcript, and meta share a stem. Given any one, the others are derivable. This is what
makes the DB disposable.

---

## 6. Data model

```sql
CREATE TABLE entries (
    id          TEXT PRIMARY KEY,     -- 20260902-143312-a3f9
    created_at  TEXT NOT NULL,        -- ISO8601, from file mtime
    source      TEXT NOT NULL,        -- 'vm' | 'note'  (note = rev 2)
    class       TEXT NOT NULL,        -- see §7
    transcript  TEXT,                 -- may be empty or garbled — that is OK
    notes       TEXT,                 -- the reader's writing, never the machine's, §9
    audio_path  TEXT,                 -- relative to ~/compost/
    lat         REAL,                 -- nullable
    lon         REAL,                 -- nullable
    place       TEXT,                 -- nullable, human-readable if available
    duration    REAL,                 -- seconds
    status      TEXT DEFAULT 'raw',   -- 'raw' | 'promoted'  — the mark, §9
    hidden      INTEGER DEFAULT 0,    -- out of view but not gone, §9
    indexed_at  TEXT
);

CREATE TABLE tags (
    entry_id    TEXT NOT NULL REFERENCES entries(id),
    tag         TEXT NOT NULL,
    PRIMARY KEY (entry_id, tag)
);

CREATE VIRTUAL TABLE entries_fts USING fts5(
    id UNINDEXED, transcript, tokenize='porter'
);
```

`source` exists so rev 2 (Apple Notes intake) writes into the same table with no migration.

---

## 7. Classification

**Classes that ship:** `song`, `idea`, `work`, `compost`

The roster is not fixed. It is db-native, editable from the search page, and
carried across a rebuild — the one deliberate exception to files-are-truth.
This archive's own roster adds a fifth, `whtc`, for show bits, which is exactly
the intended use: add a class the day you notice you keep filing the same kind
of thing, and the clerk starts filing by it.

`compost` is not the failure bucket. It is the default and expected home for most entries —
the unresolved pile, which is the most interesting part of the system. Naming matters:
never call it "unsorted" in code, comments, or UI.

**Tool surface — exactly three verbs. Do not expand.**

```python
classify(transcript: str) -> str    # one of the roster's classes
tag(transcript: str) -> list[str]   # 3-7 lowercase keywords, no invention
route(entry_id: str, cls: str)      # writes class to meta + db
```

**Classifier rules:**
- Low confidence → `compost`. Bias hard toward this. Getting it wrong in the confident
  direction is much more expensive than punting.
- Never modify `transcript`. Read-only input.
- Never generate content, titles, or summaries.
- A garbled transcript (singing, humming, noise) is **valid input**, not an error. Classify it
  `song` if it looks like singing, `compost` otherwise. The .wav is the artifact; the transcript
  is just a search handle.

---

## 8. Location

Location is **not** present in the audio file and cannot be inferred. It must be captured at
record time or not at all.

**Rev 1 approach:** ship without it. `lat`/`lon`/`place` are nullable, in the schema, unused.

**Rev 1.1:** iOS Shortcut writes a sidecar JSON to iCloud Drive at record time:
```json
{ "ts": "2026-09-02T14:33:12Z", "lat": 30.2672, "lon": -97.7431 }
```
Ingest matches sidecars to entries by timestamp proximity (±90s window). Unmatched sidecars
are discarded silently; unmatched entries just have null location. No error state.

Do not block rev 1 on this.

---

## 9. Search UI

Single HTML file. Served locally. Not a webapp.

**Requirements:**
- Search box → FTS5 query across transcripts
- Filter chips: class, date range, **the mark**, and **hidden** — separate axes, each
  filtered on its own
- Results: date, class, tags, transcript snippet, inline `<audio controls>` per row
- **Opens on `compost`, not on "all" and not on the classified folders.** Classified material is
  already findable. The unresolved pile is the only thing that benefits from being stumbled over.
- **`surprise me` button** — pulls one random entry from `compost`. Random is random: the
  filters narrow the pile, and nothing weights the draw. No age weighting, no de-weighting what
  has already been served, no record of what the button has shown. A rigged draw is a ranking
  wearing a disguise, and it would quietly decide what you are allowed to stumble into.
- **The mark** — one bit per entry meaning *there is something here*. One gesture, one
  direction, no scale and no opposite. It is not a tag and does not compete with tags: tags say
  what an entry is about and the clerk writes them, the mark says a human found something and
  only a human writes it. Deliberately **not** a star rating — a scale would grade material
  that hasn't finished breaking down, and a grade stamped today is wrong in eight months, wrong
  with authority, because the entry it dismissed never gets shown again. The absence of the mark
  is the only negative signal, and it costs nothing to leave.
- Marking writes `status` to the entry's meta JSON first, db second. It rides a `rebuild` with
  no special-casing — unlike the class roster, the mark needs no exception to files-are-truth.
- **Notes** — a text box on the active post where you work an idea out. It saves itself:
  debounced while typing, flushed on blur and on anything that tears the box down. A note you
  have to remember to save is a note you lose. The box is a **fixed height that scrolls, never
  grows**, so filling it can't push the transcript and the audio off the screen. A char count
  sits next to the label in grey against `MAX_NOTES` — a sanity bound on the payload, not a
  budget for your writing; raise it the day it ever bites.
- Notes are a **sibling of the transcript, never a replacement.** The transcript is what the
  machine heard; the notes are what you made of it. Keeping them apart is what lets you
  re-transcribe with a better model later without destroying your thinking, and what lets you
  always tell what you actually said from what you decided afterwards you meant. **The page
  never edits the transcript.** Both are indexed, so search covers your writing too and the
  snippet highlights whichever one matched.
- **It is a text box and nothing more.** No append/prepend controls, no stamping, no revision
  history — it is free text, so you add, revise and delete in it like any other text box. An
  earlier build grew a line-adder with `↑ top` / `↓ end` buttons on the theory that additive
  edits would remove the need for versioning; they do not, because the box stays editable
  anyway, and the controls were clutter wrapped around a plain affordance. Notes are plain
  files, so `git init ~/compost` with `audio/` ignored buys real history if it is ever wanted.
- **Hidden** — `[x]` beside the mark, in dark orange: the one control that takes something
  away must not wear the mark's green or the tags' blue. **One click, and it goes.** No confirm
  step: it sets a flag and **moves nothing** — audio, transcript and meta stay put — so there is
  nothing to be careful about and the undo is one tap away. Ceremony belongs in front of
  irreversible acts, and this is not one.
- A hidden entry leaves the pile completely: out of search, out of the year counts, out of the
  header total, and never drawn by `surprise me`. "Out of view" has to mean out of view, or the
  pile stops being trustworthy as the thing you are looking at.
- The `hidden` chip shows that pile and only that pile, where each entry wears `[+]` to come
  back. Restoring is a single tap — it adds something to a view, so a misfire costs a click
  rather than a thought you meant to keep. No view ever mixes hidden and live entries.

No deleting, no auth, no accounts. The mark is the one write the page makes to an entry, and it
touches nothing the pipeline produced — not the transcript, not the class, not the tags.

---

## 10. Build phases

Each phase must be independently useful and independently shippable. **Stop after each and
use it for a few days before starting the next.** Later phases may turn out to be unnecessary.

### Phase 1 — Watcher + transcription
Watch the Voice Memos dir. On new file: copy to `audio/`, generate id, run whisper.cpp, write
`transcripts/<id>.txt` and a minimal `meta/<id>.json`.

**Acceptance:** record a memo on the phone, wait for iCloud, confirm a transcript appears on
the Mac with no manual action. Confirm it survives a reboot.

### Phase 2 — Index + search page
Build `compost.db` from the folder. Add `rebuild` command that wipes and reconstructs the DB
from files alone. Ship the HTML search page.

**Acceptance:** find a specific memo by a word spoken in it, in under 5 seconds, and play its
audio in the browser. Delete `compost.db`, run `rebuild`, confirm identical results.

> **This may be the whole win.** Search alone might solve the buried-good-ideas problem, because
> the issue was never that bad ideas exist — it's that they stood between you and the good
> ones. Search routes around them instead of grading them. Live with Phase 2 for at least a week
> before building Phase 3.

### Phase 3 — Classifier + tagger (only if Phase 2 leaves a gap)
Add classify/tag/route. Backfill existing entries. Add filter chips + `surprise me`.

**Acceptance:** ≥90% of song fragments land in `song`; nothing lands in a wrong confident class
more than rarely; the `compost` pile is large and that is correct.

---

## 11. Known gotchas

- **Voice Memos path moves between macOS releases.** Do not hardcode. Resolve at startup with
  a glob under `~/Library/Group Containers/`, fail loudly with the paths checked if not found.
- **macOS 26.3.1 is an alpha build.** Expect Full Disk Access and Automation prompts to behave
  oddly. Grant FDA to the script's interpreter, not the script.
- **iCloud files may appear as 0-byte placeholders** before download completes. Ingest must wait
  for a stable, non-zero file size before transcribing. Poll with backoff; do not assume the
  file is complete on the watcher event.
- **whisper.cpp on Apple Silicon** — build with Metal support or transcription will be slow
  enough to be annoying.
- **Never write to the Voice Memos directory.** Read and copy only. Corrupting it means losing
  source material.
- **Idempotency:** re-running ingest on an already-processed file must be a no-op, not a
  duplicate. launchd will fire spuriously.
- **Never take an entry's meta file out of `meta/`.** Idempotency comes from `meta/*.json`
  alone, so an entry whose meta moves or vanishes reads as never-processed — and since the
  Voice Memos recording is deliberately left alone, the next WatchPaths fire re-ingests it
  under a new id, minutes later. This is why hiding is a flag and not a move: anything that
  relocates entries has to carry a tombstone, and a flag needs none.

---

## 12. Explicitly out of scope for Rev 1

Do not build these. They are real and they come later.

- Physical button (Flic or similar) — evaluated and deliberately deferred. Press-and-hold
  already works; a button saves ~zero presses and delays the pipeline. The pipeline must not
  know or care what triggered a recording.
- Apple Notes intake via AppleScript (`osascript`, `body of note ... of account "iCloud"`),
  plus the one-time LLM pass to split the existing blob into dated entries. `source='note'`
  is already in the schema for this.
- The ratchet: kill/keep/promote cycling. The mark (§9) spends the `status` column and is as
  far as this goes — a flag, not a cycle. There is still **no kill state**: `hidden` says where
  something is shown, never what it is worth. Nothing may ever record a judgment against an
  entry, and nothing may ever delete one.
- **Growing a note into the finished thing** — notes as a writing surface rather than an
  annotation, so a song fragment in the pile becomes the song. Compost and seed in one place,
  then grow it. The field already holds prose and already searches, so the groundwork is laid;
  what it would need is room to work (a bigger box, a bigger `MAX_NOTES`, probably a word count
  rather than a char count) and some notion of a note having become its own object rather than
  a margin on a recording. Deliberately not designed yet — note the direction, build it when
  the pile has actually been used this way for a while.
- Any hosted/remote/multi-user version.

---

## 13. Definition of done for Rev 1

You speak into your phone with one press-and-hold, do nothing else, and within a few
minutes that thought is searchable by keyword and playable on your Mac — and the pile you land
in when you open the page is the one you were least likely to have gone looking for.
