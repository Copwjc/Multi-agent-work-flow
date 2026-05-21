#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

HOST="127.0.0.1"
PORT="8765"
URL="http://${HOST}:${PORT}/"
PYTHON_EXE="${SCRIPT_DIR}/.venv/bin/python"

if [ ! -x "$PYTHON_EXE" ]; then
  osascript -e 'display alert "Missing virtual environment" message "Expected .venv/bin/python was not found. Create the virtual environment first and install requirements." as critical'
  echo "[ERROR] Missing virtual environment Python: $PYTHON_EXE"
  echo "Create it first:"
  echo "  python3 -m venv .venv"
  echo "  ./.venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

SERVER_CMD="cd \"$SCRIPT_DIR\" && \"$PYTHON_EXE\" tools/multiagent_web.py --host $HOST --port $PORT"

osascript - "$SERVER_CMD" <<'EOF'
on run argv
  tell application "Terminal"
    activate
    do script (item 1 of argv)
  end tell
end run
EOF

sleep 2
open "$URL"
