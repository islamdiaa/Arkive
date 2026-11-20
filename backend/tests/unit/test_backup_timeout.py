"""Backup runtime limits use persisted settings and keep cancellation available."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.services.backup_engine import BackupEngine
from app.utils.subprocess_runner import CommandResult


@pytest.mark.asyncio
@pytest.mark.parametrize("value,seconds", [(None, 3600), ("0", None), ("1440", 86400), ("-1", 3600), ("invalid", 3600)])
async def test_backup_uses_configured_runtime_limit(tmp_path, monkeypatch, value, seconds):
    config = SimpleNamespace(db_path=tmp_path / "settings.db")
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        if value is not None:
            await db.execute("INSERT INTO settings VALUES ('backup_timeout_minutes', ?)", (value,))
        await db.commit()
    engine = BackupEngine(config)
    engine._get_password = AsyncMock(return_value="test-password")
    engine._get_bandwidth_limit = AsyncMock(return_value="")
    run = AsyncMock(return_value=CommandResult(0, "{}", "", 0, "restic"))
    monkeypatch.setattr("app.services.backup_engine.run_command", run)
    await engine.backup({"type": "local", "config": {"path": "/data"}}, ["/source"], hostname="test-server")
    assert run.await_args.kwargs["timeout"] == seconds


@pytest.mark.asyncio
async def test_timeout_explains_runtime_limit_and_where_to_change_it(monkeypatch, tmp_path):
    engine = BackupEngine(SimpleNamespace(db_path=tmp_path / "settings.db"))
    engine._get_password = AsyncMock(return_value="test-password")
    engine._get_bandwidth_limit = AsyncMock(return_value="")
    engine._get_backup_timeout = AsyncMock(return_value=86400)
    run = AsyncMock(return_value=CommandResult(-1, "", "Command timed out after 86400s", 86400, "restic"))
    monkeypatch.setattr("app.services.backup_engine.run_command", run)
    result = await engine.backup({"type": "local", "config": {"path": "/data"}}, ["/source"], hostname="test-server")
    assert result["status"] == "failed"
    assert "1440 minutes" in result["error"]
    assert "Settings > General" in result["error"]
    assert "even while data is being transferred" in result["error"]
    assert run.await_count == 1
