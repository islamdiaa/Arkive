"""Tests for orchestrator size_history population."""

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

    # Insert a storage target and backup job
    async with aiosqlite.connect(db_file) as db:
        await db.execute(
            """INSERT INTO storage_targets (id, name, type, enabled, config)
               VALUES ('target-1', 'Local Test', 'local', 1, '{}')"""
        )
        # schedule is NOT NULL in schema
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
def mock_config(tmp_path, db_path):
    """Minimal ArkiveConfig mock."""
    config = MagicMock()
    config.db_path = db_path
    config.dump_dir = tmp_path / "dumps"
    config.dump_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir = tmp_path
    config.rclone_config = tmp_path / "rclone.conf"
    return config


def make_orchestrator(mock_config, snapshots_return=None):
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
    if snapshots_return is None:
        snapshots_return = []
    backup_engine.snapshots = AsyncMock(return_value=snapshots_return)

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
        config=mock_config,
    )
    # Bypass the lock mechanism — it tries to write to /config/backup.lock
    orchestrator._acquire_lock = MagicMock(return_value=True)
    orchestrator._release_lock = MagicMock()
    return orchestrator


@pytest.mark.asyncio
async def test_size_history_populated_after_snapshot_refresh(db_path, mock_config):
    """After a backup run, size_history table should have an entry for the target."""
    from datetime import date

    snapshots = [
        {
            "id": "abc123full",
            "short_id": "abc123",
            "time": "2026-03-01T07:00:00Z",
            "hostname": "arkive",
            "paths": ["/config"],
            "tags": [],
            "size": 500,
        },
        {
            "id": "def456full",
            "short_id": "def456",
            "time": "2026-03-01T08:00:00Z",
            "hostname": "arkive",
            "paths": ["/config"],
            "tags": [],
            "size": 300,
        },
    ]

    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        orchestrator = make_orchestrator(mock_config, snapshots_return=snapshots)
        orchestrator._self_backup = AsyncMock()
        result = await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

    assert result["status"] in ("success", "partial", "failed")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM size_history WHERE target_id = 'target-1'")
        rows = await cursor.fetchall()

    assert len(rows) == 1, f"Expected 1 size_history row, got {len(rows)}"
    row = dict(rows[0])
    assert row["target_id"] == "target-1"
    assert row["snapshot_count"] == 2
    assert row["total_size_bytes"] == 800  # 500 + 300
    assert row["date"] == date.today().isoformat()


@pytest.mark.asyncio
async def test_size_history_total_size_calculated_correctly(db_path, mock_config):
    """total_size_bytes should be the sum of sizes across all snapshots."""
    snapshots = [
        {
            "id": "s1full",
            "short_id": "s1000001",
            "time": "2026-03-01T07:00:00Z",
            "hostname": "arkive",
            "paths": [],
            "tags": [],
            "size": 100,
        },
        {
            "id": "s2full",
            "short_id": "s2000002",
            "time": "2026-03-01T08:00:00Z",
            "hostname": "arkive",
            "paths": [],
            "tags": [],
            "size": 200,
        },
        {
            "id": "s3full",
            "short_id": "s3000003",
            "time": "2026-03-01T09:00:00Z",
            "hostname": "arkive",
            "paths": [],
            "tags": [],
            "size": 700,
        },
    ]

    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        orchestrator = make_orchestrator(mock_config, snapshots_return=snapshots)
        orchestrator._self_backup = AsyncMock()
        await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT total_size_bytes, snapshot_count FROM size_history WHERE target_id = 'target-1'"
        )
        row = await cursor.fetchone()

    assert row is not None
    assert dict(row)["total_size_bytes"] == 1000  # 100 + 200 + 700
    assert dict(row)["snapshot_count"] == 3


@pytest.mark.asyncio
async def test_size_history_zero_snapshots(db_path, mock_config):
    """When there are no snapshots, size_history should record zeros."""
    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        orchestrator = make_orchestrator(mock_config, snapshots_return=[])
        orchestrator._self_backup = AsyncMock()
        await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT total_size_bytes, snapshot_count FROM size_history WHERE target_id = 'target-1'"
        )
        row = await cursor.fetchone()

    assert row is not None
    assert dict(row)["total_size_bytes"] == 0
    assert dict(row)["snapshot_count"] == 0


@pytest.mark.asyncio
async def test_size_history_upserts_on_same_date(db_path, mock_config):
    """Running backup twice on the same day should upsert (INSERT OR REPLACE), not duplicate."""
    snapshots_first = [
        {
            "id": "aaa1full",
            "short_id": "aaa1aaaa",
            "time": "2026-03-01T07:00:00Z",
            "hostname": "arkive",
            "paths": [],
            "tags": [],
            "size": 100,
        },
    ]
    snapshots_second = [
        {
            "id": "bbb2full",
            "short_id": "bbb2bbbb",
            "time": "2026-03-01T09:00:00Z",
            "hostname": "arkive",
            "paths": [],
            "tags": [],
            "size": 900,
        },
    ]

    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        orchestrator = make_orchestrator(mock_config, snapshots_return=snapshots_first)
        orchestrator._self_backup = AsyncMock()
        await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        orchestrator.backup_engine.snapshots = AsyncMock(return_value=snapshots_second)
        await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM size_history WHERE target_id = 'target-1'")
        row = await cursor.fetchone()

    # Should be exactly 1 row (upsert, not duplicate)
    assert dict(row)["cnt"] == 1


@pytest.mark.asyncio
async def test_storage_targets_total_size_updated(db_path, mock_config):
    """storage_targets.total_size_bytes should be updated alongside size_history."""
    snapshots = [
        {
            "id": "x1full",
            "short_id": "x1x1x1x1",
            "time": "2026-03-01T07:00:00Z",
            "hostname": "arkive",
            "paths": [],
            "tags": [],
            "size": 1234,
        },
    ]

    with patch("app.services.orchestrator.decrypt_config", return_value={}):
        orchestrator = make_orchestrator(mock_config, snapshots_return=snapshots)
        orchestrator._self_backup = AsyncMock()
        await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT total_size_bytes FROM storage_targets WHERE id = 'target-1'")
        row = await cursor.fetchone()

    assert row is not None
    assert dict(row)["total_size_bytes"] == 1234
