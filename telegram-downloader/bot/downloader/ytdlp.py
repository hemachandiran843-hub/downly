"""yt-dlp integration.

yt-dlp is synchronous, so it runs in a worker thread.  Progress is bridged back
to the event loop, cancellation raises inside the progress hook, and downloads
prefer Telegram-friendly containers (MP4/M4A) without re-encoding unless a merge
is genuinely required.

Only publicly accessible URLs are handled: DRM, paywalls and access controls are
never bypassed and no cookies or credentials are ever supplied.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bot.config import Settings
from bot.downloader.types import DownloadMethod, DownloadResult, MediaKind
from bot.downloader.utils import classify, ensure_disk_space, is_executable_name, kind_from_extension
from bot.exceptions import DownloadCancelled, DownloadTimeoutError, FileTooLargeError, YtDlpError
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Best MP4/M4A first so Telegram gets a playable file, then best of anything.
DEFAULT_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"

#: Media files yt-dlp may leave behind, used to locate the produced file.
_MEDIA_SUFFIXES = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
}
_SIDECAR_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".temp", ".json", ".srt", ".vtt", ".ass"}


class _QuietLogger:
    """Routes yt-dlp's own logging into our logger (secrets already none)."""

    def __init__(self, prefix: str = "yt-dlp") -> None:
        self.prefix = prefix
        self.debug = logger.debug

    def info(self, msg: str) -> None:
        logger.debug("%s: %s", self.prefix, msg)

    def warning(self, msg: str) -> None:
        logger.debug("%s warning: %s", self.prefix, msg)

    def error(self, msg: str) -> None:
        logger.info("%s error: %s", self.prefix, msg)

    def critical(self, msg: str) -> None:
        logger.warning("%s critical: %s", self.prefix, msg)


def _import_yt_dlp():
    try:
        import yt_dlp  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise YtDlpError(
            "yt-dlp is not installed; run: pip install -r requirements.txt",
            user_message="⚠️ This bot cannot handle that link: the yt-dlp package is missing.",
        ) from exc
    return yt_dlp


def ffmpeg_available(config: Settings) -> bool:
    """True when FFmpeg can be located (needed for merging/remuxing)."""
    if config.ffmpeg_location:
        return Path(config.ffmpeg_location).exists()
    return shutil.which("ffmpeg") is not None


class YtDlpDownloader:
    """Wrapper around yt-dlp with async, cancellable, progress-reporting calls."""

    def __init__(self, config: Settings) -> None:
        self.config = config

    # ------------------------------------------------------------------ options
    def build_options(
        self,
        dest_dir: str | Path,
        *,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        postprocessor_hook: Callable[[dict[str, Any]], None] | None = None,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        dest = str(Path(dest_dir))
        have_ffmpeg = ffmpeg_available(self.config)
        options: dict[str, Any] = {
            "format": DEFAULT_FORMAT,
            "paths": {"home": dest, "temp": dest},
            "outtmpl": {"default": "%(title).150B [%(id)s].%(ext)s"},
            "restrictfilenames": True,
            "windowsfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            "noplaylist": not self.config.allow_playlists,
            "ignoreerrors": False,
            "retries": self.config.max_retries,
            "fragment_retries": self.config.max_retries,
            "socket_timeout": self.config.download_timeout_seconds,
            "max_filesize": self.config.max_file_size_bytes,
            "concurrent_fragment_downloads": 3,
            "nocheckcertificate": False,
            "logger": _QuietLogger(),
            "writethumbnail": have_ffmpeg,
            "overwrites": False,
        }
        if self.config.allow_playlists:
            options["playlistend"] = 5
        if have_ffmpeg:
            options["merge_output_format"] = "mp4"
            options["postprocessors"] = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}]
        if self.config.write_subtitles:
            options["writesubtitles"] = True
            options["writeautomaticsub"] = True
            options["subtitleslangs"] = [
                lang.strip() for lang in self.config.subtitle_langs.split(",") if lang.strip()
            ]
            options["subtitlesformat"] = "srt/best"
        if self.config.ffmpeg_location:
            options["ffmpeg_location"] = self.config.ffmpeg_location
        if metadata_only:
            options.update(
                {"skip_download": True, "extract_flat": False, "writethumbnail": False, "postprocessors": []}
            )
        if progress_hook is not None:
            options["progress_hooks"] = [progress_hook]
        if postprocessor_hook is not None:
            options["postprocessor_hooks"] = [postprocessor_hook]
        return options

    # ------------------------------------------------------------------- probe
    async def probe(self, url: str) -> dict[str, Any] | None:
        """Return metadata for ``url`` when yt-dlp supports it, else ``None``."""
        loop = asyncio.get_running_loop()

        def work() -> dict[str, Any] | None:
            yt_dlp = _import_yt_dlp()
            options = self.build_options(Path(self.config.temp_dir), metadata_only=True)
            options.update({"skip_download": True, "logger": _QuietLogger()})
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
                logger.info("yt-dlp probe failed for %s: %s", url, exc)
                return None
            if not info:
                return None
            if info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return None
                first = dict(entries[0])
                first.setdefault("playlist_title", info.get("title"))
                first.setdefault("playlist_count", len(entries))
                return first
            return info

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, work), timeout=self.config.metadata_timeout_seconds
            )
        except TimeoutError:
            logger.info("yt-dlp probe timed out for %s", url)
            return None
        except YtDlpError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("yt-dlp probe error for %s: %s", url, exc)
            return None

    # ---------------------------------------------------------------- download
    async def download(
        self,
        url: str,
        dest_dir: str | Path,
        *,
        on_progress: Callable[[int, int | None, float], None] | None = None,
        cancel_event: asyncio.Event | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> DownloadResult:
        loop = asyncio.get_running_loop()
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        cancelled = False

        def emit(downloaded: int, total: int | None, speed: float) -> None:
            if on_progress is None:
                return
            loop.call_soon_threadsafe(on_progress, downloaded, total, speed)

        def progress_hook(data: dict[str, Any]) -> None:
            nonlocal cancelled
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                raise DownloadCancelled("cancelled by user")
            status = data.get("status")
            if status == "downloading":
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                total = int(total) if total else None
                if total and total > self.config.max_file_size_bytes:
                    raise FileTooLargeError(
                        f"yt-dlp reported {human_size(total)} > limit",
                        user_message=(
                            "❌ File is too large.\n\n"
                            f"Maximum allowed size: {human_size(self.config.max_file_size_bytes)}\n"
                            f"Detected size: {human_size(total)}"
                        ),
                    )
                if total:
                    ensure_disk_space(dest, total, self.config.min_free_disk_mb)
                emit(downloaded, total, float(data.get("speed") or 0.0))
            elif status == "finished":
                emit(int(data.get("total_bytes") or data.get("downloaded_bytes") or 0), None, 0.0)
                if on_stage is not None:
                    loop.call_soon_threadsafe(on_stage, "processing")
            elif status == "error":
                logger.warning("yt-dlp reported a download error for %s", url)

        def postprocessor_hook(data: dict[str, Any]) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled after download")
            if on_stage is not None and data.get("status") == "started":
                loop.call_soon_threadsafe(on_stage, "processing")

        started = time.monotonic()

        def work() -> dict[str, Any]:
            yt_dlp = _import_yt_dlp()
            options = self.build_options(
                dest, progress_hook=progress_hook, postprocessor_hook=postprocessor_hook
            )
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise yt_dlp.utils.DownloadError("no information retrieved")
                if info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        raise yt_dlp.utils.DownloadError("empty playlist")
                    info = entries[0]
                output = _pick_output_file(dest, ydl, info)
                return {"info": info, "path": str(output) if output else None}

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, work), timeout=self.config.max_download_time_seconds
            )
        except TimeoutError as exc:
            raise DownloadTimeoutError(
                f"yt-dlp exceeded {self.config.max_download_time_minutes} min for {url}",
                user_message=(
                    "⏱ The download took too long and was stopped "
                    f"(limit: {self.config.max_download_time_minutes} min)."
                ),
            ) from exc
        except DownloadCancelled:
            raise
        except FileTooLargeError:
            raise
        except YtDlpError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate yt-dlp failures
            if cancelled or (cancel_event is not None and cancel_event.is_set()):
                raise DownloadCancelled("cancelled by user") from exc
            raise _translate_error(exc, url) from exc

        info = result["info"]
        path_value = result["path"]
        if not path_value or not Path(path_value).exists():
            raise YtDlpError(
                f"yt-dlp produced no output file for {url}",
                user_message="❌ The download finished but no file was produced.",
            )
        path = Path(path_value)
        size = path.stat().st_size
        if size > self.config.max_file_size_bytes:
            try:
                path.unlink()
            finally:
                raise FileTooLargeError(
                    f"{path.name} is {human_size(size)}",
                    user_message=(
                        "❌ File is too large.\n\n"
                        f"Maximum allowed size: {human_size(self.config.max_file_size_bytes)}\n"
                        f"Detected size: {human_size(size)}"
                    ),
                )

        kind = _kind_from_path(path)
        thumbnail = _find_thumbnail(dest, path)
        logger.info(
            "yt-dlp download finished url=%s file=%s bytes=%s kind=%s duration=%.2fs",
            url,
            path.name,
            size,
            kind.value,
            time.monotonic() - started,
        )
        return DownloadResult(
            path=str(path),
            filename=path.name,
            size_bytes=size,
            kind=kind,
            method=DownloadMethod.YTDLP,
            source_url=info.get("webpage_url") or url,
            content_type="",
            title=info.get("title") or path.stem,
            duration=_as_float(info.get("duration")),
            thumbnail=str(thumbnail) if thumbnail else None,
            width=_as_int(info.get("width")),
            height=_as_int(info.get("height")),
            artist=info.get("artist") or info.get("uploader") or info.get("creator"),
            extra={
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "uploader": info.get("uploader"),
                "webpage_url": info.get("webpage_url") or url,
                "license": info.get("license"),
            },
        )


# ------------------------------------------------------------------- helpers
def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pick_output_file(dest: Path, ydl: Any, info: dict[str, Any]) -> Path | None:
    """Locate the media file yt-dlp just wrote."""
    for download in info.get("requested_downloads") or []:
        candidate = download.get("filepath") or download.get("_filename")
        if candidate:
            path = Path(candidate)
            if path.exists():
                return path
    for key in ("filepath", "_filename"):
        candidate = info.get(key)
        if candidate and Path(candidate).exists():
            return Path(candidate)
    try:
        prepared = Path(ydl.prepare_filename(info))
        if prepared.exists():
            return prepared
    except Exception:  # pragma: no cover - defensive
        logger.debug("prepare_filename failed", exc_info=True)

    candidates = [
        path
        for path in dest.iterdir()
        if path.is_file() and path.suffix.lower() in _MEDIA_SUFFIXES and not path.name.endswith(".part")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _find_thumbnail(dest: Path, media: Path) -> Path | None:
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = media.with_suffix(suffix)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    for path in sorted(dest.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return path
    return None


def _kind_from_path(path: Path) -> MediaKind:
    """Classify a downloaded file, reading a few bytes to confirm."""
    magic = b""
    try:
        with path.open("rb") as handle:
            magic = handle.read(64)
    except OSError:  # pragma: no cover - file disappeared
        pass
    kind, _ = classify(url=str(path), filename=path.name, magic=magic or None)
    if kind in {MediaKind.UNKNOWN, MediaKind.HTML}:
        guessed = kind_from_extension(path.suffix.lstrip("."))
        kind = guessed or MediaKind.DOCUMENT
    if is_executable_name(path.name):
        kind = MediaKind.DOCUMENT
    return kind


def _translate_error(exc: Exception, url: str) -> YtDlpError:
    text = str(exc)
    lowered = text.lower()
    import re as _re

    cleaned = _re.sub(r"\x1b\[[0-9;]*m", "", text).strip()
    first_line = cleaned.splitlines()[0] if cleaned else "yt-dlp error"

    if "unsupported url" in lowered or "no video formats" in lowered:
        return YtDlpError(
            f"unsupported URL {url}: {text}",
            user_message="❌ I could not find downloadable media at that link.",
        )
    if "private video" in lowered or "sign in" in lowered or "login" in lowered or "cookies" in lowered:
        return YtDlpError(
            f"login required for {url}: {text}",
            user_message=(
                "🚫 That content is private or requires a login. "
                "I only download publicly accessible media."
            ),
        )
    if "drm" in lowered or "protected" in lowered:
        return YtDlpError(
            f"DRM protected content at {url}",
            user_message="🚫 That content is DRM protected, which I do not handle.",
        )
    if "too large" in lowered or "max-filesize" in lowered or "larger than max" in lowered:
        return YtDlpError(
            f"file too large for {url}: {text}",
            user_message="❌ File is too large.",
        )
    if "ffmpeg" in lowered:
        return YtDlpError(
            f"ffmpeg failure for {url}: {text}",
            user_message="⚙️ Media processing failed on the server (FFmpeg). Please try another link.",
        )
    if "geo" in lowered and "restrict" in lowered:
        return YtDlpError(
            f"geo restricted {url}: {text}",
            user_message="🚫 That video is not available from the server's region.",
        )
    return YtDlpError(
        f"yt-dlp failed for {url}: {text}",
        user_message=f"❌ The site could not be downloaded.\n{first_line[:160]}",
    )
