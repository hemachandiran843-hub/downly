"""Uploading finished downloads back to Telegram.

Chooses the most appropriate media type, falls back to a document when Telegram
rejects it, and cancels cleanly.  The temporary file is never left behind: the
caller always cleans its job directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import InputFile
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from bot.config import Settings
from bot.downloader.types import DownloadResult, MediaKind
from bot.exceptions import DownloadCancelled, TelegramUploadError
from bot.services.metadata import result_caption
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger
from bot.utils.retry import retry_after_seconds

logger = get_logger(__name__)

PHOTO_MAX_BYTES = 10 * 1024 * 1024
THUMBNAIL_MAX_BYTES = 200 * 1024
VIDEO_EXTENSIONS = {"mp4", "mkv", "mov", "webm", "m4v", "avi"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"}
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ANIMATION_EXTENSIONS = {"gif", "mp4"}


class TelegramUploader:
    """Sends a downloaded file with the best matching Telegram method."""

    def __init__(self, config: Settings) -> None:
        self.config = config

    # ------------------------------------------------------------------ checks
    def check_uploadable(self, result: DownloadResult) -> None:
        limit = self.config.telegram_upload_limit_bytes
        if result.size_bytes > limit:
            raise TelegramUploadError(
                f"{result.filename} is {result.size_bytes} bytes > upload limit {limit}",
                user_message=(
                    "❌ The download completed, but the file is too large to upload to Telegram.\n\n"
                    f"File size: {human_size(result.size_bytes)}\n"
                    f"Telegram upload limit configured for this bot: {human_size(limit)}"
                ),
            )

    # ---------------------------------------------------------------- delivery
    async def deliver(
        self,
        bot,
        chat_id: int,
        result: DownloadResult,
        *,
        as_document: bool = False,
        reply_to_message_id: int | None = None,
        cancel_event: asyncio.Event | None = None,
        extra_caption: str | None = None,
    ) -> str:
        """Upload ``result`` and return the Telegram method that worked."""
        self.check_uploadable(result)
        path = Path(result.path)
        if not path.exists() or path.stat().st_size == 0:
            raise TelegramUploadError(
                f"missing or empty file {path}",
                user_message="❌ The downloaded file disappeared before it could be sent.",
            )
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("cancelled before upload")

        attempts = self._plan(result, as_document=as_document)
        last_error: Exception | None = None
        for method in attempts:
            try:
                await self._await_or_cancel(
                    self._dispatch(
                        bot,
                        chat_id,
                        result,
                        method,
                        reply_to_message_id=reply_to_message_id,
                        extra_caption=extra_caption,
                    ),
                    cancel_event,
                )
                logger.info(
                    "upload finished chat=%s file=%s bytes=%s method=%s",
                    chat_id,
                    result.filename,
                    result.size_bytes,
                    method,
                )
                return method
            except DownloadCancelled:
                raise
            except (BadRequest, TelegramError, NetworkError) as exc:
                last_error = exc
                logger.info("upload as %s failed (%s); trying next method", method, exc)
                continue

        raise self._translate(last_error, result)

    # ----------------------------------------------------------------- internals
    def _plan(self, result: DownloadResult, *, as_document: bool) -> list[str]:
        """Ordered fallbacks for uploading this file."""
        if as_document:
            return ["document"]

        extension = result.filename.rsplit(".", 1)[-1].lower() if "." in result.filename else ""
        methods: list[str] = []
        if result.kind is MediaKind.VIDEO and extension in VIDEO_EXTENSIONS:
            methods.append("video")
        elif result.kind is MediaKind.AUDIO and extension in AUDIO_EXTENSIONS:
            methods.append("audio")
        elif result.kind is MediaKind.IMAGE and extension in PHOTO_EXTENSIONS and result.size_bytes <= PHOTO_MAX_BYTES:
            methods.append("photo")
        elif result.kind is MediaKind.IMAGE and extension == "gif":
            methods.append("animation")
        methods.append("document")
        return methods

    async def _dispatch(
        self,
        bot,
        chat_id: int,
        result: DownloadResult,
        method: str,
        *,
        reply_to_message_id: int | None,
        extra_caption: str | None,
    ):
        caption = result_caption(result, extra=extra_caption)
        base_kwargs: dict[str, object] = {"chat_id": chat_id, "caption": caption}
        if reply_to_message_id is not None:
            base_kwargs["reply_to_message_id"] = reply_to_message_id
        file = InputFile(str(result.path), filename=result.filename)

        if method == "video":
            thumbnail = self._thumbnail(result)
            if thumbnail is not None:
                base_kwargs["thumbnail"] = InputFile(str(thumbnail), filename=Path(thumbnail).name)
            return await bot.send_video(
                video=file,
                duration=int(result.duration) if result.duration else None,
                width=result.width,
                height=result.height,
                supports_streaming=True,
                **base_kwargs,
            )
        if method == "audio":
            return await bot.send_audio(
                audio=file,
                duration=int(result.duration) if result.duration else None,
                performer=result.artist,
                title=result.title,
                **base_kwargs,
            )
        if method == "photo":
            return await bot.send_photo(photo=file, **base_kwargs)
        if method == "animation":
            return await bot.send_animation(animation=file, **base_kwargs)
        return await bot.send_document(document=file, **base_kwargs)

    def _thumbnail(self, result: DownloadResult) -> str | None:
        if not result.thumbnail:
            return None
        path = Path(result.thumbnail)
        try:
            if path.exists() and 0 < path.stat().st_size <= THUMBNAIL_MAX_BYTES:
                return str(path)
        except OSError:  # pragma: no cover - defensive
            return None
        return None

    @staticmethod
    async def _await_or_cancel(coro, cancel_event: asyncio.Event | None):
        """Run ``coro`` but abort if the job is cancelled mid-upload."""
        if cancel_event is None:
            return await coro
        upload = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({upload, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if upload in done:
                return upload.result()
            upload.cancel()
            try:
                await upload
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                logger.debug("upload task cancelled")
            raise DownloadCancelled("cancelled during upload")
        finally:
            waiter.cancel()
            if not upload.done():  # pragma: no cover - defensive
                upload.cancel()

    def _translate(self, error: Exception | None, result: DownloadResult) -> TelegramUploadError:
        if error is None:  # pragma: no cover - defensive
            return TelegramUploadError("upload failed for unknown reason")
        text = str(error).lower()
        if "too big" in text or "file is too large" in text or "entity too large" in text:
            user_message = (
                "❌ Telegram refused the upload because the file is too large.\n\n"
                f"File size: {human_size(result.size_bytes)}\n"
                f"Telegram upload limit configured for this bot: "
                f"{human_size(self.config.telegram_upload_limit_bytes)}"
            )
        elif "file must be" in text or "invalid" in text:
            user_message = (
                "❌ Telegram could not process that file format, and sending it as a document "
                "failed too.\nThe file may be corrupt or use an unsupported codec."
            )
        elif "timeout" in text or "timed out" in text:
            user_message = "⏱ The upload to Telegram timed out. Please try again."
        else:
            user_message = "❌ Telegram refused the upload. Please try again later."
        return TelegramUploadError(f"upload failed: {error}", user_message=user_message)


async def send_with_retry(func, *args, attempts: int = 2, **kwargs):
    """Retry a Telegram API call on transient network errors / flood control."""
    delay = 1.5
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as exc:
            last = exc
            await asyncio.sleep(min(20.0, retry_after_seconds(exc.retry_after) + 0.5))
        except NetworkError as exc:
            last = exc
            if attempt >= attempts:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise last if last else TelegramUploadError("telegram call failed")
