"""File-type detection, filename extraction and disk helpers.

File type is decided from several independent signals (content type, filename
extension, magic bytes) because none of them can be trusted alone.
"""

from __future__ import annotations

import shutil
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bot.downloader.types import MediaKind
from bot.exceptions import InsufficientDiskSpaceError
from bot.utils.security import sanitize_filename

# --------------------------------------------------------------- content type
_CONTENT_TYPE_KIND: dict[str, MediaKind] = {
    "video/mp4": MediaKind.VIDEO,
    "video/mpeg": MediaKind.VIDEO,
    "video/quicktime": MediaKind.VIDEO,
    "video/x-matroska": MediaKind.VIDEO,
    "video/webm": MediaKind.VIDEO,
    "video/x-msvideo": MediaKind.VIDEO,
    "video/x-flv": MediaKind.VIDEO,
    "video/mp2t": MediaKind.VIDEO,
    "application/vnd.apple.mpegurl": MediaKind.VIDEO,
    "audio/mpeg": MediaKind.AUDIO,
    "audio/mp4": MediaKind.AUDIO,
    "audio/x-m4a": MediaKind.AUDIO,
    "audio/aac": MediaKind.AUDIO,
    "audio/wav": MediaKind.AUDIO,
    "audio/x-wav": MediaKind.AUDIO,
    "audio/flac": MediaKind.AUDIO,
    "audio/x-flac": MediaKind.AUDIO,
    "audio/ogg": MediaKind.AUDIO,
    "audio/opus": MediaKind.AUDIO,
    "audio/webm": MediaKind.AUDIO,
    "image/jpeg": MediaKind.IMAGE,
    "image/png": MediaKind.IMAGE,
    "image/gif": MediaKind.IMAGE,
    "image/webp": MediaKind.IMAGE,
    "image/bmp": MediaKind.IMAGE,
    "image/tiff": MediaKind.IMAGE,
    "image/svg+xml": MediaKind.IMAGE,
    "image/heic": MediaKind.IMAGE,
    "application/pdf": MediaKind.DOCUMENT,
    "application/epub+zip": MediaKind.DOCUMENT,
    "application/msword": MediaKind.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": MediaKind.DOCUMENT,
    "application/vnd.ms-excel": MediaKind.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": MediaKind.DOCUMENT,
    "text/plain": MediaKind.DOCUMENT,
    "text/csv": MediaKind.DOCUMENT,
    "application/rtf": MediaKind.DOCUMENT,
    "application/zip": MediaKind.ARCHIVE,
    "application/x-zip-compressed": MediaKind.ARCHIVE,
    "application/x-rar-compressed": MediaKind.ARCHIVE,
    "application/vnd.rar": MediaKind.ARCHIVE,
    "application/x-7z-compressed": MediaKind.ARCHIVE,
    "application/x-tar": MediaKind.ARCHIVE,
    "application/gzip": MediaKind.ARCHIVE,
    "application/x-bzip2": MediaKind.ARCHIVE,
    "application/octet-stream": MediaKind.UNKNOWN,
    "binary/octet-stream": MediaKind.UNKNOWN,
    "text/html": MediaKind.HTML,
    "application/xhtml+xml": MediaKind.HTML,
    "application/json": MediaKind.DOCUMENT,
}

_EXTENSION_KIND: dict[str, MediaKind] = {
    "mp4": MediaKind.VIDEO,
    "m4v": MediaKind.VIDEO,
    "mkv": MediaKind.VIDEO,
    "webm": MediaKind.VIDEO,
    "mov": MediaKind.VIDEO,
    "avi": MediaKind.VIDEO,
    "flv": MediaKind.VIDEO,
    "ts": MediaKind.VIDEO,
    "mpg": MediaKind.VIDEO,
    "mpeg": MediaKind.VIDEO,
    "mp3": MediaKind.AUDIO,
    "m4a": MediaKind.AUDIO,
    "aac": MediaKind.AUDIO,
    "wav": MediaKind.AUDIO,
    "flac": MediaKind.AUDIO,
    "ogg": MediaKind.AUDIO,
    "opus": MediaKind.AUDIO,
    "jpg": MediaKind.IMAGE,
    "jpeg": MediaKind.IMAGE,
    "png": MediaKind.IMAGE,
    "gif": MediaKind.IMAGE,
    "webp": MediaKind.IMAGE,
    "bmp": MediaKind.IMAGE,
    "tiff": MediaKind.IMAGE,
    "svg": MediaKind.IMAGE,
    "heic": MediaKind.IMAGE,
    "pdf": MediaKind.DOCUMENT,
    "txt": MediaKind.DOCUMENT,
    "epub": MediaKind.DOCUMENT,
    "doc": MediaKind.DOCUMENT,
    "docx": MediaKind.DOCUMENT,
    "xls": MediaKind.DOCUMENT,
    "xlsx": MediaKind.DOCUMENT,
    "csv": MediaKind.DOCUMENT,
    "json": MediaKind.DOCUMENT,
    "rtf": MediaKind.DOCUMENT,
    "zip": MediaKind.ARCHIVE,
    "rar": MediaKind.ARCHIVE,
    "7z": MediaKind.ARCHIVE,
    "tar": MediaKind.ARCHIVE,
    "gz": MediaKind.ARCHIVE,
    "bz2": MediaKind.ARCHIVE,
}

_CONTENT_TYPE_EXTENSION: dict[str, str] = {
    "video/mp4": "mp4",
    "video/mpeg": "mpeg",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
    "video/webm": "webm",
    "video/x-msvideo": "avi",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/heic": "heic",
    "application/pdf": "pdf",
    "application/epub+zip": "epub",
    "application/zip": "zip",
    "application/x-rar-compressed": "rar",
    "application/vnd.rar": "rar",
    "application/x-7z-compressed": "7z",
    "application/x-tar": "tar",
    "application/gzip": "gz",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/json": "json",
    "application/msword": "doc",
    "application/rtf": "rtf",
}

#: Extensions that must never be executed even if a preview is requested.
EXECUTABLE_EXTENSIONS = frozenset(
    {
        "exe", "msi", "bat", "cmd", "com", "scr", "pif", "ps1", "psm1", "sh", "bash", "zsh",
        "py", "pyc", "jar", "apk", "app", "dll", "so", "vbs", "vbe", "js", "jse", "wsf",
        "hta", "cpl", "lnk", "reg", "deb", "rpm", "dmg", "run", "bin",
    }
)


def base_content_type(content_type: str | None) -> str:
    value = (content_type or "").split(";", 1)[0].strip().lower()
    return value


def kind_from_content_type(content_type: str | None) -> MediaKind:
    value = base_content_type(content_type)
    if not value:
        return MediaKind.UNKNOWN
    if value in _CONTENT_TYPE_KIND:
        return _CONTENT_TYPE_KIND[value]
    major = value.split("/", 1)[0]
    if major == "video":
        return MediaKind.VIDEO
    if major == "audio":
        return MediaKind.AUDIO
    if major == "image":
        return MediaKind.IMAGE
    if major == "text":
        return MediaKind.DOCUMENT
    if value.endswith("+zip") or value.endswith("-zip"):
        return MediaKind.ARCHIVE
    return MediaKind.UNKNOWN


def extension_from_content_type(content_type: str | None) -> str | None:
    return _CONTENT_TYPE_EXTENSION.get(base_content_type(content_type))


def kind_from_extension(extension: str | None) -> MediaKind | None:
    if not extension:
        return None
    return _EXTENSION_KIND.get(extension.strip().lstrip(".").lower())


def extension_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    if 1 <= len(ext) <= 12 and ext.isalnum():
        return ext
    return None


def filename_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    name = unquote(path.rsplit("/", 1)[-1]).strip()
    if not name or name in {"/", ""}:
        return None
    return sanitize_filename(name)


def filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = Message()
    message["Content-Disposition"] = header
    name = message.get_filename()
    if not name:
        return None
    return sanitize_filename(unquote(name))


# ----------------------------------------------------------------- magic bytes
def sniff_magic(data: bytes | None) -> tuple[MediaKind, str] | None:
    """Identify a file from its first bytes. Returns ``(kind, extension)``."""
    if not data:
        return None
    head = data[:16]
    if head.startswith(b"%PDF"):
        return MediaKind.DOCUMENT, "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return MediaKind.IMAGE, "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return MediaKind.IMAGE, "png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return MediaKind.IMAGE, "gif"
    if head.startswith(b"BM"):
        return MediaKind.IMAGE, "bmp"
    if head.startswith(b"fLaC"):
        return MediaKind.AUDIO, "flac"
    if head.startswith(b"OggS"):
        return MediaKind.AUDIO, "ogg"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):
        return MediaKind.AUDIO, "mp3"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return MediaKind.VIDEO, "mkv"
    if head.startswith(b"Rar!\x1a\x07"):
        return MediaKind.ARCHIVE, "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return MediaKind.ARCHIVE, "7z"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return MediaKind.ARCHIVE, "zip"
    if head.startswith(b"\x1f\x8b"):
        return MediaKind.ARCHIVE, "gz"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return MediaKind.AUDIO, "wav"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return MediaKind.VIDEO, "avi"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return MediaKind.IMAGE, "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        if brand.startswith(b"qt"):
            return MediaKind.VIDEO, "mov"
        if brand.startswith(b"m4a") or brand.startswith(b"m4b"):
            return MediaKind.AUDIO, "m4a"
        if brand.startswith(b"heic") or brand.startswith(b"heif"):
            return MediaKind.IMAGE, "heic"
        if brand.startswith(b"3gp"):
            return MediaKind.VIDEO, "3gp"
        return MediaKind.VIDEO, "mp4"
    if head.startswith(b"\x00\x00\x01\xba") or head.startswith(b"\x00\x00\x01\xb3"):
        return MediaKind.VIDEO, "mpeg"
    return None


def is_probably_html(data: bytes | None) -> bool:
    if not data:
        return False
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head>" in head


def classify(
    *,
    content_type: str | None = None,
    url: str | None = None,
    filename: str | None = None,
    magic: bytes | None = None,
) -> tuple[MediaKind, str | None]:
    """Combine all signals into a kind plus the best known extension.

    Magic bytes win (they describe the actual content), then the content type,
    then the filename/URL extension.
    """
    sniffed = sniff_magic(magic)
    if sniffed is not None:
        kind, ext = sniffed
        if kind is MediaKind.UNKNOWN:
            kind = kind_from_content_type(content_type)
        return kind, ext

    if magic and is_probably_html(magic):
        return MediaKind.HTML, "html"

    kind = kind_from_content_type(content_type)
    if kind is MediaKind.UNKNOWN:
        for candidate in (filename, url):
            if not candidate:
                continue
            candidate_ext = candidate.rsplit(".", 1)[-1] if "." in candidate else None
            guess = kind_from_extension(candidate_ext)
            if guess is not None:
                kind = guess
                break

    extension: str | None = None
    for candidate in (filename, filename_from_url(url or "")):
        if not candidate or "." not in candidate:
            continue
        candidate_ext = candidate.rsplit(".", 1)[-1].lower()
        if kind_from_extension(candidate_ext) is not None:
            extension = candidate_ext
            break
    if extension is None:
        extension = extension_from_content_type(content_type)
    return kind, extension


def ensure_extension(filename: str, extension: str | None) -> str:
    """Give ``filename`` an extension that matches the detected content.

    An existing extension is respected (it is usually meaningful, e.g. ``.rst``)
    except when it is executable: a file whose magic bytes say ``PDF`` must not
    reach the user as ``payload.exe``.  The upload path classifies by content, so
    a lying name cannot change how Telegram receives the file.
    """
    if not extension:
        return filename
    base = filename or "download"
    if "." in base and not base.endswith("."):
        stem, _, current = base.rpartition(".")
        current = current.lower()
        if current in EXECUTABLE_EXTENSIONS and current != extension:
            return f"{stem}.{extension}"
        if current:
            return base
    return f"{base}.{extension}"


def is_executable_name(filename: str) -> bool:
    return "." in (filename or "") and filename.rsplit(".", 1)[-1].lower() in EXECUTABLE_EXTENSIONS


# --------------------------------------------------------------- disk helpers
def available_disk_bytes(path: str | Path) -> int:
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return 0


def ensure_disk_space(path: str | Path, needed_bytes: int, min_free_mb: int = 0) -> None:
    """Raise :class:`InsufficientDiskSpaceError` when the download cannot fit."""
    free = available_disk_bytes(path)
    if free <= 0:
        return  # Cannot determine (exotic filesystem) - do not block the download.
    reserve = max(0, min_free_mb) * 1024 * 1024
    required = max(0, needed_bytes) + reserve
    if free < required:
        from bot.utils.formatting import human_size

        raise InsufficientDiskSpaceError(
            f"need {human_size(required)}, only {human_size(free)} free",
            user_message=(
                "❌ Not enough disk space to download this file.\n"
                f"Required: {human_size(required)}\nAvailable: {human_size(free)}"
            ),
        )
