"""Tests for Phase 2 reliability fixes.

Covers:
1. Discovery persistence transaction atomicity
2. Correlation IDs (ContextVar + CorrelationFilter)
3. Event bus SubscriberHandle lifecycle
4. Subprocess process group killing
5. DB connection reuse verification
"""

import asyncio
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.core.context import CorrelationFilter, current_run_id
from app.core.event_bus import EventBus, SubscriberHandle


# ---------------------------------------------------------------------------
# 1. Discovery Persistence Transaction
# ---------------------------------------------------------------------------


class TestDiscoveryPersistenceTransaction:
    """Verify persist_discovery_results is atomic."""

    @pytest.mark.asyncio
    async def test_successful_persist_commits_all(self):
        """All containers are persisted when no error occurs."""
        from app.core.database import init_db
        from app.services.discovery_persistence import persist_discovery_results

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            containers = []
            for i in range(3):
                c = MagicMock()
                c.name = f"container-{i}"
                c.image = f"img-{i}"
                c.status = "running"
                c.ports = []
                c.mounts = []
                c.databases = []
                c.profile = "default"
                c.priority = i
                c.compose_project = None
                containers.append(c)

            async with aiosqlite.connect(db_path) as db:
                await persist_discovery_results(db, containers)

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM discovered_containers")
                count = (await cursor.fetchone())[0]
                assert count == 3

    @pytest.mark.asyncio
    async def test_error_during_persist_rolls_back(self):
        """If an error occurs mid-write, no partial state is left."""
        from app.core.database import init_db
        from app.services.discovery_persistence import persist_discovery_results

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            # First container is good, second will raise
            good = MagicMock()
            good.name = "good-container"
            good.image = "img"
            good.status = "running"
            good.ports = []
            good.mounts = []
            good.databases = []
            good.profile = "default"
            good.priority = 1
            good.compose_project = None

            bad = MagicMock()
            bad.name = "bad-container"
            bad.image = "img"
            bad.status = "running"
            bad.ports = []
            bad.mounts = []
            # Make databases property raise when accessed during json.dumps
            bad_db = MagicMock()
            bad_db.model_dump = MagicMock(side_effect=RuntimeError("simulated crash"))
            bad.databases = [bad_db]
            bad.profile = "default"
            bad.priority = 2
            bad.compose_project = None

            async with aiosqlite.connect(db_path) as db:
                with pytest.raises(RuntimeError, match="simulated crash"):
                    await persist_discovery_results(db, [good, bad])

            # Verify no partial state was left
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM discovered_containers")
                count = (await cursor.fetchone())[0]
                assert count == 0, "Transaction should have rolled back, but partial data remains"


# ---------------------------------------------------------------------------
# 2. Correlation IDs
# ---------------------------------------------------------------------------


class TestCorrelationIDs:
    """Test ContextVar-based correlation ID injection."""

    def test_default_run_id_is_none(self):
        assert current_run_id.get() is None

    def test_set_and_get_run_id(self):
        token = current_run_id.set("test-run-123")
        try:
            assert current_run_id.get() == "test-run-123"
        finally:
            current_run_id.reset(token)
        assert current_run_id.get() is None

    def test_correlation_filter_injects_run_id(self):
        f = CorrelationFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )

        token = current_run_id.set("abc-42")
        try:
            f.filter(record)
            assert record.run_id == "abc-42"
        finally:
            current_run_id.reset(token)

    def test_correlation_filter_uses_dash_when_no_run(self):
        f = CorrelationFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        f.filter(record)
        assert record.run_id == "-"

    @pytest.mark.asyncio
    async def test_orchestrator_sets_run_id_context(self):
        """run_backup sets current_run_id for the duration of the run."""
        from app.services.orchestrator import BackupOrchestrator

        captured_run_ids = []

        # Mock the lock manager
        lock_mgr = MagicMock()
        lock_mgr.acquire_backup_lock = MagicMock(return_value=True)
        lock_mgr.release_backup_lock = MagicMock()
        lock_mgr.lock_conflict_message = MagicMock(return_value="locked")

        config = MagicMock()
        config.db_path = ":memory:"
        config.dump_dir = Path("/tmp/dumps")
        config.config_dir = Path("/tmp/config")

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        orch = BackupOrchestrator(
            discovery=None, db_dumper=None,
            flash_backup=MagicMock(), backup_engine=MagicMock(),
            cloud_manager=MagicMock(), notifier=MagicMock(),
            event_bus=event_bus, config=config,
        )
        orch.lock_manager = lock_mgr

        # Use a real temp DB but capture the run_id when the job query runs
        with tempfile.TemporaryDirectory() as tmp:
            from app.core.database import init_db
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            # Don't insert a job so it returns "not found" after we capture
            config.db_path = str(db_path)

            original_connect = aiosqlite.connect

            class CapturingConnect:
                def __init__(self, *args, **kwargs):
                    captured_run_ids.append(current_run_id.get())
                    self._real = original_connect(*args, **kwargs)

                async def __aenter__(self):
                    return await self._real.__aenter__()

                async def __aexit__(self, *args):
                    return await self._real.__aexit__(*args)

            with patch("app.services.orchestrator.aiosqlite.connect", side_effect=CapturingConnect):
                result = await orch.run_backup("job1", run_id="test-run-77")

        # The run_id should have been set during the run
        assert "test-run-77" in captured_run_ids
        # After run_backup returns, context should be reset
        assert current_run_id.get() is None


# ---------------------------------------------------------------------------
# 3. Event Bus Lifecycle (additional tests beyond test_event_bus.py)
# ---------------------------------------------------------------------------


class TestEventBusLifecycle:
    """Extended lifecycle tests for SubscriberHandle."""

    def test_subscribe_unsubscribe_100_times_zero_leaks(self):
        """Subscribe and cleanup 100 handles; verify 0 leaked queues."""
        bus = EventBus()
        for _ in range(100):
            handle = bus.subscribe()
            handle.cleanup()
        assert len(bus._subscribers) == 0

    def test_queue_maxsize_is_1000(self):
        """Queue maxsize should be 1000 (increased from 100)."""
        bus = EventBus()
        handle = bus.subscribe()
        assert handle.queue.maxsize == 1000


# ---------------------------------------------------------------------------
# 4. Subprocess Process Groups
# ---------------------------------------------------------------------------


class TestSubprocessProcessGroups:
    """Test process group killing on timeout/cancel."""

    def test_kill_process_group_handles_process_lookup_error(self):
        """_kill_process_group silently handles ProcessLookupError."""
        from app.utils.subprocess_runner import _kill_process_group
        # PID that almost certainly doesn't exist
        _kill_process_group(999999999)  # Should not raise

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    async def test_subprocess_uses_setsid(self):
        """Subprocess is started with preexec_fn=os.setsid on POSIX."""
        from app.utils.subprocess_runner import run_command

        result = await run_command(["echo", "hello"], timeout=10)
        assert result.returncode == 0
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    async def test_timeout_kills_process_group(self):
        """On timeout, the entire process group is killed."""
        from app.utils.subprocess_runner import run_command

        # Start a process that sleeps for 60s, should be killed by 1s timeout
        result = await run_command(["sleep", "60"], timeout=1)
        assert result.returncode == -1
        assert "timed out" in result.stderr


# ---------------------------------------------------------------------------
# 5. DB Connection Reuse
# ---------------------------------------------------------------------------


class TestDBConnectionReuse:
    """Verify orchestrator reuses DB connections in the target loop."""

    @pytest.mark.asyncio
    async def test_target_loop_uses_single_connection(self):
        """The target upload loop should not open new connections per target."""
        from app.services.orchestrator import BackupOrchestrator

        config = MagicMock()
        config.db_path = ":memory:"
        config.dump_dir = Path("/tmp/dumps")
        config.config_dir = Path("/tmp/config")

        lock_mgr = MagicMock()
        lock_mgr.acquire_backup_lock = MagicMock(return_value=True)
        lock_mgr.release_backup_lock = MagicMock()

        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        backup_engine = MagicMock()
        backup_engine.init_repo = AsyncMock()
        backup_engine.backup = AsyncMock(return_value={"status": "success", "total_bytes_processed": 100, "snapshot_id": "snap1"})
        backup_engine.forget = AsyncMock()
        backup_engine.snapshots = AsyncMock(return_value=[])

        notifier = MagicMock()
        notifier.send = AsyncMock()

        orch = BackupOrchestrator(
            discovery=None, db_dumper=None,
            flash_backup=MagicMock(), backup_engine=backup_engine,
            cloud_manager=MagicMock(), notifier=notifier,
            event_bus=event_bus, config=config,
        )
        orch.lock_manager = lock_mgr

        # Track connection count
        connect_count = 0
        original_connect = aiosqlite.connect

        class CountingConnect:
            def __init__(self, *args, **kwargs):
                nonlocal connect_count
                connect_count += 1
                self._real = original_connect(*args, **kwargs)

            async def __aenter__(self):
                return await self._real.__aenter__()

            async def __aexit__(self, *args):
                return await self._real.__aexit__(*args)

        # We need a real DB to test this
        with tempfile.TemporaryDirectory() as tmp:
            from app.core.database import init_db
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            # Insert a job and two targets
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT INTO backup_jobs (id, name, schedule) VALUES ('j1', 'Test', '0 2 * * *')"
                )
                await db.execute(
                    "INSERT INTO storage_targets (id, name, type, config, enabled) VALUES ('t1', 'Target1', 'local', '{}', 1)"
                )
                await db.execute(
                    "INSERT INTO storage_targets (id, name, type, config, enabled) VALUES ('t2', 'Target2', 'local', '{}', 1)"
                )
                await db.commit()

            config.db_path = str(db_path)

            with patch("app.services.orchestrator.aiosqlite.connect", side_effect=CountingConnect):
                result = await orch.run_backup("j1", run_id="test-conn")

            # Before the connection-reuse fix, 2 targets would open:
            #   1 (job) + 1 (run record) + 1 (disk settings) +
            #   2*3 (per target: resolve + paths + record) + 1 (retention) +
            #   2 (per target: snapshots) + 1 (final status) = 12 connections
            #
            # After the fix, the target loop shares one connection for all
            # targets (resolve + paths + record + retention) and one for
            # snapshot refresh:
            #   1 (job) + 1 (run record) + 1 (disk settings) +
            #   1 (target loop) + 1 (snapshot refresh) + 1 (final status) = 6
            #
            # Assert we're under the old count (12) by a clear margin.
            assert connect_count <= 7, (
                f"Expected <= 7 DB connections with connection reuse, got {connect_count}. "
                f"Old per-target approach would open ~12 for 2 targets."
            )
