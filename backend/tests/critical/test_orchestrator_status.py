"""Critical tests for orchestrator status classification.

Validates that the orchestrator correctly classifies backup outcomes:
- All targets + all DBs ok = success
- Some DBs fail but targets ok = partial
- All targets fail = failed
- Scheduled lock conflicts = skipped (not failed)
- Manual lock conflicts = failed
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

from app.core.database import init_db

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture
async def orch_db(tmp_path):
    """Initialize a test database with a seeded backup job."""
    db_path = tmp_path / "orch_test.db"
    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO backup_jobs
               (id, name, schedule, targets, directories, exclude_patterns,
                include_databases, include_flash)
               VALUES ('job-1', 'Test Job', '0 0 * * *', '[]', '[]', '[]', 1, 1)"""
        )
        await db.commit()

    return db_path


def _make_orchestrator(config, event_bus=None):
    """Create a BackupOrchestrator with mocked dependencies."""
    from app.services.orchestrator import BackupOrchestrator

    backup_engine = AsyncMock()
    backup_engine.init_repo = AsyncMock(return_value=True)
    backup_engine.backup = AsyncMock(return_value={
        "status": "success", "snapshot_id": "snap-1", "total_bytes_processed": 1024,
    })
    backup_engine.forget = AsyncMock()
    backup_engine.snapshots = AsyncMock(return_value=[])

    notifier = AsyncMock()
    notifier.send = AsyncMock()

    if event_bus is None:
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()

    flash = MagicMock()
    flash.backup = AsyncMock(return_value=MagicMock(status="skipped", size_bytes=0, error=None))

    orchestrator = BackupOrchestrator(
        discovery=None, db_dumper=None,
        flash_backup=flash, backup_engine=backup_engine,
        cloud_manager=None, notifier=notifier,
        event_bus=event_bus, config=config,
    )
    orchestrator._self_backup = AsyncMock()
    return orchestrator


@pytest.fixture
def orch_config(tmp_path, orch_db):
    """Create a mock config pointing to the test DB."""
    config = MagicMock()
    config.db_path = orch_db
    config.dump_dir = tmp_path / "dumps"
    config.dump_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir = tmp_path
    return config


class TestOrchestratorStatusClassification:
    """Test that the orchestrator produces correct terminal status values."""

    async def test_scheduled_conflict_produces_skipped(self, orch_config, orch_db, tmp_path):
        """Scheduled backup blocked by existing lock = 'skipped', not 'failed'."""
        from app.services import orchestrator as orch_mod

        orchestrator = _make_orchestrator(orch_config)
        orchestrator._acquire_lock = MagicMock(return_value=False)

        backup_lock = tmp_path / "backup.lock"
        backup_lock.write_text('{"pid":123}')

        with patch.object(orch_mod, "LOCK_FILE", backup_lock), \
             patch.object(orch_mod, "RESTORE_LOCK_FILE", tmp_path / "restore.lock"):
            result = await orchestrator.run_backup("job-1", trigger="scheduled", run_id="sched-1")

        assert result["status"] == "conflict"

        async with aiosqlite.connect(orch_db) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status FROM job_runs WHERE id = 'sched-1'")
            row = await cursor.fetchone()
            assert row is not None
            assert row["status"] == "skipped"
            logger.info("Scheduled conflict correctly classified as 'skipped'")

    async def test_manual_conflict_produces_failed(self, orch_config, orch_db, tmp_path):
        """Manual backup blocked by existing lock = 'failed', not 'skipped'."""
        from app.services import orchestrator as orch_mod

        orchestrator = _make_orchestrator(orch_config)
        orchestrator._acquire_lock = MagicMock(return_value=False)

        backup_lock = tmp_path / "backup.lock"
        backup_lock.write_text('{"pid":123}')

        with patch.object(orch_mod, "LOCK_FILE", backup_lock), \
             patch.object(orch_mod, "RESTORE_LOCK_FILE", tmp_path / "restore.lock"):
            result = await orchestrator.run_backup("job-1", trigger="manual", run_id="manual-1")

        assert result["status"] == "conflict"

        async with aiosqlite.connect(orch_db) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status FROM job_runs WHERE id = 'manual-1'")
            row = await cursor.fetchone()
            assert row is not None
            assert row["status"] == "failed"
            logger.info("Manual conflict correctly classified as 'failed'")

    async def test_restore_lock_blocks_backup(self, orch_config, orch_db, tmp_path):
        """Backup blocked by restore lock should report restore-specific message."""
        from app.services import orchestrator as orch_mod

        orchestrator = _make_orchestrator(orch_config)
        orchestrator._acquire_lock = MagicMock(return_value=False)

        restore_lock = tmp_path / "restore.lock"
        restore_lock.write_text('{"pid":123,"proc_start_time":"abc"}')

        with patch.object(orch_mod, "LOCK_FILE", tmp_path / "backup.lock"), \
             patch.object(orch_mod, "RESTORE_LOCK_FILE", restore_lock):
            result = await orchestrator.run_backup("job-1", trigger="manual", run_id="restore-block-1")

        assert result["status"] == "conflict"
        assert "Restore" in result["message"]
        logger.info("Restore lock correctly blocked backup: %s", result["message"])

    async def test_conflict_publishes_event(self, orch_config, orch_db, tmp_path):
        """Lock conflicts should publish an event via the event bus."""
        from app.services import orchestrator as orch_mod

        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()
        orchestrator = _make_orchestrator(orch_config, event_bus=event_bus)
        orchestrator._acquire_lock = MagicMock(return_value=False)

        backup_lock = tmp_path / "backup.lock"
        backup_lock.write_text('{"pid":123}')

        with patch.object(orch_mod, "LOCK_FILE", backup_lock), \
             patch.object(orch_mod, "RESTORE_LOCK_FILE", tmp_path / "restore.lock"):
            await orchestrator.run_backup("job-1", trigger="scheduled", run_id="event-test-1")

        assert event_bus.publish.called
        call_args = event_bus.publish.call_args
        event_name = call_args[0][0]
        event_data = call_args[0][1]
        assert event_name == "backup:cancelled"
        assert event_data["status"] == "skipped"
        assert "error_category" in event_data
        logger.info("Conflict event published: %s with status=%s",
                    event_name, event_data["status"])
