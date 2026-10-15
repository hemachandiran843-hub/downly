"""Logging: secret redaction and encoding safety."""

from __future__ import annotations

import io
import json
import logging

from bot.utils.logging import (
    RedactionFilter,
    configure_stdio_encoding,
    redact,
    setup_logging,
)

TOKEN = "123456789:AAExampleTokenValueForTests1234567890"


class TestRedact:
    def test_telegram_token(self):
        assert TOKEN not in redact(f"token is {TOKEN}")

    def test_authorization_header(self):
        assert "secretvalue" not in redact("Authorization: Bearer secretvalue")

    def test_cookie_and_api_key(self):
        assert "abc123" not in redact("api_key=abc123")
        assert "session=xyz" not in redact("Cookie: session=xyz")

    def test_credentials_in_url(self):
        cleaned = redact("https://user:hunter2@example.com/file.zip")
        assert "hunter2" not in cleaned
        assert "example.com" in cleaned

    def test_plain_text_is_untouched(self):
        assert redact("nothing to hide here") == "nothing to hide here"
        assert redact("") == ""


class TestLogPipeline:
    def test_exception_traceback_is_scrubbed(self, capsys):
        setup_logging("DEBUG", "text")
        try:
            raise RuntimeError(f"rejected token {TOKEN}")
        except RuntimeError:
            logging.getLogger("tests").exception("startup failed")
        output = capsys.readouterr().out + capsys.readouterr().err
        assert TOKEN not in output
        assert "***redacted***" in output

    def test_json_format_scrubs_and_parses(self, capsys):
        setup_logging("INFO", "json")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("tests").error("failed with %s", TOKEN, exc_info=True)
        line = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["level"] == "ERROR"
        assert TOKEN not in json.dumps(payload)

    def test_filter_rewrites_the_record(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, TOKEN, (), None)
        assert RedactionFilter().filter(record) is True
        assert TOKEN not in record.getMessage()

    def test_setup_is_idempotent(self):
        setup_logging("WARNING", "text")
        setup_logging("INFO", "text")
        assert logging.getLogger().level == logging.INFO
        assert len(logging.getLogger().handlers) == 1


def test_configure_stdio_encoding_is_safe_on_exotic_streams(monkeypatch):
    class StreamWithoutReconfigure:
        def write(self, text):  # pragma: no cover - not used
            return len(text)

        def flush(self):  # pragma: no cover
            return None

    import sys

    monkeypatch.setattr(sys, "stdout", StreamWithoutReconfigure())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    configure_stdio_encoding()  # must not raise


def test_configure_stdio_encoding_fixes_a_cp1252_stream(monkeypatch):
    """A cp1252 console must not crash on the emoji used in user messages."""
    import sys

    import pytest

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    with pytest.raises(UnicodeEncodeError):
        stream.write("✅ Download complete")

    configure_stdio_encoding()
    stream.write("✅ Download complete 🚫")
    stream.flush()
