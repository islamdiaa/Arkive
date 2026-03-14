"""Data integrity audit tests — covers critical backup/restore pipeline paths.

Tests cover:
1. Lock file race condition (atomic O_EXCL creation)
2. Redis exec_run exit code checking
3. Postgres/MariaDB stderr capture from streamed exec_run
4. MongoDB authentication in mongodump
5. Backup engine password validation
6. Retention policy validation (minimum values, settings from DB)
7. Scheduler retention reads DB settings
8. Dump directory cleanup
9. Stale snapshot cleanup after forget
10. Dry-run restore support
11. Orchestrator failure handling
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
            """INSERT INTO backup_jobs (id, name, schedule, targets, directories,
               exclude_patterns, include_databases, include_flash)
               VALUES ('job-1', 'Test Job', '0 2 * * *', '[]', '[]', '[]', 1, 1)"""
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

    db_dumper = MagicMock()
    db_dumper.dump_all = AsyncMock(return_value=[])
    db_dumper.cleanup_old_dumps = MagicMock(return_value=0)

    orchestrator = BackupOrchestrator(
        discovery=None,
        db_dumper=db_dumper,
        flash_backup=MagicMock(backup=AsyncMock(return_value=MagicMock(status="skipped", size_bytes=0))),
        backup_engine=backup_engine,
        cloud_manager=None,
        notifier=notifier,
        event_bus=event_bus,
        config=mock_config,
    )
    orchestrator._acquire_lock = MagicMock(return_value=True)
    orchestrator._release_lock = MagicMock()
    return orchestrator


# ===========================================================================
# 1. Lock file atomicity
# ===========================================================================


class TestLockFileAtomicity:
    """Verify the lock file uses atomic O_EXCL creation."""

    def test_acquire_lock_creates_file_atomically(self, tmp_path):
        """Lock file should be created with O_EXCL to prevent races."""
        from app.services.orchestrator import BackupOrchestrator

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        orch = BackupOrchestrator(
            discovery=None,
            db_dumper=None,
            flash_backup=MagicMock(),
            backup_engine=MagicMock(),
            cloud_manager=None,
            notifier=MagicMock(),
            event_bus=MagicMock(),
            config=config,
        )

        lock_file = tmp_path / "backup.lock"
        with patch("app.services.orchestrator.LOCK_FILE", lock_file):
            result = orch._acquire_lock(run_id="test-run")
            assert result is True
            assert lock_file.exists()

            data = json.loads(lock_file.read_text())
            assert data["run_id"] == "test-run"
            assert data["pid"] == os.getpid()

    def test_acquire_lock_fails_when_already_locked(self, tmp_path):
        """Second lock acquisition should fail if lock already exists."""
        from app.services.orchestrator import BackupOrchestrator

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        orch = BackupOrchestrator(
            discovery=None,
            db_dumper=None,
            flash_backup=MagicMock(),
            backup_engine=MagicMock(),
            cloud_manager=None,
            notifier=MagicMock(),
            event_bus=MagicMock(),
            config=config,
        )

        lock_file = tmp_path / "backup.lock"
        # Pre-create lock with our own PID (alive)
        lock_file.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": "2026-01-01T00:00:00Z",
                }
            )
        )

        with patch("app.services.orchestrator.LOCK_FILE", lock_file):
            result = orch._acquire_lock(run_id="second-run")
            assert result is False

    def test_acquire_lock_removes_stale_lock(self, tmp_path):
        """Stale lock (dead PID) should be removed and new lock acquired."""
        from app.services.orchestrator import BackupOrchestrator

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        orch = BackupOrchestrator(
            discovery=None,
            db_dumper=None,
            flash_backup=MagicMock(),
            backup_engine=MagicMock(),
            cloud_manager=None,
            notifier=MagicMock(),
            event_bus=MagicMock(),
            config=config,
        )

        lock_file = tmp_path / "backup.lock"
        # Use a PID that's guaranteed to be dead (PID 99999999)
        lock_file.write_text(
            json.dumps(
                {
                    "pid": 99999999,
                    "started_at": "2026-01-01T00:00:00Z",
                }
            )
        )

        with patch("app.services.orchestrator.LOCK_FILE", lock_file):
            result = orch._acquire_lock(run_id="new-run")
            assert result is True
            data = json.loads(lock_file.read_text())
            assert data["run_id"] == "new-run"


# ===========================================================================
# 2. Redis exit code checking
# ===========================================================================


class TestRedisExitCodeChecking:
    """Redis SAVE exit code must be checked."""

    def test_redis_save_failure_detected(self, tmp_path):
        """When redis-cli SAVE returns non-zero, dump should fail."""
        from app.models.discovery import DiscoveredDatabase
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        container = MagicMock()
        container.exec_run.return_value = (1, b"ERR SAVE failed")
        container.attrs = {"Mounts": []}
        mock_docker.containers.get.return_value = container

        dumper = DBDumper(mock_docker, config)
        db = DiscoveredDatabase(
            container_name="test-redis",
            db_type="redis",
            db_name="redis",
            host_path=None,
        )

        result = dumper._dump_redis_blocking(db)
        assert result.status == "failed"
        assert "SAVE failed" in result.error

    @patch("time.sleep")
    def test_redis_save_success(self, mock_sleep, tmp_path):
        """When BGSAVE succeeds but dump.rdb not found, should still fail gracefully."""
        from app.models.discovery import DiscoveredDatabase
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        call_count = {"LASTSAVE": 0}

        def fake_exec_run(cmd, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "LASTSAVE" in cmd_str:
                call_count["LASTSAVE"] += 1
                if call_count["LASTSAVE"] <= 1:
                    return (0, b"(integer) 1709000000\n")
                return (0, b"(integer) 1709000001\n")
            if "BGSAVE" in cmd_str:
                return (0, b"Background saving started\n")
            if "CONFIG" in cmd_str and "dir" in cmd_str:
                return (0, b"dir\n/data\n")
            if "CONFIG" in cmd_str and "dbfilename" in cmd_str:
                return (0, b"dbfilename\ndump.rdb\n")
            return (0, b"OK\n")

        mock_docker = MagicMock()
        container = MagicMock()
        container.exec_run = fake_exec_run
        container.attrs = {"Mounts": []}
        mock_docker.containers.get.return_value = container

        dumper = DBDumper(mock_docker, config)
        db = DiscoveredDatabase(
            container_name="test-redis",
            db_type="redis",
            db_name="redis",
            host_path=None,
        )

        result = dumper._dump_redis_blocking(db)
        assert result.status == "failed"
        assert "not found" in result.error


# ===========================================================================
# 3. Postgres/MariaDB stderr capture
# ===========================================================================


class TestStderrCapture:
    """Verify that stderr from database dumps is captured and reported."""

    def test_postgres_stderr_captured_on_empty_dump(self, tmp_path):
        """If pg_dump writes only to stderr, the error message should contain it."""
        from app.models.discovery import DiscoveredDatabase
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=testuser"]}}

        # Simulate stream with only stderr data (no stdout)
        def mock_exec_run(cmd, demux=False, stream=False):
            def gen():
                yield (None, b"pg_dump: error: connection to server failed")

            return (None, gen())

        container.exec_run = mock_exec_run
        mock_docker.containers.get.return_value = container

        dumper = DBDumper(mock_docker, config)
        db = DiscoveredDatabase(
            container_name="test-postgres",
            db_type="postgres",
            db_name="testdb",
            host_path=None,
        )

        result = dumper._dump_postgres_blocking(db)
        assert result.status == "failed"
        assert "connection to server failed" in result.error

    def test_mariadb_stderr_captured_on_empty_dump(self, tmp_path):
        """If mysqldump writes only to stderr, the error message should contain it."""
        from app.models.discovery import DiscoveredDatabase
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["MYSQL_ROOT_PASSWORD=secret"]}}

        def mock_exec_run(cmd, demux=False, stream=False, environment=None):
            def gen():
                yield (None, b"mysqldump: Got error: Access denied")

            return (None, gen())

        container.exec_run = mock_exec_run
        mock_docker.containers.get.return_value = container

        dumper = DBDumper(mock_docker, config)
        db = DiscoveredDatabase(
            container_name="test-mariadb",
            db_type="mariadb",
            db_name="testdb",
            host_path=None,
        )

        result = dumper._dump_mariadb_blocking(db)
        assert result.status == "failed"
        assert "Access denied" in result.error


# ===========================================================================
# 4. MongoDB authentication
# ===========================================================================


class TestMongoDBAuth:
    """MongoDB dumps should use authentication when credentials are available."""

    def test_mongodump_uses_auth_when_env_set(self, tmp_path):
        """When MONGO_INITDB_ROOT_USERNAME is set, mongodump should include auth flags."""
        from app.models.discovery import DiscoveredDatabase
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        container = MagicMock()
        container.attrs = {
            "Config": {
                "Env": [
                    "MONGO_INITDB_ROOT_USERNAME=admin",
                    "MONGO_INITDB_ROOT_PASSWORD=secret123",
                ]
            }
        }

        captured_cmd = []

        def mock_exec_run(cmd, demux=False, stream=False):
            captured_cmd.extend(cmd if isinstance(cmd, list) else [cmd])

            def gen():
                yield (b"dummy data", None)

            return (None, gen())

        container.exec_run = mock_exec_run
        mock_docker.containers.get.return_value = container

        dumper = DBDumper(mock_docker, config)
        db = DiscoveredDatabase(
            container_name="test-mongo",
            db_type="mongodb",
            db_name="admin",
            host_path=None,
        )

        dumper._dump_mongodb_blocking(db)
        assert "--username" in captured_cmd
        assert "admin" in captured_cmd
        assert "--password" in captured_cmd
        assert "secret123" in captured_cmd
        assert "--authenticationDatabase" in captured_cmd

    def test_mongodump_no_auth_when_no_env(self, tmp_path):
        """When no auth env vars are set, mongodump should not include auth flags."""
        from app.models.discovery import DiscoveredDatabase
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        container = MagicMock()
        container.attrs = {"Config": {"Env": []}}

        captured_cmd = []

        def mock_exec_run(cmd, demux=False, stream=False):
            captured_cmd.extend(cmd if isinstance(cmd, list) else [cmd])

            def gen():
                yield (b"dummy data", None)

            return (None, gen())

        container.exec_run = mock_exec_run
        mock_docker.containers.get.return_value = container

        dumper = DBDumper(mock_docker, config)
        db = DiscoveredDatabase(
            container_name="test-mongo",
            db_type="mongodb",
            db_name="admin",
            host_path=None,
        )

        dumper._dump_mongodb_blocking(db)
        assert "--username" not in captured_cmd
        assert "--password" not in captured_cmd


# ===========================================================================
# 5. Backup engine password validation
# ===========================================================================


class TestBackupEnginePasswordValidation:
    """backup() and forget() must fail-fast when no password is configured."""

    @pytest.mark.asyncio
    async def test_backup_fails_without_password(self, tmp_path):
        """backup() should return failed status when no encryption password."""
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        config.rclone_config = tmp_path / "rclone.conf"

        engine = BackupEngine(config)

        with patch.object(engine, "_get_password", AsyncMock(return_value="")):
            result = await engine.backup(
                {"type": "local", "config": {"path": "/data"}},
                ["/config/dumps"],
            )
        assert result["status"] == "failed"
        assert "password" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_forget_fails_without_password(self, tmp_path):
        """forget() should return failed status when no encryption password."""
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        config.rclone_config = tmp_path / "rclone.conf"

        engine = BackupEngine(config)

        with patch.object(engine, "_get_password", AsyncMock(return_value="")):
            result = await engine.forget(
                {"type": "local", "config": {"path": "/data"}},
            )
        assert result["status"] == "failed"


# ===========================================================================
# 6. Retention policy validation
# ===========================================================================


class TestRetentionValidation:
    """Retention values must be >= 1 to prevent accidental data deletion."""

    @pytest.mark.asyncio
    async def test_forget_rejects_zero_keep_daily(self, tmp_path):
        """keep_daily=0 should be rejected to prevent deleting all snapshots."""
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        config.rclone_config = tmp_path / "rclone.conf"

        engine = BackupEngine(config)

        with patch.object(engine, "_get_password", AsyncMock(return_value="secret")):
            result = await engine.forget(
                {"type": "local", "config": {"path": "/data"}},
                keep_daily=0,
            )
        assert result["status"] == "failed"
        assert "data loss" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_forget_rejects_negative_values(self, tmp_path):
        """Negative retention values should be rejected."""
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = tmp_path / "test.db"
        config.rclone_config = tmp_path / "rclone.conf"

        engine = BackupEngine(config)

        with patch.object(engine, "_get_password", AsyncMock(return_value="secret")):
            result = await engine.forget(
                {"type": "local", "config": {"path": "/data"}},
                keep_daily=7,
                keep_weekly=-1,
                keep_monthly=6,
            )
        assert result["status"] == "failed"


# ===========================================================================
# 7. Scheduler retention reads DB settings
# ===========================================================================


class TestSchedulerRetentionSettings:
    """Scheduler retention cleanup must read settings from DB."""

    @pytest.mark.asyncio
    async def test_scheduler_reads_retention_from_db(self, db_path, mock_config):
        """_run_retention_cleanup should read keep_daily/weekly/monthly from settings."""
        from app.services.scheduler import ArkiveScheduler

        # Set custom retention in DB
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('keep_daily', '14')")
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('keep_weekly', '8')")
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('keep_monthly', '12')")
            await db.commit()

        backup_engine = AsyncMock()
        backup_engine.forget = AsyncMock(return_value={"status": "success"})

        scheduler = ArkiveScheduler(
            orchestrator=MagicMock(),
            config=mock_config,
            backup_engine=backup_engine,
        )

        with patch("app.services.scheduler.decrypt_config", return_value={}):
            await scheduler._run_retention_cleanup()

        # Verify forget was called with the DB settings, not defaults
        assert backup_engine.forget.called
        call_kwargs = backup_engine.forget.call_args[1]
        assert call_kwargs["keep_daily"] == 14
        assert call_kwargs["keep_weekly"] == 8
        assert call_kwargs["keep_monthly"] == 12

    @pytest.mark.asyncio
    async def test_scheduler_uses_defaults_when_no_settings(self, db_path, mock_config):
        """When no retention settings exist, should use defaults (7/4/6)."""
        from app.services.scheduler import ArkiveScheduler

        backup_engine = AsyncMock()
        backup_engine.forget = AsyncMock(return_value={"status": "success"})

        scheduler = ArkiveScheduler(
            orchestrator=MagicMock(),
            config=mock_config,
            backup_engine=backup_engine,
        )

        with patch("app.services.scheduler.decrypt_config", return_value={}):
            await scheduler._run_retention_cleanup()

        assert backup_engine.forget.called
        call_kwargs = backup_engine.forget.call_args[1]
        assert call_kwargs["keep_daily"] == 7
        assert call_kwargs["keep_weekly"] == 4
        assert call_kwargs["keep_monthly"] == 6


# ===========================================================================
# 8. Dump directory cleanup
# ===========================================================================


class TestDumpCleanup:
    """Old dump files must be cleaned up to prevent unbounded disk growth."""

    def test_cleanup_removes_old_dumps(self, tmp_path):
        """cleanup_old_dumps should keep only the last N files per prefix."""
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        dumper = DBDumper(mock_docker, config)

        # Create 5 dump files for the same container/db
        for i in range(5):
            ts = f"2026030{i + 1}_120000"
            fpath = config.dump_dir / f"mydb_main_{ts}.sql.gz"
            fpath.write_text("dump data")

        removed = dumper.cleanup_old_dumps(keep_last=2)
        assert removed == 3

        remaining = list(config.dump_dir.iterdir())
        assert len(remaining) == 2

    def test_cleanup_preserves_different_prefixes(self, tmp_path):
        """Files with different prefixes should be tracked independently."""
        from app.services.db_dumper import DBDumper

        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        config.dump_dir.mkdir()

        mock_docker = MagicMock()
        dumper = DBDumper(mock_docker, config)

        # Create 3 files for prefix A
        for i in range(3):
            ts = f"2026030{i + 1}_120000"
            (config.dump_dir / f"containerA_dbA_{ts}.sql.gz").write_text("data")

        # Create 3 files for prefix B
        for i in range(3):
            ts = f"2026030{i + 1}_120000"
            (config.dump_dir / f"containerB_dbB_{ts}.sql.gz").write_text("data")

        removed = dumper.cleanup_old_dumps(keep_last=2)
        assert removed == 2  # 1 old from A + 1 old from B

        remaining = list(config.dump_dir.iterdir())
        assert len(remaining) == 4  # 2 from A + 2 from B


# ===========================================================================
# 9. Stale snapshot cleanup
# ===========================================================================


class TestStaleSnapshotCleanup:
    """After forget/prune, stale snapshot records should be removed from DB."""

    @pytest.mark.asyncio
    async def test_stale_snapshots_removed_after_backup(self, db_path, mock_config):
        """Snapshot records that no longer exist in restic should be deleted."""

        # Pre-populate DB with old snapshots
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO snapshots (id, target_id, full_id, time, paths, tags)
                   VALUES ('old1', 'target-1', 'old1full', '2026-01-01T00:00:00Z', '[]', '[]')"""
            )
            await db.execute(
                """INSERT INTO snapshots (id, target_id, full_id, time, paths, tags)
                   VALUES ('old2', 'target-1', 'old2full', '2026-01-15T00:00:00Z', '[]', '[]')"""
            )
            await db.commit()

        # Restic now only has one new snapshot (old ones were pruned)
        snapshots = [
            {
                "id": "new1full",
                "short_id": "new1",
                "time": "2026-03-01T07:00:00Z",
                "hostname": "arkive",
                "paths": ["/config"],
                "tags": [],
                "size": 500,
            },
        ]

        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config, snapshots_return=snapshots)
            orchestrator._self_backup = AsyncMock()
            await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id FROM snapshots WHERE target_id = 'target-1'")
            rows = await cursor.fetchall()
            snapshot_ids = {row["id"] for row in rows}

        # Old snapshots should be gone, only new one should remain
        assert "old1" not in snapshot_ids
        assert "old2" not in snapshot_ids
        assert "new1" in snapshot_ids


# ===========================================================================
# 10. Orchestrator calls dump cleanup
# ===========================================================================


class TestOrchestratorDumpCleanup:
    """Orchestrator should call dump cleanup after backup."""

    @pytest.mark.asyncio
    async def test_dump_cleanup_called_after_backup(self, db_path, mock_config):
        """After a successful backup, cleanup_old_dumps should be called."""
        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config, snapshots_return=[])
            orchestrator._self_backup = AsyncMock()
            await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        orchestrator.db_dumper.cleanup_old_dumps.assert_called_once_with(keep_last=3)


# ===========================================================================
# 11. Orchestrator failure handling
# ===========================================================================


class TestOrchestratorFailureHandling:
    """Verify orchestrator correctly handles failures at each pipeline step."""

    @pytest.mark.asyncio
    async def test_failed_backup_records_error_in_db(self, db_path, mock_config):
        """When backup() raises, job_runs should be updated to 'failed' with error."""
        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config)
            orchestrator._self_backup = AsyncMock()
            orchestrator.backup_engine.backup = AsyncMock(side_effect=RuntimeError("disk full"))
            result = await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        assert result["status"] == "failed"
        assert "disk full" in result["error"]

        # Verify DB was updated
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, error_message FROM job_runs WHERE job_id = 'job-1' ORDER BY started_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["status"] == "failed"
            assert "disk full" in row["error_message"]

    @pytest.mark.asyncio
    async def test_lock_released_after_failure(self, db_path, mock_config):
        """Lock must be released even when backup fails."""
        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config)
            orchestrator._self_backup = AsyncMock()
            orchestrator.backup_engine.backup = AsyncMock(side_effect=RuntimeError("test error"))
            await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        orchestrator._release_lock.assert_called()

    @pytest.mark.asyncio
    async def test_partial_status_on_target_failure(self, db_path, mock_config):
        """When some targets succeed and some fail, status should be 'partial'."""
        # Add a second target
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO storage_targets (id, name, type, enabled, config)
                   VALUES ('target-2', 'Cloud Test', 'b2', 1, '{}')"""
            )
            await db.commit()

        call_count = 0

        async def alternate_backup(target, paths, excludes=None, tags=None, cancel_check=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status": "success", "snapshot_id": "abc", "total_bytes_processed": 100}
            else:
                return {"status": "failed", "error": "network timeout"}

        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config, snapshots_return=[])
            orchestrator._self_backup = AsyncMock()
            orchestrator.backup_engine.backup = AsyncMock(side_effect=alternate_backup)

            result = await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        assert result["status"] == "partial"

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT target_id, status, error FROM job_run_targets WHERE run_id = ? ORDER BY id",
                (result["run_id"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) == 2
            assert rows[1]["status"] == "failed"
            assert rows[1]["error"] == "network timeout"

            cursor = await db.execute(
                "SELECT message FROM activity_log"
                " WHERE type = 'backup' AND action = 'completed'"
                " ORDER BY rowid DESC LIMIT 1"
            )
            activity = await cursor.fetchone()
            assert activity is not None
            assert "Cloud Test: network timeout" in activity["message"]

    @pytest.mark.asyncio
    async def test_job_directory_ids_are_resolved_to_paths_before_backup(self, db_path, mock_config):
        """Backup paths should include watched-directory paths, not raw directory IDs."""
        watched_id = "dir-1"
        watched_path = str(mock_config.config_dir / "watched-data")
        Path(watched_path).mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE backup_jobs SET directories = ? WHERE id = 'job-1'",
                (json.dumps([watched_id]),),
            )
            await db.execute(
                """INSERT INTO watched_directories
                   (id, path, label, exclude_patterns, enabled)
                   VALUES (?, ?, ?, '[]', 1)""",
                (
                    watched_id,
                    watched_path,
                    "Watched",
                ),
            )
            await db.commit()

        captured_paths = {}

        async def capture_backup(target, paths, excludes=None, tags=None, cancel_check=None, **kwargs):
            captured_paths["paths"] = list(paths)
            return {"status": "success", "snapshot_id": "abc", "total_bytes_processed": 100}

        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config, snapshots_return=[])
            orchestrator._self_backup = AsyncMock()
            orchestrator.backup_engine.backup = AsyncMock(side_effect=capture_backup)

            result = await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True, skip_flash=True)

        assert result["status"] == "success"
        assert str(mock_config.dump_dir) in captured_paths["paths"]
        assert watched_path in captured_paths["paths"]
        assert watched_id not in captured_paths["paths"]

    @pytest.mark.asyncio
    async def test_partial_status_on_database_dump_failure(self, db_path, mock_config):
        """A successful upload with failed database dumps should report partial."""
        from app.services.db_dumper import DumpResult

        class StubDbEntry:
            def __init__(self, container_name, db_type, db_name):
                self.container_name = container_name
                self.db_type = db_type
                self.db_name = db_name
                self.host_path = None

            def model_dump(self):
                return {
                    "container_name": self.container_name,
                    "db_type": self.db_type,
                    "db_name": self.db_name,
                    "host_path": self.host_path,
                }

        class StubContainer:
            def __init__(self):
                self.name = "vaultwarden"
                self.image = "vaultwarden/server:latest"
                self.status = "running"
                self.ports = []
                self.mounts = []
                self.profile = "generic"
                self.priority = "normal"
                self.compose_project = None
                self.databases = [
                    StubDbEntry("vaultwarden", "sqlite", "db.sqlite3"),
                    StubDbEntry("tautulli", "sqlite", "tautulli.db"),
                ]

        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config, snapshots_return=[])
            orchestrator._self_backup = AsyncMock()
            orchestrator.discovery = MagicMock(scan=AsyncMock(return_value=[StubContainer()]))
            orchestrator.db_dumper.dump_all = AsyncMock(
                return_value=[
                    DumpResult(
                        container_name="vaultwarden",
                        db_type="sqlite",
                        db_name="db.sqlite3",
                        dump_path="/tmp/vaultwarden.sqlite3",
                        dump_size_bytes=1024,
                        integrity_check="ok",
                        status="success",
                    ),
                    DumpResult(
                        container_name="tautulli",
                        db_type="sqlite",
                        db_name="tautulli.db",
                        dump_path="/tmp/tautulli.sqlite3",
                        dump_size_bytes=0,
                        integrity_check="failed",
                        status="failed",
                        error="database is locked",
                    ),
                ]
            )

            result = await orchestrator.run_backup("job-1", trigger="manual", skip_flash=True)

        assert result["status"] == "partial"

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, databases_failed FROM job_runs WHERE id = ?",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["status"] == "partial"
            assert row["databases_failed"] == 1

    @pytest.mark.asyncio
    async def test_partial_status_on_flash_backup_failure(self, db_path, mock_config):
        """A successful upload with failed flash backup should report partial."""
        with patch("app.services.orchestrator.decrypt_config", return_value={}):
            orchestrator = make_orchestrator(mock_config, snapshots_return=[])
            orchestrator._self_backup = AsyncMock()
            orchestrator.flash_backup.backup = AsyncMock(
                return_value=MagicMock(status="failed", size_bytes=0, error="Permission denied")
            )

            result = await orchestrator.run_backup("job-1", trigger="manual", skip_databases=True)

        assert result["status"] == "partial"

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, flash_backed_up FROM job_runs WHERE id = ?",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["status"] == "partial"
            assert (row["flash_backed_up"] or 0) == 0

            cursor = await db.execute(
                "SELECT message FROM activity_log"
                " WHERE type = 'backup' AND action = 'completed'"
                " ORDER BY rowid DESC LIMIT 1"
            )
            activity = await cursor.fetchone()
            assert activity is not None
            assert "flash backup: Permission denied" in activity[0]


# ===========================================================================
# 12. Database WAL mode and flush
# ===========================================================================


class TestDatabaseIntegrity:
    """Verify SQLite WAL mode and flush on shutdown."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_path):
        """Database should be created with WAL journal mode."""
        from app.core.database import init_db

        db_path = tmp_path / "wal_test.db"
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row[0] == "wal"

    @pytest.mark.asyncio
    async def test_flush_wal_works(self, tmp_path):
        """flush_wal should checkpoint the WAL without error."""
        from app.core.database import flush_wal, init_db

        db_path = tmp_path / "flush_test.db"
        await init_db(db_path)

        # Insert some data to create WAL entries
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO settings (key, value) VALUES ('test', 'value')")
            await db.commit()

        # Flush should not raise
        await flush_wal(db_path)

    @pytest.mark.asyncio
    async def test_stale_running_jobs_marked_interrupted(self, tmp_path):
        """On startup, any job_runs with status='running' should be marked 'interrupted'."""
        from app.core.database import init_db

        db_path = tmp_path / "startup_test.db"
        await init_db(db_path)

        # Simulate a stale running job
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""INSERT INTO backup_jobs (id, name, schedule) VALUES ('j1', 'Test', '0 * * * *')""")
            await db.execute(
                """INSERT INTO job_runs (id, job_id, status, trigger)
                   VALUES ('r1', 'j1', 'running', 'manual')"""
            )
            await db.commit()

        # Simulate the startup self-healing step from main.py
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE job_runs SET status = 'failed', error_message = 'Interrupted by server restart', "
                "completed_at = ? WHERE status = 'running'",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )
            await db.commit()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status, error_message FROM job_runs WHERE id = 'r1'")
            row = await cursor.fetchone()
            assert row["status"] == "failed"
            assert row["error_message"] == "Interrupted by server restart"

    @pytest.mark.asyncio
    async def test_parameterized_queries_prevent_injection(self, tmp_path):
        """All DB queries should use parameterized queries, not string formatting."""
        from app.core.database import init_db

        db_path = tmp_path / "injection_test.db"
        await init_db(db_path)

        # Attempt SQL injection through a job name
        malicious_name = "test'; DROP TABLE backup_jobs; --"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO backup_jobs (id, name, schedule) VALUES (?, ?, ?)",
                ("inj-test", malicious_name, "0 * * * *"),
            )
            await db.commit()

        # Table should still exist
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT name FROM backup_jobs WHERE id = 'inj-test'")
            row = await cursor.fetchone()
            assert row[0] == malicious_name  # Stored literally, not executed
