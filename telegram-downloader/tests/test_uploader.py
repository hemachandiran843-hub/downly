"""Telegram upload: media type selection, fallbacks, limits and cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.downloader.types import DownloadMethod, DownloadResult, MediaKind
from bot.exceptions import DownloadCancelled, TelegramUploadError
from bot.services.telegram_upload import TelegramUploader, result_caption
from tests.fakes import FakeBot, make_settings


def make_result(tmp_path: Path, *, kind=MediaKind.VIDEO, name="clip.mp4", size=1024, **kwargs) -> DownloadResult:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return DownloadResult(
        path=str(path),
        filename=name,
        size_bytes=size,
        kind=kind,
        method=DownloadMethod.DIRECT,
        source_url="https://example.com/clip.mp4",
        title=kwargs.pop("title", "A clip"),
        **kwargs,
    )


@pytest.fixture
def uploader(tmp_path):
    return TelegramUploader(make_settings(tmp_path))


async def test_video_is_sent_as_video(tmp_path, bot, uploader):
    result = make_result(tmp_path, kind=MediaKind.VIDEO, duration=12.0, width=640, height=360)
    method = await uploader.deliver(bot, 1, result)
    assert method == "video"
    assert bot.sent_files == [("video", "clip.mp4")]


async def test_audio_is_sent_as_audio(tmp_path, bot, uploader):
    result = make_result(tmp_path, kind=MediaKind.AUDIO, name="song.m4a", duration=180.0, artist="Someone")
    assert await uploader.deliver(bot, 1, result) == "audio"


async def test_small_image_is_sent_as_photo(tmp_path, bot, uploader):
    result = make_result(tmp_path, kind=MediaKind.IMAGE, name="pic.png")
    assert await uploader.deliver(bot, 1, result) == "photo"


async def test_large_image_falls_back_to_document(tmp_path, bot, uploader):
    result = make_result(tmp_path, kind=MediaKind.IMAGE, name="huge.png", size=11 * 1024 * 1024)
    assert await uploader.deliver(bot, 1, result) == "document"


async def test_gif_is_sent_as_animation(tmp_path, bot, uploader):
    result = make_result(tmp_path, kind=MediaKind.IMAGE, name="anim.gif")
    assert await uploader.deliver(bot, 1, result) == "animation"


async def test_archive_and_unknown_go_as_document(tmp_path, bot, uploader):
    archive = make_result(tmp_path, kind=MediaKind.ARCHIVE, name="files.zip")
    assert await uploader.deliver(bot, 1, archive) == "document"
    unknown = make_result(tmp_path, kind=MediaKind.UNKNOWN, name="blob")
    assert await uploader.deliver(bot, 1, unknown) == "document"


async def test_user_setting_forces_document(tmp_path, bot, uploader):
    result = make_result(tmp_path, kind=MediaKind.VIDEO)
    assert await uploader.deliver(bot, 1, result, as_document=True) == "document"


async def test_video_rejected_by_telegram_falls_back_to_document(tmp_path, uploader):
    bot = FakeBot()
    bot.fail_video = True
    result = make_result(tmp_path, kind=MediaKind.VIDEO)
    assert await uploader.deliver(bot, 1, result) == "document"
    assert [kind for kind, _ in bot.sent_files] == ["document"]


async def test_all_methods_failing_reports_upload_error(tmp_path):
    bot = FakeBot(fail_document=True)
    bot.fail_video = True
    uploader = TelegramUploader(make_settings(tmp_path))
    result = make_result(tmp_path, kind=MediaKind.VIDEO)
    with pytest.raises(TelegramUploadError):
        await uploader.deliver(bot, 1, result)


async def test_upload_limit_is_enforced_before_sending(tmp_path, bot):
    uploader = TelegramUploader(make_settings(tmp_path, TELEGRAM_UPLOAD_LIMIT_MB=1))
    result = make_result(tmp_path, size=2 * 1024 * 1024)
    with pytest.raises(TelegramUploadError) as excinfo:
        await uploader.deliver(bot, 1, result)
    assert "too large to upload" in excinfo.value.user_message
    assert "File size" in excinfo.value.user_message
    assert bot.sent_files == []


async def test_zero_byte_file_is_rejected(tmp_path, bot, uploader):
    result = make_result(tmp_path, size=0)
    with pytest.raises(TelegramUploadError):
        await uploader.deliver(bot, 1, result)


async def test_missing_file_is_rejected(tmp_path, bot, uploader):
    result = make_result(tmp_path)
    Path(result.path).unlink()
    with pytest.raises(TelegramUploadError):
        await uploader.deliver(bot, 1, result)


async def test_cancellation_before_upload(tmp_path, bot, uploader):
    result = make_result(tmp_path)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        await uploader.deliver(bot, 1, result, cancel_event=cancel)
    assert bot.sent_files == []


async def test_cancellation_during_upload(tmp_path, bot, uploader):
    result = make_result(tmp_path)
    cancel = asyncio.Event()

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.3)

    bot.send_video = slow_send  # type: ignore[assignment]

    async def cancel_soon():
        await asyncio.sleep(0.05)
        cancel.set()

    task = asyncio.create_task(cancel_soon())
    with pytest.raises(DownloadCancelled):
        await uploader.deliver(bot, 1, result, cancel_event=cancel)
    await task


async def test_thumbnail_is_attached_when_small_enough(tmp_path, uploader):
    bot = FakeBot()
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"j" * 1024)
    result = make_result(tmp_path, kind=MediaKind.VIDEO, thumbnail=str(thumb))
    captured: dict = {}

    async def send_video(chat_id, video, **kwargs):
        captured.update(kwargs)

    bot.send_video = send_video  # type: ignore[assignment]
    assert await uploader.deliver(bot, 1, result) == "video"
    assert "thumbnail" in captured and captured["thumbnail"] is not None


async def test_oversized_thumbnail_is_ignored(tmp_path, uploader):
    bot = FakeBot()
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"j" * (300 * 1024))
    result = make_result(tmp_path, kind=MediaKind.VIDEO, thumbnail=str(thumb))
    captured: dict = {}

    async def send_video(chat_id, video, **kwargs):
        captured.update(kwargs)

    bot.send_video = send_video  # type: ignore[assignment]
    await uploader.deliver(bot, 1, result)
    assert "thumbnail" not in captured


async def test_caption_contains_filename_size_and_method(tmp_path):
    result = make_result(tmp_path, kind=MediaKind.DOCUMENT, name="report.pdf", size=2048)
    caption = result_caption(result)
    assert caption is not None
    assert "report.pdf" in caption
    assert "2.0 KB" in caption
    assert "direct link" in caption


def test_caption_helper_edge_cases(tmp_path):
    result = make_result(tmp_path, kind=MediaKind.DOCUMENT, name="a.bin", size=10, title="a.bin")
    assert result_caption(result) is not None
    long_title = make_result(tmp_path, title="t" * 500, name="b.bin", size=10)
    assert len(result_caption(long_title) or "") <= 900
