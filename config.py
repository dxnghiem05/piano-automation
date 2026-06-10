"""Editable application settings for the YouTube Shorts automation app."""

from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

INPUT_DIR = BASE_DIR / "input"
PROCESSING_DIR = BASE_DIR / "processing"
CLIPS_DIR = BASE_DIR / "clips"
UPLOADED_DIR = BASE_DIR / "uploaded"
METADATA_DIR = BASE_DIR / "metadata"
LOGS_DIR = BASE_DIR / "logs"
CREDENTIALS_DIR = BASE_DIR / "credentials"

CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"
METADATA_FILE = METADATA_DIR / "metadata.csv"
UPLOAD_LOG_FILE = LOGS_DIR / "uploads_log.csv"
YOUTUBE_STATS_HISTORY_FILE = LOGS_DIR / "youtube_stats_history.csv"
PRIVACY_OVERRIDES_FILE = LOGS_DIR / "privacy_overrides.csv"
TIKTOK_SCHEDULE_FILE = LOGS_DIR / "tiktok_schedule.csv"
APP_LOG_FILE = LOGS_DIR / "app.log"
CLIP_COUNTER_FILE = PROCESSING_DIR / "clip_counter.txt"
TITLE_STATE_FILE = PROCESSING_DIR / "title_state.json"

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov"}

CLIP_SOURCE_MIN_SECONDS = 60
SKIP_INTRO_SECONDS = 8
CLIP_MIN_SECONDS = 20
CLIP_MAX_SECONDS = 30
MINIMUM_CLIP_LENGTH = 15

POST_START_HOUR = 9
POST_END_HOUR = 19
POST_INTERVAL_HOURS = 1
SCHEDULE_AFTER_EXISTING_UPLOADS = True
TIMEZONE = "America/Los_Angeles"

YOUTUBE_CATEGORY = "10"
YOUTUBE_PRIVACY_STATUS = "private"
YOUTUBE_MADE_FOR_KIDS = False
MAX_UPLOADS_PER_RUN = 11

MOOD_WORDS = [
    "VIBE",
    "FLOW",
    "DREAM",
    "GENTLE",
    "PEACE",
    "SERENE",
    "BLISS",
    "MIDNIGHT",
    "SOFT",
    "FLOAT",
    "NOSTALGIA",
    "WANDER",
    "FREE",
    "CALM",
    "STILL",
]

HASHTAGS = [
    "#foryou",
    "#shorts",
    "#viral",
    "#music",
    "#church",
    "#Jesus",
    "#God",
    "#love",
]

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
UPLOAD_TIMEOUT_SECONDS = 900
LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 5
