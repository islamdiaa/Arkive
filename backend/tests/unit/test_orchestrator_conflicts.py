"""Unit coverage for lock-conflict startup paths in BackupOrchestrator."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db_path(tmp_path):
    from app.core.database import init_db

    db_file = tmp_path / "test_arkive.db"
    await init_db(db_file)

    async with aiosqlite.connect(db_file) as db:
        await db.execute(
            """INSERT INTO backup_jobs
               (id, name, schedule, targets, directories, exclude_patterns, include_databases, include_flash)
               VALUES ('job-1', 'Conflict Test', '0 2 * * *', '[]', '[]', '[]', 1, 1)"""
        )
        await db.commit()

    return db_file


@pytest.fixture
def orchestrator_config(tmp_path, db_path):
    config = MagicMock()
    config.db_path = db_path
    config.dump_dir = tmp_path / "dumps"
    config.dump_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir = tmp_path
    config.rclone_config = tmp_path / "rclone.conf"
    return config


def make_orchestrator(config):
    from app.services.orchestrator import BackupOrchestrator

    backup_engine = AsyncMock()
    backup_engine.init_repo = AsyncMock(return_value=True)
    backup_engine.backup = AsyncMock(return_value={"status": "success", "snapshot_id": "abc123"})
    backup_engine.forget = AsyncMock(return_value={"status": "success"})
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
        config=config,
    )
    orchestrator._release_lock = MagicMock()
    orchestrator._self_backup = AsyncMock()
    return orchestrator


@pytest.mark.asyncio
async def test_run_backup_marks_precreated_run_failed_when_restore_lock_blocks_start(
    orchestrator_config, db_path, tmp_path
):
    """Conflicts during startup must not leave orphaned running rows behind."""
    from app.services import orchestrator as orchestrator_module

    orchestrator = make_orchestrator(orchestrator_config)
    orchestrator._acquire_lock = MagicMock(return_value=False)

    restore_lock = tmp_path / "restore.lock"
    restore_lock.write_text('{"pid":123,"proc_start_time":"abc"}')

    with (
        patch.object(orchestrator_module, "RESTORE_LOCK_FILE", restore_lock),
        patch.object(orchestrator_module, "LOCK_FILE", tmp_path / "backup.lock"),
    ):
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO job_runs (id, job_id, status, trigger, started_at)
                   VALUES ('run-1', 'job-1', 'running', 'manual', '2026-03-09T00:00:00Z')"""
            )
            await db.commit()

        result = await orchestrator.run_backup("job-1", trigger="manual", run_id="run-1")

    assert result["status"] == "conflict"
    assert result["message"] == "Restore operation in progress"

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, duration_seconds, completed_at, error_message FROM job_runs WHERE id = 'run-1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert row["duration_seconds"] == 0
        assert row["completed_at"] is not None
        assert row["error_message"] == "Restore operation in progress"

        cursor = await db.execute(
            """SELECT message, severity FROM activity_log
               WHERE type = 'backup' AND action = 'completed'
               ORDER BY rowid DESC LIMIT 1"""
        )
        activity = await cursor.fetchone()
        assert activity is not None
        assert activity["message"] == "Restore operation in progress"
        assert activity["severity"] == "warning"


@pytest.mark.asyncio
async def test_run_backup_marks_scheduled_conflict_as_skipped(orchestrator_config, db_path, tmp_path):
    """Scheduled conflicts should not count as failed backups."""
    from app.services import orchestrator as orchestrator_module

    orchestrator = make_orchestrator(orchestrator_config)
    orchestrator._acquire_lock = MagicMock(return_value=False)

    backup_lock = tmp_path / "backup.lock"
    backup_lock.write_text('{"pid":123,"proc_start_time":"abc"}')

    with (
        patch.object(orchestrator_module, "LOCK_FILE", backup_lock),
        patch.object(orchestrator_module, "RESTORE_LOCK_FILE", tmp_path / "restore.lock"),
    ):
        result = await orchestrator.run_backup("job-1", trigger="scheduled", run_id="run-sched")

    assert result["status"] == "conflict"
    assert result["message"] == "Another backup is already running"

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, duration_seconds, completed_at, error_message FROM job_runs WHERE id = 'run-sched'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "skipped"
        assert row["duration_seconds"] == 0
        assert row["completed_at"] is not None
        assert row["error_message"] == "Another backup is already running"

        cursor = await db.execute(
            """SELECT message, severity, details FROM activity_log
               WHERE type = 'backup' AND action = 'completed'
               ORDER BY rowid DESC LIMIT 1"""
        )
        activity = await cursor.fetchone()
        assert activity is not None
        assert activity["message"] == "Another backup is already running"
        assert activity["severity"] == "info"
        assert '"status": "skipped"' in activity["details"]
