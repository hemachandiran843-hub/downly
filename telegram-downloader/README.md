# Telegram Downloader Bot

A production-ready Telegram bot that turns **any link a user sends into a file**.

Send it a URL and it inspects the link, picks the right download method, streams the
file with live progress, uploads it back to the chat, and deletes the temporary copy.

```
You:  https://example.com/clip.mp4
Bot:  🔍 Analyzing link…
      📥 Downloading  ████████████░░░░░░░░ 61%
      Size: 128.0 MB / 210.0 MB · Speed: 8.4 MB/s · ETA: 10s
      ✅ Download complete            [❌ Cancel]
      <the file>
```

---

## Table of contents

- [Features](#features)
- [How it decides what to do](#how-it-decides-what-to-do)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration (`.env`)](#configuration-env)
- [Running locally](#running-locally)
- [Running with Docker](#running-with-docker)
- [Bot commands](#bot-commands)
- [Admin setup](#admin-setup)
- [Running tests and static checks](#running-tests-and-static-checks)
- [How it behaves](#how-it-behaves)
- [Security model](#security-model)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Legal / acceptable use](#legal--acceptable-use)

---

## Features

| Area | What it does |
| --- | --- |
| URL handling | Extracts URLs from prose ("download this: https://… thanks"), accepts up to `MAX_URLS_PER_MESSAGE` per message, processes several links sequentially |
| Download routing | Cheap HTTP probe first: real file streams go through the direct downloader, HTML pages and HLS/DASH manifests go through yt-dlp |
| Direct downloads | Streaming to disk, redirect following, retries with exponential backoff, `Content-Length`, `Content-Disposition`, URL and magic-byte filename detection |
| yt-dlp | Python API (no shelling out), MP4/M4A-first format selection, no needless re-encoding, thumbnails, optional subtitles, playlist support (opt-in) |
| Progress | One edited message per job, throttled to `PROGRESS_UPDATE_INTERVAL_SECONDS`, with a bar, size, speed, ETA and stage |
| Cancellation | Inline `❌ Cancel` button + `/cancel`, aborts direct streams, raises inside yt-dlp hooks, aborts mid-upload, deletes partial files |
| Multi-user | Per-job task, temp directory, filename, status and cancel event; global + per-user concurrency limits |
| Limits | Max download size (checked from headers *and* while streaming), Telegram upload limit, max download duration, disk-space reservation |
| Persistence | SQLite (WAL) with `users` and `downloads` tables plus indexes; interrupted jobs are marked failed on restart |
| Cleanup | Per-job directories removed immediately; periodic sweep of abandoned files older than `TEMP_FILE_MAX_AGE_HOURS` |
| Admin | `/stats`, `/users`, `/cleanup`, `/broadcast` behind `ADMIN_USER_IDS` |
| Ops | Structured text/JSON logging with secret redaction, `.env` configuration, Docker + docker-compose, offline self-test, 337 automated tests |

---

## How it decides what to do

```
URL ──► validate (scheme, host denylist, DNS → all answers public?)
         │
         ├─► inspect (HEAD, or a ranged GET when HEAD is refused)
         │     ├─ real file stream (video/audio/image/document/archive/octet-stream) ─► direct downloader
         │     ├─ HLS/DASH manifest (.m3u8/.mpd)                                     ─► yt-dlp
         │     └─ HTML page / unclassified                                            ─► yt-dlp metadata probe
         │
         └─► no route found ─► friendly error ("that link is a web page, not a file")
```

Both strategies share the same budget checks (max size, disk space, wall-clock
time) and the same progress/cancellation plumbing.

---

## Requirements

- **Python 3.11+** (developed and tested on 3.12/3.13)
- **FFmpeg** – strongly recommended. Needed to merge separate video+audio streams
  (common on streaming sites) and to convert thumbnails. Without it the bot still
  works but falls back to single-file formats. `apt install ffmpeg`, `choco install ffmpeg`,
  or set `FFMPEG_LOCATION`.
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)

---

## Quick start

```powershell
# Windows (PowerShell)
cd telegram-downloader
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
# edit .env and set BOT_TOKEN=...
python run.py
```

```bash
# Linux / macOS
cd telegram-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set BOT_TOKEN=...
python run.py
```

> Docker instead of a venv? See [Running with Docker](#running-with-docker).

---

## Configuration (`.env`)

Copy `.env.example` to `.env`. Every value has a working default except `BOT_TOKEN`.

### Core

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | – | **Required.** Token from @BotFather. Never commit it. |
| `ADMIN_USER_IDS` | – | Comma-separated Telegram user IDs allowed to use admin commands (send `/id` to the bot to learn yours). |
| `DATABASE_PATH` | `data/bot.db` | SQLite file. |
| `DOWNLOADS_DIR` | `downloads` | Where files are kept when `KEEP_FILES=true`. |
| `TEMP_DIR` | `temp` | Per-job working directories (deleted after each job). |
| `DATA_DIR` | `data` | Parent directory for data files. |

### Limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_FILE_SIZE_MB` | `2000` | Maximum size of a single download. Checked from headers before downloading and enforced while streaming. |
| `TELEGRAM_UPLOAD_LIMIT_MB` | `50` | Telegram upload limit **for this bot**. The cloud Bot API rejects files above 50 MB; set 2000 only if you run a [local Bot API server](https://github.com/tdlib/telegram-bot-api). |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Global concurrency. Extra jobs wait in a queue and say so. |
| `MAX_DOWNLOADS_PER_USER` | `2` | Per-user concurrency; further links are refused with a clear message. |
| `MAX_URLS_PER_MESSAGE` | `3` | Extra links in one message are reported and ignored. |

### Timeouts and retries

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOWNLOAD_TIMEOUT_SECONDS` | `60` | Connect/read timeout for a single HTTP request. |
| `MAX_DOWNLOAD_TIME_MINUTES` | `60` | Wall-clock budget for one job; exceeded → timeout message. |
| `MAX_REDIRECTS` | `10` | Redirect hops allowed (each hop is re-validated). |
| `PROBE_TIMEOUT_SECONDS` | `20` | Timeout for the inspection request. |
| `METADATA_TIMEOUT_SECONDS` | `30` | Timeout for a yt-dlp metadata extraction. |
| `MAX_RETRIES` | `3` | Retries for transient network errors (`429/5xx`, timeouts, resets). Permanent errors such as 404 are never retried. |
| `MIN_FREE_DISK_MB` | `512` | Free space kept in reserve; the download is refused if it cannot fit. |

### Housekeeping and UX

| Variable | Default | Meaning |
| --- | --- | --- |
| `TEMP_FILE_MAX_AGE_HOURS` | `6` | Abandoned temp entries older than this are swept. |
| `CLEANUP_INTERVAL_MINUTES` | `30` | Cleanup sweep interval. |
| `KEEP_FILES` | `false` | `true` keeps a copy in `DOWNLOADS_DIR/<user_id>/` after a successful upload. |
| `PROGRESS_UPDATE_INTERVAL_SECONDS` | `2.5` | Throttle for editing the progress message (2–5 recommended). |
| `CONFIRM_BEFORE_DOWNLOAD` | `false` | `true` shows a metadata card with **⬇️ Download / ❌ Cancel** first. Default is zero-touch: analyze → download. |

### Downloader

| Variable | Default | Meaning |
| --- | --- | --- |
| `FFMPEG_LOCATION` | – | Path to the ffmpeg binary; empty = use `$PATH`. |
| `ALLOW_PLAYLISTS` | `false` | `true` allows up to 5 entries of a playlist to be downloaded. |
| `WRITE_SUBTITLES` | `false` | Download subtitles when available. |
| `SUBTITLE_LANGS` | `en` | Comma-separated subtitle languages. |
| `USER_AGENT` | bot UA | User agent for HTTP requests. |

### Logging

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `LOG_FORMAT` | `text` | `text` or `json` (JSON is handy for log collectors). |

Secrets (bot token, cookies, API keys, `Authorization` headers) are redacted from
every log record, including exception tracebacks.

---

## Running locally

```bash
# 1. offline self-test (no token, no network): config, dirs, database, HTTP session, disk, ffmpeg
python run.py --check

# 2. start the bot
python run.py

# 3. optional overrides
python run.py --log-level DEBUG
python run.py --version
```

You should see something like:

```
2026-10-15 19:05:11 | INFO    | bot.main | bot started as @your_bot
2026-10-15 19:05:11 | INFO    | bot.main | ffmpeg available: True
2026-10-15 19:05:11 | INFO    | bot.main | starting polling loop
```

Stop with `Ctrl+C` — active downloads are cancelled and temporary files removed.

---

## Running with Docker

The image contains FFmpeg and runs as an unprivileged user (`uid 10001`).

```bash
# build and start (compose reads .env from the project root)
docker compose up -d --build

# logs
docker compose logs -f

# stop
docker compose down
```

```bash
# plain docker, no compose
docker build -t telegram-downloader .
docker run -d --name telegram-downloader --restart unless-stopped \
  --env-file .env \
  -v "$PWD/data:/app/data" -v "$PWD/downloads:/app/downloads" -v "$PWD/temp:/app/temp" \
  telegram-downloader
```

Notes:

- `data/`, `downloads/` and `temp/` are bind-mounted, so the database and any
  `KEEP_FILES` copies survive restarts.
- **Linux hosts:** the container user (`10001`) must be able to write those
  directories. If the bot logs a database permission error, run:
  `mkdir -p data downloads temp && sudo chown -R 10001:10001 data downloads temp`
- The container health check is `python run.py --check`, which needs no token.
- Telegram's cloud API caps uploads at 50 MB. To send bigger files, run a local
  Bot API server and point `TELEGRAM_UPLOAD_LIMIT_MB` (up to 2000) at it, e.g.
  add `TELEGRAM_API_URL` support through your own Bot API deployment.

---

## Bot commands

| Command | Who | What |
| --- | --- | --- |
| `/start` | everyone | Welcome message + **Help** button |
| `/help` | everyone | Supported links, limits, commands, common errors |
| `/status` | everyone | Active downloads with stage/progress, or the last five results |
| `/cancel` | everyone | Cancels all of that user's active downloads |
| `/settings` | everyone | Inline toggles: send as document, progress updates |
| `/id` | everyone | Shows your Telegram ID (needed for `ADMIN_USER_IDS`) |
| `/stats` | admin | Users, active/completed/failed/cancelled, total bytes, temp usage, uptime, ffmpeg |
| `/users` | admin | 25 most recent users with download counts |
| `/cleanup` | admin | Runs the temp-file sweep now |
| `/broadcast` | admin | `/broadcast <text>` or reply with `/broadcast` to message all users |

Admins are re-checked inside every admin handler (not just by filter).

---

## Admin setup

1. Send `/id` to your bot — it replies with your numeric Telegram ID.
2. Put it in `.env`: `ADMIN_USER_IDS=123456789,987654321`
3. Restart the bot and run `/stats`.

---

## Running tests and static checks

```bash
pip install -r requirements-dev.txt   # pytest + pytest-asyncio (plus optional ruff/mypy)

pytest                                # 337 tests, no network required
pytest -q                             # quieter
pytest tests/test_manager.py -v       # one module

ruff check .                          # lint (config in pyproject.toml)
mypy                                  # type check bot/ and run.py
```

Everything is mocked: HTTP responses, Telegram API calls and yt-dlp are replaced
by test doubles, so the suite runs offline and in CI. Live behaviour is covered
separately by the manual checks described in [Limitations](#limitations).

---

## How it behaves

**Progress.** Each job owns one Telegram message. A small ticker edits it at most
once every `PROGRESS_UPDATE_INTERVAL_SECONDS`, so chunk-by-chunk updates never
flood the chat. Stages: 🔍 Analyzing → ⏳ Queued → 📥 Downloading → ⚙️ Processing
→ 📤 Uploading → ✅ Complete / ❌ Failed / ❌ Cancelled. Flood control
(`RetryAfter`) is respected and "message not modified" errors are ignored.

**Cancellation.** `❌ Cancel` (or `/cancel`, or `/status` followed by cancel)
sets the job's cancel event. The direct downloader checks it before every chunk,
yt-dlp raises inside its progress hook, an in-flight upload is aborted, partial
files are deleted and the message is updated to `❌ Download cancelled.`
A queued job is cancelled without ever touching the network.

**Retries.** Transient failures (connection errors, timeouts, `429`, `5xx`) are
retried up to `MAX_RETRIES` with exponential backoff and a "retrying" note in the
progress message. Permanent failures (`400/401/403/404/410/451`) fail fast.

**Media handling.** Video → `send_video` (with thumbnail when available),
audio → `send_audio`, small images → `send_photo`, GIFs → `send_animation`,
everything else → `send_document`. If Telegram rejects a specialised media type,
the bot automatically retries as a document. Files above the configured upload
limit are refused *before* uploading with an exact size comparison; if the size
is already known from the probe, the bot refuses before downloading at all.

**Cleanup.** Every job gets its own `temp/<job_id>/` directory. It is removed when
the job ends (success, failure or cancel). A periodic sweep deletes anything left
behind by a crash. `KEEP_FILES=true` moves the finished file to
`downloads/<user_id>/` first.

**Restart safety.** Downloads that were in flight when the process stopped are
marked `failed` with `interrupted by restart` on the next start, so `/status` and
`/stats` never show phantom jobs.

---

## Security model

Every user-supplied URL is untrusted:

- only `http`/`https`; URLs with embedded credentials are rejected
- hostname denylist (`localhost`, `*.local`, `*.internal`, cloud metadata names)
- literal IPs and **every DNS answer** are checked against loopback, private,
  link-local, multicast, reserved and unspecified ranges — including IPv4
  addresses wrapped in IPv6 (`::ffff:`, `2002::/16`, NAT64 `64:ff9b::/96`)
- a custom `aiohttp` resolver re-applies these rules **at connect time**, which
  closes the DNS-rebinding gap between validation and connection
- redirects are followed manually, one hop at a time, and each hop is re-validated,
  so a safe URL cannot redirect into the internal network
- filenames are sanitised and written through `unique_path()`/`safe_destination()`,
  which cannot escape the job directory (path traversal is impossible)
- nothing is ever executed; no shell, no `os.system`, no `eval`
- disk usage, concurrency, file size, redirect count and wall-clock time are bounded
- downloaded content is stored under per-job directories that other users cannot
  reference
- the bot never supplies cookies, credentials or session data, and never bypasses
  logins, paywalls, DRM or other access controls

---

## Project structure

```
telegram-downloader/
├── bot/
│   ├── __init__.py
│   ├── main.py                  # application assembly, post_init/post_shutdown, self-test
│   ├── config.py                # env-driven settings, hostname denylist
│   ├── context.py               # shared service container (bot_data)
│   ├── database.py              # async SQLite: users, downloads, stats, restart recovery
│   ├── exceptions.py            # error hierarchy with user-facing messages
│   ├── handlers/
│   │   ├── __init__.py          # handler registration + command menu
│   │   ├── start.py             # /start /help /status /cancel /settings /id
│   │   ├── download.py          # URL extraction + job submission
│   │   ├── callbacks.py         # cancel / confirm / settings / help buttons
│   │   └── admin.py             # /stats /users /cleanup /broadcast (+ admin_only)
│   ├── downloader/
│   │   ├── __init__.py
│   │   ├── router.py            # direct vs yt-dlp decision + metadata
│   │   ├── direct.py            # streaming HTTP downloader, SafeResolver, retries
│   │   ├── ytdlp.py             # yt-dlp Python API, hooks, cancellation, error mapping
│   │   ├── types.py             # MediaKind, ProbeResult, DownloadResult
│   │   └── utils.py             # content-type/magic-byte detection, disk guards
│   ├── services/
│   │   ├── __init__.py
│   │   ├── manager.py           # job lifecycle, concurrency, queueing, cleanup
│   │   ├── progress.py          # throttled Telegram progress message
│   │   ├── metadata.py          # preview cards and captions
│   │   ├── telegram_upload.py   # media-type selection + document fallback
│   │   └── cleanup.py           # per-job removal + periodic sweep
│   └── utils/
│       ├── __init__.py
│       ├── logging.py           # structured logging + secret redaction + stdio safety
│       ├── security.py          # URL extraction/validation, SSRF rules, filenames
│       ├── formatting.py        # sizes, durations, progress bars, ETA
│       └── retry.py             # Telegram RetryAfter normalisation
├── tests/                       # 337 offline tests (fakes for HTTP, Telegram, yt-dlp)
├── downloads/                   # kept files (KEEP_FILES=true), gitignored
├── temp/                        # per-job working directories, gitignored
├── data/                        # SQLite database, gitignored
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml               # pytest / ruff / mypy configuration
├── .env.example
├── .gitignore
├── README.md
└── run.py                       # entry point (--check, --log-level, --version)
```

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Configuration error: BOT_TOKEN is not set` | Copy `.env.example` to `.env` and paste the token. |
| `Telegram rejected BOT_TOKEN` | The token was revoked or mistyped; get a fresh one from @BotFather. |
| `Another instance of this bot is already running` | Two processes poll the same token — stop one. |
| `FFmpeg was not found` in `--check` | Install ffmpeg or set `FFMPEG_LOCATION`; only affects merging/thumbnail conversion. |
| "That link is a web page, not a downloadable file" | Send the direct media/file link instead of the article/page URL. |
| "The download completed, but the file is too large to upload" | Telegram cloud limit is 50 MB; raise `TELEGRAM_UPLOAD_LIMIT_MB` only with a local Bot API server. |
| "Not enough disk space to download this file" | Free space or lower `MIN_FREE_DISK_MB`. |
| "You already have N download(s) in progress" | Wait or `/cancel`; raise `MAX_DOWNLOADS_PER_USER`. |
| Progress message stops updating | The user deleted the message or blocked the bot; editing is disabled for that job, the download continues. |
| Emoji print as `?` in a redirected console | The CLI forces UTF-8 with `errors="replace"`; set `PYTHONIOENCODING=utf-8` if your terminal differs. |

---

## Limitations

- **A real Telegram token is required** for the end-to-end chat flow. Everything
  except `getMe`/`sendMessage`/`send*` is implemented and tested, and the startup
  path is exercised by the test suite and `run.py --check`.
- **The Docker image was not built in this environment** (the Docker daemon was
  unavailable); the compose file validates with `docker compose config` and the
  Dockerfile follows the standard `python:3.12-slim` + ffmpeg + non-root pattern.
- **Upload progress is indeterminate.** The Bot API does not report upload
  progress, so the 📤 stage shows a spinner, not a percentage.
- **No resume of interrupted transfers.** A retry restarts the download from
  zero; there is no HTTP range resume.
- **FFmpeg is required for merging** separate video/audio streams (many streaming
  sites) and is not installed by the Python requirements.
- **Playlists are opt-in and capped** at 5 entries (`ALLOW_PLAYLISTS=true`).
- **No official Bot API local server integration** is bundled; the upload limit is
  a configuration value you can raise only if you run one.
- **Site coverage depends on yt-dlp**, and the bot intentionally refuses anything
  that requires a login, cookies, DRM or other access control.
- **`KEEP_FILES` copies are not expired automatically** — only `temp/` is swept.
- Downloads from sites that block non-browser clients, or that require a JS
  challenge, will fail with a clear error rather than being circumvented.

---

## Legal / acceptable use

This bot downloads publicly accessible content that the user is authorised to
download. It deliberately does **not** implement DRM circumvention, paywall or
authentication bypass, private-content extraction, credential/session theft,
CAPTCHA bypass or any other access-control circumvention. It never supplies
cookies or credentials and never executes downloaded files.

You are responsible for complying with the terms of service of the sites you
download from, with copyright law, and with Telegram's terms of service in your
jurisdiction.
