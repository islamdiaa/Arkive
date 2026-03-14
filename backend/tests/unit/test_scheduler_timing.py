"""Tests for scheduler system job timing and APScheduler configuration."""

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.services.scheduler import (
    SYSTEM_JOB_DISCOVERY,
    SYSTEM_JOB_HEALTH,
    SYSTEM_JOB_INTEGRITY_CHECK,
    SYSTEM_JOB_LOG_PRUNE,
    SYSTEM_JOB_RETENTION,
    ArkiveScheduler,
)

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
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "test.db")
    cfg.config_dir = str(tmp_path)
    return cfg


@pytest.fixture
def scheduler(mock_orchestrator, mock_config):
    return ArkiveScheduler(
        orchestrator=mock_orchestrator,
        config=mock_config,
        discovery=None,
        backup_engine=None,
        cloud_manager=None,
    )


# ---------------------------------------------------------------------------
# Tests: APScheduler job_defaults
# ---------------------------------------------------------------------------


class TestSchedulerJobDefaults:
    """Verify APScheduler is configured with the correct job_defaults."""

    def test_misfire_grace_time(self, scheduler):
        """Scheduler should have misfire_grace_time=3600."""
        defaults = scheduler.scheduler._job_defaults
        assert defaults.get("misfire_grace_time") == 3600

    def test_coalesce(self, scheduler):
        """Scheduler should have coalesce=True."""
        defaults = scheduler.scheduler._job_defaults
        assert defaults.get("coalesce") is True

    def test_max_instances(self, scheduler):
        """Scheduler should have max_instances=1."""
        defaults = scheduler.scheduler._job_defaults
        assert defaults.get("max_instances") == 1


# ---------------------------------------------------------------------------
# Tests: System job registration counts and triggers
# ---------------------------------------------------------------------------


class TestSystemJobRegistration:
    """Verify _register_system_jobs registers 3 jobs with correct triggers."""

    def _get_registered_jobs(self, scheduler):
        """Register system jobs and return a dict of job_id -> job."""
        # We don't start the scheduler (no event loop needed), just add_job
        scheduler._register_system_jobs()
        jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}
        return jobs

    def test_registers_exactly_five_system_jobs(self, scheduler):
        """_register_system_jobs must register exactly 5 jobs.

        Jobs: discovery, retention, health, log prune, integrity check.
        """
        scheduler._register_system_jobs()
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) == 5

    def test_discovery_uses_cron_trigger(self, scheduler):
        """Discovery job must use CronTrigger."""
        jobs = self._get_registered_jobs(scheduler)
        assert SYSTEM_JOB_DISCOVERY in jobs
        assert isinstance(jobs[SYSTEM_JOB_DISCOVERY].trigger, CronTrigger)

    def test_discovery_cron_hour_3(self, scheduler):
        """Discovery job CronTrigger must fire at hour=3."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_DISCOVERY].trigger
        # CronTrigger stores fields; check the hour field
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["hour"]) == "3"

    def test_discovery_cron_minute_0(self, scheduler):
        """Discovery job CronTrigger must fire at minute=0."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_DISCOVERY].trigger
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["minute"]) == "0"

    def test_retention_uses_cron_trigger(self, scheduler):
        """Retention job must use CronTrigger."""
        jobs = self._get_registered_jobs(scheduler)
        assert SYSTEM_JOB_RETENTION in jobs
        assert isinstance(jobs[SYSTEM_JOB_RETENTION].trigger, CronTrigger)

    def test_retention_cron_day_of_week_sunday(self, scheduler):
        """Retention job CronTrigger must fire on Sunday (day_of_week='sun')."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_RETENTION].trigger
        fields = {f.name: f for f in trigger.fields}
        # APScheduler stores 'sun' as 6 in day_of_week
        dow_str = str(fields["day_of_week"])
        # sun = 6 in APScheduler's 0-based weekday (mon=0, ... sun=6)
        assert dow_str == "sun" or dow_str == "6"

    def test_retention_cron_hour_4(self, scheduler):
        """Retention job CronTrigger must fire at hour=4."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_RETENTION].trigger
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["hour"]) == "4"

    def test_retention_cron_minute_0(self, scheduler):
        """Retention job CronTrigger must fire at minute=0."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_RETENTION].trigger
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["minute"]) == "0"

    def test_health_uses_interval_trigger(self, scheduler):
        """Health check job must use IntervalTrigger."""
        jobs = self._get_registered_jobs(scheduler)
        assert SYSTEM_JOB_HEALTH in jobs
        assert isinstance(jobs[SYSTEM_JOB_HEALTH].trigger, IntervalTrigger)

    def test_health_interval_5_minutes(self, scheduler):
        """Health check IntervalTrigger must have interval of 5 minutes (300 seconds)."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_HEALTH].trigger
        # IntervalTrigger stores interval as a timedelta
        assert trigger.interval.total_seconds() == 300

    def test_log_prune_registered(self, scheduler):
        """Activity log prune job must be registered."""
        jobs = self._get_registered_jobs(scheduler)
        assert SYSTEM_JOB_LOG_PRUNE in jobs

    def test_log_prune_uses_cron_trigger(self, scheduler):
        """Activity log prune job must use CronTrigger."""
        jobs = self._get_registered_jobs(scheduler)
        assert isinstance(jobs[SYSTEM_JOB_LOG_PRUNE].trigger, CronTrigger)

    def test_log_prune_cron_hour_2(self, scheduler):
        """Activity log prune job must fire at hour=2."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_LOG_PRUNE].trigger
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["hour"]) == "2"

    def test_integrity_check_registered(self, scheduler):
        """Integrity check job must be registered."""
        jobs = self._get_registered_jobs(scheduler)
        assert SYSTEM_JOB_INTEGRITY_CHECK in jobs

    def test_integrity_check_uses_cron_trigger(self, scheduler):
        """Integrity check job must use CronTrigger."""
        jobs = self._get_registered_jobs(scheduler)
        assert isinstance(jobs[SYSTEM_JOB_INTEGRITY_CHECK].trigger, CronTrigger)

    def test_integrity_check_cron_day_of_week_sunday(self, scheduler):
        """Integrity check job CronTrigger must fire on Sunday (day_of_week='sun')."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_INTEGRITY_CHECK].trigger
        fields = {f.name: f for f in trigger.fields}
        dow_str = str(fields["day_of_week"])
        assert dow_str == "sun" or dow_str == "6"

    def test_integrity_check_cron_hour_5(self, scheduler):
        """Integrity check job CronTrigger must fire at hour=5."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_INTEGRITY_CHECK].trigger
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["hour"]) == "5"

    def test_integrity_check_cron_minute_0(self, scheduler):
        """Integrity check job CronTrigger must fire at minute=0."""
        jobs = self._get_registered_jobs(scheduler)
        trigger = jobs[SYSTEM_JOB_INTEGRITY_CHECK].trigger
        fields = {f.name: f for f in trigger.fields}
        assert str(fields["minute"]) == "0"


# ---------------------------------------------------------------------------
# Tests: Activity log prune actually deletes old rows
# ---------------------------------------------------------------------------


class TestActivityLogPrune:
    """Verify _run_activity_log_prune deletes old rows using the correct column."""

    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "prune_test.db")

    @pytest.fixture
    def prune_scheduler(self, mock_orchestrator, db_path):
        cfg = MagicMock()
        cfg.db_path = db_path
        cfg.config_dir = str(MagicMock())
        return ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=cfg,
            discovery=None,
            backup_engine=None,
            cloud_manager=None,
        )

    @pytest.mark.asyncio
    async def test_prune_deletes_old_rows(self, prune_scheduler, db_path):
        """Prune should delete rows with timestamp older than 90 days."""
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    severity TEXT NOT NULL DEFAULT 'info',
                    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                )
            """)
            # Insert an old row (200 days ago) and a recent row (1 day ago)
            await db.execute(
                "INSERT INTO activity_log (type, action, message, timestamp)"
                " VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ',"
                " 'now', '-200 days'))",
                ("system", "old_event", "should be pruned"),
            )
            await db.execute(
                "INSERT INTO activity_log (type, action, message, timestamp)"
                " VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ',"
                " 'now', '-1 days'))",
                ("system", "recent_event", "should be kept"),
            )
            await db.commit()

        await prune_scheduler._run_activity_log_prune()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM activity_log")
            count = (await cursor.fetchone())[0]
            # Old row deleted, recent row kept, plus the prune activity log entry
            assert count >= 1
            cursor = await db.execute("SELECT message FROM activity_log WHERE action = 'old_event'")
            old_row = await cursor.fetchone()
            assert old_row is None, "Old row should have been pruned"
            cursor = await db.execute("SELECT message FROM activity_log WHERE action = 'recent_event'")
            recent_row = await cursor.fetchone()
            assert recent_row is not None, "Recent row should be kept"
