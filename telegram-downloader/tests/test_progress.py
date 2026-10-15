"""Progress rendering, throttling and stage transitions."""

from __future__ import annotations

import asyncio

from bot.services.progress import CANCEL_CALLBACK, ProgressReporter, Stage
from tests.fakes import make_settings


def build_reporting(tmp_path, bot, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return settings, ProgressReporter(
        bot, chat_id=1, message_id=42, config=settings, job_id="job1", cancelable=True
    )


async def test_render_analyzing(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.ANALYZING
    assert reporter.render() == "🔍 Analyzing link…"


async def test_render_downloading_matches_spec_shape(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.DOWNLOADING
    reporter.state.title = "Example Video"
    reporter.state.downloaded = 128 * 1024 * 1024
    reporter.state.total = 210 * 1024 * 1024
    reporter.state.speed = 8.4 * 1024 * 1024
    text = reporter.render()
    assert "📥 Downloading" in text
    assert "📦 Example Video" in text
    assert "Size: 128.0 MB / 210.0 MB" in text
    assert "Speed: 8.4 MB/s" in text
    assert "ETA: 9s" in text
    assert "61%" in text


async def test_render_without_total_shows_unknown_bar(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.DOWNLOADING
    reporter.state.downloaded = 4096
    reporter.state.total = None
    text = reporter.render()
    assert "4.0 KB" in text
    assert "%" not in text.split("Speed")[0]


async def test_stage_headers(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    for stage, expected in (
        (Stage.PROCESSING, "⚙️ Processing media…"),
        (Stage.UPLOADING, "📤 Uploading to Telegram…"),
        (Stage.QUEUED, "⏳ Waiting in queue…"),
    ):
        reporter.state.stage = stage
        assert expected in reporter.render()


async def test_report_bytes_marks_dirty_and_sets_note_on_retry(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.DOWNLOADING
    reporter.report_bytes(-1, None, 0.0)
    assert reporter.state.dirty is True
    assert "retrying" in (reporter.state.note or "")
    reporter.report_bytes(100, 200, 50.0)
    assert reporter.state.downloaded == 100
    assert reporter.state.total == 200


async def test_flush_edits_once_until_interval_passes(tmp_path, bot):
    settings, reporter = build_reporting(tmp_path, bot, PROGRESS_UPDATE_INTERVAL_SECONDS=5)
    reporter.state.stage = Stage.DOWNLOADING
    reporter.state.downloaded = 10
    await reporter.flush(force=True)
    assert len(bot.edits) == 1
    # Within the interval nothing is sent.
    reporter.state.downloaded = 20
    await reporter.flush()
    assert len(bot.edits) == 1
    # After the interval it is.
    settings.progress_update_interval_seconds = 0
    await reporter.flush()
    assert len(bot.edits) == 2
    assert "20 B" in bot.edits[-1][2]


async def test_flush_skips_identical_text(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.ANALYZING
    await reporter.flush(force=True)
    await reporter.flush(force=True)
    assert len(bot.edits) == 1


async def test_keyboard_shows_cancel_button(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.DOWNLOADING
    keyboard = reporter.keyboard()
    assert keyboard is not None
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == f"{CANCEL_CALLBACK}:job1"

    reporter.state.stage = Stage.COMPLETE
    assert reporter.keyboard() is None


async def test_finish_sends_final_text_and_stops_ticker(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot, PROGRESS_UPDATE_INTERVAL_SECONDS=0.5)
    await reporter.start()
    reporter.state.stage = Stage.DOWNLOADING
    reporter.report_bytes(10, 100, 1.0)
    await asyncio.sleep(0.7)
    assert bot.edits, "ticker should have edited the message"
    await reporter.finish("✅ Download complete")
    assert bot.edits[-1][2] == "✅ Download complete"
    assert reporter._ticker is None


async def test_mute_blocks_edits_until_unmuted(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter.state.stage = Stage.ANALYZING
    reporter.mute()
    await reporter.flush(force=True)
    assert bot.edits == []
    reporter.unmute()
    await reporter.flush(force=True)
    assert len(bot.edits) == 1


async def test_progress_disabled_still_reports_final_state(tmp_path, bot):
    _, reporter = build_reporting(tmp_path, bot)
    reporter._progress_enabled = False
    reporter.state.stage = Stage.DOWNLOADING
    reporter.state.downloaded = 5
    await reporter.flush()
    assert bot.edits == []
    await reporter.finish("✅ Download complete")
    assert bot.edits[-1][2] == "✅ Download complete"


async def test_edits_disabled_when_message_id_missing(tmp_path, bot):
    settings = make_settings(tmp_path)
    reporter = ProgressReporter(bot, chat_id=1, message_id=None, config=settings, job_id="j")
    await reporter.set_stage(Stage.DOWNLOADING, force=True)
    reporter.report_bytes(10, 20, 1.0)
    await reporter.finish("done")
    assert bot.edits == []
