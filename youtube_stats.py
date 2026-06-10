"""Fetch YouTube video statistics for uploaded clips."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from googleapiclient.errors import HttpError

from youtube_upload import get_youtube_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YouTubeVideoStats:
    """Stats and status returned by the YouTube Data API."""

    video_id: str
    view_count: str
    like_count: str
    comment_count: str
    favorite_count: str
    privacy_status: str
    upload_status: str
    youtube_publish_at: str
    youtube_title: str


def fetch_youtube_stats(video_ids: list[str]) -> dict[str, YouTubeVideoStats]:
    """Fetch statistics/status/snippet info for uploaded YouTube videos."""
    unique_ids = sorted({video_id for video_id in video_ids if video_id})
    if not unique_ids:
        return {}

    try:
        service = get_youtube_service()
    except Exception as exc:
        logger.warning("Could not authenticate for YouTube stats refresh: %s", exc)
        return {}

    stats_by_id: dict[str, YouTubeVideoStats] = {}
    for batch in chunked(unique_ids, 50):
        try:
            response = (
                service.videos()
                .list(
                    part="statistics,status,snippet",
                    id=",".join(batch),
                    maxResults=len(batch),
                )
                .execute()
            )
        except HttpError as exc:
            logger.warning("Could not fetch YouTube stats for batch %s: %s", batch, exc)
            continue

        for item in response.get("items", []):
            video_id = str(item.get("id", ""))
            statistics = item.get("statistics", {})
            status = item.get("status", {})
            snippet = item.get("snippet", {})
            if not video_id:
                continue
            stats_by_id[video_id] = YouTubeVideoStats(
                video_id=video_id,
                view_count=str(statistics.get("viewCount", "")),
                like_count=str(statistics.get("likeCount", "")),
                comment_count=str(statistics.get("commentCount", "")),
                favorite_count=str(statistics.get("favoriteCount", "")),
                privacy_status=str(status.get("privacyStatus", "")),
                upload_status=str(status.get("uploadStatus", "")),
                youtube_publish_at=str(status.get("publishAt", "")),
                youtube_title=str(snippet.get("title", "")),
            )

    return stats_by_id


def chunked(values: list[str], size: int) -> list[list[str]]:
    """Split a list into fixed-size chunks."""
    return [values[index : index + size] for index in range(0, len(values), size)]
