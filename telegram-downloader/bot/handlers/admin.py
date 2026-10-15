"""Administrator commands. Every handler checks ADMIN_USER_IDS first."""

from __future__ import annotations

import asyncio
from functools import wraps

from telegram import Update
from telegram.error import Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from bot.context import Services, services
from bot.exceptions import AdminOnlyError
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger
from bot.utils.retry import retry_after_seconds

logger = get_logger(__name__)

BROADCAST_DELAY_SECONDS = 0.05


def admin_only(func):
    """Reject non-admins before the handler body runs."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        ctx: Services = services(context)
        user = update.effective_user
        if user is None or not ctx.config.is_admin(user.id):
            logger.warning("non-admin %s tried %s", getattr(user, "id", None), func.__name__)
            message = update.effective_message
            if message is not None:
                await message.reply_text(AdminOnlyError.user_message)
            return None
        return await func(update, context, *args, **kwargs)

    return wrapper


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    message = update.effective_message
    if message is None:
        return

    stats = await ctx.db.stats()
    cleanup = ctx.cleanup.stats()
    active = ctx.manager.active_jobs()
    from bot.downloader.ytdlp import ffmpeg_available

    by_downloader = ", ".join(f"{k}: {v}" for k, v in stats["by_downloader"].items()) or "—"
    lines = [
        "📊 Bot statistics",
        "",
        f"👥 Users: {stats['users']}",
        f"🔄 Active downloads now: {len(active)} (limit {ctx.config.max_concurrent_downloads})",
        f"✅ Completed: {stats['completed']}",
        f"❌ Failed: {stats['failed']}",
        f"🚫 Cancelled: {stats['cancelled']}",
        f"📦 Total downloaded: {human_size(stats['total_bytes'])}",
        f"🧭 Downloads by method: {by_downloader}",
        "",
        f"💾 Temp: {human_size(cleanup['temp_bytes'])} · Downloads: {human_size(cleanup['downloads_bytes'])}",
        f"⏱ Uptime: {int(ctx.uptime_seconds() // 3600)}h {int(ctx.uptime_seconds() % 3600 // 60)}m",
        f"🎬 FFmpeg available: {'yes' if ffmpeg_available(ctx.config) else 'no'}",
    ]
    if active:
        lines.append("")
        lines.append("Current jobs:")
        for job in active[:10]:
            lines.append(f"• {job.job_id[:8]} · user {job.user_id} · {job.status} · {job.elapsed():.0f}s")
    await message.reply_text("\n".join(lines))


@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    message = update.effective_message
    if message is None:
        return
    users = await ctx.db.list_users(limit=25)
    total = await ctx.db.count_users()
    active_week = await ctx.db.count_active_users(7)
    lines = [f"👥 Users: {total} total · {active_week} active in the last 7 days", ""]
    for user in users:
        username = f"@{user.username}" if user.username else (user.first_name or "—")
        lines.append(f"• {user.user_id} · {username[:24]} · {user.download_count} downloads")
    if not users:
        lines.append("No users yet.")
    await message.reply_text("\n".join(lines), disable_web_page_preview=True)


@admin_only
async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    message = update.effective_message
    if message is None:
        return
    result = await ctx.cleanup.purge_old(ctx.manager.active_job_ids())
    lines = [
        "🧹 Cleanup finished",
        "",
        f"Removed entries: {result['entries']}",
        f"Freed space: {human_size(result['bytes'])}",
        f"Active downloads kept: {ctx.manager.active_count()}",
    ]
    await message.reply_text("\n".join(lines))
    logger.info("manual cleanup by admin removed %s entries", result["entries"])


@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/broadcast <text>`` or reply to a message with ``/broadcast``."""
    ctx = services(context)
    message = update.effective_message
    if message is None:
        return

    payload = ""
    if message.reply_to_message is not None:
        reply = message.reply_to_message
        payload = reply.text or reply.caption or ""
    else:
        payload = (message.text or "").partition(" ")[2].strip()

    if not payload:
        await message.reply_text(
            "Usage: `/broadcast <message>`\nOr reply to a message with /broadcast."
        )
        return

    user_ids = await ctx.db.iter_user_ids()
    await message.reply_text(f"📣 Broadcasting to {len(user_ids)} user(s)…")
    sent = 0
    failed = 0
    blocked = 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=payload, disable_web_page_preview=True)
            sent += 1
        except Forbidden:
            blocked += 1
        except RetryAfter as exc:
            await asyncio.sleep(min(30.0, retry_after_seconds(exc.retry_after) + 1))
            try:
                await context.bot.send_message(chat_id=user_id, text=payload, disable_web_page_preview=True)
                sent += 1
            except TelegramError:
                failed += 1
        except (NetworkError, TelegramError):
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    logger.info("broadcast finished sent=%s blocked=%s failed=%s", sent, blocked, failed)
    await message.reply_text(f"📣 Broadcast done.\n\n✅ Sent: {sent}\n🚫 Blocked: {blocked}\n❌ Failed: {failed}")
