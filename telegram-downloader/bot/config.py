"""Configuration.

All tunables live in the environment (see ``.env.example``).  Nothing in this
module ever logs or exposes secrets: :meth:`Settings.summary` is explicitly
built to be safe to print.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from bot.exceptions import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Hosts that must never be contacted, independent of DNS resolution.
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)


def _env_int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw.strip()))
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _env_float(env: Mapping[str, str], key: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_ids(env: Mapping[str, str], key: str) -> frozenset[int]:
    raw = env.get(key, "") or ""
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        try:
            ids.add(int(candidate))
        except ValueError as exc:
            raise ConfigError(f"{key} contains a non-numeric id: {candidate!r}") from exc
    return frozenset(ids)


def _env_path(env: Mapping[str, str], key: str, default: str) -> Path:
    raw = (env.get(key) or default).strip() or default
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(slots=True)
class Settings:
    """Runtime settings."""

    bot_token: str = ""
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)

    database_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "bot.db")
    downloads_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "downloads")
    temp_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "temp")
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    max_file_size_mb: int = 2000
    telegram_upload_limit_mb: int = 50
    max_concurrent_downloads: int = 3
    max_downloads_per_user: int = 2
    max_urls_per_message: int = 3

    download_timeout_seconds: int = 60
    max_download_time_minutes: int = 60
    max_redirects: int = 10
    probe_timeout_seconds: int = 20
    metadata_timeout_seconds: int = 30
    max_retries: int = 3
    temp_file_max_age_hours: int = 6
    cleanup_interval_minutes: int = 30
    min_free_disk_mb: int = 512

    progress_update_interval_seconds: float = 2.5
    cancel_confirm_before_download: bool = False
    keep_files: bool = False

    log_level: str = "INFO"
    log_format: str = "text"
    user_agent: str = "Mozilla/5.0 (compatible; TelegramDownloaderBot/1.0)"
    ffmpeg_location: str | None = None
    allow_playlists: bool = False
    write_subtitles: bool = False
    subtitle_langs: str = "en"

    # Derived limits used by the downloaders (bytes / seconds).
    max_file_size_bytes: int = field(default=0, init=False)
    telegram_upload_limit_bytes: int = field(default=0, init=False)
    max_download_time_seconds: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.bot_token = (self.bot_token or "").strip()
        self.log_level = (self.log_level or "INFO").upper()
        self.database_path = Path(self.database_path)
        self.downloads_dir = Path(self.downloads_dir)
        self.temp_dir = Path(self.temp_dir)
        self.data_dir = Path(self.data_dir)
        if self.ffmpeg_location is not None and not str(self.ffmpeg_location).strip():
            self.ffmpeg_location = None
        self.recalculate()

    def recalculate(self) -> None:
        self.max_file_size_bytes = self.max_file_size_mb * 1024 * 1024
        self.telegram_upload_limit_bytes = self.telegram_upload_limit_mb * 1024 * 1024
        self.max_download_time_seconds = self.max_download_time_minutes * 60.0

    # ------------------------------------------------------------------ load
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        load_dotenv_file: bool = True,
        require_token: bool = True,
    ) -> Settings:
        if env is None:
            if load_dotenv_file:
                load_dotenv(PROJECT_ROOT / ".env", override=False)
            env = os.environ

        settings = cls(
            bot_token=env.get("BOT_TOKEN", ""),
            admin_user_ids=_env_ids(env, "ADMIN_USER_IDS"),
            database_path=_env_path(env, "DATABASE_PATH", "data/bot.db"),
            downloads_dir=_env_path(env, "DOWNLOADS_DIR", "downloads"),
            temp_dir=_env_path(env, "TEMP_DIR", "temp"),
            data_dir=_env_path(env, "DATA_DIR", "data"),
            max_file_size_mb=_env_int(env, "MAX_FILE_SIZE_MB", 2000, minimum=1),
            telegram_upload_limit_mb=_env_int(env, "TELEGRAM_UPLOAD_LIMIT_MB", 50, minimum=1),
            max_concurrent_downloads=_env_int(env, "MAX_CONCURRENT_DOWNLOADS", 3, minimum=1),
            max_downloads_per_user=_env_int(env, "MAX_DOWNLOADS_PER_USER", 2, minimum=1),
            max_urls_per_message=_env_int(env, "MAX_URLS_PER_MESSAGE", 3, minimum=1),
            download_timeout_seconds=_env_int(env, "DOWNLOAD_TIMEOUT_SECONDS", 60, minimum=1),
            max_download_time_minutes=_env_int(env, "MAX_DOWNLOAD_TIME_MINUTES", 60, minimum=1),
            max_redirects=_env_int(env, "MAX_REDIRECTS", 10, minimum=0),
            probe_timeout_seconds=_env_int(env, "PROBE_TIMEOUT_SECONDS", 20, minimum=1),
            metadata_timeout_seconds=_env_int(env, "METADATA_TIMEOUT_SECONDS", 30, minimum=1),
            max_retries=_env_int(env, "MAX_RETRIES", 3, minimum=0),
            temp_file_max_age_hours=_env_int(env, "TEMP_FILE_MAX_AGE_HOURS", 6, minimum=1),
            cleanup_interval_minutes=_env_int(env, "CLEANUP_INTERVAL_MINUTES", 30, minimum=1),
            min_free_disk_mb=_env_int(env, "MIN_FREE_DISK_MB", 512, minimum=0),
            progress_update_interval_seconds=_env_float(
                env, "PROGRESS_UPDATE_INTERVAL_SECONDS", 2.5, minimum=0.5
            ),
            cancel_confirm_before_download=_env_bool(env, "CONFIRM_BEFORE_DOWNLOAD", False),
            keep_files=_env_bool(env, "KEEP_FILES", False),
            log_level=env.get("LOG_LEVEL", "INFO"),
            log_format=env.get("LOG_FORMAT", "text"),
            user_agent=env.get("USER_AGENT", "Mozilla/5.0 (compatible; TelegramDownloaderBot/1.0)"),
            ffmpeg_location=(env.get("FFMPEG_LOCATION") or "").strip() or None,
            allow_playlists=_env_bool(env, "ALLOW_PLAYLISTS", False),
            write_subtitles=_env_bool(env, "WRITE_SUBTITLES", False),
            subtitle_langs=env.get("SUBTITLE_LANGS", "en"),
        )
        if require_token:
            settings.validate_token()
        return settings

    # -------------------------------------------------------------- validate
    def validate_token(self) -> None:
        if not self.bot_token:
            raise ConfigError(
                "BOT_TOKEN is not set. Copy .env.example to .env and paste the token "
                "you received from @BotFather."
            )
        if ":" not in self.bot_token or not self.bot_token.split(":", 1)[1].strip():
            raise ConfigError(
                "BOT_TOKEN has an unexpected format. It should look like '123456789:AA...'."
            )

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.downloads_dir, self.temp_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- reporting
    def summary(self) -> dict[str, Any]:
        """Configuration overview that is safe to log (no secrets)."""
        secret_keys = {"bot_token"}
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in secret_keys or f.name.endswith("_bytes") or f.name.endswith("_seconds"):
                continue
            value = getattr(self, f.name)
            if isinstance(value, frozenset):
                out[f.name] = sorted(value)
            elif isinstance(value, Path):
                out[f.name] = str(value)
            else:
                out[f.name] = value
        out["bot_token_configured"] = bool(self.bot_token)
        out["admin_count"] = len(self.admin_user_ids)
        return out

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.admin_user_ids
