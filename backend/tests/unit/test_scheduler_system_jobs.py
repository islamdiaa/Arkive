"""Unit tests for system jobs in ArkiveScheduler.

Tests verify:
- System jobs get registered on start
- Discovery job calls scan and persists results
- Retention job calls forget on each enabled target
- Health check job updates target status
- System jobs don't crash on errors (graceful error handling)
"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.core.database import SCHEMA_SQL
from app.models.discovery import DiscoveredContainer
from app.services.scheduler import (
    SYSTEM_JOB_DISCOVERY,
    SYSTEM_JOB_HEALTH,
    SYSTEM_JOB_RETENTION,
    ArkiveScheduler,
)

# This file's tests accumulate aiosqlite daemon threads that block the event
# loop when run after other async test files.  Forking isolates each test.
pytestmark = pytest.mark.forked

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.run_backup = AsyncMock()
    return orch


@pytest.fixture
def mock_config(tmp_path):
    """Config with real temp DB for system job tests.

    Uses sync sqlite3 for DB init instead of aiosqlite to avoid
    daemon thread leaks that block process exit when combined with
    pytest-asyncio event loop teardown.
    """
    config = MagicMock()
    db_path = tmp_path / "arkive.db"
    config.db_path = str(db_path)
    config.config_dir = tmp_path

    # Set up encryption keyfile so decrypt_config works
    from app.core.security import _load_fernet_from_dir, _reset_fernet

    _reset_fernet()
    _load_fernet_from_dir(str(tmp_path))

    # Initialize DB synchronously — avoids aiosqlite thread leaks
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return config


@pytest.fixture
def mock_discovery():
    disc = MagicMock()
    disc.scan = AsyncMock(return_value=[])
    return disc


@pytest.fixture
def mock_backup_engine():
    engine = MagicMock()
    engine.forget = AsyncMock(return_value={"status": "success", "output": ""})
    return engine


@pytest.fixture
def mock_cloud_manager():
    cm = MagicMock()
    cm.test_target = AsyncMock(return_value={"status": "ok", "message": "Connection successful"})
    return cm


@pytest.fixture
def scheduler(mock_orchestrator, mock_config, mock_discovery, mock_backup_engine, mock_cloud_manager):
    sched = ArkiveScheduler(
        orchestrator=mock_orchestrator,
        config=mock_config,
        discovery=mock_discovery,
        backup_engine=mock_backup_engine,
        cloud_manager=mock_cloud_manager,
    )
    yield sched
    # Ensure APScheduler background thread is fully stopped
    if sched.scheduler.running:
        sched.scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_target(db_path, target_id="t1", name="Local", type_="local", enabled=1, config_str="{}"):
    """Insert a storage target into the DB."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO storage_targets (id, name, type, enabled, config, status)
               VALUES (?, ?, ?, ?, ?, 'unknown')""",
            (target_id, name, type_, enabled, config_str),
        )
        await db.commit()


# ===========================================================================
# 1. System jobs get registered on start
# ===========================================================================


class TestSystemJobRegistration:
    """Verify that all 3 system jobs are registered in the APScheduler."""

    def test_register_system_jobs_adds_three_jobs(self, scheduler):
        """_register_system_jobs creates discovery, retention, and health check jobs."""
        scheduler._register_system_jobs()

        assert scheduler.scheduler.get_job(SYSTEM_JOB_DISCOVERY) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_RETENTION) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_HEALTH) is not None

    def test_system_job_ids_are_correct(self, scheduler):
        """System job IDs are the expected constants."""
        scheduler._register_system_jobs()

        jobs = {j.id for j in scheduler.scheduler.get_jobs()}
        assert SYSTEM_JOB_DISCOVERY in jobs
        assert SYSTEM_JOB_RETENTION in jobs
        assert SYSTEM_JOB_HEALTH in jobs

    def test_system_jobs_have_names(self, scheduler):
        """Each system job has a human-readable name."""
        scheduler._register_system_jobs()

        disc = scheduler.scheduler.get_job(SYSTEM_JOB_DISCOVERY)
        ret = scheduler.scheduler.get_job(SYSTEM_JOB_RETENTION)
        health = scheduler.scheduler.get_job(SYSTEM_JOB_HEALTH)

        assert disc.name == "Discovery Scan"
        assert ret.name == "Retention Cleanup"
        assert health.name == "Health Check"

    def test_system_jobs_replace_existing_idempotent(self, scheduler):
        """Calling _register_system_jobs twice doesn't duplicate jobs."""
        scheduler._register_system_jobs()
        scheduler._register_system_jobs()

        # Count system jobs — replace_existing=True deduplicates
        system_ids = {SYSTEM_JOB_DISCOVERY, SYSTEM_JOB_RETENTION, SYSTEM_JOB_HEALTH}
        count = sum(1 for j in scheduler.scheduler.get_jobs() if j.id in system_ids)
        assert count == 3

    @pytest.mark.asyncio
    async def test_start_registers_system_jobs(self, scheduler):
        """scheduler.start() registers system jobs alongside user backup jobs."""
        # Patch the actual APScheduler start to avoid background thread
        with patch.object(scheduler.scheduler, "start"):
            await scheduler.start()

        assert scheduler.scheduler.get_job(SYSTEM_JOB_DISCOVERY) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_RETENTION) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_HEALTH) is not None

    def test_system_jobs_not_in_job_map(self, scheduler):
        """System jobs should NOT appear in _job_map (that's for user backup jobs only)."""
        scheduler._register_system_jobs()
        assert SYSTEM_JOB_DISCOVERY not in scheduler._job_map
        assert SYSTEM_JOB_RETENTION not in scheduler._job_map
        assert SYSTEM_JOB_HEALTH not in scheduler._job_map


# ===========================================================================
# 2. Discovery job calls scan
# ===========================================================================


class TestDiscoveryScan:
    """Test the discovery scan system job."""

    @pytest.mark.asyncio
    async def test_discovery_scan_calls_engine(self, scheduler, mock_discovery):
        """Discovery scan calls DiscoveryEngine.scan()."""
        await scheduler._run_discovery_scan()
        mock_discovery.scan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discovery_scan_persists_results(self, scheduler, mock_discovery, mock_config):
        """Discovery scan saves discovered containers to the DB."""
        mock_discovery.scan.return_value = [
            DiscoveredContainer(
                name="plex",
                image="plexinc/pms-docker:latest",
                status="running",
                databases=[],
                profile="plex",
                priority="high",
                ports=["32400:32400/tcp"],
                mounts=[{"type": "bind", "source": "/mnt/user/appdata/plex", "destination": "/config", "rw": True}],
                compose_project=None,
            ),
        ]

        await scheduler._run_discovery_scan()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM discovered_containers WHERE name = 'plex'")
            row = await cursor.fetchone()

        assert row is not None
        assert row["image"] == "plexinc/pms-docker:latest"
        assert row["status"] == "running"
        assert row["profile"] == "plex"

    @pytest.mark.asyncio
    async def test_discovery_scan_logs_activity(self, scheduler, mock_discovery, mock_config):
        """Discovery scan creates an activity log entry."""
        mock_discovery.scan.return_value = []
        await scheduler._run_discovery_scan()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM activity_log WHERE type = 'system' AND action = 'discovery_scan'")
            row = await cursor.fetchone()

        assert row is not None
        assert "Discovery scan completed" in row["message"]

    @pytest.mark.asyncio
    async def test_discovery_scan_skips_without_engine(self, mock_orchestrator, mock_config):
        """Discovery scan is a no-op when discovery engine is None."""
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            discovery=None,
        )
        # Should not raise
        await sched._run_discovery_scan()

    @pytest.mark.asyncio
    async def test_discovery_scan_handles_errors(self, scheduler, mock_discovery):
        """Discovery scan catches exceptions and does not crash."""
        mock_discovery.scan.side_effect = RuntimeError("Docker unavailable")
        # Should not raise
        await scheduler._run_discovery_scan()


# ===========================================================================
# 3. Retention job calls forget on each enabled target
# ===========================================================================


class TestRetentionCleanup:
    """Test the retention cleanup system job."""

    @pytest.mark.asyncio
    async def test_retention_calls_forget(self, scheduler, mock_backup_engine, mock_config):
        """Retention job calls BackupEngine.forget() for each enabled target."""
        await _insert_target(mock_config.db_path, "t1", "Local Target", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "B2 Target", "b2", 1, "{}")

        await scheduler._run_retention_cleanup()

        assert mock_backup_engine.forget.await_count == 2

    @pytest.mark.asyncio
    async def test_retention_skips_disabled_targets(self, scheduler, mock_backup_engine, mock_config):
        """Retention job does not call forget for disabled targets."""
        await _insert_target(mock_config.db_path, "t1", "Enabled", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "Disabled", "local", 0, "{}")

        await scheduler._run_retention_cleanup()

        assert mock_backup_engine.forget.await_count == 1

    @pytest.mark.asyncio
    async def test_retention_no_targets(self, scheduler, mock_backup_engine):
        """Retention job is a no-op when there are no enabled targets."""
        await scheduler._run_retention_cleanup()
        mock_backup_engine.forget.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retention_logs_activity(self, scheduler, mock_backup_engine, mock_config):
        """Retention job creates an activity log entry."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await scheduler._run_retention_cleanup()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM activity_log WHERE type = 'system' AND action = 'retention_cleanup'"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert "Retention cleanup completed" in row["message"]

    @pytest.mark.asyncio
    async def test_retention_continues_on_single_target_failure(self, scheduler, mock_backup_engine, mock_config):
        """If forget fails for one target, others still get processed."""
        await _insert_target(mock_config.db_path, "t1", "Failing", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "OK", "local", 1, "{}")

        call_count = 0

        async def side_effect(target, **kwargs):
            nonlocal call_count
            call_count += 1
            if target["id"] == "t1":
                raise RuntimeError("restic crashed")
            return {"status": "success", "output": ""}

        mock_backup_engine.forget.side_effect = side_effect

        # Should not raise
        await scheduler._run_retention_cleanup()
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retention_skips_without_engine(self, mock_orchestrator, mock_config):
        """Retention is a no-op when backup_engine is None."""
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            backup_engine=None,
        )
        # Should not raise
        await sched._run_retention_cleanup()

    @pytest.mark.asyncio
    async def test_retention_handles_db_error(self, scheduler, mock_config, tmp_path):
        """Retention catches top-level exceptions and does not crash."""
        # Point to an empty DB file with no tables — queries will fail but
        # aiosqlite connections close cleanly (avoids daemon thread leak from
        # nonexistent paths where __aexit__ is never called).
        empty_db = tmp_path / "empty.db"
        empty_db.touch()
        scheduler.config.db_path = str(empty_db)
        # Should not raise
        await scheduler._run_retention_cleanup()


# ===========================================================================
# 4. Health check job updates target status
# ===========================================================================


class TestHealthCheck:
    """Test the health check system job."""

    @pytest.mark.asyncio
    async def test_health_check_calls_test_target(self, scheduler, mock_cloud_manager, mock_config):
        """Health check calls CloudManager.test_target() for each enabled target."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await scheduler._run_health_check()
        mock_cloud_manager.test_target.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_health_check_updates_status_online(self, scheduler, mock_cloud_manager, mock_config):
        """Health check sets status to 'online' on successful test."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_cloud_manager.test_target.return_value = {"status": "ok", "message": "OK"}

        await scheduler._run_health_check()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status, last_tested FROM storage_targets WHERE id = 't1'")
            row = await cursor.fetchone()

        assert row["status"] == "online"
        assert row["last_tested"] is not None

    @pytest.mark.asyncio
    async def test_health_check_updates_status_error(self, scheduler, mock_cloud_manager, mock_config):
        """Health check sets status to 'error' on failed test."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_cloud_manager.test_target.return_value = {"status": "error", "message": "Connection refused"}

        await scheduler._run_health_check()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status FROM storage_targets WHERE id = 't1'")
            row = await cursor.fetchone()

        assert row["status"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_marks_error_on_exception(self, scheduler, mock_cloud_manager, mock_config):
        """If test_target raises, health check marks target as 'error'."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_cloud_manager.test_target.side_effect = RuntimeError("Network down")

        await scheduler._run_health_check()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status FROM storage_targets WHERE id = 't1'")
            row = await cursor.fetchone()

        assert row["status"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_multiple_targets(self, scheduler, mock_cloud_manager, mock_config):
        """Health check processes all enabled targets."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "B2", "b2", 1, "{}")
        await _insert_target(mock_config.db_path, "t3", "Disabled", "local", 0, "{}")

        await scheduler._run_health_check()

        # Only enabled targets get tested
        assert mock_cloud_manager.test_target.await_count == 2

    @pytest.mark.asyncio
    async def test_health_check_logs_activity(self, scheduler, mock_cloud_manager, mock_config):
        """Health check creates an activity log entry."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await scheduler._run_health_check()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM activity_log WHERE type = 'system' AND action = 'health_check'")
            row = await cursor.fetchone()

        assert row is not None
        assert "Health check completed" in row["message"]

    @pytest.mark.asyncio
    async def test_health_check_skips_without_cloud_manager(self, mock_orchestrator, mock_config):
        """Health check is a no-op when cloud_manager is None."""
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            cloud_manager=None,
        )
        await sched._run_health_check()

    @pytest.mark.asyncio
    async def test_health_check_no_targets(self, scheduler, mock_cloud_manager):
        """Health check is a no-op with no enabled targets."""
        await scheduler._run_health_check()
        mock_cloud_manager.test_target.assert_not_awaited()


# ===========================================================================
# 5. System jobs don't crash on errors
# ===========================================================================


class TestSystemJobErrorHandling:
    """Verify system jobs never propagate exceptions to the scheduler."""

    @pytest.mark.asyncio
    async def test_discovery_error_doesnt_crash(self, scheduler, mock_discovery):
        """Discovery scan exception is caught."""
        mock_discovery.scan.side_effect = Exception("Catastrophic Docker failure")
        await scheduler._run_discovery_scan()  # no raise

    @pytest.mark.asyncio
    async def test_retention_error_doesnt_crash(self, scheduler, mock_backup_engine, mock_config):
        """Retention cleanup top-level exception is caught."""
        # Insert a target, then make forget explode
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_backup_engine.forget.side_effect = Exception("restic binary missing")
        await scheduler._run_retention_cleanup()  # no raise

    @pytest.mark.asyncio
    async def test_health_check_error_doesnt_crash(self, scheduler, mock_cloud_manager, mock_config):
        """Health check per-target exception doesn't crash the whole job."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "B2", "b2", 1, "{}")

        call_count = 0

        async def side_effect(target):
            nonlocal call_count
            call_count += 1
            if target["id"] == "t1":
                raise RuntimeError("Connection timeout")
            return {"status": "ok", "message": "OK"}

        mock_cloud_manager.test_target.side_effect = side_effect

        await scheduler._run_health_check()  # no raise
        assert call_count == 2  # Both targets attempted

    @pytest.mark.asyncio
    async def test_discovery_scan_none_result(self, scheduler, mock_discovery, mock_config):
        """Discovery scan handles None return from scan()."""
        mock_discovery.scan.return_value = None
        await scheduler._run_discovery_scan()  # no raise

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM activity_log WHERE type = 'system' AND action = 'discovery_scan'")
            row = await cursor.fetchone()
        assert row is not None
        assert "0 containers found" in row["message"]


# ===========================================================================
# 6. Backward compatibility — existing tests still pass
# ===========================================================================


class TestBackwardCompatibility:
    """Ensure the new constructor signature doesn't break existing usage."""

    def test_constructor_without_new_args(self):
        """ArkiveScheduler can be created without discovery/backup_engine/cloud_manager."""
        config = MagicMock()
        config.db_path = "/tmp/test.db"
        orch = MagicMock()
        sched = ArkiveScheduler(orchestrator=orch, config=config)
        assert sched.discovery is None
        assert sched.backup_engine is None
        assert sched.cloud_manager is None

    def test_add_job_still_works(self, scheduler):
        """User backup jobs still register correctly."""
        job_def = {
            "id": "j1",
            "schedule": "0 2 * * *",
            "name": "Nightly",
            "type": "full",
        }
        scheduler._add_job(job_def)
        assert "j1" in scheduler._job_map
        assert scheduler.scheduler.get_job("backup_j1") is not None
