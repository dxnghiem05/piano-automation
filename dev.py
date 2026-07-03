#!/usr/bin/env python3
"""Auto-reloading launcher for the piano dashboard.

Run this INSTEAD of `python dashboard.py` while you are working:

    python dev.py

It starts the dashboard and then watches every .py file in the project. Whenever
one changes (you save an edit, or an edit lands from elsewhere), it restarts the
server automatically — so you never have to kill and relaunch by hand. Just save,
then refresh the browser.

Stop it with Ctrl+C.

Notes:
- Uses the same Python/venv you launch it with.
- If a change introduces a syntax error and the server crashes, it keeps
  watching; fix the file and it will start cleanly on the next save.
- The real `python dashboard.py` is unchanged, so production/normal use is
  unaffected.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TARGET = "dashboard.py"
POLL_SECONDS = 1.0

# Only watch source files. Ignore the virtualenv, caches, media, logs, and other
# private/large folders so the watcher stays fast and never reloads on data files.
IGNORE_DIRS = {
    ".venv", "__pycache__", ".git", "clips", "input", "uploaded",
    "processing", "logs", "metadata", "credentials",
}


def watched_files() -> dict[str, float]:
    """Return {path: mtime} for every watched .py file."""
    mtimes: dict[str, float] = {}
    for path in BASE_DIR.rglob("*.py"):
        parts = path.relative_to(BASE_DIR).parts
        if any(part in IGNORE_DIRS for part in parts):
            continue
        try:
            mtimes[str(path)] = path.stat().st_mtime
        except OSError:
            continue
    return mtimes


def start() -> subprocess.Popen:
    print("[dev] starting dashboard (http://localhost:8000) ...")
    return subprocess.Popen([sys.executable, TARGET], cwd=BASE_DIR)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main() -> int:
    print("[dev] auto-reload watcher running. Edit + save, then refresh the browser.")
    print("[dev] press Ctrl+C to stop.")
    proc = start()
    snapshot = watched_files()
    try:
        while True:
            time.sleep(POLL_SECONDS)
            current = watched_files()
            changed = [name for name, mtime in current.items() if snapshot.get(name) != mtime]
            added_or_removed = set(current) != set(snapshot)
            if changed or added_or_removed:
                labels = ", ".join(sorted(Path(name).name for name in changed)) or "project files"
                print(f"[dev] change detected ({labels}); restarting...")
                stop(proc)
                proc = start()
                snapshot = current
    except KeyboardInterrupt:
        print("\n[dev] stopping...")
    finally:
        stop(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
