"""Progress reporting.

A single Telegram message per job is edited at most once every
``PROGRESS_UPDATE_INTERVAL_SECONDS`` (default 2.5s) by a small ticker task, so
progress never floods the chat and never slows the download down.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from bot.config import Settings
from bot.utils.formatting import eta_seconds, human_duration, human_size, human_speed, percent, progress_bar, truncate
from bot.utils.logging import get_logger
from bot.utils.retry import retry_after_seconds

logger = get_logger(__name__)

CANCEL_CALLBACK = "dl:cancel"
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Stage(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


_STAGE_HEADERS: dict[Stage, str] = {
    Stage.QUEUED: "⏳ Waiting in queue…",
    Stage.ANALYZING: "🔍 Analyzing link…",
    Stage.DOWNLOADING: "📥 Downloading",
    Stage.PROCESSING: "⚙️ Processing media…",
    Stage.UPLOADING: "📤 Uploading to Telegram…",
    Stage.COMPLETE: "✅ Download complete",
    Stage.FAILED: "❌ Download failed",
    Stage.CANCELLED: "❌ Download cancelled.",
}


@dataclass
class ProgressState:
    """Mutable snapshot of a job's progress (owned by the reporter)."""

    stage: Stage = Stage.QUEUED
    downloaded: int = 0
    total: int | None = None
    speed: float = 0.0
    title: str | None = None
    note: str | None = None
    final_text: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_update: float = 0.0
    dirty: bool = True

    def reset_counters(self) -> None:
        self.downloaded = 0
        self.total = None
        self.speed = 0.0


class ProgressReporter:
    """Renders and edits the status message of one download job."""

    def __init__(
        self,
        bot,
        chat_id: int,
        message_id: int | None,
        *,
        config: Settings,
        job_id: str,
        cancelable: bool = True,
        progress_updates: bool = True,
    ) -> None:
        self.bot = bot
        self.chat_id = int(chat_id)
        self.message_id = message_id
        self.config = config
        self.job_id = job_id
        self.cancelable = cancelable
        self.progress_updates = progress_updates
        self.state = ProgressState()
        self._ticker: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._disabled = message_id is None
        self._muted = False
        self._progress_enabled = bool(progress_updates)
        self._last_text: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._disabled or self._ticker is not None:
            return
        self._stop.clear()
        self._ticker = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self.config.progress_update_interval_seconds)
                if self.state.dirty:
                    await self.flush()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # pragma: no cover - the ticker must never crash the bot
            logger.exception("progress ticker failed for job %s", self.job_id)

    async def stop(self) -> None:
        self._stop.set()
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ticker

    # ---------------------------------------------------------------- updates
    def set_title(self, title: str | None) -> None:
        if title and title != self.state.title:
            self.state.title = truncate(title, 80)
            self.state.dirty = True

    def set_note(self, note: str | None) -> None:
        if note != self.state.note:
            self.state.note = note
            self.state.dirty = True

    async def set_stage(self, stage: Stage, *, note: str | None = None, force: bool = False) -> None:
        changed = stage is not self.state.stage
        self.state.stage = stage
        if note is not None:
            self.state.note = note
        if stage in {Stage.DOWNLOADING, Stage.PROCESSING, Stage.UPLOADING} and changed and stage is Stage.DOWNLOADING:
            self.state.reset_counters()
        if changed or force:
            self.state.dirty = True
            await self.flush(force=True)

    def report_bytes(self, downloaded: int, total: int | None, speed: float) -> None:
        """Called for every chunk; only marks state dirty (the ticker edits)."""
        if self._disabled:
            return
        if downloaded < 0:  # retry signal from the downloader
            self.state.downloaded = 0
            self.state.speed = 0.0
            self.state.note = self.state.note or "⚠️ Connection interrupted, retrying…"
            self.state.dirty = True
            return
        self.state.downloaded = downloaded
        if total:
            self.state.total = total
        self.state.speed = speed
        self.state.dirty = True

    async def finish(self, text: str, *, stage: Stage = Stage.COMPLETE) -> None:
        self.state.stage = stage
        self.state.final_text = text
        self.state.dirty = True
        await self.stop()
        await self.flush(force=True, final=True)

    # ---------------------------------------------------------------- render
    def render(self, *, final: bool = False) -> str:
        state = self.state
        if final and state.final_text:
            return state.final_text
        if state.stage is Stage.COMPLETE and state.final_text:
            return state.final_text

        lines: list[str] = [_STAGE_HEADERS.get(state.stage, "Working…")]
        if state.title:
            lines.append("")
            lines.append(f"📦 {state.title}")

        if state.stage is Stage.DOWNLOADING:
            pct = percent(state.downloaded, state.total)
            lines.append("")
            if pct is None:
                lines.append(f"{progress_bar(None)} {human_size(state.downloaded)}")
            else:
                lines.append(f"{progress_bar(pct)} {pct:.0f}%")
            lines.append("")
            size_line = f"Size: {human_size(state.downloaded)}"
            if state.total:
                size_line += f" / {human_size(state.total)}"
            lines.append(size_line)
            lines.append(f"Speed: {human_speed(state.speed)}")
            eta = eta_seconds((state.total or 0) - state.downloaded, state.speed)
            if eta is not None:
                lines.append(f"ETA: {human_duration(eta)}")
        elif state.stage is Stage.PROCESSING:
            frame = SPINNER_FRAMES[int(time.monotonic()) % len(SPINNER_FRAMES)]
            lines.append("")
            lines.append(f"{frame} {progress_bar(None)}")
        elif state.stage is Stage.UPLOADING:
            frame = SPINNER_FRAMES[int(time.monotonic()) % len(SPINNER_FRAMES)]
            lines.append("")
            lines.append(f"{frame} {progress_bar(None)}")
            if state.total:
                lines.append(f"Size: {human_size(state.total)}")
        elif state.stage is Stage.QUEUED and state.note:
            lines.append("")
            lines.append(state.note)
        elif state.stage in {Stage.FAILED, Stage.CANCELLED} and state.final_text:
            return state.final_text

        if state.note and state.stage not in {Stage.QUEUED}:
            lines.append("")
            lines.append(state.note)
        return "\n".join(lines)

    def keyboard(self, *, stage: Stage | None = None) -> InlineKeyboardMarkup | None:
        current = stage or self.state.stage
        if not self.cancelable or current in {Stage.COMPLETE, Stage.FAILED, Stage.CANCELLED}:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"{CANCEL_CALLBACK}:{self.job_id}")]]
        )

    # ------------------------------------------------------------------ flush
    async def flush(self, *, force: bool = False, final: bool = False) -> None:
        if self._disabled or self.message_id is None:
            return
        if self._muted and not final:
            return
        if not force and not self._progress_enabled:
            return
        now = time.monotonic()
        if not force and now - self.state.last_update < self.config.progress_update_interval_seconds:
            return
        async with self._lock:
            text = self.render(final=final)
            if text == getattr(self, "_last_text", None):
                self.state.dirty = False
                return
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    reply_markup=self.keyboard(stage=Stage.CANCELLED if final else None),
                )
                self._last_text = text
                self.state.dirty = False
                self.state.last_update = time.monotonic()
            except BadRequest as exc:
                text_exc = str(exc).lower()
                if "not modified" in text_exc:
                    self.state.dirty = False
                    self.state.last_update = time.monotonic()
                elif "message to edit not found" in text_exc or "message can't be edited" in text_exc:
                    logger.info("cannot edit progress message for job %s, disabling updates", self.job_id)
                    self._disabled = True
                else:
                    logger.debug("progress edit failed for job %s: %s", self.job_id, exc)
            except RetryAfter as exc:
                pause = retry_after_seconds(exc.retry_after)
                logger.info("flood control while editing progress (%ss)", pause)
                await asyncio.sleep(min(10.0, pause + 0.5))
            except Forbidden:
                logger.info("user blocked the bot; disabling progress edits for job %s", self.job_id)
                self._disabled = True
            except NetworkError as exc:
                logger.debug("network error while editing progress for job %s: %s", self.job_id, exc)
            except TelegramError as exc:  # pragma: no cover - unexpected API error
                logger.debug("telegram error while editing progress for job %s: %s", self.job_id, exc)

    def disable(self) -> None:
        """Permanently stop editing (message deleted, chat blocked, ...)."""
        self._disabled = True

    def mute(self) -> None:
        """Pause progress edits (the caller is driving the message instead)."""
        self._muted = True

    def unmute(self) -> None:
        self._muted = False
        self._last_text = None
        self.state.dirty = True
