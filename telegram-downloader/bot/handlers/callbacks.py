"""Inline-button callbacks: cancel, confirm, settings and the help button."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.context import services
from bot.handlers.start import help_text, settings_keyboard, settings_text
from bot.services.progress import Stage
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger

logger = get_logger(__name__)

CANCEL_PREFIX = "dl:cancel"
CONFIRM_PREFIX = "dl:go"


async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles ``dl:cancel:<job_id>`` and ``dl:go:<job_id>``."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    ctx = services(context)
    user = query.from_user
    parts = query.data.split(":")  # dl:<action>:<job_id>
    if len(parts) != 3 or parts[0] != "dl":
        await query.answer("This button is no longer valid.", show_alert=False)
        return
    _, action, job_id = parts

    job = ctx.manager.get(job_id)
    if job is None:
        await query.answer("This download already finished.", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - message may be gone
            logger.debug("could not clear keyboard for %s", job_id)
        return
    if job.user_id != user.id and not ctx.config.is_admin(user.id):
        await query.answer("That download belongs to another user.", show_alert=True)
        return

    if action == "cancel":
        await ctx.manager.cancel(job_id, user_id=job.user_id)
        logger.info("job %s cancelled by user %s", job_id, user.id)
        await query.answer("Cancelling…", show_alert=False)
        if job.reporter is not None and job.reporter.state.stage is Stage.QUEUED:
            await job.reporter.set_stage(Stage.CANCELLED, force=True)
        return

    if action == "go":
        if ctx.manager.confirm(job_id):
            await query.answer("Starting download…", show_alert=False)
        else:
            await query.answer("That preview expired.", show_alert=True)
        return

    await query.answer()


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles ``set:<setting>:<value>`` toggles from /settings."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    ctx = services(context)
    user = query.from_user
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    _, setting, value = parts

    if setting == "upload_as" and value in {"auto", "document"}:
        values = await ctx.db.update_user_settings(user.id, {"upload_as": value})
        await query.answer("Files will be sent as documents." if value == "document" else "Automatic format.")
    elif setting == "progress" and value in {"on", "off"}:
        values = await ctx.db.update_user_settings(user.id, {"progress_updates": value == "on"})
        await query.answer("Progress updates on." if value == "on" else "Progress updates off.")
    else:
        await query.answer("Unknown setting.")
        return

    try:
        await query.edit_message_text(settings_text(values), reply_markup=settings_keyboard(values))
    except Exception:  # noqa: BLE001 - "message is not modified" and friends
        logger.debug("could not refresh settings message")


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    ctx = services(context)
    await query.answer()
    config = ctx.config
    try:
        await query.edit_message_text(
            help_text(config.max_file_size_mb, config.telegram_upload_limit_mb, config.max_downloads_per_user),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        await query.message.reply_text(  # type: ignore[union-attr]
            f"Max file size: {human_size(config.max_file_size_bytes)}"
        )
