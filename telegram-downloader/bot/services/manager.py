"""Download job orchestration.

Owns the lifecycle of every download: per-job isolation (own task, temp
directory, cancel event), global and per-user concurrency limits, progress
reporting, database bookkeeping, upload and cleanup.  One failing job never
affects another.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import Settings
from bot.database import Database, utcnow_iso
from bot.downloader.router import DownloaderRouter, RouterDecision
from bot.downloader.types import DownloadMethod, DownloadResult, MediaKind
from bot.exceptions import (
    BotError,
    DownloadCancelled,
    FileTooLargeError,
    QueueLimitError,
    TelegramUploadError,
)
from bot.services.cleanup import CleanupService
from bot.services.metadata import completion_message, preview_text, safe_domain
from bot.services.progress import ProgressReporter, Stage
from bot.services.telegram_upload import TelegramUploader
from bot.utils.formatting import human_size, truncate
from bot.utils.logging import get_logger

logger = get_logger(__name__)

ACTIVE_STATUSES = frozenset({"queued", "analyzing", "downloading", "processing", "uploading"})
CONFIRM_CALLBACK = "dl:go"
CONFIRM_TIMEOUT_SECONDS = 300


@dataclass
class DownloadJob:
    """Everything one URL download needs. No shared mutable state."""

    job_id: str
    user_id: int
    chat_id: int
    url: str
    download_id: int
    reply_to_message_id: int | None = None
    as_document: bool = False
    status: str = "queued"
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    confirm_future: asyncio.Future | None = None
    temp_dir: Path | None = None
    decision: RouterDecision | None = None
    result: DownloadResult | None = None
    reporter: ProgressReporter | None = None
    task: asyncio.Task | None = None
    message_id: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    error: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


class DownloadManager:
    """Tracks and runs download jobs."""

    def __init__(
        self,
        config: Settings,
        db: Database,
        router: DownloaderRouter,
        uploads: TelegramUploader,
        cleanup: CleanupService,
    ) -> None:
        self.config = config
        self.db = db
        self.router = router
        self.uploads = uploads
        self.cleanup = cleanup
        self._jobs: dict[str, DownloadJob] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_downloads)
        self._tasks: set[asyncio.Task] = set()
        self._shutting_down = False

    # -------------------------------------------------------------- inspection
    def get(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def jobs_of(self, user_id: int, *, active_only: bool = True) -> list[DownloadJob]:
        jobs = [job for job in self._jobs.values() if job.user_id == user_id]
        if active_only:
            jobs = [job for job in jobs if job.is_active]
        return sorted(jobs, key=lambda job: job.started_at)

    def active_jobs(self) -> list[DownloadJob]:
        return [job for job in self._jobs.values() if job.is_active]

    def active_job_ids(self) -> set[str]:
        return {job.job_id for job in self.active_jobs()}

    def active_count(self) -> int:
        return len(self.active_jobs())

    # ------------------------------------------------------------------ submit
    async def submit(
        self,
        bot,
        *,
        user_id: int,
        chat_id: int,
        url: str,
        reply_to_message_id: int | None = None,
        as_document: bool = False,
        progress_updates: bool = True,
    ) -> DownloadJob:
        if self._shutting_down:
            raise BotError("bot is shutting down", user_message="🛑 The bot is restarting. Try again in a moment.")

        running = self.jobs_of(user_id)
        if len(running) >= self.config.max_downloads_per_user:
            raise QueueLimitError(
                f"user {user_id} already has {len(running)} active downloads",
                user_message=(
                    f"⏳ You already have {len(running)} download(s) in progress "
                    f"(limit: {self.config.max_downloads_per_user}).\n"
                    "Wait for one to finish or cancel it with /cancel."
                ),
            )

        job_id = uuid.uuid4().hex
        queue_note = (
            f"{len(self.active_jobs())} download(s) already running…"
            if self.active_count()
            else "Waiting for a free slot…"
        )
        keyboard = _cancel_keyboard(job_id)
        message = await bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Analyzing link…\n\n⏳ {queue_note}" if self.active_count() else "🔍 Analyzing link…",
            reply_to_message_id=reply_to_message_id,
            reply_markup=keyboard,
            disable_notification=True,
        )

        record = await self.db.create_download(
            job_id,
            user_id,
            url,
            status="queued",
            domain=safe_domain(url),
            chat_id=chat_id,
            message_id=message.message_id,
        )

        reporter = ProgressReporter(
            bot,
            chat_id,
            message.message_id,
            config=self.config,
            job_id=job_id,
            cancelable=True,
            progress_updates=progress_updates,
        )
        job = DownloadJob(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            download_id=record.id,
            reply_to_message_id=reply_to_message_id,
            as_document=as_document,
            reporter=reporter,
            message_id=message.message_id,
            status="queued",
        )
        self._jobs[job_id] = job
        await reporter.start()
        task = asyncio.create_task(self._run(job, bot), name=f"download:{job_id}")
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("job %s queued url=%s user=%s", job_id, url, user_id)
        return job

    # ------------------------------------------------------------ cancellation
    async def cancel(self, job_id: str, *, user_id: int | None = None) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if user_id is not None and job.user_id != user_id:
            return False
        job.cancel_event.set()
        if job.confirm_future is not None and not job.confirm_future.done():
            job.confirm_future.cancel()
        logger.info("job %s cancellation requested", job_id)
        return True

    async def cancel_all(self, user_id: int) -> int:
        count = 0
        for job in self.jobs_of(user_id):
            if await self.cancel(job.job_id, user_id=user_id):
                count += 1
        return count

    async def shutdown(self) -> None:
        self._shutting_down = True
        for job in list(self._jobs.values()):
            job.cancel_event.set()
            if job.confirm_future is not None and not job.confirm_future.done():
                job.confirm_future.cancel()
        pending = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # -------------------------------------------------------------------- run
    async def _run(self, job: DownloadJob, bot) -> None:
        reporter = job.reporter
        assert reporter is not None
        acquired = False
        try:
            await reporter.set_stage(Stage.QUEUED, note="Waiting for a free download slot…")
            await self._acquire(job)
            acquired = True

            job.status = "analyzing"
            await reporter.set_stage(Stage.ANALYZING)
            await self.db.update_download(job.job_id, status="analyzing")

            decision = await self.router.route(job.url)
            job.decision = decision
            reporter.set_title(decision.title or decision.url)
            self._check_expected_size(decision)
            await self.db.update_download(
                job.job_id,
                downloader=decision.method.value,
                title=decision.title,
                media_kind=decision.kind.value,
                size_bytes=decision.expected_size,
            )
            logger.info(
                "job %s routed method=%s kind=%s url=%s",
                job.job_id,
                decision.method.value,
                decision.kind.value,
                decision.url,
            )

            if self.config.cancel_confirm_before_download and not await self._confirm(job, decision, bot):
                raise DownloadCancelled("user did not confirm")
            if job.cancel_event.is_set():
                raise DownloadCancelled("cancelled after analysis")

            job.temp_dir = Path(self.config.temp_dir) / job.job_id
            job.temp_dir.mkdir(parents=True, exist_ok=True)

            job.status = "downloading"
            await reporter.set_stage(Stage.DOWNLOADING)
            await self.db.update_download(job.job_id, status="downloading")

            started = time.monotonic()
            result = await self._download(job, decision)
            job.result = result
            reporter.set_title(result.title or result.filename)
            duration = time.monotonic() - started
            logger.info(
                "job %s download complete file=%s bytes=%s in %.1fs",
                job.job_id,
                result.filename,
                result.size_bytes,
                duration,
            )

            job.status = "uploading"
            reporter.state.total = result.size_bytes
            reporter.state.downloaded = result.size_bytes
            await reporter.set_stage(Stage.UPLOADING)
            await self.db.update_download(
                job.job_id,
                status="uploading",
                filename=result.filename,
                size_bytes=result.size_bytes,
                media_kind=result.kind.value,
                title=result.title,
            )

            sent_as = await self.uploads.deliver(
                bot,
                job.chat_id,
                result,
                as_document=job.as_document,
                reply_to_message_id=job.reply_to_message_id,
                cancel_event=job.cancel_event,
            )

            total_time = job.elapsed()
            job.status = "completed"
            await self.db.update_download(
                job.job_id,
                status="completed",
                completed_at=utcnow_iso(),
                duration_seconds=total_time,
                size_bytes=result.size_bytes,
                filename=result.filename,
            )
            await reporter.finish(completion_message(result))
            logger.info(
                "job %s completed sent_as=%s bytes=%s duration=%.1fs",
                job.job_id,
                sent_as,
                result.size_bytes,
                total_time,
            )
        except (DownloadCancelled, asyncio.CancelledError) as exc:
            job.status = "cancelled"
            job.error = str(exc) or "cancelled"
            await self._safe_db_update(job, status="cancelled", completed_at=utcnow_iso(), error="cancelled by user")
            if isinstance(exc, asyncio.CancelledError):
                await reporter.finish("❌ Download cancelled.", stage=Stage.CANCELLED)
                raise
            await reporter.finish("❌ Download cancelled.", stage=Stage.CANCELLED)
            logger.info("job %s cancelled", job.job_id)
        except BotError as exc:
            job.status = "failed"
            job.error = exc.detail or str(exc)
            await self._safe_db_update(
                job, status="failed", completed_at=utcnow_iso(), error=job.error[:500]
            )
            await reporter.finish(exc.user_message, stage=Stage.FAILED)
            logger.warning("job %s failed: %s", job.job_id, exc.detail or exc)
        except Exception as exc:  # noqa: BLE001 - never let one job kill the bot
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            await self._safe_db_update(
                job, status="failed", completed_at=utcnow_iso(), error=job.error[:500]
            )
            await reporter.finish(
                "❌ Something went wrong while handling that link.\n"
                "The issue has been logged. Please try another link.",
                stage=Stage.FAILED,
            )
            logger.exception("job %s crashed", job.job_id)
        finally:
            try:
                self._finalize_files(job)
            except Exception:  # noqa: BLE001 - cleanup must never raise
                logger.exception("cleanup failed for job %s", job.job_id)
            await reporter.stop()
            if acquired:
                self._semaphore.release()
            self._jobs.pop(job.job_id, None)
            if job.confirm_future is not None and not job.confirm_future.done():
                job.confirm_future.cancel()

    # ------------------------------------------------------------- sub steps
    async def _acquire(self, job: DownloadJob) -> None:
        """Wait for a global slot, staying responsive to cancellation."""
        acquire = asyncio.create_task(self._semaphore.acquire(), name=f"slot:{job.job_id}")
        cancel_wait = asyncio.create_task(job.cancel_event.wait(), name=f"cancel:{job.job_id}")
        try:
            announced = False
            while True:
                done, _ = await asyncio.wait(
                    {acquire, cancel_wait}, timeout=3.0, return_when=asyncio.FIRST_COMPLETED
                )
                if acquire in done:
                    return
                if cancel_wait in done or job.cancel_event.is_set():
                    raise DownloadCancelled("cancelled while queued")
                if not announced and job.reporter is not None:
                    announced = True
                    await job.reporter.set_stage(
                        Stage.QUEUED,
                        note=(
                            f"{self.active_count()} download(s) in progress "
                            f"(limit {self.config.max_concurrent_downloads}) — you are next…"
                        ),
                    )
        finally:
            cancel_wait.cancel()
            if not acquire.done():
                acquire.cancel()

    async def _download(self, job: DownloadJob, decision: RouterDecision) -> DownloadResult:
        reporter = job.reporter
        assert reporter is not None
        assert job.temp_dir is not None

        def on_progress(downloaded: int, total: int | None, speed: float) -> None:
            reporter.report_bytes(downloaded, total, speed)

        def on_stage(stage: str) -> None:
            if stage == "processing":
                self._schedule(reporter.set_stage(Stage.PROCESSING))

        if decision.method is DownloadMethod.DIRECT:
            return await self.router.direct.download(
                decision.url,
                job.temp_dir,
                on_progress=on_progress,
                cancel_event=job.cancel_event,
                probe=decision.probe,
                title=decision.title,
            )
        return await self.router.ytdlp.download(
            decision.url,
            job.temp_dir,
            on_progress=on_progress,
            cancel_event=job.cancel_event,
            on_stage=on_stage,
        )

    async def _confirm(self, job: DownloadJob, decision: RouterDecision, bot) -> bool:
        """Optional explicit confirmation before transferring data."""
        reporter = job.reporter
        assert reporter is not None and job.message_id is not None
        text = preview_text(
            decision,
            max_file_size_bytes=self.config.max_file_size_bytes,
            telegram_upload_limit_bytes=self.config.telegram_upload_limit_bytes,
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⬇️ Download", callback_data=f"{CONFIRM_CALLBACK}:{job.job_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"dl:cancel:{job.job_id}"),
                ]
            ]
        )
        await bot.edit_message_text(
            chat_id=job.chat_id, message_id=job.message_id, text=text, reply_markup=keyboard
        )
        reporter.mute()  # the user drives the message until the download starts

        loop = asyncio.get_running_loop()
        job.confirm_future = loop.create_future()
        try:
            await asyncio.wait_for(job.confirm_future, timeout=CONFIRM_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.message_id,
                text="⌛ Preview expired. Send the link again to download it.",
                reply_markup=None,
            )
            return False
        if job.cancel_event.is_set():
            return False
        reporter.unmute()
        return True

    def confirm(self, job_id: str) -> bool:
        """Called by the callback handler when the user taps ⬇️ Download."""
        job = self._jobs.get(job_id)
        if job is None or job.confirm_future is None or job.confirm_future.done():
            return False
        job.confirm_future.set_result(True)
        return True

    # ------------------------------------------------------------- bookkeeping
    def _check_expected_size(self, decision: RouterDecision) -> None:
        size = decision.expected_size
        if size and size > self.config.max_file_size_bytes:
            raise FileTooLargeError(
                f"{decision.url} is {human_size(size)}, limit {human_size(self.config.max_file_size_bytes)}",
                user_message=(
                    "❌ File is too large.\n\n"
                    f"Maximum allowed size: {human_size(self.config.max_file_size_bytes)}\n"
                    f"Detected size: {human_size(size)}"
                ),
            )
        authoritative = bool(decision.probe and decision.probe.content_length)
        if authoritative and size and size > self.config.telegram_upload_limit_bytes:
            raise TelegramUploadError(
                f"{decision.url} is {human_size(size)}, telegram limit "
                f"{human_size(self.config.telegram_upload_limit_bytes)}",
                user_message=(
                    "❌ That file is too large to upload to Telegram.\n\n"
                    f"File size: {human_size(size)}\n"
                    f"Telegram upload limit configured for this bot: "
                    f"{human_size(self.config.telegram_upload_limit_bytes)}\n\n"
                    "It was not downloaded."
                ),
            )

    def _finalize_files(self, job: DownloadJob) -> None:
        """Keep the file (optional) and always remove the job's temp directory."""
        if job.status == "completed" and job.result is not None and self.config.keep_files:
            source = Path(job.result.path)
            if source.exists():
                target_dir = Path(self.config.downloads_dir) / str(job.user_id)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / source.name
                if not target.exists():
                    shutil.move(str(source), str(target))
                    logger.info("job %s kept file at %s", job.job_id, target)
        if job.temp_dir is not None:
            self.cleanup.cleanup_job_dir(job.temp_dir)

    async def _safe_db_update(self, job: DownloadJob, **fields: Any) -> None:
        try:
            await self.db.update_download(job.job_id, **fields)
        except Exception:  # noqa: BLE001 - DB issues must not mask the real error
            logger.exception("could not update download record %s", job.job_id)

    def _schedule(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"dl:cancel:{job_id}")]])


def summarize_job(job: DownloadJob) -> str:
    """One-line description used by /status."""
    stage = job.reporter.state.stage.value if job.reporter else job.status
    detail = ""
    if job.reporter is not None and job.reporter.state.stage is Stage.DOWNLOADING:
        from bot.utils.formatting import percent

        pct = percent(job.reporter.state.downloaded, job.reporter.state.total)
        if pct is not None:
            detail = f" · {pct:.0f}%"
    return f"• {stage}{detail} · {truncate(safe_domain(job.url), 40)} · {job.elapsed():.0f}s"


def media_kind_label(kind: MediaKind) -> str:  # pragma: no cover - helper for handlers
    return kind.label
