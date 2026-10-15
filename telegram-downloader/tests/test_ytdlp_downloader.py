"""yt-dlp integration: options, progress bridging, cancellation, error mapping."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import bot.downloader.ytdlp as ytdlp_module
from bot.downloader.types import DownloadMethod, MediaKind
from bot.downloader.ytdlp import YtDlpDownloader, _translate_error, ffmpeg_available
from bot.exceptions import DownloadCancelled, FileTooLargeError, YtDlpError
from tests.fakes import MP4_BYTES, make_fake_yt_dlp, make_settings


@pytest.fixture
def patched_ytdlp(monkeypatch):
    """Install a fake yt_dlp module and expose its state."""

    def install(**kwargs):
        module, state, error_cls = make_fake_yt_dlp(**kwargs)
        monkeypatch.setattr(ytdlp_module, "_import_yt_dlp", lambda: module)
        return state, error_cls

    return install


@pytest.fixture(autouse=True)
def assume_ffmpeg(monkeypatch):
    monkeypatch.setattr(ytdlp_module, "ffmpeg_available", lambda config: True)


class TestOptions:
    def test_builds_telegram_friendly_options(self, tmp_path):
        settings = make_settings(tmp_path)
        downloader = YtDlpDownloader(settings)
        options = downloader.build_options(tmp_path / "job", metadata_only=True)
        assert "mp4" in options["format"]
        assert options["paths"]["home"] == str(tmp_path / "job")
        assert options["restrictfilenames"] is True
        assert options["noplaylist"] is True
        assert options["max_filesize"] == settings.max_file_size_bytes
        assert options["retries"] == settings.max_retries
        assert options["socket_timeout"] == settings.download_timeout_seconds
        assert options["merge_output_format"] == "mp4"
        assert options["skip_download"] is True
        assert options["nocheckcertificate"] is False
        assert options["quiet"] is True

    def test_playlists_and_subtitles_are_configurable(self, tmp_path):
        settings = make_settings(tmp_path, ALLOW_PLAYLISTS="true", WRITE_SUBTITLES="true", SUBTITLE_LANGS="en,de")
        options = YtDlpDownloader(settings).build_options(tmp_path)
        assert options["noplaylist"] is False
        assert options["playlistend"] == 5
        assert options["writesubtitles"] is True
        assert options["subtitleslangs"] == ["en", "de"]

    def test_without_ffmpeg_no_remux_is_requested(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ytdlp_module, "ffmpeg_available", lambda config: False)
        settings = make_settings(tmp_path)
        options = YtDlpDownloader(settings).build_options(tmp_path)
        assert "merge_output_format" not in options
        assert options["writethumbnail"] is False

    def test_ffmpeg_availability(self, tmp_path):
        settings = make_settings(tmp_path, FFMPEG_LOCATION=str(tmp_path / "missing-ffmpeg"))
        assert ffmpeg_available(settings) is False


class TestProbe:
    async def test_probe_returns_metadata(self, tmp_path, patched_ytdlp):
        state, _ = patched_ytdlp()
        downloader = YtDlpDownloader(make_settings(tmp_path))
        info = await downloader.probe("https://example.com/watch?v=1")
        assert info["title"] == "Fake title"
        assert state["options"]["skip_download"] is True
        assert state["calls"] == [("https://example.com/watch?v=1", False)]

    async def test_probe_returns_none_for_unsupported_url(self, tmp_path, patched_ytdlp):
        patched_ytdlp(behavior="unsupported")
        downloader = YtDlpDownloader(make_settings(tmp_path))
        assert await downloader.probe("https://example.com/nope") is None

    async def test_probe_flattens_playlists(self, tmp_path, monkeypatch):
        module, state, _ = make_fake_yt_dlp()
        original = module.YoutubeDL

        class PlaylistYDL(original):
            def extract_info(self, url, download=True):
                return {
                    "_type": "playlist",
                    "title": "My playlist",
                    "entries": [{"title": "First", "id": "1", "ext": "mp4"}],
                }

        module.YoutubeDL = PlaylistYDL
        monkeypatch.setattr(ytdlp_module, "_import_yt_dlp", lambda: module)
        info = await YtDlpDownloader(make_settings(tmp_path)).probe("https://example.com/playlist")
        assert info is not None
        assert info["title"] == "First"
        assert info["playlist_title"] == "My playlist"
        assert info["playlist_count"] == 1

    async def test_probe_timeout_returns_none(self, tmp_path, monkeypatch):
        module, _, _ = make_fake_yt_dlp()

        class SlowYDL(module.YoutubeDL):
            def extract_info(self, url, download=True):
                time.sleep(2.0)
                return {}

        module.YoutubeDL = SlowYDL
        monkeypatch.setattr(ytdlp_module, "_import_yt_dlp", lambda: module)
        downloader = YtDlpDownloader(make_settings(tmp_path, METADATA_TIMEOUT_SECONDS=1))
        assert await downloader.probe("https://example.com/x") is None


class TestDownload:
    async def test_download_returns_result_and_reports_progress(self, tmp_path, patched_ytdlp):
        state, _ = patched_ytdlp()
        settings = make_settings(tmp_path)
        downloader = YtDlpDownloader(settings)
        progress: list[tuple[int, int | None, float]] = []
        stages: list[str] = []

        result = await downloader.download(
            "https://example.com/watch?v=1",
            tmp_path / "job",
            on_progress=lambda d, t, s: progress.append((d, t, s)),
            on_stage=stages.append,
        )
        await asyncio.sleep(0)  # let loop.call_soon_threadsafe callbacks run

        assert result.method is DownloadMethod.YTDLP
        assert result.title == "Fake title"
        assert result.duration == 12.5
        assert result.width == 640 and result.height == 360
        assert result.kind is MediaKind.VIDEO
        assert Path(result.path).exists()
        assert result.size_bytes == len(MP4_BYTES)
        assert progress, "progress hook must be bridged to the caller"
        assert progress[0][0] == len(MP4_BYTES) // 2
        assert "processing" in stages
        assert state["options"].get("skip_download") is not True

    async def test_download_respects_max_size(self, tmp_path, patched_ytdlp):
        # The extractor announces a 5 MB file while the configured limit is 1 MB.
        patched_ytdlp(report_total=5 * 1024 * 1024)
        settings = make_settings(tmp_path, MAX_FILE_SIZE_MB=1)
        downloader = YtDlpDownloader(settings)
        with pytest.raises(FileTooLargeError):
            await downloader.download("https://example.com/x", tmp_path / "job")

    async def test_oversized_output_file_is_deleted(self, tmp_path, monkeypatch):
        module, _, _ = make_fake_yt_dlp()
        monkeypatch.setattr(ytdlp_module, "_import_yt_dlp", lambda: module)
        settings = make_settings(tmp_path, MAX_FILE_SIZE_MB=1)
        downloader = YtDlpDownloader(settings)
        # A 2 MB file is produced but never announced through the hook.
        big = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * (2 * 1024 * 1024)
        original = module.YoutubeDL

        class BigYDL(original):
            def extract_info(self, url, download=True):
                payload = super().extract_info(url, download=True)
                target = Path(payload["requested_downloads"][0]["filepath"])
                target.write_bytes(big)
                return payload

        module.YoutubeDL = BigYDL
        with pytest.raises(FileTooLargeError):
            await downloader.download("https://example.com/x", tmp_path / "job")
        assert not list((tmp_path / "job").glob("*.mp4"))

    async def test_cancellation_is_raised_from_the_progress_hook(self, tmp_path, patched_ytdlp):
        patched_ytdlp(behavior="cancel")
        downloader = YtDlpDownloader(make_settings(tmp_path))
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(DownloadCancelled):
            await downloader.download("https://example.com/x", tmp_path / "job", cancel_event=cancel)

    async def test_unsupported_url_is_translated(self, tmp_path, patched_ytdlp):
        patched_ytdlp(behavior="unsupported")
        downloader = YtDlpDownloader(make_settings(tmp_path))
        with pytest.raises(YtDlpError) as excinfo:
            await downloader.download("https://example.com/nope", tmp_path / "job")
        assert "could not find downloadable media" in excinfo.value.user_message

    async def test_private_content_is_translated(self, tmp_path, patched_ytdlp):
        patched_ytdlp(behavior="private")
        downloader = YtDlpDownloader(make_settings(tmp_path))
        with pytest.raises(YtDlpError) as excinfo:
            await downloader.download("https://example.com/private", tmp_path / "job")
        assert "private or requires a login" in excinfo.value.user_message

    async def test_unexpected_errors_become_ytdlp_errors(self, tmp_path, patched_ytdlp):
        patched_ytdlp(behavior="boom")
        downloader = YtDlpDownloader(make_settings(tmp_path))
        with pytest.raises(YtDlpError):
            await downloader.download("https://example.com/x", tmp_path / "job")

    async def test_missing_output_file_is_reported(self, tmp_path, monkeypatch):
        module, state, _ = make_fake_yt_dlp()

        class NoFileYDL(module.YoutubeDL):
            def extract_info(self, url, download=True):
                return {"title": "T", "id": "1", "ext": "mp4", "requested_downloads": []}

            def prepare_filename(self, info):
                return str(tmp_path / "job" / "nope.mp4")

        module.YoutubeDL = NoFileYDL
        monkeypatch.setattr(ytdlp_module, "_import_yt_dlp", lambda: module)
        downloader = YtDlpDownloader(make_settings(tmp_path))
        with pytest.raises(YtDlpError) as excinfo:
            await downloader.download("https://example.com/x", tmp_path / "job")
        assert "no file was produced" in excinfo.value.user_message


class TestErrorTranslation:
    @pytest.mark.parametrize(
        ("message", "fragment"),
        [
            ("ERROR: Unsupported URL: https://x", "could not find downloadable media"),
            ("ERROR: Private video. Sign in if you've been granted access", "private or requires a login"),
            ("ERROR: This video is DRM protected", "DRM protected"),
            ("ERROR: File is larger than max-filesize (1000 bytes)", "too large"),
            ("ERROR: ffmpeg not found", "FFmpeg"),
            ("ERROR: This video is not available in your country (geo restricted)", "region"),
            ("ERROR: something odd", "could not be downloaded"),
        ],
    )
    def test_translation(self, message, fragment):
        error = _translate_error(Exception(message), "https://example.com/x")
        assert fragment.lower() in error.user_message.lower()

    def test_ansi_codes_are_stripped_from_the_reported_message(self):
        error = _translate_error(Exception("\x1b[0;31mERROR:\x1b[0m weird failure"), "https://example.com/x")
        assert "\x1b" not in error.user_message
