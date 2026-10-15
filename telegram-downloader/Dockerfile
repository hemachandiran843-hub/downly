# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg is needed to merge video+audio streams and to remux to MP4.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY run.py ./
COPY .env.example ./

# Unprivileged runtime user owns the writable data directories.
RUN useradd --create-home --uid 10001 botuser \
 && mkdir -p /app/data /app/downloads /app/temp \
 && chown -R botuser:botuser /app
USER botuser

ENV DATABASE_PATH=/app/data/bot.db \
    DOWNLOADS_DIR=/app/downloads \
    TEMP_DIR=/app/temp \
    DATA_DIR=/app/data \
    LOG_FORMAT=text

# Fail the build/start when the app cannot initialise (no token needed).
HEALTHCHECK --interval=60s --timeout=20s --start-period=15s --retries=3 \
  CMD ["python", "run.py", "--check"]

CMD ["python", "run.py"]
