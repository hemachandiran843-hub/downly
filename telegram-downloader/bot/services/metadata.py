"""Turn probe/yt-dlp metadata into short, friendly Telegram messages."""

from __future__ import annotations

from urllib.parse import urlsplit

from bot.downloader.router import RouterDecision
from bot.downloader.types import DownloadResult, MediaKind
from bot.utils.formatting import human_duration, human_size, truncate

_KIND_HEADLINES: dict[MediaKind, str] = {
    MediaKind.VIDEO: "📦 Video detected",
    MediaKind.AUDIO: "🎵 Audio detected",
    MediaKind.IMAGE: "🖼 Image detected",
    MediaKind.DOCUMENT: "📄 Document detected",
    MediaKind.ARCHIVE: "🗜 Archive detected",
    MediaKind.HTML: "🌐 Web page detected",
    MediaKind.UNKNOWN: "📁 File detected",
}

CAPTION_LIMIT = 900


def safe_domain(url: str | None) -> str:
    if not url:
        return "unknown"
    try:
        host = urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"
    return host


def headline(kind: MediaKind) -> str:
    return _KIND_HEADLINES.get(kind, "📁 File detected")


def preview_text(
    decision: RouterDecision,
    *,
    max_file_size_bytes: int,
    telegram_upload_limit_bytes: int,
) -> str:
    """Build the "here is what I found" card, e.g.::

        📦 Video detected

        Title: Example Video
        Type: MP4
        Size: 182 MB

        Ready to download?
    """
    kind = decision.kind
    lines = [headline(kind), ""]
    if decision.title:
        lines.append(f"Title: {truncate(decision.title, 90)}")
    lines.append(f"Source: {safe_domain(decision.url)}")
    lines.append(f"Method: {'direct download' if decision.method.value == 'direct' else 'yt-dlp extractor'}")

    ext = decision.info.get("ext") if decision.info else None
    if ext:
        lines.append(f"Type: {str(ext).upper()}")
    elif decision.probe is not None and decision.probe.content_type:
        lines.append(f"Type: {decision.probe.content_type.split(';')[0]}")

    size = decision.expected_size
    lines.append(f"Size: {human_size(size) if size else 'unknown'}")
    if decision.duration:
        lines.append(f"Duration: {human_duration(decision.duration)}")

    warnings: list[str] = []
    if size and size > max_file_size_bytes:
        warnings.append(f"⚠️ Larger than the configured download limit ({human_size(max_file_size_bytes)}).")
    elif size and size > telegram_upload_limit_bytes:
        warnings.append(
            f"⚠️ Above the Telegram upload limit ({human_size(telegram_upload_limit_bytes)}): "
            "it can be downloaded but not sent back."
        )
    if kind is MediaKind.UNKNOWN:
        warnings.append("ℹ️ File type could not be detected from the link; I will sniff the content.")

    if warnings:
        lines.append("")
        lines.extend(warnings)
    lines.append("")
    lines.append("Ready to download?")
    return "\n".join(lines)


def result_caption(result: DownloadResult, *, extra: str | None = None) -> str | None:
    """Short caption attached to the uploaded file."""
    parts: list[str] = []
    if result.title and result.title != result.filename:
        parts.append(truncate(result.title, 200))
    if result.kind is MediaKind.VIDEO and result.height:
        parts.append(f"{result.width or '?'}x{result.height}")
    if result.duration:
        parts.append(human_duration(result.duration))
    method = "yt-dlp" if result.method.value == "ytdlp" else "direct link"
    parts.append(f"{result.filename} · {human_size(result.size_bytes)} · {method}")
    if extra:
        parts.append(extra)
    if not parts:
        return None
    return truncate("\n".join(parts), CAPTION_LIMIT)


def completion_message(result: DownloadResult) -> str:
    lines = [
        "✅ Download complete",
        "",
        f"📄 {truncate(result.filename, 80)}",
        f"📦 {human_size(result.size_bytes)}",
        f"🔗 {safe_domain(result.source_url)}",
    ]
    return "\n".join(lines)
