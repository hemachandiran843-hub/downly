"""Exception hierarchy.

Every exception that can be shown to a Telegram user carries a short
``user_message``; technical detail stays in the exception message and the logs.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for all expected, handled application errors."""

    user_message = "❌ Something went wrong. Please try again later."

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        super().__init__(message or self.user_message)
        self.detail = message
        if user_message is not None:
            self.user_message = user_message


class ConfigError(BotError):
    """Invalid or missing configuration."""

    user_message = "❌ The bot is misconfigured. Please contact the administrator."


class InvalidUrlError(BotError):
    """The user supplied something that is not a usable URL."""

    user_message = "❌ That does not look like a valid http(s) link."


class BlockedUrlError(BotError):
    """The URL targets a private/internal/blocked address (SSRF protection)."""

    user_message = "🚫 That link points to a private or internal address and is not allowed."


class UnsupportedUrlError(BotError):
    """Neither the direct downloader nor yt-dlp can handle this URL."""

    user_message = "❌ I could not find downloadable media at that link."


class FileTooLargeError(BotError):
    """The remote file exceeds the configured download limit."""

    user_message = "❌ File is too large."


class TelegramUploadLimitError(BotError):
    """The file is downloadable but cannot be uploaded to Telegram."""

    user_message = "❌ The download completed, but the file is too large to upload to Telegram."


class InsufficientDiskSpaceError(BotError):
    """Not enough free space on the server to store the download."""

    user_message = "❌ Not enough disk space to download this file."


class DownloadTimeoutError(BotError):
    """A stage of the download exceeded its configured time budget."""

    user_message = "⏱ The download timed out."


class DownloadCancelled(BotError):  # noqa: N818 - "cancelled" is not a failure, it is a state
    """The user cancelled the job (also used to abort yt-dlp download hooks)."""

    user_message = "❌ Download cancelled."


class HttpDownloadError(BotError):
    """The remote server returned an error or the transfer failed."""

    user_message = "❌ The server refused the download."


class YtDlpError(BotError):
    """yt-dlp could not extract or download the URL."""

    user_message = "❌ The site could not be downloaded."


class TelegramUploadError(BotError):
    """Telegram rejected the upload."""

    user_message = "❌ Telegram refused the upload."


class QueueLimitError(BotError):
    """The user already has the maximum number of concurrent downloads."""

    user_message = "⏳ You already have the maximum number of downloads running."


class AdminOnlyError(BotError):
    """A non-admin tried to use an admin command."""

    user_message = "🚫 This command is restricted to the bot administrator."
