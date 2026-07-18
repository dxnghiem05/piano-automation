#!/bin/zsh
# Double-click to put the dashboard online at your PERMANENT ngrok link.
# It starts the dashboard if needed, then opens the ngrok tunnel on your fixed
# domain. Also used by the auto-start launchd service (com.pianodashboard.public).
#
# Requirements (one-time):
#   1) brew install ngrok
#   2) ngrok config add-authtoken <your token>   (from ngrok.com dashboard)
#   3) In .env set:  DASHBOARD_PASSWORD=...  and  NGROK_DOMAIN=yourname.ngrok-free.dev

PROJECT_DIR="/Users/dustinnghiem/Claude/PianoDashboard"
PORT="8000"

cd "$PROJECT_DIR" || exit 1

if ! command -v ngrok >/dev/null 2>&1; then
  echo "ngrok is not installed. Install it with:  brew install ngrok"
  exit 1
fi

if [ ! -f .env ]; then
  echo "No .env file. Copy .env.example to .env and set DASHBOARD_PASSWORD and NGROK_DOMAIN."
  exit 1
fi

# Load .env values (DASHBOARD_PASSWORD, NGROK_DOMAIN, ...)
set -a
source .env 2>/dev/null
set +a

# Safety: never expose publicly without an owner password.
if [ -z "$DASHBOARD_PASSWORD" ]; then
  echo "REFUSING to go public: set DASHBOARD_PASSWORD in .env first (then restart the dashboard)."
  exit 1
fi
if [ -z "$NGROK_DOMAIN" ]; then
  echo "Set NGROK_DOMAIN in .env to your permanent ngrok domain (e.g. yourname.ngrok-free.dev)."
  exit 1
fi

# Start the dashboard if it is not already running on the port.
if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Dashboard not running - starting it..."
  mkdir -p logs
  nohup "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/dashboard.py" >"$PROJECT_DIR/logs/dashboard.out" 2>&1 &
  sleep 2
fi

echo ""
echo "Your permanent public link:  https://$NGROK_DOMAIN"
echo "Visitors see a read-only view. Click 'Owner login' to control it."
echo "Keep this running to stay online. Press Control+C to stop."
echo ""

# Newer ngrok uses --url; if your ngrok errors, replace --url with --domain.
exec ngrok http --url="https://$NGROK_DOMAIN" "$PORT"
