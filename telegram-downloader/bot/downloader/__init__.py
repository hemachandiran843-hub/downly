"""Downloader layer: routing, direct HTTP downloads and yt-dlp."""

from bot.downloader.direct import DirectDownloader, SafeResolver, create_session
from bot.downloader.router import DownloaderRouter, RouterDecision
from bot.downloader.types import DownloadMethod, DownloadResult, MediaKind, ProbeResult
from bot.downloader.ytdlp import YtDlpDownloader, ffmpeg_available

__all__ = [
    "DirectDownloader",
    "SafeResolver",
    "create_session",
    "DownloaderRouter",
    "RouterDecision",
    "DownloadMethod",
    "DownloadResult",
    "MediaKind",
    "ProbeResult",
    "YtDlpDownloader",
    "ffmpeg_available",
]
