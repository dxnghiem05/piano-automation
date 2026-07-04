"""Local web dashboard for the Shorts automation app."""

from __future__ import annotations

import base64
import cgi
import csv
import hmac
import html
import json
import logging
import mimetypes
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import config
from generate_clips import ensure_directories
from logging_setup import configure_logging
from project_dataset import read_project_dataset, refresh_project_dataset
from stats_tracker import (
    best_posting_days,
    best_posting_hours,
    latest_video_stats,
    read_stats_history,
    refresh_youtube_stats_history,
)
from tracker import update_tracker
from youtube_upload import get_youtube_service, read_upload_records
from generate_metadata import read_metadata
from googleapiclient.errors import HttpError

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 - dotenv is optional; env vars still work without it
    pass

# --- Owner authentication -----------------------------------------------------
# The public can VIEW every page (read-only). Only the owner, who logs in with a
# password, can trigger actions (run, upload, refresh, privacy, tiktok queue).
# Auth turns on automatically when DASHBOARD_PASSWORD is set (in .env); with no
# password it stays off so local-only use is unaffected.
DASHBOARD_USER = (os.getenv("DASHBOARD_USER", "admin") or "admin").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
AUTH_REQUIRED = bool(DASHBOARD_PASSWORD)

# Shared <head> markup so every page loads the same Figtree web font (matches home).
FONT_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">'
)

# Injected for non-owner visitors: hides every action form (read-only view) and
# shows an "Owner login" button. Server-side auth on POST is the real guard; this
# is just the matching UI so visitors don't see controls that wouldn't work.
VIEWER_MODE_SNIPPET = """
<style id="viewer-mode">
  form[action="/run"], form[action="/clip-only"], form[action="/upload"],
  form[action="/refresh-stats"], form[action="/queue/privacy"],
  form[action="/tiktok-schedule"], [data-owner-only] { display: none !important; }
  .owner-login-badge {
    position: fixed; right: 16px; bottom: 16px; z-index: 99999;
    background: #1ed760; color: #05140b;
    font: 700 13px/1 -apple-system, BlinkMacSystemFont, sans-serif;
    padding: 11px 16px; border-radius: 999px; text-decoration: none;
    box-shadow: 0 8px 20px rgba(0,0,0,.45);
  }
</style>
<a href="/login" class="owner-login-badge">Owner login</a>
"""


def viewer_mode_html(page_html: str, is_owner: bool) -> str:
    """For non-owners, inject the read-only viewer overlay so the site can't be edited."""
    if is_owner:
        return page_html
    if "</body>" in page_html:
        return page_html.replace("</body>", VIEWER_MODE_SNIPPET + "</body>", 1)
    return page_html + VIEWER_MODE_SNIPPET

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8000
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
HOME_PREVIEW_CLIP_LIMIT = 3
QUEUE_PAGE_SIZE = 20
STATS_TABLE_PAGE_SIZE = 25
AUTO_STATS_REFRESH_MINUTES = 2
APP_FONT_STACK = (
    '"Figtree", "CircularSp", "Circular Std", "Avenir Next", "Helvetica Neue", '
    'Helvetica, Arial, sans-serif'
)
RUN_STATE = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "last_output": "",
    "last_error": "",
}
STATS_REFRESH_LOCK = threading.Lock()
LAST_AUTO_STATS_REFRESH_ATTEMPT: datetime | None = None


def is_safe_dashboard_path(value: str) -> bool:
    """Return True for local dashboard paths that are safe redirect targets."""
    if not value.startswith("/"):
        return False
    parsed = urlparse(value)
    if parsed.netloc or parsed.scheme:
        return False
    return parsed.path in {"/", "/stats", "/queue", "/tracker", "/tiktok-candidates"}


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the local dashboard."""

    def _is_owner(self) -> bool:
        """Return True if the request has valid owner credentials (or auth is disabled)."""
        if not AUTH_REQUIRED:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        user, _, password = decoded.partition(":")
        user_ok = hmac.compare_digest(user, DASHBOARD_USER)
        password_ok = hmac.compare_digest(password, DASHBOARD_PASSWORD)
        return user_ok and password_ok

    def _send_unauthorized(self) -> None:
        """Send a 401 that prompts the browser for owner credentials."""
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Piano Dashboard (owner)"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b"Owner login required to perform this action.")

    def _require_owner(self) -> bool:
        """Guard for state-changing requests: returns False (and 401s) if not the owner."""
        if self._is_owner():
            return True
        time.sleep(0.5)  # gentle brute-force slowdown
        self._send_unauthorized()
        return False

    def do_GET(self) -> None:
        """Handle dashboard GET routes."""
        parsed = urlparse(self.path)
        path = parsed.path
        owner = self._is_owner()

        if path == "/login":
            # Owners trigger the browser Basic-Auth prompt here, then land on home.
            if owner:
                self.redirect("/")
            else:
                self._send_unauthorized()
            return

        if path == "/":
            self.send_html(viewer_mode_html(render_dashboard(), owner))
            return

        if path == "/api/status":
            self.send_json(build_status())
            return

        if path == "/api/logs":
            if not owner:
                self.send_json({"lines": [], "text": "Live activity is visible to the owner only."})
                return
            self.send_json({"lines": live_dashboard_log_lines(), "text": live_dashboard_log_text()})
            return

        if path == "/tracker":
            self.send_html(viewer_mode_html(render_tracker_page(), owner))
            return

        if path == "/queue":
            query = parse_qs(parsed.query)
            self.send_html(
                viewer_mode_html(
                    render_queue_page(
                        parse_page(query.get("page", ["1"])[0]),
                        parse_queue_sort(query.get("sort", ["oldest"])[0]),
                    ),
                    owner,
                )
            )
            return

        if path == "/tiktok-candidates":
            query = parse_qs(parsed.query)
            selected_date = query.get("date", [""])[0]
            self.send_html(viewer_mode_html(render_tiktok_candidates_page(selected_date), owner))
            return

        if path == "/stats":
            query = parse_qs(parsed.query)
            selected_range = query.get("range", ["1d"])[0]
            selected_project_week = query.get("project_week", [""])[0]
            selected_project_sort = query.get("project_sort", ["recent"])[0]
            selected_stats_page = parse_page(query.get("stats_page", ["1"])[0])
            auto_refresh_started = auto_refresh_youtube_stats_if_stale()
            self.send_html(
                viewer_mode_html(
                    render_stats_page(
                        selected_range,
                        selected_project_week,
                        selected_project_sort,
                        selected_stats_page,
                        auto_refresh_started,
                    ),
                    owner,
                )
            )
            return

        if path == "/experiment":
            self.send_html(viewer_mode_html(render_experiment_page(), owner))
            return

        # Generated CSV exports (public — they only expose already-visible showcase data).
        if path == "/queue.csv":
            self.send_download_text("upload_queue.csv", queue_csv())
            return

        if path == "/tiktok-candidates.csv":
            self.send_download_text("tiktok_candidates_all_weeks.csv", tiktok_all_weeks_csv())
            return

        # Data-file downloads are owner-only (they are raw exports, not part of the showcase).
        if path in {"/tracker.csv", "/tracker.xlsx", "/project-data.csv", "/project-data.xlsx"}:
            if not owner:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            if path == "/tracker.csv":
                self.send_file(config.METADATA_DIR / "video_tracker.csv", download=True)
            elif path == "/tracker.xlsx":
                self.send_file(config.METADATA_DIR / "video_tracker.xlsx", download=True)
            elif path == "/project-data.csv":
                refresh_project_dataset()
                self.send_file(config.PROJECT_DATASET_CSV_FILE, download=True)
            else:
                refresh_project_dataset()
                self.send_file(config.PROJECT_DATASET_EXCEL_FILE, download=True)
            return

        if path.startswith("/clips/"):
            filename = Path(unquote(path.removeprefix("/clips/"))).name
            self.send_file(config.CLIPS_DIR / filename)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        """Handle dashboard POST routes."""
        parsed = urlparse(self.path)

        # Every state-changing action requires the owner login. Public visitors
        # (no/invalid credentials) get a 401 and cannot modify anything.
        if not self._require_owner():
            return

        if parsed.path == "/upload":
            self.handle_upload()
            return

        if parsed.path == "/run":
            start_run()
            if self.is_ajax_request():
                self.send_json({"ok": True, "message": "Started clipping and YouTube scheduling."})
                return
            self.redirect(self.redirect_back_path(default="/"))
            return

        if parsed.path == "/clip-only":
            start_clip_only()
            if self.is_ajax_request():
                self.send_json({"ok": True, "message": "Started clipping input videos."})
                return
            self.redirect(self.redirect_back_path(default="/"))
            return

        if parsed.path == "/refresh-stats":
            redirect_target = self.form_redirect_target(default=self.redirect_back_path(default="/stats"))
            start_stats_refresh()
            self.redirect(redirect_target)
            return

        if parsed.path == "/queue/privacy":
            self.handle_privacy_update()
            return

        if parsed.path == "/tiktok-schedule":
            self.handle_tiktok_schedule()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_upload(self) -> None:
        """Save uploaded videos into input/."""
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_UPLOAD_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload is too large")
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        upload_action = form.getfirst("upload_action", "input_only")

        files = form["videos"] if "videos" in form else []
        if not isinstance(files, list):
            files = [files]

        saved = 0
        for item in files:
            if not item.filename:
                continue
            filename = Path(item.filename).name
            suffix = Path(filename).suffix.lower()
            if suffix not in config.SUPPORTED_VIDEO_EXTENSIONS:
                continue
            destination = unique_input_destination(config.INPUT_DIR / filename)
            with destination.open("wb") as output:
                while True:
                    chunk = item.file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            saved += 1
            logger.info("Uploaded video through dashboard: %s", destination)

        logger.info("Dashboard upload complete; saved %d file(s)", saved)
        if saved and upload_action == "upload_and_clip":
            start_clip_only()
        if self.is_ajax_request():
            self.send_json(
                {
                    "ok": True,
                    "saved": saved,
                    "input_count": len(list_input_videos()),
                    "message": upload_message(saved, upload_action),
                }
            )
            return
        self.redirect("/")

    def handle_privacy_update(self) -> None:
        """Update privacy for one uploaded YouTube video."""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(raw_body)
        video_id = (form.get("youtube_video_id", [""])[0] or "").strip()
        privacy_status = (form.get("privacy_status", [""])[0] or "").strip()
        row_anchor = (form.get("row_anchor", [""])[0] or "").strip()

        if privacy_status not in {"public", "unlisted", "private"}:
            if self.is_ajax_request():
                self.send_json({"ok": False, "error": "Invalid privacy status"}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid privacy status")
            return
        if not video_id:
            if self.is_ajax_request():
                self.send_json({"ok": False, "error": "Missing YouTube video ID"}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing YouTube video ID")
            return

        try:
            update_youtube_privacy(video_id, privacy_status)
        except Exception as exc:
            logger.exception("Could not update YouTube privacy: %s", exc)
            RUN_STATE["last_error"] = f"Privacy update failed for {video_id}: {exc}"
            if self.is_ajax_request():
                self.send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "video_id": video_id,
                        "privacy_status": privacy_status,
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
        else:
            append_privacy_override(video_id, privacy_status)
            RUN_STATE["last_output"] = f"Updated {video_id} to {privacy_status}."
            RUN_STATE["last_error"] = ""
            if self.is_ajax_request():
                self.send_json(
                    {
                        "ok": True,
                        "video_id": video_id,
                        "privacy_status": privacy_status,
                        "message": RUN_STATE["last_output"],
                    }
                )
                return

        self.redirect(f"/queue#{row_anchor}" if row_anchor.startswith("queue-") else "/queue")

    def handle_tiktok_schedule(self) -> None:
        """Create local TikTok schedule rows for one stats day."""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(raw_body)
        stats_date = (form.get("stats_date", [""])[0] or "").strip()

        if stats_date not in stats_history_dates():
            self.send_error(HTTPStatus.BAD_REQUEST, "Unknown stats date")
            return

        created = create_tiktok_schedule_for_date(stats_date)
        if created:
            RUN_STATE["last_output"] = f"Queued {created} TikTok candidate(s) from {stats_date}."
            RUN_STATE["last_error"] = ""
        else:
            RUN_STATE["last_output"] = f"TikTok candidates from {stats_date} were already queued."
            RUN_STATE["last_error"] = ""

        self.redirect(f"/tiktok-candidates#day-{stats_date}")

    def is_ajax_request(self) -> bool:
        """Return whether the request expects a JSON response."""
        return self.headers.get("X-Requested-With", "") == "fetch"

    def send_html(self, body: str) -> None:
        """Send an HTML response."""
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, body: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        """Send a JSON response."""
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_file(self, path: Path, download: bool = False) -> None:
        """Send a local file if it exists."""
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def send_download_text(self, filename: str, text: str) -> None:
        """Send generated text (CSV) as a file download."""
        encoded = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        """Redirect to another dashboard page."""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def redirect_back_path(self, default: str = "/") -> str:
        """Return a safe local path from the request referrer."""
        referer = self.headers.get("Referer", "")
        if not referer:
            return default

        parsed = urlparse(referer)
        if parsed.netloc and parsed.netloc not in {f"{HOST}:{PORT}", f"localhost:{PORT}"}:
            return default

        path = parsed.path or default
        if path not in {"/", "/stats", "/queue", "/tracker", "/tiktok-candidates"}:
            return default

        return path + (f"?{parsed.query}" if parsed.query else "")

    def form_redirect_target(self, default: str = "/") -> str:
        """Read a safe redirect_to value from a small urlencoded form."""
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return default

        raw_body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(raw_body)
        target = (form.get("redirect_to", [""])[0] or "").strip()
        return target if is_safe_dashboard_path(target) else default

    def log_message(self, format: str, *args: object) -> None:
        """Route default HTTP logs into app logging."""
        message = format % args
        if any(path in message for path in ('GET /api/status ', 'GET /api/logs ', 'GET /api/v1/courses')):
            return
        logger.info("dashboard: " + format, *args)


def parse_page(value: str) -> int:
    """Parse a positive page number from a query value."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def parse_queue_sort(value: str) -> str:
    """Return a supported queue sort mode."""
    normalized = str(value or "").strip().lower()
    if normalized in {"recent", "latest", "oldest"}:
        return normalized
    return "oldest"


def queue_page_numbers(page: int, total_pages: int) -> list[int]:
    """Return compact page numbers around the current queue page."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    candidates = {1, total_pages, page - 1, page, page + 1}
    if page <= 3:
        candidates.update({2, 3, 4})
    if page >= total_pages - 2:
        candidates.update({total_pages - 3, total_pages - 2, total_pages - 1})

    return [number for number in sorted(candidates) if 1 <= number <= total_pages]


def sort_queue_rows(rows: list[dict[str, str]], sort_order: str) -> list[dict[str, str]]:
    """Sort queue rows for the selected view."""
    if sort_order == "recent":
        return sorted(rows, key=queue_clip_number, reverse=True)
    if sort_order == "latest":
        return sorted(rows, key=queue_latest_key, reverse=True)
    return sorted(rows, key=lambda row: row["sort_key"])


def queue_clip_number(row: dict[str, str]) -> int:
    """Return numeric clip id for newest-created sorting."""
    filename = row.get("clip_filename", "")
    digits = "".join(char for char in filename if char.isdigit())
    return int(digits) if digits else -1


def queue_latest_key(row: dict[str, str]) -> tuple[int, str, int]:
    """Return latest scheduled/upload key while keeping unscheduled clips after dated clips."""
    dated_value = row.get("scheduled_publish_time") or row.get("upload_time") or ""
    if dated_value:
        return (1, dated_value, queue_clip_number(row))
    return (0, "", queue_clip_number(row))


def append_privacy_override(video_id: str, privacy_status: str) -> None:
    """Persist the newest known privacy status for immediate queue display."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = config.PRIVACY_OVERRIDES_FILE.exists()
    with config.PRIVACY_OVERRIDES_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["youtube_video_id", "privacy_status", "updated_at"])
        if not file_exists:
            writer.writeheader()
        writer.writerow(
            {
                "youtube_video_id": video_id,
                "privacy_status": privacy_status,
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )


def read_privacy_overrides() -> dict[str, str]:
    """Read newest local privacy overrides by YouTube video ID."""
    if not config.PRIVACY_OVERRIDES_FILE.exists():
        return {}

    overrides: dict[str, str] = {}
    with config.PRIVACY_OVERRIDES_FILE.open("r", newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            video_id = row.get("youtube_video_id", "")
            privacy_status = row.get("privacy_status", "")
            if video_id and privacy_status:
                overrides[video_id] = privacy_status
    return overrides


def build_queue_rows() -> list[dict[str, str]]:
    """Build queue rows from upload records and pending clips."""
    latest_by_id = {row.get("youtube_video_id", ""): row for row in latest_video_stats()}
    privacy_overrides = read_privacy_overrides()
    metadata_by_filename = read_metadata()
    latest_records_by_clip: dict[str, object] = {}
    for record in read_upload_records():
        latest_records_by_clip[record.clip_filename] = record
    records = list(latest_records_by_clip.values())
    rows: list[dict[str, str]] = []
    seen_clips = set()

    for record in records:
        seen_clips.add(record.clip_filename)
        stats = latest_by_id.get(record.youtube_video_id, {})
        privacy = privacy_overrides.get(record.youtube_video_id) or stats.get("privacy_status") or "unknown"
        rows.append(
            {
                "clip_filename": record.clip_filename,
                "youtube_video_id": record.youtube_video_id,
                "title": record.title,
                "upload_time": record.upload_time,
                "scheduled_publish_time": record.scheduled_publish_time,
                "display_time": format_queue_time(record.scheduled_publish_time),
                "status": queue_status(record.status, record.scheduled_publish_time),
                "privacy_status": privacy,
                "sort_key": record.scheduled_publish_time or record.upload_time or "9999",
            }
        )

    for clip_path in list_clip_files():
        if clip_path.name in seen_clips:
            continue
        metadata = metadata_by_filename.get(clip_path.name)
        rows.append(
            {
                "clip_filename": clip_path.name,
                "youtube_video_id": "",
                "title": metadata.title if metadata else "",
                "upload_time": "",
                "scheduled_publish_time": "",
                "display_time": "Not scheduled",
                "status": "waiting",
                "privacy_status": "not uploaded",
                "sort_key": "9999-" + clip_path.name,
            }
        )

    return sorted(rows, key=lambda row: row["sort_key"])


def queue_row_anchor(row: dict[str, str]) -> str:
    """Return a stable HTML anchor id for a queue row."""
    raw_value = row.get("youtube_video_id") or row.get("clip_filename") or "item"
    safe = "".join(char if char.isalnum() else "-" for char in raw_value).strip("-")
    return f"queue-{safe or 'item'}"


def queue_status(status: str, scheduled_time: str) -> str:
    """Return user-facing queue status."""
    if status == "deferred_quota":
        return "deferred"
    if status == "failed":
        return "failed"
    if status != "uploaded":
        return status or "waiting"

    parsed = parse_iso_datetime(scheduled_time)
    if parsed and parsed > datetime.now(parsed.tzinfo).astimezone(parsed.tzinfo):
        return "scheduled"
    return "uploaded"


def format_queue_time(value: str) -> str:
    """Format scheduled time for the queue."""
    parsed = parse_iso_datetime(value)
    if not parsed:
        return value or "Not scheduled"
    return parsed.strftime("%b %-d, %Y %-I:%M %p")


def read_tracker_rows() -> list[dict[str, str]]:
    """Read tracker CSV rows for browser preview."""
    tracker_path = config.METADATA_DIR / "video_tracker.csv"
    if not tracker_path.exists():
        return []
    with tracker_path.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def stats_history_dates() -> list[str]:
    """Return available YouTube posting dates for TikTok candidate rows."""
    return sorted({date for row in latest_tiktok_stats_rows() if (date := posted_date_for_stats_row(row))})


def latest_tiktok_stats_rows() -> list[dict[str, str]]:
    """Return the newest stats row for each tracked YouTube video."""
    latest_by_video: dict[str, dict[str, str]] = {}
    for row in read_stats_history():
        video_id = row.get("youtube_video_id", "")
        if not video_id:
            continue
        if video_id not in latest_by_video or row.get("checked_at", "") > latest_by_video[video_id].get("checked_at", ""):
            latest_by_video[video_id] = row
    return list(latest_by_video.values())


def posted_date_for_stats_row(row: dict[str, str]) -> str:
    """Return local posting date when the video should already be published."""
    scheduled = parse_iso_datetime(row.get("scheduled_publish_time", ""))
    if not scheduled:
        return ""
    now = datetime.now(scheduled.tzinfo) if scheduled.tzinfo else datetime.now()
    if scheduled > now:
        return ""
    return scheduled.date().isoformat()


def tiktok_candidate_days() -> list[dict[str, object]]:
    """Return TikTok candidate rows for every stats day."""
    days = []
    for date in reversed(stats_history_dates()):
        candidates = tiktok_candidates_for_date(date)
        if candidates:
            days.append({"date": date, "candidates": candidates})
    return days


def tiktok_candidates_for_date(selected_date: str) -> list[dict[str, str | int | float]]:
    """Return top TikTok candidates from videos posted on one YouTube date."""
    candidates = []
    for row in latest_tiktok_stats_rows():
        if posted_date_for_stats_row(row) != selected_date:
            continue

        views = parse_stat_int(row.get("view_count", ""))
        likes = parse_stat_int(row.get("like_count", ""))
        comments = parse_stat_int(row.get("comment_count", ""))
        score = views + likes * 25 + comments * 50
        like_rate = (likes / views * 100) if views else 0
        candidates.append(
            {
                "clip_filename": row.get("clip_filename", ""),
                "youtube_video_id": row.get("youtube_video_id", ""),
                "title": row.get("title", ""),
                "views": views,
                "likes": likes,
                "comments": comments,
                "score": score,
                "like_rate": like_rate,
            }
        )

    return sorted(candidates, key=lambda item: (int(item["score"]), int(item["views"])), reverse=True)[:5]


def read_tiktok_schedule() -> list[dict[str, str]]:
    """Read locally queued TikTok candidate schedule rows."""
    if not config.TIKTOK_SCHEDULE_FILE.exists():
        return []
    with config.TIKTOK_SCHEDULE_FILE.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def read_tiktok_schedule_by_date() -> dict[str, list[dict[str, str]]]:
    """Group locally queued TikTok schedule rows by source stats date."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in read_tiktok_schedule():
        grouped.setdefault(row.get("stats_date", ""), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: row.get("scheduled_time", ""))
    return grouped


def create_tiktok_schedule_for_date(stats_date: str) -> int:
    """Queue top candidates from a stats date for TikTok posting."""
    existing = read_tiktok_schedule_by_date()
    if existing.get(stats_date):
        return 0

    candidates = tiktok_candidates_for_date(stats_date)
    if not candidates:
        return 0

    schedule_date = next_tiktok_schedule_date(stats_date)
    created_at = datetime.now().astimezone().isoformat()
    rows = []
    for index, candidate in enumerate(candidates[:5]):
        scheduled_time = datetime.combine(schedule_date, datetime.min.time()).replace(hour=10 + index).isoformat()
        rows.append(
            {
                "stats_date": stats_date,
                "rank": str(index + 1),
                "clip_filename": str(candidate["clip_filename"]),
                "youtube_video_id": str(candidate["youtube_video_id"]),
                "title": str(candidate["title"]),
                "score": str(candidate["score"]),
                "views": str(candidate["views"]),
                "likes": str(candidate["likes"]),
                "scheduled_time": scheduled_time,
                "status": "queued",
                "created_at": created_at,
            }
        )

    append_tiktok_schedule_rows(rows)
    return len(rows)


def append_tiktok_schedule_rows(rows: list[dict[str, str]]) -> None:
    """Append rows to the local TikTok schedule file."""
    if not rows:
        return
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = config.TIKTOK_SCHEDULE_FILE.exists()
    fieldnames = [
        "stats_date",
        "rank",
        "clip_filename",
        "youtube_video_id",
        "title",
        "score",
        "views",
        "likes",
        "scheduled_time",
        "status",
        "created_at",
    ]
    with config.TIKTOK_SCHEDULE_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def next_tiktok_schedule_date(stats_date: str) -> datetime.date:
    """Return the planned TikTok posting date for a completed stats day."""
    try:
        return datetime.fromisoformat(stats_date).date() + timedelta(days=1)
    except ValueError:
        return datetime.now().date() + timedelta(days=1)


def tiktok_schedule_labels(schedule_date: datetime.date, count: int) -> list[str]:
    """Return human labels for TikTok schedule slots."""
    return [
        datetime.combine(schedule_date, datetime.min.time()).replace(hour=10 + index).strftime("%b %-d, %-I %p")
        for index in range(count)
    ]


def format_tiktok_schedule_time(value: str) -> str:
    """Format a queued TikTok schedule timestamp."""
    try:
        return datetime.fromisoformat(value).strftime("%b %-d, %-I %p")
    except ValueError:
        return value


def format_stats_date(value: str) -> str:
    """Format a stats date for display."""
    try:
        return datetime.fromisoformat(value).strftime("%B %-d, %Y")
    except ValueError:
        return value


STATS_RANGES = {
    "1d": ("1D", timedelta(days=1)),
    "1w": ("1W", timedelta(days=7)),
    "1m": ("1M", timedelta(days=30)),
    "3m": ("3M", timedelta(days=90)),
    "ytd": ("YTD", None),
    "all": ("ALL", None),
}


def normalize_stats_range(value: str) -> str:
    """Normalize the selected stats range."""
    value = value.lower().strip()
    return value if value in STATS_RANGES else "1d"


def stats_range_label(value: str) -> str:
    """Return user-facing label for chart range."""
    return STATS_RANGES.get(value, STATS_RANGES["1d"])[0]


def chart_view_gains(selected_range: str) -> list[dict[str, int | str]]:
    """Return period view gains for the selected chart range."""
    history = read_stats_history()
    if not history:
        return []

    parsed_rows = []
    for row in history:
        checked_at = parse_iso_datetime(row.get("checked_at", ""))
        video_id = row.get("youtube_video_id", "")
        if not checked_at or not video_id:
            continue
        parsed_rows.append((checked_at, video_id, row))

    if not parsed_rows:
        return []

    newest = max(checked_at for checked_at, _video_id, _row in parsed_rows)
    selected_range = normalize_stats_range(selected_range)

    if selected_range == "1d":
        return hourly_view_gains(parsed_rows, newest)

    return daily_view_gains(parsed_rows, newest, selected_range)


def hourly_view_gains(
    parsed_rows: list[tuple[datetime, str, dict[str, str]]],
    newest: datetime,
) -> list[dict[str, int | str]]:
    """Return channel views gained within each posting hour of the newest local day.

    Each bucket H (e.g. 10 AM) holds the views gained between H:00:00 and
    H:59:59 on the most recent day that has snapshots, computed from the saved
    stats history. Hours still in the future (or with no snapshot yet) show 0.
    Granularity depends on how often snapshots were saved during the day.
    """
    local_zone = ZoneInfo(config.TIMEZONE)
    newest_local = newest.astimezone(local_zone) if newest.tzinfo else newest.replace(tzinfo=local_zone)
    day = newest_local.date()
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=local_zone)

    # First snapshot of this day: used as a floor so the first observed hour shows
    # only in-hour growth, not overnight views lumped in from the day's first check.
    day_snapshot_times = [
        (checked_at.astimezone(local_zone) if checked_at.tzinfo else checked_at.replace(tzinfo=local_zone))
        for checked_at, _video_id, _row in parsed_rows
    ]
    day_snapshot_times = [t for t in day_snapshot_times if t.date() == day]
    first_snapshot = min(day_snapshot_times) if day_snapshot_times else newest_local

    # Overnight bucket: views gained between the previous day's last snapshot
    # (everything at or before this midnight) and today's first snapshot.
    overnight_views = max(
        0,
        channel_total_at(parsed_rows, first_snapshot) - channel_total_at(parsed_rows, midnight),
    )
    rows: list[dict[str, int | str]] = [
        {
            "label": "Night",
            "tooltip": f"Overnight ({day.strftime('%b %-d')})",
            "views": overnight_views,
            "fill": "#6a5cff",
        }
    ]

    start_hour = config.POST_START_HOUR
    end_hour = config.POST_END_HOUR
    for hour in range(start_hour, end_hour + 1):
        hour_start = midnight.replace(hour=hour)
        hour_end = hour_start + timedelta(hours=1)
        # Baseline no earlier than the first snapshot; never look past the newest one.
        lower = min(max(hour_start, first_snapshot), newest_local)
        upper = min(hour_end, newest_local)
        gained = (
            max(0, channel_total_at(parsed_rows, upper) - channel_total_at(parsed_rows, lower))
            if upper > lower
            else 0
        )
        rows.append(
            {
                "label": hour_start.strftime("%-I%p"),
                "tooltip": f"{hour_start.strftime('%-I %p')}–{hour_end.strftime('%-I %p')}",
                "views": gained,
            }
        )
    return rows


def snapshot_view_gains(
    parsed_rows: list[tuple[datetime, str, dict[str, str]]],
    newest: datetime,
) -> list[dict[str, int | str]]:
    """Return view gains between saved snapshots for the newest local day."""
    local_zone = ZoneInfo(config.TIMEZONE)
    newest_local = newest.astimezone(local_zone) if newest.tzinfo else newest.replace(tzinfo=local_zone)
    day_start = newest_local.replace(hour=0, minute=0, second=0, microsecond=0)
    snapshot_times = sorted(
        {
            (checked_at.astimezone(local_zone) if checked_at.tzinfo else checked_at.replace(tzinfo=local_zone))
            for checked_at, _video_id, _row in parsed_rows
            if (checked_at.astimezone(local_zone) if checked_at.tzinfo else checked_at.replace(tzinfo=local_zone)).date()
            == newest_local.date()
            and (checked_at.astimezone(local_zone) if checked_at.tzinfo else checked_at.replace(tzinfo=local_zone))
            <= newest_local
        }
    )

    rows = []
    previous_total = channel_total_at(parsed_rows, day_start)
    previous_time = day_start
    for checked_local in snapshot_times:
        total = channel_total_at(parsed_rows, checked_local)
        views = max(0, total - previous_total)
        rows.append(
            {
                "label": checked_local.strftime("%-I %p" if checked_local.minute == 0 else "%-I:%M %p"),
                "tooltip": (
                    f"{checked_local.strftime('%m-%d %-I:%M %p')} "
                    f"since {previous_time.strftime('%-I:%M %p')}"
                ),
                "views": views,
            }
        )
        previous_total = total
        previous_time = checked_local

    return rows


def daily_view_gains(
    parsed_rows: list[tuple[datetime, str, dict[str, str]]],
    newest: datetime,
    selected_range: str,
) -> list[dict[str, int | str]]:
    """Return view gains per day for weekly and longer chart ranges."""
    local_zone = ZoneInfo(config.TIMEZONE)
    newest_local = newest.astimezone(local_zone) if newest.tzinfo else newest.replace(tzinfo=local_zone)
    newest_day = newest_local.date()
    selected_range = normalize_stats_range(selected_range)

    if selected_range == "all":
        oldest = min(checked_at for checked_at, _video_id, _row in parsed_rows)
        oldest_local = oldest.astimezone(local_zone) if oldest.tzinfo else oldest.replace(tzinfo=local_zone)
        start_day = oldest_local.date()
    elif selected_range == "ytd":
        start_day = newest_day.replace(month=1, day=1)
    else:
        days = max(1, int((STATS_RANGES[selected_range][1] or timedelta(days=1)).days))
        start_day = newest_day - timedelta(days=days - 1)

    rows = []
    previous_boundary = datetime.combine(start_day, datetime.min.time(), tzinfo=local_zone)
    previous_total = channel_total_at(parsed_rows, previous_boundary)
    current_day = start_day
    while current_day <= newest_day:
        day_end = datetime.combine(current_day, datetime.max.time(), tzinfo=local_zone)
        if current_day == newest_day:
            day_end = newest_local
        total = channel_total_at(parsed_rows, day_end)
        views = max(0, total - previous_total)
        rows.append(
            {
                "label": current_day.strftime("%m-%d"),
                "tooltip": current_day.strftime("%b %-d"),
                "views": views,
            }
        )
        previous_total = total
        current_day += timedelta(days=1)

    return rows


def channel_total_at(
    parsed_rows: list[tuple[datetime, str, dict[str, str]]],
    cutoff: datetime,
) -> int:
    """Return total channel views using the latest snapshot per video at a cutoff."""
    latest_by_video: dict[str, tuple[datetime, int]] = {}
    for checked_at, video_id, row in parsed_rows:
        comparable_checked_at = checked_at
        comparable_cutoff = cutoff
        if comparable_checked_at.tzinfo is None and comparable_cutoff.tzinfo:
            comparable_checked_at = comparable_checked_at.replace(tzinfo=comparable_cutoff.tzinfo)
        if comparable_checked_at.tzinfo and comparable_cutoff.tzinfo is None:
            comparable_cutoff = comparable_cutoff.replace(tzinfo=comparable_checked_at.tzinfo)
        if comparable_checked_at.tzinfo and comparable_cutoff.tzinfo:
            comparable_checked_at = comparable_checked_at.astimezone(comparable_cutoff.tzinfo)
        if comparable_checked_at > comparable_cutoff:
            continue
        previous = latest_by_video.get(video_id)
        if previous is None or comparable_checked_at > previous[0]:
            latest_by_video[video_id] = (comparable_checked_at, parse_stat_int(row.get("view_count", "")))
    return sum(views for _checked_at, views in latest_by_video.values())


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO datetime safely."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_stat_int(value: str) -> int:
    """Parse integer stat strings defensively."""
    try:
        return int(str(value).replace(",", "").strip() or "0")
    except ValueError:
        return 0


def parse_float(value: str) -> float:
    """Parse decimal strings defensively."""
    try:
        return float(str(value).strip() or "0")
    except ValueError:
        return 0.0


def summarize_project_dataset(rows: list[dict[str, str]]) -> dict[str, int | str]:
    """Summarize project dataset rows for the stats page."""
    week_zero = sum(1 for row in rows if row.get("project_week") == "Week 0")
    official = sum(1 for row in rows if row.get("project_phase") == "official")
    completed_24h = sum(1 for row in rows if row.get("views_24h", "").strip())
    youtube_rows = sum(1 for row in rows if row.get("platform") == "YouTube Shorts")
    return {
        "week_zero": week_zero,
        "official": official,
        "completed_24h": completed_24h,
        "youtube_rows": youtube_rows,
        "week_1_start": config.PROJECT_WEEK_1_START_DATE,
    }


def normalize_project_week(rows: list[dict[str, str]], selected_week: str) -> str:
    """Return a valid selected project week."""
    weeks = project_weeks(rows)
    if not weeks:
        return ""
    if selected_week in weeks:
        return selected_week
    current_week = current_project_week_label()
    return current_week if current_week in weeks else weeks[-1]


def normalize_project_sort(value: str) -> str:
    """Return a supported project dataset sort mode."""
    normalized = str(value or "").strip().lower()
    if normalized in {"recent", "highest_views", "lowest_views", "like_rate", "high_performer"}:
        return normalized
    return "recent"


def project_sort_label(value: str) -> str:
    """Return a readable project sort label."""
    labels = {
        "recent": "Most Recent",
        "highest_views": "Highest Views",
        "lowest_views": "Lowest Views",
        "like_rate": "Best Like Rate",
        "high_performer": "High Performers",
    }
    return labels.get(normalize_project_sort(value), "Most Recent")


def sort_project_rows(rows: list[dict[str, str]], sort_order: str) -> list[dict[str, str]]:
    """Sort project rows for the selected analytical view."""
    sort_order = normalize_project_sort(sort_order)
    if sort_order == "highest_views":
        return sorted(rows, key=lambda row: parse_stat_int(row.get("views_24h", "")), reverse=True)
    if sort_order == "lowest_views":
        return sorted(rows, key=lambda row: parse_stat_int(row.get("views_24h", "")))
    if sort_order == "like_rate":
        return sorted(rows, key=lambda row: parse_float(row.get("like_rate_24h", "")), reverse=True)
    if sort_order == "high_performer":
        return sorted(
            rows,
            key=lambda row: (
                row.get("high_performing", "") == "1",
                parse_stat_int(row.get("views_24h", "")),
            ),
            reverse=True,
        )
    return sorted(rows, key=lambda row: row.get("scheduled_time", ""), reverse=True)


def project_weeks(rows: list[dict[str, str]]) -> list[str]:
    """Return planned project weeks plus any extra saved dataset weeks."""
    weeks = set(planned_project_weeks())
    weeks.update(row.get("project_week", "") for row in rows if row.get("project_week", ""))
    return sorted(weeks, key=project_week_sort_key)


def project_week_sort_key(value: str) -> int:
    """Sort Week labels by number."""
    if value.startswith("After Week"):
        return config.PROJECT_TOTAL_WEEKS + 1
    digits = "".join(char for char in value if char.isdigit())
    return int(digits) if digits else 9999


def planned_project_weeks() -> list[str]:
    """Return the fixed Week 0 through Week 12 project timeline."""
    return ["Week 0"] + [f"Week {week}" for week in range(1, config.PROJECT_TOTAL_WEEKS + 1)]


def current_project_week_label() -> str:
    """Return the current planned project week for the default filter."""
    start = datetime.fromisoformat(config.PROJECT_WEEK_1_START_DATE).date()
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    if today < start:
        return "Week 0"
    week_number = ((today - start).days // 7) + 1
    if week_number > config.PROJECT_TOTAL_WEEKS:
        return f"Week {config.PROJECT_TOTAL_WEEKS}"
    return f"Week {week_number}"


def build_status() -> dict[str, object]:
    """Build dashboard status counts."""
    return {
        "input_count": len(list_input_videos()),
        "clip_count": len(list_clip_files()),
        "uploaded_sources": len(list_uploaded_sources()),
        "upload_records": count_upload_records(),
        "run": RUN_STATE.copy(),
    }


def live_dashboard_log_lines(limit: int = 90) -> list[str]:
    """Return recent useful app log lines for the live dashboard feed."""
    if not config.APP_LOG_FILE.exists():
        return []

    with config.APP_LOG_FILE.open("r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()[-600:]

    useful_lines = []
    skipped_patterns = (
        'dashboard: "GET /api/status',
        'dashboard: "GET /api/logs',
        'dashboard: "GET /clips/',
        'dashboard: "GET /favicon.ico',
    )
    for line in lines:
        clean = line.rstrip()
        if not clean:
            continue
        if any(pattern in clean for pattern in skipped_patterns):
            continue
        useful_lines.append(clean)

    return useful_lines[-limit:]


def live_dashboard_log_text(limit: int = 90) -> str:
    """Return recent useful app log lines as one text block."""
    lines = live_dashboard_log_lines(limit)
    return "\n".join(lines) if lines else "No dashboard activity yet."


def list_input_videos() -> list[Path]:
    """List videos currently in input/."""
    return sorted(
        path
        for path in config.INPUT_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in config.SUPPORTED_VIDEO_EXTENSIONS
    )


def list_clip_files() -> list[Path]:
    """List generated clips newest first."""
    return sorted(config.CLIPS_DIR.glob("clip_*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)


def list_uploaded_sources() -> list[Path]:
    """List processed source videos."""
    return sorted(
        path
        for path in config.UPLOADED_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in config.SUPPORTED_VIDEO_EXTENSIONS
    )


def count_upload_records() -> int:
    """Count upload log records without importing upload code."""
    if not config.UPLOAD_LOG_FILE.exists():
        return 0
    with config.UPLOAD_LOG_FILE.open("r", encoding="utf-8") as file:
        return max(0, sum(1 for _ in file) - 1)


def start_run() -> None:
    """Run main.py in a background thread unless already running."""
    if RUN_STATE["running"]:
        return

    thread = threading.Thread(target=run_main_process, daemon=True)
    thread.start()


def start_clip_only() -> None:
    """Run clipping only in a background thread unless already running."""
    if RUN_STATE["running"]:
        return

    thread = threading.Thread(target=run_main_process, kwargs={"clip_only": True}, daemon=True)
    thread.start()


def start_stats_refresh() -> None:
    """Refresh tracker stats in a background thread."""
    if RUN_STATE["running"]:
        return
    if STATS_REFRESH_LOCK.locked():
        return

    thread = threading.Thread(target=refresh_stats_process, daemon=True)
    thread.start()


def refresh_stats_files() -> None:
    """Refresh YouTube stats and all local tracker outputs."""
    refresh_youtube_stats_history()
    update_tracker([], [])
    refresh_project_dataset()


def auto_refresh_youtube_stats_if_stale() -> bool:
    """Start a background stats refresh when saved stats are stale."""
    global LAST_AUTO_STATS_REFRESH_ATTEMPT

    if RUN_STATE["running"]:
        return False
    if not youtube_stats_are_stale():
        return False
    if LAST_AUTO_STATS_REFRESH_ATTEMPT:
        now = datetime.now(LAST_AUTO_STATS_REFRESH_ATTEMPT.tzinfo)
        if now - LAST_AUTO_STATS_REFRESH_ATTEMPT < timedelta(minutes=AUTO_STATS_REFRESH_MINUTES):
            return False
    if not STATS_REFRESH_LOCK.acquire(blocking=False):
        return False

    LAST_AUTO_STATS_REFRESH_ATTEMPT = datetime.now().astimezone()
    RUN_STATE.update(
        {
            "running": True,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "last_output": "Auto-refreshing YouTube stats for the Stats page...",
            "last_error": "",
        }
    )
    thread = threading.Thread(target=auto_refresh_stats_process, daemon=True)
    thread.start()
    return True


def auto_refresh_stats_process() -> None:
    """Refresh stats without blocking the page request that triggered it."""
    try:
        refresh_stats_files()
        RUN_STATE["last_output"] = "YouTube stats auto-refreshed for the Stats page."
    except Exception as exc:
        logger.exception("Automatic stats refresh failed: %s", exc)
        RUN_STATE["last_error"] = f"Automatic YouTube stats refresh failed: {exc}"
    finally:
        RUN_STATE["running"] = False
        RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATS_REFRESH_LOCK.release()


def youtube_stats_are_stale() -> bool:
    """Return True when local YouTube stats are missing or older than the freshness window."""
    latest = latest_stats_checked_at()
    if latest is None:
        return True
    now = datetime.now(latest.tzinfo) if latest.tzinfo else datetime.now()
    return now - latest >= timedelta(minutes=AUTO_STATS_REFRESH_MINUTES)


def latest_stats_checked_at() -> datetime | None:
    """Return the newest saved YouTube stats timestamp."""
    timestamps = []
    for row in read_stats_history():
        checked_at = parse_iso_datetime(row.get("checked_at", ""))
        if checked_at:
            timestamps.append(checked_at)
    return max(timestamps, default=None)


def refresh_stats_process() -> None:
    """Refresh YouTube stats without clipping/uploading."""
    if not STATS_REFRESH_LOCK.acquire(blocking=False):
        return

    RUN_STATE.update(
        {
            "running": True,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "last_output": "",
            "last_error": "",
        }
    )
    try:
        refresh_stats_files()
        RUN_STATE["last_output"] = "YouTube stats refreshed into tracker and project dataset files."
    except Exception as exc:
        logger.exception("Stats refresh failed: %s", exc)
        RUN_STATE["last_error"] = str(exc)
    finally:
        STATS_REFRESH_LOCK.release()
        RUN_STATE["running"] = False
        RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def update_youtube_privacy(video_id: str, privacy_status: str) -> None:
    """Update a YouTube video's privacy status."""
    service = get_youtube_service()
    try:
        update_youtube_privacy_with_service(service, video_id, privacy_status)
    except HttpError as exc:
        if "insufficientPermissions" in str(exc):
            if config.TOKEN_FILE.exists():
                config.TOKEN_FILE.unlink()
            raise RuntimeError(
                "Google needs a fresh login with YouTube privacy-edit permission. Click Save again and approve access."
            ) from exc
        raise


def update_youtube_privacy_with_service(service, video_id: str, privacy_status: str) -> None:
    """Update privacy using an authenticated YouTube service."""
    response = service.videos().list(part="status", id=video_id).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError(f"YouTube video not found: {video_id}")

    status = dict(items[0].get("status", {}))
    status["privacyStatus"] = privacy_status
    service.videos().update(
        part="status",
        body={
            "id": video_id,
            "status": status,
        },
    ).execute()


def run_main_process(clip_only: bool = False) -> None:
    """Execute the automation in a subprocess."""
    RUN_STATE.update(
        {
            "running": True,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "last_output": "",
            "last_error": "",
        }
    )
    output_lines: list[str] = []
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [
                str(Path(__file__).resolve().parent / ".venv" / "bin" / "python"),
                "main.py",
                *(["--clip-only"] if clip_only else []),
            ],
            cwd=config.BASE_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        deadline = time.monotonic() + (60 * 60 * 4)
        for line in process.stdout:
            output_lines.append(line.rstrip())
            RUN_STATE["last_output"] = "\n".join(output_lines[-80:]).strip()
            if time.monotonic() > deadline:
                process.kill()
                raise TimeoutError("Dashboard run timed out after 4 hours")

        return_code = process.wait()
        refresh_project_dataset()
        RUN_STATE["last_output"] = "\n".join(output_lines[-120:]).strip()
        if return_code != 0:
            RUN_STATE["last_error"] = RUN_STATE["last_output"]
    except Exception as exc:
        logger.exception("Dashboard run failed: %s", exc)
        output_lines.append(str(exc))
        RUN_STATE["last_output"] = "\n".join(output_lines[-120:]).strip()
        RUN_STATE["last_error"] = str(exc)
    finally:
        RUN_STATE["running"] = False
        RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def unique_input_destination(path: Path) -> Path:
    """Avoid overwriting files uploaded through the dashboard."""
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index:04d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a unique destination for {path}")


def upload_message(saved: int, upload_action: str) -> str:
    """Return a clear user-facing upload result."""
    if saved <= 0:
        return "No .mp4 or .mov files were saved."
    noun = "file" if saved == 1 else "files"
    if upload_action == "upload_and_clip":
        return f"Saved {saved} {noun} to input and started clipping."
    return f"Saved {saved} {noun} to input."


# ============================================================================
# v4 dashboard UI — real multi-page rebuild (matches dashboard_revamp_preview.html)
# Every page is a real server-rendered route. Numbers come from the live data
# functions above; owner-only actions stay inside forms hidden from public viewers.
# ============================================================================

STYLE_V4 = r"""<style>
  :root{
    --bg:#08090a;--panel:#121412;--panel-2:#171a17;--hover:#1d211d;
    --line:rgba(255,255,255,.07);--line-2:rgba(255,255,255,.13);
    --text:#f4f6f4;--muted:#98a098;--faint:#6a716a;
    --green:#1ed760;--green-soft:rgba(30,215,96,.14);
    --blue:#4f97ff;--teal:#24d6b6;--amber:#f5b544;--violet:#b18bff;--night:#6a5cff;
    --radius:16px;--font:'Figtree',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg)}
  body{font-family:var(--font);color:var(--text);-webkit-font-smoothing:antialiased;letter-spacing:-.011em;position:relative;overflow-x:hidden}
  a{color:inherit;text-decoration:none}
  ::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#2a2e2a;border-radius:8px}
  .bg-art{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
  .blob{position:absolute;border-radius:50%;filter:blur(90px)}
  .blob.b1{width:520px;height:520px;background:radial-gradient(circle,#1ed760,transparent 70%);top:-160px;left:180px;opacity:.26}
  .blob.b2{width:460px;height:460px;background:radial-gradient(circle,#1f6bff,transparent 70%);top:120px;right:-140px;opacity:.15}
  .blob.b3{width:420px;height:420px;background:radial-gradient(circle,#6a5cff,transparent 70%);bottom:-160px;left:40%;opacity:.13}
  .blob.b4{width:360px;height:360px;background:radial-gradient(circle,#24d6b6,transparent 70%);bottom:120px;left:180px;opacity:.11}
  .wave{position:absolute;top:0;left:248px;right:0;height:340px;opacity:.5}
  .grain{position:absolute;inset:0;opacity:.035;background-image:radial-gradient(#fff 1px,transparent 1px);background-size:4px 4px}

  .app{position:relative;z-index:1;display:grid;grid-template-columns:248px 1fr;min-height:100vh}
  .sidebar{background:linear-gradient(180deg,rgba(13,15,13,.92),rgba(8,9,10,.92));backdrop-filter:blur(8px);border-right:1px solid var(--line);padding:22px 16px;display:flex;flex-direction:column;gap:5px;position:sticky;top:0;height:100vh}
  .brand{display:flex;align-items:center;gap:11px;padding:6px 10px 22px}
  .brand .logo{width:34px;height:34px;border-radius:50%;background:var(--green);display:grid;place-items:center;flex:none;box-shadow:0 0 0 5px var(--green-soft),0 0 24px rgba(30,215,96,.35)}
  .brand .logo svg{width:18px;height:18px}
  .brand b{font-size:15px;font-weight:800;letter-spacing:-.02em}.brand span{display:block;font-size:11px;color:var(--faint);font-weight:600;margin-top:1px}
  .nav-label{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);padding:14px 12px 6px;font-weight:700}
  .nav-item{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:11px;color:var(--muted);font-size:13.5px;font-weight:600;cursor:pointer;transition:.18s;user-select:none}
  .nav-item svg{width:18px;height:18px;flex:none}
  .nav-item:hover{background:var(--hover);color:var(--text)}
  .nav-item.active{background:var(--green-soft);color:var(--green)}
  .sidebar .spacer{flex:1}
  .owner-card{margin-top:8px;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel);display:flex;align-items:center;gap:10px}
  .owner-card .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
  .owner-card small{color:var(--faint);font-size:11px;display:block}.owner-card b{font-size:12.5px}

  .main{padding:26px 34px 60px;max-width:1520px}
  .topbar{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:24px;flex-wrap:wrap}
  .eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--green);font-weight:700;margin:0 0 6px}
  .topbar h1{margin:0;font-size:30px;font-weight:850;letter-spacing:-.03em}
  .topbar .sub{color:var(--muted);font-size:13.5px;margin-top:4px}
  .top-actions{display:flex;align-items:center;gap:10px}
  .pill{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:999px;font-size:12.5px;font-weight:700}
  .pill.ready{background:var(--green-soft);color:var(--green)}
  .pill.ready .dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border-radius:999px;font:700 13px var(--font);cursor:pointer;border:1px solid var(--line-2);background:rgba(255,255,255,.02);color:var(--text);transition:.15s}
  .btn:hover{background:var(--hover)}
  .btn.primary{background:var(--green);color:#04140a;border-color:var(--green)}
  .btn.primary:hover{transform:translateY(-1px);box-shadow:0 8px 22px rgba(30,215,96,.3)}
  .btn svg{width:15px;height:15px}
  form.inline{display:inline-flex;margin:0}

  .panel{position:relative;background:linear-gradient(180deg,rgba(255,255,255,.028),rgba(255,255,255,.006)),var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:20px;overflow:hidden}
  .panel::before{content:"";position:absolute;top:0;left:24px;right:24px;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent)}
  .panel-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:12px}
  .panel-h h2{margin:0;font-size:16px;font-weight:750}.panel-h .hint{color:var(--faint);font-size:12px}

  .status-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
  .chip{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 16px;transition:.2s}
  .chip:hover{border-color:var(--line-2);transform:translateY(-2px)}
  .chip .l{color:var(--muted);font-size:12px;font-weight:600;display:flex;align-items:center;gap:8px}
  .chip .l svg{width:15px;height:15px;color:var(--kp,var(--green))}
  .chip .v{font-size:24px;font-weight:850;letter-spacing:-.03em;margin-top:8px}
  .chip::after{content:"";position:absolute;width:120px;height:120px;border-radius:50%;filter:blur(40px);top:-70px;right:-50px;opacity:.4;background:var(--kp,var(--green))}
  .chip.b{--kp:var(--blue)}.chip.t{--kp:var(--teal)}.chip.a{--kp:var(--amber)}

  .auto-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:20px}
  .auto{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:var(--radius);padding:16px;cursor:pointer;transition:.18s;background:var(--panel);display:block}
  .auto::after{content:"";position:absolute;width:150px;height:150px;border-radius:50%;filter:blur(44px);bottom:-80px;right:-50px;opacity:.5;background:var(--ac)}
  .auto:hover{transform:translateY(-3px);border-color:var(--line-2)}
  .auto .a-ic{position:relative;z-index:1;width:44px;height:44px;border-radius:12px;display:grid;place-items:center;margin-bottom:34px;background:color-mix(in srgb,var(--ac) 18%,transparent);color:var(--ac)}
  .auto .a-ic svg{width:22px;height:22px}
  .auto b{position:relative;z-index:1;display:block;font-size:14px;font-weight:750}
  .auto small{position:relative;z-index:1;color:var(--muted);font-size:11.5px}
  .auto button.bare{all:unset;cursor:pointer;display:block;width:100%}
  .g1{--ac:#1ed760}.g2{--ac:#4f97ff}.g3{--ac:#24d6b6}.g4{--ac:#ff5d78}.g5{--ac:#b18bff}

  .grid-2{display:grid;grid-template-columns:1.35fr 1fr;gap:16px;margin-bottom:20px}
  .grid-3{display:grid;grid-template-columns:1fr 1fr 1.1fr;gap:16px;margin-bottom:20px}

  .chart-wrap{margin-bottom:20px}
  .chart-glow{position:absolute;width:420px;height:220px;background:radial-gradient(ellipse,rgba(30,215,96,.18),transparent 70%);top:-40px;left:-30px;pointer-events:none}
  .chart-top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:8px;position:relative}
  .metric-big{font-size:44px;font-weight:850;letter-spacing:-.035em;line-height:1;background:linear-gradient(180deg,#fff,#cfeede);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .metric-cap{color:var(--muted);font-size:13px;margin-top:6px}.metric-cap b{color:var(--green)}
  .tabs{display:inline-flex;gap:4px;background:rgba(0,0,0,.35);border:1px solid var(--line);border-radius:999px;padding:4px}
  .tab{padding:6px 14px;border-radius:999px;font-size:12.5px;font-weight:700;color:var(--muted);cursor:pointer;border:none;background:transparent;transition:.16s}
  .tab.active{background:var(--green);color:#04140a}
  canvas.chart{max-height:280px;position:relative;z-index:1}

  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px}
  .kpi{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px;transition:.2s}
  .kpi:hover{transform:translateY(-2px);border-color:var(--line-2)}
  .kpi::after{content:"";position:absolute;width:180px;height:180px;border-radius:50%;filter:blur(50px);top:-90px;right:-70px;opacity:.5;background:var(--kp,var(--green))}
  .kpi .k-top{display:flex;align-items:center;justify-content:space-between;position:relative;z-index:1}
  .kpi .k-label{color:var(--muted);font-size:12.5px;font-weight:600}
  .kpi .k-ic{width:30px;height:30px;border-radius:9px;background:color-mix(in srgb,var(--kp,var(--green)) 16%,transparent);display:grid;place-items:center}
  .kpi .k-ic svg{width:16px;height:16px;color:var(--kp,var(--green))}
  .kpi .k-value{font-size:30px;font-weight:850;letter-spacing:-.03em;margin:12px 0 2px;position:relative;z-index:1}
  .kpi .k-delta{font-size:12px;font-weight:700;position:relative;z-index:1}.kpi .k-delta.up{color:var(--kp,var(--green))}.kpi .k-delta.flat{color:var(--faint)}
  .kpi.g{--kp:var(--green)}.kpi.b{--kp:var(--blue)}.kpi.t{--kp:var(--teal)}.kpi.a{--kp:var(--amber)}

  .bar-row{margin-bottom:14px}.bar-row:last-child{margin-bottom:0}
  .bar-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;font-size:13px}
  .bar-head b{font-weight:700}.bar-head span{color:var(--muted);font-size:11.5px}
  .track{height:9px;background:#1e211e;border-radius:999px;overflow:hidden}
  .fill{height:100%;border-radius:999px}
  .fill.green{background:linear-gradient(90deg,#1ed760,#8ef0ab)}.fill.teal{background:linear-gradient(90deg,#1aa7ff,#24d6b6)}
  .vrow{display:grid;grid-template-columns:24px 1fr auto;gap:12px;align-items:center;padding:11px 0;border-bottom:1px solid var(--line)}.vrow:last-child{border-bottom:0}
  .vrank{font-weight:850;font-size:14px;text-align:center;background:linear-gradient(180deg,#fff,#9fdcb4);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .vname b{display:block;font-size:13.5px}.vname small{color:var(--faint);font-size:11.5px}
  .vnum{text-align:right}.vnum b{display:block;font-size:13.5px}.vnum small{color:var(--green);font-size:11.5px}

  .exp{display:flex;gap:12px;align-items:flex-start;padding:13px 0;border-bottom:1px solid var(--line)}.exp:last-child{border-bottom:0}
  .exp .wk{flex:none;width:64px;font-size:11px;font-weight:800;color:var(--green);letter-spacing:.04em;padding-top:2px}
  .exp .body b{display:block;font-size:13.5px}.exp .body small{color:var(--muted);font-size:12px}
  .exp .status{margin-left:auto;flex:none;font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;padding:4px 9px;border-radius:999px}
  .st-live{background:var(--green-soft);color:var(--green);animation:glow 2.2s infinite}
  @keyframes glow{0%,100%{box-shadow:0 0 0 1px rgba(30,215,96,.25)}50%{box-shadow:0 0 14px rgba(30,215,96,.5)}}
  .st-next{background:rgba(79,151,255,.12);color:var(--blue)}.st-done{background:rgba(255,255,255,.05);color:var(--faint)}

  details.accw{border-bottom:1px solid var(--line)}details.accw:last-child{border-bottom:0}
  details.accw summary{list-style:none;display:flex;gap:14px;align-items:center;padding:16px 0;cursor:pointer}
  details.accw summary::-webkit-details-marker{display:none}
  details.accw summary .wk{flex:none;width:70px;font-size:11px;font-weight:800;color:var(--green);letter-spacing:.04em}
  details.accw summary .body b{display:block;font-size:14.5px}details.accw summary .body small{color:var(--muted);font-size:12.5px}
  details.accw summary .status{flex:none;font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;padding:4px 9px;border-radius:999px}
  details.accw summary .chev{flex:none;color:var(--faint);transition:transform .3s;width:18px;height:18px}
  details.accw[open] summary .chev{transform:rotate(90deg);color:var(--green)}
  .acc-inner{padding:0 0 20px 84px;color:var(--muted);font-size:13px;line-height:1.65}
  .acc-inner .lab{color:var(--faint);text-transform:uppercase;font-size:10.5px;letter-spacing:.1em;font-weight:800;display:block;margin:12px 0 4px}
  .acc-inner .lab:first-child{margin-top:0}
  .acc-inner .tags{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}
  .acc-inner .tag{font-size:11px;font-weight:700;color:var(--green);background:var(--green-soft);padding:4px 10px;border-radius:999px}

  .qtable{width:100%;border-collapse:collapse;font-size:13px}
  .qtable th{text-align:left;color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:800;padding:0 12px 12px}
  .qtable td{padding:12px;border-top:1px solid var(--line)}
  .qtable tbody tr{transition:background .15s}.qtable tbody tr:hover{background:var(--hover)}
  .qtable .clip{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:12px}
  .qtable .word{font-weight:700}
  .table-wrap{overflow-x:auto}
  .badge{font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px}
  .badge.sch{background:rgba(79,151,255,.14);color:var(--blue)}.badge.up{background:var(--green-soft);color:var(--green)}.badge.def{background:rgba(245,181,68,.15);color:var(--amber)}.badge.fail{background:rgba(255,93,120,.16);color:#ff5d78}.badge.wait{background:rgba(255,255,255,.06);color:var(--muted)}

  .tt-day{margin-bottom:18px}
  .tt-day .dh{display:flex;align-items:baseline;gap:10px;margin-bottom:11px}
  .tt-day .dh h3{margin:0;font-size:15px}.tt-day .dh span{color:var(--faint);font-size:12px;font-weight:600}
  .tt-day .dh .live{margin-left:auto;font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--green);background:var(--green-soft);padding:3px 9px;border-radius:999px}
  .tt-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  .tt-card{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:14px;padding:15px;background:var(--panel)}
  .tt-card::after{content:"";position:absolute;width:120px;height:120px;border-radius:50%;filter:blur(40px);top:-70px;right:-50px;opacity:.4;background:var(--rc,var(--green))}
  .tt-card .rank{font-size:11px;font-weight:800;color:var(--rc,var(--green))}
  .tt-card .w{font-size:16px;font-weight:800;margin:6px 0 2px}
  .tt-card .c{color:var(--faint);font-family:ui-monospace,Menlo,monospace;font-size:11px}
  .tt-card .m{display:flex;gap:14px;margin-top:12px;font-size:12px}
  .tt-card .m b{display:block;font-size:15px}.tt-card .m span{color:var(--muted);font-size:11px}
  .r1{--rc:#f5b544}.r2{--rc:#c8ccd2}.r3{--rc:#e08a4b}

  .chipset{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
  .fchip{font-size:12px;font-weight:700;color:var(--muted);background:var(--panel);border:1px solid var(--line);padding:6px 12px;border-radius:999px}
  .fchip.active{background:var(--green-soft);color:var(--green);border-color:transparent}
  .pager{display:flex;gap:6px;align-items:center;margin-top:16px;flex-wrap:wrap}
  .pager a,.pager span{font-size:12.5px;font-weight:700;padding:6px 11px;border-radius:9px;color:var(--muted);border:1px solid var(--line)}
  .pager a:hover{background:var(--hover);color:var(--text)}
  .pager .cur{background:var(--green-soft);color:var(--green);border-color:transparent}
  .pager .off{opacity:.35}
  .log{background:#06070688;border:1px solid var(--line);border-radius:12px;padding:14px 16px;font:500 12px ui-monospace,Menlo,monospace;color:#b7c0b7;margin-top:14px;max-height:220px;overflow:auto}
  .log div{padding:2px 0}.log .t{color:var(--faint)}
  .upload-zone{border:1.5px dashed var(--line-2);border-radius:14px;padding:20px;text-align:center;background:linear-gradient(180deg,rgba(30,215,96,.05),transparent)}
  select.mini{background:var(--panel);color:var(--text);border:1px solid var(--line-2);border-radius:8px;padding:6px 10px;font:700 12px var(--font)}
  .placeholder{display:grid;place-items:center;min-height:220px;text-align:center;color:var(--muted)}
  .note{text-align:center;color:var(--faint);font-size:12px;margin-top:34px}
  @media(max-width:1150px){.kpis,.status-strip{grid-template-columns:1fr 1fr}.grid-3,.grid-2,.tt-grid{grid-template-columns:1fr}.auto-grid{grid-template-columns:1fr 1fr}}
  @media(max-width:760px){.app{grid-template-columns:1fr}.sidebar{display:none}.main{padding:20px 16px 50px}.kpis,.status-strip,.auto-grid{grid-template-columns:1fr}.wave{left:0}.acc-inner{padding-left:0}}
</style>"""

BG_ART_V4 = """<div class="bg-art">
  <div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div><div class="blob b4"></div>
  <svg class="wave" viewBox="0 0 1200 340" preserveAspectRatio="none" fill="none">
    <path d="M0 180 Q150 80 300 180 T600 180 T900 180 T1200 180" stroke="url(#wg)" stroke-width="2"/>
    <path d="M0 220 Q150 300 300 220 T600 220 T900 220 T1200 220" stroke="url(#wg)" stroke-width="1.5" opacity=".5"/>
    <defs><linearGradient id="wg" x1="0" y1="0" x2="1200" y2="0"><stop stop-color="#1ed760" stop-opacity="0"/><stop offset=".5" stop-color="#1ed760" stop-opacity=".5"/><stop offset="1" stop-color="#1ed760" stop-opacity="0"/></linearGradient></defs>
  </svg>
  <div class="grain"></div>
</div>"""

# nav: (view-key, href, label, svg-inner)
NAV_ITEMS = [
    ("overview", "/", "Overview",
     '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>', "Workspace"),
    ("stats", "/stats", "YouTube Stats",
     '<path d="M4 19V5m4 14V9m4 10V7m4 12v-6m4 6V4"/>', "Workspace"),
    ("queue", "/queue", "Queue",
     '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/><circle cx="3.5" cy="6" r="1.3" fill="currentColor" stroke="none"/><circle cx="3.5" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="3.5" cy="18" r="1.3" fill="currentColor" stroke="none"/>', "Workspace"),
    ("tiktok", "/tiktok-candidates", "TikTok Candidates",
     '<path d="M9 18V5l3-1v10"/><circle cx="6" cy="18" r="3"/><path d="M14 7c1.5 2 4 2.5 6 2.5"/>', "Workspace"),
    ("tracker", "/tracker", "Video Tracker",
     '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="20" x2="9" y2="9"/>', "Workspace"),
    ("experiment", "/experiment", "12-Week Experiment",
     '<circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/>', "Project"),
]


def _sidebar_v4(active: str) -> str:
    out = ['<aside class="sidebar">']
    out.append('<div class="brand"><div class="logo"><svg viewBox="0 0 24 24" fill="#04140a"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div><div><b>Piano Shorts</b><span>Creator Analytics</span></div></div>')
    last_section = ""
    for key, href, label, svg, section in NAV_ITEMS:
        if section != last_section:
            out.append(f'<div class="nav-label">{section}</div>')
            last_section = section
        cls = "nav-item active" if key == active else "nav-item"
        out.append(
            f'<a class="{cls}" href="{href}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">{svg}</svg>{html.escape(label)}</a>'
        )
    out.append('<div class="spacer"></div>')
    out.append('<div class="owner-card"><div class="dot"></div><div><b>Owner mode</b><small>Full control · logged in</small></div></div>')
    out.append('</aside>')
    return "".join(out)


def render_shell(active: str, eyebrow: str, title: str, sub: str, body: str,
                 head_extra: str = "", top_actions: str = "") -> str:
    """Wrap page body in the shared v4 layout (sidebar + bg art + topbar)."""
    topbar = (
        '<div class="topbar"><div>'
        f'<p class="eyebrow">{html.escape(eyebrow)}</p>'
        f'<h1>{html.escape(title)}</h1>'
        + (f'<div class="sub">{sub}</div>' if sub else '')
        + '</div>'
        + (f'<div class="top-actions">{top_actions}</div>' if top_actions else '')
        + '</div>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Piano Shorts — {html.escape(title)}</title>'
        + FONT_HEAD + STYLE_V4 + head_extra
        + '</head><body>' + BG_ART_V4
        + '<div class="app">' + _sidebar_v4(active)
        + '<main class="main">' + topbar + body + '</main></div></body></html>'
    )


CHART_HEAD = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'


def build_chart_payload() -> str:
    """Build labels/data/colors per range from the real chart_view_gains()."""
    ranges = {}
    for rng in ("1d", "1w", "1m", "all"):
        rows = chart_view_gains(rng)
        ranges[rng] = {
            "labels": [str(r.get("label", "")) for r in rows],
            "data": [int(r.get("views", 0)) for r in rows],
            "colors": [str(r["fill"]) if r.get("fill") else "#1ed760" for r in rows],
            "tips": [str(r.get("tooltip", "")) for r in rows],
        }
    return json.dumps(ranges)


def chart_script(canvas_id: str, tabset: str, default_range: str = "1d") -> str:
    payload = build_chart_payload()
    tmpl = r"""
<script>
(function(){
  Chart.defaults.font.family="'Figtree', sans-serif";Chart.defaults.font.weight='600';Chart.defaults.color='#6a716a';
  var RANGES=__PAYLOAD__;var cur='__DEF__';
  var ctx=document.getElementById('__CID__');if(!ctx)return;
  function set(r){var s=RANGES[r]||RANGES['__DEF__'];return s;}
  var s0=set(cur);
  var chart=new Chart(ctx,{type:'bar',data:{labels:s0.labels,datasets:[{data:s0.data,backgroundColor:s0.colors,borderRadius:6,maxBarThickness:30}]},
    options:{plugins:{legend:{display:false},tooltip:{backgroundColor:'#0c0e0c',borderColor:'rgba(255,255,255,.12)',borderWidth:1,padding:10,displayColors:false,titleFont:{family:'Figtree',weight:'700'},bodyFont:{family:'Figtree'},callbacks:{title:function(it){var i=it[0].dataIndex;return set(cur).tips[i]||it[0].label;},label:function(x){return x.raw.toLocaleString()+' views';}}}},
      scales:{x:{grid:{display:false},ticks:{font:{family:'Figtree',size:11,weight:'600'}}},y:{grid:{color:'rgba(255,255,255,.05)'},border:{display:false},ticks:{font:{family:'Figtree',size:11},callback:function(v){return v>=1000?(v/1000)+'k':v;}}}},
      animation:{duration:900,easing:'easeOutQuart'},responsive:true,maintainAspectRatio:false}});
  if(document.fonts&&document.fonts.ready)document.fonts.ready.then(function(){chart.update();});
  var tabs=document.querySelectorAll('[data-tabs="__TAB__"] .tab');
  tabs.forEach(function(t){t.addEventListener('click',function(){
    tabs.forEach(function(x){x.classList.remove('active');});t.classList.add('active');
    cur=t.dataset.r;var s=set(cur);chart.data.labels=s.labels;chart.data.datasets[0].data=s.data;chart.data.datasets[0].backgroundColor=s.colors;chart.update();
  });});
})();
</script>"""
    return (tmpl.replace("__PAYLOAD__", payload).replace("__CID__", canvas_id)
            .replace("__TAB__", tabset).replace("__DEF__", default_range))


def _range_tabs(tabset: str, default_range: str = "1d") -> str:
    out = [f'<div class="tabs" data-tabs="{tabset}">']
    for r, lbl in (("1d", "1D"), ("1w", "1W"), ("1m", "1M"), ("all", "ALL")):
        cls = "tab active" if r == default_range else "tab"
        out.append(f'<button class="{cls}" data-r="{r}">{lbl}</button>')
    out.append('</div>')
    return "".join(out)


def _clean_title(raw: str, fallback: str = "") -> str:
    return raw.split("#", 1)[0].strip() or fallback


def _uploaded_today_count() -> int:
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    count = 0
    for row in build_queue_rows():
        parsed = parse_iso_datetime(row.get("upload_time", ""))
        if parsed and parsed.astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat() == today:
            count += 1
    return count


def _today_views_delta() -> int:
    return sum(int(r.get("views", 0)) for r in chart_view_gains("1d"))


# ---------------------------------------------------------------------------
# OVERVIEW  (/)
# ---------------------------------------------------------------------------
def render_dashboard() -> str:
    status = build_status()
    latest_rows = latest_video_stats()
    total_views = sum(parse_stat_int(r.get("view_count", "")) for r in latest_rows)
    hours = best_posting_hours()
    best_hour = str(hours[0]["hour"]) if hours else "—"
    delta_today = _today_views_delta()

    input_count = int(status["input_count"])
    clip_count = int(status["clip_count"])
    uploaded_today = _uploaded_today_count()

    # next scheduled post
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    future = [
        r for r in build_queue_rows()
        if (p := parse_iso_datetime(r.get("scheduled_publish_time", ""))) and p > now
    ]
    future.sort(key=lambda r: parse_iso_datetime(r.get("scheduled_publish_time", "")))
    next_post = html.escape(future[0]["display_time"]) if future else "None scheduled"

    running = bool(status["run"]["running"])
    run_label = "Running…" if running else "Clip + Upload"

    chart = (
        '<section class="panel chart-wrap"><div class="chart-glow"></div>'
        '<div class="chart-top"><div>'
        '<p class="eyebrow" style="color:var(--muted)">Channel Performance</p>'
        f'<div class="metric-big">{total_views:,}</div>'
        f'<div class="metric-cap">total views · <b>+{delta_today:,} today</b> · best hour {html.escape(best_hour)}</div>'
        '</div>' + _range_tabs("ov") + '</div>'
        '<canvas class="chart" id="chartOv"></canvas></section>'
    )

    strip = (
        '<div class="status-strip">'
        '<div class="chip"><div class="l"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v12H4z"/><path d="M2 20h20"/></svg>Input videos</div>'
        f'<div class="v">{input_count}</div></div>'
        '<div class="chip b"><div class="l"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9l5 3-5 3z"/></svg>Clips ready</div>'
        f'<div class="v">{clip_count:,}</div></div>'
        '<div class="chip t"><div class="l"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Next post</div>'
        f'<div class="v" style="font-size:16px">{next_post}</div></div>'
        '<div class="chip a"><div class="l"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0-12l-4 4m4-4l4 4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>Uploaded today</div>'
        f'<div class="v">{uploaded_today}</div></div>'
        '</div>'
    )

    autos = (
        '<div class="panel-h" style="margin:4px 2px 14px"><h2 style="font-size:17px">Automation</h2><span class="hint">one-click creator pipeline</span></div>'
        '<section class="auto-grid">'
        # Clip + Upload (owner action)
        '<form class="auto g1" action="/run" method="post" data-owner-only>'
        '<button class="bare" type="submit"' + (' disabled' if running else '') + '>'
        '<div class="a-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 15V3m0 0l-4 4m4-4l4 4"/><path d="M4 15v4a2 2 0 002 2h12a2 2 0 002-2v-4"/></svg></div>'
        f'<b>{html.escape(run_label)}</b><small>Clip, caption &amp; schedule</small></button></form>'
        # YouTube Stats
        '<a class="auto g2" href="/stats"><div class="a-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19V5m4 14V9m4 10V7m4 12v-6m4 6V4"/></svg></div><b>YouTube Stats</b><small>Views, likes &amp; growth</small></a>'
        # Queue
        '<a class="auto g3" href="/queue"><div class="a-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="13" y2="18"/></svg></div><b>Queue</b><small>Scheduled &amp; deferred</small></a>'
        # TikTok
        '<a class="auto g4" href="/tiktok-candidates"><div class="a-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l3-1v10"/><circle cx="6" cy="18" r="3"/><path d="M14 7c1.5 2 4 2.5 6 2.5"/></svg></div><b>TikTok Candidates</b><small>Top clips to repost</small></a>'
        # Refresh (owner action)
        '<form class="auto g5" action="/refresh-stats" method="post" data-owner-only>'
        '<input type="hidden" name="redirect_to" value="/">'
        '<button class="bare" type="submit"><div class="a-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/></svg></div><b>Refresh Stats</b><small>Pull latest data</small></button></form>'
        '</section>'
    )

    # experiment mini
    exp_mini = "".join(
        f'<div class="exp"><div class="wk">{html.escape(p["wk"])}</div>'
        f'<div class="body"><b>{html.escape(p["t"])}</b><small>{html.escape(p["d"])}</small></div>'
        f'<span class="status {experiment_status(p)[0]}">{experiment_status(p)[1]}</span></div>'
        for p in EXPERIMENT_PHASES
    )

    # live activity (owner only)
    log_lines = live_dashboard_log_lines(8)
    log_html = "".join(
        f'<div><span class="t">{html.escape(l[:9])}</span> {html.escape(l[9:200])}</div>'
        for l in reversed(log_lines[-6:])
    ) or '<div class="t">No dashboard activity yet.</div>'

    activity = (
        '<div class="panel"><div class="panel-h"><h2>Input &amp; Live Activity</h2>'
        f'<span class="hint">{input_count} waiting</span></div>'
        '<form class="upload-zone" action="/upload" method="post" enctype="multipart/form-data" data-owner-only>'
        '<b style="font-size:14px">Drop videos into input</b>'
        '<small style="display:block;color:var(--muted);font-size:12px;margin:6px 0 14px">.mp4 / .mov — then run Clip + Upload</small>'
        '<input type="hidden" name="upload_action" value="upload_and_clip">'
        '<input type="file" name="videos" accept=".mp4,.mov" multiple style="margin-bottom:12px;color:var(--muted);font-size:12px">'
        '<br><button class="btn primary" type="submit" style="margin:0 auto">Upload &amp; clip</button></form>'
        f'<div class="log" data-owner-only>{log_html}</div>'
        '</div>'
    )

    body = (
        chart + strip + autos
        + '<section class="grid-2">'
        + f'<div class="panel"><div class="panel-h"><h2>12-Week Experiment</h2><span class="hint">week {experiment_week()} of {config.PROJECT_TOTAL_WEEKS}</span></div>{exp_mini}</div>'
        + activity
        + '</section>'
    )

    head = CHART_HEAD
    ready = '<span class="pill ready"><span class="dot"></span>' + ('Running' if running else 'Ready') + '</span>'
    top_actions = ready + '<form class="inline" action="/run" method="post" data-owner-only><button class="btn primary" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>Clip + Upload</button></form>'
    return render_shell(
        "overview", "Mission Control", "Overview",
        "Clip, schedule, and run your Shorts pipeline — one quiet studio interface.",
        body, head_extra=head + chart_script("chartOv", "ov"), top_actions=top_actions,
    )


# ---------------------------------------------------------------------------
# YOUTUBE STATS  (/stats)
# ---------------------------------------------------------------------------
def render_stats_page(
    selected_range: str = "1d",
    selected_project_week: str = "",
    selected_project_sort: str = "recent",
    selected_stats_page: int = 1,
    auto_refresh_started: bool = False,
) -> str:
    selected_range = normalize_stats_range(selected_range)
    latest_rows = latest_video_stats()
    total_views = sum(parse_stat_int(r.get("view_count", "")) for r in latest_rows)
    total_likes = sum(parse_stat_int(r.get("like_count", "")) for r in latest_rows)
    total_comments = sum(parse_stat_int(r.get("comment_count", "")) for r in latest_rows)
    engagement = ((total_likes + total_comments) / total_views * 100) if total_views else 0.0
    tracked = len(latest_rows)
    hours = best_posting_hours()
    best_hour = str(hours[0]["hour"]) if hours else "—"
    delta_today = _today_views_delta()
    upload_records = count_upload_records()
    clips_ready = int(build_status()["clip_count"])
    uploaded_today = _uploaded_today_count()

    chart = (
        '<section class="panel chart-wrap"><div class="chart-glow"></div>'
        '<div class="chart-top"><div>'
        '<p class="eyebrow" style="color:var(--muted)">Channel Performance</p>'
        f'<div class="metric-big">{total_views:,}</div>'
        f'<div class="metric-cap">total views · <b>best hour {html.escape(best_hour)}</b> · {tracked:,} tracked clips</div>'
        '</div>' + _range_tabs("st", selected_range) + '</div>'
        '<canvas class="chart" id="chartSt"></canvas>'
        '<div class="metric-cap" style="margin-top:10px">Views gained per hour today (9 AM–9 PM) plus the overnight bar.</div>'
        '</section>'
    )

    def kpi(cls, label, ic, value, delta, delta_cls="up"):
        return (
            f'<div class="kpi {cls}"><div class="k-top"><span class="k-label">{label}</span>'
            f'<span class="k-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">{ic}</svg></span></div>'
            f'<div class="k-value">{value}</div><div class="k-delta {delta_cls}">{delta}</div></div>'
        )

    kpis = (
        '<section class="kpis">'
        + kpi("g", "Total Views", '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>',
              f'{total_views:,}', f'▲ {delta_today:,} today')
        + kpi("b", "Tracked Clips", '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 9l5 3-5 3z"/>',
              f'{tracked:,}', f'▲ {clips_ready:,} clips ready')
        + kpi("t", "Upload Records", '<path d="M12 3v12m0-12l-4 4m4-4l4 4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/>',
              f'{upload_records:,}', f'▲ {uploaded_today} today')
        + kpi("a", "Engagement", '<path d="M20.8 4.6a5.5 5.5 0 00-7.8 0L12 5.6l-1-1a5.5 5.5 0 00-7.8 7.8l9 9 8-8.2a5.5 5.5 0 000-7.6z"/>',
              f'{engagement:.2f}%', f'{total_likes:,} likes · {total_comments:,} comments', "flat")
        + '</section>'
    )

    # bars
    hour_bars = "".join(_bar_row(str(h["hour"]), f'{float(h["average_views"]):,.0f} avg · {int(h["video_count"])} vids', float(h["average_views"]), max(float(x["average_views"]) for x in hours[:5]) or 1, "green") for h in hours[:5])
    days = best_posting_days()
    max_day = max(float(x["average_views"]) for x in days) or 1
    day_bars = "".join(_bar_row(str(d["day"]), f'{float(d["average_views"]):,.0f} avg · {int(d["video_count"])} vids', float(d["average_views"]), max_day, "teal") for d in days)
    top_videos = _top_videos_markup(latest_rows)

    grid = (
        '<section class="grid-3">'
        f'<div class="panel"><div class="panel-h"><h2>Best Posting Hours</h2><span class="hint">Top 5 · avg</span></div>{hour_bars}</div>'
        f'<div class="panel"><div class="panel-h"><h2>Top Days to Post</h2><span class="hint">avg views</span></div>{day_bars}</div>'
        f'<div class="panel"><div class="panel-h"><h2>Top Videos</h2><span class="hint">by views</span></div>{top_videos}</div>'
        '</section>'
    )

    project = render_project_tracker_section(
        selected_project_week, selected_project_sort, selected_stats_page, selected_range
    )

    body = chart + kpis + grid + project
    top_actions = '<form class="inline" action="/refresh-stats" method="post" data-owner-only><input type="hidden" name="redirect_to" value="/stats"><button class="btn primary" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/></svg>Refresh YouTube Stats</button></form>'
    return render_shell(
        "stats", "Performance Center", "YouTube Stats",
        "Every clip's growth, best windows, and top performers.",
        body, head_extra=CHART_HEAD + chart_script("chartSt", "st", selected_range),
        top_actions=top_actions,
    )


def _bar_row(label: str, sub: str, value: float, max_v: float, cls: str) -> str:
    pct = max(4, round(value / max_v * 100)) if max_v else 4
    return (
        f'<div class="bar-row"><div class="bar-head"><b>{html.escape(label)}</b><span>{html.escape(sub)}</span></div>'
        f'<div class="track"><div class="fill {cls}" style="width:{pct}%"></div></div></div>'
    )


def _top_videos_markup(rows: list) -> str:
    ranked = sorted(rows, key=lambda r: parse_stat_int(r.get("view_count", "")), reverse=True)[:5]
    if not ranked:
        return '<p class="metric-cap">No ranked videos yet.</p>'
    out = []
    for i, r in enumerate(ranked, 1):
        views = parse_stat_int(r.get("view_count", ""))
        likes = parse_stat_int(r.get("like_count", ""))
        title = _clean_title(r.get("title", ""), r.get("clip_filename", ""))
        clip = r.get("clip_filename", "")
        out.append(
            f'<div class="vrow"><div class="vrank">{i}</div>'
            f'<div class="vname"><b>{html.escape(title[:28])}</b><small>{html.escape(clip)}</small></div>'
            f'<div class="vnum"><b>{views:,}</b><small>{likes} likes</small></div></div>'
        )
    return "".join(out)


# ---- project dataset tracker (kept functional, restyled to v4) ----
def render_project_tracker_section(selected_week: str, selected_sort: str,
                                   stats_page: int, selected_range: str) -> str:
    rows = read_project_dataset()
    selected_week = normalize_project_week(rows, selected_week)
    selected_sort = normalize_project_sort(selected_sort)
    summary = summarize_project_dataset(rows)

    weeks = planned_project_weeks()
    chips = ['<div class="chipset">']
    for wk in weeks:
        active = "fchip active" if wk == selected_week else "fchip"
        chips.append(f'<a class="{active}" href="/stats?project_week={wk.replace(" ", "%20")}&project_sort={selected_sort}&range={selected_range}">{html.escape(wk)}</a>')
    chips.append('</div>')
    chips = "".join(chips)

    filtered = [r for r in rows if r.get("project_week") == selected_week] if selected_week else rows
    filtered = sort_project_rows(filtered, selected_sort)

    page_size = STATS_TABLE_PAGE_SIZE
    total = len(filtered)
    total_pages = max(1, (total + page_size - 1) // page_size)
    stats_page = max(1, min(stats_page, total_pages))
    start = (stats_page - 1) * page_size
    visible = filtered[start:start + page_size]

    trs = []
    for r in visible:
        clip = r.get("clip_id", "") or r.get("clip_filename", "")
        caption = r.get("caption_word", "") or _clean_title(r.get("title", ""), clip)
        views = r.get("views_24h", "") or r.get("current_views", "") or "—"
        eng_raw = r.get("engagement_rate_24h", "") or r.get("current_engagement_rate", "")
        try:
            eng = f"{float(eng_raw) * 100:.2f}%" if eng_raw else "—"
        except ValueError:
            eng = html.escape(str(eng_raw)) or "—"
        trs.append(
            f'<tr><td class="clip">{html.escape(clip)}</td>'
            f'<td class="word">{html.escape(str(caption)[:26])}</td>'
            f'<td>{html.escape(str(r.get("project_week", "")))}</td>'
            f'<td>{html.escape(str(views))}</td>'
            f'<td>{eng}</td></tr>'
        )
    tbody = "".join(trs) or '<tr><td colspan="5" class="metric-cap">No rows for this week yet.</td></tr>'

    pager = _simple_pager(stats_page, total_pages,
                          lambda p: f'/stats?project_week={selected_week.replace(" ", "%20")}&project_sort={selected_sort}&range={selected_range}&stats_page={p}')

    downloads = (
        '<div class="top-actions" data-owner-only>'
        '<a class="btn" href="/project-data.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>CSV</a>'
        '<a class="btn" href="/project-data.xlsx"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>XLSX</a>'
        '</div>'
    )

    return (
        '<section class="panel"><div class="panel-h"><h2>Data-Science Tracker</h2>'
        f'<span class="hint">{summary.get("official", 0)} official · {summary.get("completed_24h", 0)} with 24h data</span></div>'
        + downloads + chips
        + '<div class="table-wrap"><table class="qtable"><thead><tr><th>Clip</th><th>Caption</th><th>Week</th><th>24h Views</th><th>Engagement</th></tr></thead>'
        + f'<tbody>{tbody}</tbody></table></div>' + pager
        + '</section>'
    )


def _simple_pager(page: int, total_pages: int, href) -> str:
    if total_pages <= 1:
        return ""
    out = ['<div class="pager">']
    prev_cls = "off" if page <= 1 else ""
    out.append(f'<a class="{prev_cls}" href="{href(max(1, page-1))}">‹ Prev</a>')
    for p in queue_page_numbers(page, total_pages):
        if p == page:
            out.append(f'<span class="cur">{p}</span>')
        else:
            out.append(f'<a href="{href(p)}">{p}</a>')
    next_cls = "off" if page >= total_pages else ""
    out.append(f'<a class="{next_cls}" href="{href(min(total_pages, page+1))}">Next ›</a>')
    out.append('</div>')
    return "".join(out)


# ---------------------------------------------------------------------------
# QUEUE  (/queue)
# ---------------------------------------------------------------------------
def render_queue_page(page: int = 1, sort_order: str = "oldest") -> str:
    sort_order = parse_queue_sort(sort_order)
    rows = sort_queue_rows(build_queue_rows(), sort_order)
    total = len(rows)
    total_pages = max(1, (total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * QUEUE_PAGE_SIZE
    visible = rows[start:start + QUEUE_PAGE_SIZE]

    trs = []
    for r in visible:
        clip = r.get("clip_filename", "")
        title = _clean_title(r.get("title", ""), clip)
        display = r.get("display_time", "") or "Not scheduled"
        st = queue_status(r.get("status", ""), r.get("scheduled_publish_time", ""))
        badge_cls, badge_txt = _queue_badge(st)
        trs.append(
            f'<tr><td class="clip">{html.escape(clip)}</td>'
            f'<td class="word">{html.escape(title[:30])}</td>'
            f'<td>{html.escape(display)}</td>'
            f'<td><span class="badge {badge_cls}">{badge_txt}</span>'
            f'{_queue_privacy_control(r)}</td></tr>'
        )
    tbody = "".join(trs) or '<tr><td colspan="4" class="metric-cap">No queued uploads yet.</td></tr>'

    pager = _simple_pager(page, total_pages, lambda p: f'/queue?page={p}&sort={sort_order}')
    sort_toggle = (
        '<select class="mini" onchange="location.href=this.value">'
        f'<option value="/queue?sort=oldest" {"selected" if sort_order=="oldest" else ""}>Oldest first</option>'
        f'<option value="/queue?sort=newest" {"selected" if sort_order=="newest" else ""}>Newest first</option>'
        '</select>'
    )

    body = (
        '<section class="panel"><div class="panel-h"><h2>Upcoming &amp; recent</h2>'
        f'<span class="hint">showing {len(visible)} of {total:,} records</span></div>'
        f'<div style="margin-bottom:14px">{sort_toggle}</div>'
        '<div class="table-wrap"><table class="qtable"><thead><tr><th>Clip</th><th>Caption</th><th>Scheduled slot</th><th>Status</th></tr></thead>'
        f'<tbody>{tbody}</tbody></table></div>{pager}'
        '<div class="metric-cap" style="margin-top:14px">Download the CSV for the full record of all uploads.</div>'
        '</section>'
    )
    top_actions = (
        '<a class="btn" href="/queue.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>Download CSV</a>'
    )
    return render_shell(
        "queue", "Publishing schedule", "Queue",
        "Your current upload schedule at a glance.", body, top_actions=top_actions,
    )


def _queue_badge(status: str):
    mapping = {
        "scheduled": ("sch", "Scheduled"),
        "uploaded": ("up", "Uploaded"),
        "deferred": ("def", "Deferred"),
        "failed": ("fail", "Failed"),
        "waiting": ("wait", "Waiting"),
    }
    return mapping.get(status, ("wait", status.title() or "Waiting"))


def _queue_privacy_control(row: dict) -> str:
    """Owner-only inline privacy switcher (hidden from public viewers)."""
    video_id = row.get("youtube_video_id", "")
    if not video_id:
        return ""
    current = row.get("privacy_status", "") or "private"
    options = "".join(
        f'<option value="{p}"{" selected" if p == current else ""}>{p.title()}</option>'
        for p in ("public", "unlisted", "private")
    )
    return (
        '<form action="/queue/privacy" method="post" data-owner-only '
        'style="display:inline-block;margin-left:10px">'
        f'<input type="hidden" name="youtube_video_id" value="{html.escape(video_id)}">'
        f'<input type="hidden" name="row_anchor" value="{queue_row_anchor(row)}">'
        f'<select class="mini" name="privacy_status" onchange="this.form.submit()">{options}</select>'
        '</form>'
    )


def queue_csv() -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["clip_filename", "youtube_video_id", "caption", "scheduled_slot", "status"])
    for r in sort_queue_rows(build_queue_rows(), "oldest"):
        st = queue_status(r.get("status", ""), r.get("scheduled_publish_time", ""))
        writer.writerow([
            r.get("clip_filename", ""), r.get("youtube_video_id", ""),
            _clean_title(r.get("title", ""), r.get("clip_filename", "")),
            r.get("display_time", ""), st,
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# TIKTOK CANDIDATES  (/tiktok-candidates)
# ---------------------------------------------------------------------------
def render_tiktok_candidates_page(selected_date: str = "") -> str:
    _ = selected_date
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    all_days = tiktok_candidate_days()
    # past week only
    week_days = []
    for day in all_days:
        try:
            d = datetime.fromisoformat(str(day["date"])).date()
        except ValueError:
            continue
        if (today - d).days <= 6:
            week_days.append(day)

    cards = []
    for day in week_days:
        date_str = str(day["date"])
        candidates = list(day["candidates"])[:3]
        is_today = date_str == today.isoformat()
        grid = []
        for i, c in enumerate(candidates, 1):
            title = _clean_title(str(c["title"]), str(c["clip_filename"]))
            grid.append(
                f'<div class="tt-card r{i}"><div class="rank">#{i} candidate</div>'
                f'<div class="w">{html.escape(title)}</div>'
                f'<div class="c">{html.escape(str(c["clip_filename"]))}</div>'
                f'<div class="m"><div><b>{int(c["views"]):,}</b><span>views</span></div>'
                f'<div><b>{int(c["likes"]):,}</b><span>likes</span></div>'
                f'<div><b>{float(c["like_rate"]):.1f}%</b><span>like rate</span></div></div></div>'
            )
        live = '<span class="live">Updates daily</span>' if is_today else ''
        label = "Today" if is_today else format_stats_date(date_str)
        cards.append(
            f'<div class="tt-day panel" id="day-{html.escape(date_str)}"><div class="dh"><h3>{html.escape(label)}</h3>'
            f'<span>{html.escape(format_stats_date(date_str))} · {len(candidates)} clips</span>{live}</div>'
            f'<div class="tt-grid">{"".join(grid)}</div></div>'
        )

    body = "".join(cards) or '<div class="panel"><div class="placeholder"><h3>No candidates in the past week</h3><div>Once clips have 24h of stats they appear here.</div></div></div>'
    top_actions = (
        '<a class="btn primary" href="/tiktok-candidates.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>Download CSV of all weeks</a>'
    )
    return render_shell(
        "tiktok", "YouTube winners for TikTok", "TikTok Candidates",
        "Top 3 performing clips per day over the past week — refreshed automatically to repost on TikTok.",
        body, top_actions=top_actions,
    )


def tiktok_all_weeks_csv() -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["stats_date", "rank", "clip_filename", "youtube_video_id", "caption", "views", "likes", "comments", "score", "like_rate_pct"])
    for day in tiktok_candidate_days():
        date_str = str(day["date"])
        for i, c in enumerate(list(day["candidates"]), 1):
            writer.writerow([
                date_str, i, c.get("clip_filename", ""), c.get("youtube_video_id", ""),
                _clean_title(str(c.get("title", "")), str(c.get("clip_filename", ""))),
                int(c.get("views", 0)), int(c.get("likes", 0)), int(c.get("comments", 0)),
                int(c.get("score", 0)), f'{float(c.get("like_rate", 0)):.2f}',
            ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# VIDEO TRACKER  (/tracker)
# ---------------------------------------------------------------------------
def render_tracker_page() -> str:
    rows = read_tracker_rows()
    total = len(rows)
    preferred = ["clip_filename", "title", "youtube_video_id", "scheduled_publish_time", "view_count", "like_count", "comment_count"]
    headers = [h for h in preferred if rows and h in rows[0]]
    if not headers and rows:
        headers = list(rows[0].keys())[:7]

    def cell(row, key):
        val = row.get(key, "")
        if key == "title":
            val = _clean_title(val, row.get("clip_filename", ""))[:30]
        cls = ' class="clip"' if key in ("clip_filename", "youtube_video_id") else ''
        return f'<td{cls}>{html.escape(str(val))}</td>'

    thead = "".join(f'<th>{html.escape(h.replace("_", " ").title())}</th>' for h in headers)
    trs = "".join('<tr>' + "".join(cell(r, h) for h in headers) + '</tr>' for r in rows[:200])
    tbody = trs or f'<tr><td colspan="{max(1,len(headers))}" class="metric-cap">No tracker rows yet.</td></tr>'

    body = (
        '<section class="panel"><div class="panel-h"><h2>Clip &amp; metadata tracker</h2>'
        f'<span class="hint">{total:,} records · showing first {min(200,total)}</span></div>'
        f'<div class="table-wrap"><table class="qtable"><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>'
        '<div class="metric-cap" style="margin-top:14px">Download the full CSV or XLSX for every tracked clip.</div>'
        '</section>'
    )
    top_actions = (
        '<div class="top-actions" data-owner-only>'
        '<a class="btn" href="/tracker.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>CSV</a>'
        '<a class="btn" href="/tracker.xlsx"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>XLSX</a></div>'
    )
    return render_shell(
        "tracker", "Automation records", "Video Tracker",
        "Every clip, its metadata, and its YouTube identifiers.", body, top_actions=top_actions,
    )


# ---------------------------------------------------------------------------
# 12-WEEK EXPERIMENT  (/experiment)
# ---------------------------------------------------------------------------
EXPERIMENT_PHASES = [
    {"wk": "Wk 1–2", "lo": 1, "hi": 2, "t": "Baseline",
     "d": "Establish per-clip 24h view & engagement rates",
     "goal": "Build a clean control set so every later test has a benchmark.",
     "method": "For each clip, record 24h views, likes, comments; compute engagement = (likes+comments)/views. Summarize mean &amp; variance and the distribution of 24h views.",
     "tags": ["Descriptive stats", "24h view distribution", "Control baseline"]},
    {"wk": "Wk 3–4", "lo": 3, "hi": 4, "t": "Posting-time test",
     "d": "9 AM–3 PM vs 4 PM–9 PM windows",
     "goal": "Find whether morning/afternoon or evening posting drives more 24h views.",
     "method": "Randomly assign clips to each window to remove content bias. Compare mean 24h views with a two-sample t-test (Mann-Whitney if skewed); report effect size + 95% CI.",
     "tags": ["A/B test", "t-test", "Effect size", "Confidence interval"]},
    {"wk": "Wk 5–6", "lo": 5, "hi": 6, "t": "Caption test",
     "d": "Emotional vs energy caption words",
     "goal": "Test if emotional (PEACE, SERENE, STILL) vs energy (FLOW, VIBE, FREE) captions change reach.",
     "method": "Balance caption family across posting times. Compare group means; fit a logistic regression on a \"high performer\" flag controlling for hour to isolate caption effect.",
     "tags": ["A/B test", "Logistic regression", "Confounder control"]},
    {"wk": "Wk 7–8", "lo": 7, "hi": 8, "t": "Frequency test",
     "d": "1 / hour vs 2× in best windows",
     "goal": "See if posting denser in peak windows lifts total views without cannibalizing per-clip views.",
     "method": "Compare per-day total views and per-clip views between cadences. Paired comparison across matched days; watch for diminishing returns.",
     "tags": ["Paired test", "Cannibalization check", "Per-day totals"]},
    {"wk": "Wk 9–12", "lo": 9, "hi": 12, "t": "Content type + capstone model",
     "d": "Solo / church / jazz · predictive model",
     "goal": "Predict which clips will perform from their features, and write the capstone.",
     "method": "Tag clips by type, shot, orientation. Fit multivariate regression / gradient-boosted model to predict high performers; report feature importance and a final writeup.",
     "tags": ["Feature engineering", "Regression", "Gradient boosting", "Feature importance"]},
]


def experiment_week() -> int:
    start = datetime.fromisoformat(config.PROJECT_WEEK_1_START_DATE).date()
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    if today < start:
        return 1
    return max(1, (today - start).days // 7 + 1)


def experiment_status(phase: dict):
    wk = experiment_week()
    if wk > phase["hi"]:
        return ("st-done", "Done")
    if phase["lo"] <= wk <= phase["hi"]:
        return ("st-live", "Live")
    return ("st-next", "Next")


def render_experiment_page() -> str:
    wk = experiment_week()
    rows_out = []
    for p in EXPERIMENT_PHASES:
        s_cls, s_lbl = experiment_status(p)
        tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in p["tags"])
        rows_out.append(
            f'<details class="accw"{" open" if s_cls=="st-live" else ""}>'
            f'<summary><div class="wk">{html.escape(p["wk"])}</div>'
            f'<div class="body" style="flex:1"><b>{html.escape(p["t"])}</b><small>{html.escape(p["d"])}</small></div>'
            f'<span class="status {s_cls}">{s_lbl}</span>'
            '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M9 6l6 6-6 6"/></svg></summary>'
            f'<div class="acc-inner"><span class="lab">Goal</span>{p["goal"]}'
            f'<span class="lab">How we analyze</span>{p["method"]}'
            f'<div class="tags">{tags}</div></div></details>'
        )
    body = (
        '<div class="panel"><div class="panel-h"><h2>Experiment roadmap</h2>'
        '<span class="hint">click a phase to expand the analysis plan</span></div>'
        + "".join(rows_out) + '</div>'
    )
    sub = f'Currently week {wk} of {config.PROJECT_TOTAL_WEEKS} · primary metric: 24h views'
    return render_shell("experiment", "Data Science Project", "12-Week Experiment", sub, body)


def run_server() -> None:
    """Start the local dashboard server."""
    ensure_directories()
    configure_logging()
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    url = f"http://localhost:{PORT}/"
    logger.info("Dashboard running at %s", url)
    print(f"Dashboard running at {url}")
    print("Press Control+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
        print("Dashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
