"""Message handler: extract URLs from ordinary messages and start downloads."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from bot.context import Services, services
from bot.database import DEFAULT_USER_SETTINGS
from bot.exceptions import BotError
from bot.services.metadata import safe_domain
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger
from bot.utils.security import extract_url_candidates, validate_url

logger = get_logger(__name__)

NO_URL_HINT = (
    "🔗 I could not find a link in that message.\n\n"
    "Send me an http/https URL (for example https://example.com/file.mp4) "
    "and I will download it.\nUse /help for details."
)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = services(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    await ctx.db.upsert_user(user.id, user.username, user.first_name, user.language_code)

    text = message.text or message.caption or ""
    limit = ctx.config.max_urls_per_message
    candidates = extract_url_candidates(text, limit=limit + 1)
    if not candidates:
        await message.reply_text(NO_URL_HINT, disable_web_page_preview=True)
        return

    overflow = len(candidates) > limit
    candidates = candidates[:limit]

    record = await ctx.db.get_user(user.id)
    user_settings = dict(record.settings) if record else dict(DEFAULT_USER_SETTINGS)
    as_document = user_settings.get("upload_as") == "document"
    progress_updates = bool(user_settings.get("progress_updates", True))

    valid: list[str] = []
    problems: list[str] = []
    for candidate in candidates:
        try:
            valid.append(validate_url(candidate))
        except BotError as exc:
            problems.append(f"• {candidate[:60]} — {exc.user_message.lstrip('❌🚫 ')}")
            logger.info("rejected url from user %s: %s (%s)", user.id, candidate[:120], exc.detail)
        except Exception as exc:  # noqa: BLE001 - never trust URL parsing
            problems.append(f"• {candidate[:60]} — invalid link")
            logger.info("url validation error for %s: %s", candidate[:120], exc)

    if problems:
        await message.reply_text("⚠️ Some links were skipped:\n" + "\n".join(problems[:5]))
    if overflow:
        await message.reply_text(
            f"ℹ️ Only the first {limit} links of that message were processed."
        )
    if not valid:
        return

    if len(valid) == 1:
        await _submit_one(
            ctx,
            update,
            context.bot,
            valid[0],
            as_document=as_document,
            progress_updates=progress_updates,
        )
        return

    # Several links: process them one after another so the per-user limit is honoured.
    for url in valid:
        try:
            job = await ctx.manager.submit(
                context.bot,
                user_id=user.id,
                chat_id=message.chat_id,
                url=url,
                reply_to_message_id=message.message_id,
                as_document=as_document,
                progress_updates=progress_updates,
            )
        except BotError as exc:
            await message.reply_text(exc.user_message)
            break
        except Exception:  # noqa: BLE001 - keep going, the job itself reports failures
            logger.exception("could not queue %s", url)
            continue
        if job.task is not None:
            await asyncio.wait({job.task})


async def _submit_one(
    ctx: Services,
    update: Update,
    bot,
    url: str,
    *,
    as_document: bool,
    progress_updates: bool,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    logger.info("user %s requested %s", user.id, url)
    try:
        await ctx.manager.submit(
            bot,
            user_id=user.id,
            chat_id=message.chat_id,
            url=url,
            reply_to_message_id=message.message_id,
            as_document=as_document,
            progress_updates=progress_updates,
        )
    except BotError as exc:
        await message.reply_text(exc.user_message)
    except Exception:  # noqa: BLE001 - the bot must survive handler bugs
        logger.exception("failed to queue %s", url)
        await message.reply_text(
            "❌ Could not start the download.\n"
            f"Link: {safe_domain(url)}\n"
            f"Maximum allowed size: {human_size(ctx.config.max_file_size_bytes)}"
        )
