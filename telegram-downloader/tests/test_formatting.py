"""Size/duration/ETA formatting and progress calculations."""

from __future__ import annotations

import pytest

from bot.utils.formatting import (
    eta_seconds,
    human_duration,
    human_size,
    human_speed,
    local_time,
    percent,
    progress_bar,
    truncate,
)


class TestHumanSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024 * 1024, "1.0 MB"),
            (210 * 1024 * 1024, "210.0 MB"),
            (2 * 1024**3, "2.0 GB"),
        ],
    )
    def test_values(self, value, expected):
        assert human_size(value) == expected

    def test_unknown(self):
        assert human_size(None) == "unknown"
        assert human_size(-1) == "unknown"
        assert human_size("nope") == "unknown"

    def test_speed(self):
        assert human_speed(8.4 * 1024 * 1024) == "8.4 MB/s"
        assert human_speed(0) == "—"
        assert human_speed(None) == "—"


class TestHumanDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0s"), (45, "45s"), (60, "1m 00s"), (125, "2m 05s"), (3725, "1h 02m 05s")],
    )
    def test_values(self, seconds, expected):
        assert human_duration(seconds) == expected

    def test_unknown(self):
        assert human_duration(None) == "—"


class TestProgressBar:
    def test_full_and_empty(self):
        assert progress_bar(100, width=10) == "█" * 10
        assert progress_bar(0, width=10) == "░" * 10

    def test_halfway(self):
        bar = progress_bar(50, width=20)
        assert len(bar) == 20
        assert bar.count("█") == 10
        assert bar.count("░") == 10

    def test_unknown_is_empty(self):
        assert progress_bar(None, width=8) == "░" * 8

    def test_clamped(self):
        assert progress_bar(150, width=4) == "█" * 4
        assert progress_bar(-10, width=4) == "░" * 4

    def test_matches_spec_example(self):
        # 61% of 20 cells -> 12 filled
        assert progress_bar(61) == "█" * 12 + "░" * 8


class TestCalculations:
    def test_percent(self):
        assert percent(128, 210) == pytest.approx(60.95, abs=0.01)
        assert percent(0, 100) == 0
        assert percent(1, 0) is None
        assert percent(None, 100) is None
        assert percent(200, 100) == 100.0

    def test_eta(self):
        assert eta_seconds(1024 * 1024, 1024 * 1024) == pytest.approx(1.0)
        assert eta_seconds(0, 100) is None
        assert eta_seconds(100, 0) is None
        assert eta_seconds(None, 100) is None

    def test_truncate(self):
        assert truncate("short", 20) == "short"
        assert truncate("a" * 50, 10) == "a" * 9 + "…"
        assert truncate(None, 10) == ""
        assert truncate("  many   spaces  ", 30) == "many spaces"

    def test_local_time_handles_strings_and_garbage(self):
        assert local_time(None) == "—"
        assert local_time("2024-01-01T10:00:00+00:00")
        assert local_time("not a date") == "not a date"
