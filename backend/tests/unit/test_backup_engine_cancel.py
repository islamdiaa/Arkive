"""Cancellation coverage for BackupEngine backup uploads."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_backup_returns_cancelled_when_subprocess_is_cancelled(monkeypatch):
    from app.services.backup_engine import BackupEngine
    from app.utils.subprocess_runner import CommandResult

    config = MagicMock()
    config.db_path = "/tmp/ignored.db"
    config.rclone_config = "/tmp/rclone.conf"

    engine = BackupEngine(config)
    engine._get_password = AsyncMock(return_value="secret")
    engine._get_bandwidth_limit = AsyncMock(return_value="")
    engine._get_server_name = AsyncMock(return_value="tower")

    async def fake_run_command(*args, **kwargs):
        return CommandResult(
            returncode=-2,
            stdout="",
            stderr="Command cancelled",
            duration_seconds=0.2,
            command="restic backup",
        )

    monkeypatch.setattr("app.services.backup_engine.run_command", fake_run_command)

    result = await engine.backup(
        {"id": "target1", "name": "Target One", "type": "local", "config": {"path": "/data"}},
        ["/config/dumps"],
        cancel_check=lambda: True,
    )

    assert result["status"] == "cancelled"
    assert result["error"] == "Command cancelled"
