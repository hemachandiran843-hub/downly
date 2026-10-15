"""Direct HTTP downloader: streaming, redirects, SSRF, limits, retries, cancel."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.downloader.direct import DirectDownloader, SafeResolver, _content_length, _http_error, _is_retryable
from bot.downloader.types import DownloadMethod, MediaKind
from bot.exceptions import (
    BlockedUrlError,
    DownloadCancelled,
    FileTooLargeError,
    HttpDownloadError,
    InsufficientDiskSpaceError,
)
from tests.fakes import PNG_BYTES, ZIP_BYTES, FakeResponse, FakeSession, make_settings


class _InnerResolver:
    """Stands in for aiohttp's DefaultResolver."""

    def __init__(self, answer):
        self.answer = answer

    async def resolve(self, host, port=0, family=0):
        return self.answer

    async def close(self):
        return None


def build(tmp_path, session, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return settings, DirectDownloader(settings, session=session)


class TestProbe:
    async def test_head_request_reports_metadata(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/video.mp4",
            FakeResponse(
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": "2048",
                    "Content-Disposition": 'attachment; filename="movie.mp4"',
                    "Accept-Ranges": "bytes",
                }
            ),
        )
        _, downloader = build(tmp_path, session)
        probe = await downloader.probe("https://example.com/video.mp4")
        assert probe.status == 200
        assert probe.content_type == "video/mp4"
        assert probe.content_length == 2048
        assert probe.filename == "movie.mp4"
        assert probe.kind is MediaKind.VIDEO
        assert probe.accepts_ranges is True
        assert session.calls == [("HEAD", "https://example.com/video.mp4")]

    async def test_falls_back_to_ranged_get_and_reads_magic(self, tmp_path):
        session = FakeSession()
        session.add("https://example.com/file", FakeResponse(status=200, body=PNG_BYTES))
        session.add(
            ("HEAD", "https://example.com/file"),
            FakeResponse(status=405, headers={"Content-Type": ""}),
        )
        _, downloader = build(tmp_path, session)
        probe = await downloader.probe("https://example.com/file")
        assert probe.kind is MediaKind.IMAGE
        assert probe.magic_extension == "png"
        assert [call[0] for call in session.calls] == ["HEAD", "GET"]

    async def test_html_page_is_classified_as_html(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/watch",
            FakeResponse(status=200, headers={"Content-Type": "text/html; charset=utf-8"}),
        )
        _, downloader = build(tmp_path, session)
        probe = await downloader.probe("https://example.com/watch")
        assert probe.kind is MediaKind.HTML
        assert probe.is_html is True

    async def test_http_error_raises_friendly_message(self, tmp_path):
        session = FakeSession()
        session.add("https://example.com/missing", FakeResponse(status=404))
        _, downloader = build(tmp_path, session)
        with pytest.raises(HttpDownloadError) as excinfo:
            await downloader.probe("https://example.com/missing")
        assert "no longer exists" in excinfo.value.user_message

    async def test_rejects_private_target_before_requesting(self, tmp_path):
        session = FakeSession()
        _, downloader = build(tmp_path, session)
        with pytest.raises(BlockedUrlError):
            await downloader.probe("http://169.254.169.254/latest/meta-data/")
        assert session.calls == []


class TestRedirects:
    async def test_follows_redirect_and_uses_final_url(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/start",
            FakeResponse(status=302, headers={"Location": "/real/file.zip"}),
        )
        session.add("https://example.com/real/file.zip", FakeResponse(headers={"Content-Type": "application/zip"}))
        _, downloader = build(tmp_path, session)
        probe = await downloader.probe("https://example.com/start")
        assert probe.final_url == "https://example.com/real/file.zip"
        assert probe.kind is MediaKind.ARCHIVE
        assert probe.is_redirected is True

    async def test_absolute_redirect_is_followed(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/a",
            FakeResponse(status=301, headers={"Location": "https://cdn.example.net/file.mp4"}),
        )
        session.add("https://cdn.example.net/file.mp4", FakeResponse(headers={"Content-Type": "video/mp4"}))
        _, downloader = build(tmp_path, session)
        probe = await downloader.probe("https://example.com/a")
        assert probe.final_url == "https://cdn.example.net/file.mp4"

    async def test_redirect_to_private_address_is_blocked(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/evil",
            FakeResponse(status=302, headers={"Location": "http://127.0.0.1:8000/admin"}),
        )
        _, downloader = build(tmp_path, session)
        with pytest.raises(BlockedUrlError):
            await downloader.probe("https://example.com/evil")
        assert session.calls == [("HEAD", "https://example.com/evil")]

    async def test_too_many_redirects(self, tmp_path):
        session = FakeSession()
        for index in range(10):
            session.add(
                f"https://example.com/{index}",
                FakeResponse(status=302, headers={"Location": f"/{index + 1}"}),
            )
        _, downloader = build(tmp_path, session, MAX_REDIRECTS=3)
        with pytest.raises(HttpDownloadError) as excinfo:
            await downloader.probe("https://example.com/0")
        assert "redirects" in excinfo.value.user_message.lower()

    async def test_redirect_without_location(self, tmp_path):
        session = FakeSession()
        session.add("https://example.com/x", FakeResponse(status=302))
        _, downloader = build(tmp_path, session)
        with pytest.raises(HttpDownloadError):
            await downloader.probe("https://example.com/x")


class TestDownload:
    async def test_streams_to_disk_and_reports_progress(self, tmp_path):
        body = b"z" * (300 * 1024)
        session = FakeSession()
        session.add(
            "https://example.com/big.bin",
            FakeResponse(body=body, chunk_size=64 * 1024, headers={"Content-Type": "application/octet-stream"}),
        )
        _, downloader = build(tmp_path, session)
        progress: list[tuple[int, int | None, float]] = []
        result = await downloader.download(
            "https://example.com/big.bin",
            tmp_path / "job",
            on_progress=lambda d, t, s: progress.append((d, t, s)),
        )
        assert result.method is DownloadMethod.DIRECT
        assert result.size_bytes == len(body)
        assert Path(result.path).read_bytes() == body
        assert len(progress) == 5  # 300KB in 64KB chunks
        assert progress[-1][0] == len(body)
        assert all(step[2] > 0 for step in progress)
        assert not list((tmp_path / "job").glob("*.part"))

    async def test_uses_content_disposition_filename(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/dl",
            FakeResponse(
                body=ZIP_BYTES,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Disposition": 'attachment; filename="archive.zip"',
                },
            ),
        )
        _, downloader = build(tmp_path, session)
        result = await downloader.download("https://example.com/dl", tmp_path / "job")
        assert result.filename == "archive.zip"
        assert result.kind is MediaKind.ARCHIVE

    async def test_extension_is_fixed_from_magic_bytes(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/image",
            FakeResponse(body=PNG_BYTES, headers={"Content-Type": "application/octet-stream"}),
        )
        _, downloader = build(tmp_path, session)
        result = await downloader.download("https://example.com/image", tmp_path / "job")
        assert result.filename.endswith(".png")
        assert result.kind is MediaKind.IMAGE

    async def test_rejects_oversized_content_length_before_downloading(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/huge.iso",
            FakeResponse(
                body=b"", headers={"Content-Type": "application/octet-stream", "Content-Length": str(5 * 1024 * 1024)}
            ),
        )
        _, downloader = build(tmp_path, session, MAX_FILE_SIZE_MB=1)
        with pytest.raises(FileTooLargeError) as excinfo:
            await downloader.download("https://example.com/huge.iso", tmp_path / "job")
        assert "Maximum allowed size" in excinfo.value.user_message
        assert "Detected size" in excinfo.value.user_message
        assert not list((tmp_path / "job").glob("*"))

    async def test_enforces_limit_while_streaming_when_header_lies(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/liar",
            FakeResponse(body=b"a" * (2 * 1024 * 1024), chunk_size=256 * 1024, headers={"Content-Type": "text/plain"}),
        )
        _, downloader = build(tmp_path, session, MAX_FILE_SIZE_MB=1, MAX_RETRIES=0)
        with pytest.raises(FileTooLargeError):
            await downloader.download("https://example.com/liar", tmp_path / "job")
        assert not list((tmp_path / "job").glob("*.part"))

    async def test_cancellation_mid_stream_cleans_up(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/slow.bin",
            FakeResponse(
                body=b"b" * (512 * 1024),
                chunk_size=64 * 1024,
                delay=0.05,
                headers={"Content-Type": "application/octet-stream"},
            ),
        )
        _, downloader = build(tmp_path, session)
        cancel = asyncio.Event()

        async def cancel_soon():
            await asyncio.sleep(0.12)
            cancel.set()

        task = asyncio.create_task(cancel_soon())
        with pytest.raises(DownloadCancelled):
            await downloader.download("https://example.com/slow.bin", tmp_path / "job", cancel_event=cancel)
        await task
        assert not list((tmp_path / "job").glob("*"))

    async def test_http_error_is_reported_and_no_file_is_left(self, tmp_path):
        session = FakeSession()
        session.add("https://example.com/forbidden.zip", FakeResponse(status=403))
        _, downloader = build(tmp_path, session)
        with pytest.raises(HttpDownloadError) as excinfo:
            await downloader.download("https://example.com/forbidden.zip", tmp_path / "job")
        assert "refused access" in excinfo.value.user_message
        assert not list((tmp_path / "job").glob("*"))

    async def test_empty_body_is_rejected(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/empty",
            FakeResponse(body=b"", headers={"Content-Type": "application/octet-stream"}),
        )
        _, downloader = build(tmp_path, session, MAX_RETRIES=0)
        with pytest.raises(HttpDownloadError):
            await downloader.download("https://example.com/empty", tmp_path / "job")

    async def test_disk_space_is_checked(self, tmp_path, monkeypatch):
        session = FakeSession()
        session.add(
            "https://example.com/big.bin",
            FakeResponse(
                body=b"c" * 1024,
                headers={"Content-Type": "application/octet-stream", "Content-Length": "1024"},
            ),
        )
        _, downloader = build(tmp_path, session)

        import bot.downloader.direct as direct_module

        monkeypatch.setattr(direct_module, "ensure_disk_space", _raise_no_space)
        with pytest.raises(InsufficientDiskSpaceError):
            await downloader.download("https://example.com/big.bin", tmp_path / "job")


def _raise_no_space(*args, **kwargs):
    raise InsufficientDiskSpaceError("no space", user_message="❌ Not enough disk space to download this file.")


class TestRetries:
    async def test_retries_transient_failure_then_succeeds(self, tmp_path):
        session = FakeSession()
        session.add(
            "https://example.com/flaky.bin",
            [FakeResponse(status=503), FakeResponse(status=503), FakeResponse(body=b"ok")],
        )
        _, downloader = build(tmp_path, session, MAX_RETRIES=3, DOWNLOAD_TIMEOUT_SECONDS=5)
        progress: list[tuple[int, int | None, float]] = []
        result = await downloader.download(
            "https://example.com/flaky.bin",
            tmp_path / "job",
            on_progress=lambda d, t, s: progress.append((d, t, s)),
        )
        assert result.size_bytes == 2
        assert len(session.calls) == 3
        assert any(step[0] == -1 for step in progress), "retry should be signalled to the reporter"

    async def test_does_not_retry_permanent_404(self, tmp_path):
        session = FakeSession()
        session.add("https://example.com/gone.zip", [FakeResponse(status=404)] * 5)
        _, downloader = build(tmp_path, session, MAX_RETRIES=3)
        with pytest.raises(HttpDownloadError):
            await downloader.download("https://example.com/gone.zip", tmp_path / "job")
        assert len(session.calls) == 1

    async def test_gives_up_after_max_retries(self, tmp_path):
        session = FakeSession()
        session.add("https://example.com/down.bin", [FakeResponse(status=502)] * 10)
        _, downloader = build(tmp_path, session, MAX_RETRIES=2)
        with pytest.raises(HttpDownloadError):
            await downloader.download("https://example.com/down.bin", tmp_path / "job")
        assert len(session.calls) == 3

    def test_retry_classification(self):
        assert _is_retryable(HttpDownloadError("HTTP 429 for x")) is True
        assert _is_retryable(HttpDownloadError("HTTP 500 for x")) is True
        assert _is_retryable(HttpDownloadError("HTTP 404 for x")) is False
        assert _is_retryable(ValueError("nope")) is False


class TestHelpers:
    def test_content_length_header(self):
        assert _content_length({"content-length": "123"}) == 123
        assert _content_length({"content-length": "abc"}) is None
        assert _content_length({"content-range": "bytes 0-99/5000"}) == 5000
        assert _content_length({}) is None

    @pytest.mark.parametrize(
        ("status", "fragment"),
        [
            (401, "login"),
            (403, "refused"),
            (404, "no longer exists"),
            (429, "rate limiting"),
            (451, "legal"),
            (500, "problem"),
            (418, "http 418"),
        ],
    )
    def test_http_error_messages(self, status, fragment):
        assert fragment in _http_error(status, "https://example.com/x").user_message.lower()

    async def test_safe_resolver_keeps_public_answers(self):
        # aiohttp's ResolveResult carries the hostname in "hostname" and the
        # resolved address in "host" - only the latter may be judged as an IP.
        answer = [{"hostname": "good.example", "host": "93.184.216.34", "port": 443, "family": 2}]
        resolver = SafeResolver(_InnerResolver(answer))
        assert await resolver.resolve("good.example") == answer

    async def test_safe_resolver_filters_private_answers(self):
        resolver = SafeResolver(_InnerResolver([{"hostname": "evil.example", "host": "10.0.0.1"}]))
        with pytest.raises(OSError):
            await resolver.resolve("evil.example")

    async def test_safe_resolver_filters_nat64_mapped_private_answers(self):
        resolver = SafeResolver(
            _InnerResolver([{"hostname": "evil.example", "host": "64:ff9b::7f00:1"}])
        )
        with pytest.raises(OSError):
            await resolver.resolve("evil.example")

    async def test_safe_resolver_drops_non_address_answers(self):
        resolver = SafeResolver(_InnerResolver([{"hostname": "weird.example", "host": "weird.example"}]))
        with pytest.raises(OSError):
            await resolver.resolve("weird.example")

    async def test_safe_resolver_mixes_public_and_private_answers(self):
        public = {"hostname": "dual.example", "host": "93.184.216.34"}
        resolver = SafeResolver(
            _InnerResolver([{"hostname": "dual.example", "host": "10.0.0.1"}, public])
        )
        assert await resolver.resolve("dual.example") == [public]

    async def test_safe_resolver_rejects_private_literals(self):
        class Inner:
            async def resolve(self, host, port=0, family=0):  # pragma: no cover - must not be reached
                raise AssertionError("resolver must not be consulted for blocked literals")

            async def close(self):
                return None

        resolver = SafeResolver(Inner())
        with pytest.raises(BlockedUrlError):
            await resolver.resolve("169.254.169.254")
