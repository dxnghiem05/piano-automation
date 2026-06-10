"""Watch the input folder and run the app when new videos arrive."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import config
from generate_clips import ensure_directories
from logging_setup import configure_logging
from main import main

logger = logging.getLogger(__name__)

POLL_SECONDS = 10
STABLE_CHECK_SECONDS = 5
STABLE_CHECKS_REQUIRED = 3


def watch_input_folder() -> None:
    """Poll the input folder and process videos when files are stable."""
    ensure_directories()
    configure_logging()
    logger.info("Watching %s for new videos", config.INPUT_DIR)
    print(f"Watching for videos in: {config.INPUT_DIR}")
    print("Leave this running. Press Control+C to stop.")

    known_paths = current_video_paths()

    try:
        while True:
            paths = current_video_paths()
            new_paths = sorted(paths - known_paths)
            ready_paths = [path for path in new_paths if is_file_stable(path)]

            if ready_paths:
                logger.info("Detected ready video files: %s", ", ".join(str(path) for path in ready_paths))
                print("")
                print("New video detected. Running automation...")
                main()
                known_paths = current_video_paths()
                print("")
                print(f"Watching again: {config.INPUT_DIR}")
            else:
                known_paths = paths

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Input folder watcher stopped by user")
        print("")
        print("Stopped watching.")


def current_video_paths() -> set[Path]:
    """Return currently visible supported video files in input/."""
    return {
        path
        for path in config.INPUT_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in config.SUPPORTED_VIDEO_EXTENSIONS
    }


def is_file_stable(path: Path) -> bool:
    """Return True when a file's size stops changing across several checks."""
    last_size = -1
    stable_count = 0

    for _ in range(STABLE_CHECKS_REQUIRED + 1):
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return False

        if current_size == last_size and current_size > 0:
            stable_count += 1
        else:
            stable_count = 0
            last_size = current_size

        if stable_count >= STABLE_CHECKS_REQUIRED:
            return True

        time.sleep(STABLE_CHECK_SECONDS)

    return False


if __name__ == "__main__":
    watch_input_folder()
