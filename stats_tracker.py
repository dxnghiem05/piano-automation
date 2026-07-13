"""YouTube statistics history tracking and dashboard summaries."""

from __future__ import annotations

import csv
import io
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


# The history CSV is strictly append-only (append_snapshots always opens it in
# "a" mode and every row ends in a newline), so we never need to re-parse the
# whole file. This cache keeps the parsed rows plus the byte offset we last
# consumed; on each read we parse ONLY the newly appended tail. A month of
# 30-min snapshots for ~34 videos is already 180k+ rows / 35 MB, and a full
# reparse on every page load is what made reloads get slower and slower. The
# raw file is never modified — only read — so no historical data is affected.
_HISTORY_CACHE: dict[str, object] = {"size": 0, "mtime_ns": 0, "header": None, "rows": []}


def read_stats_history() -> list[dict[str, str]]:
    """Read all saved YouTube stats snapshots (incrementally cached)."""
    path = config.YOUTUBE_STATS_HISTORY_FILE
    cache = _HISTORY_CACHE

    if not path.exists():
        cache.update(size=0, mtime_ns=0, header=None, rows=[])
        return []

    try:
        stat = path.stat()
    except OSError:
        return cache["rows"]  # type: ignore[return-value]

    # Unchanged since last read -> return the cached rows untouched.
    if (
        cache["header"] is not None
        and stat.st_size == cache["size"]
        and stat.st_mtime_ns == cache["mtime_ns"]
    ):
        return cache["rows"]  # type: ignore[return-value]

    # Append-only fast path: file only grew, so parse just the new tail.
    if cache["header"] is not None and cache["size"] and stat.st_size > cache["size"]:  # type: ignore[operator]
        with path.open("rb") as file:
            file.seek(cache["size"])  # type: ignore[arg-type]
            tail = file.read()
        # Only consume up to the last complete line, so a snapshot being written
        # concurrently can never leave a torn final row in the cache.
        cut = tail.rfind(b"\n") + 1
        consumed = tail[:cut]
        if consumed:
            reader = csv.DictReader(
                io.StringIO(consumed.decode("utf-8", "replace")),
                fieldnames=cache["header"],  # type: ignore[arg-type]
            )
            cache["rows"].extend(reader)  # type: ignore[attr-defined]
        cache["size"] = cache["size"] + len(consumed)  # type: ignore[operator]
        cache["mtime_ns"] = stat.st_mtime_ns
        return cache["rows"]  # type: ignore[return-value]

    # Full parse: first load, or the file shrank / was rotated / reset.
    data = path.read_bytes()
    cut = data.rfind(b"\n") + 1
    consumed = data[:cut]
    reader = csv.DictReader(io.StringIO(consumed.decode("utf-8", "replace")))
    rows = list(reader)
    cache["header"] = reader.fieldnames
    cache["rows"] = rows
    cache["size"] = len(consumed)
    cache["mtime_ns"] = stat.st_mtime_ns
    return rows


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
