"""Human-readable formatting helpers used in Telegram messages."""

from __future__ import annotations

from datetime import UTC, datetime

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_BAR_FILLED = "█"
_BAR_EMPTY = "░"


def human_size(num_bytes: float | int | None) -> str:
    """``1536`` -> ``'1.5 KB'``."""
    if num_bytes is None:
        return "unknown"
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0:
        return "unknown"
    if value < 1024:
        return f"{int(value)} B"
    for index, unit in enumerate(_SIZE_UNITS[1:], start=1):
        divisor = 1024.0**index
        if value < divisor * 1024 or unit == _SIZE_UNITS[-1]:
            return f"{value / divisor:.1f} {unit}"
    return f"{value:.1f} B"


def human_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "—"
    return f"{human_size(bytes_per_second)}/s"


def human_duration(seconds: float | int | None) -> str:
    """``3725`` -> ``'1h 02m 05s'``; short form for small values."""
    if seconds is None:
        return "—"
    try:
        total = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def progress_bar(percent: float | None, width: int = 20) -> str:
    """``61`` -> ``'████████████░░░░░░░░'`` (always ``width`` characters)."""
    width = max(4, int(width))
    if percent is None:
        return _BAR_EMPTY * width
    pct = min(100.0, max(0.0, float(percent)))
    filled = int(round(pct / 100.0 * width))
    filled = min(width, max(0, filled))
    return _BAR_FILLED * filled + _BAR_EMPTY * (width - filled)


def percent(completed: float | int | None, total: float | int | None) -> float | None:
    """Percentage of ``completed`` out of ``total``, or ``None`` if unknown."""
    if not total or total <= 0 or completed is None:
        return None
    return min(100.0, max(0.0, float(completed) / float(total) * 100.0))


def eta_seconds(remaining_bytes: float | None, bytes_per_second: float | None) -> float | None:
    if not remaining_bytes or not bytes_per_second or bytes_per_second <= 0 or remaining_bytes <= 0:
        return None
    return remaining_bytes / bytes_per_second


def truncate(text: str | None, limit: int = 120) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def escape_html(text: str | None) -> str:
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def local_time(value: datetime | str | None) -> str:
    """Format a stored timestamp (ISO string or datetime) for display."""
    if value is None:
        return "—"
    parsed: datetime
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
