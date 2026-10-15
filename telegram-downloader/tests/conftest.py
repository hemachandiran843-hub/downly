"""Shared fixtures. The suite never touches the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakeBot, make_settings


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    """Keep every test offline.

    URL validation still runs for real (so SSRF rules are exercised), but DNS
    lookups are replaced by a deterministic public answer so no test depends on
    a resolver or on a third-party site.
    """
    import bot.downloader.direct as direct_module
    from bot.utils import security

    async def passthrough(url: str) -> str:
        return security.validate_url(url)

    monkeypatch.setattr(direct_module, "ensure_public_url", passthrough)
    monkeypatch.setattr(security, "resolve_host", lambda host, port=None: ["93.184.216.34"])
    yield


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(tmp_path)


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
async def database(tmp_path: Path):
    from bot.database import Database

    db = Database(tmp_path / "data" / "bot.db")
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
