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
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo

import config
import tiktok_publish as tiktok
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
from youtube_upload import (
    get_youtube_service,
    read_stale_deferred_filenames,
    read_upload_attempted_filenames,
    read_upload_records,
)
from scheduler import generate_schedule
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

# Shared <head> markup. The UI uses the Apple system font stack (SF Pro on
# macOS/iOS — the same face apple.com renders with), so no webfont is loaded.
FONT_HEAD = '<link rel="icon" href="/favicon.ico" type="image/svg+xml">'

# Injected for non-owner visitors: hides every action form (read-only view) and
# shows an "Owner login" button. Server-side auth on POST is the real guard; this
# is just the matching UI so visitors don't see controls that wouldn't work.
VIEWER_MODE_SNIPPET = """
<style id="viewer-mode">
  form[action="/run"], form[action="/clip-only"], form[action="/upload"],
  form[action="/stop"], form[action="/refresh-stats"], form[action="/queue/privacy"],
  form[action="/tiktok-schedule"], form[action="/tiktok/post"], [data-owner-only] { display: none !important; }
  .owner-login-badge {
    position: fixed; right: 16px; bottom: 16px; z-index: 99999;
    background: #4ade80; color: #05140b;
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
    # Key and rows live in ONE tuple so a concurrent reader can never observe a
    # half-written cache (the old two-slot layout raced with the background
    # snapshot thread on startup: key set, rows not yet parsed -> KeyError).
    entry = _STATS_HISTORY_CACHE.get("entry")
    if entry is None or entry[0] != key:
        entry = (key, _stats_tracker._uncached_read_stats_history())
        _STATS_HISTORY_CACHE["entry"] = entry
    return entry[1]  # type: ignore[return-value]


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
# How stale saved stats must be before the /stats page triggers a full, heavy
# rebuild (tracker CSV + project Excel) on load. Kept high because the cheap
# background snapshotter already keeps the view history fresh every 30 min, so
# the expensive rebuild only needs to run occasionally (or on manual Refresh).
AUTO_STATS_REFRESH_MINUTES = 45
# Apple-website font stack: renders SF Pro on Apple devices, falls back cleanly.
APP_FONT_STACK = (
    '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", '
    '"Helvetica Neue", Helvetica, Arial, sans-serif'
)
RUN_STATE = {
    "running": False,          # the clip + upload pipeline is actually running
    "stats_running": False,    # a stats-only refresh is running (not the pipeline)
    "started_at": "",
    "finished_at": "",
    "last_output": "",
    "last_error": "",
    # Live run progress (extended for the /api/status live updates):
    "phase": "",               # "" | "starting" | "clipping" | "uploading" | "finishing"
    "done": 0,                 # uploads attempted so far in this run
    "total": 0,                # uploads expected this run (0 = unknown / clip-only)
    "clips_made": 0,           # clips generated so far in this run
    "fails": 0,                # failed uploads so far in this run
    "stopping": False,         # a Stop was requested and is being honored
    "stopped": False,          # the last run ended because the owner pressed Stop
    "quota_hit": False,        # the last/current run hit the YouTube daily limit
    "run_seq": 0,              # increments when a run/refresh finishes (client change detection)
}
# Handle to the live pipeline subprocess so /stop can terminate it gracefully.
PIPELINE_PROCESS: subprocess.Popen | None = None
PIPELINE_PROCESS_LOCK = threading.Lock()
# Wall-clock time the uploading phase started (for the ~time-left estimate).
UPLOAD_PHASE_STARTED_AT: float = 0.0
# YouTube's practical daily upload cap for this channel (surface only; override
# with YOUTUBE_DAILY_UPLOAD_CAP in .env if your channel's cap differs).
YOUTUBE_DAILY_UPLOAD_CAP = max(1, int(os.getenv("YOUTUBE_DAILY_UPLOAD_CAP", "20") or "20"))
STATS_REFRESH_LOCK = threading.Lock()
# Separate lock for the lightweight background snapshotter so it never holds the
# STATS_REFRESH_LOCK that the clip/upload pipeline checks — otherwise a 30-min
# background snapshot could silently swallow a "Clip + Upload" click.
SNAPSHOT_LOCK = threading.Lock()
LAST_AUTO_STATS_REFRESH_ATTEMPT: datetime | None = None

# TikTok OAuth CSRF state + PKCE verifier + last post result (shown on the page).
TIKTOK_OAUTH_STATE = ""
TIKTOK_CODE_VERIFIER = ""
TIKTOK_RESULT = {"message": "", "error": ""}


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

        if path in ("/favicon.ico", "/favicon.svg"):
            self.send_favicon()
            return

        if path == "/":
            self.send_html(viewer_mode_html(render_dashboard(), owner))
            return

        if path == "/api/status":
            self.send_json(build_status(include_private=owner))
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

        if path == "/tiktok/connect":
            if not owner:
                self._send_unauthorized()
                return
            global TIKTOK_OAUTH_STATE, TIKTOK_CODE_VERIFIER
            if not tiktok.is_configured():
                TIKTOK_RESULT.update({"error": "TikTok is not configured. Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env.", "message": ""})
                self.redirect("/tiktok-candidates")
                return
            TIKTOK_OAUTH_STATE = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
            TIKTOK_CODE_VERIFIER = tiktok.make_code_verifier()
            self.redirect(tiktok.authorize_url(TIKTOK_OAUTH_STATE, TIKTOK_CODE_VERIFIER))
            return

        if path == "/tiktok/callback":
            query = parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            err = query.get("error", [""])[0]
            if err:
                TIKTOK_RESULT.update({"error": f"TikTok authorization was declined ({err}).", "message": ""})
            elif not code or state != TIKTOK_OAUTH_STATE or not TIKTOK_OAUTH_STATE:
                TIKTOK_RESULT.update({"error": "TikTok login could not be verified (state mismatch). Please try again.", "message": ""})
            else:
                try:
                    tiktok.exchange_code(code, TIKTOK_CODE_VERIFIER)
                    TIKTOK_RESULT.update({"message": "TikTok account connected.", "error": ""})
                except Exception as exc:  # noqa: BLE001
                    logger.exception("TikTok token exchange failed: %s", exc)
                    TIKTOK_RESULT.update({"error": f"Could not complete TikTok login: {exc}", "message": ""})
            self.redirect("/tiktok-candidates")
            return

        if path == "/tiktok/disconnect":
            if not owner:
                self._send_unauthorized()
                return
            tiktok.clear_token()
            TIKTOK_RESULT.update({"message": "TikTok account disconnected.", "error": ""})
            self.redirect("/tiktok-candidates")
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
            self.redirect(self.form_redirect_target(default="/overview"))
            return

        if parsed.path == "/clip-only":
            start_clip_only()
            if self.is_ajax_request():
                self.send_json({"ok": True, "message": "Started clipping input videos."})
                return
            self.redirect(self.redirect_back_path(default="/"))
            return

        if parsed.path == "/stop":
            stopped = stop_pipeline()
            if self.is_ajax_request():
                self.send_json({"ok": True, "stopping": stopped})
                return
            self.redirect(self.form_redirect_target(default="/overview"))
            return

        if parsed.path == "/refresh-stats":
            redirect_target = self.form_redirect_target(default=self.redirect_back_path(default="/stats"))
            start_stats_refresh()
            if self.is_ajax_request():
                self.send_json({"ok": True, "message": "Stats refresh started."})
                return
            self.redirect(redirect_target)
            return

        if parsed.path == "/tiktok/post":
            self.handle_tiktok_post()
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

    def handle_tiktok_post(self) -> None:
        """Direct-post one clip to the connected TikTok account."""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(raw_body)
        clip_filename = Path((form.get("clip_filename", [""])[0] or "").strip()).name
        caption = (form.get("caption", [""])[0] or "").strip() or "Piano clip"

        if not tiktok.is_connected():
            TIKTOK_RESULT.update({"error": "Connect a TikTok account first.", "message": ""})
            self.redirect("/tiktok-candidates")
            return

        clip_path = config.CLIPS_DIR / clip_filename
        if not clip_filename or not clip_path.exists():
            TIKTOK_RESULT.update({"error": f"Clip not found: {clip_filename}", "message": ""})
            self.redirect("/tiktok-candidates")
            return

        try:
            _ = caption  # caption is set by the creator in the TikTok app for drafts
            tiktok.upload_video_draft(clip_path)
            TIKTOK_RESULT.update({
                "message": f"Sent {clip_filename} to your TikTok inbox as a draft. "
                           "Open the TikTok app, tap the notification, add your caption and post it when ready.",
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("TikTok upload failed: %s", exc)
            TIKTOK_RESULT.update({"error": f"TikTok upload failed: {exc}", "message": ""})

        self.redirect("/tiktok-candidates")

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

    def send_favicon(self) -> None:
        """Serve a small green piano tab icon (SVG) so browsers don't 404 on /favicon.ico."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="8" fill="#4ade80"/>'
            '<path d="M13 21.5V11l8-1.4v9.4" fill="none" stroke="#04140a" stroke-width="2.2" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="11" cy="21.5" r="2.4" fill="#04140a"/>'
            '<circle cx="19" cy="20.1" r="2.4" fill="#04140a"/></svg>'
        )
        encoded = svg.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(encoded)

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
    # Single-entry tuple for the same thread-safety reason as the stats cache.
    entry = _QUEUE_ROWS_CACHE.get("entry")
    if entry is None or entry[0] != key:
        entry = (key, _build_queue_rows_uncached())
        _QUEUE_ROWS_CACHE["entry"] = entry
    return entry[1]  # type: ignore[return-value]


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


def build_status(include_private: bool = False) -> dict[str, object]:
    """Build dashboard status counts (plus live-run detail for /api/status).

    include_private adds owner-only extras (log lines, last-run summary).
    """
    run = RUN_STATE.copy()
    run["eta_seconds"] = estimate_run_eta_seconds()
    payload: dict[str, object] = {
        "input_count": len(list_input_videos()),
        "clip_count": len(list_clip_files()),
        "uploaded_sources": len(list_uploaded_sources()),
        "upload_records": count_upload_records(),
        "run": run,
        "pending_uploads": len(count_pending_clips()),
        "uploaded_today": _uploaded_today_count(),
        "total_views": sum(parse_stat_int(r.get("view_count", "")) for r in latest_video_stats()),
        "today_views": _today_views_delta(),
        "next_post": next_scheduled_post_display(),
        "quota": quota_status(),
    }
    if include_private:
        payload["log"] = live_dashboard_log_lines(140)
        payload["last_run"] = last_run_summary()
    return payload


def count_pending_clips() -> list[str]:
    """Clip filenames a full run would upload (same exclusions as main.py)."""
    try:
        skip = read_upload_attempted_filenames() | read_stale_deferred_filenames()
    except Exception:  # noqa: BLE001 - a malformed log line must not break status
        skip = set()
    return sorted(p.name for p in config.CLIPS_DIR.glob("clip_*.mp4") if p.name not in skip)


def next_scheduled_post_display() -> str:
    """Display string for the next future scheduled publish slot."""
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    future = [
        p for r in build_queue_rows()
        if (p := parse_iso_datetime(r.get("scheduled_publish_time", ""))) and p > now
    ]
    return format_queue_time(min(future).isoformat()) if future else "None scheduled"


def estimate_run_eta_seconds() -> int:
    """Rough seconds remaining for the uploading phase (0 = unknown)."""
    if not RUN_STATE["running"] or RUN_STATE["phase"] != "uploading":
        return 0
    done = int(RUN_STATE["done"])
    total = int(RUN_STATE["total"])
    if done <= 0 or total <= done or not UPLOAD_PHASE_STARTED_AT:
        return 0
    per_clip = (time.time() - UPLOAD_PHASE_STARTED_AT) / done
    return int(per_clip * (total - done))


def quota_status() -> dict[str, object]:
    """Today's YouTube upload usage vs the known daily cap."""
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    used = 0
    deferred_today = False
    if config.UPLOAD_LOG_FILE.exists():
        with config.UPLOAD_LOG_FILE.open("r", newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                parsed = parse_iso_datetime(row.get("upload_time", ""))
                if not parsed:
                    continue
                if parsed.astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat() != today:
                    continue
                status = row.get("status", "")
                if status == "uploaded":
                    used += 1
                elif status == "deferred_quota":
                    deferred_today = True
    cap = YOUTUBE_DAILY_UPLOAD_CAP
    exhausted = deferred_today or bool(RUN_STATE.get("quota_hit")) or used >= cap
    return {
        "used": used,
        "cap": cap,
        "remaining": max(0, cap - used),
        "exhausted": exhausted,
    }


_RUN_SUMMARY_RE = re.compile(
    r"RunSummary\(videos_found=(\d+), clips_generated=(\d+), clips_considered_for_upload=(\d+), "
    r"uploads_completed=(\d+), uploads_skipped=(\d+), upload_failures=(\d+)\)"
)


def last_run_summary() -> dict[str, object]:
    """Parse the newest 'Finished run: RunSummary(...)' from the app log."""
    if not config.APP_LOG_FILE.exists():
        return {}
    try:
        with config.APP_LOG_FILE.open("r", encoding="utf-8", errors="replace") as file:
            lines = file.readlines()[-2500:]
    except OSError:
        return {}
    for line in reversed(lines):
        if "Finished run:" not in line:
            continue
        match = _RUN_SUMMARY_RE.search(line)
        if not match:
            continue
        videos, clips, considered, uploads, skipped, fails = (int(g) for g in match.groups())
        stamp = line[:19] if len(line) >= 19 else ""
        return {
            "when": stamp,
            "videos_found": videos,
            "clips_generated": clips,
            "clips_considered": considered,
            "uploads_completed": uploads,
            "uploads_skipped": skipped,
            "upload_failures": fails,
        }
    return {}


def live_dashboard_log_lines(limit: int = 90) -> list[str]:
    """Return recent useful app log lines for the live dashboard feed."""
    if not config.APP_LOG_FILE.exists():
        return []

    with config.APP_LOG_FILE.open("r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()[-600:]

    useful_lines = []
    # Drop HTTP-server chatter (page loads, asset fetches, 404 pings, google
    # cache notices) so the feed surfaces real pipeline events like
    # "Generated clip …", "Finished run …", uploads, and errors.
    skipped_patterns = (
        'dashboard: "GET ',
        'dashboard: code ',
        'file_cache is only supported',
        'googleapiclient.discovery_cache',
    )
    for line in lines:
        clean = line.rstrip()
        if not clean:
            continue
        if any(pattern in clean for pattern in skipped_patterns):
            continue
        useful_lines.append(_format_log_line(clean))

    return useful_lines[-limit:]


def _format_log_line(raw: str) -> str:
    """Turn a raw app.log line into 'HH:MM:SS  message' with short paths."""
    parts = raw.split(" | ")
    if len(parts) >= 4 and len(parts[0]) >= 19:
        stamp = parts[0][11:19]  # HH:MM:SS
        message = parts[-1]
    else:
        stamp = raw[11:19] if len(raw) >= 19 else ""
        message = raw
    # Collapse absolute paths to just the file/relative name.
    message = message.replace(str(config.BASE_DIR) + "/", "")
    message = message.replace("dashboard: ", "")
    return f"{stamp}  {message}".strip()


# --- Log prettifying (Vision live-activity feed) ---------------------------
# Turns raw pipeline log messages into the condensed feed style:
#   "Uploaded clip_000612.mp4 as YouTube video k2Xw9abc" -> "✔ Uploaded clip_000612 → k2Xw9…"
# The same rules exist client-side in LIVE_SCRIPT; these run for the initial
# server render so no-JS views (and first paint) match.
_LOG_UPLOADED_RE = re.compile(r"^Uploaded (\S+?)\.mp4 as YouTube video (\S+)\s*$")
_LOG_FAILED_RE = re.compile(r"^Upload failed for (\S+?)\.mp4[:\s]*(.*)$")
_LOG_GENERATED_RE = re.compile(r"^Generated clip .*?(clip_\d+\.mp4)\s*$")
_LOG_MOVED_RE = re.compile(r"^Moved processed source .*?([^/]+) to .*$")


def _pretty_log_message(message: str) -> str:
    """Condense one raw log message for the live feed (display only)."""
    match = _LOG_UPLOADED_RE.match(message)
    if match:
        return f"✔ Uploaded {match.group(1)} → {match.group(2)[:5]}…"
    match = _LOG_FAILED_RE.match(message)
    if match:
        tail = match.group(2).strip()
        return f"✘ Upload failed {match.group(1)}" + (f" — {tail[:60]}" if tail else "")
    match = _LOG_GENERATED_RE.match(message)
    if match:
        return f"Generated {match.group(1)}"
    match = _LOG_MOVED_RE.match(message)
    if match:
        return f"Moved {match.group(1)} → uploaded/"
    return message


def _log_line_class(message: str) -> str:
    """Feed color class for one raw log message (matches LIVE_SCRIPT's rules)."""
    lower = message.lower()
    if any(t in lower for t in ("upload failed", "error", "traceback", "quota", "upload limit", "failed:")):
        return "err"
    if any(t in lower for t in ("as youtube video", "automation complete", "stats refreshed",
                                "stats auto-refreshed", "finished run")) or lower.startswith("saved "):
        return "ok"
    if any(t in lower for t in ("generated clip", "generating clips", "discovered video",
                                "moved processed", "scanning for videos")):
        return "clip"
    return "dim"


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
    """Run main.py in a background thread unless already running or refreshing stats."""
    if RUN_STATE["running"] or STATS_REFRESH_LOCK.locked():
        return

    thread = threading.Thread(target=run_main_process, daemon=True)
    thread.start()


def start_clip_only() -> None:
    """Run clipping only in a background thread unless already running or refreshing stats."""
    if RUN_STATE["running"] or STATS_REFRESH_LOCK.locked():
        return

    thread = threading.Thread(target=run_main_process, kwargs={"clip_only": True}, daemon=True)
    thread.start()


def start_stats_refresh() -> None:
    """Refresh tracker stats in a background thread."""
    if RUN_STATE["running"]:
        return
    if STATS_REFRESH_LOCK.locked():
        return

    # Flip the flag synchronously so the page that redirects right after this
    # renders in the "Refreshing…" state and starts auto-reloading. The worker
    # clears it in its finally block when the fresh stats are written.
    RUN_STATE["stats_running"] = True
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
            "stats_running": True,
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
        RUN_STATE["stats_running"] = False
        RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        RUN_STATE["run_seq"] = int(RUN_STATE["run_seq"]) + 1
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
        RUN_STATE["stats_running"] = False
        return

    RUN_STATE.update(
        {
            "stats_running": True,
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
        RUN_STATE["stats_running"] = False
        RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        RUN_STATE["run_seq"] = int(RUN_STATE["run_seq"]) + 1


# Sample YouTube stats on a timer during posting hours so the hourly "today's
# gains" chart fills in even when nobody has the dashboard open. Without this,
# a bucket only shows a bar for the hours the page happened to be loaded, which
# makes it look like views only arrived at one or two moments in the day.
SNAPSHOT_INTERVAL_MINUTES = 30


def snapshot_youtube_stats_quietly() -> None:
    """Append ONE stats snapshot only — no tracker/project rebuild, no UI busy
    state. This is what the hourly chart needs, and keeping it lightweight means
    the background sampler doesn't steal CPU from page rendering or trigger the
    auto-reload the way a full manual refresh does. The heavy tracker/project
    rebuilds stay on the manual Refresh button and the pipeline run."""
    if RUN_STATE["running"] or RUN_STATE["stats_running"]:
        return
    # Skip if a heavy refresh holds the main lock (avoid two writers appending to
    # the history CSV at once) but do NOT take that lock ourselves — the pipeline
    # must stay free to start.
    if STATS_REFRESH_LOCK.locked():
        return
    if not SNAPSHOT_LOCK.acquire(blocking=False):
        return
    try:
        refresh_youtube_stats_history()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Background stats snapshot failed: %s", exc)
    finally:
        SNAPSHOT_LOCK.release()


def _snapshot_scheduler_loop() -> None:
    local_zone = ZoneInfo(config.TIMEZONE)
    # Sample when the last snapshot is older than the interval (minus a little
    # slack so timer jitter doesn't push us to every-other-cycle). Decoupled
    # from AUTO_STATS_REFRESH_MINUTES so the two behaviours can't fight.
    threshold = timedelta(minutes=max(5, SNAPSHOT_INTERVAL_MINUTES - 5))
    while True:
        try:
            now = datetime.now(local_zone)
            if config.POST_START_HOUR <= now.hour <= config.POST_END_HOUR:
                latest = latest_stats_checked_at()
                if latest is None:
                    due = True
                else:
                    ref_now = datetime.now(latest.tzinfo) if latest.tzinfo else datetime.now()
                    due = ref_now - latest >= threshold
                if due:
                    snapshot_youtube_stats_quietly()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Background snapshot scheduler error: %s", exc)
        time.sleep(SNAPSHOT_INTERVAL_MINUTES * 60)


def start_snapshot_scheduler() -> None:
    """Launch the background stats-sampling loop (daemon so it dies with the server)."""
    thread = threading.Thread(target=_snapshot_scheduler_loop, daemon=True)
    thread.start()
    logger.info("Hourly stats snapshotter started (every %d min, %d:00–%d:00).",
                SNAPSHOT_INTERVAL_MINUTES, config.POST_START_HOUR, config.POST_END_HOUR)


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


def _track_run_progress(line: str) -> None:
    """Update RUN_STATE progress counters from one line of pipeline output."""
    global UPLOAD_PHASE_STARTED_AT
    if "Generating clips from" in line or "Discovered video:" in line:
        RUN_STATE["phase"] = "clipping"
    elif "Generated clip" in line:
        RUN_STATE["phase"] = "clipping"
        RUN_STATE["clips_made"] = int(RUN_STATE["clips_made"]) + 1
    elif " as YouTube video " in line and "Uploaded" in line:
        if RUN_STATE["phase"] != "uploading":
            RUN_STATE["phase"] = "uploading"
            UPLOAD_PHASE_STARTED_AT = time.time()
        RUN_STATE["done"] = int(RUN_STATE["done"]) + 1
    elif "Upload failed for" in line:
        if RUN_STATE["phase"] != "uploading":
            RUN_STATE["phase"] = "uploading"
            UPLOAD_PHASE_STARTED_AT = time.time()
        RUN_STATE["done"] = int(RUN_STATE["done"]) + 1
        RUN_STATE["fails"] = int(RUN_STATE["fails"]) + 1
    elif "DAILY UPLOAD LIMIT" in line or "daily upload limit hit" in line:
        RUN_STATE["quota_hit"] = True
    elif "Finished run:" in line:
        RUN_STATE["phase"] = "finishing"


def stop_pipeline() -> bool:
    """Gracefully terminate the running pipeline subprocess (owner Stop button).

    Sends SIGTERM to the whole process group (main.py + any live ffmpeg), then
    escalates to SIGKILL a few seconds later if it is still alive. Returns True
    when a stop was actually initiated.
    """
    with PIPELINE_PROCESS_LOCK:
        process = PIPELINE_PROCESS
        if process is None or process.poll() is not None:
            return False
        RUN_STATE["stopping"] = True
        logger.info("Stop requested by owner; terminating pipeline run")
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()

        def _escalate(proc: subprocess.Popen) -> None:
            time.sleep(8)
            if proc.poll() is None:
                logger.warning("Pipeline did not exit after SIGTERM; killing it")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()

        threading.Thread(target=_escalate, args=(process,), daemon=True).start()
        return True


def run_main_process(clip_only: bool = False) -> None:
    """Execute the automation in a subprocess."""
    global PIPELINE_PROCESS, UPLOAD_PHASE_STARTED_AT
    UPLOAD_PHASE_STARTED_AT = 0.0
    RUN_STATE.update(
        {
            "running": True,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "last_output": "",
            "last_error": "",
            "phase": "starting",
            "done": 0,
            "total": 0 if clip_only else len(count_pending_clips()),
            "clips_made": 0,
            "fails": 0,
            "stopping": False,
            "stopped": False,
            "quota_hit": False,
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
            start_new_session=True,  # own process group so Stop can signal the whole tree
        )
        with PIPELINE_PROCESS_LOCK:
            PIPELINE_PROCESS = process
        assert process.stdout is not None
        deadline = time.monotonic() + (60 * 60 * 4)
        for line in process.stdout:
            clean = line.rstrip()
            output_lines.append(clean)
            _track_run_progress(clean)
            RUN_STATE["last_output"] = "\n".join(output_lines[-80:]).strip()
            if time.monotonic() > deadline:
                process.kill()
                raise TimeoutError("Dashboard run timed out after 4 hours")

        return_code = process.wait()
        refresh_project_dataset()
        RUN_STATE["last_output"] = "\n".join(output_lines[-120:]).strip()
        if RUN_STATE["stopping"]:
            RUN_STATE["stopped"] = True
            logger.info("Pipeline run stopped by owner (after %s upload(s))", RUN_STATE["done"])
        elif return_code != 0:
            RUN_STATE["last_error"] = RUN_STATE["last_output"]
    except Exception as exc:
        logger.exception("Dashboard run failed: %s", exc)
        output_lines.append(str(exc))
        RUN_STATE["last_output"] = "\n".join(output_lines[-120:]).strip()
        RUN_STATE["last_error"] = str(exc)
    finally:
        with PIPELINE_PROCESS_LOCK:
            PIPELINE_PROCESS = None
        RUN_STATE["running"] = False
        RUN_STATE["stopping"] = False
        RUN_STATE["phase"] = ""
        RUN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        RUN_STATE["run_seq"] = int(RUN_STATE["run_seq"]) + 1


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
# v6 "Vision" theme — visionOS-style floating glass panels layered over the
# original piano/aurora stage. Same class names as v5 so every render function
# keeps working unchanged; only the presentation layer moves. Fonts are the
# Apple system stack (SF Pro on Apple hardware) everywhere, including charts.
# ============================================================================
STYLE_V6 = r"""<style>
  :root{
    --green:#4ade80;--green-soft:rgba(74,222,128,.16);
    --blue:#7dd3fc;--violet:#c4b5fd;--teal:#5eead4;--amber:#fcd34d;--rose:#fb7185;
    --text:#f4f6fb;--muted:rgba(235,240,255,.62);--faint:rgba(235,240,255,.4);
    --panel:rgba(255,255,255,.085);--panel2:rgba(255,255,255,.14);--line:rgba(255,255,255,.2);
    --font:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Helvetica Neue",Helvetica,Arial,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;min-height:100%}
  a{color:inherit;text-decoration:none}
  body{font-family:var(--font);color:var(--text);background:#05060d;-webkit-font-smoothing:antialiased;letter-spacing:-.012em;min-height:100vh;position:relative;overflow-x:hidden}
  ::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:8px}

  /* ---- the piano stage (kept from the original dashboard) ---- */
  /* Fixed background promoted to its own GPU layer so it is rasterized once and
     just composited (not repainted) on every scroll frame — the main scroll-lag
     fix, especially on the Retina panel. pointer-events:none keeps hit-testing
     off it entirely. */
  .stage{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none;
    transform:translateZ(0);will-change:transform;backface-visibility:hidden;contain:paint}
  .wash{position:absolute;inset:-12%;
    background:
      radial-gradient(48% 40% at 20% 16%, rgba(109,91,208,.5), transparent 60%),
      radial-gradient(46% 42% at 84% 24%, rgba(43,139,201,.42), transparent 60%),
      radial-gradient(65% 55% at 50% 104%, rgba(31,138,99,.32), transparent 60%),
      radial-gradient(40% 40% at 92% 88%, rgba(251,113,133,.2), transparent 60%),
      linear-gradient(180deg,#0a0b16,#05070f 60%,#03050c)}
  .blob{position:absolute;border-radius:50%;filter:blur(80px);opacity:.42;transform:translateZ(0)}
  .blob.g{width:640px;height:640px;background:radial-gradient(circle,#22c55e,transparent 68%);top:-190px;left:6%;opacity:.3}
  .blob.v{width:560px;height:560px;background:radial-gradient(circle,#6d5bd0,transparent 68%);top:-130px;right:5%}
  .blob.b{width:520px;height:520px;background:radial-gradient(circle,#2b8bc9,transparent 68%);bottom:-170px;left:42%}
  .grain{position:absolute;inset:0;opacity:.045;background-image:radial-gradient(#fff 1px,transparent 1px);background-size:4px 4px}
  .keys{position:absolute;left:50%;bottom:-46px;transform:translateX(-50%) perspective(1150px) rotateX(51deg);
    transform-origin:bottom center;display:flex;gap:4px;opacity:.4;transition:opacity .6s}
  body.tab .keys{opacity:.14}
  .key{width:50px;height:278px;border-radius:0 0 8px 8px;background:linear-gradient(180deg,#e9edf6,#aab3c6);position:relative;box-shadow:inset 0 -12px 20px rgba(0,0,0,.25)}
  .key.g{background:linear-gradient(180deg,#c9ffe0,#4ade80);box-shadow:0 0 30px rgba(74,222,128,.8)}
  .key.v{background:linear-gradient(180deg,#e5dcff,#c4b5fd);box-shadow:0 0 30px rgba(196,181,253,.8)}
  .key.b{background:linear-gradient(180deg,#d5e7ff,#7dd3fc);box-shadow:0 0 30px rgba(125,211,252,.8)}
  .bk{position:absolute;top:0;right:-14px;width:28px;height:176px;border-radius:0 0 5px 5px;background:linear-gradient(180deg,#181c26,#05070c);z-index:3;box-shadow:0 6px 8px rgba(0,0,0,.5)}
  .key.nb .bk{display:none}
  .note{position:absolute;bottom:3%;color:rgba(255,255,255,.4);text-shadow:0 0 6px rgba(74,222,128,.35);will-change:transform,opacity;pointer-events:none}
  @keyframes rise{0%{transform:translateY(20px) rotate(0);opacity:0}12%{opacity:.9}85%{opacity:.6}100%{transform:translateY(-48vh) rotate(16deg);opacity:0}}

  /* ---- home splash ---- */
  .home{position:relative;z-index:2;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:40px 20px}
  .shell{position:relative;z-index:2;width:min(1160px,94vw);margin:0 auto}
  .mark{width:64px;height:64px;border-radius:50%;background:linear-gradient(135deg,#4ade80,#16a34a);display:grid;place-items:center;margin:0 auto 12px;color:#03170c;
    box-shadow:0 0 0 10px rgba(74,222,128,.12),0 0 60px rgba(74,222,128,.5);animation:hoverfloat 5s ease-in-out infinite}
  @keyframes hoverfloat{50%{transform:translateY(-8px)}}
  .mark svg{width:29px;height:29px}
  h1.big{font-size:clamp(40px,6.4vw,72px);font-weight:800;margin:6px 0 0;letter-spacing:-.045em;background:linear-gradient(180deg,#fff,#a9c8e8);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .kicker{margin:14px 0 4px;letter-spacing:.42em;font-size:12px;font-weight:700;color:rgba(235,240,255,.7);text-transform:uppercase}
  .stat-line{color:var(--muted);font-size:13.5px;margin-bottom:26px;font-weight:600}.stat-line b{color:var(--green)}
  .glass{position:relative;margin:0 auto;padding:32px 26px 28px;border-radius:28px;width:min(1180px,95vw);
    background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.22);
    backdrop-filter:blur(28px) saturate(150%);-webkit-backdrop-filter:blur(28px) saturate(150%);
    box-shadow:0 40px 120px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.25);transition:transform .2s cubic-bezier(.2,.7,.2,1)}
  .glass::before{content:"";position:absolute;top:0;left:38px;right:38px;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent)}
  .grid5{display:grid;grid-template-columns:repeat(5,1fr);gap:13px}
  .tile{position:relative;overflow:hidden;padding:25px 16px 22px;border-radius:22px;cursor:pointer;text-align:center;
    background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);
    transition:transform .3s cubic-bezier(.2,.8,.2,1),background .3s,border-color .3s,box-shadow .3s;display:block}
  .tile::after{content:"";position:absolute;width:180px;height:180px;border-radius:50%;filter:blur(46px);top:-100px;right:-60px;opacity:0;transition:opacity .35s;background:var(--ac,var(--green))}
  .tile:hover{transform:translateY(-8px) scale(1.03);background:rgba(255,255,255,.12);border-color:color-mix(in srgb,var(--ac,var(--green)) 55%,transparent);box-shadow:0 26px 60px rgba(0,0,0,.5)}
  .tile:hover::after{opacity:.5}
  .tile .ic{width:56px;height:56px;margin:0 auto 15px;display:grid;place-items:center;border-radius:16px;color:#eef2f8;background:rgba(255,255,255,.07);transition:transform .35s cubic-bezier(.2,.8,.2,1),color .3s,background .3s}
  .tile:hover .ic{color:var(--ac);background:color-mix(in srgb,var(--ac) 18%,transparent);transform:translateY(-2px) scale(1.08)}
  .tile .ic svg{width:30px;height:30px;transition:filter .3s}
  .tile:hover .ic svg{filter:drop-shadow(0 0 10px color-mix(in srgb,var(--ac) 90%,transparent))}
  .tile b{display:block;font-size:15px;font-weight:700;color:var(--text)}
  .tile small{display:block;color:var(--muted);font-size:11.5px;margin-top:3px;opacity:.6;transition:.3s}
  .tile:hover small{opacity:1;color:#dbe4f2}
  .tile .go{position:absolute;top:13px;right:14px;color:var(--ac);opacity:0;transform:translateX(-4px);transition:.3s;font-weight:800}
  .tile:hover .go{opacity:1;transform:none}
  .t1{--ac:#4ade80}.t2{--ac:#7dd3fc}.t3{--ac:#fb7185}.t4{--ac:#fcd34d}.t5{--ac:#c4b5fd}
  .eq rect{transform-origin:bottom;animation:bar 1.1s ease-in-out infinite;animation-play-state:paused}
  .tile:hover .eq rect{animation-play-state:running}
  .eq rect:nth-child(2){animation-delay:.18s}.eq rect:nth-child(3){animation-delay:.36s}.eq rect:nth-child(4){animation-delay:.1s}
  @keyframes bar{0%,100%{transform:scaleY(.4)}50%{transform:scaleY(1)}}
  .social{margin-top:32px;display:flex;justify-content:center;gap:22px}
  .social a{width:44px;height:44px;display:grid;place-items:center;border-radius:50%;color:#cdd4de;border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.06);backdrop-filter:blur(10px);transition:transform .25s cubic-bezier(.2,.8,.2,1),color .25s,box-shadow .25s,background .25s}
  .social a:hover{transform:translateY(-4px) scale(1.12);color:#fff;background:var(--green-soft);box-shadow:0 10px 26px rgba(74,222,128,.4);border-color:transparent}
  .social a svg{width:19px;height:19px}

  /* ---- floating pill nav (Vision) ---- */
  .bar{position:sticky;top:14px;z-index:6;display:flex;align-items:center;gap:14px;
    width:min(1160px,94vw);margin:14px auto 0;padding:9px 14px;border-radius:999px;
    background:rgba(14,16,28,.92);border:1px solid rgba(255,255,255,.2);
    box-shadow:0 20px 60px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.2)}
  .backhome{display:inline-flex;align-items:center;gap:9px;font-weight:700;font-size:14px;cursor:pointer;padding:7px 12px;border-radius:999px;transition:.2s}
  .backhome:hover{background:rgba(255,255,255,.08)}
  .backhome .dot{width:24px;height:24px;border-radius:50%;background:linear-gradient(135deg,#4ade80,#16a34a);display:grid;place-items:center;box-shadow:0 0 18px rgba(74,222,128,.45)}
  .backhome .dot svg{width:13px;height:13px}
  .pills{display:flex;gap:4px;flex-wrap:wrap;margin-left:auto}
  .pills a{font-size:12.5px;font-weight:600;color:var(--muted);padding:8px 15px;border-radius:999px;transition:.2s}
  .pills a:hover{background:rgba(255,255,255,.09);color:var(--text)}
  .pills a.on{background:rgba(255,255,255,.92);color:#10122b;box-shadow:0 6px 18px rgba(0,0,0,.35)}
  .page{padding:30px clamp(16px,4vw,40px) 90px}
  .eyebrow{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--blue);font-weight:700;margin:0 0 6px}
  .h2{font-size:clamp(26px,3.4vw,38px);font-weight:800;letter-spacing:-.035em;margin:0}
  .sub{color:var(--muted);font-size:14px;margin-top:5px}
  .topline{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:8px}
  .top-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}

  /* ---- glass panels ---- */
  .panel{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:24px;padding:22px;
    backdrop-filter:blur(24px) saturate(150%);-webkit-backdrop-filter:blur(24px) saturate(150%);
    box-shadow:0 24px 70px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.22);
    transition:transform .3s cubic-bezier(.2,.8,.2,1),background .3s}
  .panel::before{content:"";position:absolute;top:0;left:28px;right:28px;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent)}
  .panel:hover{transform:translateY(-3px);background:var(--panel2)}
  .panel h3{margin:0 0 14px;font-size:14.5px;font-weight:700}
  .row{display:grid;gap:16px}
  .r-4{grid-template-columns:repeat(4,1fr)}.r-3{grid-template-columns:repeat(3,1fr)}.r-2{grid-template-columns:1.5fr 1fr}
  @media(max-width:960px){.r-4,.r-3,.r-2,.grid5{grid-template-columns:1fr 1fr}}
  @media(max-width:560px){.r-4,.r-3,.r-2,.grid5{grid-template-columns:1fr}}
  .mt{margin-top:16px}

  .pill{display:inline-flex;align-items:center;gap:8px;padding:8px 15px;border-radius:999px;font-size:12px;font-weight:700}
  .pill.ready{background:rgba(74,222,128,.15);color:var(--green);border:1px solid rgba(74,222,128,.3)}
  .pill.ready .d{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:10px 17px;border-radius:999px;font:600 13px var(--font);cursor:pointer;
    border:1px solid var(--line);background:rgba(255,255,255,.08);color:var(--text);backdrop-filter:blur(10px);transition:.2s}
  .btn:hover{background:rgba(255,255,255,.16);transform:translateY(-1px)}
  .btn.primary{background:rgba(74,222,128,.92);color:#03170c;border-color:transparent;box-shadow:0 10px 30px rgba(74,222,128,.3)}
  .btn.primary:hover{transform:translateY(-2px);box-shadow:0 14px 36px rgba(74,222,128,.45);background:var(--green)}
  .btn svg{width:15px;height:15px}
  form.inline{display:inline-flex;margin:0}

  .kpi{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:19px;
    backdrop-filter:blur(20px) saturate(150%);-webkit-backdrop-filter:blur(20px) saturate(150%);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.2);transition:.3s cubic-bezier(.2,.8,.2,1)}
  .kpi:hover{transform:translateY(-4px);background:var(--panel2);border-color:color-mix(in srgb,var(--kc,var(--green)) 55%,rgba(255,255,255,.2));box-shadow:0 18px 44px rgba(0,0,0,.4)}
  .kpi::after{content:"";position:absolute;width:150px;height:150px;border-radius:50%;filter:blur(50px);top:-80px;right:-50px;opacity:.3;background:var(--kc,var(--green))}
  .kpi .lab{color:var(--muted);font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.1em}
  .kpi .val{font-size:30px;font-weight:800;letter-spacing:-.04em;margin-top:8px;text-shadow:0 2px 20px rgba(125,211,252,.25)}
  .kpi .d{font-size:12px;font-weight:600;color:var(--kc,var(--green));margin-top:3px}
  .kc-g{--kc:#4ade80}.kc-b{--kc:#7dd3fc}.kc-t{--kc:#5eead4}.kc-a{--kc:#fcd34d}
  .hero-num{font-size:clamp(46px,7vw,84px);font-weight:800;letter-spacing:-.045em;line-height:1;background:linear-gradient(180deg,#fff,#a9c8e8);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .chartbox{position:relative;height:240px;margin-top:14px;width:100%}

  /* piano-key hour chart */
  .pk-wrap{display:flex;align-items:flex-end;gap:10px;height:220px;padding-top:10px}
  .pk{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
  .pk .col{width:100%;max-width:52px;border-radius:9px 9px 6px 6px;background:linear-gradient(180deg,rgba(255,255,255,.55),rgba(255,255,255,.16));transition:transform .3s,box-shadow .3s;box-shadow:inset 0 1px 0 rgba(255,255,255,.5)}
  .pk.best .col{background:linear-gradient(180deg,#c9ffe0,#4ade80);box-shadow:0 0 26px rgba(74,222,128,.65)}
  .pk:hover .col{transform:translateY(-5px);box-shadow:0 0 26px rgba(125,211,252,.55)}
  .pk .v{font-size:12px;font-weight:700;margin-bottom:6px;color:#eef2f8}
  .pk .h{font-size:11px;color:var(--muted);margin-top:8px;font-weight:600}

  .lead{display:grid;grid-template-columns:34px 1fr auto;gap:14px;align-items:center;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.1)}
  .lead:last-child{border-bottom:0}
  .lead .rk{font-weight:800;font-size:16px;text-align:center;background:linear-gradient(180deg,#fff,#a9d8f5);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .lead .nm b{display:block;font-size:14px;font-weight:600}.lead .nm small{color:var(--faint);font-size:11.5px;font-family:ui-monospace,Menlo,monospace}
  .lead .nu{text-align:right}.lead .nu b{display:block;font-weight:700}.lead .nu small{color:var(--green);font-size:11.5px}

  .set{display:flex;align-items:center;gap:14px;padding:13px 14px;border-radius:16px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.05);margin-bottom:9px;transition:.2s}
  .set:hover{transform:translateX(4px);border-color:rgba(74,222,128,.45);background:rgba(74,222,128,.07)}
  .set .np{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:rgba(125,211,252,.14);color:var(--blue);flex:none}
  .set .np svg{width:16px;height:16px}
  .set .mid{flex:1}.set .mid b{font-size:14px;font-weight:600}.set .mid small{display:block;color:var(--faint);font-size:11.5px;font-family:ui-monospace,Menlo,monospace}
  .set .time{color:var(--muted);font-size:12.5px;font-weight:600}
  .badge{font-size:10.5px;font-weight:700;padding:4px 11px;border-radius:999px;margin-left:12px}
  .badge.up{background:rgba(74,222,128,.15);color:var(--green)}.badge.sch{background:rgba(125,211,252,.15);color:var(--blue)}

  /* TikTok podium */
  .podium{display:grid;grid-template-columns:1fr 1.15fr 1fr;gap:16px;align-items:end;margin-bottom:8px}
  @media(max-width:880px){.podium{grid-template-columns:1fr}}
  .pod{position:relative;overflow:hidden;border-radius:22px;padding:22px 16px;text-align:center;border:1px solid var(--line);background:var(--panel);
    backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);box-shadow:inset 0 1px 0 rgba(255,255,255,.2);transition:.3s cubic-bezier(.2,.8,.2,1)}
  .pod::after{content:"";position:absolute;inset:0;opacity:.1;background:var(--pc);pointer-events:none}
  .pod .medal{font-size:26px}.pod .w{font-size:18px;font-weight:800;margin:8px 0 2px;position:relative}
  .pod .c{font-size:11px;color:var(--faint);font-family:ui-monospace,Menlo,monospace;position:relative}
  .pod .met{margin-top:10px;font-size:13px;position:relative}.pod .met b{color:#fff}
  .pod.gold{--pc:#fcd34d;transform:translateY(-10px);border-color:rgba(252,211,77,.45)}
  .pod.silver{--pc:#c8ccd2}.pod.bronze{--pc:#e08a4b}
  .pod:hover{transform:translateY(-14px);background:var(--panel2);box-shadow:0 24px 54px rgba(0,0,0,.45)}
  .pod.gold:hover{transform:translateY(-20px)}
  .day-block{margin-top:24px}.day-block h4{margin:0 0 12px;font-size:11.5px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.16em}
  .day-block h4 b{color:var(--blue)}

  /* tracker library */
  .lib{display:grid;grid-template-columns:repeat(7,1fr);gap:13px}
  @media(max-width:1100px){.lib{grid-template-columns:repeat(4,1fr)}}
  @media(max-width:640px){.lib{grid-template-columns:repeat(2,1fr)}}
  .alb{border-radius:18px;overflow:hidden;border:1px solid var(--line);background:var(--panel);backdrop-filter:blur(14px);transition:.3s cubic-bezier(.2,.8,.2,1);cursor:pointer;display:block}
  .alb:hover{transform:translateY(-6px) scale(1.02);box-shadow:0 20px 46px rgba(0,0,0,.5);border-color:rgba(125,211,252,.5)}
  .alb .cover{height:104px;background:#0e1118 center/cover no-repeat;position:relative}
  .alb .play{position:absolute;top:8px;right:8px;width:22px;height:22px;border-radius:50%;background:rgba(0,0,0,.45);display:grid;place-items:center;opacity:0;transition:.25s}
  .alb:hover .play{opacity:1}.alb .play svg{width:11px;height:11px;color:#fff}
  .alb .meta{padding:10px 12px}.alb .meta b{font-size:12.5px;display:block;font-weight:600}.alb .meta small{color:var(--faint);font-size:10.5px;font-family:ui-monospace,Menlo,monospace}
  .dlbtn{display:inline-flex;align-items:center;gap:8px;padding:10px 17px;border-radius:999px;font-weight:700;font-size:13px;cursor:pointer;color:#03170c;background:rgba(74,222,128,.92);box-shadow:0 10px 26px rgba(74,222,128,.3);transition:.2s}
  .dlbtn:hover{transform:translateY(-2px);box-shadow:0 14px 34px rgba(74,222,128,.45)}.dlbtn svg{width:15px;height:15px}

  /* experiment octave */
  .octave{display:flex;gap:5px;justify-content:center;margin:10px 0 22px;flex-wrap:wrap}
  .wkey{width:58px;height:150px;border-radius:0 0 12px 12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);position:relative;display:flex;align-items:flex-end;justify-content:center;padding-bottom:12px;transition:.3s;backdrop-filter:blur(8px)}
  .wkey .n{font-size:12px;font-weight:700;color:var(--faint)}
  .wkey.done{background:rgba(74,222,128,.14);border-color:rgba(74,222,128,.3)}.wkey.done .n{color:#8ef0ab}
  .wkey.live{background:linear-gradient(180deg,#c9ffe0,#4ade80);box-shadow:0 0 30px rgba(74,222,128,.6);animation:hoverfloat 4s infinite}.wkey.live .n{color:#03170c}
  .wkey:hover{transform:translateY(-6px)}
  .phase{border:1px solid var(--line);border-radius:18px;background:var(--panel);backdrop-filter:blur(18px);margin-bottom:10px;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,.18)}
  .phase summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:14px;padding:16px}
  .phase summary::-webkit-details-marker{display:none}
  .phase .wk{font-size:11px;font-weight:700;color:var(--green);width:64px;flex:none}
  .phase .pt b{display:block;font-size:15px;font-weight:700}.phase .pt small{color:var(--muted);font-size:12.5px}
  .phase .st{margin-left:auto;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:4px 11px;border-radius:999px}
  .st.live{background:rgba(74,222,128,.16);color:var(--green)}.st.done{background:rgba(255,255,255,.08);color:var(--faint)}.st.next{background:rgba(125,211,252,.14);color:var(--blue)}
  .phase .body{padding:0 16px 18px 78px;color:var(--muted);font-size:13px;line-height:1.6}
  .phase .body .l{color:var(--faint);text-transform:uppercase;font-size:10px;letter-spacing:.12em;font-weight:700;display:block;margin:10px 0 3px}
  .tag{display:inline-block;font-size:11px;font-weight:600;color:var(--green);background:var(--green-soft);padding:4px 11px;border-radius:999px;margin:6px 6px 0 0}
  .prog{height:8px;border-radius:999px;background:rgba(255,255,255,.1);overflow:hidden;margin:6px 0 4px}
  .prog i{display:block;height:100%;background:linear-gradient(90deg,#4ade80,#7dd3fc);border-radius:999px;box-shadow:0 0 14px rgba(74,222,128,.45)}

  .chip{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:15px 17px;backdrop-filter:blur(16px);box-shadow:inset 0 1px 0 rgba(255,255,255,.18);transition:.25s}
  .chip:hover{transform:translateY(-3px);background:var(--panel2)}
  .chip .l{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em}.chip .v{font-size:23px;font-weight:800;margin-top:7px;letter-spacing:-.03em}
  .log{background:rgba(0,0,0,.32);border:1px solid rgba(255,255,255,.1);border-radius:14px;padding:14px 16px;font:500 12px ui-monospace,Menlo,monospace;color:rgba(220,230,225,.6);margin-top:0;max-height:210px;overflow:auto}
  .log div{padding:2px 0}.log .t{color:rgba(255,255,255,.3)}
  .upzone{border:1.5px dashed rgba(255,255,255,.25);border-radius:16px;padding:18px;text-align:center;background:linear-gradient(180deg,rgba(74,222,128,.06),transparent)}
  .now-card{border:1px solid rgba(74,222,128,.35);background:linear-gradient(180deg,rgba(74,222,128,.1),transparent);border-radius:18px;padding:18px;margin-bottom:16px;display:flex;align-items:center;gap:16px;box-shadow:inset 0 1px 0 rgba(255,255,255,.15)}
  .now-card .big-np{width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,#4ade80,#16a34a);color:#03170c;display:grid;place-items:center;flex:none;box-shadow:0 0 26px rgba(74,222,128,.45);animation:hoverfloat 4.5s infinite}
  .now-card .big-np svg{width:24px;height:24px}
  .chipset{display:flex;flex-wrap:nowrap;gap:8px;margin-bottom:16px;overflow-x:auto;padding-bottom:6px;-webkit-overflow-scrolling:touch}
  .chipset::-webkit-scrollbar{height:6px}.chipset::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:8px}
  .fchip{flex:none;white-space:nowrap;font-size:12px;font-weight:600;color:var(--muted);background:rgba(255,255,255,.07);border:1px solid var(--line);padding:7px 14px;border-radius:999px}
  .fchip.active{background:rgba(255,255,255,.92);color:#10122b;border-color:transparent}

  /* data-science split */
  .ds-scroll{max-height:calc(100vh - 430px);min-height:240px;overflow:auto;border:1px solid rgba(255,255,255,.14);border-radius:16px;
    transform:translateZ(0);contain:paint;-webkit-overflow-scrolling:touch}
  /* Isolate each card's paint so scrolling one region can't invalidate the rest.
     All of these already clip with overflow:hidden, so containment is free. */
  .panel,.kpi,.chip,.pod,.alb,.tile{contain:layout paint}
  .ds-split{display:grid;grid-template-columns:346px 1fr;gap:18px}
  @media(max-width:900px){.ds-split{grid-template-columns:1fr}}
  .ds-list{max-height:calc(100vh - 430px);min-height:240px;overflow:auto;padding-right:6px}
  .ds-list::-webkit-scrollbar{width:8px}.ds-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:8px}
  .arow{display:flex;align-items:center;gap:13px;padding:9px 10px;border-radius:14px;cursor:pointer;transition:.16s}
  .arow:hover{background:rgba(255,255,255,.08)}
  .arow.sel{background:rgba(255,255,255,.1)}
  .arow .acov{width:52px;height:52px;border-radius:10px;flex:none;background:#0e1118 center/cover no-repeat;box-shadow:0 4px 12px rgba(0,0,0,.4)}
  .arow .acov.all{background:linear-gradient(135deg,#4ade80,#0d6e46);display:grid;place-items:center}
  .arow .acov.all svg{width:24px;height:24px}
  .arow .acov.empty{background:rgba(255,255,255,.06)}
  .arow .atx{min-width:0;flex:1}
  .arow .att{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:6px}
  .arow.sel .att{color:var(--green)}
  .arow .ast{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
  .arow.future{opacity:.5}.arow.future .ast{color:var(--faint)}
  .apin{width:12px;height:12px;color:var(--green);flex:none;opacity:0}
  .arow.sel .apin{opacity:1}
  .acount{margin-left:auto;font-size:11px;font-weight:700;color:var(--muted);background:rgba(255,255,255,.08);padding:3px 9px;border-radius:999px;flex:none}
  .arow.sel .acount{background:var(--green-soft);color:var(--green)}
  .ds-scroll table{margin:0;width:100%;border-collapse:collapse;font-size:13px}
  .ds-scroll th{position:sticky;top:0;z-index:2;background:#12141f;text-align:left;color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.1em;font-weight:700;padding:12px;border-bottom:1px solid rgba(255,255,255,.14)}
  .ds-scroll td{padding:11px 12px;border-top:1px solid rgba(255,255,255,.08);white-space:nowrap}
  .ds-scroll tbody tr:hover{background:rgba(255,255,255,.05)}
  .ds-scroll .clip{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:12px}.ds-scroll .word{font-weight:600}
  .placeholder{display:grid;place-items:center;min-height:220px;text-align:center;color:var(--muted)}
  .cbadge{position:fixed;left:14px;bottom:12px;z-index:9;color:rgba(255,255,255,.35);font-size:11px;font-weight:600}

  /* calm mode: soften the glass + still the stage */
  body.calm .panel,body.calm .kpi,body.calm .chip,body.calm .glass,body.calm .pod,body.calm .alb,body.calm .phase{backdrop-filter:none;-webkit-backdrop-filter:none}
  body.calm .mark,body.calm .now-card .big-np,body.calm .wkey.live{animation:none}

  /* ---- perf mode (auto on Retina / high-DPI) --------------------------------
     On the built-in MacBook display devicePixelRatio is 2, so every
     backdrop-filter panel costs ~4x the GPU work of a 1x external monitor, and
     any element moving *behind* the glass (the floating notes) forces the whole
     blur pass to re-run each frame. That's the lag. `perf` mode (added by JS
     when devicePixelRatio>=2 or prefers-reduced-motion) trims the blur radius
     so scrolling stays cheap; the JS also stops spawning notes behind the
     glass. The look stays glassy — it's just far lighter to composite. */
  /* Drop backdrop-filter ENTIRELY in perf mode: a blurred panel must re-sample
     everything behind it as you scroll, which is the Retina scroll-lag. We keep
     the glassy look with a slightly more opaque panel fill + lighter shadows, so
     scrolling composites cheaply. Low-DPI external monitors keep the full glass. */
  body.perf .panel,body.perf .kpi,body.perf .chip,body.perf .glass,body.perf .pod,
  body.perf .alb,body.perf .phase,body.perf .wkey,body.perf .btn,body.perf .social a,
  body.perf .octave .wkey{backdrop-filter:none;-webkit-backdrop-filter:none}
  body.perf .panel,body.perf .kpi,body.perf .chip,body.perf .pod,body.perf .alb,body.perf .phase{
    background:rgba(21,23,36,.68);box-shadow:0 12px 30px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.13)}
  body.perf .glass{background:rgba(21,23,36,.74);box-shadow:0 20px 50px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.16)}
  body.perf .blob{filter:blur(56px);opacity:.28}
  body.perf .panel:hover,body.perf .kpi:hover,body.perf .chip:hover,body.perf .pod:hover,body.perf .alb:hover{transform:none}

  @media (prefers-reduced-motion: reduce){
    .mark,.now-card .big-np,.wkey.live,.pill.ready .d,.eq rect{animation:none!important}
    .note{display:none}
    *{scroll-behavior:auto!important}
  }

  /* ===================== SMOOTH-SCROLL OVERRIDES ============================
     Applied to ALL displays (kept last so they win). Three things made scroll
     jank at ~1s/frame on Retina, and none of them are removed by Calm mode:
       1. backdrop-filter: every glass card re-blurs what's behind it each frame
          while you scroll — the single biggest cost. Removed everywhere.
       2. Huge soft box-shadows (blur radius 70–120px) that re-rasterize as cards
          scroll into view. Reduced to a small, cheap shadow.
       3. An always-on 50px blur glow on every KPI (.kpi::after) — swapped for a
          cheap radial-gradient that needs no filter pass.
     Cards still read as glass via translucency + a thin top highlight + border,
     so the Vision look is preserved; it just composites at 60fps now. */
  .panel,.kpi,.chip,.glass,.pod,.alb,.phase,.wkey,.octave .wkey,
  .btn,.social a,.now-card,.upzone,.fchip,.set,.arow{
    backdrop-filter:none!important;-webkit-backdrop-filter:none!important}
  .panel,.kpi,.chip,.pod,.alb,.phase{background:rgba(20,22,34,.74)!important}
  .glass{background:rgba(20,22,34,.82)!important}
  .panel,.kpi,.chip,.pod,.alb,.glass,.tile,.now-card,.set{
    box-shadow:0 6px 18px rgba(0,0,0,.26),inset 0 1px 0 rgba(255,255,255,.1)!important}
  .panel:hover,.kpi:hover,.chip:hover,.pod:hover,.alb:hover,.tile:hover{
    box-shadow:0 10px 26px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.12)!important}
  /* replace the filter-blur corner glows with a no-cost radial gradient */
  .kpi::after,.tile::after{filter:none!important;
    background:radial-gradient(circle at 75% 12%,var(--kc,var(--ac,#4ade80)),transparent 70%)}
  .blob{filter:blur(60px)!important}

  /* ---- Retina / perf HARD-CUT (built-in display + FPS-flagged slow monitors) --
     backdrop-filter is already gone everywhere, yet the 2x built-in panel still
     stalled because of effects whose cost scales with pixel count (4x at 2x DPI):
       1. the aurora blobs' filter:blur(60px) — a giant blurred raster buffer;
       2. the tiled grain overlay;
       3. TRANSLUCENT cards — a semi-transparent panel can't composite as a flat
          tile, so the GPU re-blends it against the background on every scroll
          frame. Making the scrolling cards opaque removes that per-frame blend,
          which is the biggest remaining Retina scroll cost;
       4. always-on filter/text-shadow glows + idle animations that keep the
          compositor busy (and delay input) even when nothing is happening.
     All scoped to body.perf, so low-DPI external monitors keep the full glass. */
  body.perf .blob{filter:none!important;opacity:.2}
  body.perf .grain{display:none!important}
  body.perf .panel,body.perf .kpi,body.perf .chip,body.perf .pod,body.perf .alb,
  body.perf .phase,body.perf .now-card,body.perf .set,body.perf .arow,body.perf .pill{
    background:#12131f!important;box-shadow:0 4px 14px rgba(0,0,0,.28)!important}
  body.perf .glass{background:#111220!important;box-shadow:0 8px 22px rgba(0,0,0,.32)!important}
  body.perf .kpi::after,body.perf .tile::after{display:none!important}
  body.perf .mark,body.perf .now-card .big-np,body.perf .wkey.live,body.perf .pill.ready .d,
  body.perf .eq rect,body.perf .chiplet.live,body.perf .runprog .fill.indet{animation:none!important}
  body.perf .note{display:none!important}
  body.perf *{text-shadow:none!important}
</style>"""

BG_MARKUP = """<div class="stage">
  <div class="wash"></div>
  <div class="blob g"></div><div class="blob v"></div><div class="blob b"></div>
  <div class="keys" id="keys"></div>
  <div class="grain"></div>
</div>"""

BG_SCRIPT = r"""<script>
(function(){
  // Perf mode (auto): on the built-in Retina panel devicePixelRatio is 2, so the
  // backdrop-filter glass costs ~4x an external 1x monitor and any element moving
  // behind it re-runs the blur every frame. Detecting hi-DPI / reduced-motion lets
  // us trim the blur AND skip the floating-note spawner (the main cause of the
  // MacBook-screen lag) while low-DPI monitors keep the full effect.
  var reduceMotion=false;
  try{reduceMotion=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;}catch(e){}
  // Perf mode auto-enables on hi-DPI/Retina and reduced-motion. It is ALSO
  // remembered in localStorage: once a machine is known to be slow (see the FPS
  // watchdog below) every reload paints straight into the fast path with no
  // first-scroll jank -- so reloads keep getting cheaper, not slower.
  var perfStored=false;try{perfStored=localStorage.getItem('ps-perf')==='1';}catch(e){}
  var perfOn=perfStored||(window.devicePixelRatio||1)>=2||reduceMotion;
  function setPerf(){if(document.body.classList.contains('perf'))return;perfOn=true;document.body.classList.add('perf');try{localStorage.setItem('ps-perf','1');}catch(e){}}
  if(perfOn)document.body.classList.add('perf');
  // FPS watchdog: on a low-DPI (1x) external monitor perf stays off, but the
  // backdrop-filter glass can still stutter on scroll. Sample ~1s of frames
  // after load; if the effective frame rate is poor, switch to perf mode and
  // remember it. Only runs when perf isn't already on, and a hidden tab simply
  // never completes the sample (no false positives).
  if(!perfOn){try{var _frames=0,_start=0;var _tick=function(now){if(!_start)_start=now;_frames++;if(now-_start<1000){requestAnimationFrame(_tick);return;}if(_frames*1000/(now-_start)<45)setPerf();};requestAnimationFrame(_tick);}catch(e){}}

  // Calm mode: persisted in localStorage, applied ASAP so animations never start.
  var calmOn=false;try{calmOn=localStorage.getItem('ps-calm')==='1';}catch(e){}
  if(calmOn)document.body.classList.add('calm');
  var cb=document.getElementById('calmBtn');
  if(cb){
    if(calmOn)cb.classList.add('on');
    cb.addEventListener('click',function(){
      calmOn=!calmOn;
      document.body.classList.toggle('calm',calmOn);
      cb.classList.toggle('on',calmOn);
      try{localStorage.setItem('ps-calm',calmOn?'1':'0');}catch(e){}
    });
  }
  var keys=document.getElementById('keys');if(keys){var NK=30,cols=['g','v','b'],lit={},n=4+Math.floor(Math.random()*3);
    while(Object.keys(lit).length<n){lit[Math.floor(Math.random()*NK)]=cols[Math.floor(Math.random()*3)];}
    for(var i=0;i<NK;i++){var nb=(i%7===2||i%7===6);var k=document.createElement('div');k.className='key'+(nb?' nb':'')+(lit[i]?(' '+lit[i]):'');if(!nb){var b=document.createElement('div');b.className='bk';k.appendChild(b);}keys.appendChild(k);}}
  var stage=document.querySelector('.stage');var gl=['♪','♫','♩','♬'];var live=0;
  // Skip the floating-note animation entirely in perf/calm mode: a note moving
  // behind the glass forces every backdrop-filter panel to re-blur each frame,
  // which is what makes the Retina MacBook screen lag. Low-DPI monitors keep it.
  if(!perfOn)
  setInterval(function(){if(document.hidden||live>6||!stage||document.body.classList.contains('calm')||document.body.classList.contains('perf'))return;var e=document.createElement('div');e.className='note';e.textContent=gl[Math.random()*4|0];
    e.style.left=(5+Math.random()*88)+'%';e.style.fontSize=(16+Math.random()*16)+'px';e.style.animation='rise '+(8+Math.random()*5).toFixed(1)+'s linear forwards';
    stage.appendChild(e);live++;e.addEventListener('animationend',function(){e.remove();live--;});},1500);
  var glass=document.getElementById('glass');
  if(glass&&!perfOn){window.addEventListener('mousemove',function(ev){if(document.body.classList.contains('calm')||document.body.classList.contains('perf'))return;var x=ev.clientX/innerWidth-.5,y=ev.clientY/innerHeight-.5;
    glass.style.transform='rotateX('+(-y*4).toFixed(2)+'deg) rotateY('+(x*5).toFixed(2)+'deg)';});}
})();
</script>"""

# Additive style layer for the live-update UI (progress, logs, modal, toast,
# quota meter, calm mode, sparklines, skeletons, mobile). Kept separate from
# STYLE_V6 so the base theme stays untouched.
STYLE_LIVE = r"""<style>
  /* run progress */
  .runprog{display:none;margin:0 0 14px}
  .runprog.on{display:block}
  .runprog .track{height:10px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}
  .runprog .fill{height:100%;width:0%;border-radius:999px;background:linear-gradient(90deg,#4ade80,#7dd3fc);box-shadow:0 0 16px rgba(74,222,128,.5);transition:width .6s cubic-bezier(.2,.7,.2,1)}
  .runprog .fill.indet{width:38%;transition:none;animation:indet 1.5s ease-in-out infinite}
  @keyframes indet{0%{margin-left:-38%}100%{margin-left:100%}}
  .runprog .lbl{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:8px;font-size:12.5px;color:var(--muted);font-weight:600;min-height:22px}
  .btn.stop{background:rgba(255,93,120,.12);color:#ff9dae;border-color:rgba(255,93,120,.35);padding:6px 13px}
  .btn.stop:hover{background:rgba(255,93,120,.22)}

  /* quota meter */
  .quota{margin-top:14px}
  .quota .qrow{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px}
  .quota .ql{color:var(--text);font-size:13px;font-weight:700;opacity:.85}
  .quota .qv{font-size:12.5px;font-weight:800;color:var(--green)}
  .quota .qv.warn{color:#fcd34d}
  .quota .qv.hot{color:#fb7185}
  .quota .track{height:7px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}
  .quota .fill{height:100%;width:0%;border-radius:999px;background:linear-gradient(90deg,#4ade80,#fcd34d);transition:width .5s}
  .quota .fill.hot{background:#fb7185}

  /* log feed */
  .logwrap{margin-top:14px}
  .logbar{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .logbar .ll{color:var(--muted);font-size:12px;font-weight:700}
  .lastrun{margin-left:auto;color:var(--faint);font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .logbar button{flex:none;background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;font:700 12px var(--font);padding:3px 9px;cursor:pointer}
  .logbar button:hover{color:var(--text);background:rgba(255,255,255,.06)}
  .log .ln{padding:2px 0;color:#96a09b;white-space:pre-wrap;word-break:break-word}
  .log .ln .t{color:var(--faint);margin-right:8px}
  .log .ln.ok{color:#7ef0a4}
  .log .ln.err{color:#ff8a9c}
  .log .ln.clip{color:#8fc2ff}
  .log details.rg{margin:4px 0;border-left:2px solid rgba(255,255,255,.1);padding-left:8px}
  .log details.rg[open]{border-left-color:rgba(30,215,96,.45)}
  .log details.rg summary{cursor:pointer;list-style:none;color:#cfd6e0;font-weight:700;padding:2px 0}
  .log details.rg summary::-webkit-details-marker{display:none}
  .log details.rg summary::before{content:"▸ ";color:var(--faint)}
  .log details.rg[open] summary::before{content:"▾ "}
  .chiplet{display:inline-block;margin-left:8px;font-size:10.5px;font-weight:800;padding:1px 8px;border-radius:999px;background:rgba(74,222,128,.15);color:#8ef0ab}
  .chiplet.live{background:rgba(125,211,252,.16);color:#a8ddff;animation:pulse 2s infinite}
  .log.full{position:fixed;inset:20px;z-index:990;max-height:none;background:rgba(5,8,7,.97);font-size:13px;padding:20px 22px;box-shadow:0 30px 90px rgba(0,0,0,.7)}

  /* toast */
  .toast{position:fixed;right:18px;bottom:18px;z-index:1000;max-width:min(420px,86vw);background:rgba(12,24,17,.88);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid rgba(74,222,128,.5);color:var(--text);padding:13px 17px;border-radius:16px;box-shadow:0 18px 50px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.2);font-size:13.5px;font-weight:600;opacity:0;transform:translateY(10px);transition:.35s;pointer-events:none}
  .toast.on{opacity:1;transform:none}
  .toast.err{border-color:rgba(251,113,133,.6);background:rgba(26,12,16,.88)}

  /* confirm modal */
  .modal-back{position:fixed;inset:0;z-index:995;background:rgba(3,5,8,.68);backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);display:none;place-items:center;padding:20px}
  .modal-back.on{display:grid}
  .modal{width:min(460px,94vw);border-radius:22px;background:rgba(20,22,38,.92);backdrop-filter:blur(28px) saturate(150%);-webkit-backdrop-filter:blur(28px) saturate(150%);border:1px solid rgba(255,255,255,.22);padding:24px;box-shadow:0 30px 90px rgba(0,0,0,.65),inset 0 1px 0 rgba(255,255,255,.25)}
  .modal h3{margin:0 0 10px;font-size:17px;font-weight:800}
  .modal .sub{font-size:13.5px;line-height:1.55}
  .modal .sub b{color:var(--green)}
  .modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}

  /* calm mode */
  .calmbtn{display:inline-flex;align-items:center;gap:7px;flex:none;background:none;border:1px solid var(--line);color:var(--muted);border-radius:999px;font:700 12px var(--font);padding:7px 13px;cursor:pointer;transition:.18s}
  .calmbtn:hover{color:var(--text);background:rgba(255,255,255,.06)}
  .calmbtn.on{background:rgba(139,108,255,.16);color:#b8a5ff;border-color:transparent}
  .calmbtn svg{width:13px;height:13px}
  body.calm .note{display:none}
  body.calm .blob{opacity:.16}
  body.calm .mark,body.calm .now-card .big-np,body.calm .wkey.live,body.calm .pill.ready .d,body.calm .chiplet.live{animation:none}
  body.calm .eq rect{animation:none!important}
  body.calm .glass{transform:none!important}
  body.calm .grain{opacity:.02}

  /* skeleton shimmer */
  .skel{position:relative;overflow:hidden;background:rgba(255,255,255,.05);border-radius:6px;height:11px;margin:7px 0}
  .skel::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.08),transparent);animation:shim 1.2s infinite}
  @keyframes shim{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
  body.calm .skel::after{animation:none}
  .alb .cover{background-color:#0e1118;background-image:linear-gradient(110deg,#0e1118 30%,#161a24 50%,#0e1118 70%)}

  /* sparklines */
  .spark{display:block;margin-top:9px;opacity:.95}

  /* mobile */
  @media(max-width:640px){
    .bar{flex-wrap:wrap;gap:8px;padding:10px 12px;border-radius:24px}
    .pills{margin-left:0;flex:1;flex-wrap:nowrap;overflow-x:auto;padding-bottom:2px;-webkit-overflow-scrolling:touch}
    .pills::-webkit-scrollbar{display:none}
    .page{padding:18px 12px 70px}
    .topline{flex-direction:column;align-items:flex-start}
    .top-actions{width:100%;flex-wrap:wrap}
    .hero-num{font-size:clamp(38px,11vw,56px)}
    .kpi .val{font-size:24px}
    .podium{grid-template-columns:1fr}
    .pod.gold{transform:none}
    .pod:hover,.pod.gold:hover{transform:none}
    .log.full{inset:8px}
    .toast{left:12px;right:12px;max-width:none}
  }
</style>"""

# Client-side live updater: polls GET /api/status and patches the DOM in place
# (KPI numbers, run pill, progress bar + Stop, quota meter, colored log feed,
# finish toast + browser notification, Clip+Upload confirm modal). This replaces
# the old full-page 5s auto-reload; _busy_autoreload() remains as the no-JS
# fallback only.
LIVE_SCRIPT = r"""<script>
(function(){
  'use strict';
  var calm=function(){return document.body.classList.contains('calm');};
  var $=function(id){return document.getElementById(id);};
  var fmtInt=function(v){return (v==null?0:v).toLocaleString('en-US');};
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}

  /* ---- count-up ---- */
  function animateNum(el,from,to,ms){
    if(calm()||from===to){el.textContent=fmtInt(to);return;}
    var t0=null;
    function step(ts){if(!t0)t0=ts;var p=Math.min(1,(ts-t0)/ms);p=1-Math.pow(1-p,3);
      el.textContent=fmtInt(Math.round(from+(to-from)*p));
      if(p<1)requestAnimationFrame(step);}
    requestAnimationFrame(step);
  }
  function firstCountUp(){
    if(calm())return;
    document.querySelectorAll('.hero-num,.kpi .val,.chip .v').forEach(function(el){
      var raw=(el.textContent||'').trim();
      if(!/^[\d,]+$/.test(raw))return;
      var v=parseInt(raw.replace(/,/g,''),10);
      if(!isFinite(v)||v<=0)return;
      el._v=v;el._init=true;animateNum(el,Math.floor(v*0.35),v,700);
    });
  }

  /* ---- toast + notification ---- */
  var toastEl=null,toastTimer=null;
  function toast(msg,isErr){
    if(!toastEl){toastEl=document.createElement('div');toastEl.className='toast';document.body.appendChild(toastEl);}
    toastEl.textContent=msg;toastEl.classList.toggle('err',!!isErr);
    requestAnimationFrame(function(){toastEl.classList.add('on');});
    clearTimeout(toastTimer);toastTimer=setTimeout(function(){toastEl.classList.remove('on');},6500);
  }
  function notify(msg){
    try{if('Notification' in window&&Notification.permission==='granted'){new Notification('Piano Shorts',{body:msg});}}catch(e){}
  }

  /* ---- colored, grouped log feed ---- */
  var logEl=$('liveLog');
  function lineClass(m){
    if(/upload failed|error|traceback|quota|upload limit|failed:/i.test(m))return 'err';
    if(/uploaded .* as youtube video|automation complete|stats refreshed|stats auto-refreshed|saved \d|finished run/i.test(m))return 'ok';
    if(/generated clip|generating clips|discovered video|moved processed|scanning for videos/i.test(m))return 'clip';
    return 'dim';
  }
  function pretty(m){
    m=m.replace(/^Uploaded (\S+?)\.mp4 as YouTube video (\S+)\s*$/,function(_,c,id){return '✔ Uploaded '+c+' → '+id.slice(0,5)+'…';});
    m=m.replace(/^Upload failed for (\S+?)\.mp4[:\s]*(.*)$/,function(_,c,rest){rest=rest.trim();return '✘ Upload failed '+c+(rest?' — '+rest.slice(0,60):'');});
    m=m.replace(/^Generated clip .*?(clip_\d+\.mp4)\s*$/,'Generated $1');
    m=m.replace(/^Moved processed source .*?([^\/]+) to .*$/,'Moved $1 → uploaded/');
    return m;
  }
  function lineHtml(l){
    var m=l.slice(8).replace(/^\s+/,'');
    var cls=lineClass(m);  // classify on the raw message, then condense it
    return '<div class="ln '+cls+'"><span class="t">'+esc(l.slice(0,5))+'</span>'+esc(pretty(m))+'</div>';
  }
  function summaryChip(l){
    var m=l.match(/clips_generated=(\d+)[\s\S]*uploads_completed=(\d+)[\s\S]*upload_failures=(\d+)/);
    if(!m)return '';
    return '<span class="chiplet">'+m[1]+' clips · '+m[2]+' uploads · '+m[3]+' fail'+(m[3]==='1'?'':'s')+'</span>';
  }
  function renderLog(lines){
    if(!logEl||!lines)return;
    var html='',groups=[],cur=null,pre=[];
    lines.forEach(function(l){
      if(l.indexOf('Starting YouTube Shorts automation')>-1){cur={lines:[l],done:null};groups.push(cur);return;}
      if(cur){cur.lines.push(l);if(l.indexOf('Finished run:')>-1){cur.done=l;cur=null;}}
      else pre.push(l);
    });
    pre.forEach(function(l){html+=lineHtml(l);});
    groups.forEach(function(g,i){
      var open=(i===groups.length-1)?' open':'';
      var head=g.lines[0]||'';
      var chip=g.done?summaryChip(g.done):'<span class="chiplet live">running</span>';
      html+='<details class="rg"'+open+'><summary><span class="t">'+esc(head.slice(0,8))+'</span>Run'+chip+'</summary>'
        +g.lines.map(lineHtml).join('')+'</details>';
    });
    var nearBottom=!logEl._painted||(logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight<48);
    logEl.innerHTML=html||'<div class="ln dim">No dashboard activity yet.</div>';
    logEl._painted=true;
    if(nearBottom)logEl.scrollTop=logEl.scrollHeight;
  }
  var fullBtn=$('logFullBtn');
  if(fullBtn&&logEl){fullBtn.addEventListener('click',function(){
    var on=logEl.classList.toggle('full');
    fullBtn.textContent=on?'✕ Close':'⛶ Expand';
    logEl.scrollTop=logEl.scrollHeight;});}

  /* ---- live DOM patching ---- */
  function setNum(el,v){
    v=Math.round(Number(v)||0);
    var from=(typeof el._v==='number')?el._v:v;
    if(from===v&&el._init)return;
    el._v=v;el._init=true;
    animateNum(el,from,v,600);
  }
  function patchValues(s){
    document.querySelectorAll('[data-live]').forEach(function(el){
      var k=el.getAttribute('data-live');
      if(k in s)setNum(el,s[k]);
    });
    document.querySelectorAll('[data-live-text]').forEach(function(el){
      var k=el.getAttribute('data-live-text');
      if(k in s&&el.textContent!==String(s[k]))el.textContent=s[k];
    });
  }
  function etaTxt(sec){
    if(sec>=90)return '~'+Math.round(sec/60)+' min left';
    return '~'+Math.max(5,Math.round(sec/5)*5)+' sec left';
  }
  function runLabel(r){
    if(r.stopping)return 'Stopping run…';
    var p=r.phase;
    if(p==='clipping')return 'Clipping… '+(r.clips_made||0)+' clip'+((r.clips_made||0)===1?'':'s')+' generated';
    if(p==='uploading'){
      var total=r.total>0?r.total:'?';
      var cur=Math.min((r.done||0)+1,r.total||((r.done||0)+1));
      var txt='Uploading clip '+cur+' of '+total;
      if(r.eta_seconds>0)txt+=' · '+etaTxt(r.eta_seconds);
      if(r.fails>0)txt+=' · '+r.fails+' failed';
      return txt;
    }
    if(p==='finishing')return 'Finishing up…';
    return 'Starting run…';
  }
  function patchRun(s){
    var r=s.run||{};
    var pill=$('livePill');
    if(pill)pill.textContent=r.running?'Running':(r.stats_running?'Refreshing stats…':'Ready');
    var state=$('liveState');
    if(state)state.textContent=r.running?'Pipeline running…':(r.stats_running?'Refreshing YouTube stats…':'Pipeline idle · ready');
    var prog=$('runProg'),fill=$('runFill'),lbl=$('runLabel'),stopF=$('stopForm');
    if(prog){
      var busy=!!(r.running||r.stats_running);
      prog.classList.toggle('on',busy);
      if(fill){
        if(r.running&&r.total>0&&r.phase==='uploading'){
          fill.classList.remove('indet');
          fill.style.width=Math.min(100,Math.round((r.done||0)/r.total*100))+'%';
        }else{fill.classList.add('indet');}
      }
      if(lbl)lbl.textContent=r.running?runLabel(r):(r.stats_running?'Refreshing YouTube stats…':'');
      if(stopF)stopF.style.display=r.running?'':'none';
    }
    var q=s.quota,qf=$('quotaFill'),qt=$('quotaTxt');
    if(q&&qf){qf.style.width=Math.min(100,Math.round(q.used/q.cap*100))+'%';qf.classList.toggle('hot',!!q.exhausted);}
    if(q&&qt){
      qt.textContent=q.exhausted?(q.used+' of '+q.cap+' · daily limit hit'):(q.used+' of '+q.cap+' · '+q.remaining+' left');
      qt.classList.toggle('hot',!!q.exhausted);
      qt.classList.toggle('warn',!q.exhausted&&q.remaining<=Math.max(1,Math.floor(q.cap/10)));
    }
    var lr=s.last_run,le=$('lastRun');
    if(le&&lr&&lr.when){
      le.textContent='Last run '+lr.when.slice(11,16)+' · '+lr.clips_generated+' clips · '
        +lr.uploads_completed+' uploads · '+lr.upload_failures+' fail'+(lr.upload_failures===1?'':'s');
    }
  }

  /* ---- run/refresh finish detection ---- */
  var prevSeq=null,prevRunning=false,prevStats=false;
  function checkFinish(s){
    var r=s.run||{};
    if(prevSeq===null){prevSeq=r.run_seq;prevRunning=!!r.running;prevStats=!!r.stats_running;return;}
    if(r.run_seq!==prevSeq){
      var wasRun=prevRunning,wasStats=prevStats;
      prevSeq=r.run_seq;
      if(!r.running&&!r.stats_running){
        if(wasRun){
          var msg;
          if(r.stopped)msg='Run stopped — '+(r.done||0)+' upload'+((r.done||0)===1?'':'s')+' completed first';
          else if(r.quota_hit)msg='Run paused — YouTube daily upload limit hit';
          else if(r.last_error)msg='Run finished with errors — check the log';
          else msg='Run finished — '+(r.done||0)+' upload'+((r.done||0)===1?'':'s')
            +((r.fails||0)>0?(' · '+r.fails+' failed'):'')
            +((r.clips_made||0)>0?(' · '+r.clips_made+' new clips'):'');
          toast(msg,!!(r.last_error||r.quota_hit));notify(msg);
          setTimeout(function(){location.reload();},2400);
        }else if(wasStats){
          toast('YouTube stats refreshed');
          setTimeout(function(){location.reload();},1500);
        }
      }
    }
    prevRunning=!!r.running;prevStats=!!r.stats_running;
  }

  /* ---- polling ---- */
  var FAST=2000,SLOW=4500,timer=null,busyNow=false;
  function poll(){
    fetch('/api/status',{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){
      busyNow=!!(s.run&&(s.run.running||s.run.stats_running));
      patchValues(s);patchRun(s);checkFinish(s);
      if(s.log)renderLog(s.log);
      var rf=$('runForm');
      if(rf&&typeof s.pending_uploads==='number')rf.setAttribute('data-pending',s.pending_uploads);
    }).catch(function(){}).then(schedule);
  }
  function schedule(){
    clearTimeout(timer);
    timer=setTimeout(function(){
      if(document.hidden){schedule();return;}
      poll();
    },busyNow?FAST:SLOW);
  }
  document.addEventListener('visibilitychange',function(){if(!document.hidden){clearTimeout(timer);poll();}});

  /* ---- async owner actions (no page reloads) ---- */
  function asyncPost(url,params){
    return fetch(url,{method:'POST',headers:{'X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'},body:params||''});
  }
  var stopF=$('stopForm');
  if(stopF)stopF.addEventListener('submit',function(e){
    e.preventDefault();
    asyncPost('/stop','redirect_to=/overview').then(function(){toast('Stopping run…');poll();});
  });
  document.querySelectorAll('form[action="/refresh-stats"]').forEach(function(f){
    f.addEventListener('submit',function(e){
      e.preventDefault();
      asyncPost('/refresh-stats','redirect_to=/overview').then(function(){toast('Refreshing YouTube stats…');busyNow=true;poll();});
    });
  });
  var runForm=$('runForm'),modal=$('runModal');
  if(runForm&&modal){
    var txt=$('runModalText'),okBtn=$('runConfirm'),noBtn=$('runCancel');
    runForm.addEventListener('submit',function(e){
      e.preventDefault();
      var n=parseInt(runForm.getAttribute('data-pending')||'0',10);
      var slot=runForm.getAttribute('data-first-slot')||'';
      if(txt){
        txt.innerHTML=n>0
          ?('This will clip any new input videos, then upload <b>'+n+'</b> pending clip'+(n===1?'':'s')
            +' to YouTube as private, scheduled videos'+(slot?', publishing from <b>'+esc(slot)+'</b>':'')+'. Continue?')
          :'No clips are waiting right now — this run will clip any new input videos and upload whatever they produce. Continue?';
      }
      modal.classList.add('on');
    });
    if(noBtn)noBtn.addEventListener('click',function(){modal.classList.remove('on');});
    modal.addEventListener('click',function(e){if(e.target===modal)modal.classList.remove('on');});
    document.addEventListener('keydown',function(e){if(e.key==='Escape')modal.classList.remove('on');});
    if(okBtn)okBtn.addEventListener('click',function(){
      try{if('Notification' in window&&Notification.permission==='default')Notification.requestPermission();}catch(e){}
      modal.classList.remove('on');
      asyncPost('/run','redirect_to=/overview').then(function(){toast('Run started');busyNow=true;poll();});
    });
  }

  function init(){firstCountUp();poll();}
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',init);}else{init();}
})();
</script>"""

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


def svg_bar_chart(labels: list, data: list, colors: list, height: int = 260) -> str:
    """Server-rendered inline SVG bar chart — always renders, no JS/CDN needed.

    Vision styling: bars use a vertical fade gradient; text inherits the page's
    Apple system font stack (no font-family on the svg, so it cascades in).
    """
    W, pad_l, pad_r, pad_t, pad_b = 720, 46, 14, 14, 32
    plot_w = W - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(data) or 1
    maxv = max(data) if data and max(data) > 0 else 1
    slot = plot_w / n
    barw = min(42, slot * 0.62)
    base = pad_t + plot_h

    # One <linearGradient> per distinct bar color (deterministic ids so repeated
    # charts on a page never collide with each other).
    distinct = []
    for c in colors or ["#4ade80"]:
        if c not in distinct:
            distinct.append(c)
    if not distinct:
        distinct = ["#4ade80"]

    def grad_id(color: str) -> str:
        return "vg" + "".join(ch for ch in color if ch.isalnum())

    defs = "".join(
        f'<linearGradient id="{grad_id(c)}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{c}"/><stop offset="1" stop-color="{c}" stop-opacity=".25"/>'
        f'</linearGradient>'
        for c in distinct
    )
    parts = [f"<defs>{defs}</defs>"]

    for frac in (0.0, 0.5, 1.0):
        y = pad_t + plot_h * (1 - frac)
        val = int(maxv * frac)
        lab = f"{val // 1000}k" if val >= 1000 else str(val)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="rgba(255,255,255,.07)"/>')
        parts.append(f'<text x="{pad_l - 7}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="rgba(235,240,255,.45)">{lab}</text>')

    for i, v in enumerate(data):
        c = colors[i] if i < len(colors) else "#4ade80"
        lb = str(labels[i]) if i < len(labels) else ""
        h = (v / maxv) * plot_h if maxv else 0
        x = pad_l + slot * i + (slot - barw) / 2
        y = base - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{barw:.1f}" height="{max(0, h):.1f}" rx="7" fill="url(#{grad_id(c)})">'
            f'<title>{html.escape(lb)}: {v:,} views</title></rect>'
        )
        parts.append(
            f'<text x="{x + barw / 2:.1f}" y="{height - pad_b + 14}" text-anchor="middle" font-size="9.5" fill="rgba(235,240,255,.5)">{html.escape(lb)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {W} {height}" '
        f'style="width:100%;height:auto;display:block;margin-top:14px;font-family:{APP_FONT_STACK}" '
        f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}</svg>'
    )


def svg_sparkline(values: list, color: str = "#4ade80", width: int = 132, height: int = 32) -> str:
    """Tiny server-rendered 7-day trend line for KPI tiles."""
    values = [max(0, int(v)) for v in values]
    if len(values) < 2 or max(values) <= 0:
        return ""
    maxv = max(values)
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = 2 + (i / (n - 1)) * (width - 4)
        y = height - 3 - (v / maxv) * (height - 8)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    area = f"2,{height - 2} " + poly + f" {width - 2},{height - 2}"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
        f'<polygon points="{area}" fill="{color}" opacity=".12"/>'
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _uploads_last_7_days() -> list[int]:
    """Uploads completed per day for the last 7 local days (oldest first)."""
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz).date()
    counts = {(today - timedelta(days=i)).isoformat(): 0 for i in range(6, -1, -1)}
    for record in read_upload_records():
        if record.status != "uploaded":
            continue
        parsed = parse_iso_datetime(record.upload_time)
        if not parsed:
            continue
        key = parsed.astimezone(tz).date().isoformat()
        if key in counts:
            counts[key] += 1
    return list(counts.values())


def _topbar(active: str) -> str:
    pills = "".join(
        f'<a class="{"on" if (key == active or (active == "dsci" and key == "stats")) else ""}" href="{href}">{html.escape(label)}</a>'
        for key, href, label in PILLS
    )
    calm_btn = (
        '<button class="calmbtn" id="calmBtn" type="button" '
        'title="Calm mode — stills animations and softens the glow" aria-label="Toggle calm mode">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>Calm</button>'
    )
    return (
        '<header class="bar">'
        f'<a class="backhome" href="/"><span class="dot">{BRAND_SVG}</span>Piano Shorts</a>'
        f'<nav class="pills">{pills}</nav>'
        + calm_btn +
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
        + FONT_HEAD + STYLE_V6 + STYLE_LIVE + head_extra
        + '</head><body class="tab">' + BG_MARKUP + _topbar(active)
        + '<main class="page shell">' + topline + body + '</main>'
        + '<div class="cbadge">Piano Shorts · Creator Analytics</div>'
        + _busy_autoreload() + BG_SCRIPT + LIVE_SCRIPT + '</body></html>'
    )


def _busy_autoreload() -> str:
    """No-JS fallback only: browsers without JavaScript still see fresh numbers
    via a meta refresh while a run/refresh is busy. JS clients get live partial
    updates from /api/status (LIVE_SCRIPT) instead of full page reloads."""
    if RUN_STATE.get("running") or RUN_STATE.get("stats_running"):
        return '<noscript><meta http-equiv="refresh" content="7"></noscript>'
    return ""


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
        '<title>Piano Shorts</title>' + FONT_HEAD + STYLE_V6 + STYLE_LIVE
        + '</head><body>' + BG_MARKUP + body + BG_SCRIPT + '</body></html>'
    )


# ---------------------------------------------------------------------------
# OVERVIEW  (/overview)
# ---------------------------------------------------------------------------
# Uploads the chosen videos via XHR so we can show a live % progress bar and,
# on completion, confirm the files actually landed in the input folder (reading
# the saved count the server reports back) before clipping kicks off.
UPLOAD_PROGRESS_SCRIPT = r"""<script>
(function(){
  var f=document.getElementById('upform'); if(!f) return;
  var file=document.getElementById('upfile'), prog=document.getElementById('upprog'),
      bar=document.getElementById('upbar'), msg=document.getElementById('upmsg');
  f.addEventListener('submit', function(e){
    if(!file || !file.files || !file.files.length){ return; }
    e.preventDefault();
    var xhr=new XMLHttpRequest();
    xhr.open('POST','/upload');
    xhr.setRequestHeader('X-Requested-With','fetch');
    prog.style.display='block'; bar.style.width='0%';
    bar.style.background='linear-gradient(90deg,#4ade80,#7dd3fc)';
    msg.style.color=''; msg.textContent='Uploading… 0%';
    xhr.upload.onprogress=function(ev){
      if(ev.lengthComputable){
        var p=Math.round(ev.loaded/ev.total*100);
        bar.style.width=p+'%';
        msg.textContent = p<100 ? ('Uploading… '+p+'%') : 'Saving to input folder…';
      }
    };
    xhr.onload=function(){
      var r={}; try{ r=JSON.parse(xhr.responseText); }catch(err){}
      bar.style.width='100%';
      if(r.ok && r.saved>0){
        msg.style.color='var(--green)';
        msg.textContent='✓ '+r.saved+' file'+(r.saved>1?'s':'')+' in input folder ('+r.input_count+' total) · clipping started';
        var ic=document.getElementById('inputCount'); if(ic) ic.textContent=r.input_count;
        file.value='';
        // No page reload: the live status poller picks up the clipping run,
        // shows progress, and patches the counts in place.
      } else {
        bar.style.background='#fb7185';
        msg.style.color='#fb7185';
        msg.textContent = (r && r.message) ? r.message : 'No valid .mp4/.mov files were saved.';
      }
    };
    xhr.onerror=function(){
      bar.style.background='#fb7185'; msg.style.color='#fb7185';
      msg.textContent='Upload failed — check the connection and try again.';
    };
    xhr.send(new FormData(f));
  });
})();
</script>"""


def render_overview() -> str:
    status = build_status()
    latest_rows = latest_video_stats()
    total_views = sum(parse_stat_int(r.get("view_count", "")) for r in latest_rows)
    hours = best_posting_hours()
    best_hour = str(hours[0]["hour"]) if hours else "—"
    delta = _today_views_delta()
    running = bool(status["run"]["running"])
    stats_running = bool(status["run"].get("stats_running"))
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
    colors = ["#c4b5fd" if r.get("fill") else "#4ade80" for r in rows1d]
    chart_svg = svg_bar_chart(labels, data, colors)

    log_lines = live_dashboard_log_lines(40)
    log_html = "".join(
        f'<div class="ln {_log_line_class(l[8:].lstrip())}"><span class="t">{html.escape(l[:5])}</span>'
        f'{html.escape(_pretty_log_message(l[8:180].lstrip()))}</div>'
        for l in log_lines
    ) or '<div class="skel" style="width:82%"></div><div class="skel" style="width:64%"></div><div class="skel" style="width:73%"></div>'

    # Confirm-modal facts: how many clips a full run would upload, and the first
    # publish slot the scheduler would give them.
    pending = count_pending_clips()
    try:
        first_slot = format_queue_time(generate_schedule(1)[0].isoformat()) if pending else ""
    except Exception:  # noqa: BLE001 - a scheduling hiccup must not break the page
        first_slot = ""
    quota = quota_status()
    quota_pct = min(100, round(int(quota["used"]) / int(quota["cap"]) * 100))
    if quota["exhausted"]:
        quota_txt = f'{quota["used"]} of {quota["cap"]} · daily limit hit'
        quota_cls = " hot"
    elif int(quota["remaining"]) <= max(1, int(quota["cap"]) // 10):
        quota_txt = f'{quota["used"]} of {quota["cap"]} · {quota["remaining"]} left'
        quota_cls = " warn"
    else:
        quota_txt = f'{quota["used"]} of {quota["cap"]} · {quota["remaining"]} left'
        quota_cls = ""
    busy = running or stats_running

    run_progress = (
        f'<div class="runprog{" on" if busy else ""}" id="runProg">'
        '<div class="track"><div class="fill indet" id="runFill"></div></div>'
        '<div class="lbl"><span id="runLabel">'
        + ("Run in progress…" if running else ("Refreshing YouTube stats…" if stats_running else ""))
        + '</span>'
        '<form class="inline" action="/stop" method="post" data-owner-only id="stopForm"'
        + ('' if running else ' style="display:none"') +
        '><input type="hidden" name="redirect_to" value="/overview">'
        '<button class="btn stop" type="submit">■ Stop</button></form>'
        '</div></div>'
    )
    quota_meter = (
        '<div class="quota" data-owner-only>'
        '<div class="qrow"><span class="ql">YouTube uploads today</span>'
        f'<span class="qv{quota_cls}" id="quotaTxt">{quota_txt}</span></div>'
        f'<div class="track"><div class="fill{" hot" if quota["exhausted"] else ""}" id="quotaFill" style="width:{quota_pct}%"></div></div>'
        '</div>'
    )
    log_feed = (
        '<div class="logwrap" data-owner-only>'
        '<div class="logbar"><span class="ll">Live activity log</span>'
        '<span class="lastrun" id="lastRun"></span>'
        '<button type="button" id="logFullBtn">⛶ Expand</button></div>'
        f'<div class="log" id="liveLog">{log_html}</div></div>'
    )
    confirm_modal = (
        '<div class="modal-back" id="runModal" data-owner-only>'
        '<div class="modal"><h3>Start Clip + Upload?</h3>'
        '<p class="sub" id="runModalText"></p>'
        '<div class="modal-actions">'
        '<button class="btn" type="button" id="runCancel">Cancel</button>'
        '<button class="btn primary" type="button" id="runConfirm">Yes, start the run</button>'
        '</div></div></div>'
    )

    body = (
        '<div class="row r-2 mt">'
        '<div class="panel"><p class="eyebrow" style="color:var(--muted)">Total channel views</p>'
        f'<div class="hero-num" data-live="total_views">{total_views:,}</div>'
        f'<div class="sub" style="margin-top:8px">+<b style="color:var(--green)" data-live="today_views">{delta:,}</b> overnight · best hour <b style="color:var(--green)">{html.escape(best_hour)}</b> · today’s gains below</div>'
        f'{chart_svg}</div>'
        '<div class="panel"><h3>Live activity</h3>'
        + run_progress + quota_meter +
        '<div class="now-card" style="margin-top:14px"><div class="big-np"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></div>'
        f'<div><b style="font-size:15px" id="liveState">{"Pipeline running…" if running else ("Refreshing YouTube stats…" if stats_running else "Pipeline idle · ready")}</b><div class="sub" style="margin-top:2px">Drop videos in input, then Clip + Upload</div></div></div>'
        '<form id="upform" class="upzone" action="/upload" method="post" enctype="multipart/form-data" data-owner-only>'
        '<input type="hidden" name="upload_action" value="upload_and_clip">'
        '<input id="upfile" type="file" name="videos" accept=".mp4,.mov" multiple style="color:var(--muted);font-size:12px;margin-bottom:10px"><br>'
        '<button class="btn primary" type="submit">Upload &amp; clip</button>'
        '<div id="upprog" style="display:none;margin-top:12px">'
        '<div style="height:9px;border-radius:6px;background:rgba(255,255,255,.09);overflow:hidden">'
        '<div id="upbar" style="height:100%;width:0%;background:linear-gradient(90deg,#4ade80,#7dd3fc);transition:width .15s"></div></div>'
        '<div id="upmsg" class="sub" style="margin-top:7px;font-size:12px">Uploading… 0%</div></div>'
        '</form>'
        + log_feed + '</div>'
        '</div>'
        '<div class="row r-4 mt">'
        f'<div class="chip"><div class="l">Input videos</div><div class="v" id="inputCount" data-live="input_count">{input_count}</div></div>'
        f'<div class="chip"><div class="l">Clips ready</div><div class="v" data-live="clip_count">{clip_count:,}</div></div>'
        f'<div class="chip"><div class="l">Next post</div><div class="v" style="font-size:15px" data-live-text="next_post">{next_post}</div></div>'
        f'<div class="chip"><div class="l">Uploaded today</div><div class="v" data-live="uploaded_today">{uploaded_today}</div></div>'
        '</div>'
        + confirm_modal + UPLOAD_PROGRESS_SCRIPT
    )
    pill_label = "Running" if running else ("Refreshing stats…" if stats_running else "Ready")
    ready = '<span class="pill ready"><span class="d"></span><span id="livePill">' + pill_label + '</span></span>'
    top_actions = ready + (
        f'<form class="inline" action="/run" method="post" data-owner-only id="runForm" '
        f'data-pending="{len(pending)}" data-first-slot="{html.escape(first_slot)}">'
        '<input type="hidden" name="redirect_to" value="/overview">'
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
    colors = ["#4ade80"] * len(data)
    chart_svg = svg_bar_chart(labels, data, colors)

    def kpi(cls, lab, val, delta_txt, dcolor="", live="", spark=""):
        style = f' style="color:{dcolor}"' if dcolor else ""
        live_attr = f' data-live="{live}"' if live else ""
        return (
            f'<div class="kpi {cls}"><div class="lab">{lab}</div><div class="val"{live_attr}>{val}</div>'
            f'<div class="d"{style}>{delta_txt}</div>{spark}</div>'
        )

    views_7d = [int(r.get("views", 0)) for r in rows1w]
    kpis = (
        '<div class="row r-4 mt">'
        + kpi("kc-g", "Total Views", f"{total_views:,}", f'▲ <span data-live="today_views">{delta:,}</span> today',
              live="total_views", spark=svg_sparkline(views_7d, "#4ade80"))
        + kpi("kc-b", "Tracked Clips", f"{tracked:,}", f'<span data-live="clip_count">{clips_ready:,}</span> clips ready')
        + kpi("kc-t", "Upload Records", f"{uploads:,}", f'▲ <span data-live="uploaded_today">{uploaded_today}</span> today',
              live="upload_records", spark=svg_sparkline(_uploads_last_7_days(), "#5eead4"))
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
TIKTOK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l3-1v10"/><circle cx="6" cy="18" r="3"/><path d="M14 7c1.5 2 4 2.5 6 2.5"/></svg>'
DOWNLOAD_CLIP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>'


def render_tiktok_candidates_page(selected_date: str = "") -> str:
    _ = selected_date
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    medal = ["\U0001f947", "\U0001f948", "\U0001f949"]
    cls = ["gold", "silver", "bronze"]

    def download_btn(clip_filename: str) -> str:
        if not clip_filename:
            return ''
        exists = (config.CLIPS_DIR / clip_filename).exists()
        if not exists:
            return '<div class="sub" style="margin-top:12px;font-size:11.5px">clip file not on disk</div>'
        href = "/clips/" + quote(clip_filename)
        return (
            f'<a class="btn primary" href="{html.escape(href)}" download '
            'style="width:100%;justify-content:center;margin-top:12px">'
            + DOWNLOAD_CLIP_ICON + 'Download clip</a>'
        )

    tiktok_connected = tiktok.is_connected()

    def post_btn(clip_filename: str, caption: str, compact: bool = False) -> str:
        # Owner-only per-clip action, shown under every clip. When an account is
        # connected it uploads the clip to the creator's TikTok inbox as a DRAFT
        # via /tiktok/post (that form is hidden from public viewers by CSS); the
        # creator finishes and posts from the TikTok app. When not connected it
        # routes the owner through Connect first. `compact` renders a small inline
        # button for the archive rows. All variants carry data-owner-only.
        if not clip_filename or not (config.CLIPS_DIR / clip_filename).exists():
            return ''
        label = 'Send' if compact else 'Send to TikTok'
        icon = '' if compact else TIKTOK_ICON
        if compact:
            btn_style = 'padding:4px 10px;font-size:12px'
            wrap_style = 'display:inline'
        else:
            btn_style = 'width:100%;justify-content:center'
            wrap_style = 'margin-top:8px;width:100%'
        if not tiktok_connected:
            a_style = btn_style if compact else 'width:100%;justify-content:center;margin-top:8px'
            return (
                f'<a class="btn" data-owner-only href="/tiktok/connect" style="{a_style}" '
                f'title="Connect your TikTok account first">{icon}{label}</a>'
            )
        return (
            f'<form class="inline" action="/tiktok/post" method="post" data-owner-only '
            "onsubmit=\"return confirm('Send this clip to your TikTok inbox as a draft? "
            "You will finish the caption and post it from the TikTok app.');\" "
            f'style="{wrap_style}">'
            f'<input type="hidden" name="clip_filename" value="{html.escape(clip_filename)}">'
            f'<input type="hidden" name="caption" value="{html.escape(caption)}">'
            f'<button class="btn" type="submit" style="{btn_style}">{icon}{label}</button>'
            '</form>'
        )

    all_days = tiktok_candidate_days()
    blocks = []
    for day in all_days:
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
            clip_fn = str(c["clip_filename"])
            pods.append(
                f'<div class="pod {cls[i]}"><div class="medal">{medal[i]}</div><div class="w">{title}</div>'
                f'<div class="c">{html.escape(clip_fn)}</div>'
                f'<div class="met"><b>{int(c["views"]):,}</b> views · <b>{int(c["likes"]):,}</b> likes</div>'
                f'{download_btn(clip_fn)}{post_btn(clip_fn, _clean_title(str(c["title"]), clip_fn))}</div>'
            )
        while len(pods) < 3:
            pods.append('<div class="pod"><div class="met sub">—</div></div>')
        ordered = pods[1] + pods[0] + pods[2]
        age = (today - d).days
        if age == 0:
            qualifier = "Today"
        elif age == 1:
            qualifier = "Yesterday"
        else:
            qualifier = d.strftime("%A")  # weekday name for the rest of the week
        label = f'<b>{html.escape(qualifier)}</b> · {html.escape(format_stats_date(str(day["date"])))}'
        blocks.append(f'<div class="day-block"><h4>{label}</h4><div class="podium">{ordered}</div></div>')

    grid = "".join(blocks) or '<div class="panel"><div class="placeholder"><h3>No candidates in the past week</h3><div>Once clips have 24h of stats they appear here.</div></div></div>'

    # How-it-works strip: log in to TikTok, download a clip, post it yourself.
    how_panel = (
        '<div class="panel" style="margin-bottom:16px;display:flex;align-items:center;'
        'justify-content:space-between;gap:16px;flex-wrap:wrap">'
        '<div style="max-width:640px">'
        '<h3 style="margin:0 0 4px">Post them yourself, one tap at a time</h3>'
        '<span class="sub">Open the TikTok upload page and log in, then <b>Download clip</b> on any '
        'winner below and drag the file in. You decide what goes live — nothing posts automatically.</span>'
        '</div>'
        '<a class="btn primary" href="https://www.tiktok.com/tiktokstudio/upload" target="_blank" rel="noopener">'
        + TIKTOK_ICON + 'Open TikTok Upload</a>'
        '</div>'
    )

    # --- Owner-only TikTok connection panel + last-action banner -------------
    banner = ''
    if TIKTOK_RESULT.get("error"):
        banner = (
            '<div class="panel" data-owner-only style="margin-bottom:16px;border-left:3px solid #e5484d">'
            f'<b style="color:#e5484d">{html.escape(TIKTOK_RESULT["error"])}</b></div>'
        )
    elif TIKTOK_RESULT.get("message"):
        banner = (
            '<div class="panel" data-owner-only style="margin-bottom:16px;border-left:3px solid #30a46c">'
            f'<b style="color:#30a46c">{html.escape(TIKTOK_RESULT["message"])}</b></div>'
        )

    if tiktok_connected:
        disp = tiktok.connected_display()
        who = html.escape(disp.get("display_name") or "your TikTok account")
        connect_panel = (
            '<div class="panel" data-owner-only style="margin-bottom:16px;display:flex;'
            'align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap">'
            '<div style="max-width:640px">'
            f'<h3 style="margin:0 0 4px">Connected as {who} · draft upload is on</h3>'
            '<span class="sub">Hit <b>Send to TikTok</b> on any winner below and it uploads to your '
            'TikTok app as a <b>draft</b> — open TikTok, tap the notification, add a caption and post it '
            'when you want. Nothing goes live until you post it yourself.</span>'
            '</div>'
            '<a class="btn" href="/tiktok/disconnect">Disconnect</a>'
            '</div>'
        )
    else:
        connect_panel = (
            '<div class="panel" data-owner-only style="margin-bottom:16px;display:flex;'
            'align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap">'
            '<div style="max-width:640px">'
            '<h3 style="margin:0 0 4px">Connect your TikTok account</h3>'
            '<span class="sub">Authorize once to turn on one-click posting from this page. '
            f'Callback in use: <code>{html.escape(tiktok.redirect_uri())}</code></span>'
            '</div>'
            '<a class="btn primary" href="/tiktok/connect">' + TIKTOK_ICON + 'Connect TikTok</a>'
            '</div>'
        )

    # --- Collapsible full-history archive: top 5 winners per day -------------
    # Collapsed by default so it adds no visual noise and the browser doesn't
    # paint it until opened. Compact text rows (no video players) keep it fast.
    arch_days = []
    for day in all_days:
        cands = list(day["candidates"])[:5]
        if not cands:
            continue
        try:
            day_label = datetime.fromisoformat(str(day["date"])).date().strftime("%a · %b %d, %Y")
        except ValueError:
            day_label = str(day["date"])
        rows = []
        for i, c in enumerate(cands):
            clip_fn = str(c["clip_filename"])
            title = html.escape(_clean_title(str(c["title"]), clip_fn))
            exists = bool(clip_fn) and (config.CLIPS_DIR / clip_fn).exists()
            if exists:
                dl = (
                    f'<a class="btn" href="/clips/{quote(clip_fn)}" download '
                    'style="padding:4px 10px;font-size:12px">Download</a>'
                )
                post = post_btn(clip_fn, _clean_title(str(c["title"]), clip_fn), compact=True)
            else:
                dl = '<span class="sub" style="font-size:11px">no file</span>'
                post = ''
            rows.append(
                '<div style="display:flex;align-items:center;gap:10px;padding:7px 0;'
                'border-bottom:1px solid rgba(128,128,128,.18)">'
                f'<span style="width:26px;opacity:.6;font-weight:700">#{i + 1}</span>'
                '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;'
                f'white-space:nowrap">{title}</span>'
                f'<span class="sub" style="white-space:nowrap;font-size:12px">'
                f'{int(c["views"]):,} views · {int(c["likes"]):,} likes</span>'
                f'<span style="display:flex;gap:6px;flex-shrink:0">{dl}{post}</span>'
                '</div>'
            )
        arch_days.append(
            '<div style="margin:14px 0 4px;font-weight:700">' + html.escape(day_label) + '</div>'
            + ''.join(rows)
        )
    archive = ''
    if arch_days:
        archive = (
            '<details class="panel" style="margin-top:16px">'
            '<summary style="cursor:pointer;font-weight:700;font-size:15px">'
            'All past candidates · top 5 per day, full history</summary>'
            '<div class="sub" style="margin:6px 0 4px">Every winner ever, oldest days below newest. '
            'Post any of them privately with one tap — collapsed by default so it stays out of the way.</div>'
            + ''.join(arch_days)
            + '</details>'
        )

    body = banner + connect_panel + how_panel + grid + archive
    top_actions = (
        '<a class="btn" href="https://www.tiktok.com/tiktokstudio/upload" target="_blank" rel="noopener">'
        + TIKTOK_ICON + 'Open TikTok</a>'
        '<a class="btn" href="/tiktok-candidates.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>Download CSV</a>'
    )
    return render_page(
        "tiktok", "YouTube winners → TikTok", "TikTok Candidates",
        "The daily podium of clips worth reposting — download a winner and post it to TikTok yourself.",
        body, top_actions=top_actions,
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

    # Per-week cover = the highest-viewed clip's YouTube thumbnail, and clip counts.
    covers: dict[str, tuple[int, str]] = {}
    counts: dict[str, int] = {}
    for r in rows:
        wk = r.get("project_week", "")
        counts[wk] = counts.get(wk, 0) + 1
        vid = r.get("youtube_video_id", "")
        if vid:
            try:
                cv = int(float(r.get("current_views") or 0))
            except ValueError:
                cv = 0
            if wk not in covers or cv > covers[wk][0]:
                covers[wk] = (cv, vid)

    def phase_for(label: str) -> str:
        if label == "Week 0":
            return "Pre-project baseline"
        try:
            n = int(label.split()[1])
        except (IndexError, ValueError):
            return ""
        if n <= 2:
            return "Baseline"
        if n <= 4:
            return "Posting-time test"
        if n <= 6:
            return "Caption test"
        if n <= 8:
            return "Frequency test"
        return "Content type + capstone"

    pin = '<svg class="apin" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C8.7 2 6 4.7 6 8c0 4.5 6 12 6 12s6-7.5 6-12c0-3.3-2.7-6-6-6zm0 8.5A2.5 2.5 0 1112 5a2.5 2.5 0 010 5.5z"/></svg>'
    note = '<svg viewBox="0 0 24 24" fill="#04140a"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'

    def album_row(key: str, title: str, subtitle: str, count: int, cover_vid: str,
                  is_all: bool = False, future: bool = False) -> str:
        sel = (show_all and is_all) or (not show_all and key == selected_week and not is_all)
        cls = "arow" + (" sel" if sel else "") + (" all" if is_all else "") + (" future" if future else "")
        if is_all:
            cov = f'<div class="acov all">{note}</div>'
        elif cover_vid:
            cov = f'<div class="acov" style="background-image:url(\'https://i.ytimg.com/vi/{html.escape(cover_vid)}/mqdefault.jpg\')"></div>'
        else:
            cov = '<div class="acov empty"></div>'
        return (
            f'<a class="{cls}" href="{week_href("all" if is_all else key)}">{cov}'
            f'<div class="atx"><div class="att">{pin}{html.escape(title)}</div><div class="ast">{html.escape(subtitle)}</div></div>'
            f'<span class="acount">{count}</span></a>'
        )

    list_html = [album_row("all", "All weeks", f"Full experiment · {len(rows)} clips", len(rows), "", is_all=True)]
    for wk in weeks:
        c = counts.get(wk, 0)
        future = c == 0
        subtitle = "Upcoming" if future else f"{phase_for(wk)} · {c} clips"
        list_html.append(album_row(wk, wk, subtitle, c, covers.get(wk, (0, ""))[1], future=future))
    album_list = '<div class="ds-list">' + "".join(list_html) + '</div>'

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
        '<div class="top-actions" data-owner-only>'
        '<a class="btn" href="/project-data.csv"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>CSV (all weeks)</a>'
        '<a class="btn" href="/project-data.xlsx"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"/></svg>XLSX (all weeks)</a></div>'
    )
    return (
        '<div class="panel mt"><div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px">'
        '<h3 style="margin:0">Experiment library <span style="color:var(--faint);font-weight:600;font-size:12px">· pick a week like a playlist</span></h3>'
        f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap"><span class="sub" style="margin:0">{scope} · {total:,} clips</span>{downloads}</div></div>'
        f'<div class="ds-split">{album_list}'
        f'<div class="ds-scroll"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div></div></div>'
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
    start_snapshot_scheduler()
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
