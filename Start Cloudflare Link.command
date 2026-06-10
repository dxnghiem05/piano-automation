#!/bin/zsh

set -e

PROJECT_DIR="/Users/dustinnghiem/.codex/workspaces/default"
PORT="8000"

cd "$PROJECT_DIR"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not available in this Terminal."
  echo ""
  echo "Try installing it with:"
  echo "brew install cloudflared"
  echo ""
  echo "Press any key to close."
  read -k 1
  exit 1
fi

if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "The dashboard is not running yet."
  echo ""
  echo "First double-click:"
  echo "Start Piano Dashboard.command"
  echo ""
  echo "Then run this Cloudflare launcher again."
  echo ""
  echo "Press any key to close."
  read -k 1
  exit 1
fi

echo "Starting Cloudflare tunnel for:"
echo "http://localhost:$PORT"
echo ""
echo "Cloudflare will print a public trycloudflare.com link below."
echo "Keep this Terminal window open while using that link."
echo "Press Control+C here when you want to stop it."
echo ""

cloudflared tunnel --url "http://localhost:$PORT"
