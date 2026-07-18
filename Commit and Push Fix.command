#!/bin/bash
# Double-click to commit the dashboard performance fix and push it to GitHub.
# Runs on your Mac, so it uses your saved GitHub login — no token needed.

cd "/Users/dustinnghiem/Claude/PianoDashboard" || exit 1

# Clear any stale git lock left behind by a crashed/earlier process.
if [ -f .git/index.lock ]; then
  echo "Clearing a stale git lock..."
  rm -f .git/index.lock
fi

echo "Committing the performance fix (dashboard.py, stats_tracker.py)..."
git add dashboard.py stats_tracker.py
if git diff --cached --quiet; then
  echo "Nothing new to commit — the fix is already committed."
else
  git commit -m "Perf: incremental stats-history cache + stronger Retina perf tier"
fi

echo ""
echo "Pushing to GitHub (origin/main)..."
git push origin main
status=$?
echo ""
if [ $status -eq 0 ]; then
  echo "Done — GitHub is up to date. You can close this window."
else
  echo "Push did not complete (exit $status)."
  echo "If it asked for a login, sign in to GitHub once in Terminal, then run this again."
fi
echo ""
