"""Build the data-science project dataset from upload and stats history."""

from __future__ import annotations

import csv
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config
from stats_tracker import int_or_zero, read_stats_history
from youtube_upload import read_upload_records

logger = logging.getLogger(__name__)

EMOTIONAL_WORDS = {"PEACE", "SERENE", "GENTLE", "STILL"}
ENERGY_WORDS = {"FLOW", "VIBE", "FREE", "DREAM"}


@dataclass(frozen=True)
class ProjectDatasetRow:
    """One analytics-ready clip row for the 12-week data science project."""

    project_week: str
    project_phase: str
    clip_id: str
    source_video: str
    platform: str
    caption_word: str
    caption_style: str
    hashtags: str
    clip_length_seconds: str
    scheduled_time: str
    actual_publish_time: str
    posting_hour: str
    posting_time_group: str
    day_of_week: str
    views_1h: str
    views_6h: str
    views_24h: str
    likes_24h: str
    comments_24h: str
    like_rate_24h: str
    engagement_rate_24h: str
    privacy_status: str
    upload_status: str
    video_orientation: str
    content_type: str
    high_performing: str
    youtube_video_id: str
    last_checked_at: str


def refresh_project_dataset() -> list[ProjectDatasetRow]:
    """Write the current analytics project dataset to CSV and Excel."""
    rows = build_project_dataset()
    write_project_dataset(rows)
    return rows


def read_project_dataset() -> list[dict[str, str]]:
    """Read the saved project dataset, creating it if needed."""
    if not config.PROJECT_DATASET_CSV_FILE.exists():
        refresh_project_dataset()

    if not config.PROJECT_DATASET_CSV_FILE.exists():
        return []

    with config.PROJECT_DATASET_CSV_FILE.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def build_project_dataset() -> list[ProjectDatasetRow]:
    """Join uploads, stats snapshots, and local clip metadata into project rows."""
    stats_by_video = group_stats_by_video(read_stats_history())
    tracker_by_clip = read_existing_tracker_rows()
    previous_project_rows = read_existing_project_rows()
    clip_info_by_name: dict[str, dict[str, str]] = {}

    rows: list[ProjectDatasetRow] = []
    for record in dedupe_upload_records():
        if not record.youtube_video_id:
            continue

        snapshots = stats_by_video.get(record.youtube_video_id, [])
        latest = snapshots[-1] if snapshots else {}
        scheduled_at = parse_datetime(record.scheduled_publish_time)
        one_hour = checkpoint_stats(snapshots, scheduled_at, hours=1)
        six_hours = checkpoint_stats(snapshots, scheduled_at, hours=6)
        twenty_four_hours = checkpoint_stats(snapshots, scheduled_at, hours=24)
        views_24h = int_or_zero(twenty_four_hours.get("view_count", ""))
        likes_24h = int_or_zero(twenty_four_hours.get("like_count", ""))
        comments_24h = int_or_zero(twenty_four_hours.get("comment_count", ""))
        clip_info = clip_info_for(record.clip_filename, tracker_by_clip, previous_project_rows, clip_info_by_name)
        caption_word = parse_caption_word(record.title)

        rows.append(
            ProjectDatasetRow(
                project_week=project_week_label(scheduled_at),
                project_phase=project_phase(scheduled_at),
                clip_id=record.clip_filename,
                source_video=clip_info.get("source_video", ""),
                platform="YouTube Shorts",
                caption_word=caption_word,
                caption_style=caption_style(caption_word),
                hashtags=parse_hashtags(record.title),
                clip_length_seconds=clip_info.get("clip_length_seconds", ""),
                scheduled_time=record.scheduled_publish_time,
                actual_publish_time=record.scheduled_publish_time,
                posting_hour=scheduled_at.strftime("%-I %p") if scheduled_at else "",
                posting_time_group=posting_time_group(scheduled_at),
                day_of_week=scheduled_at.strftime("%A") if scheduled_at else "",
                views_1h=str(int_or_zero(one_hour.get("view_count", ""))) if one_hour else "",
                views_6h=str(int_or_zero(six_hours.get("view_count", ""))) if six_hours else "",
                views_24h=str(views_24h) if twenty_four_hours else "",
                likes_24h=str(likes_24h) if twenty_four_hours else "",
                comments_24h=str(comments_24h) if twenty_four_hours else "",
                like_rate_24h=rate(likes_24h, views_24h) if twenty_four_hours else "",
                engagement_rate_24h=rate(likes_24h + comments_24h, views_24h) if twenty_four_hours else "",
                privacy_status=latest.get("privacy_status", ""),
                upload_status=latest.get("upload_status", record.status),
                video_orientation=clip_info.get("video_orientation", ""),
                content_type=clip_info.get("content_type", "unlabeled"),
                high_performing="",
                youtube_video_id=record.youtube_video_id,
                last_checked_at=latest.get("checked_at", ""),
            )
        )

    return add_high_performing_labels(rows)


def dedupe_upload_records():
    """Return the newest successful upload record per clip."""
    latest = {}
    for record in read_upload_records():
        if record.status != "uploaded" or not record.youtube_video_id:
            continue
        previous = latest.get(record.clip_filename)
        if previous is None or record.upload_time > previous.upload_time:
            latest[record.clip_filename] = record
    return sorted(latest.values(), key=lambda item: item.scheduled_publish_time or item.upload_time)


def group_stats_by_video(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Group stats snapshots by YouTube video id."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        video_id = row.get("youtube_video_id", "")
        if not video_id:
            continue
        grouped.setdefault(video_id, []).append(row)

    for snapshots in grouped.values():
        snapshots.sort(key=lambda row: row.get("checked_at", ""))
    return grouped


def checkpoint_stats(
    snapshots: list[dict[str, str]],
    scheduled_at: datetime | None,
    hours: int,
) -> dict[str, str]:
    """Return the first snapshot at or after a scheduled checkpoint."""
    if not snapshots or not scheduled_at:
        return {}

    target = scheduled_at + timedelta(hours=hours)
    fallback = {}
    for row in snapshots:
        checked_at = parse_datetime(row.get("checked_at", ""))
        if not checked_at:
            continue
        fallback = row
        if checked_at >= target:
            return row

    now = datetime.now(scheduled_at.tzinfo or ZoneInfo(config.TIMEZONE))
    return fallback if now >= target else {}


def add_high_performing_labels(rows: list[ProjectDatasetRow]) -> list[ProjectDatasetRow]:
    """Label rows with above-median 24-hour views when available."""
    values = sorted(int(row.views_24h) for row in rows if row.views_24h.isdigit())
    if not values:
        return rows

    median = values[len(values) // 2]
    labeled = []
    for row in rows:
        if not row.views_24h.isdigit():
            labeled.append(row)
            continue
        labeled.append(ProjectDatasetRow(**{**row.__dict__, "high_performing": "1" if int(row.views_24h) >= median else "0"}))
    return labeled


def write_project_dataset(rows: list[ProjectDatasetRow]) -> None:
    """Persist the project dataset as CSV and Excel."""
    config.METADATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = list(ProjectDatasetRow.__dataclass_fields__.keys())

    with config.PROJECT_DATASET_CSV_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)

    dataframe = pd.DataFrame([row.__dict__ for row in rows], columns=fieldnames)
    try:
        dataframe.to_excel(config.PROJECT_DATASET_EXCEL_FILE, index=False)
    except ImportError:
        logger.warning("Could not write project Excel dataset because openpyxl is not installed")


def read_existing_tracker_rows() -> dict[str, dict[str, str]]:
    """Read source/clip details from the existing tracker when available."""
    tracker_path = config.METADATA_DIR / "video_tracker.csv"
    if not tracker_path.exists():
        return {}
    with tracker_path.open("r", newline="", encoding="utf-8") as file:
        return {row.get("clip_filename", ""): row for row in csv.DictReader(file) if row.get("clip_filename")}


def read_existing_project_rows() -> dict[str, dict[str, str]]:
    """Read prior project dataset rows so expensive local clip probes can be reused."""
    if not config.PROJECT_DATASET_CSV_FILE.exists():
        return {}
    with config.PROJECT_DATASET_CSV_FILE.open("r", newline="", encoding="utf-8") as file:
        return {row.get("clip_id", ""): row for row in csv.DictReader(file) if row.get("clip_id")}


def clip_info_for(
    clip_filename: str,
    tracker_by_clip: dict[str, dict[str, str]],
    previous_project_rows: dict[str, dict[str, str]],
    cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Return local clip features for project analysis."""
    if clip_filename in cache:
        return cache[clip_filename]

    tracker_row = tracker_by_clip.get(clip_filename, {})
    previous_row = previous_project_rows.get(clip_filename, {})
    info = {
        "source_video": tracker_row.get("source_filename", "") or previous_row.get("source_video", ""),
        "clip_length_seconds": tracker_row.get("clip_duration_seconds", "") or previous_row.get("clip_length_seconds", ""),
        "video_orientation": previous_row.get("video_orientation", ""),
        "content_type": previous_row.get("content_type", "") or "unlabeled",
    }

    if not info["clip_length_seconds"] or not info["video_orientation"]:
        clip_path = config.CLIPS_DIR / clip_filename
        probe = probe_video(clip_path)
        if probe:
            duration, width, height = probe
            if not info["clip_length_seconds"]:
                info["clip_length_seconds"] = f"{duration:.1f}"
            if not info["video_orientation"]:
                info["video_orientation"] = orientation_label(width, height)

    cache[clip_filename] = info
    return info


def probe_video(path: Path) -> tuple[float, int, int] | None:
    """Read duration and dimensions with ffprobe when the local clip exists."""
    if not path.exists():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    parts = [part.strip() for part in result.stdout.strip().split(",")]
    if len(parts) < 3:
        return None
    try:
        width = int(float(parts[0]))
        height = int(float(parts[1]))
        duration = float(parts[2])
    except ValueError:
        return None
    return duration, width, height


def orientation_label(width: int, height: int) -> str:
    """Return a simple video orientation label."""
    if height > width:
        return "vertical"
    if width > height:
        return "horizontal"
    return "square"


def parse_datetime(value: str) -> datetime | None:
    """Parse ISO datetimes defensively."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_caption_word(title: str) -> str:
    """Extract the one-word caption from a generated title."""
    return (title or "").split(" ", 1)[0].strip().upper()


def parse_hashtags(title: str) -> str:
    """Extract hashtags from a title."""
    return " ".join(token for token in (title or "").split() if token.startswith("#"))


def caption_style(word: str) -> str:
    """Classify caption word into experiment groups."""
    if word in EMOTIONAL_WORDS:
        return "emotional"
    if word in ENERGY_WORDS:
        return "energy"
    return "other"


def posting_time_group(timestamp: datetime | None) -> str:
    """Classify scheduled posting hour into the planned A/B test groups."""
    if not timestamp:
        return ""
    if 9 <= timestamp.hour <= 15:
        return "morning_afternoon"
    if 16 <= timestamp.hour <= 21:
        return "evening"
    return "outside_test_window"


def project_week_label(timestamp: datetime | None) -> str:
    """Return Week 0 for pre-project rows, then Week 1+ from the project start date."""
    if not timestamp:
        return ""
    start = datetime.fromisoformat(config.PROJECT_WEEK_1_START_DATE).date()
    date = timestamp.astimezone(ZoneInfo(config.TIMEZONE)).date() if timestamp.tzinfo else timestamp.date()
    if date < start:
        return "Week 0"
    return f"Week {((date - start).days // 7) + 1}"


def project_phase(timestamp: datetime | None) -> str:
    """Return the project phase label."""
    return "baseline" if project_week_label(timestamp) == "Week 0" else "official"


def rate(numerator: int, denominator: int) -> str:
    """Format a decimal rate."""
    if denominator <= 0:
        return "0"
    return f"{numerator / denominator:.4f}"
