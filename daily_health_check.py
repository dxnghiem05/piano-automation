#!/usr/bin/env python3
"""Daily health check for the piano-dashboard YouTube Shorts project.

Read-only by default. It NEVER uploads videos, commits, or pushes. It only
deletes junk files (.DS_Store / __pycache__) when explicitly asked with
--clean-junk, and even then it touches nothing inside clips/, logs/, metadata/,
credentials/, input/, uploaded/ or processing/ beyond those junk patterns.

Checks performed:
    1. Core project files still compile.
    2. Dashboard is running, or (if not) would start cleanly.
    3. /api/status responds.
    4. No private files are staged or tracked by git.
    5. credentials/token.json and credentials/credentials.json are ignored.
    6. logs, metadata, clips, input, uploaded are ignored.
    7. No obvious junk files (.DS_Store, __pycache__).
    8. Upload queue / schedule looks healthy.
    9. YouTube quota / deferred errors are summarized.

Outputs:
    * Prints a clear report to stdout.
    * Writes the latest report to logs/health_check_latest.txt (overwrite).
    * Appends a summary row to logs/health_check_history.csv.

Usage:
    python daily_health_check.py              # read-only report
    python daily_health_check.py --clean-junk # also remove .DS_Store/__pycache__
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import config

BASE_DIR = config.BASE_DIR

CORE_FILES = [
    "dashboard.py",
    "main.py",
    "generate_clips.py",
    "generate_metadata.py",
    "youtube_upload.py",
    "youtube_stats.py",
    "stats_tracker.py",
    "project_dataset.py",
    "scheduler.py",
    "tracker.py",
    "media_tools.py",
]

# Status levels, ordered by severity.
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
_SEVERITY = {OK: 0, WARN: 1, FAIL: 2}

# Directories that must remain git-ignored (checked with a representative path).
IGNORED_DIR_SAMPLES = {
    "logs": "logs/app.log",
    "metadata": "metadata/metadata.csv",
    "clips": "clips/clip_000001.mp4",
    "input": "input/example.mp4",
    "uploaded": "uploaded/example.mp4",
}


class CheckResult:
    def __init__(self, name: str, status: str, detail: str) -> None:
        self.name = name
        self.status = status
        self.detail = detail


def dashboard_base_url() -> str:
    """Resolve the dashboard URL, defaulting to localhost:8000."""
    host = "localhost"
    port = 8000
    try:
        import dashboard  # noqa: F401 - only importing to read config-like constants
        port = getattr(dashboard, "PORT", port)
    except Exception:  # noqa: BLE001 - fall back to defaults if import fails
        pass
    return f"http://{host}:{port}"


def run_git(args: list[str]) -> tuple[int, str]:
    """Run a git command in the project dir; return (returncode, output)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except Exception as exc:  # noqa: BLE001
        return 1, f"git error: {exc}"


# --------------------------------------------------------------------------- #
# Individual checks. Each returns a CheckResult and never raises.
# --------------------------------------------------------------------------- #

def check_compile() -> CheckResult:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", *CORE_FILES],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            return CheckResult("compile", OK, f"all {len(CORE_FILES)} core files compile")
        return CheckResult("compile", FAIL, proc.stderr.strip() or "py_compile failed")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("compile", FAIL, f"could not run py_compile: {exc}")


def fetch_status(base_url: str, timeout: float = 5.0):
    """Return (ok, data_or_error). ok=True means /api/status responded."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/status", timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            return True, json.loads(body)
        except json.JSONDecodeError:
            return True, {"_raw": body[:500]}
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def check_dashboard_startable(status_ok: bool) -> CheckResult:
    if status_ok:
        return CheckResult("dashboard", OK, "dashboard is running (responded on /api/status)")
    # Not running: confirm it *would* start by importing the module (no server bind).
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import dashboard"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            return CheckResult(
                "dashboard",
                WARN,
                "dashboard not running, but imports cleanly and can be started "
                "(python dashboard.py)",
            )
        return CheckResult("dashboard", FAIL, "dashboard not running and import failed: "
                           + (proc.stderr.strip() or "unknown error"))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("dashboard", FAIL, f"dashboard not running; import check errored: {exc}")


def check_api_status(status_ok: bool, data) -> CheckResult:
    if not status_ok:
        return CheckResult("api_status", WARN,
                           f"/api/status not reachable ({data}); expected if dashboard is stopped")
    if isinstance(data, dict) and data:
        keys = ", ".join(sorted(data.keys())[:8])
        return CheckResult("api_status", OK, f"/api/status OK; keys: {keys}")
    return CheckResult("api_status", WARN, "/api/status responded but payload was empty/unreadable")


def check_git_privacy() -> CheckResult:
    # Nothing private staged.
    rc_staged, staged = run_git(["diff", "--cached", "--name-only"])
    private_markers = ("credentials", "token", "secret", ".env", "logs/", "metadata/",
                       "clips/", "input/", "uploaded/", ".mp4", ".mov", ".csv")
    staged_private = [
        line for line in staged.splitlines()
        if line and any(m in line for m in private_markers) and not line.endswith(".gitkeep")
        and line != ".env.example"
    ]
    # Nothing private tracked (allow only the safe placeholders).
    rc_tracked, tracked = run_git(["ls-files"])
    allowed = {".env.example", "clips/.gitkeep", "credentials/.gitignore", "input/.gitkeep",
               "logs/.gitkeep", "metadata/.gitkeep", "uploaded/.gitkeep", "processing/.gitkeep"}
    tracked_private = [
        line for line in tracked.splitlines()
        if line and any(m in line for m in private_markers) and line not in allowed
    ]
    if staged_private or tracked_private:
        detail = ""
        if staged_private:
            detail += f"STAGED private: {staged_private}. "
        if tracked_private:
            detail += f"TRACKED private: {tracked_private}."
        return CheckResult("git_privacy", FAIL, detail.strip())
    return CheckResult("git_privacy", OK, "no private files staged or tracked")


def check_credentials_ignored() -> CheckResult:
    targets = ["credentials/token.json", "credentials/credentials.json"]
    not_ignored = []
    for rel in targets:
        rc, _ = run_git(["check-ignore", "-q", rel])
        # check-ignore -q: exit 0 => ignored, exit 1 => NOT ignored.
        if rc != 0:
            not_ignored.append(rel)
    if not_ignored:
        return CheckResult("credentials_ignored", FAIL,
                           f"NOT ignored (would be exposed): {not_ignored}")
    return CheckResult("credentials_ignored", OK, "token.json and credentials.json are ignored")


def check_dirs_ignored() -> CheckResult:
    not_ignored = []
    for name, sample in IGNORED_DIR_SAMPLES.items():
        rc, _ = run_git(["check-ignore", "-q", sample])
        if rc != 0:
            not_ignored.append(name)
    if not_ignored:
        return CheckResult("dirs_ignored", FAIL, f"NOT ignored: {not_ignored}")
    return CheckResult("dirs_ignored", OK,
                       "logs, metadata, clips, input, uploaded are ignored")


def find_junk() -> tuple[list[Path], list[Path]]:
    """Return (ds_store_files, pycache_dirs) found under the project."""
    ds_store = list(BASE_DIR.rglob(".DS_Store"))
    pycache = [p for p in BASE_DIR.rglob("__pycache__") if p.is_dir()]
    # Never look inside the virtualenv for junk we would clean.
    ds_store = [p for p in ds_store if ".venv" not in p.parts]
    pycache = [p for p in pycache if ".venv" not in p.parts]
    return ds_store, pycache


def check_junk(clean: bool) -> CheckResult:
    ds_store, pycache = find_junk()
    total = len(ds_store) + len(pycache)
    if total == 0:
        return CheckResult("junk", OK, "no .DS_Store or __pycache__ found")
    if not clean:
        return CheckResult("junk", WARN,
                           f"{len(ds_store)} .DS_Store, {len(pycache)} __pycache__ dirs "
                           f"present (run with --clean-junk to remove)")
    removed = 0
    errors = []
    for f in ds_store:
        try:
            f.unlink()
            removed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{f}: {exc}")
    for d in pycache:
        try:
            shutil.rmtree(d)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{d}: {exc}")
    if errors:
        return CheckResult("junk", WARN, f"removed {removed}; some could not be removed: {errors[:3]}")
    return CheckResult("junk", OK, f"cleaned {removed} junk item(s)")


def _read_upload_rows() -> list[dict]:
    path = config.UPLOAD_LOG_FILE
    if not path.exists():
        return []
    rows = []
    try:
        with path.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows.append(row)
    except Exception:  # noqa: BLE001
        return []
    return rows


def check_queue() -> tuple[CheckResult, dict]:
    rows = _read_upload_rows()
    stats = {"uploaded": 0, "deferred": 0, "failed": 0, "latest_slot": "", "eligible": 0}
    if not rows:
        return CheckResult("queue", WARN, "uploads_log.csv missing or empty"), stats

    uploaded_slots = []
    for row in rows:
        status = (row.get("status") or "").strip()
        if status == "uploaded":
            stats["uploaded"] += 1
            slot = (row.get("scheduled_publish_time") or "").strip()
            if slot:
                uploaded_slots.append(slot)
        elif status == "deferred_quota":
            stats["deferred"] += 1
        elif status == "failed":
            stats["failed"] += 1
    stats["latest_slot"] = max(uploaded_slots) if uploaded_slots else ""

    # Eligible backlog per the real Run Now filter (never uploads).
    try:
        from main import filter_not_attempted
        all_clips = sorted(config.CLIPS_DIR.glob("clip_*.mp4"))
        eligible = filter_not_attempted(all_clips)
        stats["eligible"] = len(eligible)
        first_eligible = sorted(p.name for p in eligible)[:1]
    except Exception as exc:  # noqa: BLE001
        return (CheckResult("queue", WARN,
                            f"uploaded={stats['uploaded']} but eligibility check failed: {exc}"),
                stats)

    detail = (f"uploaded={stats['uploaded']}, deferred={stats['deferred']}, "
              f"failed={stats['failed']}, latest scheduled slot={stats['latest_slot'] or 'n/a'}, "
              f"eligible backlog={stats['eligible']}"
              + (f" (next: {first_eligible[0]})" if first_eligible else ""))
    status = FAIL if stats["failed"] else OK
    return CheckResult("queue", status, detail), stats


def check_quota_deferred() -> CheckResult:
    rows = _read_upload_rows()
    if not rows:
        return CheckResult("quota", OK, "no upload log yet; nothing deferred")

    # Latest deferred slot per clip, excluding any clip that later uploaded.
    uploaded = set()
    latest_deferred: dict[str, str] = {}
    for row in rows:
        name = (row.get("clip_filename") or "").strip()
        status = (row.get("status") or "").strip()
        if not name:
            continue
        if status == "uploaded":
            uploaded.add(name)
        elif status == "deferred_quota":
            slot = (row.get("scheduled_publish_time") or "").strip()
            if not slot:
                continue
            if name not in latest_deferred or slot > latest_deferred[name]:
                latest_deferred[name] = slot

    active, stale = [], []
    try:
        from youtube_upload import read_stale_deferred_filenames
        stale_names = read_stale_deferred_filenames()
    except Exception:  # noqa: BLE001
        stale_names = set()

    for name, slot in latest_deferred.items():
        if name in uploaded:
            continue
        if name in stale_names:
            stale.append((name, slot))
        else:
            active.append((name, slot))

    active.sort()
    stale.sort()
    if not active and not stale:
        return CheckResult("quota", OK, "no outstanding deferred/quota clips")

    detail = f"active deferred (will retry): {len(active)}"
    if active:
        first = active[0]
        detail += f" -> next {first[0]} @ {first[1]}"
    detail += f"; stale deferred (skipped by design): {len(stale)}"
    status = WARN if active else OK
    return CheckResult("quota", status, detail)


# --------------------------------------------------------------------------- #
# Report assembly + persistence
# --------------------------------------------------------------------------- #

def overall_status(results: list[CheckResult]) -> str:
    worst = OK
    for r in results:
        if _SEVERITY[r.status] > _SEVERITY[worst]:
            worst = r.status
    return worst


def build_report(results: list[CheckResult], overall: str, when: datetime) -> str:
    out = StringIO()
    out.write("PIANO DASHBOARD - DAILY HEALTH CHECK\n")
    out.write("===================================\n")
    out.write(f"Time     : {when.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    out.write(f"Overall  : {overall}\n")
    out.write("\n")
    for r in results:
        out.write(f"[{r.status:4}] {r.name}\n")
        out.write(f"        {r.detail}\n")
    out.write("\n")
    if overall == FAIL:
        out.write("ACTION: One or more critical checks FAILED. Review the items above.\n")
    elif overall == WARN:
        out.write("NOTE: Warnings present (often just 'dashboard stopped'). No action required unless unexpected.\n")
    else:
        out.write("All good.\n")
    return out.getvalue()


def write_latest(report: str) -> None:
    path = config.LOGS_DIR / "health_check_latest.txt"
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def append_history(results: list[CheckResult], overall: str, when: datetime) -> None:
    path = config.LOGS_DIR / "health_check_history.csv"
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["timestamp", "overall"] + [r.name for r in results]
    exists = path.exists()
    row = {"timestamp": when.astimezone().isoformat(), "overall": overall}
    for r in results:
        row[r.name] = r.status
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily read-only health check for the piano dashboard.")
    parser.add_argument("--clean-junk", action="store_true",
                        help="Remove .DS_Store files and __pycache__ dirs (still never touches videos/logs/creds).")
    parser.add_argument("--no-history", action="store_true",
                        help="Do not append to logs/health_check_history.csv.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    when = datetime.now(timezone.utc)

    base_url = dashboard_base_url()
    status_ok, status_data = fetch_status(base_url)

    results = [
        check_compile(),
        check_dashboard_startable(status_ok),
        check_api_status(status_ok, status_data),
        check_git_privacy(),
        check_credentials_ignored(),
        check_dirs_ignored(),
        check_junk(args.clean_junk),
    ]
    queue_result, _ = check_queue()
    results.append(queue_result)
    results.append(check_quota_deferred())

    overall = overall_status(results)
    report = build_report(results, overall, when)

    print(report)
    try:
        write_latest(report)
    except Exception as exc:  # noqa: BLE001
        print(f"(warning) could not write health_check_latest.txt: {exc}")
    if not args.no_history:
        try:
            append_history(results, overall, when)
        except Exception as exc:  # noqa: BLE001
            print(f"(warning) could not append health_check_history.csv: {exc}")

    return 2 if overall == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
