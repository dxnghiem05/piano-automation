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

# --- Stats-history read cache -------------------------------------------------
# The YouTube stats history CSV is large (tens of thousands of rows) and several
# functions read it multiple times per page render. Cache the parsed rows keyed
# by the file's modification time so repeated reads in one request are instant,
# while any refresh (which rewrites the file) transparently invalidates it.
import stats_tracker as _stats_tracker

_STATS_HISTORY_CACHE: dict[str, object] = {}


def _cached_read_stats_history() -> list[dict[str, str]]:
    path = config.YOUTUBE_STATS_HISTORY_FILE
    key = path.stat().st_mtime_ns if path.exists() else 0
    if _STATS_HISTORY_CACHE.get("key") != key:
        _STATS_HISTORY_CACHE["key"] = key
        _STATS_HISTORY_CACHE["rows"] = _stats_tracker._uncached_read_stats_history()
    return _STATS_HISTORY_CACHE["rows"]  # type: ignore[return-value]


if not hasattr(_stats_tracker, "_uncached_read_stats_history"):
    _stats_tracker._uncached_read_stats_history = _stats_tracker.read_stats_history
    _stats_tracker.read_stats_history = _cached_read_stats_history
# Point this module's imported name at the cached version too.
read_stats_history = _cached_read_stats_history


def _hist_mtime_key() -> tuple:
    def _m(path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0
    return (_m(config.YOUTUBE_STATS_HISTORY_FILE), _m(config.UPLOAD_LOG_FILE))


def _memoize_by_history(func):
    """Cache a pure history-derived function's result until the stats files change."""
    store: dict = {}

    def wrapper(*args):
        key = _hist_mtime_key()
        entry = store.get(args)
        if entry is None or entry[0] != key:
            value = func(*args)
            store[args] = (key, value)
            return value
        return entry[1]

    wrapper.__name__ = getattr(func, "__name__", "wrapped")
    return wrapper


# Memoize the heavy per-request aggregations so repeat page loads are instant.
latest_video_stats = _memoize_by_history(latest_video_stats)
best_posting_hours = _memoize_by_history(best_posting_hours)
best_posting_days = _memoize_by_history(best_posting_days)

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
    return parsed.path in {"/", "/overview", "/stats", "/tracker", "/tiktok-candidates", "/experiment", "/data-science"}


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

        if path == "/overview":
            self.send_html(viewer_mode_html(render_overview(), owner))
            return

        if path == "/tracker":
            self.send_html(viewer_mode_html(render_tracker_page(), owner))
            return

        if path == "/queue":
            # Queue is folded into the Stats page now; keep the old link working.
            self.redirect("/stats")
            return

        if path == "/tiktok-candidates":
            query = parse_qs(parsed.query)
            selected_date = query.get("date", [""])[0]
            self.send_html(viewer_mode_html(render_tiktok_candidates_page(selected_date), owner))
            return

        if path == "/stats":
            query = parse_qs(parsed.query)
            selected_range = query.get("range", ["1d"])[0]
            auto_refresh_youtube_stats_if_stale()
            self.send_html(viewer_mode_html(render_stats_page(selected_range), owner))
            return

        if path == "/data-science":
            query = parse_qs(parsed.query)
            self.send_html(
                viewer_mode_html(
                    render_data_science_page(
                        query.get("project_week", [""])[0],
                        query.get("project_sort", ["recent"])[0],
                        parse_page(query.get("stats_page", ["1"])[0]),
                        query.get("range", ["1d"])[0],
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
        if path not in {"/", "/overview", "/stats", "/tracker", "/tiktok-candidates", "/experiment", "/data-science"}:
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


def _build_queue_rows_uncached() -> list[dict[str, str]]:
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


_QUEUE_ROWS_CACHE: dict[str, object] = {}


def build_queue_rows() -> list[dict[str, str]]:
    """Cached build_queue_rows keyed by the files it depends on (invalidates on change)."""
    def _mtime(path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0
    key = (
        _mtime(config.UPLOAD_LOG_FILE), _mtime(config.PRIVACY_OVERRIDES_FILE),
        _mtime(config.METADATA_FILE), _mtime(config.YOUTUBE_STATS_HISTORY_FILE),
    )
    if _QUEUE_ROWS_CACHE.get("key") != key:
        _QUEUE_ROWS_CACHE["key"] = key
        _QUEUE_ROWS_CACHE["rows"] = _build_queue_rows_uncached()
    return _QUEUE_ROWS_CACHE["rows"]  # type: ignore[return-value]


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


chart_view_gains = _memoize_by_history(chart_view_gains)


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
# v5 dashboard UI — vibrant "Piano Shorts" concept, server-rendered & wired to
# the live data functions. Home splash + real routes per tab. Owner-only actions
# stay inside forms hidden from public viewers by the viewer-mode overlay.
# ============================================================================

STYLE_V5 = r"""<style>
  :root{
    --green:#1ed760;--green-soft:rgba(30,215,96,.16);
    --blue:#4f97ff;--violet:#8b6cff;--teal:#24d6b6;--amber:#f5b544;--rose:#ff5d78;
    --text:#f6f8f6;--muted:#aeb6c2;--faint:#7c8595;
    --panel:rgba(255,255,255,.045);--line:rgba(255,255,255,.10);
    --font:'Figtree',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;min-height:100%}
  a{color:inherit;text-decoration:none}
  body{font-family:var(--font);color:var(--text);background:#05060a;-webkit-font-smoothing:antialiased;letter-spacing:-.011em;min-height:100vh;position:relative;overflow-x:hidden}
  ::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#2a2e2a;border-radius:8px}

  .stage{position:fixed;inset:0;z-index:0;overflow:hidden}
  .wash{position:absolute;inset:-12%;
    background:
      radial-gradient(48% 40% at 20% 16%, rgba(139,108,255,.50), transparent 60%),
      radial-gradient(46% 42% at 84% 24%, rgba(79,151,255,.42), transparent 60%),
      radial-gradient(65% 55% at 50% 104%, rgba(30,215,96,.30), transparent 60%),
      radial-gradient(40% 40% at 92% 88%, rgba(245,105,120,.28), transparent 60%),
      linear-gradient(180deg,#070812,#04060b 60%,#02040a)}
  .blob{position:absolute;border-radius:50%;filter:blur(70px);opacity:.4}
  .blob.g{width:640px;height:640px;background:radial-gradient(circle,#1ed760,transparent 68%);top:-190px;left:6%;animation:drift 16s ease-in-out infinite}
  .blob.v{width:560px;height:560px;background:radial-gradient(circle,#8b6cff,transparent 68%);top:-130px;right:5%;animation:drift 19s ease-in-out infinite reverse}
  .blob.b{width:520px;height:520px;background:radial-gradient(circle,#4f97ff,transparent 68%);bottom:-170px;left:42%;animation:drift 22s ease-in-out infinite}
  @keyframes drift{0%,100%{transform:translate(0,0)}50%{transform:translate(26px,-22px)}}
  .grain{position:absolute;inset:0;opacity:.05;background-image:radial-gradient(#fff 1px,transparent 1px);background-size:4px 4px}
  .keys{position:absolute;left:50%;bottom:-46px;transform:translateX(-50%) perspective(1150px) rotateX(51deg);
    transform-origin:bottom center;display:flex;gap:4px;opacity:.46;transition:opacity .6s}
  body.tab .keys{opacity:.16}
  .key{width:50px;height:278px;border-radius:0 0 8px 8px;background:linear-gradient(180deg,#e9edf6,#aab3c6);position:relative;box-shadow:inset 0 -12px 20px rgba(0,0,0,.25)}
  .key.g{background:linear-gradient(180deg,#c9ffe0,#1ed760);box-shadow:0 0 30px rgba(30,215,96,.8)}
  .key.v{background:linear-gradient(180deg,#e5dcff,#8b6cff);box-shadow:0 0 30px rgba(139,108,255,.8)}
  .key.b{background:linear-gradient(180deg,#d5e7ff,#4f97ff);box-shadow:0 0 30px rgba(79,151,255,.8)}
  .bk{position:absolute;top:0;right:-14px;width:28px;height:176px;border-radius:0 0 5px 5px;background:linear-gradient(180deg,#181c26,#05070c);z-index:3;box-shadow:0 6px 8px rgba(0,0,0,.5)}
  .key.nb .bk{display:none}
  .note{position:absolute;bottom:3%;color:rgba(255,255,255,.4);text-shadow:0 0 6px rgba(30,215,96,.35);will-change:transform,opacity;pointer-events:none}
  @keyframes rise{0%{transform:translateY(20px) rotate(0);opacity:0}12%{opacity:.9}85%{opacity:.6}100%{transform:translateY(-48vh) rotate(16deg);opacity:0}}

  /* home */
  .home{position:relative;z-index:2;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:40px 20px}
  .shell{position:relative;z-index:2;width:min(1160px,94vw);margin:0 auto}
  .mark{width:54px;height:54px;border-radius:50%;background:var(--green);display:grid;place-items:center;margin:0 auto 8px;box-shadow:0 0 0 7px var(--green-soft),0 0 40px rgba(30,215,96,.6);animation:beat 3.4s ease-in-out infinite}
  @keyframes beat{0%,100%{box-shadow:0 0 0 7px var(--green-soft),0 0 32px rgba(30,215,96,.5)}50%{box-shadow:0 0 0 10px var(--green-soft),0 0 56px rgba(30,215,96,.85)}}
  .mark svg{width:27px;height:27px}
  h1.big{font-size:clamp(40px,6.4vw,72px);font-weight:850;margin:6px 0 0;letter-spacing:-.045em;background:linear-gradient(180deg,#fff,#cfe9d9);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .kicker{margin:14px 0 4px;letter-spacing:.42em;font-size:13px;font-weight:700;color:rgba(255,255,255,.72);text-transform:uppercase}
  .stat-line{color:var(--muted);font-size:13.5px;margin-bottom:26px;font-weight:600}.stat-line b{color:var(--green)}
  .glass{position:relative;margin:0 auto;padding:32px 26px 28px;border-radius:26px;width:min(1180px,95vw);
    background:linear-gradient(180deg,rgba(255,255,255,.13),rgba(255,255,255,.045));border:1px solid rgba(255,255,255,.18);
    backdrop-filter:blur(26px) saturate(150%);-webkit-backdrop-filter:blur(26px) saturate(150%);
    box-shadow:0 40px 120px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.25);transition:transform .2s cubic-bezier(.2,.7,.2,1)}
  .glass::before{content:"";position:absolute;top:0;left:38px;right:38px;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent)}
  .grid5{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
  .tile{position:relative;overflow:hidden;padding:24px 16px 22px;border-radius:18px;cursor:pointer;text-align:center;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);transition:transform .28s cubic-bezier(.2,.8,.2,1),background .28s,border-color .28s,box-shadow .28s;display:block}
  .tile::after{content:"";position:absolute;width:180px;height:180px;border-radius:50%;filter:blur(46px);top:-100px;right:-60px;opacity:0;transition:opacity .35s;background:var(--ac,var(--green))}
  .tile:hover{transform:translateY(-8px) scale(1.035);background:rgba(255,255,255,.07);border-color:color-mix(in srgb,var(--ac,var(--green)) 55%,transparent);box-shadow:0 22px 50px rgba(0,0,0,.4)}
  .tile:hover::after{opacity:.55}
  .tile .ic{width:56px;height:56px;margin:0 auto 15px;display:grid;place-items:center;border-radius:15px;color:#eef2f8;background:rgba(255,255,255,.04);transition:transform .35s cubic-bezier(.2,.8,.2,1),color .3s,background .3s}
  .tile:hover .ic{color:var(--ac);background:color-mix(in srgb,var(--ac) 18%,transparent);transform:translateY(-2px) scale(1.08)}
  .tile .ic svg{width:30px;height:30px;transition:filter .3s}
  .tile:hover .ic svg{filter:drop-shadow(0 0 10px color-mix(in srgb,var(--ac) 90%,transparent))}
  .tile b{display:block;font-size:15px;font-weight:750;color:#f6f8f6}
  .tile small{display:block;color:var(--muted);font-size:11.5px;margin-top:3px;opacity:.55;transition:.3s}
  .tile:hover small{opacity:1;color:#cfd6e0}
  .tile .go{position:absolute;top:13px;right:14px;color:var(--ac);opacity:0;transform:translateX(-4px);transition:.3s;font-weight:800}
  .tile:hover .go{opacity:1;transform:none}
  .t1{--ac:#1ed760}.t2{--ac:#4f97ff}.t3{--ac:#ff5d78}.t4{--ac:#f5b544}.t5{--ac:#8b6cff}
  .eq rect{transform-origin:bottom;animation:bar 1.1s ease-in-out infinite;animation-play-state:paused}
  .tile:hover .eq rect{animation-play-state:running}
  .eq rect:nth-child(2){animation-delay:.18s}.eq rect:nth-child(3){animation-delay:.36s}.eq rect:nth-child(4){animation-delay:.1s}
  @keyframes bar{0%,100%{transform:scaleY(.4)}50%{transform:scaleY(1)}}
  .social{margin-top:32px;display:flex;justify-content:center;gap:22px}
  .social a{width:44px;height:44px;display:grid;place-items:center;border-radius:50%;color:#cdd4de;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.04);transition:transform .25s cubic-bezier(.2,.8,.2,1),color .25s,box-shadow .25s,background .25s}
  .social a:hover{transform:translateY(-4px) scale(1.12);color:#fff;background:var(--green-soft);box-shadow:0 10px 26px rgba(30,215,96,.4);border-color:transparent}
  .social a svg{width:19px;height:19px}

  /* tab chrome */
  .bar{position:sticky;top:0;z-index:6;display:flex;align-items:center;gap:16px;padding:14px clamp(16px,4vw,40px);backdrop-filter:blur(14px);background:linear-gradient(180deg,rgba(6,8,12,.72),rgba(6,8,12,.3));border-bottom:1px solid var(--line)}
  .backhome{display:inline-flex;align-items:center;gap:9px;font-weight:800;font-size:14px;cursor:pointer;padding:8px 12px;border-radius:11px;transition:.2s}
  .backhome:hover{background:rgba(255,255,255,.06)}
  .backhome .dot{width:22px;height:22px;border-radius:50%;background:var(--green);display:grid;place-items:center}
  .backhome .dot svg{width:12px;height:12px}
  .pills{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
  .pills a{font-size:12.5px;font-weight:700;color:var(--muted);padding:8px 13px;border-radius:999px;transition:.18s}
  .pills a:hover{background:rgba(255,255,255,.06);color:var(--text)}
  .pills a.on{background:var(--green-soft);color:var(--green)}
  .page{padding:26px clamp(16px,4vw,40px) 90px}
  .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--green);font-weight:800;margin:0 0 6px}
  .h2{font-size:clamp(24px,3vw,34px);font-weight:850;letter-spacing:-.03em;margin:0}
  .sub{color:var(--muted);font-size:14px;margin-top:5px}
  .topline{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:6px}
  .top-actions{display:flex;align-items:center;gap:10px}
  .panel{position:relative;overflow:hidden;background:rgba(16,18,23,.66);border:1px solid var(--line);border-radius:18px;padding:20px}
  .panel h3{margin:0 0 14px;font-size:15px;font-weight:750}
  .row{display:grid;gap:16px}
  .r-4{grid-template-columns:repeat(4,1fr)}.r-3{grid-template-columns:repeat(3,1fr)}.r-2{grid-template-columns:1.5fr 1fr}
  @media(max-width:960px){.r-4,.r-3,.r-2,.grid5{grid-template-columns:1fr 1fr}}
  @media(max-width:560px){.r-4,.r-3,.r-2,.grid5{grid-template-columns:1fr}}
  .mt{margin-top:16px}

  .pill{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:999px;font-size:12.5px;font-weight:700}
  .pill.ready{background:var(--green-soft);color:var(--green)}
  .pill.ready .d{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border-radius:999px;font:800 13px var(--font);cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--text);transition:.18s}
  .btn:hover{background:rgba(255,255,255,.08)}
  .btn.primary{background:var(--green);color:#04140a;border-color:var(--green)}
  .btn.primary:hover{transform:translateY(-2px);box-shadow:0 10px 26px rgba(30,215,96,.4)}
  .btn svg{width:15px;height:15px}
  form.inline{display:inline-flex;margin:0}

  .kpi{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;transition:.25s}
  .kpi:hover{transform:translateY(-4px);border-color:color-mix(in srgb,var(--kc,var(--green)) 50%,transparent);box-shadow:0 18px 40px rgba(0,0,0,.35)}
  .kpi::after{content:"";position:absolute;width:150px;height:150px;border-radius:50%;filter:blur(46px);top:-80px;right:-50px;opacity:.4;background:var(--kc,var(--green))}
  .kpi .lab{color:var(--muted);font-size:12.5px;font-weight:600}
  .kpi .val{font-size:30px;font-weight:850;letter-spacing:-.03em;margin-top:8px}
  .kpi .d{font-size:12px;font-weight:700;color:var(--kc,var(--green));margin-top:3px}
  .kc-g{--kc:#1ed760}.kc-b{--kc:#4f97ff}.kc-t{--kc:#24d6b6}.kc-a{--kc:#f5b544}
  .hero-num{font-size:clamp(46px,7vw,84px);font-weight:900;letter-spacing:-.04em;line-height:1;background:linear-gradient(180deg,#fff,#bfe9cf);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .chartbox{position:relative;height:240px;margin-top:14px;width:100%}

  .pk-wrap{display:flex;align-items:flex-end;gap:10px;height:220px;padding-top:10px}
  .pk{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
  .pk .col{width:100%;max-width:52px;border-radius:8px 8px 6px 6px;background:linear-gradient(180deg,#e9edf6,#aab3c6);transition:transform .3s,box-shadow .3s;box-shadow:inset 0 -10px 16px rgba(0,0,0,.2)}
  .pk.best .col{background:linear-gradient(180deg,#c9ffe0,#1ed760);box-shadow:0 0 24px rgba(30,215,96,.7)}
  .pk:hover .col{transform:translateY(-5px) scaleY(1.02);box-shadow:0 0 26px rgba(79,151,255,.6)}
  .pk .v{font-size:12px;font-weight:800;margin-bottom:6px;color:#eef2f8}
  .pk .h{font-size:11px;color:var(--muted);margin-top:8px;font-weight:700}

  .lead{display:grid;grid-template-columns:34px 1fr auto;gap:14px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
  .lead:last-child{border-bottom:0}
  .lead .rk{font-weight:900;font-size:16px;text-align:center;background:linear-gradient(180deg,#fff,#9fdcb4);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .lead .nm b{display:block;font-size:14px}.lead .nm small{color:var(--faint);font-size:11.5px;font-family:ui-monospace,Menlo,monospace}
  .lead .nu{text-align:right}.lead .nu b{display:block}.lead .nu small{color:var(--green);font-size:11.5px}

  .set{display:flex;align-items:center;gap:14px;padding:13px 14px;border-radius:13px;border:1px solid var(--line);background:var(--panel);margin-bottom:9px;transition:.2s}
  .set:hover{transform:translateX(4px);border-color:rgba(30,215,96,.5);background:rgba(30,215,96,.06)}
  .set .np{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;background:var(--green-soft);color:var(--green);flex:none}
  .set .np svg{width:16px;height:16px}
  .set .mid{flex:1}.set .mid b{font-size:14px}.set .mid small{display:block;color:var(--faint);font-size:11.5px;font-family:ui-monospace,Menlo,monospace}
  .set .time{color:var(--muted);font-size:12.5px;font-weight:700}
  .badge{font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;margin-left:12px}
  .badge.up{background:var(--green-soft);color:var(--green)}.badge.sch{background:rgba(79,151,255,.16);color:var(--blue)}

  .podium{display:grid;grid-template-columns:1fr 1.15fr 1fr;gap:14px;align-items:end;margin-bottom:8px}
  .pod{position:relative;overflow:hidden;border-radius:16px;padding:20px 16px;text-align:center;border:1px solid var(--line);background:var(--panel);transition:.25s}
  .pod::after{content:"";position:absolute;inset:0;opacity:.14;background:var(--pc)}
  .pod .medal{font-size:26px}.pod .w{font-size:18px;font-weight:850;margin:8px 0 2px;position:relative}
  .pod .c{font-size:11px;color:var(--faint);font-family:ui-monospace,Menlo,monospace;position:relative}
  .pod .met{margin-top:10px;font-size:13px;position:relative}.pod .met b{color:#fff}
  .pod.gold{--pc:#f5b544;transform:translateY(-10px)}.pod.silver{--pc:#c8ccd2}.pod.bronze{--pc:#e08a4b}
  .pod:hover{transform:translateY(-14px);box-shadow:0 20px 44px rgba(0,0,0,.4)}
  .pod.gold:hover{transform:translateY(-22px)}
  .day-block{margin-top:20px}.day-block h4{margin:0 0 10px;font-size:13px;color:var(--muted);font-weight:700}

  .lib{display:grid;grid-template-columns:repeat(7,1fr);gap:12px}
  @media(max-width:1100px){.lib{grid-template-columns:repeat(4,1fr)}}
  @media(max-width:640px){.lib{grid-template-columns:repeat(2,1fr)}}
  .alb{border-radius:14px;overflow:hidden;border:1px solid var(--line);background:var(--panel);transition:.25s;cursor:pointer;display:block}
  .alb:hover{transform:translateY(-6px) scale(1.03);box-shadow:0 18px 40px rgba(0,0,0,.4);border-color:rgba(139,108,255,.5)}
  .alb .cover{height:104px;background:#0e1118 center/cover no-repeat;position:relative}
  .alb .play{position:absolute;top:8px;right:8px;width:22px;height:22px;border-radius:50%;background:rgba(0,0,0,.45);display:grid;place-items:center;opacity:0;transition:.25s}
  .alb:hover .play{opacity:1}.alb .play svg{width:11px;height:11px;color:#fff}
  .alb .meta{padding:10px 11px}.alb .meta b{font-size:12.5px;display:block}.alb .meta small{color:var(--faint);font-size:10.5px;font-family:ui-monospace,Menlo,monospace}
  .dlbtn{display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border-radius:999px;font-weight:800;font-size:13px;cursor:pointer;color:#04140a;background:var(--green);transition:.2s}
  .dlbtn:hover{transform:translateY(-2px);box-shadow:0 10px 26px rgba(30,215,96,.4)}.dlbtn svg{width:15px;height:15px}

  .octave{display:flex;gap:5px;justify-content:center;margin:10px 0 22px;flex-wrap:wrap}
  .wkey{width:58px;height:150px;border-radius:0 0 9px 9px;background:linear-gradient(180deg,#20242e,#12151c);border:1px solid rgba(255,255,255,.08);position:relative;display:flex;align-items:flex-end;justify-content:center;padding-bottom:12px;transition:.3s}
  .wkey .n{font-size:12px;font-weight:800;color:var(--faint)}
  .wkey.done{background:linear-gradient(180deg,#123a24,#0c2417)}.wkey.done .n{color:#8ef0ab}
  .wkey.live{background:linear-gradient(180deg,#c9ffe0,#1ed760);box-shadow:0 0 30px rgba(30,215,96,.75);animation:beat 2.4s infinite}.wkey.live .n{color:#04140a}
  .wkey:hover{transform:translateY(-6px)}
  .phase{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:10px;overflow:hidden}
  .phase summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:14px;padding:16px}
  .phase summary::-webkit-details-marker{display:none}
  .phase .wk{font-size:11px;font-weight:800;color:var(--green);width:64px;flex:none}
  .phase .pt b{display:block;font-size:15px}.phase .pt small{color:var(--muted);font-size:12.5px}
  .phase .st{margin-left:auto;font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;padding:4px 10px;border-radius:999px}
  .st.live{background:var(--green-soft);color:var(--green)}.st.done{background:rgba(255,255,255,.06);color:var(--faint)}.st.next{background:rgba(79,151,255,.14);color:var(--blue)}
  .phase .body{padding:0 16px 18px 78px;color:var(--muted);font-size:13px;line-height:1.6}
  .phase .body .l{color:var(--faint);text-transform:uppercase;font-size:10px;letter-spacing:.1em;font-weight:800;display:block;margin:10px 0 3px}
  .tag{display:inline-block;font-size:11px;font-weight:700;color:var(--green);background:var(--green-soft);padding:4px 10px;border-radius:999px;margin:6px 6px 0 0}
  .prog{height:8px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;margin:6px 0 4px}
  .prog i{display:block;height:100%;background:linear-gradient(90deg,#1ed760,#8ef0ab);border-radius:999px}

  .chip{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 16px}
  .chip .l{color:var(--muted);font-size:12px;font-weight:600}.chip .v{font-size:22px;font-weight:850;margin-top:7px}
  .log{background:#06070688;border:1px solid var(--line);border-radius:12px;padding:14px 16px;font:500 12px ui-monospace,Menlo,monospace;color:#b7c0b7;margin-top:14px;max-height:210px;overflow:auto}
  .log div{padding:2px 0}.log .t{color:var(--faint)}
  .upzone{border:1.5px dashed var(--line);border-radius:14px;padding:18px;text-align:center;background:linear-gradient(180deg,rgba(30,215,96,.05),transparent)}
  .now-card{border:1px solid rgba(30,215,96,.4);background:linear-gradient(180deg,rgba(30,215,96,.1),transparent);border-radius:16px;padding:18px;margin-bottom:16px;display:flex;align-items:center;gap:16px}
  .now-card .big-np{width:52px;height:52px;border-radius:14px;background:var(--green);color:#04140a;display:grid;place-items:center;flex:none;animation:beat 3s infinite}
  .now-card .big-np svg{width:24px;height:24px}
  .chipset{display:flex;flex-wrap:nowrap;gap:8px;margin-bottom:16px;overflow-x:auto;padding-bottom:6px;-webkit-overflow-scrolling:touch}
  .chipset::-webkit-scrollbar{height:6px}.chipset::-webkit-scrollbar-thumb{background:#2a2e2a;border-radius:8px}
  .fchip{flex:none;white-space:nowrap;font-size:12px;font-weight:700;color:var(--muted);background:var(--panel);border:1px solid var(--line);padding:6px 12px;border-radius:999px}
  .fchip.active{background:var(--green-soft);color:var(--green);border-color:transparent}
  .ds-scroll{max-height:calc(100vh - 470px);min-height:220px;overflow:auto;border:1px solid var(--line);border-radius:12px}
  .ds-scroll table{margin:0;width:100%;border-collapse:collapse;font-size:13px}
  .ds-scroll th{position:sticky;top:0;z-index:2;background:#141613;text-align:left;color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:800;padding:12px;border-bottom:1px solid var(--line)}
  .ds-scroll td{padding:11px 12px;border-top:1px solid var(--line);white-space:nowrap}
  .ds-scroll tbody tr:hover{background:rgba(255,255,255,.03)}
  .ds-scroll .clip{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:12px}.ds-scroll .word{font-weight:700}
  .placeholder{display:grid;place-items:center;min-height:220px;text-align:center;color:var(--muted)}
  .cbadge{position:fixed;left:14px;bottom:12px;z-index:9;color:rgba(255,255,255,.4);font-size:11px;font-weight:600}
</style>"""

BG_MARKUP = """<div class="stage">
  <div class="wash"></div>
  <div class="blob g"></div><div class="blob v"></div><div class="blob b"></div>
  <div class="keys" id="keys"></div>
  <div class="grain"></div>
</div>"""

BG_SCRIPT = r"""<script>
(function(){
  var keys=document.getElementById('keys');if(keys){var NK=30,cols=['g','v','b'],lit={},n=4+Math.floor(Math.random()*3);
    while(Object.keys(lit).length<n){lit[Math.floor(Math.random()*NK)]=cols[Math.floor(Math.random()*3)];}
    for(var i=0;i<NK;i++){var nb=(i%7===2||i%7===6);var k=document.createElement('div');k.className='key'+(nb?' nb':'')+(lit[i]?(' '+lit[i]):'');if(!nb){var b=document.createElement('div');b.className='bk';k.appendChild(b);}keys.appendChild(k);}}
  var stage=document.querySelector('.stage');var gl=['♪','♫','♩','♬'];var live=0;
  setInterval(function(){if(document.hidden||live>6||!stage)return;var e=document.createElement('div');e.className='note';e.textContent=gl[Math.random()*4|0];
    e.style.left=(5+Math.random()*88)+'%';e.style.fontSize=(16+Math.random()*16)+'px';e.style.animation='rise '+(8+Math.random()*5).toFixed(1)+'s linear forwards';
    stage.appendChild(e);live++;e.addEventListener('animationend',function(){e.remove();live--;});},1500);
  var glass=document.getElementById('glass');
  if(glass){window.addEventListener('mousemove',function(ev){var x=ev.clientX/innerWidth-.5,y=ev.clientY/innerHeight-.5;
    glass.style.transform='rotateX('+(-y*4).toFixed(2)+'deg) rotateY('+(x*5).toFixed(2)+'deg)';});}
})();
</script>"""

CHART_HEAD = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'

PILLS = [
    ("overview", "/overview", "Overview"),
    ("stats", "/stats", "Stats"),
    ("tiktok", "/tiktok-candidates", "TikTok"),
    ("tracker", "/tracker", "Tracker"),
    ("experiment", "/experiment", "Experiment"),
]

BRAND_SVG = '<svg viewBox="0 0 24 24" fill="#04140a"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'
SOCIAL_HTML = (
    '<div class="social">'
    '<a href="https://www.instagram.com/dustinspiano/" target="_blank" rel="noopener" aria-label="Instagram"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1" fill="currentColor" stroke="none"/></svg></a>'
    '<a href="https://www.youtube.com/@dustin.nghiem" target="_blank" rel="noopener" aria-label="YouTube"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M23 12s0-3.6-.46-5.3a2.8 2.8 0 0 0-2-2C18.8 4.2 12 4.2 12 4.2s-6.8 0-8.54.5a2.8 2.8 0 0 0-2 2C1 8.4 1 12 1 12s0 3.6.46 5.3a2.8 2.8 0 0 0 2 2c1.74.5 8.54.5 8.54.5s6.8 0 8.54-.5a2.8 2.8 0 0 0 2-2C23 15.6 23 12 23 12zM9.8 15.3V8.7l5.7 3.3z"/></svg></a>'
    '</div>'
)


def _clean_title(raw: str, fallback: str = "") -> str:
    return (raw or "").split("#", 1)[0].strip() or fallback


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


def _cap(raw: str, fallback: str = "") -> str:
    """Clean caption (strip hashtags) and HTML-escape; keeps the leaf glyph if present."""
    return html.escape(_clean_title(raw, fallback))


def concept_chart(canvas_id: str, labels: list, data: list, colors: list) -> str:
    payload = json.dumps({"l": labels, "d": data, "c": colors})
    tmpl = r"""<script>
(function(){
  var CID='__CID__';var P=__P__;var drawn=false;
  function draw(){
    if(drawn)return;
    if(typeof Chart==='undefined'){return setTimeout(draw,60);}
    var ctx=document.getElementById(CID);if(!ctx)return;
    drawn=true;
    Chart.defaults.font.family="'Figtree',sans-serif";Chart.defaults.color='#8892a3';
    new Chart(ctx,{type:'bar',data:{labels:P.l,datasets:[{data:P.d,backgroundColor:P.c,borderRadius:6,maxBarThickness:34}]},
      options:{plugins:{legend:{display:false},tooltip:{callbacks:{label:function(x){return x.raw.toLocaleString()+' views';}}}},
        scales:{x:{grid:{display:false},ticks:{font:{size:11}}},y:{grid:{color:'rgba(255,255,255,.05)'},ticks:{callback:function(v){return v>=1000?(v/1000)+'k':v;}}}},
        responsive:true,maintainAspectRatio:false,animation:{duration:800}});
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',draw);}else{draw();}
  window.addEventListener('load',draw);
})();
</script>"""
    return tmpl.replace("__CID__", canvas_id).replace("__P__", payload)


def svg_bar_chart(labels: list, data: list, colors: list, height: int = 260) -> str:
    """Server-rendered inline SVG bar chart — always renders, no JS/CDN needed."""
    W, pad_l, pad_r, pad_t, pad_b = 720, 46, 14, 14, 32
    plot_w = W - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(data) or 1
    maxv = max(data) if data and max(data) > 0 else 1
    slot = plot_w / n
    barw = min(42, slot * 0.62)
    base = pad_t + plot_h

    parts = []
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + plot_h * (1 - frac)
        val = int(maxv * frac)
        lab = f"{val // 1000}k" if val >= 1000 else str(val)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="rgba(255,255,255,.06)"/>')
        parts.append(f'<text x="{pad_l - 7}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="#7c8595">{lab}</text>')

    for i, v in enumerate(data):
        c = colors[i] if i < len(colors) else "#1ed760"
        lb = str(labels[i]) if i < len(labels) else ""
        h = (v / maxv) * plot_h if maxv else 0
        x = pad_l + slot * i + (slot - barw) / 2
        y = base - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{barw:.1f}" height="{max(0, h):.1f}" rx="4" fill="{c}">'
            f'<title>{html.escape(lb)}: {v:,} views</title></rect>'
        )
        parts.append(
            f'<text x="{x + barw / 2:.1f}" y="{height - pad_b + 14}" text-anchor="middle" font-size="9.5" fill="#7c8595">{html.escape(lb)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {W} {height}" style="width:100%;height:auto;display:block;margin-top:14px" '
        f'preserveAspectRatio="xMidYMid meet" font-family="Figtree,sans-serif">{"".join(parts)}</svg>'
    )


def _topbar(active: str) -> str:
    pills = "".join(
        f'<a class="{"on" if (key == active or (active == "dsci" and key == "stats")) else ""}" href="{href}">{html.escape(label)}</a>'
        for key, href, label in PILLS
    )
    return (
        '<header class="bar">'
        f'<a class="backhome" href="/"><span class="dot">{BRAND_SVG}</span>Piano Shorts</a>'
        f'<nav class="pills">{pills}</nav>'
        '</header>'
    )


def render_page(active: str, eyebrow: str, title: str, sub: str, body: str,
                head_extra: str = "", top_actions: str = "") -> str:
    """Full-page shell for a tab (aurora bg + top bar + page content)."""
    topline = (
        '<div class="topline"><div>'
        f'<p class="eyebrow">{html.escape(eyebrow)}</p>'
        f'<h2 class="h2">{html.escape(title)}</h2>'
        + (f'<div class="sub">{sub}</div>' if sub else '')
        + '</div>'
        + (f'<div class="top-actions">{top_actions}</div>' if top_actions else '')
        + '</div>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Piano Shorts — {html.escape(title)}</title>'
        + FONT_HEAD + STYLE_V5 + head_extra
        + '</head><body class="tab">' + BG_MARKUP + _topbar(active)
        + '<main class="page shell">' + topline + body + '</main>'
        + '<div class="cbadge">Piano Shorts · Creator Analytics</div>'
        + BG_SCRIPT + '</body></html>'
    )


# ---------------------------------------------------------------------------
# HOME  (/)
# ---------------------------------------------------------------------------
def render_dashboard() -> str:
    """Home splash — choose a view."""
    latest_rows = latest_video_stats()
    total_views = sum(parse_stat_int(r.get("view_count", "")) for r in latest_rows)
    tracked = len(latest_rows)
    week = experiment_week()

    tiles = [
        ("t1", "/overview", "Overview", "Mission control",
         '<rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/>', False),
        ("t2", "/stats", "YouTube Stats", "Growth &amp; queue log", None, True),
        ("t3", "/tiktok-candidates", "TikTok Candidates", "Top clips to repost",
         '<path d="M9 18V5l3-1v10"/><circle cx="6" cy="18" r="3"/><path d="M14 7c1.5 2 4 2.5 6 2.5"/>', False),
        ("t4", "/tracker", "Video Tracker", "Every clip logged",
         '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="20" x2="9" y2="9"/>', False),
        ("t5", "/experiment", "12-Week Experiment", "Data-science project",
         '<circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/>', False),
    ]
    tile_html = []
    for cls, href, label, small, svg, is_eq in tiles:
        if is_eq:
            icon = '<svg class="eq" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="9" width="3.4" height="12" rx="1"/><rect x="8.6" y="5" width="3.4" height="16" rx="1"/><rect x="14.2" y="11" width="3.4" height="10" rx="1"/><rect x="18" y="7" width="3.4" height="14" rx="1"/></svg>'
        else:
            icon = f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">{svg}</svg>'
        tile_html.append(
            f'<a class="tile {cls}" href="{href}"><span class="go">→</span>'
            f'<div class="ic">{icon}</div><b>{label}</b><small>{small}</small></a>'
        )

    body = (
        '<div class="home">'
        f'<div class="mark">{BRAND_SVG}</div>'
        '<h1 class="big">Piano Shorts</h1>'
        '<div class="kicker">Choose a view</div>'
        f'<div class="stat-line"><b>{total_views:,}</b> views · <b>{tracked:,}</b> clips tracked · week <b>{week}</b> of {config.PROJECT_TOTAL_WEEKS}</div>'
        f'<div class="glass" id="glass"><div class="grid5">{"".join(tile_html)}</div></div>'
        + SOCIAL_HTML + '</div>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Piano Shorts</title>' + FONT_HEAD + STYLE_V5
        + '</head><body>' + BG_MARKUP + body + BG_SCRIPT + '</body></html>'
    )


# ---------------------------------------------------------------------------
# OVERVIEW  (/overview)
# ---------------------------------------------------------------------------
def render_overview() -> str:
    status = build_status()
    latest_rows = latest_video_stats()
    total_views = sum(parse_stat_int(r.get("view_count", "")) for r in latest_rows)
    hours = best_posting_hours()
    best_hour = str(hours[0]["hour"]) if hours else "—"
    delta = _today_views_delta()
    running = bool(status["run"]["running"])
    input_count = int(status["input_count"])
    clip_count = int(status["clip_count"])
    uploaded_today = _uploaded_today_count()

    now = datetime.now(ZoneInfo(config.TIMEZONE))
    future = sorted(
        [r for r in build_queue_rows() if (p := parse_iso_datetime(r.get("scheduled_publish_time", ""))) and p > now],
        key=lambda r: parse_iso_datetime(r.get("scheduled_publish_time", "")),
    )
    next_post = html.escape(future[0]["display_time"]) if future else "None scheduled"

    rows1d = chart_view_gains("1d")
    labels = [str(r.get("label", "")) for r in rows1d]
    data = [int(r.get("views", 0)) for r in rows1d]
    colors = ["#8b6cff" if r.get("fill") else "#1ed760" for r in rows1d]
    chart_svg = svg_bar_chart(labels, data, colors)

    log_lines = live_dashboard_log_lines(6)
    log_html = "".join(
        f'<div><span class="t">{html.escape(l[:9])}</span> {html.escape(l[9:180])}</div>'
        for l in reversed(log_lines[-6:])
    ) or '<div class="t">No dashboard activity yet.</div>'

    body = (
        '<div class="row r-2 mt">'
        '<div class="panel"><p class="eyebrow" style="color:var(--muted)">Total channel views</p>'
        f'<div class="hero-num">{total_views:,}</div>'
        f'<div class="sub" style="margin-top:8px">+<b style="color:var(--green)">{delta:,}</b> overnight · best hour <b style="color:var(--green)">{html.escape(best_hour)}</b> · today’s gains below</div>'
        f'{chart_svg}</div>'
        '<div class="panel"><h3>Live activity</h3>'
        '<div class="now-card"><div class="big-np"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></div>'
        f'<div><b style="font-size:15px">{"Pipeline running…" if running else "Pipeline idle · ready"}</b><div class="sub" style="margin-top:2px">Drop videos in input, then Clip + Upload</div></div></div>'
        '<form class="upzone" action="/upload" method="post" enctype="multipart/form-data" data-owner-only>'
        '<input type="hidden" name="upload_action" value="upload_and_clip">'
        '<input type="file" name="videos" accept=".mp4,.mov" multiple style="color:var(--muted);font-size:12px;margin-bottom:10px"><br>'
        '<button class="btn primary" type="submit">Upload &amp; clip</button></form>'
        f'<div class="log" data-owner-only>{log_html}</div></div>'
        '</div>'
        '<div class="row r-4 mt">'
        f'<div class="chip"><div class="l">Input videos</div><div class="v">{input_count}</div></div>'
        f'<div class="chip"><div class="l">Clips ready</div><div class="v">{clip_count:,}</div></div>'
        f'<div class="chip"><div class="l">Next post</div><div class="v" style="font-size:15px">{next_post}</div></div>'
        f'<div class="chip"><div class="l">Uploaded today</div><div class="v">{uploaded_today}</div></div>'
        '</div>'
    )
    ready = '<span class="pill ready"><span class="d"></span>' + ("Running" if running else "Ready") + '</span>'
    top_actions = ready + (
        '<form class="inline" action="/run" method="post" data-owner-only>'
        '<button class="btn primary" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>Clip + Upload</button></form>'
        '<form class="inline" action="/refresh-stats" method="post" data-owner-only><input type="hidden" name="redirect_to" value="/overview">'
        '<button class="btn" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/></svg>Refresh</button></form>'
    )
    return render_page(
        "overview", "Mission Control", "Overview",
        "Your whole Shorts studio at a glance.", body,
        top_actions=top_actions,
    )


# ---------------------------------------------------------------------------
# YOUTUBE STATS  (/stats)
# ---------------------------------------------------------------------------
def render_stats_page(selected_range: str = "1d", *_ignore, **_kw) -> str:
    latest_rows = latest_video_stats()
    total_views = sum(parse_stat_int(r.get("view_count", "")) for r in latest_rows)
    total_likes = sum(parse_stat_int(r.get("like_count", "")) for r in latest_rows)
    total_comments = sum(parse_stat_int(r.get("comment_count", "")) for r in latest_rows)
    engagement = ((total_likes + total_comments) / total_views * 100) if total_views else 0.0
    tracked = len(latest_rows)
    delta = _today_views_delta()
    uploads = count_upload_records()
    clips_ready = int(build_status()["clip_count"])
    uploaded_today = _uploaded_today_count()

    rows1w = chart_view_gains("1w")
    labels = [str(r.get("label", "")) for r in rows1w]
    data = [int(r.get("views", 0)) for r in rows1w]
    colors = ["#1ed760"] * len(data)
    chart_svg = svg_bar_chart(labels, data, colors)

    def kpi(cls, lab, val, delta_txt, dcolor=""):
        style = f' style="color:{dcolor}"' if dcolor else ""
        return f'<div class="kpi {cls}"><div class="lab">{lab}</div><div class="val">{val}</div><div class="d"{style}>{delta_txt}</div></div>'

    kpis = (
        '<div class="row r-4 mt">'
        + kpi("kc-g", "Total Views", f"{total_views:,}", f"▲ {delta:,} today")
        + kpi("kc-b", "Tracked Clips", f"{tracked:,}", f"{clips_ready:,} clips ready")
        + kpi("kc-t", "Upload Records", f"{uploads:,}", f"▲ {uploaded_today} today")
        + kpi("kc-a", "Engagement", f"{engagement:.2f}%", f"{total_likes:,} likes · {total_comments:,} comments", "var(--faint)")
        + '</div>'
    )

    # top performers
    ranked = sorted(latest_rows, key=lambda r: parse_stat_int(r.get("view_count", "")), reverse=True)[:6]
    top_rows = []
    for i, r in enumerate(ranked, 1):
        cap = _cap(r.get("title", ""), r.get("clip_filename", ""))
        top_rows.append(
            f'<div class="lead"><div class="rk">{i}</div><div class="nm"><b>{cap}</b><small>{html.escape(r.get("clip_filename", ""))}</small></div>'
            f'<div class="nu"><b>{parse_stat_int(r.get("view_count", "")):,}</b><small>{parse_stat_int(r.get("like_count", "")):,} likes</small></div></div>'
        )
    top_html = "".join(top_rows) or '<p class="sub">No ranked videos yet.</p>'

    # piano-key hours
    hours = best_posting_hours()[:8]
    maxv = max((float(h["average_views"]) for h in hours), default=1) or 1
    pk = "".join(
        f'<div class="pk {"best" if i == 0 else ""}"><div class="v">{float(h["average_views"]):,.0f}</div>'
        f'<div class="col" style="height:{max(6, round(float(h["average_views"]) / maxv * 100))}%"></div><div class="h">{html.escape(str(h["hour"]))}</div></div>'
        for i, h in enumerate(hours)
    )

    # upcoming queue log
    from collections import Counter
    qrows = build_queue_rows()
    counts = Counter(queue_status(r.get("status", ""), r.get("scheduled_publish_time", "")) for r in qrows)
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    upcoming = sorted(
        [r for r in qrows if (p := parse_iso_datetime(r.get("scheduled_publish_time", ""))) and p > now],
        key=lambda r: parse_iso_datetime(r.get("scheduled_publish_time", "")),
    )[:8]
    play = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'
    qlog_rows = []
    for i, r in enumerate(upcoming):
        np = play if i == 0 else f'<b style="font-size:13px">{i + 1}</b>'
        cap = _cap(r.get("title", ""), r.get("clip_filename", ""))
        qlog_rows.append(
            f'<div class="set"><div class="np">{np}</div>'
            f'<div class="mid"><b>{cap}</b><small>{html.escape(r.get("clip_filename", ""))}</small></div>'
            f'<div class="time">{html.escape(r.get("display_time", ""))}</div><span class="badge sch">Scheduled</span></div>'
        )
    qlog = "".join(qlog_rows) or '<p class="sub">Nothing scheduled ahead.</p>'

    ds_btn = '<a class="btn" href="/data-science"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Data-Science Tracker</a>'

    body = (
        kpis
        + '<div class="row r-2 mt">'
        f'<div class="panel"><h3>Views gained · last 7 days</h3>{chart_svg}</div>'
        f'<div class="panel"><h3>Top performers</h3>{top_html}</div>'
        '</div>'
        f'<div class="panel mt"><h3>Best posting hours <span style="color:var(--faint);font-weight:600;font-size:12px">· taller key = more avg views</span></h3><div class="pk-wrap">{pk}</div></div>'
        '<div class="panel mt"><div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px">'
        '<h3 style="margin:0">Upcoming queue <span style="color:var(--faint);font-weight:600;font-size:12px">· next tracks cued to post</span></h3>'
        f'<span style="color:var(--muted);font-size:12.5px;font-weight:700"><span style="color:var(--green)">{counts.get("uploaded", 0)}</span> uploaded · <span style="color:var(--blue)">{counts.get("scheduled", 0)}</span> scheduled</span></div>'
        f'{qlog}</div>'
    )
    top_actions = ds_btn + (
        '<form class="inline" action="/refresh-stats" method="post" data-owner-only><input type="hidden" name="redirect_to" value="/stats">'
        '<button class="btn primary" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/></svg>Refresh Stats</button></form>'
    )
    return render_page(
        "stats", "Performance Center", "YouTube Stats",
        "Where and when your clips actually land.", body,
        top_actions=top_actions,
    )


# ---------------------------------------------------------------------------
# TIKTOK CANDIDATES  (/tiktok-candidates)
# ---------------------------------------------------------------------------
def render_tiktok_candidates_page(selected_date: str = "") -> str:
    _ = selected_date
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    medal = ["\U0001f947", "\U0001f948", "\U0001f949"]
    cls = ["gold", "silver", "bronze"]
    blocks = []
    for day in tiktok_candidate_days():
        try:
            d = datetime.fromisoformat(str(day["date"])).date()
        except ValueError:
            continue
        if (today - d).days > 6:
            continue
        cand = list(day["candidates"])[:3]
        pods = []
        for i, c in enumerate(cand):
            title = _cap(str(c["title"]), str(c["clip_filename"]))
            pods.append(
                f'<div class="pod {cls[i]}"><div class="medal">{medal[i]}</div><div class="w">{title}</div>'
                f'<div class="c">{html.escape(str(c["clip_filename"]))}</div>'
                f'<div class="met"><b>{int(c["views"]):,}</b> views · <b>{int(c["likes"]):,}</b> likes</div></div>'
            )
        while len(pods) < 3:
            pods.append('<div class="pod"><div class="met sub">—</div></div>')
        ordered = pods[1] + pods[0] + pods[2]
        label = "Today · " + format_stats_date(str(day["date"])) if d == today else format_stats_date(str(day["date"]))
        blocks.append(f'<div class="day-block"><h4>{html.escape(label)}</h4><div class="podium">{ordered}</div></div>')

    body = "".join(blocks) or '<div class="panel"><div class="placeholder"><h3>No candidates in the past week</h3><div>Once clips have 24h of stats they appear here.</div></div></div>'
    top_actions = '<a class="btn primary" href="/tiktok-candidates.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>Download CSV of all weeks</a>'
    return render_page(
        "tiktok", "YouTube winners → TikTok", "TikTok Candidates",
        "The daily podium of clips worth reposting.", body, top_actions=top_actions,
    )


# ---------------------------------------------------------------------------
# VIDEO TRACKER  (/tracker)
# ---------------------------------------------------------------------------
def _recent_tracker_clips(limit: int = 14) -> list:
    rows = [r for r in build_queue_rows() if r.get("youtube_video_id") and r.get("status") == "uploaded"]
    rows.sort(key=lambda r: r.get("upload_time", ""), reverse=True)
    views = {r.get("youtube_video_id", ""): parse_stat_int(r.get("view_count", "")) for r in latest_video_stats()}
    out = []
    for r in rows[:limit]:
        vid = r.get("youtube_video_id", "")
        out.append({
            "caption": _clean_title(r.get("title", ""), r.get("clip_filename", "")),
            "clip": r.get("clip_filename", ""), "vid": vid, "views": views.get(vid, 0),
        })
    return out


def render_tracker_page() -> str:
    clips = _recent_tracker_clips(14)
    project = summarize_project_dataset(read_project_dataset())
    cards = "".join(
        f'<a class="alb" href="https://www.youtube.com/shorts/{html.escape(c["vid"])}" target="_blank" rel="noopener">'
        f'<div class="cover" style="background-image:url(\'https://i.ytimg.com/vi/{html.escape(c["vid"])}/mqdefault.jpg\')">'
        '<span class="play"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></span></div>'
        f'<div class="meta"><b>{html.escape(c["caption"])}</b><small>{html.escape(c["clip"])} · {c["views"]:,} views</small></div></a>'
        for c in clips
    ) or '<p class="sub">No uploaded clips yet.</p>'

    body = (
        '<div class="row r-4 mt" style="grid-template-columns:repeat(3,1fr)">'
        f'<div class="chip"><div class="l">Clips in library</div><div class="v">{int(build_status()["clip_count"]):,}</div></div>'
        f'<div class="chip"><div class="l">Tracked on YouTube</div><div class="v">{len(latest_video_stats()):,}</div></div>'
        f'<div class="chip"><div class="l">With 24h data</div><div class="v">{project.get("completed_24h", 0):,}</div></div>'
        '</div>'
        '<div class="panel mt"><h3>Recent clips <span style="color:var(--faint);font-weight:600;font-size:12px">· latest 14 · click a cover to watch on YouTube</span></h3>'
        f'<div class="lib">{cards}</div></div>'
    )
    top_actions = (
        '<a class="dlbtn" href="/tracker.csv" data-owner-only><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>Download tracker CSV</a>'
    )
    return render_page(
        "tracker", "Automation records", "Video Tracker · Library",
        "Every clip as an album — click any cover to open it on YouTube.", body, top_actions=top_actions,
    )


# ---------------------------------------------------------------------------
# 12-WEEK EXPERIMENT  (/experiment)
# ---------------------------------------------------------------------------
EXPERIMENT_PHASES = [
    {"wk": "Wk 1–2", "lo": 1, "hi": 2, "t": "Baseline",
     "d": "Establish per-clip 24h view & engagement rates",
     "goal": "Build a clean control set so every later test has a benchmark.",
     "method": "Record 24h views, likes, comments per clip; compute engagement = (likes+comments)/views. Summarize mean &amp; variance and the distribution of 24h views.",
     "tags": ["Descriptive stats", "24h view distribution", "Control baseline"]},
    {"wk": "Wk 3–4", "lo": 3, "hi": 4, "t": "Posting-time test",
     "d": "9 AM–3 PM vs 4 PM–9 PM windows",
     "goal": "Find whether morning/afternoon or evening posting drives more 24h views.",
     "method": "Randomly assign clips to each window to remove content bias. Compare mean 24h views with a two-sample t-test (Mann-Whitney if skewed); report effect size + 95% CI.",
     "tags": ["A/B test", "t-test", "Effect size", "Confidence interval"]},
    {"wk": "Wk 5–6", "lo": 5, "hi": 6, "t": "Caption test",
     "d": "Emotional vs energy caption words",
     "goal": "Test if emotional (PEACE, SERENE) vs energy (FLOW, VIBE) captions change reach.",
     "method": "Balance caption family across posting times. Compare group means; fit a logistic regression on a high-performer flag controlling for hour.",
     "tags": ["A/B test", "Logistic regression", "Confounder control"]},
    {"wk": "Wk 7–8", "lo": 7, "hi": 8, "t": "Frequency test",
     "d": "1 / hour vs 2× in best windows",
     "goal": "See if denser posting in peak windows lifts total views without cannibalizing per-clip views.",
     "method": "Compare per-day total views and per-clip views between cadences. Paired comparison across matched days; watch diminishing returns.",
     "tags": ["Paired test", "Cannibalization check", "Per-day totals"]},
    {"wk": "Wk 9–12", "lo": 9, "hi": 12, "t": "Content type + capstone",
     "d": "Solo / church / jazz · predictive model",
     "goal": "Predict which clips will perform from their features, and write the capstone.",
     "method": "Tag clips by type, shot, orientation. Fit a gradient-boosted model to predict high performers; report feature importance and a final writeup.",
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
        return ("done", "Done")
    if phase["lo"] <= wk <= phase["hi"]:
        return ("live", "Live")
    return ("next", "Next")


def render_experiment_page() -> str:
    wk = experiment_week()
    total = config.PROJECT_TOTAL_WEEKS
    octave = "".join(
        f'<div class="wkey {"done" if w < wk else ("live" if w == wk else "")}"><span class="n">{w}</span></div>'
        for w in range(1, total + 1)
    )
    pct = min(100, round(wk / total * 100))
    phase_html = []
    for p in EXPERIMENT_PHASES:
        scls, slbl = experiment_status(p)
        tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in p["tags"])
        open_attr = "open" if scls == "live" else ""
        phase_html.append(
            f'<details class="phase" {open_attr}>'
            f'<summary><span class="wk">{p["wk"]}</span><div class="pt"><b>{html.escape(p["t"])}</b><small>{p["d"]}</small></div>'
            f'<span class="st {scls}">{slbl}</span></summary>'
            f'<div class="body"><span class="l">Goal</span>{p["goal"]}<span class="l">How we analyze</span>{p["method"]}'
            f'<div>{tags}</div></div></details>'
        )
    phases = "".join(phase_html)
    body = (
        '<div class="panel mt"><h3>The octave <span style="color:var(--faint);font-weight:600;font-size:12px">· each key is a week</span></h3>'
        f'<div class="octave">{octave}</div><div class="prog"><i style="width:{pct}%"></i></div>'
        f'<div class="sub">Week {wk} of {total} · primary metric: 24h views</div></div>'
        f'<div class="mt">{phases}</div>'
    )
    return render_page(
        "experiment", "Data-Science Project", "12-Week Experiment",
        f"Twelve weeks, twelve keys — currently on week {wk}.", body,
    )


# ---------------------------------------------------------------------------
# DATA-SCIENCE TRACKER  (/data-science)  — scrollable full dataset
# ---------------------------------------------------------------------------
def render_data_science_page(selected_week: str = "", selected_sort: str = "recent",
                             stats_page: int = 1, selected_range: str = "1d") -> str:
    section = render_project_tracker_section(selected_week, selected_sort, stats_page, selected_range)
    back = '<a class="btn" href="/stats"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg>Back to Stats</a>'
    return render_page(
        "dsci", "Performance Center · Experiment data", "Data-Science Tracker",
        "Every clip tagged for the 12-week experiment — filter by week, sort, and export.",
        section, top_actions=back,
    )


def render_project_tracker_section(selected_week: str, selected_sort: str,
                                   stats_page: int, selected_range: str,
                                   base_path: str = "/data-science") -> str:
    rows = read_project_dataset()
    show_all = str(selected_week).strip().lower() == "all"
    if not show_all:
        selected_week = normalize_project_week(rows, selected_week)
    selected_sort = normalize_project_sort(selected_sort)
    summary = summarize_project_dataset(rows)

    def week_href(token: str) -> str:
        return f'{base_path}?project_week={token.replace(" ", "%20")}&project_sort={selected_sort}'

    weeks = planned_project_weeks()
    chips = ['<div class="chipset">']
    chips.append(f'<a class="{"fchip active" if show_all else "fchip"}" href="{week_href("all")}">All weeks</a>')
    for wk in weeks:
        active = "fchip active" if (not show_all and wk == selected_week) else "fchip"
        chips.append(f'<a class="{active}" href="{week_href(wk)}">{html.escape(wk)}</a>')
    chips.append('</div>')
    chips = "".join(chips)

    if show_all:
        filtered = list(rows)
    else:
        filtered = [r for r in rows if r.get("project_week") == selected_week] if selected_week else rows
    visible = sort_project_rows(filtered, selected_sort)
    total = len(visible)

    columns = [
        ("Week", "project_week", "plain"), ("Clip", "clip_id", "clip"), ("Platform", "platform", "plain"),
        ("Caption", "caption_word", "word"), ("Style", "caption_style", "plain"), ("Group", "posting_time_group", "plain"),
        ("Length", "clip_length_seconds", "secs"), ("Orientation", "video_orientation", "plain"),
        ("Content Type", "content_type", "plain"), ("Live Views", "current_views", "int"),
        ("Live Like Rate", "current_like_rate", "rate"), ("24h Views", "views_24h", "int"),
        ("24h Like Rate", "like_rate_24h", "rate"), ("High Performer", "high_performing", "flag"),
        ("Last Updated", "last_checked_at", "time"),
    ]

    def fmt(kind, value):
        raw = "" if value is None else str(value).strip()
        if raw == "":
            return "—"
        if kind == "int":
            try:
                return f"{int(float(raw)):,}"
            except ValueError:
                return html.escape(raw)
        if kind == "rate":
            try:
                return f"{float(raw) * 100:.2f}%"
            except ValueError:
                return html.escape(raw)
        if kind == "secs":
            try:
                return f"{float(raw):.0f}s"
            except ValueError:
                return html.escape(raw)
        if kind == "flag":
            return "★ Yes" if raw in ("1", "1.0", "True", "true") else "No"
        if kind == "time":
            parsed = parse_iso_datetime(raw)
            return parsed.strftime("%b %-d, %-I:%M %p") if parsed else html.escape(raw[:16])
        if kind == "word":
            return html.escape(raw[:22])
        return html.escape(raw)

    trs = []
    for r in visible:
        cells = []
        for _lab, key, kind in columns:
            css = ' class="clip"' if kind == "clip" else (' class="word"' if kind == "word" else "")
            cells.append(f"<td{css}>{fmt(kind, r.get(key, ''))}</td>")
        trs.append("<tr>" + "".join(cells) + "</tr>")
    tbody = "".join(trs) or f'<tr><td colspan="{len(columns)}" class="sub">No rows for this week yet.</td></tr>'
    thead = "".join(f"<th>{html.escape(lab)}</th>" for lab, _k, _kd in columns)
    scope = "all weeks" if show_all else html.escape(str(selected_week))

    downloads = (
        '<div class="top-actions" data-owner-only style="margin-bottom:14px">'
        '<a class="btn" href="/project-data.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>CSV</a>'
        '<a class="btn" href="/project-data.xlsx"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>XLSX</a></div>'
    )
    return (
        '<div class="panel mt"><div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">'
        '<h3 style="margin:0">Data-Science Tracker</h3>'
        f'<span class="sub" style="margin:0">{total:,} rows · {scope} · scroll to see all</span></div>'
        + downloads + chips
        + f'<div class="ds-scroll"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div></div>'
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


def queue_csv() -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["clip_filename", "youtube_video_id", "caption", "scheduled_slot", "status"])
    for r in sort_queue_rows(build_queue_rows(), "oldest"):
        writer.writerow([
            r.get("clip_filename", ""), r.get("youtube_video_id", ""),
            _clean_title(r.get("title", ""), r.get("clip_filename", "")),
            r.get("display_time", ""), queue_status(r.get("status", ""), r.get("scheduled_publish_time", "")),
        ])
    return buf.getvalue()


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
