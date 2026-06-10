"""Local web dashboard for the Shorts automation app."""

from __future__ import annotations

import cgi
import csv
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

import config
from generate_clips import ensure_directories
from logging_setup import configure_logging
from stats_tracker import best_posting_hours, latest_video_stats, read_stats_history, refresh_youtube_stats_history
from tracker import update_tracker
from youtube_upload import get_youtube_service, read_upload_records
from generate_metadata import read_metadata
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8000
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
HOME_RECENT_CLIP_LIMIT = 10
APP_FONT_STACK = (
    '"CircularSp", "Circular Std", "Avenir Next", "Helvetica Neue", '
    'Helvetica, Arial, sans-serif'
)
RUN_STATE = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "last_output": "",
    "last_error": "",
}


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

    def do_GET(self) -> None:
        """Handle dashboard GET routes."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_html(render_dashboard())
            return

        if path == "/api/status":
            self.send_json(build_status())
            return

        if path == "/tracker":
            self.send_html(render_tracker_page())
            return

        if path == "/queue":
            self.send_html(render_queue_page())
            return

        if path == "/tiktok-candidates":
            query = parse_qs(parsed.query)
            selected_date = query.get("date", [""])[0]
            self.send_html(render_tiktok_candidates_page(selected_date))
            return

        if path == "/stats":
            query = parse_qs(parsed.query)
            selected_range = query.get("range", ["1d"])[0]
            self.send_html(render_stats_page(selected_range))
            return

        if path == "/tracker.csv":
            self.send_file(config.METADATA_DIR / "video_tracker.csv", download=True)
            return

        if path == "/tracker.xlsx":
            self.send_file(config.METADATA_DIR / "video_tracker.xlsx", download=True)
            return

        if path.startswith("/clips/"):
            filename = Path(unquote(path.removeprefix("/clips/"))).name
            self.send_file(config.CLIPS_DIR / filename)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        """Handle dashboard POST routes."""
        parsed = urlparse(self.path)

        if parsed.path == "/upload":
            self.handle_upload()
            return

        if parsed.path == "/run":
            start_run()
            self.redirect(self.redirect_back_path(default="/"))
            return

        if parsed.path == "/clip-only":
            start_clip_only()
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
        logger.info("dashboard: " + format, *args)


def render_dashboard() -> str:
    """Render the dashboard HTML."""
    status = build_status()
    all_clips = list_clip_files()
    clips = all_clips[:HOME_RECENT_CLIP_LIMIT]
    upload_running = bool(status["run"]["running"])
    run_label = "Running..." if upload_running else "Run Now"
    run_disabled = "disabled" if upload_running else ""

    clip_markup = "\n".join(render_clip_card(path) for path in clips)
    if not clip_markup:
        clip_markup = '<p class="muted">No clips yet.</p>'
    hero_preview = render_hero_preview(clips[:3])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Piano Shorts Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1d1d1f;
      --muted: #6e6e73;
      --line: rgba(29, 29, 31, .12);
      --paper: #f5f5f7;
      --panel: rgba(255, 255, 255, .82);
      --panel-solid: #ffffff;
      --accent: #0071e3;
      --accent-dark: #0077ed;
      --ok: #008f7a;
      --warn: #b26a00;
      --stage: #0b0b0d;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: {APP_FONT_STACK};
      color: var(--ink);
      background: var(--paper);
      letter-spacing: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid rgba(255, 255, 255, .12);
      background: rgba(22, 22, 24, .82);
      backdrop-filter: saturate(180%) blur(22px);
      -webkit-backdrop-filter: saturate(180%) blur(22px);
      color: #f5f5f7;
    }}
    .shell {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 18px 28px;
    }}
    .top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
      font-weight: 560;
      letter-spacing: 0;
    }}
    .brand-mark {{
      width: 28px;
      height: 28px;
      display: grid;
      place-items: center;
      border-radius: 999px;
      background: linear-gradient(145deg, #ffffff, #d7d7dc);
      color: #111;
      box-shadow: inset 0 -1px 2px rgba(0, 0, 0, .18);
      font-weight: 560;
    }}
    h1 {{
      font-size: clamp(42px, 7vw, 88px);
      line-height: .96;
      margin: 0;
      font-weight: 620;
      letter-spacing: 0;
    }}
    h2 {{
      font-size: 17px;
      line-height: 1.2;
      margin: 0 0 16px;
      font-weight: 620;
    }}
    .section-heading {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 16px;
    }}
    .section-heading h2 {{ margin: 0; }}
    .section-heading span {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .hero {{
      color: #f5f5f7;
      background:
        radial-gradient(circle at 50% 18%, rgba(255,255,255,.22), transparent 16%),
        radial-gradient(circle at 78% 34%, rgba(0,113,227,.28), transparent 24%),
        linear-gradient(180deg, #09090b 0%, #121318 78%, #f5f5f7 78%);
      min-height: 520px;
      overflow: hidden;
    }}
    .hero .shell {{
      padding-top: 58px;
      padding-bottom: 42px;
      text-align: center;
    }}
    .hero-copy {{
      max-width: 780px;
      margin: 0 auto;
    }}
    .kicker {{
      margin: 0 0 12px;
      color: rgba(245,245,247,.68);
      font-size: 18px;
      font-weight: 430;
    }}
    .subtitle {{
      margin: 18px auto 0;
      max-width: 720px;
      color: rgba(245,245,247,.82);
      font-size: clamp(19px, 2vw, 27px);
      line-height: 1.18;
      font-weight: 430;
    }}
    .hero-actions {{
      display: flex;
      justify-content: center;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 28px;
    }}
    .hero-actions .button, .hero-actions button {{
      width: auto;
      min-width: 132px;
      padding: 0 20px;
      border-radius: 999px;
    }}
    .ghost {{
      background: transparent;
      border-color: #2997ff;
      color: #2997ff;
    }}
    .ghost:hover {{ background: rgba(41, 151, 255, .12); }}
    .hero-preview {{
      position: relative;
      display: flex;
      justify-content: center;
      align-items: flex-end;
      gap: clamp(12px, 4vw, 44px);
      min-height: 270px;
      margin-top: 46px;
      perspective: 900px;
    }}
    .device {{
      width: clamp(120px, 18vw, 220px);
      padding: 8px;
      border-radius: 28px;
      background: linear-gradient(145deg, #313138, #060607);
      box-shadow: 0 26px 80px rgba(0, 0, 0, .52), inset 0 0 0 1px rgba(255,255,255,.12);
      transition: transform .28s ease, filter .28s ease;
    }}
    .device:hover {{ transform: translateY(-10px) rotateX(2deg); filter: brightness(1.08); }}
    .device:nth-child(1) {{ transform: rotate(-5deg) translateY(22px); }}
    .device:nth-child(2) {{ width: clamp(150px, 21vw, 260px); }}
    .device:nth-child(3) {{ transform: rotate(5deg) translateY(22px); }}
    .device video {{
      border-radius: 20px;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.08);
    }}
    main .shell {{
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 22px;
      align-items: start;
      padding-top: 8px;
      padding-bottom: 48px;
    }}
    main section, .clip-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 18px 46px rgba(0, 0, 0, .06);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
    }}
    main section {{ padding: 20px; }}
    .stack {{
      display: grid;
      gap: 16px;
      position: sticky;
      top: 84px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 13px;
      min-height: 74px;
      background: rgba(255, 255, 255, .68);
    }}
    .stat strong {{
      display: block;
      font-size: 28px;
      line-height: 1;
      letter-spacing: 0;
    }}
    .stat span, .muted {{
      color: var(--muted);
      font-size: 13px;
    }}
    .actions {{
      display: grid;
      gap: 10px;
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 44px;
      width: 100%;
      border: 1px solid var(--accent);
      border-radius: 14px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      font-weight: 560;
      text-decoration: none;
      cursor: pointer;
      transition: transform .18s ease, background .18s ease, border-color .18s ease, box-shadow .18s ease;
    }}
    button:hover, .button:hover {{
      background: var(--accent-dark);
      transform: translateY(-1px);
      box-shadow: 0 12px 26px rgba(0, 113, 227, .24);
    }}
    button:disabled {{
      cursor: wait;
      opacity: .6;
    }}
    .secondary {{
      background: #fff;
      color: var(--accent);
    }}
    .secondary:hover {{
      background: #eef4ff;
    }}
    input[type="file"] {{
      width: 100%;
      border: 1px dashed var(--line);
      border-radius: 18px;
      padding: 18px;
      background: rgba(255, 255, 255, .66);
    }}
    .clips {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 16px;
    }}
    .clip-card {{
      overflow: hidden;
      min-width: 0;
      transition: transform .22s ease, box-shadow .22s ease;
    }}
    .clip-card:hover {{
      transform: translateY(-6px);
      box-shadow: 0 24px 56px rgba(0, 0, 0, .12);
    }}
    video {{
      display: block;
      width: 100%;
      aspect-ratio: 9 / 16;
      background: #111;
      object-fit: contain;
    }}
    .clip-name {{
      padding: 12px 14px;
      font-size: 12px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .status-line {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-size: 13px;
      color: rgba(245,245,247,.72);
    }}
    .pill {{
      color: #fff;
      background: var(--ok);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .pill.warn {{ background: var(--warn); }}
    .run-meta {{
      margin-top: 12px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      max-height: 180px;
      overflow: auto;
      padding: 12px;
      border-radius: 16px;
      background: rgba(0, 0, 0, .045);
    }}
    @media (max-width: 820px) {{
      main .shell {{ grid-template-columns: 1fr; }}
      .stack {{ position: static; }}
      .top {{ align-items: flex-start; flex-direction: column; }}
      .hero .shell {{ padding-top: 38px; }}
      .hero-preview {{ min-height: 220px; }}
    }}
    :root {{
      color-scheme: dark;
      --ink: #f5f5f5;
      --muted: #b3b3b3;
      --line: rgba(255, 255, 255, .09);
      --paper: #121212;
      --panel: #181818;
      --panel-solid: #181818;
      --accent: #1ed760;
      --accent-dark: #1fdf64;
      --ok: #1ed760;
      --warn: #ffa42b;
      --stage: #121212;
    }}
    body {{
      font-family: {APP_FONT_STACK};
      font-weight: 430;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 0%, rgba(30, 215, 96, .18), transparent 30%),
        linear-gradient(180deg, #1f2b29 0, #121212 360px);
    }}
    header {{
      border-bottom: 1px solid rgba(255,255,255,.06);
      background: rgba(0, 0, 0, .78);
    }}
    .brand-mark {{
      background: #1ed760;
      color: #050505;
      font-weight: 560;
    }}
    .hero {{
      min-height: 460px;
      background:
        radial-gradient(circle at 50% 8%, rgba(255,255,255,.12), transparent 18%),
        radial-gradient(circle at 68% 34%, rgba(30, 215, 96, .22), transparent 30%),
        linear-gradient(180deg, #15201d 0%, #121212 100%);
    }}
    .hero .shell {{
      text-align: left;
      display: grid;
      grid-template-columns: minmax(0, .9fr) minmax(360px, 1.1fr);
      gap: 42px;
      align-items: center;
    }}
    .hero-copy {{ max-width: 620px; margin: 0; }}
    .kicker {{ color: #1ed760; font-weight: 560; font-size: 14px; text-transform: uppercase; }}
    h1 {{ font-size: clamp(46px, 6vw, 82px); font-weight: 620; }}
    .subtitle {{ margin-left: 0; color: #d7d7d7; font-size: clamp(18px, 1.6vw, 24px); }}
    .hero-actions {{ justify-content: flex-start; }}
    .hero-actions .button, .hero-actions button {{
      background: #1ed760;
      border-color: #1ed760;
      color: #050505;
      font-weight: 560;
    }}
    .hero-actions .ghost {{
      background: rgba(255,255,255,.08);
      border-color: rgba(255,255,255,.12);
      color: #fff;
    }}
    .hero-preview {{
      justify-content: flex-end;
      min-height: 330px;
      margin-top: 0;
    }}
    .device {{
      border-radius: 16px;
      background: #000;
      box-shadow: 0 30px 90px rgba(0,0,0,.65);
    }}
    .device video {{ border-radius: 10px; }}
    main .shell {{
      max-width: 1440px;
      grid-template-columns: 360px 1fr;
      padding-top: 26px;
    }}
    main section, .clip-card {{
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025));
      border-color: rgba(255,255,255,.08);
      border-radius: 14px;
      box-shadow: none;
    }}
    h2 {{ color: #fff; font-size: 22px; }}
    .stat {{
      background: #242424;
      border-color: rgba(255,255,255,.06);
      border-radius: 10px;
    }}
    .stat strong {{ color: #fff; }}
    .stat span, .muted, .clip-name {{ color: var(--muted); }}
    button, .button {{
      border-radius: 999px;
      background: #1ed760;
      border-color: #1ed760;
      color: #050505;
      font-weight: 560;
    }}
    button:hover, .button:hover {{
      background: #3be477;
      transform: scale(1.02);
      box-shadow: none;
    }}
    .secondary {{
      background: #242424;
      border-color: #242424;
      color: #fff;
    }}
    .secondary:hover {{ background: #303030; }}
    input[type="file"] {{
      color: #d7d7d7;
      background: #242424;
      border-color: rgba(255,255,255,.12);
    }}
    .album-actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .album-action {{
      position: relative;
      min-height: 128px;
      display: flex;
      align-items: flex-end;
      justify-content: flex-start;
      overflow: hidden;
      padding: 14px;
      border: 0;
      border-radius: 12px;
      color: #fff;
      text-align: left;
      text-decoration: none;
      font-size: 14px;
      font-weight: 560;
      background: #282828;
      cursor: pointer;
      transition: transform .18s ease, background .18s ease;
    }}
    .album-action:hover {{ transform: translateY(-3px); background: #333; }}
    .album-action::after {{
      content: "";
      position: absolute;
      right: -24px;
      bottom: -22px;
      width: 86px;
      height: 86px;
      border-radius: 10px;
      background:
        linear-gradient(135deg, rgba(255,255,255,.22), transparent),
        var(--cover, linear-gradient(135deg, #1ed760, #005c38));
      transform: rotate(18deg);
      box-shadow: 0 12px 24px rgba(0,0,0,.35);
    }}
    .album-action span {{ position: relative; z-index: 1; max-width: 120px; }}
    .cover-run {{ --cover: linear-gradient(135deg, #1ed760, #137b43); }}
    .cover-stats {{ --cover: linear-gradient(135deg, #00d4ff, #2554ff); }}
    .cover-refresh {{ --cover: linear-gradient(135deg, #f43f5e, #7c3aed); }}
    .cover-queue {{ --cover: linear-gradient(135deg, #1ed760, #0ea5e9); }}
    .cover-tiktok {{ --cover: linear-gradient(135deg, #1ed760, #ff0050); }}
    .album-action:disabled {{ opacity: .55; cursor: wait; }}
    .upload-album {{
      display: grid;
      gap: 12px;
    }}
    .upload-drop {{
      min-height: 128px;
      display: grid;
      place-items: center;
      gap: 8px;
      border-radius: 12px;
      background:
        linear-gradient(135deg, rgba(255,255,255,.10), rgba(255,255,255,.02)),
        linear-gradient(135deg, #3b2f63, #10251c);
      cursor: pointer;
      text-align: center;
      padding: 14px;
    }}
    .upload-drop input {{ width: 100%; max-width: 220px; }}
    .upload-title {{ font-size: 16px; font-weight: 560; }}
    .upload-buttons {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }}
    .clips {{ grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }}
    .clip-card {{
      border-radius: 10px;
      background: #181818;
      padding: 10px;
    }}
    .clip-card video {{ border-radius: 8px; }}
    .clip-name {{ padding: 10px 2px 0; }}
    pre {{ background: #0b0b0b; color: #c7c7c7; }}
    @media (max-width: 980px) {{
      .hero .shell {{ grid-template-columns: 1fr; text-align: center; }}
      .hero-copy {{ margin: 0 auto; }}
      .subtitle {{ margin-left: auto; }}
      .hero-actions {{ justify-content: center; }}
      .hero-preview {{ justify-content: center; }}
    }}
    @media (max-width: 820px) {{
      .album-actions {{ grid-template-columns: 1fr; }}
    }}
    .dashboard-grid {{
      display: grid;
      gap: 18px;
    }}
    main .shell {{
      display: block;
      max-width: 1440px;
    }}
    .control-row {{
      display: grid;
      grid-template-columns: minmax(260px, .95fr) minmax(420px, 1.65fr) minmax(260px, .85fr);
      gap: 18px;
      align-items: stretch;
    }}
    .control-row section {{
      min-width: 0;
    }}
    .automation-wide {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .automation-wide .cover-run,
    .automation-wide .cover-refresh {{
      grid-column: auto;
      width: 100%;
    }}
    .status-card pre {{
      margin-top: 14px;
      background: rgba(0,0,0,.18);
    }}
    @media (max-width: 1120px) {{
      .control-row {{ grid-template-columns: 1fr 1fr; }}
      .status-card {{ grid-column: 1 / -1; }}
    }}
    @media (max-width: 760px) {{
      .control-row,
      .automation-wide, .upload-buttons {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="shell top">
      <div class="brand"><span class="brand-mark">♪</span><span>Piano Shorts</span></div>
      <div class="status-line">
        <span>{html.escape(str(config.INPUT_DIR))}</span>
        <span class="pill {'warn' if upload_running else ''}" data-run-pill>{'Running' if upload_running else 'Ready'}</span>
      </div>
    </div>
  </header>
  <section class="hero">
    <div class="shell">
      <div class="hero-copy">
        <p class="kicker">Creator automation for your piano videos</p>
        <h1>Piano Shorts Dashboard</h1>
        <p class="subtitle">Clip, schedule, track, and study your YouTube Shorts from one quiet studio interface.</p>
        <div class="hero-actions">
          <form action="/run" method="post">
            <button type="submit" data-run-primary {run_disabled}>{run_label}</button>
          </form>
          <a class="button ghost" href="/stats">View Stats</a>
          <a class="button ghost" href="#clips">Browse Clips</a>
        </div>
      </div>
      <div class="hero-preview">
        {hero_preview}
      </div>
    </div>
  </section>
  <main>
    <div class="shell">
      <div class="dashboard-grid">
      <div class="control-row">
        <section>
          <h2>Upload Videos</h2>
          <form action="/upload" method="post" enctype="multipart/form-data" class="upload-album">
            <label class="upload-drop">
              <span class="upload-title">Add Videos</span>
              <input type="file" name="videos" accept=".mp4,.mov,video/mp4,video/quicktime" multiple>
            </label>
            <div class="upload-buttons">
              <button type="submit" name="upload_action" value="input_only">Add To Input</button>
              <button type="submit" name="upload_action" value="upload_and_clip" data-run-lock {run_disabled}>Add + Clip Only</button>
            </div>
          </form>
        </section>
        <section>
          <h2>Automation</h2>
          <div class="automation-wide">
            <form action="/run" method="post">
              <button class="album-action cover-run" type="submit" data-run-primary {run_disabled}><span>{run_label}</span></button>
            </form>
            <form action="/clip-only" method="post">
              <button class="album-action cover-run" type="submit" data-run-lock {run_disabled}><span>Clip Input Only</span></button>
            </form>
            <a class="album-action cover-stats" href="/stats"><span>YouTube Stats</span></a>
            <a class="album-action cover-queue" href="/queue"><span>Queue</span></a>
            <a class="album-action cover-tiktok" href="/tiktok-candidates"><span>TikTok Candidates</span></a>
            <form action="/refresh-stats" method="post" class="refresh-form">
              <input type="hidden" name="redirect_to" value="/">
              <button class="album-action cover-refresh" type="submit" data-run-lock {run_disabled}><span>Refresh Stats</span></button>
            </form>
          </div>
        </section>
        <section class="status-card">
          <h2>Status</h2>
          <div class="stats">
            <div class="stat"><strong>{status['input_count']}</strong><span>input videos</span></div>
            <div class="stat"><strong>{status['clip_count']}</strong><span>clips</span></div>
            <div class="stat"><strong>{status['uploaded_sources']}</strong><span>processed videos</span></div>
            <div class="stat"><strong>{status['upload_records']}</strong><span>upload records</span></div>
          </div>
          <div class="run-meta">
            <span data-run-started>{'Started ' + html.escape(str(status['run']['started_at'])) if status['run']['started_at'] else 'Not started'}</span>
            <span data-run-finished>{'Finished ' + html.escape(str(status['run']['finished_at'])) if status['run']['finished_at'] else ''}</span>
          </div>
          <pre data-live-log>{html.escape(str(status['run']['last_output'] or status['run']['last_error'] or 'No dashboard run yet.'))}</pre>
        </section>
      </div>
      <section id="clips">
        <div class="section-heading">
          <h2>Recent Clips</h2>
          <span>Showing latest {len(clips)} of {len(all_clips)}</span>
        </div>
        <div class="clips">
          {clip_markup}
        </div>
      </section>
      </div>
    </div>
  </main>
  <script>
    async function refreshStatus() {{
      try {{
        const response = await fetch('/api/status', {{ cache: 'no-store' }});
        if (!response.ok) return;
        const status = await response.json();
        const run = status.run || {{}};
        const pill = document.querySelector('[data-run-pill]');
        const log = document.querySelector('[data-live-log]');
        const started = document.querySelector('[data-run-started]');
        const finished = document.querySelector('[data-run-finished]');
        const message = run.last_output || run.last_error || 'No dashboard run yet.';

        if (pill) {{
          pill.textContent = run.running ? 'Running' : 'Ready';
          pill.classList.toggle('warn', Boolean(run.running));
        }}
        document.querySelectorAll('[data-run-primary]').forEach((button) => {{
          button.disabled = Boolean(run.running);
          const label = run.running ? 'Running...' : 'Run Now';
          const span = button.querySelector('span');
          if (span) span.textContent = label;
          else button.textContent = label;
        }});
        document.querySelectorAll('[data-run-lock]').forEach((button) => {{
          button.disabled = Boolean(run.running);
        }});
        if (started) started.textContent = run.started_at ? `Started ${{run.started_at}}` : 'Not started';
        if (finished) finished.textContent = run.finished_at ? `Finished ${{run.finished_at}}` : '';
        if (log && log.textContent !== message) {{
          log.textContent = message;
          log.scrollTop = log.scrollHeight;
        }}
      }} catch (error) {{
        console.error(error);
      }}
    }}
    refreshStatus();
    window.setInterval(refreshStatus, 2000);
    document.querySelectorAll('.refresh-form').forEach((form) => {{
      form.addEventListener('submit', async (event) => {{
        event.preventDefault();
        const button = form.querySelector('button');
        const label = button ? button.textContent : '';
        if (button) {{
          button.disabled = true;
          button.textContent = 'Refreshing...';
        }}
        try {{
          await fetch(form.action, {{
            method: 'POST',
            body: new URLSearchParams(new FormData(form)),
            headers: {{ 'X-Requested-With': 'fetch' }},
          }});
          await refreshStatus();
        }} catch (error) {{
          console.error(error);
        }} finally {{
          if (button) {{
            button.disabled = false;
            button.textContent = label;
          }}
        }}
      }});
    }});
  </script>
</body>
</html>"""


def render_hero_preview(paths: list[Path]) -> str:
    """Render floating clip previews for the dashboard hero."""
    if not paths:
        return """
          <div class="device"><video controls preload="metadata"></video></div>
          <div class="device"><video controls preload="metadata"></video></div>
          <div class="device"><video controls preload="metadata"></video></div>
        """

    cards = []
    for path in paths:
        name = html.escape(path.name)
        cards.append(f'<div class="device"><video src="/clips/{name}" controls muted preload="metadata"></video></div>')
    return "\n".join(cards)


def render_tracker_page() -> str:
    """Render a browser-readable tracker table."""
    rows = read_tracker_rows()
    if not rows:
        body = '<p class="muted">No tracker rows yet. Run the automation first.</p>'
    else:
        headers = list(rows[0].keys())
        head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
        rendered_rows = []
        for row in rows[:300]:
            rendered_rows.append(
                "<tr>"
                + "".join(f"<td>{html.escape(str(row.get(header, '')))}</td>" for header in headers)
                + "</tr>"
            )
        body = f"""
          <div class="table-wrap">
            <table>
              <thead><tr>{head}</tr></thead>
              <tbody>{''.join(rendered_rows)}</tbody>
            </table>
          </div>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video Tracker</title>
  <style>
    :root {{
      color-scheme: dark;
      --ink: #f5f5f5;
      --muted: #b3b3b3;
      --line: rgba(255, 255, 255, .09);
      --paper: #121212;
      --panel: #181818;
      --accent: #1ed760;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: {APP_FONT_STACK};
      font-weight: 430;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 0%, rgba(30, 215, 96, .18), transparent 30%),
        linear-gradient(180deg, #1f2b29 0, #121212 360px);
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 58px 24px; }}
    .top {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 28px;
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 560;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: clamp(46px, 6vw, 78px);
      line-height: .96;
      margin: 0;
      letter-spacing: 0;
    }}
    a {{
      color: var(--ink);
      font-weight: 560;
      text-decoration: none;
    }}
    .links {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      padding: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.08);
    }}
    .links form {{ margin: 0; }}
    .links a {{
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      padding: 0 16px;
      border-radius: 999px;
      background: #242424;
    }}
    .links a:hover {{ background: #303030; transform: translateY(-1px); }}
    .links a:nth-child(2),
    .links a:nth-child(3) {{
      background: var(--accent);
      color: #050505;
    }}
    .table-wrap {{
      overflow: auto;
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025));
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 24px 70px rgba(0,0,0,.32);
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      min-width: 1600px;
      font-size: 12px;
    }}
    th, td {{
      border-bottom: 1px solid rgba(255,255,255,.07);
      padding: 11px 12px;
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #242424;
      color: var(--muted);
      z-index: 1;
      font-weight: 560;
    }}
    td {{ color: #d7d7d7; }}
    .muted {{
      color: var(--muted);
      padding: 24px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel);
    }}
    @media (max-width: 820px) {{
      .top {{ align-items: flex-start; flex-direction: column; }}
      .shell {{ padding-top: 36px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div>
        <p class="eyebrow">Automation records</p>
        <h1>Video Tracker</h1>
      </div>
      <div class="links">
        <a href="/">Dashboard</a>
        <a href="/queue">Queue</a>
        <a href="/tiktok-candidates">TikTok Candidates</a>
        <a href="/tracker.xlsx">Download Excel</a>
        <a href="/tracker.csv">Download CSV</a>
      </div>
    </div>
    {body}
  </div>
</body>
</html>"""


def render_queue_page() -> str:
    """Render uploaded and pending video queue."""
    rows = build_queue_rows()
    deferred_count = sum(1 for row in rows if row.get("status") == "deferred")
    run_running = bool(RUN_STATE["running"])
    retry_label = f"Retry Deferred ({deferred_count})" if deferred_count else "Run Uploads"
    retry_disabled = "disabled" if run_running else ""
    table_rows = "".join(render_queue_row(row) for row in rows)
    if not table_rows:
        table_rows = '<tr><td colspan="7" class="muted">No queue items yet.</td></tr>'
    message = render_queue_message(deferred_count)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video Queue</title>
  <style>
    :root {{
      color-scheme: dark;
      --ink: #f5f5f5;
      --muted: #b3b3b3;
      --line: rgba(255, 255, 255, .09);
      --panel: #181818;
      --accent: #1ed760;
      --warn: #ffa42b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: {APP_FONT_STACK};
      font-weight: 430;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 0%, rgba(30, 215, 96, .18), transparent 30%),
        linear-gradient(180deg, #1f2b29 0, #121212 360px);
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 58px 24px; }}
    .top {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 28px;
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 560;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: clamp(46px, 6vw, 78px);
      line-height: .96;
      margin: 0;
    }}
    .links {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      padding: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.08);
    }}
    .links form {{ margin: 0; }}
    .links a, .links button {{
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      padding: 0 16px;
      border-radius: 999px;
      background: #242424;
      color: var(--ink);
      font-weight: 560;
      text-decoration: none;
    }}
    .links button {{
      border: 0;
      background: var(--accent);
      color: #050505;
      cursor: pointer;
    }}
    .links a:hover, .links button:hover {{ background: #303030; transform: translateY(-1px); }}
    .links button:hover {{ background: #3be477; }}
    .links button:disabled {{ cursor: wait; opacity: .72; transform: none; }}
    .queue-panel {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025));
      box-shadow: 0 24px 70px rgba(0,0,0,.32);
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      min-width: 1120px;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid rgba(255,255,255,.07);
      padding: 13px 14px;
      text-align: left;
      vertical-align: middle;
      white-space: nowrap;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #242424;
      color: var(--muted);
      z-index: 1;
      font-weight: 560;
    }}
    td {{ color: #d7d7d7; }}
    tr {{ scroll-margin-top: 24px; }}
    tr:target {{
      background: rgba(30,215,96,.10);
      outline: 1px solid rgba(30,215,96,.35);
    }}
    tr.saved-row {{
      background: rgba(30,215,96,.10);
    }}
    .title-cell {{
      max-width: 340px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: #242424;
      color: #fff;
      font-size: 12px;
      font-weight: 560;
    }}
    .badge.future {{ background: rgba(30,215,96,.16); color: var(--accent); }}
    .badge.issue {{ background: rgba(255,164,43,.16); color: var(--warn); }}
    .youtube-link {{ color: var(--accent); font-weight: 560; text-decoration: none; }}
    form.privacy-form {{ display: flex; align-items: center; gap: 8px; margin: 0; }}
    select {{
      min-height: 36px;
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 999px;
      background: #242424;
      color: #fff;
      padding: 0 12px;
      font: inherit;
    }}
    button {{
      min-height: 36px;
      border: 1px solid var(--accent);
      border-radius: 999px;
      background: var(--accent);
      color: #050505;
      font-weight: 560;
      padding: 0 14px;
      cursor: pointer;
    }}
    button:hover {{ background: #3be477; transform: scale(1.02); }}
    button:disabled {{ cursor: wait; opacity: .72; transform: none; }}
    .inline-status {{
      color: var(--muted);
      font-size: 12px;
      min-width: 48px;
    }}
    .inline-status.ok {{ color: var(--accent); }}
    .inline-status.error {{ color: var(--warn); }}
    .message {{
      margin: 0 0 18px;
      padding: 14px 16px;
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 14px;
      background: rgba(30,215,96,.12);
      color: #d7d7d7;
    }}
    .message.error {{
      background: rgba(255,164,43,.14);
      color: #ffd9a0;
    }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 820px) {{
      .top {{ align-items: flex-start; flex-direction: column; }}
      .shell {{ padding-top: 36px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div>
        <p class="eyebrow">Publishing schedule</p>
        <h1>Video Queue</h1>
      </div>
      <div class="links">
        <a href="/">Dashboard</a>
        <a href="/stats">YouTube Stats</a>
        <a href="/tiktok-candidates">TikTok Candidates</a>
        <form action="/run" method="post">
          <button type="submit" {retry_disabled}>{html.escape('Running...' if run_running else retry_label)}</button>
        </form>
      </div>
    </div>
    {message}
    <div class="queue-panel">
      <table>
        <thead>
          <tr>
            <th>Clip</th>
            <th>Title</th>
            <th>Scheduled</th>
            <th>Status</th>
            <th>Privacy</th>
            <th>YouTube</th>
            <th>Edit Privacy</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
  <script>
    document.querySelectorAll('.privacy-form').forEach((form) => {{
      form.addEventListener('submit', async (event) => {{
        event.preventDefault();
        const button = form.querySelector('button');
        const select = form.querySelector('select');
        const status = form.querySelector('.inline-status');
        const row = form.closest('tr');
        const privacyCell = row ? row.querySelector('[data-privacy-cell]') : null;
        const previousText = button.textContent;
        const selectedLabel = select.options[select.selectedIndex].text;

        button.disabled = true;
        button.textContent = 'Saving';
        status.textContent = '';
        status.className = 'inline-status';

        try {{
          const response = await fetch(form.action, {{
            method: 'POST',
            body: new URLSearchParams(new FormData(form)),
            headers: {{ 'X-Requested-With': 'fetch' }},
          }});
          const data = await response.json();
          if (!response.ok || !data.ok) {{
            throw new Error(data.error || 'Save failed');
          }}

          if (privacyCell) privacyCell.textContent = data.privacy_status;
          select.value = data.privacy_status;
          status.textContent = 'Saved';
          status.classList.add('ok');
          if (row) {{
            row.classList.add('saved-row');
            window.setTimeout(() => row.classList.remove('saved-row'), 1400);
          }}
        }} catch (error) {{
          status.textContent = 'Error';
          status.classList.add('error');
          console.error(error);
        }} finally {{
          button.disabled = false;
          button.textContent = previousText;
        }}
      }});
    }});
  </script>
</body>
</html>"""


def render_queue_message(deferred_count: int = 0) -> str:
    """Render queue action feedback."""
    error = str(RUN_STATE.get("last_error") or "")
    output = str(RUN_STATE.get("last_output") or "")
    if "Privacy update failed" in error:
        friendly = (
            "Privacy update needs a fresh Google login. I reset the saved token when possible; "
            "click Save again and approve the YouTube permissions if Google asks."
        )
        return f'<div class="message error">{html.escape(friendly)}</div>'
    if "YOUTUBE DAILY UPLOAD LIMIT HIT" in output or "uploadLimitExceeded" in output or "exceeded the number of videos" in output:
        friendly = (
            "YouTube daily upload limit hit. Stop uploading for today; tomorrow, press Run Now "
            "to continue uploading the waiting clips."
        )
        return f'<div class="message error">{html.escape(friendly)}</div>'
    if deferred_count:
        friendly = (
            f"YouTube stopped accepting uploads before. {deferred_count} clip(s) are deferred; "
            "press Run Now tomorrow to retry them and continue the queue."
        )
        return f'<div class="message error">{html.escape(friendly)}</div>'
    if output.startswith("Updated "):
        return f'<div class="message">{html.escape(output)}</div>'
    return ""


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
                "scheduled_publish_time": "",
                "display_time": "Not scheduled",
                "status": "waiting",
                "privacy_status": "not uploaded",
                "sort_key": "9999-" + clip_path.name,
            }
        )

    return sorted(rows, key=lambda row: row["sort_key"])


def render_queue_row(row: dict[str, str]) -> str:
    """Render one queue row."""
    video_id = row.get("youtube_video_id", "")
    row_anchor = queue_row_anchor(row)
    privacy = row.get("privacy_status", "unknown")
    status = row.get("status", "")
    status_class = "future" if status == "scheduled" else "issue" if status in {"failed", "deferred", "waiting"} else ""
    youtube_link = (
        f'<a class="youtube-link" href="https://youtu.be/{html.escape(video_id)}" target="_blank">Open</a>'
        if video_id
        else '<span class="muted">Not uploaded</span>'
    )
    privacy_form = render_privacy_form(video_id, privacy, row_anchor) if video_id else '<span class="muted">Unavailable</span>'
    title = row.get("title") or "No title yet"

    return f"""
      <tr id="{html.escape(row_anchor)}">
        <td>{html.escape(row.get("clip_filename", ""))}</td>
        <td class="title-cell" title="{html.escape(title)}">{html.escape(title)}</td>
        <td>{html.escape(row.get("display_time", ""))}</td>
        <td><span class="badge {status_class}">{html.escape(status)}</span></td>
        <td data-privacy-cell>{html.escape(privacy)}</td>
        <td>{youtube_link}</td>
        <td>{privacy_form}</td>
      </tr>
    """


def queue_row_anchor(row: dict[str, str]) -> str:
    """Return a stable HTML anchor id for a queue row."""
    raw_value = row.get("youtube_video_id") or row.get("clip_filename") or "item"
    safe = "".join(char if char.isalnum() else "-" for char in raw_value).strip("-")
    return f"queue-{safe or 'item'}"


def render_privacy_form(video_id: str, current_privacy: str, row_anchor: str) -> str:
    """Render privacy edit form for a YouTube video."""
    options = []
    for value in ("private", "unlisted", "public"):
        selected = " selected" if value == current_privacy else ""
        options.append(f'<option value="{value}"{selected}>{value.title()}</option>')

    return f"""
      <form class="privacy-form" action="/queue/privacy" method="post">
        <input type="hidden" name="youtube_video_id" value="{html.escape(video_id)}">
        <input type="hidden" name="row_anchor" value="{html.escape(row_anchor)}">
        <select name="privacy_status">{"".join(options)}</select>
        <button type="submit">Save</button>
        <span class="inline-status" aria-live="polite"></span>
      </form>
    """


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


def render_tiktok_candidates_page(selected_date: str = "") -> str:
    """Render daily YouTube-filtered TikTok candidate schedules."""
    _ = selected_date
    days = tiktok_candidate_days()
    scheduled_by_date = read_tiktok_schedule_by_date()
    day_rows = "".join(render_tiktok_day_row(day, scheduled_by_date.get(day["date"], [])) for day in days)
    if not day_rows:
        day_rows = '<p class="muted">No candidates yet. Refresh YouTube Stats after a posting day to rank clips.</p>'

    scheduled_count = sum(len(rows) for rows in scheduled_by_date.values())
    ready_count = sum(1 for day in days if not scheduled_by_date.get(day["date"], []))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TikTok Candidates</title>
  <style>
    :root {{
      color-scheme: dark;
      --ink: #f5f5f5;
      --muted: #b3b3b3;
      --line: rgba(255, 255, 255, .09);
      --accent: #1ed760;
      --panel: #181818;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: {APP_FONT_STACK};
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 0%, rgba(30, 215, 96, .18), transparent 30%),
        linear-gradient(180deg, #1f2b29 0, #121212 360px);
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 48px 24px 64px; }}
    .top {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 26px;
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 560;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: clamp(42px, 5.8vw, 76px);
      line-height: .96;
      margin: 0;
    }}
    h2 {{ margin: 0; font-size: 22px; }}
    a {{ color: var(--ink); text-decoration: none; font-weight: 560; }}
    .links {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      padding: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.08);
    }}
    .links a {{
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      padding: 0 16px;
      border-radius: 999px;
      background: #242424;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 420px;
      gap: 24px;
      align-items: start;
      margin-bottom: 24px;
    }}
    .strategy, .summary {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025));
      padding: 22px;
    }}
    .strategy p {{
      margin: 12px 0 0;
      color: #d7d7d7;
      font-size: 17px;
      max-width: 820px;
    }}
    .formula {{
      display: inline-flex;
      margin-top: 18px;
      padding: 10px 14px;
      border-radius: 999px;
      background: #242424;
      color: var(--accent);
      font-weight: 560;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin-top: 14px;
    }}
    .summary-stat {{
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255,255,255,.04);
    }}
    .summary-stat strong {{ display: block; font-size: 24px; }}
    .summary-stat span {{ color: var(--muted); font-size: 12px; }}
    .day-list {{
      display: grid;
      gap: 18px;
    }}
    .day-row {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #101010;
      overflow: hidden;
    }}
    .day-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px;
      border-bottom: 1px solid rgba(255,255,255,.07);
    }}
    .day-title strong {{
      display: block;
      font-size: 22px;
      margin-bottom: 4px;
    }}
    .day-title span {{ color: var(--muted); font-size: 13px; }}
    .candidate-strip {{
      display: grid;
      grid-template-columns: repeat(5, minmax(150px, 1fr));
      gap: 12px;
      padding: 18px;
    }}
    .candidate {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #181818;
      padding: 12px;
    }}
    .rank {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      border-radius: 999px;
      background: var(--accent);
      color: #050505;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .score {{ color: var(--accent); font-size: 12px; margin-top: 8px; }}
    .score strong {{ display: block; font-size: 22px; }}
    .score span {{ color: var(--muted); font-size: 12px; }}
    .title {{
      font-size: 15px;
      font-weight: 560;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .clip {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 5px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-top: 12px;
    }}
    .metric {{
      border-radius: 10px;
      background: #242424;
      padding: 8px;
    }}
    .metric strong {{ font-size: 13px; }}
    .metric span {{ color: var(--muted); font-size: 11px; }}
    .metric strong {{ display: block; }}
    .schedule-box {{
      min-width: 230px;
      display: grid;
      gap: 10px;
      justify-items: end;
    }}
    .schedule-times {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      color: var(--muted);
      font-size: 12px;
    }}
    .time-chip {{
      padding: 6px 9px;
      border-radius: 999px;
      background: #242424;
      color: #d7d7d7;
    }}
    .status-chip {{
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(30,215,96,.14);
      color: var(--accent);
      font-size: 12px;
      font-weight: 560;
    }}
    .button, button {{
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 14px;
      border-radius: 999px;
      background: #242424;
      border: 0;
      color: #fff;
      font: inherit;
      font-weight: 560;
      cursor: pointer;
    }}
    .button.primary, button.primary {{
      background: var(--accent);
      color: #050505;
    }}
    button:disabled {{
      cursor: default;
      opacity: .58;
    }}
    .muted {{
      color: var(--muted);
      padding: 24px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel);
    }}
    @media (max-width: 980px) {{
      .top, .hero-grid {{ grid-template-columns: 1fr; flex-direction: column; align-items: flex-start; }}
      .summary-grid {{ grid-template-columns: 1fr; }}
      .day-head {{ align-items: flex-start; flex-direction: column; }}
      .candidate-strip {{ grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }}
      .schedule-box {{ justify-items: start; }}
      .schedule-times {{ justify-content: flex-start; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div>
        <p class="eyebrow">YouTube winners for TikTok</p>
        <h1>TikTok Candidates</h1>
      </div>
      <div class="links">
        <a href="/">Dashboard</a>
        <a href="/stats">YouTube Stats</a>
        <a href="/queue">Queue</a>
      </div>
    </div>
    <div class="hero-grid">
      <section class="strategy">
        <h2>Daily TikTok export plan</h2>
        <p>Each completed YouTube stats day gets a row of its top five Shorts. Press the schedule button for that row when you want those candidates queued for TikTok, one per hour starting at 10 AM.</p>
        <div class="formula">score = views + likes x 25 + comments x 50</div>
      </section>
      <aside class="summary">
        <h2>Schedule Queue</h2>
        <div class="summary-grid">
          <div class="summary-stat"><strong>{len(days)}</strong><span>ranked days</span></div>
          <div class="summary-stat"><strong>{ready_count}</strong><span>ready days</span></div>
          <div class="summary-stat"><strong>{scheduled_count}</strong><span>queued clips</span></div>
        </div>
      </aside>
    </div>
    <div class="day-list">
      {day_rows}
    </div>
  </div>
</body>
</html>"""


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


def render_tiktok_day_row(day: dict[str, object], scheduled_rows: list[dict[str, str]]) -> str:
    """Render one daily TikTok candidate row."""
    stats_date = str(day["date"])
    candidates = list(day["candidates"])  # type: ignore[arg-type]
    candidate_markup = "".join(
        render_tiktok_candidate_card(candidate, index) for index, candidate in enumerate(candidates, 1)
    )
    total_views = sum(int(candidate["views"]) for candidate in candidates)
    total_likes = sum(int(candidate["likes"]) for candidate in candidates)
    schedule_date = next_tiktok_schedule_date(stats_date)
    time_chips = "".join(f'<span class="time-chip">{html.escape(label)}</span>' for label in tiktok_schedule_labels(schedule_date, len(candidates)))

    if scheduled_rows:
        times = ", ".join(format_tiktok_schedule_time(row.get("scheduled_time", "")) for row in scheduled_rows[:5])
        action_markup = f"""
          <div class="status-chip">Queued for TikTok</div>
          <div class="schedule-times">{html.escape(times)}</div>
        """
    else:
        action_markup = f"""
          <form action="/tiktok-schedule" method="post">
            <input type="hidden" name="stats_date" value="{html.escape(stats_date)}">
            <button class="primary" type="submit">Schedule All To TikTok</button>
          </form>
          <div class="schedule-times">{time_chips}</div>
        """

    return f"""
      <section class="day-row" id="day-{html.escape(stats_date)}">
        <div class="day-head">
          <div class="day-title">
            <strong>{html.escape(format_stats_date(stats_date))}</strong>
            <span>{len(candidates)} candidates • {total_views:,} views • {total_likes:,} likes</span>
          </div>
          <div class="schedule-box">
            {action_markup}
          </div>
        </div>
        <div class="candidate-strip">
          {candidate_markup}
        </div>
      </section>
    """


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


def render_tiktok_candidate_card(candidate: dict[str, str | int | float], index: int) -> str:
    """Render one TikTok candidate card."""
    title = str(candidate["title"]).split("#", 1)[0].strip() or str(candidate["clip_filename"])
    clip_filename = str(candidate["clip_filename"])

    return f"""
      <article class="candidate">
        <div class="rank">{index}</div>
        <div class="title" title="{html.escape(title)}">{html.escape(title)}</div>
        <div class="clip">{html.escape(clip_filename)}</div>
        <div class="metrics">
          <div class="metric"><strong>{int(candidate["views"]):,}</strong><span>views</span></div>
          <div class="metric"><strong>{int(candidate["likes"]):,}</strong><span>likes</span></div>
          <div class="metric"><strong>{float(candidate["like_rate"]):.1f}%</strong><span>like rate</span></div>
        </div>
        <div class="score">{int(candidate["score"]):,} score</div>
      </article>
    """


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


def render_stats_page(selected_range: str = "1d") -> str:
    """Render YouTube stats dashboard."""
    latest_rows = latest_video_stats()
    hour_rows = best_posting_hours()
    selected_range = normalize_stats_range(selected_range)
    hourly_rows = hourly_total_views(selected_range)

    total_views = sum(parse_stat_int(row.get("view_count", "")) for row in latest_rows)
    total_likes = sum(parse_stat_int(row.get("like_count", "")) for row in latest_rows)
    total_comments = sum(parse_stat_int(row.get("comment_count", "")) for row in latest_rows)
    public_count = sum(1 for row in latest_rows if row.get("privacy_status", "") == "public")
    best_hour = str(hour_rows[0]["hour"]) if hour_rows else "No data"
    top_video_markup = render_top_video_list(latest_rows)

    table_rows = "".join(render_stats_table_row(row) for row in latest_rows[:200])
    if not table_rows:
        table_rows = '<tr><td colspan="9" class="muted">No YouTube stats yet. Upload videos, then click Refresh YouTube Stats.</td></tr>'

    hour_markup = "".join(render_hour_bar(row) for row in hour_rows)
    if not hour_markup:
        hour_markup = '<p class="muted">No posting-hour data yet.</p>'

    chart_svg = render_hourly_views_chart(hourly_rows)
    range_tabs = render_range_tabs(selected_range)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Stats</title>
  <style>
    :root {{
      color-scheme: dark;
      --ink: #f5f5f5;
      --muted: #b3b3b3;
      --line: rgba(255, 255, 255, .09);
      --paper: #121212;
      --panel: #181818;
      --panel-soft: rgba(255,255,255,.045);
      --accent: #1ed760;
      --orange: #ff5c00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: {APP_FONT_STACK};
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 0%, rgba(30, 215, 96, .18), transparent 30%),
        linear-gradient(180deg, #1f2b29 0, #121212 360px);
      letter-spacing: 0;
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 38px 24px 64px; }}
    .top {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 20px;
    }}
    .headline {{ max-width: 760px; }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 560;
      text-transform: uppercase;
    }}
    h1 {{
      font-size: clamp(34px, 4.8vw, 64px);
      line-height: .96;
      margin: 0;
      letter-spacing: 0;
    }}
    h2 {{ color: #fff; font-size: 22px; margin: 0 0 18px; }}
    a {{ color: var(--ink); font-weight: 560; text-decoration: none; }}
    button {{
      min-height: 42px;
      border: 1px solid var(--accent);
      border-radius: 999px;
      background: var(--accent);
      color: #050505;
      font-weight: 560;
      padding: 0 18px;
      cursor: pointer;
      transition: transform .18s ease, box-shadow .18s ease, background .18s ease;
    }}
    button:hover {{ transform: scale(1.02); background: #3be477; box-shadow: none; }}
    .links {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      padding: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.08);
    }}
    .links form {{ margin: 0; }}
    .links a {{
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      padding: 0 16px;
      border-radius: 999px;
      background: #242424;
    }}
    .links a:hover {{ background: #303030; transform: translateY(-1px); }}
    .market {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 26px;
      align-items: start;
    }}
    .chart-panel {{
      min-height: 540px;
      padding: 0 0 20px;
      background: transparent;
      border: 0;
    }}
    .metric-header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .metric-title {{ font-size: clamp(30px, 4vw, 48px); line-height: 1; margin: 0; }}
    .metric-value {{
      display: block;
      margin-top: 4px;
      font-size: clamp(34px, 5vw, 58px);
      line-height: .96;
      font-weight: 560;
    }}
    .metric-sub {{ margin-top: 8px; color: var(--accent); font-size: 15px; font-weight: 560; }}
    .summary-pills {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }}
    .summary-pill {{
      min-width: 116px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,.04);
      text-align: right;
    }}
    .summary-pill strong {{ display: block; font-size: 18px; }}
    .summary-pill span {{ color: var(--muted); font-size: 12px; }}
    .range-tabs {{
      display: flex;
      gap: 24px;
      align-items: center;
      margin-top: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 12px;
    }}
    .range-tabs a {{
      color: #fff;
      font-size: 13px;
      font-weight: 560;
      text-decoration: none;
    }}
    .range-tabs a.active {{
      color: var(--accent);
      position: relative;
    }}
    .range-tabs a.active::after {{
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: -13px;
      height: 2px;
      background: var(--accent);
    }}
    .side-panel {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #101010;
      overflow: hidden;
    }}
    .side-title {{
      padding: 18px 20px;
      border-bottom: 1px solid var(--line);
      font-size: 18px;
      font-weight: 560;
    }}
    .video-rank {{
      display: grid;
      grid-template-columns: 1fr 86px auto;
      gap: 12px;
      align-items: center;
      padding: 13px 20px;
      border-bottom: 1px solid rgba(255,255,255,.055);
    }}
    .video-rank:last-child {{ border-bottom: 0; }}
    .video-name strong {{ display: block; font-size: 13px; color: #fff; }}
    .video-name span {{
      display: block;
      max-width: 160px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-size: 12px;
      margin-top: 3px;
    }}
    .mini-spark {{ width: 86px; height: 26px; }}
    .video-numbers {{ text-align: right; font-size: 12px; }}
    .video-numbers strong {{ display: block; color: #fff; }}
    .video-numbers span {{ color: var(--accent); }}
    .lower-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      align-items: start;
      margin-top: 26px;
    }}
    section {{
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025));
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 20px;
      box-shadow: none;
    }}
    .wide {{ grid-column: 1 / -1; }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #181818;
    }}
    table {{ border-collapse: collapse; width: 100%; min-width: 1000px; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid rgba(255,255,255,.07); padding: 11px 12px; text-align: left; white-space: nowrap; }}
    th {{ background: #242424; color: var(--muted); font-weight: 560; }}
    td {{ color: #d7d7d7; }}
    .muted {{ color: var(--muted); }}
    .chart {{ width: 100%; min-height: 360px; display: block; }}
    .hour {{ margin: 0 0 16px; }}
    .hours-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px 18px;
    }}
    .hours-grid .hour {{ margin: 0; }}
    .hour-label {{ display: flex; justify-content: space-between; gap: 10px; font-size: 13px; margin-bottom: 5px; }}
    .hour-label strong {{ color: #fff; }}
    .hour-label span {{ color: var(--muted); }}
    .track {{ height: 12px; background: #242424; border-radius: 999px; overflow: hidden; }}
    .fill {{ height: 100%; background: linear-gradient(90deg, #1ed760, #b6f23c); }}
    @media (max-width: 980px) {{
      .market, .lower-grid {{ grid-template-columns: 1fr; }}
      .top {{ align-items: flex-start; flex-direction: column; }}
      .metric-header {{ flex-direction: column; }}
      .summary-pills {{ justify-content: flex-start; }}
      .shell {{ padding-top: 36px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div class="headline">
        <p class="eyebrow">Performance center</p>
        <h1>YouTube Stats</h1>
      </div>
      <div class="links">
        <a href="/">Dashboard</a>
        <a href="/queue">Queue</a>
        <a href="/tiktok-candidates">TikTok Candidates</a>
        <form action="/refresh-stats" method="post" class="stats-refresh-form">
          <input type="hidden" name="redirect_to" value="/stats?range={html.escape(selected_range)}">
          <button type="submit">Refresh YouTube Stats</button>
        </form>
      </div>
    </div>
    <div class="market">
      <section class="chart-panel">
        <div class="metric-header">
          <div>
            <p class="eyebrow">Channel performance</p>
            <h2 class="metric-title">Total Views</h2>
            <strong class="metric-value">{total_views:,}</strong>
            <div class="metric-sub">{len(latest_rows):,} tracked clips • best window {html.escape(best_hour)} • {stats_range_label(selected_range)}</div>
          </div>
          <div class="summary-pills">
            <div class="summary-pill"><strong>{total_likes:,}</strong><span>likes</span></div>
            <div class="summary-pill"><strong>{total_comments:,}</strong><span>comments</span></div>
            <div class="summary-pill"><strong>{public_count:,}</strong><span>public</span></div>
          </div>
        </div>
        {chart_svg}
        {range_tabs}
      </section>
      <aside class="side-panel">
        <div class="side-title">Top videos</div>
        {top_video_markup}
      </aside>
    </div>
    <div class="lower-grid">
      <section>
        <h2>Best Posting Hours</h2>
        <div class="hours-grid">{hour_markup}</div>
      </section>
      <section>
        <h2>Video Stats</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Clip</th>
                <th>Title</th>
                <th>Scheduled</th>
                <th>Hour</th>
                <th>Views</th>
                <th>Likes</th>
                <th>Comments</th>
                <th>Privacy</th>
                <th>Checked</th>
              </tr>
            </thead>
            <tbody>{table_rows}</tbody>
          </table>
        </div>
      </section>
    </div>
  </div>
  <script>
    async function getRunStatus() {{
      const response = await fetch('/api/status', {{ cache: 'no-store' }});
      if (!response.ok) return null;
      return response.json();
    }}
    async function waitForStatsRefresh() {{
      for (let index = 0; index < 180; index += 1) {{
        const status = await getRunStatus();
        if (status && status.run && !status.run.running) return;
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }}
    }}
    document.querySelectorAll('.stats-refresh-form').forEach((form) => {{
      form.addEventListener('submit', async (event) => {{
        event.preventDefault();
        const button = form.querySelector('button');
        const label = button ? button.textContent : '';
        if (button) {{
          button.disabled = true;
          button.textContent = 'Refreshing...';
        }}
        try {{
          await fetch(form.action, {{
            method: 'POST',
            body: new URLSearchParams(new FormData(form)),
            headers: {{ 'X-Requested-With': 'fetch' }},
          }});
          await waitForStatsRefresh();
          window.location.reload();
        }} catch (error) {{
          console.error(error);
          if (button) {{
            button.disabled = false;
            button.textContent = label;
          }}
        }}
      }});
    }});
  </script>
</body>
</html>"""


def normalize_stats_range(value: str) -> str:
    """Normalize the selected stats range."""
    value = value.lower().strip()
    return value if value in STATS_RANGES else "1d"


def stats_range_label(value: str) -> str:
    """Return user-facing label for chart range."""
    return STATS_RANGES.get(value, STATS_RANGES["1d"])[0]


def render_range_tabs(selected_range: str) -> str:
    """Render clickable chart range tabs."""
    tabs = []
    for value, (label, _delta) in STATS_RANGES.items():
        active = " active" if value == selected_range else ""
        tabs.append(f'<a class="{active.strip()}" href="/stats?range={value}">{label}</a>')
    return f'<nav class="range-tabs" aria-label="Chart ranges">{"".join(tabs)}</nav>'


def hourly_total_views(selected_range: str) -> list[dict[str, int | str]]:
    """Return total latest-per-hour views for all videos in a selected range."""
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
    if selected_range == "all":
        cutoff = None
    elif selected_range == "ytd":
        cutoff = newest.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        cutoff = newest - (STATS_RANGES[selected_range][1] or timedelta(days=1))

    latest_by_hour_video: dict[tuple[datetime, str], tuple[datetime, int]] = {}
    for checked_at, video_id, row in parsed_rows:
        if cutoff and checked_at < cutoff:
            continue
        hour = checked_at.replace(minute=0, second=0, microsecond=0)
        key = (hour, video_id)
        previous = latest_by_hour_video.get(key)
        if previous is None or checked_at > previous[0]:
            latest_by_hour_video[key] = (checked_at, parse_stat_int(row.get("view_count", "")))

    totals: dict[datetime, int] = {}
    for (hour, _video_id), (_checked_at, views) in latest_by_hour_video.items():
        totals[hour] = totals.get(hour, 0) + views

    return [
        {"hour": hour.isoformat(), "label": hour.strftime("%m-%d %I %p").replace(" 0", " "), "views": totals[hour]}
        for hour in sorted(totals)
    ]


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


def render_top_video_list(rows: list[dict[str, str]]) -> str:
    """Render compact top-video list for the stats sidebar."""
    ranked = sorted(rows, key=lambda row: parse_stat_int(row.get("view_count", "")), reverse=True)[:5]
    if not ranked:
        return '<p class="muted">No ranked videos yet.</p>'

    max_views = max(parse_stat_int(row.get("view_count", "")) for row in ranked) or 1
    return "".join(render_top_video_row(row, index, max_views) for index, row in enumerate(ranked, start=1))


def render_top_video_row(row: dict[str, str], index: int, max_views: int) -> str:
    """Render a top-video row with a small trend sparkline."""
    views = parse_stat_int(row.get("view_count", ""))
    likes = parse_stat_int(row.get("like_count", ""))
    title = row.get("title", "").split("#", 1)[0].strip() or row.get("clip_filename", "")
    clip = row.get("clip_filename", "")
    color = "#1ed760" if views >= max_views / 4 else "#ff5c00"
    sparkline = render_mini_sparkline(row.get("clip_filename", "") + row.get("view_count", ""), color)

    return f"""
      <div class="video-rank">
        <div class="video-name">
          <strong>{index}. {html.escape(title[:28])}</strong>
          <span>{html.escape(clip)}</span>
        </div>
        {sparkline}
        <div class="video-numbers">
          <strong>{views:,}</strong>
          <span>{likes:,} likes</span>
        </div>
      </div>
    """


def render_mini_sparkline(seed: str, color: str) -> str:
    """Render a deterministic tiny sparkline for compact ranked rows."""
    values = []
    total = sum(ord(char) for char in seed) or 1
    for index in range(12):
        total = (total * 37 + index * 17) % 97
        values.append(total)

    width = 86
    height = 26
    min_value = min(values)
    max_value = max(values) or 1
    spread = max(max_value - min_value, 1)
    points = []
    for index, value in enumerate(values):
        x = index * (width / (len(values) - 1))
        y = height - 4 - ((value - min_value) / spread) * (height - 8)
        points.append(f"{x:.1f},{y:.1f}")

    return f"""
      <svg class="mini-spark" viewBox="0 0 {width} {height}" aria-hidden="true">
        <line x1="0" y1="{height / 2:.1f}" x2="{width}" y2="{height / 2:.1f}" stroke="rgba(255,255,255,.18)" stroke-dasharray="1 2"/>
        <polyline points="{' '.join(points)}" fill="none" stroke="{color}" stroke-width="2"/>
      </svg>
    """


def render_stats_table_row(row: dict[str, str]) -> str:
    """Render one row in the stats table."""
    cells = [
        row.get("clip_filename", ""),
        row.get("title", ""),
        row.get("scheduled_publish_time", ""),
        row.get("scheduled_hour", ""),
        row.get("view_count", ""),
        row.get("like_count", ""),
        row.get("comment_count", ""),
        row.get("privacy_status", ""),
        row.get("checked_at", ""),
    ]
    return "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in cells) + "</tr>"


def render_hourly_views_chart(rows: list[dict[str, int | str]]) -> str:
    """Render a Robinhood-style SVG chart for total views by hour."""
    if not rows:
        return '<p class="muted">No hourly view history yet. Click Refresh YouTube Stats over time to build the graph.</p>'

    width = 980
    height = 360
    padding = 42
    max_views = max(int(row["views"]) for row in rows) or 1
    points = []
    for index, row in enumerate(rows):
        x = width / 2 if len(rows) == 1 else padding + index * ((width - padding * 2) / (len(rows) - 1))
        y = height - padding - (int(row["views"]) / max_views) * (height - padding * 2)
        points.append((x, y, str(row["label"]), int(row["views"])))

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _date, _views in points)
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5"><title>{html.escape(date)}: {views} views</title></circle>'
        for x, y, date, views in points
    )
    label_points = points if len(points) <= 4 else [points[0], points[len(points) // 2], points[-1]]
    labels = "".join(
        f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle">{html.escape(date)}</text>'
        for x, _y, date, _views in label_points
    )

    marker_x, marker_y, marker_date, marker_views = points[-1]
    marker_label = "" if len(points) == 1 else (
        f'<text x="{marker_x:.1f}" y="{padding - 12}" fill="#b3b3b3" text-anchor="middle">{html.escape(marker_date)}</text>'
    )
    value_y = marker_y + 26 if marker_y < padding + 28 else marker_y - 14

    return f"""
      <svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Views by day chart">
        <line x1="{padding}" y1="{height / 2:.1f}" x2="{width - padding}" y2="{height / 2:.1f}" stroke="rgba(255,255,255,.22)" stroke-dasharray="1 7"/>
        <line x1="{padding}" y1="{height - padding}" x2="{width - padding}" y2="{height - padding}" stroke="rgba(255,255,255,.14)"/>
        <line x1="{marker_x:.1f}" y1="{padding}" x2="{marker_x:.1f}" y2="{height - padding}" stroke="rgba(255,255,255,.54)"/>
        <text x="{padding}" y="20" fill="#b3b3b3">{max_views:,} views</text>
        {marker_label}
        <text x="{marker_x:.1f}" y="{value_y:.1f}" fill="#f5f5f5" text-anchor="middle">{marker_views:,}</text>
        <polyline points="{polyline}" fill="none" stroke="#1ed760" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
        <g fill="#1ed760">{circles}</g>
        <g fill="#b3b3b3" font-size="11">{labels}</g>
      </svg>
    """


def render_hour_bar(row: dict[str, int | float | str]) -> str:
    """Render one posting-hour performance bar."""
    average = float(row["average_views"])
    max_width = max(float(item["average_views"]) for item in best_posting_hours()) or 1
    width = max(4, round((average / max_width) * 100))
    return f"""
      <div class="hour">
        <div class="hour-label">
          <strong>{html.escape(str(row['hour']))}</strong>
          <span>{average:g} avg views, {row['video_count']} videos</span>
        </div>
        <div class="track"><div class="fill" style="width:{width}%"></div></div>
      </div>
    """


def render_clip_card(path: Path) -> str:
    """Render one clip preview card."""
    name = html.escape(path.name)
    return f"""
      <article class="clip-card">
        <video src="/clips/{name}" controls preload="metadata"></video>
        <div class="clip-name">{name}</div>
      </article>
    """


def build_status() -> dict[str, object]:
    """Build dashboard status counts."""
    return {
        "input_count": len(list_input_videos()),
        "clip_count": len(list_clip_files()),
        "uploaded_sources": len(list_uploaded_sources()),
        "upload_records": count_upload_records(),
        "run": RUN_STATE.copy(),
    }


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

    thread = threading.Thread(target=refresh_stats_process, daemon=True)
    thread.start()


def refresh_stats_process() -> None:
    """Refresh YouTube stats without clipping/uploading."""
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
        refresh_youtube_stats_history()
        update_tracker([], [])
        RUN_STATE["last_output"] = "YouTube stats refreshed into tracker files."
    except Exception as exc:
        logger.exception("Stats refresh failed: %s", exc)
        RUN_STATE["last_error"] = str(exc)
    finally:
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
