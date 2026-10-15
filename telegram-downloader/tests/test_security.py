"""URL extraction, validation and SSRF protection."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.exceptions import BlockedUrlError, InvalidUrlError
from bot.utils.security import (
    ensure_public_url,
    extract_url_candidates,
    extract_urls,
    is_hostname_blocked,
    is_ip_blocked,
    safe_destination,
    sanitize_filename,
    unique_path,
    validate_url,
)


class TestExtractUrls:
    def test_extracts_url_from_prose(self):
        text = "Download this:\nhttps://example.com/file.mp4\nthanks!"
        assert extract_urls(text) == ["https://example.com/file.mp4"]

    def test_multiple_urls_keep_order_and_dedupe(self):
        text = "a https://a.example/1.zip b http://b.example/x c https://a.example/1.zip"
        assert extract_urls(text) == ["https://a.example/1.zip", "http://b.example/x"]

    def test_strips_trailing_punctuation(self):
        assert extract_urls("see (https://example.com/a.pdf).") == ["https://example.com/a.pdf"]
        assert extract_urls("Really? https://example.com/b.zip!") == ["https://example.com/b.zip"]

    def test_balanced_parentheses_are_kept(self):
        assert extract_urls("https://example.com/a_(b).zip") == ["https://example.com/a_(b).zip"]

    def test_ignores_non_http_schemes(self):
        assert extract_urls("ftp://example.com/x magnet:?xt=urn:btih:abc") == []

    def test_respects_limit(self):
        text = " ".join(f"https://example.com/{i}" for i in range(10))
        assert len(extract_urls(text, limit=3)) == 3

    def test_empty_and_none(self):
        assert extract_urls(None) == []
        assert extract_urls("") == []
        assert extract_urls("no links here") == []

    def test_scheme_less_candidate_with_path(self):
        assert extract_url_candidates("get example.com/file.zip now") == ["https://example.com/file.zip"]

    def test_scheme_less_known_tld(self):
        assert extract_url_candidates("visit youtube.com") == ["https://youtube.com"]

    def test_scheme_less_unknown_tld_needs_path(self):
        # "video.mp4" must not be mistaken for a host.
        assert extract_url_candidates("my video.mp4 file") == []

    def test_full_urls_take_precedence(self):
        text = "https://real.example/file.zip and fake.example"
        assert extract_url_candidates(text) == ["https://real.example/file.zip"]


class TestValidateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/file.mp4",
            "http://example.com:8080/a?b=c",
            "https://sub.domain.example.org/path/to/file.pdf",
        ],
    )
    def test_accepts_public_urls(self, url):
        assert validate_url(url)

    def test_normalises_and_drops_fragment(self):
        result = validate_url("HTTPS://Example.COM/file.mp4#frag")
        assert result == "https://example.com/file.mp4"

    @pytest.mark.parametrize("url", ["", "   ", "not-a-url", "example.com/file", "ftp://example.com/x"])
    def test_rejects_invalid(self, url):
        with pytest.raises(InvalidUrlError):
            validate_url(url)

    def test_rejects_credentials(self):
        with pytest.raises(InvalidUrlError):
            validate_url("https://user:pass@example.com/file.mp4")

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/admin",
            "http://127.0.0.1/x",
            "http://0.0.0.0/",
            "http://10.1.2.3/file",
            "http://192.168.0.1/router",
            "http://172.16.5.5/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://[fd00::1]/",
            "http://[fe80::1]/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://something.local/file",
            "http://service.internal/file",
            "http://foo.localhost/file",
        ],
    )
    def test_blocks_internal_targets(self, url):
        with pytest.raises((BlockedUrlError, InvalidUrlError)):
            validate_url(url)

    def test_rejects_overlong_url(self):
        with pytest.raises(InvalidUrlError):
            validate_url("https://example.com/" + "a" * 3000)


class TestIpRules:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "172.20.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "garbage",
            # IPv4 addresses wrapped in IPv6 (NAT64 / 6to4 / mapped).
            "64:ff9b::7f00:1",
            "64:ff9b::a00:1",
            "64:ff9b:1::a00:1",
            "2002:7f00:0001::1",
        ],
    )
    def test_blocked(self, ip):
        assert is_ip_blocked(ip) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "2606:4700::1111",
            # NAT64 mapping of public IPv4 space must stay usable (IPv6-only networks).
            "64:ff9b::808:808",
            "64:ff9b::5db8:d822",
        ],
    )
    def test_allowed(self, ip):
        assert is_ip_blocked(ip) is False

    def test_hostname_denylist(self):
        assert is_hostname_blocked("localhost")
        assert is_hostname_blocked("METADATA.GOOGLE.INTERNAL")
        assert is_hostname_blocked("")
        assert is_hostname_blocked("api.example.local")
        assert not is_hostname_blocked("example.com")
        # IP literals are handled by the IP rules, not the hostname rules.
        assert not is_hostname_blocked("127.0.0.1")


class TestEnsurePublicUrl:
    async def test_blocks_hostname_resolving_to_private_ip(self, monkeypatch):
        from bot.utils import security

        monkeypatch.setattr(security, "resolve_host", lambda host, port=None: ["10.0.0.5"])
        with pytest.raises(BlockedUrlError):
            await ensure_public_url("https://evil.example/file.zip")

    async def test_allows_public_resolution(self, monkeypatch):
        from bot.utils import security

        monkeypatch.setattr(security, "resolve_host", lambda host, port=None: ["93.184.216.34", "2606:2800:220:1::1"])
        assert await ensure_public_url("https://example.com/file.zip") == "https://example.com/file.zip"

    async def test_dns_failure_is_not_fatal(self, monkeypatch):
        from bot.utils import security

        monkeypatch.setattr(security, "resolve_host", lambda host, port=None: [])
        assert await ensure_public_url("https://nx.example/file.zip")


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("video.mp4", "video.mp4"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\evil.exe", "evil.exe"),
            ("/absolute/path/file.zip", "file.zip"),
            ("with:colon.txt", "with-colon.txt"),
            ("null\x00byte.txt", "nullbyte.txt"),
            ("spaces   collapse.pdf", "spaces collapse.pdf"),
            ("dots...in...name.bin", "dots.in.name.bin"),
        ],
    )
    def test_sanitize(self, raw, expected):
        assert sanitize_filename(raw) == expected

    def test_empty_falls_back(self):
        assert sanitize_filename("") == "download"
        assert sanitize_filename(None) == "download"
        assert sanitize_filename("..") == "download"

    def test_reserved_windows_names(self):
        assert sanitize_filename("con.txt").startswith("_con")

    def test_length_is_capped(self):
        result = sanitize_filename("a" * 500 + ".mp4")
        assert len(result) <= 200
        assert result.endswith(".mp4")

    def test_safe_destination_stays_inside_base(self, tmp_path: Path):
        target = safe_destination(tmp_path, "../../../etc/passwd")
        assert target.parent == tmp_path.resolve()

    def test_unique_path_avoids_collisions(self, tmp_path: Path):
        first = unique_path(tmp_path, "file.bin")
        first.write_bytes(b"x")
        second = unique_path(tmp_path, "file.bin")
        assert second != first
        assert second.name.startswith("file_")
