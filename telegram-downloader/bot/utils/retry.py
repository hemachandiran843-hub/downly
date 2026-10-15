"""Helpers for Telegram flood-control handling."""

from __future__ import annotations

from datetime import timedelta


def retry_after_seconds(value: float | int | timedelta | None, default: float = 1.0) -> float:
    """Normalise a ``RetryAfter.retry_after`` value to seconds.

    PTB reports either a number of seconds or a :class:`datetime.timedelta`.
    """
    if value is None:
        return default
    if isinstance(value, timedelta):
        return max(0.0, value.total_seconds())
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default
