#!/bin/zsh

set -e

PROJECT_DIR="/Users/dustinnghiem/Claude/PianoDashboard"
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

# Safety gate: never expose the dashboard publicly without an owner password.
if [ ! -f .env ] || ! grep -qE '^DASHBOARD_PASSWORD=.+' .env; then
  echo "REFUSING to start a public link: no DASHBOARD_PASSWORD is set."
  echo ""
  echo "Without a password, anyone with the link could run uploads and edit your data."
  echo "Fix: copy .env.example to .env, set a strong DASHBOARD_PASSWORD, then RESTART"
  echo "the dashboard so it loads the password, and run this launcher again."
  echo ""
  echo "Press any key to close."
  read -k 1
  exit 1
fi

echo "Owner password detected - the public link will be read-only for visitors."
echo ""
echo "Starting Cloudflare tunnel for:"
echo "http://localhost:$PORT"
echo ""
echo "Cloudflare will print a public link below."
echo "Look for the line that starts with:"
echo "https://"
echo ""
echo "That link changes each time you restart this tunnel."
echo "Keep this Terminal window open while using that link."
echo "Press Control+C here when you want to stop it."
echo ""

cloudflared tunnel \
  --url "http://localhost:$PORT" \
  --edge-ip-version 4 \
  --transport-loglevel warn \
  --retries 10
