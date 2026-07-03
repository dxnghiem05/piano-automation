"""YouTube OAuth, upload, and upload log persistence."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, ResumableUploadError

import config
from generate_metadata import ClipMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadRecord:
    """A persisted upload record."""

    clip_filename: str
    youtube_video_id: str
    title: str
    upload_time: str
    scheduled_publish_time: str
    status: str


@dataclass(frozen=True)
class UploadResult:
    """Result of an upload attempt."""

    clip_filename: str
    uploaded: bool
    skipped: bool
    youtube_video_id: str | None = None
    error: str | None = None


def get_youtube_service():
    """Authenticate with OAuth and return a YouTube Data API service."""
    if not config.CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file: {config.CREDENTIALS_FILE}. Add Google OAuth credentials.json first."
        )

    credentials: Credentials | None = None
    if config.TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(config.TOKEN_FILE),
            scopes=config.YOUTUBE_SCOPES,
        )

    if credentials and not credentials.has_scopes(config.YOUTUBE_SCOPES):
        credentials = None
        if config.TOKEN_FILE.exists():
            config.TOKEN_FILE.unlink()

    granted_scopes = set(getattr(credentials, "granted_scopes", None) or []) if credentials else set()
    if credentials and granted_scopes and not set(config.YOUTUBE_SCOPES).issubset(granted_scopes):
        logger.info("Saved Google token is missing required scopes and will be replaced")
        credentials = None
        if config.TOKEN_FILE.exists():
            config.TOKEN_FILE.unlink()

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            logger.warning("Saved Google token could not refresh and will be replaced: %s", exc)
            credentials = None
            if config.TOKEN_FILE.exists():
                config.TOKEN_FILE.unlink()

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.CREDENTIALS_FILE),
            scopes=config.YOUTUBE_SCOPES,
        )
        credentials = flow.run_local_server(port=0)

    config.TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=credentials)


def upload_clips(
    clip_paths: list[Path],
    metadata_by_filename: dict[str, ClipMetadata],
    schedule_times: list[datetime],
) -> list[UploadResult]:
    """Upload unsent clips to YouTube and persist results."""
    skipped_filenames = read_upload_attempted_filenames()
    service = None
    results: list[UploadResult] = []
    schedule_index = 0

    for clip_path in sorted(clip_paths):
        if clip_path.name in skipped_filenames:
            logger.info("Skipping clip with previous upload attempt: %s", clip_path.name)
            results.append(UploadResult(clip_filename=clip_path.name, uploaded=False, skipped=True))
            continue

        metadata = metadata_by_filename.get(clip_path.name)
        if not metadata:
            error = "missing metadata"
            logger.error("Skipping %s because metadata is missing", clip_path.name)
            results.append(UploadResult(clip_filename=clip_path.name, uploaded=False, skipped=False, error=error))
            continue

        if schedule_index >= len(schedule_times):
            error = "missing schedule time"
            logger.error("Skipping %s because no schedule time is available", clip_path.name)
            results.append(UploadResult(clip_filename=clip_path.name, uploaded=False, skipped=False, error=error))
            continue

        scheduled_time = schedule_times[schedule_index]
        schedule_index += 1

        try:
            if service is None:
                service = get_youtube_service()
            video_id = upload_single_clip(service, clip_path, metadata, scheduled_time)
            append_upload_record(
                UploadRecord(
                    clip_filename=clip_path.name,
                    youtube_video_id=video_id,
                    title=metadata.title,
                    upload_time=datetime.now().astimezone().isoformat(),
                    scheduled_publish_time=scheduled_time.isoformat(),
                    status="uploaded",
                )
            )
            skipped_filenames.add(clip_path.name)
            results.append(
                UploadResult(
                    clip_filename=clip_path.name,
                    uploaded=True,
                    skipped=False,
                    youtube_video_id=video_id,
                )
            )
            logger.info("Uploaded %s as YouTube video %s", clip_path.name, video_id)
        except (HttpError, ResumableUploadError, TimeoutError, OSError, ValueError) as exc:
            logger.exception("Upload failed for %s: %s", clip_path.name, exc)
            is_quota_error = is_youtube_quota_error(exc)
            append_upload_record(
                UploadRecord(
                    clip_filename=clip_path.name,
                    youtube_video_id="",
                    title=metadata.title,
                    upload_time=datetime.now().astimezone().isoformat(),
                    scheduled_publish_time=scheduled_time.isoformat(),
                    status="deferred_quota" if is_quota_error else "failed",
                )
            )
            if not is_quota_error:
                skipped_filenames.add(clip_path.name)
            results.append(
                UploadResult(
                    clip_filename=clip_path.name,
                    uploaded=False,
                    skipped=False,
                    error=str(exc),
                )
            )
            if is_fatal_youtube_error(exc):
                logger.error("Stopping upload batch because YouTube cannot accept more uploads right now")
                break

    return results


def upload_single_clip(service, clip_path: Path, metadata: ClipMetadata, scheduled_time: datetime) -> str:
    """Upload one clip to YouTube as a scheduled private video."""
    request_body = {
        "snippet": {
            "title": metadata.title,
            "description": metadata.description,
            "categoryId": config.YOUTUBE_CATEGORY,
        },
        "status": {
            "privacyStatus": config.YOUTUBE_PRIVACY_STATUS,
            "publishAt": scheduled_time.isoformat(),
            "selfDeclaredMadeForKids": config.YOUTUBE_MADE_FOR_KIDS,
        },
    }

    media = MediaFileUpload(
        str(clip_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )
    request = service.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media,
    )

    response = None
    while response is None:
        _status, response = request.next_chunk()

    video_id = response.get("id")
    if not video_id:
        raise ValueError(f"YouTube upload response did not include a video id: {response}")
    return str(video_id)


def read_upload_records() -> list[UploadRecord]:
    """Read uploads_log.csv, recovering from malformed rows where possible."""
    if not config.UPLOAD_LOG_FILE.exists():
        return []

    records: list[UploadRecord] = []
    try:
        with config.UPLOAD_LOG_FILE.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                clip_filename = (row.get("clip_filename") or "").strip()
                if not clip_filename:
                    logger.warning("Skipping upload log row with missing clip filename")
                    continue
                records.append(
                    UploadRecord(
                        clip_filename=clip_filename,
                        youtube_video_id=row.get("youtube_video_id") or "",
                        title=row.get("title") or "",
                        upload_time=row.get("upload_time") or "",
                        scheduled_publish_time=row.get("scheduled_publish_time") or "",
                        status=row.get("status") or "",
                    )
                )
    except csv.Error as exc:
        backup = config.UPLOAD_LOG_FILE.with_suffix(".csv.corrupt")
        config.UPLOAD_LOG_FILE.replace(backup)
        logger.exception("uploads_log.csv was corrupt and was moved to %s: %s", backup, exc)

    return records


def read_uploaded_filenames() -> set[str]:
    """Return clip filenames that have already been uploaded successfully."""
    return {
        record.clip_filename
        for record in read_upload_records()
        if record.clip_filename and record.status == "uploaded"
    }


def read_upload_attempted_filenames() -> set[str]:
    """Return clip filenames that have already had any upload attempt."""
    return {
        record.clip_filename
        for record in read_upload_records()
        if record.clip_filename and record.status in {"uploaded", "failed"}
    }


def read_stale_deferred_filenames(now: datetime | None = None) -> set[str]:
    """Return deferred_quota clips whose intended slot has already passed.

    A clip is "stale" when every deferred_quota slot recorded for it is in the
    past. Such clips can never be scheduled again (YouTube cannot publish to a
    past time) and re-queuing them would let old June clips jump ahead of the
    current prepared backlog. Deferred clips whose latest slot is still in the
    future (e.g. clip_000515 -> July 11) are NOT stale and stay eligible for
    retry. Uploaded/failed clips are handled separately and ignored here.
    """
    reference = now or datetime.now().astimezone()

    latest_deferred: dict[str, datetime] = {}
    uploaded: set[str] = set()
    for record in read_upload_records():
        name = record.clip_filename
        if not name:
            continue
        if record.status == "uploaded":
            uploaded.add(name)
            continue
        if record.status != "deferred_quota":
            continue
        slot = _parse_schedule_time(record.scheduled_publish_time)
        if slot is None:
            continue
        current = latest_deferred.get(name)
        if current is None or slot > current:
            latest_deferred[name] = slot

    return {
        name
        for name, slot in latest_deferred.items()
        if name not in uploaded and slot < reference
    }


def _parse_schedule_time(value: str) -> datetime | None:
    """Parse an ISO scheduled_publish_time into a timezone-aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def append_upload_record(record: UploadRecord) -> None:
    """Append an upload record to uploads_log.csv."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = config.UPLOAD_LOG_FILE.exists()
    with config.UPLOAD_LOG_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "clip_filename",
                "youtube_video_id",
                "title",
                "upload_time",
                "scheduled_publish_time",
                "status",
            ],
        )
        if not file_exists:
            writer.writeheader()
        writer.writerow(
            {
                "clip_filename": record.clip_filename,
                "youtube_video_id": record.youtube_video_id,
                "title": record.title,
                "upload_time": record.upload_time,
                "scheduled_publish_time": record.scheduled_publish_time,
                "status": record.status,
            }
        )


def is_fatal_youtube_error(error: Exception) -> bool:
    """Return True for project/account errors that will fail every upload in the batch."""
    text = str(error)
    fatal_markers = [
        "accessNotConfigured",
        "YouTube Data API v3 has not been used",
        "it is disabled",
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "Quota exceeded",
        "Video Uploads per day",
        "uploadLimitExceeded",
        "exceeded the number of videos",
    ]
    return any(marker in text for marker in fatal_markers)


def is_youtube_quota_error(error: Exception) -> bool:
    """Return True when YouTube rejected the upload because daily/user upload capacity is exhausted."""
    text = str(error)
    quota_markers = [
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "Quota exceeded",
        "Video Uploads per day",
        "uploadLimitExceeded",
        "exceeded the number of videos",
    ]
    return any(marker in text for marker in quota_markers)
