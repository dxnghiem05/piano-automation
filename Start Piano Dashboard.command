#!/bin/zsh

set -e

PROJECT_DIR="/Users/dustinnghiem/Claude/PianoDashboard"
PORT="8000"

cd "$PROJECT_DIR"

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Piano Dashboard is already running."
  echo "Opening http://localhost:$PORT ..."
  open "http://localhost:$PORT"
  exit 0
fi

if [ ! -f ".venv/bin/activate" ]; then
  echo "Could not find .venv/bin/activate in:"
  echo "$PROJECT_DIR"
  echo ""
  echo "Press any key to close."
  read -k 1
  exit 1
fi

source .venv/bin/activate

echo "Starting Piano Dashboard..."
echo "Project: $PROJECT_DIR"
echo "URL: http://localhost:$PORT"
echo ""
echo "Keep this Terminal window open while using the dashboard."
echo "Press Control+C here when you want to stop it."
echo ""

open "http://localhost:$PORT"
python dashboard.py
