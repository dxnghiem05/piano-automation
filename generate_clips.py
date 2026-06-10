"""Video discovery and clip generation using ffmpeg and ffprobe."""

from __future__ import annotations

import json
import logging
import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceVideo:
    """A discovered source video and its measured duration."""

    path: Path
    duration_seconds: float


@dataclass(frozen=True)
class GeneratedClip:
    """A generated short clip."""

    source_path: Path
    clip_path: Path
    start_seconds: float
    duration_seconds: float


def ensure_directories() -> None:
    """Create all application folders if they are missing."""
    for directory in [
        config.INPUT_DIR,
        config.PROCESSING_DIR,
        config.CLIPS_DIR,
        config.UPLOADED_DIR,
        config.METADATA_DIR,
        config.LOGS_DIR,
        config.CREDENTIALS_DIR,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def verify_ffmpeg_tools() -> None:
    """Verify ffmpeg and ffprobe are available on PATH."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            f"Missing required tool(s): {', '.join(missing)}. Install ffmpeg with Homebrew: brew install ffmpeg"
        )


def discover_videos(input_dir: Path = config.INPUT_DIR) -> list[SourceVideo]:
    """Find valid source videos recursively in the input folder."""
    logger.info("Scanning for videos in %s", input_dir)
    videos: list[SourceVideo] = []

    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in config.SUPPORTED_VIDEO_EXTENSIONS:
            continue

        try:
            duration = get_video_duration(path)
        except Exception as exc:
            logger.exception("Skipping invalid or unreadable video %s: %s", path, exc)
            continue

        if duration <= 0:
            logger.error("Skipping video with non-positive duration: %s", path)
            continue

        videos.append(SourceVideo(path=path, duration_seconds=duration))
        logger.info("Discovered video: %s (%.2f seconds)", path, duration)

    return videos


def get_video_duration(path: Path) -> float:
    """Read actual video duration with ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    return float(payload["format"]["duration"])


def generate_clips_for_videos(videos: Iterable[SourceVideo]) -> list[GeneratedClip]:
    """Generate clips for all source videos."""
    generated: list[GeneratedClip] = []
    for video in videos:
        try:
            clips = generate_clips_for_video(video)
            generated.extend(clips)
            if clips:
                move_processed_source(video.path)
            else:
                logger.warning("No clips generated for %s; source left in input folder", video.path)
        except Exception as exc:
            logger.exception("Failed to process source video %s: %s", video.path, exc)
    return generated


def generate_clips_for_video(video: SourceVideo) -> list[GeneratedClip]:
    """Split one source video into varied Shorts clips after skipping the intro."""
    logger.info("Generating clips from %s", video.path)
    clips: list[GeneratedClip] = []
    start = min(float(config.SKIP_INTRO_SECONDS), video.duration_seconds)

    while start < video.duration_seconds:
        remaining = video.duration_seconds - start
        if remaining < config.MINIMUM_CLIP_LENGTH:
            logger.info(
                "Discarding %.2f-second remainder from %s because it is shorter than %d seconds",
                remaining,
                video.path,
                config.MINIMUM_CLIP_LENGTH,
            )
            break

        clip_duration = choose_clip_duration(remaining)
        clip_number = next_clip_number()
        output_path = config.CLIPS_DIR / f"clip_{clip_number:06d}.mp4"

        run_ffmpeg_clip(video.path, output_path, start, clip_duration)
        clips.append(
            GeneratedClip(
                source_path=video.path,
                clip_path=output_path,
                start_seconds=start,
                duration_seconds=clip_duration,
            )
        )
        logger.info("Generated clip %s", output_path)
        start += clip_duration

    return clips


def choose_clip_duration(remaining_seconds: float) -> float:
    """Choose a clip duration using the configured 20-30 second range."""
    if config.CLIP_MIN_SECONDS > config.CLIP_MAX_SECONDS:
        raise ValueError("CLIP_MIN_SECONDS cannot be greater than CLIP_MAX_SECONDS")

    if remaining_seconds <= config.CLIP_MAX_SECONDS:
        return remaining_seconds

    return float(random.randint(config.CLIP_MIN_SECONDS, config.CLIP_MAX_SECONDS))


def run_ffmpeg_clip(source: Path, output: Path, start_seconds: float, duration_seconds: float) -> None:
    """Create a YouTube Shorts-compatible MP4 clip while preserving orientation and aspect ratio."""
    temp_output = config.PROCESSING_DIR / f"{output.stem}.partial{output.suffix}"
    if temp_output.exists():
        temp_output.unlink()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-ss",
        _format_seconds(start_seconds),
        "-i",
        str(source),
        "-t",
        _format_seconds(duration_seconds),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        str(temp_output),
    ]

    try:
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=600)
        temp_output.replace(output)
    except subprocess.CalledProcessError as exc:
        logger.error("ffmpeg failed for %s: %s", source, exc.stderr)
        if temp_output.exists():
            temp_output.unlink()
        raise


def next_clip_number() -> int:
    """Get and persist the next global clip number."""
    current = 0
    if config.CLIP_COUNTER_FILE.exists():
        try:
            current = int(config.CLIP_COUNTER_FILE.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            logger.warning("Clip counter was invalid; rebuilding from existing clips")
            current = highest_existing_clip_number()

    next_number = max(current, highest_existing_clip_number()) + 1
    config.CLIP_COUNTER_FILE.write_text(str(next_number), encoding="utf-8")
    return next_number


def highest_existing_clip_number() -> int:
    """Find the highest clip number already present in the clips folder."""
    highest = 0
    for path in config.CLIPS_DIR.glob("clip_*.mp4"):
        try:
            highest = max(highest, int(path.stem.split("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return highest


def move_processed_source(source: Path) -> Path:
    """Move a fully processed source video to uploaded/, avoiding overwrites."""
    destination = unique_destination(config.UPLOADED_DIR / source.name)
    shutil.move(str(source), str(destination))
    logger.info("Moved processed source %s to %s", source, destination)
    return destination


def unique_destination(path: Path) -> Path:
    """Return a non-colliding destination path."""
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}_{index:04d}{suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find a unique destination for {path}")


def _format_seconds(value: float) -> str:
    """Format seconds for ffmpeg arguments."""
    if math.isclose(value, round(value), abs_tol=0.001):
        return str(int(round(value)))
    return f"{value:.3f}"
