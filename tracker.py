"""Run tracker generation for source videos, clips, and platform posting status."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from generate_clips import GeneratedClip, SourceVideo
from generate_metadata import read_metadata
from youtube_upload import read_upload_records
from youtube_stats import fetch_youtube_stats

logger = logging.getLogger(__name__)

TRACKER_CSV_FILE = config.METADATA_DIR / "video_tracker.csv"
TRACKER_EXCEL_FILE = config.METADATA_DIR / "video_tracker.xlsx"


@dataclass(frozen=True)
class TrackerRow:
    """One source/clip/platform status row in the tracker."""

    source_filename: str
    source_path: str
    source_duration_seconds: float | str
    clip_filename: str
    clip_path: str
    clip_start_seconds: float | str
    clip_duration_seconds: float | str
    title: str
    youtube_status: str
    youtube_video_id: str
    youtube_scheduled_publish_time: str
    youtube_upload_time: str
    youtube_view_count: str
    youtube_like_count: str
    youtube_comment_count: str
    youtube_favorite_count: str
    youtube_privacy_status: str
    youtube_upload_status: str
    youtube_stats_last_checked: str
    tiktok_status: str
    tiktok_video_id: str
    tiktok_scheduled_publish_time: str
    last_updated: str


def update_tracker(videos: list[SourceVideo], generated_clips: list[GeneratedClip]) -> None:
    """Write tracker CSV and Excel files from current app state."""
    config.METADATA_DIR.mkdir(parents=True, exist_ok=True)

    metadata_by_filename = read_metadata()
    upload_by_filename = {record.clip_filename: record for record in read_upload_records()}
    youtube_stats_by_id = fetch_youtube_stats(
        [record.youtube_video_id for record in upload_by_filename.values() if record.youtube_video_id]
    )
    clip_by_filename = {clip.clip_path.name: clip for clip in generated_clips}
    now = datetime.now().astimezone().isoformat()

    rows: list[TrackerRow] = []
    source_video_by_name = {video.path.name: video for video in videos}

    for clip_path in sorted(config.CLIPS_DIR.glob("clip_*.mp4")):
        clip = clip_by_filename.get(clip_path.name)
        metadata = metadata_by_filename.get(clip_path.name)
        upload = upload_by_filename.get(clip_path.name)
        youtube_stats = youtube_stats_by_id.get(upload.youtube_video_id) if upload else None

        rows.append(
            TrackerRow(
                source_filename=clip.source_path.name if clip else "",
                source_path=str(clip.source_path) if clip else "",
                source_duration_seconds=source_video_by_name.get(clip.source_path.name).duration_seconds
                if clip and clip.source_path.name in source_video_by_name
                else "",
                clip_filename=clip_path.name,
                clip_path=str(clip_path),
                clip_start_seconds=clip.start_seconds if clip else "",
                clip_duration_seconds=clip.duration_seconds if clip else "",
                title=metadata.title if metadata else "",
                youtube_status=upload.status if upload else "not_uploaded",
                youtube_video_id=upload.youtube_video_id if upload else "",
                youtube_scheduled_publish_time=upload.scheduled_publish_time if upload else "",
                youtube_upload_time=upload.upload_time if upload else "",
                youtube_view_count=youtube_stats.view_count if youtube_stats else "",
                youtube_like_count=youtube_stats.like_count if youtube_stats else "",
                youtube_comment_count=youtube_stats.comment_count if youtube_stats else "",
                youtube_favorite_count=youtube_stats.favorite_count if youtube_stats else "",
                youtube_privacy_status=youtube_stats.privacy_status if youtube_stats else "",
                youtube_upload_status=youtube_stats.upload_status if youtube_stats else "",
                youtube_stats_last_checked=now if youtube_stats else "",
                tiktok_status="not_configured",
                tiktok_video_id="",
                tiktok_scheduled_publish_time="",
                last_updated=now,
            )
        )

    for video in videos:
        has_generated_clip = any(clip.source_path == video.path for clip in generated_clips)
        if has_generated_clip:
            continue
        rows.append(
            TrackerRow(
                source_filename=video.path.name,
                source_path=str(video.path),
                source_duration_seconds=video.duration_seconds,
                clip_filename="",
                clip_path="",
                clip_start_seconds="",
                clip_duration_seconds="",
                title="",
                youtube_status="no_clip_generated",
                youtube_video_id="",
                youtube_scheduled_publish_time="",
                youtube_upload_time="",
                youtube_view_count="",
                youtube_like_count="",
                youtube_comment_count="",
                youtube_favorite_count="",
                youtube_privacy_status="",
                youtube_upload_status="",
                youtube_stats_last_checked="",
                tiktok_status="not_configured",
                tiktok_video_id="",
                tiktok_scheduled_publish_time="",
                last_updated=now,
            )
        )

    write_tracker(rows)


def write_tracker(rows: list[TrackerRow]) -> None:
    """Persist tracker as CSV and Excel."""
    fieldnames = list(TrackerRow.__dataclass_fields__.keys())
    with TRACKER_CSV_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)

    dataframe = pd.DataFrame([row.__dict__ for row in rows], columns=fieldnames)
    try:
        dataframe.to_excel(TRACKER_EXCEL_FILE, index=False)
        logger.info("Updated tracker files: %s and %s", TRACKER_CSV_FILE, TRACKER_EXCEL_FILE)
    except ImportError:
        logger.warning("Could not write Excel tracker because openpyxl is not installed")
        logger.info("Updated tracker CSV: %s", TRACKER_CSV_FILE)
