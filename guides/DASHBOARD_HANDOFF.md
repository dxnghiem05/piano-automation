# Piano Shorts Dashboard — Enhancement Handoff

**Purpose of this doc:** brief a fresh session on the existing dashboard so it can enhance
functionality, live logs, and design **without changing the core workflow or the CSV data
layer.** Minor tweaks are fine as long as the overall goal is preserved.

---

## 1. What this is (the goal — keep it)

A self-hosted creator dashboard for a YouTube Shorts piano channel. It runs a local Python
HTTP server that shows analytics and drives an automation pipeline:

**raw video → clips → metadata → schedule → upload to YouTube (private, auto-publishing on a
spread-out schedule).**

The UI is "Piano Shorts" (v5): dark aurora background, glass panels, a piano-key motif, one
page per sidebar tab. Owner controls are gated behind HTTP Basic auth; the public sees a
read-only viewer mode.

**Do not change:** the clip/upload pipeline behavior, the folder flow (input → uploaded), the
CSV/dataset formats and files, or owner-auth/viewer-mode. Enhancements should sit on the
UI/UX and presentation layer.

---

## 2. Architecture

- **Server:** hand-rolled `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` in
  `dashboard.py`. No web framework. One `render_*` function per page returns a full HTML
  string. Styling is one big CSS constant (`STYLE_V5`); background markup/scripts are
  `BG_MARKUP` / `BG_SCRIPT`.
- **Pipeline:** the dashboard shells out to `main.py` via `subprocess` (`.venv/bin/python
  main.py [--clip-only]`) on a background thread (`run_main_process`).
- **Auth:** `AUTH_REQUIRED` is on when `DASHBOARD_PASSWORD` is set (in `.env`). Owner-only
  UI carries `data-owner-only`; `VIEWER_MODE_SNIPPET` hides those for public visitors.
- **Caching:** stats reads are memoized by file mtime so warm page renders are ~10ms.
- **Background sampler:** a daemon thread appends a lightweight YouTube-stats snapshot every
  30 min during posting hours so the hourly chart fills in (uses `SNAPSHOT_LOCK`, separate
  from the pipeline's `STATS_REFRESH_LOCK`).

---

## 3. File map

| File | Role |
|---|---|
| `dashboard.py` | **The whole web app** (~2.9k lines): server, routing, all `render_*` pages, auth, caching, background sampler, upload handler, TikTok routes. Most enhancement work happens here. |
| `main.py` | Pipeline entrypoint. `--clip-only` clips without uploading; full run clips + uploads. |
| `generate_clips.py` | Video discovery + ffmpeg clipping (9:16 vertical, blurred-fill background). |
| `generate_metadata.py` | Builds titles/description/tags per clip (mood words + hashtags). |
| `scheduler.py` | Computes spread-out publish times within posting hours. |
| `youtube_upload.py` | Uploads clips to YouTube as **private, scheduled** (`publishAt`). Writes `uploads_log.csv`. |
| `stats_tracker.py` | Fetches YouTube stats, appends snapshots to `youtube_stats_history.csv`, read helpers. |
| `tracker.py` | Rebuilds the tracker CSV joining clips + stats. |
| `project_dataset.py` | Builds the data-science dataset (CSV/XLSX). |
| `youtube_stats.py` | YouTube API stats fetch helpers. |
| `media_tools.py` | Locates ffmpeg/ffprobe. |
| `tiktok_publish.py` | TikTok OAuth (Login Kit) — **currently unused by the UI** (we moved to a manual "download clip + open TikTok Upload" flow). Safe to leave. |
| `daily_health_check.py` | Read-only health/security check script. Invoked by the weekly Claude security-check task (Mondays 12pm) and the on-Mac launchd health-check job. |
| `logging_setup.py` | Configures logging to `logs/app.log` (both dashboard + pipeline log here). |
| `config.py` | All paths + tunables (see below). |
| `watch.py`, `dev.py` | Dev helpers. |
| `*.command` | macOS launchers (Start dashboard, ngrok public link, push to GitHub). |
| `README.md`, `HOW_TO_OPEN_DASHBOARD.md` | User docs. |

---

## 4. Data files — **read-only contract, do not restructure**

Under `logs/` and project dirs. These formats are load-bearing for the pipeline; the UI reads
them but must not change their schemas:

- `logs/app.log` — combined log (dashboard HTTP + pipeline events). Feeds the live log panel.
- `logs/uploads_log.csv` — every upload attempt: `clip_filename, youtube_video_id, title, upload_time, scheduled_publish_time, status`.
- `logs/youtube_stats_history.csv` — append-only view/like/comment snapshots per clip.
- `project_dataset.csv` / `.xlsx`, `privacy_overrides.csv`, `tiktok_schedule.json`, `clip_counter.txt`, `title_state.json`.
- Folders: `input/` (raw drop) → clipped → source moved to `uploaded/`; `clips/` (all generated clips); `processing/` (in-flight encodes).

> **Rule for the next session:** you may read these however you like and add *new* files, but
> do not rename columns, change delimiters, or migrate the CSVs. The pipeline depends on them.

---

## 5. Routes (in `dashboard.py`)

Pages (GET): `/` (home splash), `/overview`, `/stats`, `/tiktok-candidates`, `/tracker`,
`/experiment`, `/data-science`. `/queue` redirects to `/stats`.

Actions (POST, owner-only): `/upload` (save to input + optional clip), `/run` (full
clip+upload), `/clip-only`, `/refresh-stats`, `/queue/privacy`, `/tiktok-schedule`.

Assets/data (GET): `/clips/<file>`, `/tracker.csv`, `/tiktok-candidates.csv`,
`/project-data...`, `/favicon.ico`. `/login` for auth.

Key handlers: `run_main_process`, `start_run` / `start_clip_only` (guarded by
`RUN_STATE["running"]` and `STATS_REFRESH_LOCK`), `handle_upload` (already returns JSON for
AJAX via `X-Requested-With: fetch`), `live_dashboard_log_lines` (feeds the log panel; filters
HTTP noise, formats as `HH:MM:SS  message`).

---

## 6. Runtime behaviors / invariants to preserve

- `RUN_STATE` dict tracks `running` (pipeline) and `stats_running` (stats refresh) separately.
- While busy, pages currently **auto-reload every 5s** (`_busy_autoreload()`), the mechanism
  we most want to replace (see roadmap #1).
- The full run uploads **all not-yet-uploaded clips** in `clips/`, skipping ones already in
  `uploads_log.csv`. Uploads are private + `publishAt` scheduled. Keep this behavior.
- Owner-only elements must stay hidden in viewer mode.
- Background stats sampler must never hold `STATS_REFRESH_LOCK` (it uses `SNAPSHOT_LOCK`) so
  it can't block a Clip+Upload click.

---

## 7. Recent changes (context for the next session)

- Rebuilt UI to v5 "Piano Shorts" wired to live data.
- Perf: mtime caches + server-side SVG charts (replaced Chart.js).
- Background hourly stats snapshotter (fills the "today's gains" chart).
- Flicker fixes: static aurora glows (no animated blurred blobs); opaque top bar (dropped
  sticky `backdrop-filter`).
- Upload progress bar + "landed in input" confirmation (XHR to `/upload`).
- Live log: filtered HTTP noise, `HH:MM:SS  message` formatting.
- TikTok: switched from auto Direct-Post to a manual **download clip → open TikTok Upload**
  flow (OAuth code left in place, unused).
- `Clip + Upload` now stays on `/overview`.

---

## 8. Enhancement roadmap (priority order)

### 1 — Keystone: replace full-page auto-reload with live partial updates
Add a lightweight `GET /api/status` returning JSON (`running`, `stats_running`, run
progress, latest log lines, KPI numbers), and have the page **poll (or use SSE) and patch the
DOM in place** instead of `location.reload()`. This kills the reload flash, fixes the
old-monitor flicker, and is the foundation for everything below. Keep `_busy_autoreload` as a
no-JS fallback only.

### 2 — Live run progress + Stop
- Progress bar: "Uploading clip 12 of 19 · ~2 min left". Source the counts from `RUN_STATE`
  (extend it with `phase`, `done`, `total`) updated as `run_main_process` streams subprocess
  output.
- **Stop button:** keep a handle to the pipeline `subprocess.Popen` so a `/stop` route can
  terminate it gracefully.

### 3 — Confirm before Clip + Upload
A small modal: "This will upload N pending clips to YouTube, publishing from <date>. Continue?"
Compute N with the existing pending-clip logic. Prevents surprise mass uploads.

### 4 — Colored, streaming live logs
Once #1 exists: stream log lines into an auto-scrolling feed, color-coded (green
`Uploaded…`, red errors/quota, dim routine). Add collapsible per-run groups with a summary
chip ("15 clips · 19 uploads · 0 fails"), a pinned "last run" status line, and an
expand-to-fullscreen view.

### 5 — YouTube quota meter + finish notification
- Show "uploads remaining today" (track count vs the known daily cap; surface quota errors).
- Browser notification/toast when a run finishes.

### 6 — Design polish
- **Calm mode** toggle (stills animations, flattens brightest gradients) for the second
  monitor — persist choice in `localStorage`.
- Count-up animation on big KPI numbers; sparklines on KPI tiles (7-day trend).
- Mobile-responsive layout (check/trigger from phone).
- Loading skeletons on first paint.

---

## 9. Guardrails for the next session

- Do **not** modify CSV schemas or the pipeline's file flow.
- Do **not** change owner-auth or viewer-mode gating.
- Keep the clip→upload behavior and scheduling identical.
- Prefer additive changes (new routes/endpoints, new CSS) over rewrites.
- `dashboard.py` targets the macOS `.venv` (Python 3.11+); avoid 3.12-only syntax if running
  under older interpreters.
- Test each page renders 200 for both owner and public before committing; verify a warm
  render stays fast (~10ms).
- Commit in small, described steps.
