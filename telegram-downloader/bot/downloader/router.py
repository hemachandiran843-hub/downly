"""Downloader router.

Decides *how* a URL should be fetched instead of blindly using one strategy::

    URL -> validate -> inspect (cheap HTTP probe)
          |-- real file stream (video/audio/image/document/archive) -> direct
          |-- HLS/DASH manifest or an HTML page                    -> yt-dlp
          |-- probe failed but yt-dlp knows the site                -> yt-dlp
          |-- probe failed and yt-dlp does not know it              -> direct (last resort)
    no route at all -> a useful error for the user
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bot.config import Settings
from bot.downloader.direct import DirectDownloader
from bot.downloader.types import DownloadMethod, MediaKind, ProbeResult
from bot.downloader.ytdlp import YtDlpDownloader
from bot.exceptions import BlockedUrlError, InvalidUrlError, UnsupportedUrlError
from bot.utils.logging import get_logger
from bot.utils.security import validate_url

logger = get_logger(__name__)

#: Media containers that are really just a manifest and need yt-dlp (or a player).
MANIFEST_CONTENT_TYPES = frozenset(
    {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "audio/x-mpegurl",
        "application/dash+xml",
        "application/x-mpegurl; charset=utf-8",
    }
)
MANIFEST_EXTENSIONS = frozenset({"m3u8", "mpd", "ism", "f4m"})

#: Kinds that are worth streaming straight from the server.
DIRECT_KINDS = frozenset(
    {MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.IMAGE, MediaKind.DOCUMENT, MediaKind.ARCHIVE, MediaKind.UNKNOWN}
)


@dataclass(slots=True)
class RouterDecision:
    """The chosen download strategy plus whatever metadata we already have."""

    url: str
    method: DownloadMethod
    reason: str
    probe: ProbeResult | None = None
    info: dict[str, Any] = field(default_factory=dict)
    probe_error: str | None = None

    @property
    def kind(self) -> MediaKind:
        if self.info:
            guessed = _kind_from_info(self.info)
            if guessed is not None:
                return guessed
        if self.probe is not None and self.probe.kind is not MediaKind.UNKNOWN:
            return self.probe.kind
        return MediaKind.UNKNOWN

    @property
    def title(self) -> str | None:
        value = self.info.get("title")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @property
    def expected_size(self) -> int | None:
        """Best known size in bytes.

        The extractor's own figure wins; the HTTP probe is only trusted when it
        described an actual file (an HTML page size says nothing about the media).
        """
        size = self.info.get("filesize") or self.info.get("filesize_approx")
        try:
            if size:
                return int(size)
        except (TypeError, ValueError):
            pass
        if self.probe is not None and self.probe.content_length and self.probe.kind is not MediaKind.HTML:
            return self.probe.content_length
        return None

    @property
    def duration(self) -> float | None:
        try:
            value = self.info.get("duration")
            return float(value) if value else None
        except (TypeError, ValueError):
            return None


def _kind_from_info(info: dict[str, Any]) -> MediaKind | None:
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    if vcodec and vcodec != "none":
        return MediaKind.VIDEO
    if acodec and acodec != "none":
        return MediaKind.AUDIO
    ext = (info.get("ext") or "").lower()
    if ext in {"mp4", "mkv", "webm", "mov", "avi", "flv"}:
        return MediaKind.VIDEO
    if ext in {"m4a", "mp3", "aac", "opus", "ogg", "flac", "wav"}:
        return MediaKind.AUDIO
    if ext in {"jpg", "jpeg", "png", "gif", "webp"}:
        return MediaKind.IMAGE
    return None


def is_manifest(probe: ProbeResult | None, url: str) -> bool:
    if probe is None:
        return _looks_like_manifest_url(url)
    content_type = (probe.content_type or "").split(";", 1)[0].strip().lower()
    if content_type in MANIFEST_CONTENT_TYPES:
        return True
    if probe.kind in {MediaKind.UNKNOWN, MediaKind.HTML} and _looks_like_manifest_url(probe.final_url):
        return True
    return _looks_like_manifest_url(url)


def _looks_like_manifest_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0]
    if "." not in path.rsplit("/", 1)[-1]:
        return False
    return path.rsplit(".", 1)[-1].lower() in MANIFEST_EXTENSIONS


class DownloaderRouter:
    """Chooses and exposes the right downloader for a URL."""

    def __init__(
        self,
        config: Settings,
        direct: DirectDownloader,
        ytdlp: YtDlpDownloader,
    ) -> None:
        self.config = config
        self.direct = direct
        self.ytdlp = ytdlp

    async def route(self, url: str, *, want_metadata: bool = True) -> RouterDecision:
        validated = validate_url(url)  # raises InvalidUrlError / BlockedUrlError

        probe: ProbeResult | None = None
        probe_error: Exception | None = None
        try:
            probe = await self.direct.probe(validated)
        except (InvalidUrlError, BlockedUrlError) as exc:
            raise exc
        except Exception as exc:  # noqa: BLE001 - probe failures are informational
            probe_error = exc
            logger.info("probe failed for %s: %s", validated, exc)

        if probe is not None and probe.kind in DIRECT_KINDS and not is_manifest(probe, validated):
            decision = RouterDecision(
                url=validated,
                method=DownloadMethod.DIRECT,
                reason=f"direct file stream ({probe.kind.value}, content-type={probe.content_type or 'unknown'})",
                probe=probe,
            )
            logger.info("router: %s -> direct (%s)", validated, decision.reason)
            return decision

        info: dict[str, Any] = {}
        if want_metadata:
            try:
                info = await self.ytdlp.probe(validated) or {}
            except Exception as exc:  # noqa: BLE001 - a broken extractor must not kill the job
                logger.info("yt-dlp probe error for %s: %s", validated, exc)
                info = {}

        if info:
            decision = RouterDecision(
                url=validated,
                method=DownloadMethod.YTDLP,
                reason=f"yt-dlp extractor ({info.get('extractor_key') or info.get('extractor') or 'generic'})",
                probe=probe,
                info=info,
                probe_error=str(probe_error) if probe_error else None,
            )
            logger.info("router: %s -> yt-dlp (%s)", validated, decision.reason)
            return decision

        if probe is not None and probe.kind is MediaKind.HTML:
            raise UnsupportedUrlError(
                f"{validated} served an HTML page and no yt-dlp extractor matched",
                user_message=(
                    "❌ That link is a web page, not a downloadable file.\n"
                    "If it links to media, send the direct file link instead."
                ),
            )

        if probe is not None:
            # Octet-stream / missing content type: try the plain HTTP downloader.
            decision = RouterDecision(
                url=validated,
                method=DownloadMethod.DIRECT,
                reason=f"unclassified response ({probe.content_type or 'no content-type'}) -> direct attempt",
                probe=probe,
            )
            logger.info("router: %s -> direct (%s)", validated, decision.reason)
            return decision

        # Nothing worked while inspecting: still give the plain GET one chance.
        if probe_error is None:
            raise UnsupportedUrlError(f"no route found for {validated}")
        decision = RouterDecision(
            url=validated,
            method=DownloadMethod.DIRECT,
            reason=f"inspection failed ({type(probe_error).__name__}) -> direct attempt",
            probe_error=str(probe_error),
        )
        logger.info("router: %s -> direct (%s)", validated, decision.reason)
        return decision
