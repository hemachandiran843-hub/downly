"""Shared service container passed to handlers via ``Application.bot_data``."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from bot.config import Settings
from bot.database import Database
from bot.downloader.router import DownloaderRouter
from bot.services.cleanup import CleanupService
from bot.services.telegram_upload import TelegramUploader

BOT_DATA_KEY = "services"


@dataclass
class Services:
    config: Settings
    db: Database
    router: DownloaderRouter
    cleanup: CleanupService
    uploads: TelegramUploader
    manager: Any = None  # DownloadManager (set after construction)
    started_at: float = field(default_factory=time.monotonic)
    cleanup_stop: Any = None  # asyncio.Event
    cleanup_task: Any = None

    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at


def set_services(application, services: Services) -> None:
    application.bot_data[BOT_DATA_KEY] = services


def services(context) -> Services:
    """Fetch the container from a PTB callback/message context."""
    return context.application.bot_data[BOT_DATA_KEY]
