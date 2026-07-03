#!/bin/bash
# Double-click this file to push all committed changes to GitHub.
# It runs on your Mac, so it uses your saved GitHub login — no token needed.

cd "/Users/dustinnghiem/.codex/workspaces/default" || exit 1

echo "Pushing committed changes to GitHub (origin/main)..."
echo ""
git push origin main
status=$?
echo ""
if [ $status -eq 0 ]; then
  echo "Done — GitHub is up to date. You can close this window."
else
  echo "Push did not complete (exit $status). If it asked for a login,"
  echo "you may need to sign in to GitHub once in Terminal, then try again."
fi
echo ""
