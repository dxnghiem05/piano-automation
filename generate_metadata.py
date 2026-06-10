"""Metadata generation for YouTube Shorts clips."""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger(__name__)
TITLE_PATTERN = re.compile(r"^[A-Z]+ 🍃( #[A-Za-z0-9_]+)+$")
HASHTAG_PATTERN = re.compile(r"^#[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class ClipMetadata:
    """Metadata for one clip."""

    filename: str
    title: str
    description: str


def generate_metadata_for_clips(clip_paths: list[Path]) -> list[ClipMetadata]:
    """Generate and persist metadata for clips that do not already have it."""
    existing = read_metadata()
    records: list[ClipMetadata] = list(existing.values())

    for clip_path in sorted(clip_paths):
        if clip_path.name in existing:
            logger.info("Metadata already exists for %s", clip_path.name)
            continue

        metadata = ClipMetadata(
            filename=clip_path.name,
            title=next_title(),
            description=build_description(),
        )
        records.append(metadata)
        existing[clip_path.name] = metadata
        logger.info("Generated metadata for %s", clip_path.name)

    write_metadata(records)
    return [existing[clip_path.name] for clip_path in sorted(clip_paths) if clip_path.name in existing]


def read_metadata() -> dict[str, ClipMetadata]:
    """Read metadata.csv, recovering from malformed rows where possible."""
    if not config.METADATA_FILE.exists():
        return {}

    records: dict[str, ClipMetadata] = {}
    try:
        with config.METADATA_FILE.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                filename = (row.get("filename") or "").strip()
                title = row.get("title") or ""
                description = row.get("description") or ""
                if not filename:
                    logger.warning("Skipping metadata row with missing filename")
                    continue
                records[filename] = ClipMetadata(filename=filename, title=title, description=description)
    except csv.Error as exc:
        backup = config.METADATA_FILE.with_suffix(".csv.corrupt")
        config.METADATA_FILE.replace(backup)
        logger.exception("metadata.csv was corrupt and was moved to %s: %s", backup, exc)

    return records


def write_metadata(records: list[ClipMetadata]) -> None:
    """Write metadata.csv."""
    config.METADATA_DIR.mkdir(parents=True, exist_ok=True)
    with config.METADATA_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["filename", "title", "description"])
        writer.writeheader()
        for record in sorted(records, key=lambda item: item.filename):
            writer.writerow(
                {
                    "filename": record.filename,
                    "title": record.title,
                    "description": record.description,
                }
            )


def next_title() -> str:
    """Generate a title while cycling through mood words before reuse."""
    mood_words = normalized_mood_words()
    used_indices = read_title_state()
    if len(used_indices) >= len(mood_words):
        used_indices = []

    for index, word in enumerate(mood_words):
        if index not in used_indices:
            used_indices.append(index)
            write_title_state(used_indices)
            return format_title(word)

    write_title_state([0])
    return format_title(mood_words[0])


def build_description() -> str:
    """Build the configured description text."""
    return " ".join(normalized_hashtags())


def format_title(word: str) -> str:
    """Format a title as one all-caps word, a leaf emoji, and configured hashtags."""
    normalized = normalize_mood_word(word)
    title = f"{normalized} 🍃 {build_description()}"
    if not TITLE_PATTERN.fullmatch(title):
        raise ValueError(f"Invalid generated title: {title}")
    return title


def normalized_mood_words() -> list[str]:
    """Return valid one-word all-caps mood words."""
    words = [normalize_mood_word(word) for word in config.MOOD_WORDS]
    if not words:
        raise ValueError("MOOD_WORDS must contain at least one valid word")
    return words


def normalize_mood_word(word: str) -> str:
    """Normalize one mood word and reject values that would create bad captions."""
    normalized = str(word).strip().upper()
    if not normalized.isalpha():
        raise ValueError(f"MOOD_WORDS entries must be one alphabetic word only: {word!r}")
    return normalized


def normalized_hashtags() -> list[str]:
    """Return configured hashtags after validation."""
    hashtags = [str(hashtag).strip() for hashtag in config.HASHTAGS]
    invalid = [hashtag for hashtag in hashtags if not HASHTAG_PATTERN.fullmatch(hashtag)]
    if invalid:
        raise ValueError(f"Invalid hashtags in config.py: {', '.join(invalid)}")
    return hashtags


def read_title_state() -> list[int]:
    """Read title cycle state."""
    if not config.TITLE_STATE_FILE.exists():
        return []

    try:
        payload = json.loads(config.TITLE_STATE_FILE.read_text(encoding="utf-8"))
        used = payload.get("used_indices", [])
        return [int(index) for index in used if isinstance(index, int) or str(index).isdigit()]
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read title state; restarting title cycle: %s", exc)
        return []


def write_title_state(used_indices: list[int]) -> None:
    """Persist title cycle state."""
    config.PROCESSING_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"used_indices": used_indices}
    config.TITLE_STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
