# YouTube Shorts Piano Automation

A production-ready Python 3.11+ macOS app that turns iPhone-recorded piano videos into scheduled YouTube Shorts.

Drop videos into `input/`, run:

```bash
python main.py
```

The app discovers videos, skips the quiet intro, creates varied Shorts-length clips, generates metadata, schedules hourly YouTube uploads, uploads through OAuth, tracks completed uploads, and prevents duplicate uploads.

## Features

- Recursively scans `input/` for `.mp4`, `.MP4`, `.mov`, and `.MOV`
- Supports iPhone 15 Pro Max footage, including HEVC/H.265, H.264, variable frame rate, vertical 9:16 orientation, and rotation metadata
- Uses `ffprobe` to read real video duration
- Uses `ffmpeg` to create YouTube Shorts-compatible MP4 clips
- Preserves vertical orientation and aspect ratio without cropping, resizing, or stretching
- Skips the first 8 seconds of each source video by default
- Creates varied 20 to 30 second clips
- Discards final remainders shorter than 15 seconds
- Creates a final shorter clip when the remainder is 15 to 30 seconds
- Uses persistent clip filenames like `clip_000001.mp4`
- Generates `metadata/metadata.csv`
- Schedules posts hourly from 9 AM through 7 PM America/Los_Angeles
- Uploads through YouTube Data API v3
- Stores OAuth token for future runs
- Tracks uploads in `logs/uploads_log.csv`
- Creates tracker files at `metadata/video_tracker.csv` and `metadata/video_tracker.xlsx`
- Skips clips that were already uploaded
- Writes rotating logs to `logs/app.log`
- Moves processed source videos from `input/` to `uploaded/`

## Folder Structure

```text
project/
  input/
  processing/
  clips/
  uploaded/
  metadata/
  logs/
  credentials/
    credentials.json
  config.py
  generate_clips.py
  generate_metadata.py
  youtube_upload.py
  scheduler.py
  dashboard.py
  main.py
  requirements.txt
  README.md
  .env.example
```

## Installation

### 1. Install Homebrew

If Homebrew is not installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. Install FFmpeg

```bash
brew install ffmpeg
```

Verify:

```bash
ffmpeg -version
ffprobe -version
```

### 3. Create a Virtual Environment

From the project folder:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If your Mac uses `python3` instead of `python3.11`, this is fine:

```bash
python3 -m venv .venv
```

## Google Cloud and YouTube API Setup

### 1. Create a Google Cloud Project

1. Go to Google Cloud Console.
2. Create a new project.
3. Open `APIs & Services`.
4. Enable `YouTube Data API v3`.

### 2. Configure OAuth Consent

1. Open `APIs & Services` -> `OAuth consent screen`.
2. Choose `External` unless you are using a Workspace-only account.
3. Add yourself as a test user.
4. Save the consent setup.

### 3. Create OAuth Credentials

1. Open `APIs & Services` -> `Credentials`.
2. Click `Create Credentials`.
3. Choose `OAuth client ID`.
4. Application type: `Desktop app`.
5. Download the JSON file.
6. Rename it to `credentials.json`.
7. Place it here:

```text
credentials/credentials.json
```

On first run, the app opens a browser for Google OAuth. After authentication, it saves:

```text
credentials/token.json
```

Future runs reuse and refresh this token automatically.

## Configuration

Edit `config.py` to change major settings.

Important defaults:

```python
SKIP_INTRO_SECONDS = 8
CLIP_MIN_SECONDS = 20
CLIP_MAX_SECONDS = 30
MINIMUM_CLIP_LENGTH = 15
POST_START_HOUR = 9
POST_END_HOUR = 19
POST_INTERVAL_HOURS = 1
SCHEDULE_AFTER_EXISTING_UPLOADS = True
MAX_UPLOADS_PER_RUN = 11
TIMEZONE = "America/Los_Angeles"
YOUTUBE_CATEGORY = "10"
```

Titles are generated from `MOOD_WORDS` and formatted like:

```text
VIBE 🍃 #foryou #shorts #viral #music #church #Jesus #God #love
DREAM 🍃 #foryou #shorts #viral #music #church #Jesus #God #love
SERENE 🍃 #foryou #shorts #viral #music #church #Jesus #God #love
```

Upload descriptions are generated as:

```text
#foryou #shorts #viral #music #church #Jesus #God #love
```

Hashtags are loaded from `config.py`.

## Running the Application

1. Put source videos into `input/`.
2. Activate the virtual environment:

```bash
source .venv/bin/activate
```

3. Run:

```bash
python main.py
```

The first run requires OAuth. A browser window will open. Sign in to the YouTube account where you want to upload Shorts.

## Auto-Watch Mode

To leave the app running and automatically process new videos when they are added to `input/`, run:

```bash
python watch.py
```

The watcher checks the input folder every 10 seconds. When it sees a new `.mp4` or `.mov`, it waits until the file size stops changing so large iPhone videos are not processed while they are still copying. Press `Control+C` to stop watching.

## Local Dashboard

To open a browser dashboard on your Mac, run:

```bash
python dashboard.py
```

Then open:

```text
http://localhost:8000/
```

The dashboard lets you add videos to `input/`, run the automation, preview recent clips, and open the tracker files. Keep Terminal open while using the dashboard. Press `Control+C` to stop it.

## Scheduling Behavior

The app schedules uploads hourly from 9 AM through 7 PM in `America/Los_Angeles`.

That is 11 uploads per day:

```text
9 AM
10 AM
11 AM
12 PM
1 PM
2 PM
3 PM
4 PM
5 PM
6 PM
7 PM
```

For 40 clips:

```text
Day 1: 11 uploads
Day 2: 11 uploads
Day 3: 11 uploads
Day 4: 7 uploads
```

Publish times are always generated in the future.

## Duplicate Prevention

Successful uploads are logged in:

```text
logs/uploads_log.csv
```

The app checks this file before uploading. If a clip filename is already marked as uploaded or failed, it is skipped on future runs so older clips are not repeatedly retried.

By default, each run only queues clips generated during that same run. Existing files already sitting in `clips/` are not uploaded again just because they are present.

## Video Tracker

Every run writes tracker files here:

```text
metadata/video_tracker.csv
metadata/video_tracker.xlsx
```

The tracker includes:

- original source video filename
- generated clip filename
- clip start and duration
- generated title
- YouTube upload status
- YouTube video ID
- scheduled publish time
- YouTube view count
- YouTube like count
- YouTube comment count
- YouTube privacy/upload status
- YouTube stats refresh timestamp
- TikTok status columns reserved for future TikTok posting support

Open `metadata/video_tracker.xlsx` in Excel or Numbers.

The dashboard also has a `Refresh YouTube Stats` button. Because this reads YouTube video data, Google may ask you to sign in again after the app adds the YouTube read-only permission.

## TikTok Posting

TikTok posting is possible through TikTok's official Content Posting API, but it requires a TikTok developer app, OAuth, and Content Posting API access. Direct posting clients must pass TikTok audit before public posting is allowed. Until the client is audited, TikTok restricts direct posts from that app to private visibility.

The tracker already includes TikTok status columns so TikTok posting can be added cleanly once the TikTok developer setup is ready.

## Logs

Application logs are written to:

```text
logs/app.log
```

The logger uses rotating file handlers so logs do not grow forever.

## Troubleshooting

### Missing ffmpeg or ffprobe

Install FFmpeg:

```bash
brew install ffmpeg
```

### Missing credentials.json

Create OAuth Desktop credentials in Google Cloud and place them at:

```text
credentials/credentials.json
```

### OAuth Browser Did Not Open

Check that you are running on macOS with a normal desktop browser available. Re-run:

```bash
python main.py
```

### YouTube API Quota Errors

YouTube uploads consume API quota. If quota is exhausted, wait for quota reset or request additional quota from Google Cloud.

### Corrupted Video

Invalid or corrupted videos are logged and skipped. Other videos continue processing.

### CSV Corruption

If `metadata.csv` or `uploads_log.csv` cannot be parsed, the app moves it aside with a `.corrupt` suffix and continues with a fresh file.

## Future Enhancements

- Add automatic thumbnail generation
- Add per-source-video grouping in metadata
- Add dry-run mode
- Add retry queues for failed uploads
- Add optional cloud backup for logs and metadata
- Add a small desktop UI
