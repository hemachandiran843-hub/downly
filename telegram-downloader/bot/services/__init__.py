"""Application services: progress, metadata, upload, cleanup, job manager."""

from bot.services.cleanup import CleanupService
from bot.services.manager import ACTIVE_STATUSES, DownloadJob, DownloadManager, summarize_job
from bot.services.metadata import completion_message, preview_text, result_caption, safe_domain
from bot.services.progress import ProgressReporter, Stage
from bot.services.telegram_upload import TelegramUploader

__all__ = [
    "ACTIVE_STATUSES",
    "CleanupService",
    "DownloadJob",
    "DownloadManager",
    "ProgressReporter",
    "Stage",
    "TelegramUploader",
    "completion_message",
    "preview_text",
    "result_caption",
    "safe_domain",
    "summarize_job",
]
