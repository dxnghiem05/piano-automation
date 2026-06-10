"""Application entry point."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

import config
from generate_clips import discover_videos, ensure_directories, generate_clips_for_videos, verify_ffmpeg_tools
from generate_metadata import ClipMetadata, generate_metadata_for_clips
from logging_setup import configure_logging
from scheduler import generate_schedule
from tracker import update_tracker
from youtube_upload import is_youtube_quota_error, read_upload_attempted_filenames, upload_clips

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    """End-of-run summary."""

    videos_found: int
    clips_generated: int
    clips_considered_for_upload: int
    uploads_completed: int
    uploads_skipped: int
    upload_failures: int


def main() -> None:
    """Run the full Shorts automation workflow."""
    args = parse_args()
    load_dotenv()
    ensure_directories()
    configure_logging()

    logger.info("Starting YouTube Shorts automation%s", " in clip-only mode" if args.clip_only else "")

    try:
        verify_ffmpeg_tools()
    except RuntimeError as exc:
        logger.exception("Startup check failed: %s", exc)
        print(f"Startup check failed: {exc}")
        return

    videos = discover_videos()
    generated_clips = generate_clips_for_videos(videos)

    clip_paths_to_consider = sorted({clip.clip_path for clip in generated_clips} | set(config.CLIPS_DIR.glob("clip_*.mp4")))
    pending_clip_paths = filter_not_attempted(clip_paths_to_consider)
    not_attempted = [] if args.clip_only else pending_clip_paths
    metadata_targets = sorted({clip.clip_path for clip in generated_clips}) if args.clip_only else pending_clip_paths
    metadata = generate_metadata_for_clips(metadata_targets)
    metadata_by_filename: dict[str, ClipMetadata] = {record.filename: record for record in metadata}
    schedule_times = generate_schedule(len(not_attempted))

    upload_results = upload_clips(not_attempted, metadata_by_filename, schedule_times) if not_attempted else []
    if not args.clip_only:
        update_tracker(videos, generated_clips)

    summary = RunSummary(
        videos_found=len(videos),
        clips_generated=len(generated_clips),
        clips_considered_for_upload=len(not_attempted),
        uploads_completed=sum(1 for result in upload_results if result.uploaded),
        uploads_skipped=sum(1 for result in upload_results if result.skipped),
        upload_failures=sum(1 for result in upload_results if result.error),
    )

    logger.info("Finished run: %s", summary)
    if any(result.error and is_youtube_quota_error(Exception(result.error)) for result in upload_results):
        logger.error("YouTube daily upload limit hit; stop uploading for today and try again tomorrow")
        print("")
        print("YOUTUBE DAILY UPLOAD LIMIT HIT")
        print("--------------------------------")
        print("YouTube rejected more uploads for today.")
        print("Stop uploading for the day, then press Run Now tomorrow to continue with the waiting clips.")
    print_summary(summary)


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description="Run the Shorts automation workflow.")
    parser.add_argument(
        "--clip-only",
        action="store_true",
        help="Only move input videos into generated clips; do not upload anything to YouTube.",
    )
    return parser.parse_args()


def filter_not_attempted(clip_paths: list[Path]) -> list[Path]:
    """Return current-run clips that have not already had an upload attempt."""
    attempted = read_upload_attempted_filenames()
    return [clip_path for clip_path in clip_paths if clip_path.name not in attempted]


def print_summary(summary: RunSummary) -> None:
    """Print a concise final summary."""
    print("")
    print("YouTube Shorts automation complete")
    print("----------------------------------")
    print(f"Videos found:              {summary.videos_found}")
    print(f"Clips generated:           {summary.clips_generated}")
    print(f"Clips queued for upload:   {summary.clips_considered_for_upload}")
    print(f"Uploads completed:         {summary.uploads_completed}")
    print(f"Uploads skipped:           {summary.uploads_skipped}")
    print(f"Upload failures:           {summary.upload_failures}")
    print(f"Log file:                  {config.APP_LOG_FILE}")


if __name__ == "__main__":
    main()
