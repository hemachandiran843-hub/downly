"""SQLite persistence: users, downloads, settings, statistics, recovery."""

from __future__ import annotations

from pathlib import Path

from bot.database import DEFAULT_USER_SETTINGS, Database


async def test_initialization_creates_schema_and_indexes(tmp_path: Path):
    db = Database(tmp_path / "data" / "bot.db")
    await db.connect()
    try:
        async with db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ) as cursor:
            indexes = {row["name"] for row in await cursor.fetchall()}
        assert "idx_downloads_user_created" in indexes
        assert "idx_downloads_status" in indexes
        assert "idx_users_last_active" in indexes
        stats = await db.stats()
        assert stats["users"] == 0
        assert stats["total_downloads"] == 0
    finally:
        await db.close()


async def test_user_upsert_is_idempotent(database: Database):
    first = await database.upsert_user(1, "alice", "Alice", "en")
    assert first.user_id == 1
    assert first.settings == DEFAULT_USER_SETTINGS

    again = await database.upsert_user(1, "alice2", "Alice B", "de")
    assert again.first_seen == first.first_seen
    assert again.username == "alice2"
    assert await database.count_users() == 1


async def test_user_settings_roundtrip(database: Database):
    await database.upsert_user(7)
    settings = await database.update_user_settings(7, {"upload_as": "document"})
    assert settings["upload_as"] == "document"
    assert settings["progress_updates"] is True

    settings = await database.update_user_settings(7, {"progress_updates": False})
    assert settings["upload_as"] == "document"
    stored = await database.get_user(7)
    assert stored is not None
    assert stored.settings["progress_updates"] is False

    # Settings for an unknown user are created on the fly.
    created = await database.update_user_settings(99, {"upload_as": "document"})
    assert created["upload_as"] == "document"
    assert await database.count_users() == 2


async def test_download_lifecycle(database: Database):
    await database.upsert_user(5)
    record = await database.create_download("job1", 5, "https://example.com/a.mp4", domain="example.com")
    assert record.status == "queued"
    assert record.id > 0

    await database.update_download(
        "job1", status="completed", filename="a.mp4", size_bytes=1024, downloader="direct", media_kind="video"
    )
    stored = await database.get_download("job1")
    assert stored is not None
    assert stored.status == "completed"
    assert stored.size_bytes == 1024
    assert stored.downloader == "direct"
    assert stored.filename == "a.mp4"

    user = await database.get_user(5)
    assert user is not None
    assert user.download_count == 1

    stats = await database.stats()
    assert stats["completed"] == 1
    assert stats["total_bytes"] == 1024
    assert stats["by_downloader"] == {"direct": 1}


async def test_update_download_ignores_unknown_columns(database: Database):
    await database.upsert_user(6)
    await database.create_download("job2", 6, "https://example.com/b.zip")
    # Must not raise (and must not build broken SQL).
    await database.update_download("job2", status="failed", nonsense="ignored", error="boom")
    stored = await database.get_download("job2")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error == "boom"


async def test_job_id_is_unique(database: Database):
    import aiosqlite
    import pytest

    await database.upsert_user(8)
    await database.create_download("dup", 8, "https://example.com/1")
    with pytest.raises(aiosqlite.IntegrityError):
        await database.create_download("dup", 8, "https://example.com/2")


async def test_list_downloads_filters(database: Database):
    await database.upsert_user(9)
    await database.upsert_user(10)
    await database.create_download("done", 9, "https://example.com/1")
    await database.create_download("failed", 9, "https://example.com/2")
    await database.create_download("other", 10, "https://example.com/3")
    await database.update_download("done", status="completed")
    await database.update_download("failed", status="failed")

    user_rows = await database.list_downloads(user_id=9)
    assert len(user_rows) == 2
    failed_only = await database.list_downloads(statuses=["failed"])
    assert [row.job_id for row in failed_only] == ["failed"]


async def test_stale_downloads_are_failed_on_restart(database: Database):
    await database.upsert_user(11)
    for index, status in enumerate(["queued", "analyzing", "downloading", "processing", "uploading", "completed"]):
        job_id = f"job{index}"
        await database.create_download(job_id, 11, f"https://example.com/{index}")
        await database.update_download(job_id, status=status)

    marked = await database.mark_stale_active_downloads()
    assert marked == 5
    assert (await database.get_download("job5")).status == "completed"
    assert (await database.get_download("job3")).status == "failed"
    assert (await database.get_download("job3")).error == "interrupted by restart"


async def test_stats_and_users_overview(database: Database):
    for user_id in range(1, 4):
        await database.upsert_user(user_id, f"user{user_id}")
    await database.create_download("x1", 1, "https://example.com/1")
    await database.update_download("x1", status="completed", size_bytes=2048)
    await database.create_download("x2", 2, "https://example.com/2")
    await database.update_download("x2", status="cancelled")

    stats = await database.stats()
    assert stats["users"] == 3
    assert stats["completed"] == 1
    assert stats["cancelled"] == 1
    assert stats["total_bytes"] == 2048
    assert await database.count_active_users(7) == 3
    assert len(await database.list_users()) == 3
    assert sorted(await database.iter_user_ids()) == [1, 2, 3]
    assert await database.recent_failures() == []


async def test_unknown_download_returns_none(database: Database):
    assert await database.get_download("missing") is None
    assert await database.get_download_by_id(999) is None


async def test_context_manager(tmp_path: Path):
    async with Database(tmp_path / "ctx.db") as db:
        assert await db.count_users() == 0
