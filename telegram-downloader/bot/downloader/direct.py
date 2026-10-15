"""Direct HTTP/HTTPS downloader.

* streams to disk (never loads a whole file into memory)
* follows redirects manually and re-validates every hop against the SSRF rules
* enforces size, disk-space, timeout and retry limits
* is cancellable and always cleans up partial files
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

import aiohttp
from aiohttp import ClientResponse, ClientSession
from aiohttp.abc import ResolveResult

from bot.config import Settings
from bot.downloader.types import DownloadMethod, DownloadResult, MediaKind, ProbeResult
from bot.downloader.utils import (
    classify,
    ensure_disk_space,
    ensure_extension,
    extension_from_url,
    filename_from_content_disposition,
    filename_from_url,
)
from bot.exceptions import (
    BlockedUrlError,
    DownloadCancelled,
    DownloadTimeoutError,
    FileTooLargeError,
    HttpDownloadError,
    InvalidUrlError,
)
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger
from bot.utils.security import ensure_public_url, is_ip_blocked, sanitize_filename, unique_path, validate_url

logger = get_logger(__name__)

CHUNK_SIZE = 256 * 1024
SNIFF_BYTES = 2048
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 507, 509}

#: Emitted after every chunk (the progress service throttles Telegram updates).
ProgressCallback = Callable[[int, int | None, float], None]


class HTTPSessionLike(Protocol):
    """The subset of :class:`aiohttp.ClientSession` this module relies on.

    Describing the exact call shape lets tests inject a fake session while the
    real ``ClientSession`` still satisfies the protocol.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        allow_redirects: bool = ...,
        headers: dict[str, str] | None = ...,
        timeout: aiohttp.ClientTimeout | None = ...,
    ) -> Any: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------- DNS guard
class SafeResolver(aiohttp.abc.AbstractResolver):
    """Resolver that drops private/internal addresses at connect time.

    Validating a URL is not enough on its own: DNS can answer differently for
    the same name (rebinding). Filtering here guarantees the socket we open is
    always a public address.
    """

    def __init__(self, inner: aiohttp.abc.AbstractResolver | None = None) -> None:
        self._inner = inner or aiohttp.resolver.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        # A literal address is judged directly; hostnames are judged by their answers.
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None and is_ip_blocked(literal):
            raise BlockedUrlError(f"blocked address {host!r}")

        results = await self._inner.resolve(host, port, family)
        safe: list[ResolveResult] = []
        for entry in results:
            # aiohttp stores the resolved address in "host" ("hostname" is the name).
            address = str(entry.get("host") or "")
            try:
                literal = ipaddress.ip_address(address)
            except ValueError:
                logger.warning("resolver returned %r for %s, which is not an address", address, host)
                continue
            if is_ip_blocked(literal):
                logger.warning("DNS answer for %s points to blocked address %s", host, address)
                continue
            safe.append(entry)
        if not safe:
            raise OSError(f"no public address found for {host}")
        return safe

    async def close(self) -> None:
        await self._inner.close()


def create_session(config: Settings, *, resolver: aiohttp.abc.AbstractResolver | None = None) -> ClientSession:
    """Build the shared aiohttp session used for probing and downloading."""
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=min(30, config.download_timeout_seconds),
        sock_read=config.download_timeout_seconds,
    )
    connector = aiohttp.TCPConnector(
        resolver=resolver or SafeResolver(),
        limit=max(8, config.max_concurrent_downloads * 4),
        ttl_dns_cache=60,
        enable_cleanup_closed=True,
    )
    return ClientSession(
        connector=connector,
        timeout=timeout,
        headers={"User-Agent": config.user_agent, "Accept-Encoding": "identity"},
        auto_decompress=False,
    )


class DirectDownloader:
    """Streaming HTTP downloader with probing support."""

    def __init__(self, config: Settings, session: HTTPSessionLike | None = None) -> None:
        self.config = config
        self._session: HTTPSessionLike | None = session
        self._owns_session = session is None

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._session is None:
            self._session = create_session(self.config)

    async def close(self) -> None:
        session = self._session
        self._session = None
        if self._owns_session and session is not None:
            try:
                await session.close()
            except Exception:  # pragma: no cover - best effort
                logger.debug("failed to close http session", exc_info=True)

    @property
    def session(self) -> HTTPSessionLike:
        if self._session is None:
            raise RuntimeError("DirectDownloader.start() has not been awaited")
        return self._session

    # --------------------------------------------------------------- requests
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[ClientResponse, str]:
        """Perform a request, following redirects safely. Never auto-redirects."""
        current = url
        method = method.upper()
        hops = 0
        while True:
            safe_url = await ensure_public_url(current)
            response = await self.session.request(
                method,
                safe_url,
                allow_redirects=False,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=min(30, timeout or self.config.download_timeout_seconds),
                    sock_read=timeout or self.config.download_timeout_seconds,
                )
                if timeout
                else None,
            )
            if response.status in _REDIRECT_CODES:
                location = response.headers.get("Location")
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                if not location:
                    raise HttpDownloadError(
                        f"redirect without Location header from {safe_url}",
                        user_message="❌ The link redirects to nowhere.",
                    )
                if hops >= self.config.max_redirects:
                    raise HttpDownloadError(
                        f"too many redirects ({hops}) for {url}",
                        user_message="❌ Too many redirects while following that link.",
                    )
                hops += 1
                if response.status == 303 and method == "HEAD":
                    method = "GET"
                current = urljoin(safe_url, location)
                continue
            return response, safe_url

    # ------------------------------------------------------------------ probe
    async def probe(self, url: str, *, method: str = "HEAD") -> ProbeResult:
        """Inspect a URL without downloading the body."""
        validated = validate_url(url)
        response, final_url = await self._request(
            method, validated, timeout=self.config.probe_timeout_seconds
        )
        try:
            if response.status >= 400 and method == "HEAD":
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                return await self.probe(validated, method="GET")

            headers = {k.lower(): v for k, v in response.headers.items()}
            content_type = headers.get("content-type", "")
            length = _content_length(headers)
            filename = filename_from_content_disposition(headers.get("content-disposition"))
            magic = b""

            if method == "HEAD" and (response.status >= 400 or not content_type):
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                return await self.probe(validated, method="GET")

            if method == "GET":
                magic = await _read_prefix(response, SNIFF_BYTES)
                close = getattr(response, "close", None)
                if callable(close):
                    close()

            kind, magic_ext = classify(
                content_type=content_type, url=final_url, filename=filename, magic=magic or None
            )
            if response.status >= 400:
                raise _http_error(response.status, final_url)
            return ProbeResult(
                url=validated,
                final_url=final_url,
                status=response.status,
                content_type=content_type,
                content_length=length,
                filename=filename,
                accepts_ranges=headers.get("accept-ranges", "").lower() == "bytes",
                kind=kind,
                magic_extension=magic_ext,
                headers=headers,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    # --------------------------------------------------------------- download
    async def download(
        self,
        url: str,
        dest_dir: str | Path,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        probe: ProbeResult | None = None,
        title: str | None = None,
    ) -> DownloadResult:
        validated = validate_url(url)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        attempts = max(1, self.config.max_retries + 1)
        last_error: Exception | None = None
        deadline = time.monotonic() + self.config.max_download_time_seconds

        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(
                    self._attempt(validated, dest, on_progress, cancel_event, probe, title),
                    timeout=max(1.0, deadline - time.monotonic()),
                )
            except DownloadCancelled:
                raise
            except (FileTooLargeError, InvalidUrlError, BlockedUrlError) as exc:
                raise exc
            except TimeoutError as exc:
                raise DownloadTimeoutError(
                    f"exceeded {self.config.max_download_time_minutes} min budget for {validated}",
                    user_message=(
                        "⏱ The download took too long and was stopped "
                        f"(limit: {self.config.max_download_time_minutes} min)."
                    ),
                ) from exc
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if not _is_retryable(exc) or attempt >= attempts:
                    raise
                delay = min(15.0, 1.5**attempt)
                logger.warning(
                    "download attempt %s/%s failed for %s (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    validated,
                    exc,
                    delay,
                )
                if on_progress is not None:
                    try:
                        on_progress(-1, None, 0.0)
                    except Exception:  # pragma: no cover - progress must never break downloads
                        logger.debug("progress callback failed", exc_info=True)
                await asyncio.sleep(delay)

        raise HttpDownloadError(str(last_error))  # pragma: no cover - loop always returns/raises

    async def _attempt(
        self,
        url: str,
        dest: Path,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        probe: ProbeResult | None,
        title: str | None,
    ) -> DownloadResult:
        response, final_url = await self._request("GET", url)
        try:
            if response.status >= 400:
                raise _http_error(response.status, final_url)

            headers = {k.lower(): v for k, v in response.headers.items()}
            content_type = headers.get("content-type", "")
            total = _content_length(headers)
            if total is not None and total > self.config.max_file_size_bytes:
                raise FileTooLargeError(
                    f"{final_url} is {human_size(total)} > limit {human_size(self.config.max_file_size_bytes)}",
                    user_message=(
                        "❌ File is too large.\n\n"
                        f"Maximum allowed size: {human_size(self.config.max_file_size_bytes)}\n"
                        f"Detected size: {human_size(total)}"
                    ),
                )
            ensure_disk_space(
                dest,
                total or min(self.config.max_file_size_bytes, 64 * 1024 * 1024),
                self.config.min_free_disk_mb,
            )

            disposition_name = filename_from_content_disposition(headers.get("content-disposition"))
            url_name = filename_from_url(final_url) or filename_from_url(url)
            provisional = sanitize_filename(
                disposition_name or (probe.filename if probe else None) or url_name or "download"
            )
            part_path = unique_path(dest, f"{provisional}.part")

            downloaded = 0
            magic = b""
            started = time.monotonic()

            with part_path.open("wb") as handle:
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("cancelled by user")
                    if not chunk:
                        continue
                    if len(magic) < SNIFF_BYTES:
                        magic += chunk[: SNIFF_BYTES - len(magic)]
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > self.config.max_file_size_bytes:
                        raise FileTooLargeError(
                            f"{final_url} exceeded {human_size(self.config.max_file_size_bytes)} while streaming",
                            user_message=(
                                "❌ File is too large.\n\n"
                                f"Maximum allowed size: {human_size(self.config.max_file_size_bytes)}"
                            ),
                        )
                    if on_progress is not None:
                        elapsed = max(1e-6, time.monotonic() - started)
                        try:
                            on_progress(downloaded, total, downloaded / elapsed)
                        except Exception:  # pragma: no cover - progress must never break downloads
                            logger.debug("progress callback failed", exc_info=True)
                handle.flush()

            if downloaded == 0:
                raise HttpDownloadError(
                    f"empty response body from {final_url}",
                    user_message="❌ The server returned an empty file.",
                )

            kind, magic_ext = classify(
                content_type=content_type,
                url=final_url,
                filename=disposition_name or url_name,
                magic=magic,
            )
            if kind is MediaKind.UNKNOWN:
                kind = MediaKind.DOCUMENT
            final_name = ensure_extension(
                disposition_name or url_name or provisional,
                magic_ext or extension_from_url(final_url) or extension_from_url(url),
            )
            final_path = unique_path(dest, sanitize_filename(final_name))
            part_path.replace(final_path)

            logger.info(
                "direct download finished url=%s bytes=%s kind=%s duration=%.2fs",
                final_url,
                downloaded,
                kind.value,
                time.monotonic() - started,
            )
            return DownloadResult(
                path=str(final_path),
                filename=final_path.name,
                size_bytes=downloaded,
                kind=kind,
                method=DownloadMethod.DIRECT,
                source_url=final_url,
                content_type=content_type,
                title=title or final_path.name,
                extra={"declared_size": total},
            )
        except Exception:
            # Remove the partial file for this attempt before propagating.
            _cleanup_partials(dest)
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


# ------------------------------------------------------------------- helpers
def _content_length(headers: dict[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw:
        try:
            value = int(raw)
            return value if value >= 0 else None
        except ValueError:
            pass
    content_range = headers.get("content-range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    return None


async def _read_prefix(response: ClientResponse, limit: int) -> bytes:
    data = b""
    try:
        async for chunk in response.content.iter_chunked(limit):
            data += chunk
            if len(data) >= limit:
                break
    except (TimeoutError, aiohttp.ClientError):  # pragma: no cover - short read is fine
        pass
    return data[:limit]


def _http_error(status: int, url: str) -> HttpDownloadError:
    mapping = {
        400: "❌ The server rejected the request (HTTP 400).",
        401: "🚫 That link requires a login the bot does not have (HTTP 401).",
        403: "🚫 The server refused access to that link (HTTP 403).",
        404: "❌ That link no longer exists (HTTP 404).",
        410: "❌ That link has been removed (HTTP 410).",
        429: "⏳ The server is rate limiting requests (HTTP 429). Try again in a minute.",
        451: "🚫 The content is unavailable for legal reasons (HTTP 451).",
    }
    message = mapping.get(status)
    if message is None:
        if 500 <= status < 600:
            message = f"❌ The server had a problem (HTTP {status}). Try again later."
        else:
            message = f"❌ The server returned HTTP {status}."
    return HttpDownloadError(f"HTTP {status} for {url}", user_message=message)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError)):
        return True
    if isinstance(exc, (asyncio.TimeoutError, socket.gaierror, ConnectionResetError, IncompleteReadError)):
        return True
    if isinstance(exc, HttpDownloadError):
        text = str(exc)
        if "HTTP " in text:
            try:
                status = int(text.split("HTTP ", 1)[1].split(maxsplit=1)[0])
            except (IndexError, ValueError):
                return False
            return status in _RETRYABLE_STATUS
    return False


class IncompleteReadError(OSError):
    """Raised by a fake/streaming server that closes the connection early."""


def _cleanup_partials(directory: Path) -> None:
    for leftover in _iter_partials(directory):
        try:
            leftover.unlink()
        except OSError:  # pragma: no cover - best effort
            logger.debug("could not delete partial file %s", leftover)


def _iter_partials(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return list(directory.glob("*.part"))
