"""Application assembly and entry points."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from bot.config import Settings
from bot.context import BOT_DATA_KEY, Services, set_services
from bot.database import Database
from bot.downloader.direct import DirectDownloader
from bot.downloader.router import DownloaderRouter
from bot.downloader.ytdlp import YtDlpDownloader, ffmpeg_available
from bot.handlers import BOT_COMMANDS, register_handlers
from bot.services.cleanup import CleanupService
from bot.services.manager import DownloadManager
from bot.services.telegram_upload import TelegramUploader
from bot.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)

GENERIC_ERROR = "❌ Something went wrong while handling your request. Please try again."


def build_services(settings: Settings) -> Services:
    db = Database(settings.database_path)
    direct = DirectDownloader(settings)
    ytdlp = YtDlpDownloader(settings)
    router = DownloaderRouter(settings, direct, ytdlp)
    uploads = TelegramUploader(settings)
    cleanup = CleanupService(settings)
    manager = DownloadManager(settings, db, router, uploads, cleanup)
    return Services(config=settings, db=db, router=router, cleanup=cleanup, uploads=uploads, manager=manager)


async def post_init(application: Application) -> None:
    """Prepare directories, database, HTTP session and background tasks."""
    services: Services = application.bot_data[BOT_DATA_KEY]
    settings: Settings = services.config

    settings.ensure_directories()
    await services.db.connect()
    interrupted = await services.db.mark_stale_active_downloads()
    if interrupted:
        logger.warning("marked %s download(s) as interrupted by the previous shutdown", interrupted)
    await services.router.direct.start()

    services.cleanup_stop = asyncio.Event()
    services.cleanup_task = asyncio.create_task(
        services.cleanup.run_periodic(services.cleanup_stop, services.manager.active_job_ids),
        name="periodic-cleanup",
    )

    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001 - a cosmetic API call must never block startup
        logger.warning("could not set bot commands: %s", exc)

    try:
        username = application.bot.username
    except RuntimeError:  # bot not initialized (offline self-test)
        username = None
    logger.info("bot started as @%s", username or "unknown")
    logger.info("configuration: %s", settings.summary())
    logger.info("ffmpeg available: %s", ffmpeg_available(settings))


async def post_shutdown(application: Application) -> None:
    """Stop background tasks and close every resource."""
    services: Services | None = application.bot_data.get(BOT_DATA_KEY)
    if services is None:
        return
    logger.info("shutting down: cancelling active downloads")
    if services.cleanup_stop is not None:
        services.cleanup_stop.set()
    if services.cleanup_task is not None:
        services.cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await services.cleanup_task
    try:
        await services.manager.shutdown()
    except Exception:  # noqa: BLE001
        logger.exception("error while shutting down download manager")
    try:
        await services.router.direct.close()
    except Exception:  # noqa: BLE001
        logger.debug("error while closing http session", exc_info=True)
    try:
        await services.db.close()
    except Exception:  # noqa: BLE001
        logger.exception("error while closing the database")
    logger.info("shutdown complete")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global safety net: log everything, never leak stack traces to users."""
    error = context.error
    logger.exception("unhandled exception while processing update", exc_info=error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(GENERIC_ERROR)
        except Exception:  # noqa: BLE001 - nothing else to do
            logger.debug("could not send the generic error message", exc_info=True)


def build_application(settings: Settings) -> Application:
    """Create the PTB application with all handlers wired up."""
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .connection_pool_size(16)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    set_services(application, build_services(settings))
    register_handlers(application)
    application.add_error_handler(error_handler)
    return application


def run_bot(settings: Settings) -> None:
    """Blocking entry point used by ``run.py``."""
    # Format check only; the real validation happens during getMe().
    settings.validate_token()
    application = build_application(settings)
    logger.info("starting polling loop")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


async def smoke_check(settings: Settings) -> dict[str, Any]:
    """Offline self-test: directories, database, sessions, imports, disk space.

    Runs without a Telegram token and without touching the network, which makes
    it usable in CI and as a Docker health check.
    """
    from bot.downloader.utils import available_disk_bytes

    report: dict[str, Any] = {}
    settings.ensure_directories()
    report["directories"] = {
        "data": settings.data_dir.exists(),
        "downloads": settings.downloads_dir.exists(),
        "temp": settings.temp_dir.exists(),
    }

    db = Database(settings.database_path)
    await db.connect()
    try:
        stats = await db.stats()
        report["database"] = {"path": str(settings.database_path), "stats": stats}
    finally:
        await db.close()

    direct = DirectDownloader(settings)
    await direct.start()
    report["http_session"] = direct.session is not None
    await direct.close()

    probe_file = settings.temp_dir / ".write-test"
    probe_file.write_text("ok", encoding="utf-8")
    probe_file.unlink()
    report["temp_writable"] = True
    report["free_disk_mb"] = available_disk_bytes(settings.temp_dir) // (1024 * 1024)
    report["ffmpeg"] = ffmpeg_available(settings)
    report["token_configured"] = bool(settings.bot_token)
    report["admins"] = sorted(settings.admin_user_ids)
    return report


def configure_logging(settings: Settings) -> None:
    setup_logging(settings.log_level, settings.log_format)
