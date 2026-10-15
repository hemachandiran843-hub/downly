"""Basic commands: /start, /help, /status, /cancel, /settings and /id."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.context import services
from bot.database import DEFAULT_USER_SETTINGS
from bot.services.manager import summarize_job
from bot.services.progress import Stage
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger

logger = get_logger(__name__)

WELCOME_TEXT = """👋 Welcome!

Send me a downloadable link and I'll try to download it for you.

I support:
• Direct file links
• Video/audio links
• Many sites supported by yt-dlp
• Images and documents

Just send a URL."""


def help_text(max_size_mb: int, telegram_limit_mb: int, per_user: int) -> str:
    return f"""ℹ️ Help

🔗 What you can send
• Direct files: .zip .pdf .mp4 .mp3 .jpg …
• Video/audio pages supported by yt-dlp (YouTube and many others)
• Any http/https link — I inspect it and pick the best method
• Several links in one message are processed one after another

📦 Limits
• Max download size: {max_size_mb} MB
• Telegram upload limit for this bot: {telegram_limit_mb} MB
• Concurrent downloads per user: {per_user}

🧭 Commands
/start – welcome message
/help – this text
/status – show your active downloads
/cancel – cancel your active downloads
/settings – choose how files are sent
/id – show your Telegram id (needed for admin setup)

⚠️ Common errors
• "not a downloadable file" – the link is a web page, send the direct file link
• "private or requires a login" – I only download public content
• "too large" – exceeds the limits above
• "rate limited" – the remote site is throttling, try again later

🔒 I never bypass logins, paywalls or DRM, and I never run downloaded files."""


def settings_text(settings_map: dict) -> str:
    upload_as = settings_map.get("upload_as", "auto")
    progress = settings_map.get("progress_updates", True)
    return (
        "⚙️ Settings\n\n"
        f"• Upload format: {'as document (original file)' if upload_as == 'document' else 'automatic'}\n"
        f"• Progress updates: {'on' if progress else 'off'}\n\n"
        "Tap a button to change it."
    )


def settings_keyboard(settings_map: dict) -> InlineKeyboardMarkup:
    upload_as = settings_map.get("upload_as", "auto")
    progress = settings_map.get("progress_updates", True)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📄 Send as document ✓" if upload_as == "document" else "📄 Send as document",
                    callback_data="set:upload_as:document" if upload_as != "document" else "set:upload_as:auto",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔔 Progress updates ✓" if progress else "🔕 Progress updates",
                    callback_data="set:progress:on" if not progress else "set:progress:off",
                )
            ],
        ]
    )


async def _register_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    await services(context).db.upsert_user(user.id, user.username, user.first_name, user.language_code)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update, context)
    message = update.effective_message
    if message is None:
        return
    config = services(context).config
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("ℹ️ Help", callback_data="help")]]
    )
    await message.reply_text(
        WELCOME_TEXT + f"\n\n📦 Max file size: {human_size(config.max_file_size_bytes)}",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update, context)
    message = update.effective_message
    if message is None:
        return
    config = services(context).config
    await message.reply_text(
        help_text(config.max_file_size_mb, config.telegram_upload_limit_mb, config.max_downloads_per_user),
        disable_web_page_preview=True,
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update, context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    config = services(context).config
    role = "administrator" if config.is_admin(user.id) else "user"
    await message.reply_text(f"🆔 Your Telegram id: `{user.id}`\nRole: {role}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    await _register_user(update, context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    jobs = ctx.manager.jobs_of(user.id)
    if not jobs:
        recent = await ctx.db.list_downloads(user_id=user.id, limit=5)
        lines = ["📊 No active downloads right now."]
        if recent:
            lines.append("")
            lines.append("Last downloads:")
            for record in recent:
                icon = {
                    "completed": "✅",
                    "failed": "❌",
                    "cancelled": "🚫",
                }.get(record.status, "⏳")
                name = record.filename or record.domain or record.url
                lines.append(f"{icon} {name[:48]} · {record.status}")
        await message.reply_text("\n".join(lines))
        return

    lines = [f"📥 Active downloads ({len(jobs)}/{ctx.config.max_downloads_per_user}):", ""]
    lines.extend(summarize_job(job) for job in jobs)
    lines.append("")
    lines.append("Use /cancel to stop them.")
    await message.reply_text("\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    await _register_user(update, context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    cancelled = await ctx.manager.cancel_all(user.id)
    if cancelled:
        logger.info("user %s cancelled %s download(s)", user.id, cancelled)
        await message.reply_text(f"❌ Cancelled {cancelled} download(s).")
    else:
        await message.reply_text("ℹ️ You have no active downloads.")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    await _register_user(update, context)
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    record = await ctx.db.get_user(user.id)
    values = dict(record.settings) if record else dict(DEFAULT_USER_SETTINGS)
    await message.reply_text(settings_text(values), reply_markup=settings_keyboard(values))


async def status_after_stage(job) -> Stage:  # pragma: no cover - tiny helper for tests
    return job.reporter.state.stage if job.reporter else Stage.QUEUED
