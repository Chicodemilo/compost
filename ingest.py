#!/usr/bin/env python3
"""COMPOST — Phase 1 ingest.

Fired by launchd (WatchPaths on the Voice Memos recordings dir) or by hand.
For each new .m4a: copy to ~/compost/audio/, transcribe with whisper.cpp,
write transcript + meta. Files are the source of truth; re-running on
already-processed files is a no-op.

Usage:
    ingest.py            process anything new
    ingest.py doctor     check paths, permissions, and tools; exit non-zero on trouble
"""

import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

COMPOST = Path.home() / "compost"
AUDIO = COMPOST / "audio"
TRANSCRIPTS = COMPOST / "transcripts"
META = COMPOST / "meta"
LOGS = COMPOST / "logs"
FAILED = COMPOST / "inbox_failed"
MODEL = COMPOST / "models" / "ggml-large-v3.bin"
WHISPER = "/opt/homebrew/bin/whisper-cli"
DISPATCH = Path.home() / "Dev" / "watchtower" / "dispatch"

# iCloud placeholder wait: size must be non-zero and stable this long
STABLE_CHECKS = 3
STABLE_INTERVAL = 2.0
STABLE_TIMEOUT = 45  # skip fast — WatchPaths refires when iCloud finishes writing, so retries are free

# Ghost placeholders: recordings deleted in iCloud leave 0-byte files that never fill.
# After this many consecutive failed stability waits, stop polling (45s each) and just
# stat the file once per fire. Low is safe: bytes appearing later trigger a full retry
# automatically, and WatchPaths refires the moment iCloud writes them.
GIVE_UP_AFTER = 2
STRIKES = COMPOST / "skip_strikes.json"


def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(LOGS / "ingest.log", "a") as f:
        f.write(line + "\n")


def resolve_recordings_dir() -> Path:
    """Voice Memos path moves between macOS releases — glob, never hardcode."""
    candidates = glob.glob(
        str(Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos*/Recordings")
    )
    dirs = [Path(c) for c in candidates if Path(c).is_dir()]
    if not dirs:
        checked = glob.glob(str(Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos*"))
        sys.exit(
            "FATAL: no Voice Memos Recordings dir found (or no Full Disk Access).\n"
            f"Containers checked: {checked or ['<none matched>']}\n"
            "If a container exists but Recordings is unreadable, grant Full Disk Access "
            "to the python3 interpreter (System Settings > Privacy & Security > Full Disk Access)."
        )
    return dirs[0]


def processed_source_files() -> set[str]:
    """Idempotency from files alone: every meta json records its source file name."""
    seen = set()
    for p in META.rglob("*.json"):
        try:
            seen.add(json.loads(p.read_text()).get("source_file", ""))
        except (json.JSONDecodeError, OSError):
            continue
    return seen


def load_strikes() -> dict[str, int]:
    try:
        return json.loads(STRIKES.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_strikes(strikes: dict[str, int]) -> None:
    STRIKES.write_text(json.dumps(strikes, indent=2, sort_keys=True))


def wait_for_stable(path: Path) -> bool:
    """iCloud files can appear as 0-byte placeholders. Wait for a stable, non-zero size."""
    try:
        st = path.stat()
        if st.st_size > 0 and time.time() - st.st_mtime > 120:
            return True  # settled long ago; skip the polling dance (matters at 300-file backfill scale)
    except OSError:
        return False
    deadline = time.time() + STABLE_TIMEOUT
    last, stable = -1, 0
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size > 0 and size == last:
            stable += 1
            if stable >= STABLE_CHECKS:
                return True
        else:
            stable = 0
        last = size
        time.sleep(STABLE_INTERVAL)
    return False


def recorded_at(src: Path) -> float:
    """True record time. Voice Memos filenames embed it ('20250425 200500-77609DDE.m4a')
    and survive iCloud sync; mtime can be reset to the sync date. Filename wins."""
    m = re.match(r"^(\d{8}) (\d{6})-", src.name)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
    return src.stat().st_mtime


def make_id(mtime: float) -> str:
    stamp = datetime.fromtimestamp(mtime).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def duration_of(path: Path) -> float | None:
    try:
        out = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"estimated duration:\s*([\d.]+)", out)
        return float(m.group(1)) if m else None
    except (subprocess.SubprocessError, OSError):
        return None


def transcribe(m4a: Path) -> str:
    """m4a -> 16kHz mono wav (afconvert) -> whisper.cpp large-v3."""
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(m4a), str(wav)],
            check=True, capture_output=True, timeout=300,
        )
        out = Path(td) / "out"
        subprocess.run(
            [WHISPER, "-m", str(MODEL), "-f", str(wav), "-otxt", "-of", str(out), "-np"],
            check=True, capture_output=True, timeout=1800,
        )
        return (Path(td) / "out.txt").read_text().strip()


def dispatch(target: str, state: str, note: str) -> None:
    if DISPATCH.exists():
        subprocess.run([str(DISPATCH), "compost", target, state, note],
                       capture_output=True, timeout=15)


def ingest_one(src: Path, strikes: dict[str, int]) -> None:
    if not wait_for_stable(src):
        n = strikes[src.name] = strikes.get(src.name, 0) + 1
        save_strikes(strikes)
        if n >= GIVE_UP_AFTER:
            log(f"GIVE UP {src.name}: zero-byte on {n} consecutive checks — likely deleted "
                f"in iCloud; downgrading to a bare stat per fire (delete its {STRIKES.name} "
                "entry to force a full retry)")
        else:
            log(f"SKIP {src.name}: size never stabilized (iCloud still downloading?) [strike {n}/{GIVE_UP_AFTER}]")
        return
    if strikes.pop(src.name, None) is not None:
        save_strikes(strikes)

    mtime = recorded_at(src)
    entry_id = make_id(mtime)
    ym = datetime.fromtimestamp(mtime).strftime("%Y/%m")
    audio_dst = AUDIO / ym / f"{entry_id}{src.suffix}"
    audio_dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, audio_dst)  # read + copy only; never touch the VM dir
    except OSError as e:
        audio_dst.unlink(missing_ok=True)
        log(f"SKIP {src.name}: copy failed ({e}) — likely still materializing from iCloud, will retry")
        return

    try:
        text = transcribe(audio_dst)
    except Exception as e:
        FAILED.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_dst, FAILED / audio_dst.name)
        err = getattr(e, "stderr", b"")
        (FAILED / f"{entry_id}.err").write_text(
            f"{type(e).__name__}: {e}\n{err.decode(errors='replace') if err else ''}"
        )
        log(f"FAIL {src.name} -> inbox_failed/{entry_id}.err")
        dispatch(entry_id, "failed", f"transcription error on {src.name}")
        return

    for d, name, content in [
        (TRANSCRIPTS, f"{entry_id}.txt", text),
        (META, f"{entry_id}.json", json.dumps({
            "id": entry_id,
            "created_at": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
            "source": "vm",
            "source_file": src.name,
            "class": "compost",
            "tags": [],
            "audio_path": str(audio_dst.relative_to(COMPOST)),
            "duration": duration_of(audio_dst),
            "lat": None, "lon": None, "place": None,
            "status": "raw",
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2)),
    ]:
        p = d / ym / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    words = len(text.split())
    log(f"OK {src.name} -> {entry_id} ({words} words)")
    dispatch(entry_id, "ingested", f"{words} words, {duration_of(audio_dst) or '?'}s")

    try:
        import classify
        r = classify.apply(entry_id)
        log(f"CLASS {entry_id} -> {r['class']} {r['tags']}")
    except Exception as e:
        log(f"classify deferred for {entry_id} ({e}) — `classify.py backfill` will catch it")


def doctor() -> None:
    problems = []
    rec = None
    try:
        rec = resolve_recordings_dir()
        try:
            # os.listdir, not glob: glob swallows PermissionError and fakes an empty dir
            names = os.listdir(rec)
            n = sum(1 for x in names if x.endswith((".m4a", ".qta")))
            print(f"ok: recordings dir readable: {rec} ({n} recordings)")
        except PermissionError:
            problems.append(f"recordings dir found but unreadable (no Full Disk Access): {rec}")
    except SystemExit as e:
        problems.append(str(e))
    if not Path(WHISPER).exists():
        problems.append(f"whisper-cli missing at {WHISPER} (brew install whisper-cpp)")
    else:
        print(f"ok: {WHISPER}")
    if not MODEL.exists() or MODEL.stat().st_size < 1_000_000_000:
        problems.append(f"model missing or incomplete: {MODEL}")
    else:
        print(f"ok: model {MODEL} ({MODEL.stat().st_size // 2**20} MB)")
    print(f"{'ok' if DISPATCH.exists() else 'warn'}: watchtower dispatch {'found' if DISPATCH.exists() else 'missing (events will not be logged)'}")
    if problems:
        print("\n".join(f"PROBLEM: {p}" for p in problems))
        sys.exit(1)
    print("all clear")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        doctor()
        return
    rec = resolve_recordings_dir()
    try:
        # .m4a = classic format; .qta = QuickTime audio, what Voice Memos records
        # from late 2025 on. Both are audio, both transcribe the same way.
        candidates = sorted(list(rec.glob("*.m4a")) + list(rec.glob("*.qta")))
    except PermissionError:
        sys.exit(f"FATAL: {rec} exists but is unreadable — grant Full Disk Access to python3.")
    seen = processed_source_files()
    strikes = load_strikes()
    new = []
    for p in candidates:
        if p.name in seen:
            continue
        if strikes.get(p.name, 0) >= GIVE_UP_AFTER:
            try:
                if p.stat().st_size == 0:
                    continue  # still a ghost: one stat, no polling, no log noise
            except OSError:
                continue
            del strikes[p.name]  # it materialized after all — full retry
            save_strikes(strikes)
            log(f"RETRY {p.name}: given-up placeholder finally has bytes")
        new.append(p)
    if not new:
        return
    log(f"found {len(new)} new recording(s)")
    for i, src in enumerate(new, 1):
        ingest_one(src, strikes)
        if i % 25 == 0:  # keep the search page fresh during long backfill batches
            try:
                import index_db
                index_db.rebuild()
            except Exception:
                pass
    try:
        import index_db
        log(f"index rebuilt: {index_db.rebuild()} entries")
    except Exception as e:
        log(f"index rebuild failed (files are fine, rerun index_db.py rebuild): {e}")


if __name__ == "__main__":
    main()
