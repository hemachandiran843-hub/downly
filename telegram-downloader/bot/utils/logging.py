"""Logging helpers: structured output with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Telegram bot tokens: 123456789:AA...
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{20,}\b"),
    # Authorization / cookie / api-key headers: the whole value is removed, since
    # "Authorization: Bearer <secret>" hides the secret behind a scheme word.
    re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[_-]?key)\b\s*[:=][^\n]*"),
    # Common key query params.
    re.compile(r"(?i)\b(token|access_token|api_key|apikey|signature|sig|password)=([^&\s]+)"),
    re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+(@)"),
)

REDACTED = "***redacted***"


def redact(text: str) -> str:
    """Remove credentials from a string before it is logged."""
    if not text:
        return text
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            cleaned = pattern.sub(lambda m: m.group(1) + REDACTED + m.group(2), cleaned)
        else:
            cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


class RedactionFilter(logging.Filter):
    """Scrubs credentials from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "extra_fields", None)
        if extra:
            base += " " + " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        # Exception tracebacks are formatted here, so scrub the whole line.
        return redact(base)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def configure_stdio_encoding() -> None:
    """Make stdout/stderr UTF-8 safe.

    Windows consoles (and any redirected stream) may default to cp1252, which
    cannot encode the emoji used in user-facing messages and log lines.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):  # pragma: no cover - exotic streams
            continue


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure root logging once. Safe to call multiple times."""
    configure_stdio_encoding()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactionFilter())
    if (fmt or "").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _TextFormatter("%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s", "%Y-%m-%d %H:%M:%S")
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))

    # These are noisy at INFO and can include request internals.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
