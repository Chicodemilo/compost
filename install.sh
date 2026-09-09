#!/bin/bash
# COMPOST installer — generates launchd agents from the templates and loads them.
# Idempotent: re-running replaces the agents (e.g. after a `brew upgrade` of python
# moves the Cellar path).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"

PYTHON3="$(command -v python3)"
[ -x "$PYTHON3" ] || { echo "FATAL: python3 not found"; exit 1; }

# Resolve the Python.app bundle for this python3 — the FDA grant must go on it,
# and the ingest agent must launch its inner binary (see the template comment).
PREFIX="$("$PYTHON3" -c 'import sys; print(sys.prefix)')"
PYTHON_APP="$PREFIX/Resources/Python.app"
[ -d "$PYTHON_APP" ] || { echo "FATAL: no Python.app at $PYTHON_APP (need a framework build, e.g. Homebrew python)"; exit 1; }

command -v whisper-cli >/dev/null || echo "WARN: whisper-cli not found — brew install whisper-cpp"
[ -f "$HOME/compost/models/ggml-large-v3.bin" ] || \
    echo "WARN: model missing — download ggml-large-v3.bin to ~/compost/models/ (see README)"

mkdir -p "$HOME/compost/logs" "$AGENTS"

render() {  # render <template> <dest>
    sed -e "s|__HOME__|$HOME|g" \
        -e "s|__REPO__|$REPO|g" \
        -e "s|__PYTHON3__|$PYTHON3|g" \
        -e "s|__PYTHON_APP__|$PYTHON_APP|g" \
        "$1" > "$2"
}

for name in com.compost.ingest com.compost.web; do
    tmpl="$REPO/launchd/$name.plist.template"
    dest="$AGENTS/$name.plist"
    render "$tmpl" "$dest"
    launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dest"
    echo "loaded $name"
done

cat <<EOF

Almost done — one manual step macOS requires:
  System Settings > Privacy & Security > Full Disk Access > add:
    $PYTHON_APP
  (Cmd+Shift+G in the file picker to paste the path.)

Then verify:  python3 "$REPO/ingest.py" doctor
Search page:  http://127.0.0.1:8771
EOF
