"""Locate ffmpeg tools across terminal and macOS app launch environments."""

from __future__ import annotations

import shutil
from pathlib import Path


STANDARD_BINARY_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)


def media_tool_path(tool: str) -> str | None:
    """Return an executable media-tool path, including standard Homebrew paths."""
    discovered = shutil.which(tool)
    if discovered:
        return discovered

    for directory in STANDARD_BINARY_DIRS:
        candidate = directory / tool
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None


def require_media_tool(tool: str) -> str:
    """Return a media-tool path or raise a clear setup error."""
    path = media_tool_path(tool)
    if path:
        return path
    raise RuntimeError(
        f"Missing required tool: {tool}. Install ffmpeg with Homebrew: brew install ffmpeg"
    )
