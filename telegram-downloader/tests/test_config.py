"""Environment parsing, defaults and secret safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import Settings
from bot.exceptions import ConfigError


def build(env: dict[str, str] | None = None, **kwargs) -> Settings:
    kwargs.setdefault("require_token", False)
    return Settings.from_env(env=env or {}, load_dotenv_file=False, **kwargs)


class TestDefaults:
    def test_documented_defaults(self):
        settings = build()
        assert settings.max_file_size_mb == 2000
        assert settings.max_concurrent_downloads == 3
        assert settings.max_downloads_per_user == 2
        assert settings.download_timeout_seconds == 60
        assert settings.max_download_time_minutes == 60
        assert settings.temp_file_max_age_hours == 6
        assert settings.max_redirects == 10
        assert settings.log_level == "INFO"
        assert settings.telegram_upload_limit_mb == 50
        assert settings.max_file_size_bytes == 2000 * 1024 * 1024
        assert settings.telegram_upload_limit_bytes == 50 * 1024 * 1024
        assert settings.max_download_time_seconds == 3600.0

    def test_paths_are_resolved_relative_to_project_root(self):
        settings = build({"DATABASE_PATH": "data/custom.db"})
        assert settings.database_path.is_absolute()
        assert settings.database_path.name == "custom.db"

    def test_absolute_paths_are_kept(self, tmp_path: Path):
        settings = build({"DATABASE_PATH": str(tmp_path / "x.db")})
        assert settings.database_path == tmp_path / "x.db"


class TestParsing:
    def test_overrides(self):
        settings = build(
            {
                "MAX_FILE_SIZE_MB": "100",
                "MAX_CONCURRENT_DOWNLOADS": "5",
                "MAX_DOWNLOADS_PER_USER": "4",
                "PROGRESS_UPDATE_INTERVAL_SECONDS": "3.5",
                "CONFIRM_BEFORE_DOWNLOAD": "true",
                "KEEP_FILES": "yes",
                "ADMIN_USER_IDS": "111, 222;333",
                "ALLOW_PLAYLISTS": "1",
                "LOG_LEVEL": "debug",
            }
        )
        assert settings.max_file_size_mb == 100
        assert settings.max_concurrent_downloads == 5
        assert settings.max_downloads_per_user == 4
        assert settings.progress_update_interval_seconds == 3.5
        assert settings.cancel_confirm_before_download is True
        assert settings.keep_files is True
        assert settings.admin_user_ids == frozenset({111, 222, 333})
        assert settings.allow_playlists is True
        assert settings.log_level == "DEBUG"

    def test_blank_values_fall_back_to_defaults(self):
        settings = build({"MAX_FILE_SIZE_MB": "", "ADMIN_USER_IDS": "", "FFMPEG_LOCATION": "  "})
        assert settings.max_file_size_mb == 2000
        assert settings.admin_user_ids == frozenset()
        assert settings.ffmpeg_location is None

    @pytest.mark.parametrize(
        "env",
        [
            {"MAX_CONCURRENT_DOWNLOADS": "zero"},
            {"MAX_FILE_SIZE_MB": "-5"},
            {"MAX_DOWNLOADS_PER_USER": "0"},
            {"ADMIN_USER_IDS": "abc"},
            {"PROGRESS_UPDATE_INTERVAL_SECONDS": "fast"},
        ],
    )
    def test_invalid_values_raise_config_error(self, env):
        with pytest.raises(ConfigError):
            build(env)


class TestTokenValidation:
    def test_missing_token(self):
        settings = build()
        with pytest.raises(ConfigError) as excinfo:
            settings.validate_token()
        assert "BOT_TOKEN" in str(excinfo.value)

    def test_malformed_token(self):
        with pytest.raises(ConfigError):
            build({"BOT_TOKEN": "notatoken"}).validate_token()
        with pytest.raises(ConfigError):
            build({"BOT_TOKEN": "123456:"}).validate_token()

    def test_valid_token(self):
        build({"BOT_TOKEN": "123456789:AAExampleTokenValueForTests"}).validate_token()

    def test_require_token_flag(self):
        with pytest.raises(ConfigError):
            build(require_token=True)
        build(require_token=False)


class TestReporting:
    def test_summary_never_leaks_the_token(self):
        secret = "123456789:AAExampleTokenValueForTests"
        settings = build({"BOT_TOKEN": secret, "ADMIN_USER_IDS": "42"})
        summary = settings.summary()
        assert secret not in str(summary)
        assert summary["bot_token_configured"] is True
        assert summary["admin_count"] == 1
        assert "bot_token" not in summary

    def test_is_admin(self):
        settings = build({"ADMIN_USER_IDS": "42"})
        assert settings.is_admin(42)
        assert not settings.is_admin(43)
        assert not settings.is_admin(None)


class TestDirectories:
    def test_ensure_directories(self, tmp_path: Path):
        settings = build(
            {
                "DATA_DIR": str(tmp_path / "data"),
                "TEMP_DIR": str(tmp_path / "temp"),
                "DOWNLOADS_DIR": str(tmp_path / "downloads"),
                "DATABASE_PATH": str(tmp_path / "data" / "bot.db"),
            }
        )
        settings.ensure_directories()
        assert settings.data_dir.is_dir()
        assert settings.temp_dir.is_dir()
        assert settings.downloads_dir.is_dir()
