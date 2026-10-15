"""Router: direct vs yt-dlp selection and metadata propagation."""

from __future__ import annotations

import pytest

from bot.downloader.router import DownloaderRouter, is_manifest
from bot.downloader.types import DownloadMethod, MediaKind, ProbeResult
from bot.exceptions import BlockedUrlError, HttpDownloadError, InvalidUrlError, UnsupportedUrlError
from tests.fakes import StubDirect, StubYtDlp, make_settings


def build(tmp_path, direct: StubDirect | None = None, ytdlp: StubYtDlp | None = None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    direct = direct or StubDirect()
    ytdlp = ytdlp or StubYtDlp()
    return settings, DownloaderRouter(settings, direct, ytdlp), direct, ytdlp


async def test_video_file_goes_direct(tmp_path):
    _, router, direct, ytdlp = build(tmp_path)
    decision = await router.route("https://example.com/clip.mp4")
    assert decision.method is DownloadMethod.DIRECT
    assert decision.kind is MediaKind.VIDEO
    assert direct.probe_calls == 1
    assert ytdlp.probe_calls == 0, "no need to consult yt-dlp for a plain file"


async def test_octet_stream_without_extension_goes_direct(tmp_path):
    direct = StubDirect(kind=MediaKind.UNKNOWN, content_type="application/octet-stream", filename="blob")
    _, router, _, ytdlp = build(tmp_path, direct)
    decision = await router.route("https://example.com/download?id=7")
    assert decision.method is DownloadMethod.DIRECT
    assert decision.kind is MediaKind.UNKNOWN
    assert ytdlp.probe_calls == 0


async def test_html_page_goes_to_ytdlp(tmp_path):
    direct = StubDirect(kind=MediaKind.HTML, content_type="text/html", filename=None)
    ytdlp = StubYtDlp(info={"title": "A video", "ext": "mp4", "vcodec": "avc1", "duration": 90, "filesize": 2048})
    _, router, _, _ = build(tmp_path, direct, ytdlp)
    decision = await router.route("https://example.com/watch?v=abc")
    assert decision.method is DownloadMethod.YTDLP
    assert decision.title == "A video"
    assert decision.kind is MediaKind.VIDEO
    assert decision.expected_size == 2048
    assert decision.duration == 90.0


async def test_html_page_without_extractor_is_unsupported(tmp_path):
    direct = StubDirect(kind=MediaKind.HTML, content_type="text/html", filename=None)
    _, router, _, _ = build(tmp_path, direct, StubYtDlp())
    with pytest.raises(UnsupportedUrlError) as excinfo:
        await router.route("https://example.com/article")
    assert "web page" in excinfo.value.user_message


async def test_probe_failure_falls_back_to_ytdlp(tmp_path):
    direct = StubDirect(probe_error=HttpDownloadError("HTTP 403 for https://example.com/watch"))
    ytdlp = StubYtDlp(info={"title": "T", "ext": "mp4", "vcodec": "avc1"})
    _, router, _, _ = build(tmp_path, direct, ytdlp)
    decision = await router.route("https://example.com/watch")
    assert decision.method is DownloadMethod.YTDLP
    assert decision.probe_error is not None


async def test_probe_and_ytdlp_failure_falls_back_to_direct(tmp_path):
    direct = StubDirect(probe_error=HttpDownloadError("HTTP 403"))
    _, router, _, _ = build(tmp_path, direct, StubYtDlp())
    decision = await router.route("https://example.com/weird")
    assert decision.method is DownloadMethod.DIRECT
    assert "inspection failed" in decision.reason


@pytest.mark.parametrize("url", ["https://cdn.example.com/live/stream.m3u8", "https://example.com/manifest.mpd"])
async def test_manifests_are_handled_by_ytdlp(tmp_path, url):
    direct = StubDirect(kind=MediaKind.VIDEO, content_type="application/vnd.apple.mpegurl", filename=None)
    ytdlp = StubYtDlp(info={"title": "Stream", "ext": "mp4", "vcodec": "avc1"})
    _, router, _, _ = build(tmp_path, direct, ytdlp)
    decision = await router.route(url)
    assert decision.method is DownloadMethod.YTDLP


async def test_manifest_detection_helpers():
    assert is_manifest(None, "https://x.example/a/b.m3u8")
    assert is_manifest(None, "https://x.example/a/b.mpd?token=1")
    assert not is_manifest(None, "https://x.example/a/b.mp4")
    probe = ProbeResult(url="u", final_url="u", status=200, content_type="application/dash+xml")
    assert is_manifest(probe, "https://x.example/a/b.mp4")


async def test_invalid_url_raises_before_any_network_call(tmp_path):
    _, router, direct, _ = build(tmp_path)
    with pytest.raises(InvalidUrlError):
        await router.route("not a url")
    with pytest.raises(BlockedUrlError):
        await router.route("http://127.0.0.1/secret")
    assert direct.probe_calls == 0


async def test_audio_info_maps_to_audio_kind(tmp_path):
    direct = StubDirect(kind=MediaKind.HTML, content_type="text/html", filename=None)
    ytdlp = StubYtDlp(info={"title": "Song", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"})
    _, router, _, _ = build(tmp_path, direct, ytdlp)
    decision = await router.route("https://example.com/track")
    assert decision.kind is MediaKind.AUDIO


async def test_decision_uses_probe_metadata_when_info_is_empty(tmp_path):
    direct = StubDirect(
        kind=MediaKind.DOCUMENT, content_type="application/pdf", filename="doc.pdf", size=4096
    )
    _, router, _, _ = build(tmp_path, direct)
    decision = await router.route("https://example.com/doc.pdf")
    assert decision.expected_size == 4096
    assert decision.kind is MediaKind.DOCUMENT
    assert decision.title is None
