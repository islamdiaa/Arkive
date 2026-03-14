"""Tests for orchestrator pre-backup disk space check."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db_path(tmp_path):
    """Create an isolated test database with schema."""
    from app.core.database import init_db

    db_file = tmp_path / "test_arkive.db"
    await init_db(db_file)

    async with aiosqlite.connect(db_file) as db:
        await db.execute(
            """INSERT INTO storage_targets (id, name, type, enabled, config)
               VALUES ('target-1', 'Local Test', 'local', 1, '{}')"""
        )
        await db.execute(
            """INSERT INTO backup_jobs
               (id, name, schedule, targets, directories,
               exclude_patterns, include_databases, include_flash)
               VALUES ('job-1', 'Test Job', '0 2 * * *',
               '[]', '[]', '[]', 1, 1)"""
        )
        await db.commit()

    return db_file


@pytest.fixture
def orchestrator_config(tmp_path, db_path):
    """Minimal ArkiveConfig mock."""
    config = MagicMock()
    config.db_path = db_path
    config.dump_dir = tmp_path / "dumps"
    config.dump_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir = tmp_path
    config.rclone_config = tmp_path / "rclone.conf"
    return config


def make_orchestrator(orchestrator_config):
    """Build a BackupOrchestrator with all dependencies mocked."""
    from app.services.orchestrator import BackupOrchestrator

    backup_engine = AsyncMock()
    backup_engine.init_repo = AsyncMock(return_value=True)
    backup_engine.backup = AsyncMock(
        return_value={
            "status": "success",
            "snapshot_id": "abc123",
            "total_bytes_processed": 1024,
        }
    )
    backup_engine.forget = AsyncMock(return_value={"status": "success", "output": ""})
    backup_engine.snapshots = AsyncMock(return_value=[])

    notifier = AsyncMock()
    notifier.send = AsyncMock()

    event_bus = AsyncMock()
    event_bus.publish = AsyncMock()

    orchestrator = BackupOrchestrator(
        discovery=None,
        db_dumper=None,
        flash_backup=MagicMock(backup=AsyncMock(return_value=MagicMock(status="skipped", size_bytes=0))),
        backup_engine=backup_engine,
        cloud_manager=None,
        notifier=notifier,
        event_bus=event_bus,
        config=orchestrator_config,
    )
    orchestrator._acquire_lock = MagicMock(return_value=True)
    orchestrator._release_lock = MagicMock()
    orchestrator._self_backup = AsyncMock()
    return orchestrator


# ---------------------------------------------------------------------------
# Direct unit tests for _check_disk_space_for_backup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_check_aborts_below_min(orchestrator_config):
    """free=500MB, min=1GB → RuntimeError raised."""
    orchestrator = make_orchestrator(orchestrator_config)
    fake_usage = MagicMock()
    fake_usage.free = 500 * 1024**2  # 500 MB

    with patch("shutil.disk_usage", return_value=fake_usage):
        with pytest.raises(RuntimeError, match="Insufficient disk space"):
            await orchestrator._check_disk_space_for_backup(
                "run-1",
                min_bytes=1 * 1024**3,
                warn_bytes=5 * 1024**3,
            )


@pytest.mark.asyncio
async def test_disk_check_warns_between_warn_and_min(orchestrator_config, caplog):
    """free=3GB, warn=5GB, min=1GB → no error, warning logged."""
    import logging

    orchestrator = make_orchestrator(orchestrator_config)
    fake_usage = MagicMock()
    fake_usage.free = 3 * 1024**3  # 3 GB

    with patch("shutil.disk_usage", return_value=fake_usage):
        with caplog.at_level(logging.WARNING, logger="arkive.orchestrator"):
            # Should not raise
            await orchestrator._check_disk_space_for_backup(
                "run-1",
                min_bytes=1 * 1024**3,
                warn_bytes=5 * 1024**3,
            )

    assert any("Low disk space" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_disk_check_passes_above_warn(orchestrator_config, caplog):
    """free=20GB → no error, no warning."""
    import logging

    orchestrator = make_orchestrator(orchestrator_config)
    fake_usage = MagicMock()
    fake_usage.free = 20 * 1024**3  # 20 GB

    with patch("shutil.disk_usage", return_value=fake_usage):
        with caplog.at_level(logging.WARNING, logger="arkive.orchestrator"):
            await orchestrator._check_disk_space_for_backup(
                "run-1",
                min_bytes=1 * 1024**3,
                warn_bytes=5 * 1024**3,
            )

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warning_records, f"Unexpected warnings: {[r.message for r in warning_records]}"


@pytest.mark.asyncio
async def test_disk_check_stat_error_proceeds(orchestrator_config):
    """OSError from disk_usage → no error raised (graceful fallback)."""
    orchestrator = make_orchestrator(orchestrator_config)

    with patch("shutil.disk_usage", side_effect=OSError("Permission denied")):
        # Should not raise
        await orchestrator._check_disk_space_for_backup("run-1")


@pytest.mark.asyncio
async def test_exact_min_threshold_does_not_raise(orchestrator_config):
    """free == min_bytes exactly should NOT raise (boundary is exclusive)."""
    min_bytes = 1 * 1024**3  # 1 GB
    orchestrator = make_orchestrator(orchestrator_config)
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(free=min_bytes)  # exactly at threshold
        # Should NOT raise — boundary is exclusive (<, not <=)
        await orchestrator._check_disk_space_for_backup("run-boundary", min_bytes=min_bytes, warn_bytes=5 * 1024**3)


@pytest.mark.asyncio
async def test_one_byte_below_min_raises(orchestrator_config):
    """free == min_bytes - 1 should raise."""
    min_bytes = 1 * 1024**3
    orchestrator = make_orchestrator(orchestrator_config)
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(free=min_bytes - 1)
        with pytest.raises(RuntimeError, match="[Dd]isk"):
            await orchestrator._check_disk_space_for_backup("run-below", min_bytes=min_bytes, warn_bytes=5 * 1024**3)


# ---------------------------------------------------------------------------
# Integration tests: thresholds read from DB during run_backup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_check_reads_thresholds_from_db(db_path, orchestrator_config):
    """Custom thresholds from DB are used: min=2GB, warn=10GB, free=1GB → RuntimeError."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, '2026-01-01T00:00:00Z')",
            ("min_disk_space_bytes", str(2 * 1024**3)),
        )
        await db.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, '2026-01-01T00:00:00Z')",
            ("warn_disk_space_bytes", str(10 * 1024**3)),
        )
        await db.commit()

    fake_usage = MagicMock()
    fake_usage.free = 1 * 1024**3  # 1 GB — below custom min of 2 GB

    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        with patch("shutil.disk_usage", return_value=fake_usage):
            orchestrator = make_orchestrator(orchestrator_config)
            result = await orchestrator.run_backup(
                "job-1",
                trigger="manual",
                skip_databases=True,
                skip_flash=True,
            )

    # run_backup catches RuntimeError and returns failed status
    assert result["status"] == "failed"
    assert "disk" in result.get("error", "").lower() or "disk" in result.get("message", "").lower()


@pytest.mark.asyncio
async def test_disk_check_invalid_setting_falls_back(db_path, orchestrator_config):
    """Non-integer setting value falls back to default; 20GB free passes default 1GB min."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, '2026-01-01T00:00:00Z')",
            ("min_disk_space_bytes", "not_a_number"),
        )
        await db.commit()

    fake_usage = MagicMock()
    fake_usage.free = 20 * 1024**3  # 20 GB — passes default 1 GB min

    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        with patch("shutil.disk_usage", return_value=fake_usage):
            orchestrator = make_orchestrator(orchestrator_config)
            result = await orchestrator.run_backup(
                "job-1",
                trigger="manual",
                skip_databases=True,
                skip_flash=True,
            )

    # Should succeed (not fail due to disk space)
    assert result["status"] in ("success", "partial")
