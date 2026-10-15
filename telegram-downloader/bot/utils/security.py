"""URL extraction, validation and SSRF protection.

Everything a user sends is untrusted.  A URL is only considered safe after the
scheme is checked, the hostname is checked against a denylist, *and* every DNS
answer for that host resolves to a public unicast address.  Redirect targets go
through exactly the same checks (see ``bot/downloader/direct.py``).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

from bot.config import BLOCKED_HOSTNAMES
from bot.exceptions import BlockedUrlError, InvalidUrlError

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Trailing characters that are almost always punctuation in prose, not part of
# the URL, e.g. "see https://example.com/file.zip." or "(https://x.com/a)".
_TRAILING_JUNK = ".,;:!?'\"<>"
_MATCH_CLOSERS = {")": "(", "]": "[", "}": "{"}

_URL_RE = re.compile(r"https?://[^\s<>\"'`\[\]{}|\\^]+", re.IGNORECASE)

_MAX_URL_LENGTH = 2048
_MAX_FILENAME_LENGTH = 180
_RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------- extract
def _trim_url(candidate: str) -> str:
    url = candidate.strip()
    while url and url[-1] in _TRAILING_JUNK:
        url = url[:-1]
    # Drop unbalanced closing brackets/parens that came from prose.
    while url and url[-1] in _MATCH_CLOSERS:
        closer = url[-1]
        opener = _MATCH_CLOSERS[closer]
        if url.count(closer) > url.count(opener):
            url = url[:-1]
        else:
            break
    while url and url[-1] in _TRAILING_JUNK:
        url = url[:-1]
    return url


def extract_urls(text: str | None, limit: int | None = None) -> list[str]:
    """Extract http(s) URLs from arbitrary message text, in order, deduplicated.

    ``download this: https://example.com/file.mp4 please`` -> one URL.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        url = _trim_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        found.append(url)
        if limit is not None and len(found) >= limit:
            break
    return found


# -------------------------------------------------------------------- validate
# Bare hosts such as "example.com/file.mp4" (no scheme).  Only accepted when the
# match has a path or a plausible public TLD, to avoid treating "video.mp4" as a host.
_BARE_URL_RE = re.compile(
    r"(?<![\w./@-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d{1,5})?(?:/[^\s<>\"'`\[\]{}|\\^]*)?)",
    re.IGNORECASE,
)
_COMMON_TLDS = frozenset(
    {
        "com", "org", "net", "edu", "gov", "int", "io", "co", "dev", "app", "ai", "so", "tv", "cc",
        "me", "info", "biz", "site", "online", "store", "cloud", "xyz", "link", "to", "fm", "gg", "sh",
        "uk", "de", "fr", "nl", "it", "es", "pt", "ru", "ua", "pl", "se", "no", "fi", "dk", "cz", "gr",
        "tr", "in", "jp", "kr", "cn", "tw", "hk", "sg", "ph", "id", "th", "vn", "my", "br", "mx", "ar",
        "cl", "ca", "au", "nz", "za", "il", "ie", "ch", "at", "be", "ro", "hu", "bg", "hr", "sk", "si",
        "lt", "lv", "ee", "is",
    }
)


def extract_url_candidates(text: str | None, limit: int | None = None) -> list[str]:
    """Like :func:`extract_urls`, but also accepts scheme-less links.

    ``"download example.com/file.mp4"`` yields ``https://example.com/file.mp4``.
    """
    urls = extract_urls(text, limit)
    if urls or not text:
        return urls
    found: list[str] = []
    seen: set[str] = set()
    for match in _BARE_URL_RE.finditer(text):
        candidate = _trim_url(match.group(1))
        if not candidate:
            continue
        host_part, _, path_part = candidate.partition("/")
        tld = host_part.rsplit(".", 1)[-1].split(":", 1)[0].lower()
        if not path_part and tld not in _COMMON_TLDS:
            continue
        url = f"https://{candidate}"
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
        if limit is not None and len(found) >= limit:
            break
    return found


def is_hostname_blocked(hostname: str) -> bool:
    host = (hostname or "").strip().strip(".").lower()
    if not host:
        return True
    if host in BLOCKED_HOSTNAMES:
        return True
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    # A bare or bracketed IP literal is judged by the IP rules below.
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return False


#: Prefixes that carry an IPv4 address inside an IPv6 address (RFC 6052 NAT64,
#: 6to4 and IPv4-mapped forms). The embedded IPv4 decides whether it is safe.
_NAT64_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address an IPv6 address wraps, if any."""
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    for prefix in _NAT64_PREFIXES:
        if addr in prefix:
            return ipaddress.IPv4Address(addr.packed[-4:])
    return None


def is_ip_blocked(ip: str | ipaddress._BaseAddress) -> bool:
    """True when an address must never be contacted (private/internal/etc.)."""
    try:
        addr = ip if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(
            str(ip).strip("[]")
        )
    except ValueError:
        # Not an address at all -> treat as unsafe.
        return True

    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            # IPv4-mapped / 6to4 / NAT64: the wrapped address is what is reached.
            return is_ip_blocked(embedded)
        if addr.teredo is not None:
            return True

    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_url(url: str | None) -> str:
    """Validate syntax and hostname rules; returns the normalized URL.

    Raises :class:`InvalidUrlError` or :class:`BlockedUrlError`.
    """
    if not url or not url.strip():
        raise InvalidUrlError("empty url", user_message="❌ No link found in your message.")
    candidate = url.strip()
    if len(candidate) > _MAX_URL_LENGTH:
        raise InvalidUrlError("url too long", user_message="❌ That link is too long.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidUrlError(f"unparsable url: {exc!r}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidUrlError(
            f"unsupported scheme {scheme!r}", user_message="❌ Only http and https links are supported."
        )
    if not parts.netloc or not parts.hostname:
        raise InvalidUrlError("missing host", user_message="❌ That link is missing a hostname.")
    if parts.username or parts.password:
        raise InvalidUrlError(
            "credentials in url",
            user_message="🚫 Links containing credentials are not supported.",
        )

    host = parts.hostname
    if is_hostname_blocked(host):
        raise BlockedUrlError(f"blocked hostname {host!r}")

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError("invalid port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise InvalidUrlError("port out of range")

    # Reject literal addresses that point inside the network before any DNS work.
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and is_ip_blocked(literal):
        raise BlockedUrlError(f"blocked address {host!r}")

    normalized_host = host.lower()
    bracketed = ":" in normalized_host and not normalized_host.startswith("[")
    netloc = f"[{normalized_host}]" if bracketed else normalized_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))  # fragment dropped


def resolve_host(hostname: str, port: int | None = None) -> list[str]:
    """Resolve a hostname to a list of IP strings (may be empty on failure)."""
    try:
        infos = socket.getaddrinfo(hostname, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError):
        return []
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        address = str(sockaddr[0]) if sockaddr else ""
        if address and address not in ips:
            ips.append(address)
    return ips


async def resolve_host_async(hostname: str, port: int | None = None) -> list[str]:
    """Async wrapper around :func:`resolve_host` (run in a worker thread)."""
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(None, resolve_host, hostname, port)


async def ensure_public_url(url: str) -> str:
    """Full validation including DNS, returning the normalized URL.

    DNS failures are *not* fatal here: the downloader surfaces the connection
    error with a clearer message.  What matters is that no answer is private.
    """
    normalized = validate_url(url)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    # Literal addresses were already checked in validate_url.
    try:
        ipaddress.ip_address(host.strip("[]"))
        return normalized
    except ValueError:
        pass

    ips = await resolve_host_async(host, parts.port)
    for ip in ips:
        if is_ip_blocked(ip):
            raise BlockedUrlError(f"{host} resolves to blocked address {ip}")
    return normalized


def is_private_redirect(original_url: str, redirect_url: str) -> bool:
    """Cheap synchronous pre-check used while following redirects."""
    try:
        validate_url(redirect_url)
    except (InvalidUrlError, BlockedUrlError):
        return True
    return False


# ------------------------------------------------------------------ filenames
def sanitize_filename(name: str | None, *, fallback: str = "download", max_length: int = _MAX_FILENAME_LENGTH) -> str:
    """Make an arbitrary (possibly hostile) string safe to use as a filename."""
    raw = (name or "").strip()
    if not raw:
        return fallback
    # Strip any directory component (both separators) and NULs.
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = raw.replace("\x00", "")
    raw = "".join(ch for ch in raw if ch.isprintable() and ch not in '<>"|?*')
    raw = re.sub(r"[\s\x00-\x1f]+", " ", raw).strip(" .")
    raw = raw.replace(":", "-")
    # Collapse traversal sequences and repeated dots.
    raw = re.sub(r"\.{2,}", ".", raw).strip(" .")
    if not raw or raw in {".", ".."}:
        return fallback

    stem, dot, suffix = raw.rpartition(".")
    if not dot:
        stem, suffix = raw, ""
    suffix = re.sub(r"[^A-Za-z0-9]", "", suffix)[:12]
    stem = stem or fallback
    if stem.lower() in _RESERVED_WINDOWS_NAMES:
        stem = f"_{stem}"
    if max_length and len(stem) > max_length:
        stem = stem[:max_length].rstrip(" .")
    cleaned = f"{stem}.{suffix}" if suffix else stem
    return cleaned or fallback


def safe_destination(base_dir, filename: str):
    """Join ``filename`` under ``base_dir``, guaranteeing no path traversal."""
    from pathlib import Path

    base = Path(base_dir).resolve()
    candidate = (base / sanitize_filename(filename)).resolve()
    if base != candidate and base not in candidate.parents:
        raise InvalidUrlError("path traversal attempt", user_message="❌ Invalid filename.")
    return candidate


def unique_path(directory, filename: str):
    """Return a non-existing path inside ``directory`` for ``filename``."""
    from pathlib import Path

    directory = Path(directory)
    target = safe_destination(directory, filename)
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(1, 1000):
        candidate = target.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}_{__import__('uuid').uuid4().hex[:8]}{suffix}")
