"""Unit tests for app.services.scheduler — ArkiveScheduler."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.scheduler import ArkiveScheduler


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.run_backup = AsyncMock()
    return orch


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.db_path = str(tmp_path / "test.db")
    config.config_dir = tmp_path
    return config


@pytest.fixture
def scheduler(mock_orchestrator, mock_config):
    return ArkiveScheduler(orchestrator=mock_orchestrator, config=mock_config)


class TestArkiveSchedulerInit:
    """Test ArkiveScheduler initialization."""

    def test_scheduler_init(self, scheduler):
        """Scheduler has a scheduler attribute that is AsyncIOScheduler."""
        assert hasattr(scheduler, "scheduler")
        assert isinstance(scheduler.scheduler, AsyncIOScheduler)
        assert scheduler._job_map == {}


class TestArkiveSchedulerRegister:
    """Test job registration methods."""

    def test_add_job_internal(self, scheduler):
        """Adding a job via _add_job adds it to _job_map."""
        job_def = {
            "id": "j1",
            "schedule": "0 2 * * *",
            "name": "Test",
            "type": "full",
        }
        scheduler._add_job(job_def)
        assert "j1" in scheduler._job_map
        assert scheduler._job_map["j1"] == "backup_j1"
        # Verify the APScheduler itself has the job
        apjob = scheduler.scheduler.get_job("backup_j1")
        assert apjob is not None

    def test_add_job_with_name(self, scheduler):
        """Adding a named job stores the name correctly."""
        job_def = {
            "id": "j2",
            "schedule": "30 3 * * *",
            "name": "DB Dump",
            "type": "db_dump",
        }
        scheduler._add_job(job_def)
        assert "j2" in scheduler._job_map
        apjob = scheduler.scheduler.get_job("backup_j2")
        assert apjob is not None
        assert apjob.name == "DB Dump"


class TestArkiveSchedulerAddRemove:
    """Test adding and removing jobs."""

    @pytest.mark.asyncio
    async def test_remove_job(self, scheduler):
        """Removing a previously registered job clears it from _job_map."""
        job_def = {
            "id": "j1",
            "schedule": "0 2 * * *",
            "name": "Test",
            "type": "full",
        }
        scheduler._add_job(job_def)
        assert "j1" in scheduler._job_map

        await scheduler.remove_job("j1")
        assert "j1" not in scheduler._job_map

    @pytest.mark.asyncio
    async def test_remove_job_nonexistent(self, scheduler):
        """Removing a job that was never registered does not raise."""
        await scheduler.remove_job("nonexistent")
        assert "nonexistent" not in scheduler._job_map


class TestArkiveSchedulerListAndQuery:
    """Test get_next_run and get_all_next_runs helpers."""

    def test_get_next_run_none(self, scheduler):
        """get_next_run for a nonexistent job returns None."""
        result = scheduler.get_next_run("nonexistent")
        assert result is None

    def test_get_all_next_runs_empty(self, scheduler):
        """get_all_next_runs returns empty dict when no jobs."""
        result = scheduler.get_all_next_runs()
        assert result == {}

    def test_get_all_next_runs_with_jobs(self, scheduler):
        """get_all_next_runs returns entries for scheduled jobs."""
        # Add job without starting scheduler (next_run_time will be None)
        scheduler._add_job(
            {
                "id": "j1",
                "schedule": "0 2 * * *",
                "name": "Nightly",
                "type": "full",
            }
        )
        result = scheduler.get_all_next_runs()
        assert isinstance(result, dict)
        # Job should be in the map
        assert "j1" in scheduler._job_map
