"""YouTube statistics history tracking and dashboard summaries."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import config
from youtube_stats import fetch_youtube_stats
from youtube_upload import read_upload_records


@dataclass(frozen=True)
class StatsSnapshot:
    """One saved YouTube stats snapshot."""

    checked_at: str
    checked_date: str
    clip_filename: str
    youtube_video_id: str
    title: str
    scheduled_publish_time: str
    scheduled_hour: str
    view_count: str
    like_count: str
    comment_count: str
    favorite_count: str
    privacy_status: str
    upload_status: str


def refresh_youtube_stats_history() -> list[StatsSnapshot]:
    """Fetch current YouTube stats and append a historical snapshot."""
    upload_records = [record for record in read_upload_records() if record.youtube_video_id]
    stats_by_id = fetch_youtube_stats([record.youtube_video_id for record in upload_records])
    now = datetime.now().astimezone()
    snapshots: list[StatsSnapshot] = []

    for record in upload_records:
        stats = stats_by_id.get(record.youtube_video_id)
        if not stats:
            continue

        snapshots.append(
            StatsSnapshot(
                checked_at=now.isoformat(),
                checked_date=now.date().isoformat(),
                clip_filename=record.clip_filename,
                youtube_video_id=record.youtube_video_id,
                title=record.title,
                scheduled_publish_time=record.scheduled_publish_time,
                scheduled_hour=scheduled_hour(record.scheduled_publish_time),
                view_count=stats.view_count,
                like_count=stats.like_count,
                comment_count=stats.comment_count,
                favorite_count=stats.favorite_count,
                privacy_status=stats.privacy_status,
                upload_status=stats.upload_status,
            )
        )

    append_snapshots(snapshots)
    return snapshots


def append_snapshots(snapshots: list[StatsSnapshot]) -> None:
    """Append snapshots to youtube_stats_history.csv."""
    if not snapshots:
        return

    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = config.YOUTUBE_STATS_HISTORY_FILE.exists()
    fieldnames = list(StatsSnapshot.__dataclass_fields__.keys())

    with config.YOUTUBE_STATS_HISTORY_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for snapshot in snapshots:
            writer.writerow(snapshot.__dict__)


def read_stats_history() -> list[dict[str, str]]:
    """Read all saved YouTube stats snapshots."""
    if not config.YOUTUBE_STATS_HISTORY_FILE.exists():
        return []

    with config.YOUTUBE_STATS_HISTORY_FILE.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def latest_video_stats() -> list[dict[str, str]]:
    """Return the newest stats row for each YouTube video."""
    latest: dict[str, dict[str, str]] = {}
    for row in read_stats_history():
        video_id = row.get("youtube_video_id", "")
        if not video_id:
            continue
        if video_id not in latest or row.get("checked_at", "") > latest[video_id].get("checked_at", ""):
            latest[video_id] = row

    return sorted(
        latest.values(),
        key=lambda row: int_or_zero(row.get("view_count", "")),
        reverse=True,
    )


def daily_total_views() -> list[dict[str, int | str]]:
    """Return total latest-per-day views for all videos."""
    latest_by_day_video: dict[tuple[str, str], int] = {}
    for row in read_stats_history():
        date = row.get("checked_date", "")
        video_id = row.get("youtube_video_id", "")
        if not date or not video_id:
            continue
        latest_by_day_video[(date, video_id)] = int_or_zero(row.get("view_count", ""))

    totals: defaultdict[str, int] = defaultdict(int)
    for (date, _video_id), views in latest_by_day_video.items():
        totals[date] += views

    return [{"date": date, "views": totals[date]} for date in sorted(totals)]


def best_posting_hours() -> list[dict[str, int | float | str]]:
    """Summarize latest views by scheduled posting hour."""
    groups: defaultdict[str, list[int]] = defaultdict(list)
    for row in latest_video_stats():
        hour = row.get("scheduled_hour", "")
        if not hour:
            continue
        groups[hour].append(int_or_zero(row.get("view_count", "")))

    summaries = []
    for hour, values in groups.items():
        average = sum(values) / len(values)
        summaries.append(
            {
                "hour": hour,
                "video_count": len(values),
                "average_views": round(average, 1),
                "best_views": max(values),
            }
        )

    return sorted(summaries, key=lambda row: float(row["average_views"]), reverse=True)


def best_posting_days() -> list[dict[str, int | float | str]]:
    """Summarize latest views by scheduled posting weekday (Mon-Sun)."""
    groups: defaultdict[str, list[int]] = defaultdict(list)
    for row in latest_video_stats():
        day = weekday_name(row.get("scheduled_publish_time", ""))
        if not day:
            continue
        groups[day].append(int_or_zero(row.get("view_count", "")))

    summaries = []
    for day, values in groups.items():
        average = sum(values) / len(values)
        summaries.append(
            {
                "day": day,
                "video_count": len(values),
                "average_views": round(average, 1),
                "best_views": max(values),
            }
        )

    return sorted(summaries, key=lambda row: float(row["average_views"]), reverse=True)


def weekday_name(value: str) -> str:
    """Extract the local weekday name from an ISO timestamp."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%A")
    except ValueError:
        return ""


def scheduled_hour(value: str) -> str:
    """Extract local scheduled hour from an ISO timestamp."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%I %p").lstrip("0")
    except ValueError:
        return ""


def int_or_zero(value: str) -> int:
    """Parse an int string safely."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
