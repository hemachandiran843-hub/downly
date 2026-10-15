#!/usr/bin/env python3
"""Entry point.

    python run.py            # start the bot (needs BOT_TOKEN)
    python run.py --check    # offline self-test: config, dirs, database, deps
    python run.py --version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from telegram.error import Conflict, InvalidToken, TelegramError

from bot import __version__
from bot.config import Settings
from bot.exceptions import ConfigError
from bot.main import configure_logging, run_bot, smoke_check
from bot.utils.logging import configure_stdio_encoding, get_logger, redact

logger = get_logger("run")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run.py", description="Telegram media downloader bot")
    parser.add_argument("--check", action="store_true", help="run an offline self-test and exit")
    parser.add_argument("--version", action="version", version=f"telegram-downloader {__version__}")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="override LOG_LEVEL from the environment",
    )
    return parser.parse_args(argv)


async def _check(settings: Settings) -> int:
    report = await smoke_check(settings)
    print(json.dumps(report, indent=2, default=str))
    problems: list[str] = []
    if not report["token_configured"]:
        problems.append("BOT_TOKEN is not set (required to run the bot, not for this check)")
    if not all(report["directories"].values()):
        problems.append("could not create the data/downloads/temp directories")
    if not report["temp_writable"]:
        problems.append("temp directory is not writable")
    if not report["ffmpeg"]:
        problems.append(
            "FFmpeg was not found: merging video+audio for some sites will fail "
            "(install ffmpeg or set FFMPEG_LOCATION)"
        )
    if problems:
        print("\n⚠️  Warnings:")
        for problem in problems:
            print(f"  - {problem}")
        print("\n✅ Self-test finished with warnings.")
        return 0
    print("\n✅ Self-test passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_stdio_encoding()
    args = parse_args(argv)

    if args.check:
        try:
            settings = Settings.from_env(require_token=False)
        except ConfigError as exc:
            print(f"❌ Configuration error: {exc}")
            return 2
        configure_logging(settings)
        return asyncio.run(_check(settings))

    try:
        settings = Settings.from_env(require_token=True)
    except ConfigError as exc:
        print(f"❌ Configuration error: {exc}")
        print("\nℹ️  Copy .env.example to .env and set BOT_TOKEN (get one from @BotFather).")
        return 2

    if args.log_level:
        settings.log_level = args.log_level
    configure_logging(settings)
    settings.ensure_directories()
    try:
        run_bot(settings)
    except InvalidToken:
        # NB: PTB's message contains the token itself, so it is never printed raw.
        print("❌ Telegram rejected BOT_TOKEN. Check that .env contains the exact value from @BotFather.")
        return 3
    except Conflict:
        print(
            "❌ Another instance of this bot is already running with the same token.\n"
            "Stop the other process (or use a different token) and try again."
        )
        return 4
    except TelegramError as exc:
        print(f"❌ Telegram error while starting: {type(exc).__name__}: {redact(str(exc))}")
        return 4
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nStopped.")
        sys.exit(0)
