"""Build the data-science project dataset from upload and stats history."""

from __future__ import annotations

import csv
import logging
import math
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
PROJECT_EXPERIMENT_BLOCKS = {
    "Week 0": {
        "block": "pre_project_baseline",
        "focus": "baseline",
        "question": "What was normal performance before the 12-week experiment began?",
        "planned_change": "No controlled change; use as historical baseline.",
    },
    "Week 1": {
        "block": "baseline",
        "focus": "baseline",
        "question": "What does standard posting performance look like before planned changes?",
        "planned_change": "Keep schedule, captions, hashtags, and posting volume consistent.",
    },
    "Week 2": {
        "block": "baseline",
        "focus": "baseline",
        "question": "What does standard posting performance look like before planned changes?",
        "planned_change": "Keep schedule, captions, hashtags, and posting volume consistent.",
    },
    "Week 3": {
        "block": "posting_time",
        "focus": "posting_time_ab_test",
        "question": "Do morning/afternoon posts or evening posts perform better?",
        "planned_change": "Compare 9 AM-3 PM against 4 PM-9 PM.",
    },
    "Week 4": {
        "block": "posting_time",
        "focus": "posting_time_ab_test",
        "question": "Do morning/afternoon posts or evening posts perform better?",
        "planned_change": "Compare 9 AM-3 PM against 4 PM-9 PM.",
    },
    "Week 5": {
        "block": "caption_style",
        "focus": "caption_ab_test",
        "question": "Do emotional caption words or energy caption words perform better?",
        "planned_change": "Compare emotional words against energy words.",
    },
    "Week 6": {
        "block": "caption_style",
        "focus": "caption_ab_test",
        "question": "Do emotional caption words or energy caption words perform better?",
        "planned_change": "Compare emotional words against energy words.",
    },
    "Week 7": {
        "block": "posting_frequency",
        "focus": "posting_frequency_test",
        "question": "Does increasing posts during strong windows increase total growth or dilute performance?",
        "planned_change": "Compare standard hourly posting against higher-density best-window posting.",
    },
    "Week 8": {
        "block": "posting_frequency",
        "focus": "posting_frequency_test",
        "question": "Does increasing posts during strong windows increase total growth or dilute performance?",
        "planned_change": "Compare standard hourly posting against higher-density best-window posting.",
    },
    "Week 9": {
        "block": "content_type",
        "focus": "content_classification",
        "question": "Which content types and visual formats perform best?",
        "planned_change": "Label clips by content type, audience context, shot type, and orientation.",
    },
    "Week 10": {
        "block": "content_type",
        "focus": "content_classification",
        "question": "Which content types and visual formats perform best?",
        "planned_change": "Label clips by content type, audience context, shot type, and orientation.",
    },
    "Week 11": {
        "block": "modeling",
        "focus": "prediction_model",
        "question": "Which features best predict a high-performing clip?",
        "planned_change": "Train and interpret high-performing prediction models.",
    },
    "Week 12": {
        "block": "capstone",
        "focus": "final_recommendations",
        "question": "What posting, caption, content, and platform strategy should come next?",
        "planned_change": "Summarize findings and build final recommendations.",
    },
}
CONTENT_LABEL_FIELDS = (
    "solo_piano",
    "church_performance",
    "jazz_ensemble",
    "live_audience",
    "close_up",
    "wide_shot",
    "vertical",
    "horizontal",
)


@dataclass(frozen=True)
class ProjectDatasetRow:
    """One analytics-ready clip row for the 12-week data science project."""

    project_week: str
    project_phase: str
    week_start_date: str
    week_end_date: str
    experiment_block: str
    experiment_focus: str
    experiment_question: str
    planned_change: str
    experiment_variant: str
    clip_id: str
    source_video: str
    platform: str
    caption_word: str
    caption_style: str
    hashtags: str
    hashtag_count: str
    clip_length_seconds: str
    scheduled_time: str
    actual_publish_time: str
    posting_hour: str
    posting_time_group: str
    day_of_week: str
    views_1h: str
    views_6h: str
    views_24h: str
    views_24h_log1p: str
    likes_24h: str
    comments_24h: str
    like_rate_24h: str
    engagement_rate_24h: str
    privacy_status: str
    upload_status: str
    video_orientation: str
    content_type: str
    shot_type: str
    audience_context: str
    solo_piano: str
    church_performance: str
    jazz_ensemble: str
    live_audience: str
    close_up: str
    wide_shot: str
    vertical: str
    horizontal: str
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
        publish_at = project_publish_datetime(record)
        one_hour = checkpoint_stats(snapshots, publish_at, hours=1)
        six_hours = checkpoint_stats(snapshots, publish_at, hours=6)
        twenty_four_hours = checkpoint_stats(snapshots, publish_at, hours=24)
        views_24h = int_or_zero(twenty_four_hours.get("view_count", ""))
        likes_24h = int_or_zero(twenty_four_hours.get("like_count", ""))
        comments_24h = int_or_zero(twenty_four_hours.get("comment_count", ""))
        clip_info = clip_info_for(record.clip_filename, tracker_by_clip, previous_project_rows, clip_info_by_name)
        caption_word = parse_caption_word(record.title)
        project_week = project_week_label(publish_at)
        experiment = experiment_plan(project_week)
        week_start, week_end = project_week_dates(project_week)
        label_flags = content_label_flags(clip_info)

        rows.append(
            ProjectDatasetRow(
                project_week=project_week,
                project_phase=project_phase(publish_at),
                week_start_date=week_start,
                week_end_date=week_end,
                experiment_block=experiment["block"],
                experiment_focus=experiment["focus"],
                experiment_question=experiment["question"],
                planned_change=experiment["planned_change"],
                experiment_variant=experiment_variant(project_week, publish_at, caption_word, clip_info),
                clip_id=record.clip_filename,
                source_video=clip_info.get("source_video", ""),
                platform="YouTube Shorts",
                caption_word=caption_word,
                caption_style=caption_style(caption_word),
                hashtags=parse_hashtags(record.title),
                hashtag_count=str(hashtag_count(record.title)),
                clip_length_seconds=clip_info.get("clip_length_seconds", ""),
                scheduled_time=record.scheduled_publish_time,
                actual_publish_time=record.scheduled_publish_time or record.upload_time,
                posting_hour=publish_at.strftime("%-I %p") if publish_at else "",
                posting_time_group=posting_time_group(publish_at),
                day_of_week=publish_at.strftime("%A") if publish_at else "",
                views_1h=str(int_or_zero(one_hour.get("view_count", ""))) if one_hour else "",
                views_6h=str(int_or_zero(six_hours.get("view_count", ""))) if six_hours else "",
                views_24h=str(views_24h) if twenty_four_hours else "",
                views_24h_log1p=f"{math.log1p(views_24h):.4f}" if twenty_four_hours else "",
                likes_24h=str(likes_24h) if twenty_four_hours else "",
                comments_24h=str(comments_24h) if twenty_four_hours else "",
                like_rate_24h=rate(likes_24h, views_24h) if twenty_four_hours else "",
                engagement_rate_24h=rate(likes_24h + comments_24h, views_24h) if twenty_four_hours else "",
                privacy_status=latest.get("privacy_status", ""),
                upload_status=latest.get("upload_status", record.status),
                video_orientation=clip_info.get("video_orientation", ""),
                content_type=clip_info.get("content_type", "unlabeled"),
                shot_type=clip_info.get("shot_type", "unlabeled"),
                audience_context=clip_info.get("audience_context", "unlabeled"),
                solo_piano=label_flags["solo_piano"],
                church_performance=label_flags["church_performance"],
                jazz_ensemble=label_flags["jazz_ensemble"],
                live_audience=label_flags["live_audience"],
                close_up=label_flags["close_up"],
                wide_shot=label_flags["wide_shot"],
                vertical=label_flags["vertical"],
                horizontal=label_flags["horizontal"],
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


def project_publish_datetime(record) -> datetime | None:
    """Return the timestamp used for project week and posting-time analysis."""
    return parse_datetime(record.scheduled_publish_time) or parse_datetime(record.upload_time)


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
        with pd.ExcelWriter(config.PROJECT_DATASET_EXCEL_FILE) as writer:
            dataframe.to_excel(writer, sheet_name="tracker", index=False)
            experiment_plan_dataframe().to_excel(writer, sheet_name="experiment_plan", index=False)
            label_guide_dataframe().to_excel(writer, sheet_name="label_guide", index=False)
    except ImportError:
        logger.warning("Could not write project Excel dataset because openpyxl is not installed")


def experiment_plan_dataframe() -> pd.DataFrame:
    """Return the 12-week experiment plan as an Excel sheet."""
    rows = []
    for week in ["Week 0", *[f"Week {number}" for number in range(1, config.PROJECT_TOTAL_WEEKS + 1)]]:
        start, end = project_week_dates(week)
        plan = experiment_plan(week)
        rows.append(
            {
                "project_week": week,
                "week_start_date": start,
                "week_end_date": end,
                "experiment_block": plan["block"],
                "experiment_focus": plan["focus"],
                "experiment_question": plan["question"],
                "planned_change": plan["planned_change"],
            }
        )
    return pd.DataFrame(rows)


def label_guide_dataframe() -> pd.DataFrame:
    """Return manual label definitions for model-ready content fields."""
    rows = [
        ("content_type", "solo_piano", "Solo piano performance clip."),
        ("content_type", "church_performance", "Church, worship, or service-performance clip."),
        ("content_type", "jazz_ensemble", "Jazz ensemble, band, or multi-instrument performance clip."),
        ("audience_context", "live_audience", "Visible or audible live audience context."),
        ("shot_type", "close_up", "Close-up shot focused on hands, instrument, or performer."),
        ("shot_type", "wide_shot", "Wide shot showing stage, room, ensemble, or full scene."),
        ("video_orientation", "vertical", "Portrait/vertical video format."),
        ("video_orientation", "horizontal", "Landscape/horizontal video format."),
    ]
    return pd.DataFrame(
        [{"field": field, "value": value, "definition": definition} for field, value, definition in rows]
    )


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
        "shot_type": previous_row.get("shot_type", "") or "unlabeled",
        "audience_context": previous_row.get("audience_context", "") or "unlabeled",
    }
    for field in CONTENT_LABEL_FIELDS:
        info[field] = previous_row.get(field, "")

    if not info["clip_length_seconds"] or not info["video_orientation"]:
        clip_path = config.CLIPS_DIR / clip_filename
        probe = probe_video(clip_path)
        if probe:
            duration, width, height = probe
            if not info["clip_length_seconds"]:
                info["clip_length_seconds"] = f"{duration:.1f}"
            if not info["video_orientation"]:
                info["video_orientation"] = orientation_label(width, height)

    if info["video_orientation"] == "vertical":
        info["vertical"] = "1"
        info["horizontal"] = info.get("horizontal") or "0"
    elif info["video_orientation"] == "horizontal":
        info["horizontal"] = "1"
        info["vertical"] = info.get("vertical") or "0"

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


def hashtag_count(title: str) -> int:
    """Return the number of hashtags in a title."""
    return sum(1 for token in (title or "").split() if token.startswith("#"))


def caption_style(word: str) -> str:
    """Classify caption word into experiment groups."""
    if word in EMOTIONAL_WORDS:
        return "emotional"
    if word in ENERGY_WORDS:
        return "energy"
    return "other"


def experiment_plan(project_week: str) -> dict[str, str]:
    """Return the planned experiment metadata for a project week."""
    if project_week.startswith("After Week"):
        return {
            "block": "post_project",
            "focus": "follow_up",
            "question": "How does performance behave after the planned 12-week project?",
            "planned_change": "Use as post-project follow-up data.",
        }
    return PROJECT_EXPERIMENT_BLOCKS.get(
        project_week,
        {
            "block": "unplanned",
            "focus": "unplanned",
            "question": "No planned experiment assigned.",
            "planned_change": "No planned change assigned.",
        },
    )


def experiment_variant(
    project_week: str,
    timestamp: datetime | None,
    caption_word: str,
    clip_info: dict[str, str],
) -> str:
    """Return the row-level group used for the active week experiment."""
    plan = experiment_plan(project_week)
    focus = plan["focus"]
    if focus == "posting_time_ab_test":
        return posting_time_group(timestamp)
    if focus == "caption_ab_test":
        return caption_style(caption_word)
    if focus == "content_classification":
        values = [
            clip_info.get("content_type", "unlabeled"),
            clip_info.get("shot_type", "unlabeled"),
            clip_info.get("audience_context", "unlabeled"),
            clip_info.get("video_orientation", "unlabeled"),
        ]
        return " | ".join(value for value in values if value and value != "unlabeled") or "unlabeled"
    if focus == "posting_frequency_test":
        return posting_time_group(timestamp)
    if focus in {"prediction_model", "final_recommendations"}:
        return "model_ready"
    if focus == "baseline":
        return "standard_workflow"
    return focus


def content_label_flags(clip_info: dict[str, str]) -> dict[str, str]:
    """Return model-ready 0/1 content label flags."""
    labels = {field: normalize_flag(clip_info.get(field, "")) for field in CONTENT_LABEL_FIELDS}
    content_type = clip_info.get("content_type", "")
    shot_type = clip_info.get("shot_type", "")
    audience_context = clip_info.get("audience_context", "")
    orientation = clip_info.get("video_orientation", "")

    if content_type == "solo_piano":
        labels["solo_piano"] = "1"
    if content_type == "church_performance":
        labels["church_performance"] = "1"
    if content_type == "jazz_ensemble":
        labels["jazz_ensemble"] = "1"
    if audience_context == "live_audience":
        labels["live_audience"] = "1"
    if shot_type == "close_up":
        labels["close_up"] = "1"
    if shot_type == "wide_shot":
        labels["wide_shot"] = "1"
    if orientation == "vertical":
        labels["vertical"] = "1"
    if orientation == "horizontal":
        labels["horizontal"] = "1"

    return labels


def normalize_flag(value: str) -> str:
    """Normalize manual label flags to 0/1/blank strings."""
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return "1"
    if normalized in {"0", "false", "no", "n"}:
        return "0"
    return ""


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
    week_number = ((date - start).days // 7) + 1
    if week_number > config.PROJECT_TOTAL_WEEKS:
        return f"After Week {config.PROJECT_TOTAL_WEEKS}"
    return f"Week {week_number}"


def project_week_dates(project_week: str) -> tuple[str, str]:
    """Return ISO start/end dates for a project week label."""
    start = datetime.fromisoformat(config.PROJECT_WEEK_1_START_DATE).date()
    if project_week == "Week 0":
        return "", (start - timedelta(days=1)).isoformat()
    if project_week.startswith("After Week"):
        after_start = start + timedelta(weeks=config.PROJECT_TOTAL_WEEKS)
        return after_start.isoformat(), ""

    digits = "".join(char for char in project_week if char.isdigit())
    if not digits:
        return "", ""
    week_number = int(digits)
    week_start = start + timedelta(weeks=week_number - 1)
    week_end = week_start + timedelta(days=6)
    return week_start.isoformat(), week_end.isoformat()


def project_phase(timestamp: datetime | None) -> str:
    """Return the project phase label."""
    return "baseline" if project_week_label(timestamp) == "Week 0" else "official"


def rate(numerator: int, denominator: int) -> str:
    """Format a decimal rate."""
    if denominator <= 0:
        return "0"
    return f"{numerator / denominator:.4f}"
