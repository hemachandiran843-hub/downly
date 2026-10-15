"""Temporary file cleanup: job directories, stale sweeps, periodic task."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from bot.services.cleanup import CleanupService, _tree_size
from tests.fakes import make_settings


def make_stale(path: Path, *, age_hours: float = 10, payload: bytes = b"x" * 128) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    file = path / "file.bin"
    file.write_bytes(payload)
    timestamp = time.time() - age_hours * 3600
    os.utime(file, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))
    return file


async def test_remove_path_handles_files_and_directories(tmp_path):
    service = CleanupService(make_settings(tmp_path))
    file = tmp_path / "a.bin"
    file.write_bytes(b"x")
    assert service.remove_path(file) is True
    assert not file.exists()

    directory = tmp_path / "dir" / "nested"
    directory.mkdir(parents=True)
    (directory / "f").write_bytes(b"y")
    assert service.remove_path(tmp_path / "dir") is True
    assert not (tmp_path / "dir").exists()

    # Missing paths and None are not errors.
    assert service.remove_path(None) is False
    assert service.remove_path(tmp_path / "nope") is False


async def test_cleanup_job_dir_removes_tree(tmp_path):
    settings = make_settings(tmp_path)
    service = CleanupService(settings)
    job_dir = settings.temp_dir / "job123"
    job_dir.mkdir(parents=True)
    (job_dir / "video.mp4").write_bytes(b"data")
    service.cleanup_job_dir(job_dir)
    assert not job_dir.exists()


async def test_purge_old_removes_stale_and_keeps_fresh(tmp_path):
    settings = make_settings(tmp_path)
    service = CleanupService(settings)
    settings.ensure_directories()

    make_stale(settings.temp_dir / "old-job")
    make_stale(settings.downloads_dir / "old-download-1", payload=b"y" * 300)
    fresh = settings.temp_dir / "fresh-job"
    fresh.mkdir()
    (fresh / "current.part").write_bytes(b"z")

    result = await service.purge_old()
    assert result["entries"] == 2
    assert result["bytes"] >= 428
    assert not (settings.temp_dir / "old-job").exists()
    assert fresh.exists()


async def test_purge_old_skips_active_jobs(tmp_path):
    settings = make_settings(tmp_path)
    service = CleanupService(settings)
    settings.ensure_directories()
    make_stale(settings.temp_dir / "active-job")
    make_stale(settings.temp_dir / "abandoned-job")

    result = await service.purge_old({"active-job"})
    assert result["entries"] == 1
    assert (settings.temp_dir / "active-job").exists()
    assert not (settings.temp_dir / "abandoned-job").exists()


async def test_purge_respects_configured_age(tmp_path):
    settings = make_settings(tmp_path, TEMP_FILE_MAX_AGE_HOURS=48)
    service = CleanupService(settings)
    settings.ensure_directories()
    make_stale(settings.temp_dir / "six-hours-old", age_hours=6)

    result = await service.purge_old()
    assert result["entries"] == 0
    assert (settings.temp_dir / "six-hours-old").exists()


async def test_stats_reports_sizes(tmp_path):
    settings = make_settings(tmp_path)
    service = CleanupService(settings)
    settings.ensure_directories()
    (settings.temp_dir / "a.bin").write_bytes(b"0" * 1000)
    (settings.downloads_dir / "user1").mkdir()
    (settings.downloads_dir / "user1" / "b.bin").write_bytes(b"0" * 2000)

    stats = service.stats()
    assert stats["temp_bytes"] == 1000
    assert stats["downloads_bytes"] == 2000
    assert _tree_size(tmp_path / "does-not-exist") == 0


async def test_periodic_loop_stops_on_event(tmp_path):
    settings = make_settings(tmp_path)
    service = CleanupService(settings)
    settings.ensure_directories()
    make_stale(settings.temp_dir / "abandoned")

    stop = asyncio.Event()
    calls: list[int] = []

    def provider():
        calls.append(1)
        return set()

    task = asyncio.create_task(service.run_periodic(stop, provider, interval_seconds=0.05))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert calls
    assert not (settings.temp_dir / "abandoned").exists()
