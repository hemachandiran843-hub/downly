"""Handler registration."""

from __future__ import annotations

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.handlers.admin import broadcast_command, cleanup_command, stats_command, users_command
from bot.handlers.callbacks import download_callback, help_callback, settings_callback
from bot.handlers.download import handle_message
from bot.handlers.start import (
    cancel_command,
    help_command,
    id_command,
    settings_command,
    start_command,
    status_command,
)

__all__ = ["register_handlers"]

#: Commands surfaced in Telegram's command menu.
BOT_COMMANDS = [
    ("start", "Welcome message and quick help"),
    ("help", "Supported links, limits and commands"),
    ("status", "Show your active downloads"),
    ("cancel", "Cancel your active downloads"),
    ("settings", "Choose how files are sent"),
    ("id", "Show your Telegram id"),
]


def register_handlers(application) -> None:
    """Attach every handler. Order matters: commands before the text catcher."""
    for command, handler in (
        ("start", start_command),
        ("help", help_command),
        ("id", id_command),
        ("status", status_command),
        ("cancel", cancel_command),
        ("settings", settings_command),
        ("stats", stats_command),
        ("users", users_command),
        ("cleanup", cleanup_command),
        ("broadcast", broadcast_command),
    ):
        application.add_handler(CommandHandler(command, handler))

    # Callback data is namespaced: dl:* download actions, set:* user settings, help.
    application.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^set:"))
    application.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    application.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help$"))

    # Ordinary messages containing links (new messages only).
    application.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message)
    )
    # Media with a caption that contains a link.
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.CAPTION & filters.Regex(r"https?://"),
            handle_message,
        )
    )
