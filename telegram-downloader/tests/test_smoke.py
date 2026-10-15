"""Startup path: config check, application assembly, error handler, CLI."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from bot.config import Settings
from bot.main import build_application, build_services, error_handler, run_bot, smoke_check
from bot.utils.formatting import human_size
from tests.fakes import FakeBot, make_settings


@pytest.fixture(autouse=True)
def no_telegram_api(monkeypatch):
    """Keep the lifecycle tests offline (set_my_commands is a real API call)."""

    async def noop(self, *args, **kwargs):
        return True

    monkeypatch.setattr("telegram.ext.ExtBot.set_my_commands", noop)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


async def test_smoke_check_verifies_the_environment(tmp_settings):
    report = await smoke_check(tmp_settings)
    assert report["directories"] == {"data": True, "downloads": True, "temp": True}
    assert report["temp_writable"] is True
    assert report["token_configured"] is True
    assert report["http_session"] is True
    assert report["free_disk_mb"] > 0
    assert "database" in report
    assert Path(report["database"]["path"]).exists()


def test_build_application_registers_handlers(tmp_settings):
    application = build_application(tmp_settings)
    handlers = application.handlers[0]
    command_names: set[str] = set()
    for handler in handlers:
        commands = getattr(handler, "commands", None)
        if commands:
            command_names.update(commands)
    assert {
        "start",
        "help",
        "status",
        "cancel",
        "settings",
        "stats",
        "users",
        "cleanup",
        "broadcast",
        "id",
    } <= command_names
    assert application.error_handlers, "a global error handler must be installed"
    assert application.bot_data["services"].config is tmp_settings


def test_build_services_wires_everything(tmp_settings):
    services = build_services(tmp_settings)
    assert services.manager is not None
    assert services.router.direct is not None
    assert services.uploads is not None


def test_run_bot_without_token_fails_clearly(tmp_path):
    settings = make_settings(tmp_path, BOT_TOKEN="")
    with pytest.raises(Exception) as excinfo:
        run_bot(settings)
    assert "BOT_TOKEN" in str(excinfo.value)


def test_cli_check_succeeds(tmp_path, monkeypatch, capsys):
    from run import main

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "cli.db"))
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")

    assert main(["--check"]) == 0
    output = capsys.readouterr().out
    assert "Self-test" in output
    assert "directories" in output


def test_cli_check_without_token_still_reports(tmp_path, monkeypatch, capsys):
    from run import main

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "cli.db"))
    monkeypatch.setenv("BOT_TOKEN", "")

    assert main(["--check"]) == 0
    assert "BOT_TOKEN is not set" in capsys.readouterr().out


def test_cli_rejects_invalid_configuration(monkeypatch, capsys):
    from run import main

    monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "lots")
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
    assert main([]) == 2
    assert "Configuration error" in capsys.readouterr().out


def test_cli_requires_token_to_start(monkeypatch, capsys):
    from run import main

    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    assert main([]) == 2
    assert "BOT_TOKEN" in capsys.readouterr().out


async def test_error_handler_logs_and_informs_the_user():
    from telegram import Update

    bot = FakeBot()
    message = build_message(bot, "boom")
    update = Update(update_id=1, message=message)

    class Context:
        error = RuntimeError("boom")

    await error_handler(update, Context())
    assert [text for _, text in bot.messages] == [main_module_generic_error()]


async def test_error_handler_survives_non_update_objects():
    class Context:
        error = RuntimeError("boom")

    await error_handler(object(), Context())


def test_registered_handlers_load_without_import_cycles():
    from bot import handlers

    assert handlers.BOT_COMMANDS
    assert callable(handlers.register_handlers)


def test_formatting_helper_is_consistent():
    assert human_size(1024) == "1.0 KB"


def test_settings_summary_is_loggable(tmp_settings):
    summary = tmp_settings.summary()
    assert summary["bot_token_configured"] is True
    assert "123456" not in str(summary)


async def test_post_init_starts_cleanup_and_shutdown_stops_it(tmp_settings, monkeypatch):
    """Exercise the real post_init/post_shutdown lifecycle without Telegram."""
    from bot import main as main_module

    application = build_application(tmp_settings)
    await main_module.post_init(application)
    services = application.bot_data["services"]
    try:
        assert services.cleanup_task is not None
        assert not services.cleanup_task.done()
        health = await services.db.stats()
        assert health["users"] == 0
    finally:
        await main_module.post_shutdown(application)
    assert services.cleanup_task.cancelled() or services.cleanup_task.done()
    assert services.db._conn is None


async def test_post_init_marks_interrupted_downloads(tmp_settings, monkeypatch):
    from bot import main as main_module
    from bot.database import Database

    db = Database(tmp_settings.database_path)
    await db.connect()
    await db.upsert_user(1)
    await db.create_download("half-done", 1, "https://example.com/x")
    await db.close()

    application = build_application(tmp_settings)
    await main_module.post_init(application)
    services = application.bot_data["services"]
    try:
        record = await services.db.get_download("half-done")
        assert record.status == "failed"
        assert record.error == "interrupted by restart"
    finally:
        await main_module.post_shutdown(application)


def main_module_generic_error() -> str:
    from bot.main import GENERIC_ERROR

    return GENERIC_ERROR


def build_message(bot, text: str):
    """A real PTB Message bound to a fake bot, so reply_text works offline."""
    from datetime import datetime

    from telegram import Chat, Message, User

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, first_name="T", is_bot=False),
        text=text,
    )
    message.set_bot(bot)
    return message


class HandlerHarness:
    """Small harness that runs the real message handler against fake services."""

    def __init__(self, services, bot):
        self.services = services
        self.bot = bot

        class Application:
            bot_data = {"services": services}

        class Context:
            pass

        self.context = Context()
        self.context.bot = bot
        self.context.application = Application()

    async def dispatch(self, text: str):
        from telegram import Update

        from bot.handlers.download import handle_message

        update = Update(update_id=1, message=build_message(self.bot, text))
        await handle_message(update, self.context)
        return [message for _, message in self.bot.messages]


async def test_handler_reports_missing_links(tmp_settings):
    services = build_services(tmp_settings)
    await services.db.connect()
    try:
        bot = FakeBot()
        replies = await HandlerHarness(services, bot).dispatch("hello there")
        assert replies and "could not find a link" in replies[0]
    finally:
        await services.db.close()
        await services.router.direct.close()


async def test_handler_rejects_private_urls(tmp_settings):
    services = build_services(tmp_settings)
    await services.db.connect()
    try:
        bot = FakeBot()
        replies = await HandlerHarness(services, bot).dispatch("try http://127.0.0.1:8000/admin please")
        assert any("skipped" in reply for reply in replies)
        assert services.manager.active_jobs() == []
    finally:
        await services.db.close()
        await services.router.direct.close()


async def test_handler_submits_valid_urls_with_user_settings(tmp_settings):
    services = build_services(tmp_settings)
    await services.db.connect()
    await services.db.upsert_user(1)
    await services.db.update_user_settings(1, {"upload_as": "document", "progress_updates": False})
    try:
        submitted: list[dict] = []

        class StubJob:
            task = None
            job_id = "stub"

        class StubManager:
            def active_jobs(self):
                return []

            def jobs_of(self, user_id, active_only=True):
                return []

            async def submit(self, bot, **kwargs):
                submitted.append(kwargs)
                return StubJob()

        services.manager = StubManager()
        bot = FakeBot()
        harness = HandlerHarness(services, bot)
        await harness.dispatch("download this: https://example.com/file.zip thanks")

        assert len(submitted) == 1
        assert submitted[0]["url"] == "https://example.com/file.zip"
        assert submitted[0]["user_id"] == 1
        assert submitted[0]["as_document"] is True
        assert submitted[0]["progress_updates"] is False
    finally:
        await services.db.close()
        await services.router.direct.close()
