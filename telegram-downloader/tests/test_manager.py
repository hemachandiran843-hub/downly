"""Job orchestration: success, failure, cancellation, limits, cleanup, isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.database import Database
from bot.downloader.router import DownloaderRouter
from bot.downloader.types import MediaKind
from bot.exceptions import (
    BotError,
    DownloadCancelled,
    FileTooLargeError,
    QueueLimitError,
    TelegramUploadError,
    YtDlpError,
)
from bot.services.cleanup import CleanupService
from bot.services.manager import DownloadManager, summarize_job
from bot.services.telegram_upload import TelegramUploader
from tests.fakes import FakeBot, StubDirect, StubYtDlp, make_settings


class Harness:
    def __init__(self, tmp_path, direct=None, ytdlp=None, **overrides):
        self.settings = make_settings(tmp_path, **overrides)
        self.settings.ensure_directories()
        self.bot = FakeBot()
        self.direct = direct or StubDirect()
        self.ytdlp = ytdlp or StubYtDlp()
        self.router = DownloaderRouter(self.settings, self.direct, self.ytdlp)
        self.uploads = TelegramUploader(self.settings)
        self.cleanup = CleanupService(self.settings)
        self.db = Database(self.settings.database_path)
        self.manager = DownloadManager(self.settings, self.db, self.router, self.uploads, self.cleanup)

    async def start(self):
        await self.db.connect()
        await self.db.upsert_user(1, "tester")
        return self

    async def stop(self):
        await self.manager.shutdown()
        await self.db.close()

    async def run(self, url="https://example.com/clip.mp4", **kwargs):
        job = await self.manager.submit(self.bot, user_id=1, chat_id=1, url=url, **kwargs)
        assert job.task is not None
        await asyncio.wait_for(job.task, timeout=20)
        return job


@pytest.fixture
async def harness(tmp_path):
    h = await Harness(tmp_path).start()
    try:
        yield h
    finally:
        await h.stop()


async def test_successful_job_end_to_end(harness):
    job = await harness.run()
    assert job.status == "completed"
    record = await harness.db.get_download(job.job_id)
    assert record.status == "completed"
    assert record.downloader == "direct"
    assert record.media_kind == "video"
    assert record.size_bytes == job.result.size_bytes
    assert record.completed_at is not None
    assert record.duration_seconds is not None
    assert harness.bot.sent_files == [("video", "clip.mp4")]
    assert not (harness.settings.temp_dir / job.job_id).exists()
    assert job.job_id not in harness.manager._jobs
    assert harness.manager.active_jobs() == []


async def test_status_message_sequence(harness):
    await harness.run()
    texts = [text for _, _, text in harness.bot.edits]
    assert any("Analyzing" in text for text in texts)
    assert any("Downloading" in text for text in texts)
    assert any("Uploading" in text for text in texts)
    assert texts[-1].startswith("✅ Download complete")


async def test_per_job_isolation_and_concurrency(harness):
    harness.settings.max_downloads_per_user = 3
    jobs = await asyncio.gather(
        harness.run("https://example.com/a.mp4"),
        harness.run("https://example.com/b.mp4"),
        harness.run("https://example.com/c.mp4"),
    )
    assert all(job.status == "completed" for job in jobs)
    assert len({job.job_id for job in jobs}) == 3
    assert all(job.temp_dir != other.temp_dir for job in jobs for other in jobs if job is not other)
    assert len(harness.bot.sent_files) == 3


async def test_per_user_limit(harness):
    harness.settings.max_downloads_per_user = 1
    slow = StubDirect(progress_steps=20, step_delay=0.05)
    harness.manager.router.direct = slow

    first = await harness.manager.submit(harness.bot, user_id=1, chat_id=1, url="https://example.com/1.mp4")
    with pytest.raises(QueueLimitError) as excinfo:
        await harness.manager.submit(harness.bot, user_id=1, chat_id=1, url="https://example.com/2.mp4")
    assert "already have" in excinfo.value.user_message
    assert first.task is not None
    await asyncio.wait_for(first.task, timeout=20)


async def test_global_concurrency_limit_queues_jobs(tmp_path):
    h = await Harness(
        tmp_path,
        direct=StubDirect(progress_steps=8, step_delay=0.05),
        MAX_CONCURRENT_DOWNLOADS=1,
        MAX_DOWNLOADS_PER_USER=3,
    ).start()
    try:
        first = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/1.mp4")
        second = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/2.mp4")
        assert second.status == "queued"
        await asyncio.wait_for(asyncio.gather(first.task, second.task), timeout=30)
        assert first.status == "completed" and second.status == "completed"
    finally:
        await h.stop()


async def test_size_limit_fails_before_downloading(harness):
    harness.direct.size = 5 * 1024 * 1024
    harness.settings.max_file_size_bytes = 1024 * 1024
    job = await harness.run()
    assert job.status == "failed"
    assert job.error and "limit" in job.error.lower()
    assert harness.direct.download_calls == 0
    record = await harness.db.get_download(job.job_id)
    assert record.status == "failed"
    assert "limit" in (record.error or "").lower()
    assert any("File is too large" in text for _, _, text in harness.bot.edits)


async def test_upload_limit_fails_before_downloading(harness):
    harness.direct.size = 100 * 1024 * 1024
    harness.settings.telegram_upload_limit_bytes = 10 * 1024 * 1024
    job = await harness.run()
    assert job.status == "failed"
    assert harness.direct.download_calls == 0
    assert any("too large to upload to Telegram" in text for _, _, text in harness.bot.edits)


async def test_cancellation_stops_the_download(harness):
    slow = StubDirect(progress_steps=50, step_delay=0.02)
    harness.manager.router.direct = slow
    job = await harness.manager.submit(harness.bot, user_id=1, chat_id=1, url="https://example.com/slow.mp4")
    await asyncio.sleep(0.05)
    assert await harness.manager.cancel(job.job_id, user_id=1) is True
    await asyncio.wait_for(job.task, timeout=20)
    assert job.status == "cancelled"
    record = await harness.db.get_download(job.job_id)
    assert record.status == "cancelled"
    assert harness.bot.sent_files == []
    assert not (harness.settings.temp_dir / job.job_id).exists()
    assert any("cancelled" in text for _, _, text in harness.bot.edits)


async def test_cancel_rejects_other_users(harness):
    slow = StubDirect(progress_steps=30, step_delay=0.02)
    harness.manager.router.direct = slow
    job = await harness.manager.submit(harness.bot, user_id=1, chat_id=1, url="https://example.com/x.mp4")
    assert await harness.manager.cancel(job.job_id, user_id=999) is False
    assert job.cancel_event.is_set() is False
    await harness.manager.cancel(job.job_id, user_id=1)
    await asyncio.wait_for(job.task, timeout=20)


async def test_cancel_all(harness):
    harness.settings.max_downloads_per_user = 3
    slow = StubDirect(progress_steps=40, step_delay=0.02)
    harness.manager.router.direct = slow
    jobs = [
        await harness.manager.submit(harness.bot, user_id=1, chat_id=1, url=f"https://example.com/{i}.mp4")
        for i in range(2)
    ]
    await asyncio.sleep(0.05)
    assert await harness.manager.cancel_all(1) == 2
    await asyncio.wait_for(asyncio.gather(*[job.task for job in jobs]), timeout=20)
    assert all(job.status == "cancelled" for job in jobs)
    assert await harness.manager.cancel_all(1) == 0


async def test_ytdlp_route_is_used_for_pages(harness):
    harness.manager.router.direct = StubDirect(kind=MediaKind.HTML, content_type="text/html", filename=None)
    harness.manager.router.ytdlp = StubYtDlp(info={"title": "Page video", "ext": "mp4", "vcodec": "avc1"})
    job = await harness.run("https://example.com/watch?v=1")
    assert job.status == "completed"
    record = await harness.db.get_download(job.job_id)
    assert record.downloader == "ytdlp"
    assert record.title == "Page video"
    assert harness.bot.sent_files[0][0] == "video"
    assert any("Processing" in text for _, _, text in harness.bot.edits)


async def test_download_error_is_reported_friendly(harness):
    harness.manager.router.direct = StubDirect(download_error=YtDlpError("boom", user_message="❌ Custom failure."))
    job = await harness.run("https://example.com/broken.mp4")
    assert job.status == "failed"
    assert harness.bot.edits[-1][2] == "❌ Custom failure."
    record = await harness.db.get_download(job.job_id)
    assert record.status == "failed"
    assert record.error == "boom"


async def test_unexpected_exception_is_contained(harness):
    harness.manager.router.direct = StubDirect(download_error=RuntimeError("kaboom"))
    job = await harness.run("https://example.com/weird.mp4")
    assert job.status == "failed"
    assert "went wrong" in harness.bot.edits[-1][2]
    assert not (harness.settings.temp_dir / job.job_id).exists()


async def test_upload_failure_marks_job_failed_and_cleans_up(harness):
    harness.settings.telegram_upload_limit_bytes = 10
    job = await harness.run()
    assert job.status == "failed"
    record = await harness.db.get_download(job.job_id)
    assert record.status == "failed"
    assert not (harness.settings.temp_dir / job.job_id).exists()
    assert harness.bot.sent_files == []


async def test_keep_files_moves_download_to_downloads_dir(harness):
    harness.settings.keep_files = True
    job = await harness.run()
    assert job.status == "completed"
    kept = Path(harness.settings.downloads_dir) / "1" / "clip.mp4"
    assert kept.exists()
    assert not (harness.settings.temp_dir / job.job_id).exists()


async def test_confirmation_flow(tmp_path):
    h = await Harness(tmp_path, CONFIRM_BEFORE_DOWNLOAD="true").start()
    try:
        job = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/clip.mp4")
        await asyncio.sleep(0.1)
        assert job.status == "analyzing"
        assert h.direct.download_calls == 0
        assert any("Ready to download?" in text for _, _, text in h.bot.edits)
        assert h.manager.confirm(job.job_id) is True
        await asyncio.wait_for(job.task, timeout=20)
        assert job.status == "completed"
    finally:
        await h.stop()


async def test_confirmation_can_be_cancelled(tmp_path):
    h = await Harness(tmp_path, CONFIRM_BEFORE_DOWNLOAD="true").start()
    try:
        job = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/clip.mp4")
        await asyncio.sleep(0.1)
        await h.manager.cancel(job.job_id, user_id=1)
        await asyncio.wait_for(job.task, timeout=20)
        assert job.status == "cancelled"
        assert h.direct.download_calls == 0
    finally:
        await h.stop()


async def test_status_helpers(harness):
    slow = StubDirect(progress_steps=40, step_delay=0.02)
    harness.manager.router.direct = slow
    job = await harness.manager.submit(harness.bot, user_id=1, chat_id=1, url="https://example.com/x.mp4")
    assert harness.manager.get(job.job_id) is job
    assert harness.manager.jobs_of(1) == [job]
    assert harness.manager.active_job_ids() == {job.job_id}
    assert harness.manager.active_count() == 1
    line = summarize_job(job)
    assert "example.com" in line
    await harness.manager.cancel(job.job_id, user_id=1)
    await asyncio.wait_for(job.task, timeout=20)
    assert harness.manager.get(job.job_id) is None


async def test_shutdown_cancels_active_jobs(tmp_path):
    h = await Harness(tmp_path, direct=StubDirect(progress_steps=50, step_delay=0.02)).start()
    try:
        job = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/slow.mp4")
        await asyncio.sleep(0.05)
        await h.manager.shutdown()
        assert job.status == "cancelled"
        assert h.manager.active_jobs() == []
    finally:
        await h.db.close()


async def test_submit_after_shutdown_is_rejected(tmp_path):
    h = await Harness(tmp_path).start()
    try:
        await h.manager.shutdown()
        with pytest.raises(BotError) as excinfo:
            await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/x.mp4")
        assert "restarting" in excinfo.value.user_message
    finally:
        await h.db.close()


async def test_cancelling_queued_job_never_downloads(tmp_path):
    h = await Harness(
        tmp_path, direct=StubDirect(), MAX_CONCURRENT_DOWNLOADS=1, MAX_DOWNLOADS_PER_USER=5
    ).start()
    try:
        blocker = StubDirect(progress_steps=30, step_delay=0.02)
        h.manager.router.direct = blocker
        first = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/first.mp4")
        await asyncio.sleep(0.05)
        second = await h.manager.submit(h.bot, user_id=1, chat_id=1, url="https://example.com/second.mp4")
        assert second.status == "queued"
        await h.manager.cancel(second.job_id, user_id=1)
        await asyncio.wait_for(second.task, timeout=20)
        assert second.status == "cancelled"
        assert blocker.download_calls == 1  # only the first job ever downloaded
        await asyncio.wait_for(first.task, timeout=20)
    finally:
        await h.stop()


async def test_file_too_large_error_type_is_preserved(harness):
    harness.manager.router.direct = StubDirect(download_error=FileTooLargeError("nope"))
    job = await harness.run()
    assert job.status == "failed"
    assert job.error is not None


async def test_telegram_upload_error_type_is_preserved(harness):
    harness.manager.router.direct = StubDirect(download_error=TelegramUploadError("nope"))
    job = await harness.run()
    assert job.status == "failed"


async def test_download_cancelled_error_type_is_preserved(harness):
    harness.manager.router.direct = StubDirect(download_error=DownloadCancelled("stopped"))
    job = await harness.run()
    assert job.status == "cancelled"
