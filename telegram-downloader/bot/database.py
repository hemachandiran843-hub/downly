"""SQLite persistence layer (async).

Stores the minimum needed to run the bot: who used it, what they downloaded and
how it went.  No message contents, no personal data beyond the Telegram profile
fields the user already published to the bot.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from bot.utils.logging import get_logger

logger = get_logger(__name__)


class RowLike(Protocol):
    """What the row factories need: anything indexable by column name.

    ``aiosqlite.Row`` is not a ``Mapping`` subclass, so its shape is described
    structurally instead of by inheritance.
    """

    def __getitem__(self, key: str) -> Any: ...

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    language_code  TEXT,
    first_seen     TEXT NOT NULL,
    last_active    TEXT NOT NULL,
    download_count INTEGER NOT NULL DEFAULT 0,
    settings_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL UNIQUE,
    user_id       INTEGER NOT NULL,
    url           TEXT NOT NULL,
    domain        TEXT,
    title         TEXT,
    filename      TEXT,
    size_bytes    INTEGER,
    downloader    TEXT,
    media_kind    TEXT,
    status        TEXT NOT NULL,
    chat_id       INTEGER,
    message_id    INTEGER,
    error         TEXT,
    duration_seconds REAL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT,
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_downloads_user_created ON downloads (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads (status);
CREATE INDEX IF NOT EXISTS idx_downloads_user_status ON downloads (user_id, status);
CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users (last_active DESC);
"""

DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "upload_as": "auto",  # auto | document
    "progress_updates": True,
}


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class UserRecord:
    user_id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None
    first_seen: str = ""
    last_active: str = ""
    download_count: int = 0
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: RowLike) -> UserRecord:
        try:
            settings = json.loads(row["settings_json"] or "{}")
        except (TypeError, ValueError):
            settings = {}
        merged = {**DEFAULT_USER_SETTINGS, **settings}
        return cls(
            user_id=int(row["user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            language_code=row["language_code"],
            first_seen=row["first_seen"],
            last_active=row["last_active"],
            download_count=int(row["download_count"] or 0),
            settings=merged,
        )


@dataclass(slots=True)
class DownloadRecord:
    id: int
    job_id: str
    user_id: int
    url: str
    status: str
    domain: str | None = None
    title: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    downloader: str | None = None
    media_kind: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> DownloadRecord:
        return cls(
            id=int(row["id"]),
            job_id=row["job_id"],
            user_id=int(row["user_id"]),
            url=row["url"],
            status=row["status"],
            domain=row["domain"],
            title=row["title"],
            filename=row["filename"],
            size_bytes=row["size_bytes"],
            downloader=row["downloader"],
            media_kind=row["media_kind"],
            error=row["error"],
            duration_seconds=row["duration_seconds"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )


class Database:
    """Thin async wrapper around SQLite with all queries in one place."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self.initialize()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._conn

    async def initialize(self) -> None:
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ users
    async def upsert_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        language_code: str | None = None,
    ) -> UserRecord:
        now = utcnow_iso()
        await self.connection.execute(
            """
            INSERT INTO users (user_id, username, first_name, language_code, first_seen, last_active,
                               download_count, settings_json)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                language_code = excluded.language_code,
                last_active = excluded.last_active
            """,
            (user_id, username, first_name, language_code, now, now, json.dumps({})),
        )
        await self.connection.commit()
        user = await self.get_user(user_id)
        assert user is not None
        return user

    async def touch_user(self, user_id: int) -> None:
        await self.connection.execute(
            "UPDATE users SET last_active = ? WHERE user_id = ?", (utcnow_iso(), user_id)
        )
        await self.connection.commit()

    async def get_user(self, user_id: int) -> UserRecord | None:
        async with self.connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        return UserRecord.from_row(row) if row else None

    async def update_user_settings(self, user_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
        user = await self.get_user(user_id)
        settings = dict(user.settings) if user else dict(DEFAULT_USER_SETTINGS)
        settings.update(changes)
        await self.connection.execute(
            """
            INSERT INTO users (user_id, first_seen, last_active, settings_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json
            """,
            (user_id, utcnow_iso(), utcnow_iso(), json.dumps(settings)),
        )
        await self.connection.commit()
        return settings

    async def count_users(self) -> int:
        async with self.connection.execute("SELECT COUNT(*) AS c FROM users") as cursor:
            row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def count_active_users(self, days: int = 7) -> int:
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        async with self.connection.execute("SELECT last_active FROM users") as cursor:
            rows = await cursor.fetchall()
        total = 0
        for row in rows:
            try:
                if datetime.fromisoformat(row["last_active"]).timestamp() >= cutoff:
                    total += 1
            except (TypeError, ValueError):
                continue
        return total

    async def list_users(self, limit: int = 20, offset: int = 0) -> list[UserRecord]:
        async with self.connection.execute(
            "SELECT * FROM users ORDER BY last_active DESC LIMIT ? OFFSET ?", (limit, offset)
        ) as cursor:
            rows = await cursor.fetchall()
        return [UserRecord.from_row(row) for row in rows]

    async def iter_user_ids(self) -> list[int]:
        async with self.connection.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
        return [int(row["user_id"]) for row in rows]

    # -------------------------------------------------------------- downloads
    async def create_download(
        self,
        job_id: str,
        user_id: int,
        url: str,
        *,
        status: str = "queued",
        domain: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> DownloadRecord:
        now = utcnow_iso()
        cursor = await self.connection.execute(
            """
            INSERT INTO downloads (job_id, user_id, url, domain, status, chat_id, message_id,
                                   created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, user_id, url, domain, status, chat_id, message_id, now, now),
        )
        await self.connection.execute(
            "UPDATE users SET download_count = download_count + 1, last_active = ? WHERE user_id = ?",
            (now, user_id),
        )
        await self.connection.commit()
        row_id = cursor.lastrowid
        record = await self.get_download_by_id(int(row_id)) if row_id else None
        assert record is not None
        return record

    async def update_download(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "filename",
            "size_bytes",
            "downloader",
            "media_kind",
            "title",
            "error",
            "duration_seconds",
            "completed_at",
            "message_id",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), job_id]
        await self.connection.execute(f"UPDATE downloads SET {assignments} WHERE job_id = ?", values)
        await self.connection.commit()

    async def get_download_by_id(self, row_id: int) -> DownloadRecord | None:
        async with self.connection.execute("SELECT * FROM downloads WHERE id = ?", (row_id,)) as cursor:
            row = await cursor.fetchone()
        return DownloadRecord.from_row(row) if row else None

    async def get_download(self, job_id: str) -> DownloadRecord | None:
        async with self.connection.execute("SELECT * FROM downloads WHERE job_id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
        return DownloadRecord.from_row(row) if row else None

    async def list_downloads(
        self,
        *,
        user_id: int | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[DownloadRecord]:
        query = "SELECT * FROM downloads"
        params: list[Any] = []
        clauses: list[str] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self.connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [DownloadRecord.from_row(row) for row in rows]

    async def mark_stale_active_downloads(self, reason: str = "interrupted by restart") -> int:
        """Fail rows that were mid-flight when the process stopped."""
        now = utcnow_iso()
        cursor = await self.connection.execute(
            """
            UPDATE downloads
               SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
             WHERE status IN ('queued', 'analyzing', 'downloading', 'processing', 'uploading')
            """,
            (reason, now, now),
        )
        await self.connection.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------ stats
    async def stats(self) -> dict[str, Any]:
        async with self.connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status IN ('queued','analyzing','downloading','processing','uploading')
                         THEN 1 ELSE 0 END) AS active,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN size_bytes ELSE 0 END), 0) AS bytes
            FROM downloads
            """
        ) as cursor:
            row = await cursor.fetchone()
        async with self.connection.execute(
            """
            SELECT downloader, COUNT(*) AS c FROM downloads
             WHERE status = 'completed' AND downloader IS NOT NULL
             GROUP BY downloader
            """
        ) as cursor:
            by_downloader = {r["downloader"]: int(r["c"]) for r in await cursor.fetchall()}
        return {
            "total_downloads": int(row["total"] or 0) if row else 0,
            "completed": int(row["completed"] or 0) if row else 0,
            "failed": int(row["failed"] or 0) if row else 0,
            "cancelled": int(row["cancelled"] or 0) if row else 0,
            "active": int(row["active"] or 0) if row else 0,
            "total_bytes": int(row["bytes"] or 0) if row else 0,
            "by_downloader": by_downloader,
            "users": await self.count_users(),
        }

    async def recent_failures(self, limit: int = 5) -> list[DownloadRecord]:
        return await self.list_downloads(statuses=["failed"], limit=limit)
