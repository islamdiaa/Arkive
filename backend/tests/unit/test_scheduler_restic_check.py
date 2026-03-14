"""Unit tests for the restic integrity check system job.

Tests verify:
- Integrity check job is registered with correct trigger (Sunday 5 AM)
- Job calls backup_engine.check() for each enabled target
- Failed checks trigger notifications via notifier
- Job handles empty target list gracefully
- Job registration is idempotent
"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from apscheduler.triggers.cron import CronTrigger

from app.core.database import SCHEMA_SQL
from app.services.scheduler import (
    SYSTEM_JOB_INTEGRITY_CHECK,
    ArkiveScheduler,
)

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
    """Config with real temp DB.

    Uses sync sqlite3 for DB init to avoid aiosqlite daemon thread leaks.
    """
    config = MagicMock()
    db_path = tmp_path / "arkive.db"
    config.db_path = str(db_path)
    config.config_dir = tmp_path

    from app.core.security import _load_fernet_from_dir, _reset_fernet

    _reset_fernet()
    _load_fernet_from_dir(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return config


@pytest.fixture
def mock_backup_engine():
    engine = MagicMock()
    engine.check = AsyncMock(return_value={"status": "success", "output": ""})
    return engine


@pytest.fixture
def mock_notifier():
    notifier = MagicMock()
    notifier.send = AsyncMock(return_value=[])
    return notifier


@pytest.fixture
def scheduler(mock_orchestrator, mock_config, mock_backup_engine, mock_notifier):
    sched = ArkiveScheduler(
        orchestrator=mock_orchestrator,
        config=mock_config,
        backup_engine=mock_backup_engine,
        notifier=mock_notifier,
    )
    yield sched
    if sched.scheduler.running:
        sched.scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_target(db_path, target_id="t1", name="Local", type_="local", enabled=1, config_str="{}"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO storage_targets (id, name, type, enabled, config, status)
               VALUES (?, ?, ?, ?, ?, 'unknown')""",
            (target_id, name, type_, enabled, config_str),
        )
        await db.commit()


# ===========================================================================
# 1. Job registered with correct trigger (Sunday 5 AM)
# ===========================================================================


class TestIntegrityCheckRegistration:
    """Verify the integrity check job registers with the correct schedule."""

    def test_integrity_check_job_registered(self, scheduler):
        """_register_system_jobs registers the integrity check job."""
        scheduler._register_system_jobs()
        job = scheduler.scheduler.get_job(SYSTEM_JOB_INTEGRITY_CHECK)
        assert job is not None

    def test_integrity_check_job_has_correct_name(self, scheduler):
        """Integrity check job has a human-readable name."""
        scheduler._register_system_jobs()
        job = scheduler.scheduler.get_job(SYSTEM_JOB_INTEGRITY_CHECK)
        assert job.name == "Integrity Check"

    def test_integrity_check_job_trigger_is_sunday_5am(self, scheduler):
        """Integrity check trigger is CronTrigger with day_of_week=sun, hour=5, minute=0."""
        scheduler._register_system_jobs()
        job = scheduler.scheduler.get_job(SYSTEM_JOB_INTEGRITY_CHECK)

        trigger = job.trigger
        assert isinstance(trigger, CronTrigger)

        # Inspect trigger fields by name
        field_map = {f.name: f for f in trigger.fields}
        assert str(field_map["day_of_week"]) == "sun"
        assert str(field_map["hour"]) == "5"
        assert str(field_map["minute"]) == "0"

    def test_integrity_check_constant_value(self):
        """SYSTEM_JOB_INTEGRITY_CHECK constant has expected string value."""
        assert SYSTEM_JOB_INTEGRITY_CHECK == "system_integrity_check"

    def test_integrity_check_not_in_job_map(self, scheduler):
        """Integrity check job should not appear in _job_map (user jobs only)."""
        scheduler._register_system_jobs()
        assert SYSTEM_JOB_INTEGRITY_CHECK not in scheduler._job_map


# ===========================================================================
# 2. Job calls backup_engine.check() for each enabled target
# ===========================================================================


class TestIntegrityCheckExecution:
    """Verify the integrity check job calls check() on each enabled target."""

    @pytest.mark.asyncio
    async def test_check_called_for_single_target(self, scheduler, mock_backup_engine, mock_config):
        """check() is called once for a single enabled target."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await scheduler._run_integrity_check()
        mock_backup_engine.check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_called_for_each_enabled_target(self, scheduler, mock_backup_engine, mock_config):
        """check() is called once per enabled target."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "B2", "b2", 1, "{}")
        await scheduler._run_integrity_check()
        assert mock_backup_engine.check.await_count == 2

    @pytest.mark.asyncio
    async def test_check_skips_disabled_targets(self, scheduler, mock_backup_engine, mock_config):
        """check() is not called for disabled targets."""
        await _insert_target(mock_config.db_path, "t1", "Enabled", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "Disabled", "local", 0, "{}")
        await scheduler._run_integrity_check()
        assert mock_backup_engine.check.await_count == 1

    @pytest.mark.asyncio
    async def test_check_logs_activity_on_success(self, scheduler, mock_backup_engine, mock_config):
        """A successful integrity check logs an activity entry."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        await scheduler._run_integrity_check()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM activity_log WHERE type = 'system' AND action = 'integrity_check'")
            row = await cursor.fetchone()

        assert row is not None
        assert "Integrity check completed" in row["message"]

    @pytest.mark.asyncio
    async def test_check_continues_on_single_target_failure(
        self, scheduler, mock_backup_engine, mock_notifier, mock_config
    ):
        """If check() fails for one target, other targets are still processed."""
        await _insert_target(mock_config.db_path, "t1", "Failing", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "OK", "local", 1, "{}")

        call_count = 0

        async def side_effect(target):
            nonlocal call_count
            call_count += 1
            if target["id"] == "t1":
                raise RuntimeError("restic error: pack file missing")
            return {"status": "success", "output": ""}

        mock_backup_engine.check.side_effect = side_effect
        await scheduler._run_integrity_check()
        assert call_count == 2


# ===========================================================================
# 3. Failed checks trigger notifications
# ===========================================================================


class TestIntegrityCheckNotifications:
    """Verify that failed checks send notifications via notifier."""

    @pytest.mark.asyncio
    async def test_notification_sent_on_check_failure(self, scheduler, mock_backup_engine, mock_notifier, mock_config):
        """When check() raises, notifier.send() is called with integrity.failed."""
        await _insert_target(mock_config.db_path, "t1", "MyRepo", "local", 1, "{}")
        mock_backup_engine.check.side_effect = RuntimeError("pack integrity error")

        await scheduler._run_integrity_check()

        mock_notifier.send.assert_awaited_once()
        args = mock_notifier.send.call_args
        assert args[0][0] == "integrity.failed"
        assert args[0][3] == "error"

    @pytest.mark.asyncio
    async def test_notification_body_contains_target_name(
        self, scheduler, mock_backup_engine, mock_notifier, mock_config
    ):
        """Notification body references the failing target name."""
        await _insert_target(mock_config.db_path, "t1", "OffSiteBackup", "b2", 1, "{}")
        mock_backup_engine.check.side_effect = RuntimeError("corrupt index")

        await scheduler._run_integrity_check()

        args = mock_notifier.send.call_args
        body = args[0][2]
        assert "OffSiteBackup" in body
        assert "corrupt index" in body

    @pytest.mark.asyncio
    async def test_notification_not_sent_on_success(self, scheduler, mock_backup_engine, mock_notifier, mock_config):
        """No notification is sent when check() succeeds."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_backup_engine.check.return_value = {"status": "success", "output": ""}

        await scheduler._run_integrity_check()

        mock_notifier.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notification_sent_per_failing_target(
        self, scheduler, mock_backup_engine, mock_notifier, mock_config
    ):
        """One notification is sent per failing target."""
        await _insert_target(mock_config.db_path, "t1", "Repo1", "local", 1, "{}")
        await _insert_target(mock_config.db_path, "t2", "Repo2", "b2", 1, "{}")
        mock_backup_engine.check.side_effect = RuntimeError("index error")

        await scheduler._run_integrity_check()

        assert mock_notifier.send.await_count == 2

    @pytest.mark.asyncio
    async def test_no_notification_without_notifier(self, mock_orchestrator, mock_config, mock_backup_engine):
        """No error when notifier is None and check() fails."""
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            backup_engine=mock_backup_engine,
            notifier=None,
        )
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_backup_engine.check.side_effect = RuntimeError("error")
        # Should not raise
        await sched._run_integrity_check()
        if sched.scheduler.running:
            sched.scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_crash_job(
        self, scheduler, mock_backup_engine, mock_notifier, mock_config
    ):
        """If notifier.send() itself raises, the job still completes."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local", 1, "{}")
        mock_backup_engine.check.side_effect = RuntimeError("check error")
        mock_notifier.send.side_effect = RuntimeError("SMTP unreachable")
        # Should not raise
        await scheduler._run_integrity_check()


# ===========================================================================
# 4. Empty target list
# ===========================================================================


class TestIntegrityCheckEmptyTargets:
    """Verify graceful handling when no enabled targets exist."""

    @pytest.mark.asyncio
    async def test_empty_targets_no_check_calls(self, scheduler, mock_backup_engine):
        """check() is never called when there are no enabled targets."""
        await scheduler._run_integrity_check()
        mock_backup_engine.check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_targets_no_notification(self, scheduler, mock_notifier):
        """No notification is sent when there are no enabled targets."""
        await scheduler._run_integrity_check()
        mock_notifier.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_targets_no_crash(self, scheduler):
        """Job exits cleanly with no targets."""
        await scheduler._run_integrity_check()  # no raise

    @pytest.mark.asyncio
    async def test_skips_without_backup_engine(self, mock_orchestrator, mock_config):
        """Integrity check is a no-op when backup_engine is None."""
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            backup_engine=None,
        )
        await sched._run_integrity_check()  # no raise
        if sched.scheduler.running:
            sched.scheduler.shutdown(wait=False)


# ===========================================================================
# 5. Idempotent job registration
# ===========================================================================


class TestIntegrityCheckIdempotency:
    """Verify registering the integrity check job twice does not duplicate it."""

    def test_register_twice_no_duplicate(self, scheduler):
        """Calling _register_system_jobs twice leaves exactly one integrity check job."""
        scheduler._register_system_jobs()
        scheduler._register_system_jobs()

        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id == SYSTEM_JOB_INTEGRITY_CHECK]
        assert len(jobs) == 1

    def test_integrity_job_id_is_unique_in_scheduler(self, scheduler):
        """After double registration, only one job has the integrity check ID."""
        scheduler._register_system_jobs()
        scheduler._register_system_jobs()

        all_ids = [j.id for j in scheduler.scheduler.get_jobs()]
        assert all_ids.count(SYSTEM_JOB_INTEGRITY_CHECK) == 1

    @pytest.mark.asyncio
    async def test_start_registers_integrity_job(self, scheduler):
        """scheduler.start() registers the integrity check job."""
        with patch.object(scheduler.scheduler, "start"):
            await scheduler.start()

        assert scheduler.scheduler.get_job(SYSTEM_JOB_INTEGRITY_CHECK) is not None
