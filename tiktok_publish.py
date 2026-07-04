"""TikTok Login Kit + Content Posting API integration for PianoClip.

Handles the OAuth flow (connect a TikTok account), token storage/refresh, and
direct-posting a local clip to the connected account via the Content Posting API.

Only the standard library is used (urllib) so there are no extra dependencies.

Environment variables (put these in .env):
    TIKTOK_CLIENT_KEY      - from the TikTok developer portal
    TIKTOK_CLIENT_SECRET   - from the TikTok developer portal
    TIKTOK_REDIRECT_URI    - optional explicit callback URL. If unset, it is
                             derived from PUBLIC_BASE_URL or NGROK_DOMAIN, and
                             falls back to http://localhost:8000/tiktok/callback.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# --- Endpoints ---------------------------------------------------------------
AUTH_BASE = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
POST_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
POST_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

# Scopes: user.info.basic is required to show the creator's name/avatar before
# posting (a TikTok review requirement); video.publish enables Direct Post.
SCOPES = "user.info.basic,video.publish"

TOKEN_FILE = config.CREDENTIALS_DIR / "tiktok_token.json"


# --- Configuration helpers ---------------------------------------------------
def client_key() -> str:
    return (os.getenv("TIKTOK_CLIENT_KEY", "") or "").strip()


def client_secret() -> str:
    return (os.getenv("TIKTOK_CLIENT_SECRET", "") or "").strip()


def is_configured() -> bool:
    return bool(client_key() and client_secret())


def redirect_uri() -> str:
    """Resolve the OAuth callback URL. Explicit env wins; else derive; else localhost."""
    explicit = (os.getenv("TIKTOK_REDIRECT_URI", "") or "").strip()
    if explicit:
        return explicit
    base = (os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    if not base:
        domain = (os.getenv("NGROK_DOMAIN", "") or "").strip()
        if domain:
            base = f"https://{domain}"
    if not base:
        base = "http://localhost:8000"
    return base.rstrip("/") + "/tiktok/callback"


# --- OAuth -------------------------------------------------------------------
def authorize_url(state: str) -> str:
    params = {
        "client_key": client_key(),
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    return AUTH_BASE + "?" + urllib.parse.urlencode(params)


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, body: dict, access_token: str) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for an access token and persist it."""
    result = _post_form(TOKEN_URL, {
        "client_key": client_key(),
        "client_secret": client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
    })
    if "access_token" not in result:
        raise RuntimeError(f"TikTok token exchange failed: {result}")
    _save_token(result)
    return result


def refresh_access_token(refresh_token: str) -> dict:
    result = _post_form(TOKEN_URL, {
        "client_key": client_key(),
        "client_secret": client_secret(),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if "access_token" not in result:
        raise RuntimeError(f"TikTok token refresh failed: {result}")
    _save_token(result)
    return result


# --- Token storage -----------------------------------------------------------
def _save_token(token: dict) -> None:
    token = dict(token)
    token["obtained_at"] = int(time.time())
    config.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token, indent=2), encoding="utf-8")


def load_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_token() -> None:
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


def is_connected() -> bool:
    return load_token() is not None


def valid_access_token() -> str:
    """Return a usable access token, refreshing if it looks expired."""
    token = load_token()
    if not token:
        raise RuntimeError("No TikTok account connected.")
    obtained = int(token.get("obtained_at", 0))
    expires_in = int(token.get("expires_in", 0) or 0)
    # Refresh a little early to be safe.
    if expires_in and time.time() > obtained + expires_in - 120:
        refresh = token.get("refresh_token", "")
        if refresh:
            token = refresh_access_token(refresh)
    return token.get("access_token", "")


# --- User info ---------------------------------------------------------------
def get_user_info() -> dict:
    token = valid_access_token()
    fields = "open_id,union_id,avatar_url,display_name"
    url = USER_INFO_URL + "?" + urllib.parse.urlencode({"fields": fields})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return (result.get("data", {}) or {}).get("user", {}) or {}


def connected_display() -> dict:
    """Return {display_name, avatar_url} for the connected account, or {} on error."""
    try:
        info = get_user_info()
        return {
            "display_name": info.get("display_name", "your TikTok"),
            "avatar_url": info.get("avatar_url", ""),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch TikTok user info: %s", exc)
        return {}


# --- Direct posting ----------------------------------------------------------
def query_creator_info() -> dict:
    """Required before Direct Post: returns allowed privacy levels + creator info."""
    token = valid_access_token()
    result = _post_json(CREATOR_INFO_URL, {}, token)
    return result.get("data", {}) or {}


def direct_post_video(video_path: Path, title: str) -> dict:
    """Direct-post a local video file to the connected TikTok account.

    In an unaudited sandbox app the post is forced to SELF_ONLY (private).
    Returns {publish_id, status} or raises on hard failure.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"Clip not found: {video_path}")

    token = valid_access_token()

    # Creator info gates the allowed privacy levels; unaudited => SELF_ONLY only.
    creator = query_creator_info()
    privacy_options = creator.get("privacy_level_options", []) or []
    privacy = "SELF_ONLY" if "SELF_ONLY" in privacy_options or not privacy_options else privacy_options[0]

    video_size = video_path.stat().st_size
    init_body = {
        "post_info": {
            "title": title[:150],
            "privacy_level": privacy,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": video_size,
            "total_chunk_count": 1,
        },
    }
    init = _post_json(POST_INIT_URL, init_body, token)
    data = init.get("data", {}) or {}
    publish_id = data.get("publish_id", "")
    upload_url = data.get("upload_url", "")
    if not upload_url or not publish_id:
        raise RuntimeError(f"TikTok post init failed: {init}")

    # Upload the whole file as a single chunk.
    with video_path.open("rb") as fh:
        video_bytes = fh.read()
    put_req = urllib.request.Request(
        upload_url, data=video_bytes, method="PUT",
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
            "Content-Length": str(video_size),
        },
    )
    with urllib.request.urlopen(put_req, timeout=300) as resp:
        _ = resp.read()

    return {"publish_id": publish_id, "privacy_level": privacy}


def fetch_post_status(publish_id: str) -> dict:
    token = valid_access_token()
    result = _post_json(POST_STATUS_URL, {"publish_id": publish_id}, token)
    return result.get("data", {}) or {}
