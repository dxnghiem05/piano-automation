#!/bin/zsh
# =============================================================================
#  Piano Dashboard — one-time relocation out of ~/.codex into a Claude-named home
#  Run this ONCE on your Mac (double-click won't work — run it in Terminal):
#
#      zsh "/Users/dustinnghiem/Claude/PianoDashboard/ops/migrate_to_claude.sh"
#
#  What it does (safe, reversible up to the move):
#    1. Stops the launchd public tunnel + health-check jobs and the dashboard.
#    2. Moves the whole project (instant mv, same disk) to ~/Claude/PianoDashboard
#       — ALL your data (clips, uploaded, logs, metadata, .git) comes with it.
#    3. Rewrites every hard-coded old path to the new one.
#    4. Rebuilds the Python virtualenv at the new location.
#    5. Reinstalls + reloads the launchd jobs from the new path.
#    6. Verifies and prints next steps.
#  It never deletes your data and never touches YouTube/TikTok.
# =============================================================================
set -e
setopt no_nomatch 2>/dev/null || true

OLD="/Users/dustinnghiem/Claude/PianoDashboard"
NEW="/Users/dustinnghiem/Claude/PianoDashboard"
LA="$HOME/Library/LaunchAgents"
PORT="8000"

say(){ print -P "%F{green}>>%f $1"; }
warn(){ print -P "%F{yellow}!!%f $1"; }
die(){ print -P "%F{red}xx%f $1"; exit 1; }

# ---- 0. Pre-flight -----------------------------------------------------------
[ -d "$OLD" ] || die "Old project not found at $OLD (already migrated?)."
if [ -e "$NEW" ] && [ -n "$(ls -A "$NEW" 2>/dev/null)" ]; then
  die "$NEW already exists and is not empty. Move/rename it first, then re-run."
fi
# Same volume? (mv is instant only within one volume)
if [ "$(stat -f '%d' "$OLD")" != "$(stat -f '%d' "$(dirname "$HOME")")" ]; then
  warn "Old path and home may be on different volumes — the move could take a while (copy)."
fi

say "Migrating:"
print "    FROM  $OLD"
print "    TO    $NEW"
print ""

# ---- 1. Stop services --------------------------------------------------------
say "Stopping launchd jobs + dashboard (if running)…"
launchctl unload "$LA/com.pianodashboard.public.plist"     2>/dev/null || true
launchctl unload "$LA/com.pianodashboard.healthcheck.plist" 2>/dev/null || true
# Kill any dashboard still holding the port (KeepAlive is now off, so it stays down)
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  lsof -nP -tiTCP:$PORT -sTCP:LISTEN | xargs -r kill 2>/dev/null || true
  sleep 1
fi

# ---- 2. Move the project -----------------------------------------------------
say "Moving the project (this preserves ALL data)…"
mkdir -p "$(dirname "$NEW")"
mv "$OLD" "$NEW"
cd "$NEW"
# Clean any empty leftover dirs from the earlier tidy step
rmdir design_concepts 2>/dev/null || true

# ---- 3. Rewrite hard-coded paths --------------------------------------------
say "Rewriting hard-coded paths ($OLD → $NEW)…"
# Only text files, skip data/binary/vcs/venv. -I skips binary files.
grep -rIl --exclude-dir=.git --exclude-dir=.venv --exclude-dir=clips \
     --exclude-dir=uploaded --exclude-dir=logs --exclude-dir=processing \
     "$OLD" "$NEW" 2>/dev/null | while read -r f; do
  sed -i '' "s#$OLD#$NEW#g" "$f"
  print "    fixed: ${f#$NEW/}"
done

# ---- 4. Rebuild the virtualenv ----------------------------------------------
say "Rebuilding the Python virtualenv…"
rm -rf "$NEW/.venv"
/usr/bin/python3 -m venv "$NEW/.venv"
"$NEW/.venv/bin/python" -m pip install -q --upgrade pip
"$NEW/.venv/bin/pip" install -q -r "$NEW/requirements.txt"
say "venv Python: $("$NEW/.venv/bin/python" --version 2>&1)"

# ---- 5. Reinstall + reload launchd jobs -------------------------------------
say "Reinstalling launchd jobs from the new path…"
mkdir -p "$LA"
cp "$NEW/ops/com.pianodashboard.public.plist"      "$LA/"
cp "$NEW/ops/com.pianodashboard.healthcheck.plist" "$LA/"
launchctl load "$LA/com.pianodashboard.public.plist"
launchctl load "$LA/com.pianodashboard.healthcheck.plist"

# ---- 6. Verify ---------------------------------------------------------------
print ""
say "Verifying…"
"$NEW/.venv/bin/python" -c "import ast,sys; ast.parse(open('$NEW/dashboard.py').read()); print('   dashboard.py: syntax OK')"
if launchctl list | grep -q pianodashboard; then
  print "   launchd jobs loaded:"; launchctl list | grep pianodashboard | sed 's/^/     /'
else
  warn "launchd jobs not showing yet — run: launchctl list | grep pianodashboard"
fi
print ""
print -P "%F{green}✔ Done.%f Project now lives at:  $NEW"
print ""
print "Next:"
print "  • The public ngrok link relaunches automatically (KeepAlive). Give it ~10s."
print "  • In the Claude desktop app, connect the NEW folder: $NEW"
print "    (so Claude and the weekly security check use the new location)."
print "  • The old ~/.codex/workspaces/default path no longer exists — that's expected."
