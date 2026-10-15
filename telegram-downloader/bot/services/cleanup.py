"""Temporary file management.

Every job gets its own directory under ``temp/``.  Directories are removed as
soon as the job finishes, and a periodic sweep removes anything abandoned (for
example after a crash) based on ``TEMP_FILE_MAX_AGE_HOURS``.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from bot.config import Settings
from bot.utils.formatting import human_size
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class CleanupService:
    def __init__(self, config: Settings) -> None:
        self.config = config

    # ------------------------------------------------------------- primitives
    def remove_path(self, path: str | Path | None) -> bool:
        """Delete a file or directory tree. Never raises."""
        if path is None:
            return False
        target = Path(path)
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=False)
            elif target.exists():
                target.unlink()
            else:
                return False
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("cleanup failed for %s: %s", target, exc)
            return False

    def cleanup_job_dir(self, path: str | Path | None) -> None:
        if self.remove_path(path):
            logger.debug("removed job directory %s", path)

    def scan(self, active_job_ids: set[str] | None = None) -> dict[str, int]:
        """Remove stale temp entries (blocking; run in a worker thread)."""
        active = active_job_ids or set()
        cutoff = time.time() - self.config.temp_file_max_age_hours * 3600
        removed_entries = 0
        freed_bytes = 0

        for base in (self.config.temp_dir, self.config.downloads_dir):
            if not base.exists():
                continue
            for entry in list(base.iterdir()):
                try:
                    if entry.is_dir() and entry.name in active:
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                # A directory's mtime changes whenever its contents change, so
                # an untouched directory older than the cutoff is abandoned.
                if stat.st_mtime > cutoff:
                    continue
                size = _tree_size(entry) if entry.is_dir() else stat.st_size
                if self.remove_path(entry):
                    removed_entries += 1
                    freed_bytes += size
        return {"entries": removed_entries, "bytes": freed_bytes}

    async def purge_old(self, active_job_ids: set[str] | None = None) -> dict[str, int]:
        result = await asyncio.to_thread(self.scan, active_job_ids or set())
        if result["entries"]:
            logger.info(
                "cleanup removed %s stale temp entries (%s)",
                result["entries"],
                human_size(result["bytes"]),
            )
        return result

    async def run_periodic(
        self,
        stop_event: asyncio.Event,
        active_job_ids_provider,
        interval_seconds: int | None = None,
    ) -> None:
        """Sweep abandoned files periodically until ``stop_event`` is set."""
        interval = interval_seconds or self.config.cleanup_interval_minutes * 60
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop requested
            except TimeoutError:
                pass
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            try:
                active = set(active_job_ids_provider() or set())
                await self.purge_old(active)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # noqa: BLE001 - maintenance must never crash the bot
                logger.exception("periodic cleanup failed")

    def stats(self) -> dict[str, int]:
        return {
            "temp_bytes": _tree_size(self.config.temp_dir),
            "downloads_bytes": _tree_size(self.config.downloads_dir),
        }


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total
