"""Timezone-aware scheduling for YouTube publish times."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config


def generate_schedule(
    count: int,
    start_from: datetime | None = None,
    existing_times: set[datetime] | None = None,
) -> list[datetime]:
    """Generate future schedule timestamps without double-booking existing upload slots."""
    if count <= 0:
        return []

    timezone = ZoneInfo(config.TIMEZONE)
    now = start_from.astimezone(timezone) if start_from else datetime.now(timezone)
    occupied = normalize_existing_times(existing_times or read_existing_schedule_times(), timezone)
    latest_future = max((timestamp for timestamp in occupied if timestamp > now), default=None)

    if latest_future and config.SCHEDULE_AFTER_EXISTING_UPLOADS:
        cursor = latest_future + timedelta(hours=config.POST_INTERVAL_HOURS)
    else:
        cursor = next_candidate_after(now)
    cursor = normalize_candidate(cursor, timezone)

    timestamps: list[datetime] = []
    while len(timestamps) < count:
        if cursor > now and is_posting_hour(cursor) and cursor not in occupied:
            timestamps.append(cursor)
            occupied.add(cursor)

        cursor += timedelta(hours=config.POST_INTERVAL_HOURS)
        cursor = normalize_candidate(cursor, timezone)

    return timestamps


def to_youtube_publish_at(timestamp: datetime) -> str:
    """Format a timestamp as RFC3339 for YouTube."""
    return timestamp.isoformat()


def read_existing_schedule_times() -> set[datetime]:
    """Read real YouTube scheduled/uploaded slots from uploads_log.csv."""
    if not config.UPLOAD_LOG_FILE.exists():
        return set()

    timestamps: set[datetime] = set()
    with config.UPLOAD_LOG_FILE.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            status = row.get("status", "")
            scheduled = row.get("scheduled_publish_time", "")
            if status != "uploaded" or not scheduled:
                continue
            try:
                timestamps.add(datetime.fromisoformat(scheduled))
            except ValueError:
                continue
    return timestamps


def normalize_existing_times(timestamps: set[datetime], timezone: ZoneInfo) -> set[datetime]:
    """Normalize existing timestamps to comparable local hour slots."""
    normalized = set()
    for timestamp in timestamps:
        local = timestamp.astimezone(timezone) if timestamp.tzinfo else timestamp.replace(tzinfo=timezone)
        normalized.add(local.replace(minute=0, second=0, microsecond=0))
    return normalized


def next_candidate_after(timestamp: datetime) -> datetime:
    """Return the next top-of-hour candidate after a timestamp."""
    cursor = timestamp.replace(minute=0, second=0, microsecond=0)
    if cursor <= timestamp:
        cursor += timedelta(hours=1)
    return cursor


def normalize_candidate(timestamp: datetime, timezone: ZoneInfo) -> datetime:
    """Move a candidate timestamp into the configured posting window."""
    cursor = timestamp.astimezone(timezone)
    cursor = cursor.replace(minute=0, second=0, microsecond=0)

    if cursor.hour < config.POST_START_HOUR:
        return cursor.replace(hour=config.POST_START_HOUR)

    if cursor.hour > config.POST_END_HOUR:
        next_day = cursor.date() + timedelta(days=1)
        return datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            config.POST_START_HOUR,
            tzinfo=timezone,
        )

    return cursor


def is_posting_hour(timestamp: datetime) -> bool:
    """Return True when a timestamp is inside the configured posting window."""
    return config.POST_START_HOUR <= timestamp.hour <= config.POST_END_HOUR
