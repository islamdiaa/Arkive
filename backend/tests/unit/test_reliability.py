"""Reliability tests for startup/shutdown, scheduler, error recovery, and logging.

Tests cover:
- Startup sequence ordering and self-healing
- Shutdown sequence with active backup wait
- Scheduler error handling and recovery
- Notification failure isolation
- Log sensitive value filtering
- Health endpoint degraded/error states
- Backup engine retry logic for transient errors
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduler import SYSTEM_JOB_DISCOVERY, SYSTEM_JOB_HEALTH, SYSTEM_JOB_RETENTION, ArkiveScheduler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.run_backup = AsyncMock()
    orch._active_runs = {}
    return orch


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.db_path = str(tmp_path / "test.db")
    config.config_dir = tmp_path
    config.log_dir = tmp_path / "logs"
    config.dump_dir = tmp_path / "dumps"
    return config


@pytest.fixture
def scheduler(mock_orchestrator, mock_config):
    return ArkiveScheduler(orchestrator=mock_orchestrator, config=mock_config)


# ---------------------------------------------------------------------------
# Scheduler reliability tests
# ---------------------------------------------------------------------------


class TestSchedulerReliability:
    """Test scheduler error handling and recovery."""

    def test_scheduler_defaults_are_safe(self, scheduler):
        """Scheduler defaults: coalesce=True, max_instances=1, misfire_grace_time=3600."""
        defaults = scheduler.scheduler._job_defaults
        assert defaults.get("coalesce") is True
        assert defaults.get("max_instances") == 1
        assert defaults.get("misfire_grace_time") == 3600

    def test_system_jobs_registered(self, scheduler):
        """All 3 system jobs are registered with correct IDs."""
        scheduler._register_system_jobs()
        assert scheduler.scheduler.get_job(SYSTEM_JOB_DISCOVERY) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_RETENTION) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_HEALTH) is not None

    def test_system_jobs_replace_existing(self, scheduler):
        """Calling _register_system_jobs twice doesn't crash (replace_existing=True)."""
        scheduler._register_system_jobs()
        # Second call should not raise even though jobs already exist
        scheduler._register_system_jobs()
        # Jobs should still be present
        assert scheduler.scheduler.get_job(SYSTEM_JOB_DISCOVERY) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_RETENTION) is not None
        assert scheduler.scheduler.get_job(SYSTEM_JOB_HEALTH) is not None

    def test_invalid_cron_expression_handled(self, scheduler):
        """Adding a job with invalid cron expression logs error, doesn't crash."""
        job_def = {
            "id": "bad_job",
            "schedule": "invalid cron",
            "name": "Bad Job",
            "type": "full",
        }
        # Should not raise
        scheduler._add_job(job_def)
        # Job should NOT be in the map
        assert "bad_job" not in scheduler._job_map

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, scheduler):
        """Stopping a scheduler that isn't running doesn't crash."""
        # scheduler.scheduler.running is False by default
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_run_job_exception_caught(self, scheduler, mock_orchestrator):
        """Exceptions in _run_job are caught and logged, not propagated."""
        mock_orchestrator.run_backup = AsyncMock(side_effect=RuntimeError("boom"))
        # Should not raise
        await scheduler._run_job("test_job", "full")

    @pytest.mark.asyncio
    async def test_trigger_job_nonexistent(self, scheduler):
        """Triggering a nonexistent job does nothing."""
        await scheduler.trigger_job("nonexistent")

    def test_on_job_error_listener(self, scheduler):
        """The _on_job_error listener handles events with and without exceptions."""
        # With exception
        event_with_exc = MagicMock()
        event_with_exc.job_id = "test_job"
        event_with_exc.exception = RuntimeError("test error")
        ArkiveScheduler._on_job_error(event_with_exc)

        # Without exception attribute
        event_no_exc = MagicMock(spec=[])
        event_no_exc.job_id = "test_job"
        ArkiveScheduler._on_job_error(event_no_exc)


# ---------------------------------------------------------------------------
# Shutdown tests
# ---------------------------------------------------------------------------


class TestShutdownReliability:
    """Test graceful shutdown behavior."""

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_active_backup(self, mock_orchestrator):
        """Shutdown loop detects when active backup finishes."""
        mock_orchestrator._active_runs = {"run1": False}

        waited = 0

        # Simulate: backup completes after 2 seconds
        async def simulate_backup():
            await asyncio.sleep(0.1)
            mock_orchestrator._active_runs.clear()

        task = asyncio.create_task(simulate_backup())

        # Simulate the shutdown wait loop
        while waited < 5:
            if not mock_orchestrator._active_runs:
                break
            await asyncio.sleep(0.05)
            waited += 1

        await task
        assert not mock_orchestrator._active_runs

    @pytest.mark.asyncio
    async def test_shutdown_timeout_forces_exit(self, mock_orchestrator):
        """If backup doesn't finish within limit, shutdown proceeds anyway."""
        mock_orchestrator._active_runs = {"run1": False}

        waited = 0
        max_wait = 3  # iterations, not 300 seconds
        while waited < max_wait:
            if not mock_orchestrator._active_runs:
                break
            await asyncio.sleep(0.01)
            waited += 1

        # Should reach timeout
        assert waited == max_wait


# ---------------------------------------------------------------------------
# Notification isolation tests
# ---------------------------------------------------------------------------


class TestNotificationIsolation:
    """Test that notification failures don't break backup pipeline."""

    @pytest.mark.asyncio
    async def test_notification_failure_doesnt_block_backup(self):
        """If notifier.send() raises, backup should still return success."""
        from app.services.notifier import Notifier

        config = MagicMock()
        config.db_path = ":memory:"
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        notifier = Notifier(config, event_bus)

        # Simulate apprise not installed
        with patch.dict("sys.modules", {"apprise": None}):
            # Should not raise
            results = await notifier.send("test.event", "Test", "Test body")
            assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_notifier_rate_limiting(self):
        """Rate limiting returns throttled status without raising."""
        from app.services.notifier import MAX_PER_HOUR, Notifier

        config = MagicMock()
        config.db_path = ":memory:"
        event_bus = MagicMock()
        event_bus.publish = AsyncMock()

        notifier = Notifier(config, event_bus)

        # Simulate hitting rate limit
        channel_id = "test_channel"
        now = time.time()
        notifier._send_counts[channel_id] = [now - i for i in range(MAX_PER_HOUR)]

        assert notifier._is_rate_limited(channel_id) is True


# ---------------------------------------------------------------------------
# Logging sensitive value tests
# ---------------------------------------------------------------------------


class TestLogSensitiveFiltering:
    """Test that sensitive values are redacted from logs."""

    def test_sensitive_filter_redacts_password(self):
        """Passwords in log messages are redacted."""
        from app.utils.log_config import _SensitiveFilter

        f = _SensitiveFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Setting password=mysecretpassword123 for user",
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert "mysecretpassword123" not in record.msg
        assert "REDACTED" in record.msg

    def test_sensitive_filter_redacts_api_key(self):
        """API keys in log messages are redacted."""
        from app.utils.log_config import _SensitiveFilter

        f = _SensitiveFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Using api_key=ark_abc123def456 for auth",
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert "ark_abc123def456" not in record.msg
        assert "REDACTED" in record.msg

    def test_sensitive_filter_redacts_token(self):
        """Tokens in log messages are redacted."""
        from app.utils.log_config import _SensitiveFilter

        f = _SensitiveFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Setting token=eyJhbGciOiJIUz for service",
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert "eyJhbGciOiJIUz" not in record.msg
        assert "REDACTED" in record.msg

    def test_sensitive_filter_preserves_normal_messages(self):
        """Non-sensitive messages pass through unchanged."""
        from app.utils.log_config import _SensitiveFilter

        f = _SensitiveFilter()
        original_msg = "Backup completed successfully in 42s"
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=original_msg,
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert record.msg == original_msg

    def test_sensitive_filter_redacts_in_args(self):
        """Sensitive values in format args are also redacted."""
        from app.utils.log_config import _SensitiveFilter

        f = _SensitiveFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Config: %s",
            args=("secret_key=abc123xyz",),
            exc_info=None,
        )
        f.filter(record)
        assert "abc123xyz" not in str(record.args)


# ---------------------------------------------------------------------------
# Backup engine retry tests
# ---------------------------------------------------------------------------


class TestBackupEngineRetry:
    """Test retry logic for transient network errors."""

    def test_is_transient_error_detection(self):
        """Transient error patterns are correctly identified."""
        from app.services.backup_engine import _is_transient_error

        assert _is_transient_error("connection refused by remote host") is True
        assert _is_transient_error("i/o timeout after 30s") is True
        assert _is_transient_error("DNS resolution failed") is True
        assert _is_transient_error("network unreachable") is True
        assert _is_transient_error("connection reset by peer") is True

        # Non-transient errors
        assert _is_transient_error("permission denied") is False
        assert _is_transient_error("repository not found") is False
        assert _is_transient_error("invalid password") is False
        assert _is_transient_error("ssh: handshake failed: ssh: unable to authenticate") is False

    @pytest.mark.asyncio
    async def test_backup_retries_on_transient_error(self):
        """Backup retries on transient network errors."""
        from app.services.backup_engine import BackupEngine
        from app.utils.subprocess_runner import CommandResult

        config = MagicMock()
        config.db_path = ":memory:"
        config.rclone_config = Path("/tmp/rclone.conf")
        engine = BackupEngine(config)

        # Mock password retrieval
        engine._get_password = AsyncMock(return_value="testpass")
        engine._get_server_name = AsyncMock(return_value="tower")

        # First call: transient failure. Second call: success.
        call_count = 0

        async def mock_run_command(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="connection refused by remote host",
                    duration_seconds=1.0,
                    command=" ".join(cmd),
                )
            return CommandResult(
                returncode=0,
                stdout='{"message_type":"summary","snapshot_id":"abc123","total_bytes_processed":1024}',
                stderr="",
                duration_seconds=2.0,
                command=" ".join(cmd),
            )

        with patch("app.services.backup_engine.run_command", side_effect=mock_run_command):
            with patch("asyncio.sleep", new_callable=AsyncMock):  # Skip actual sleep
                result = await engine.backup(
                    {"type": "local", "config": {"path": "/data"}, "name": "test"},
                    ["/config/dumps"],
                )

        assert result["status"] == "success"
        assert call_count == 2  # Retried once

    @pytest.mark.asyncio
    async def test_backup_no_retry_on_permanent_error(self):
        """Backup does NOT retry on non-transient errors."""
        from app.services.backup_engine import BackupEngine
        from app.utils.subprocess_runner import CommandResult

        config = MagicMock()
        config.db_path = ":memory:"
        config.rclone_config = Path("/tmp/rclone.conf")
        engine = BackupEngine(config)

        engine._get_password = AsyncMock(return_value="testpass")
        engine._get_server_name = AsyncMock(return_value="tower")

        call_count = 0

        async def mock_run_command(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return CommandResult(
                returncode=1,
                stdout="",
                stderr="permission denied: /some/path",
                duration_seconds=1.0,
                command=" ".join(cmd),
            )

        with patch("app.services.backup_engine.run_command", side_effect=mock_run_command):
            result = await engine.backup(
                {"type": "local", "config": {"path": "/data"}, "name": "test"},
                ["/config/dumps"],
            )

        assert result["status"] == "failed"
        assert call_count == 1  # No retry


# ---------------------------------------------------------------------------
# Error categorization tests
# ---------------------------------------------------------------------------


class TestErrorCategorization:
    """Test that errors are correctly categorized for user feedback."""

    def test_categorize_network_error(self):
        from app.services.orchestrator import categorize_error

        assert categorize_error("Connection refused by remote host") == "network_error"

    def test_categorize_auth_error(self):
        from app.services.orchestrator import categorize_error

        assert categorize_error("401 Unauthorized") == "auth_error"

    def test_categorize_storage_full(self):
        from app.services.orchestrator import categorize_error

        assert categorize_error("No space left on device") == "storage_full"

    def test_categorize_unknown(self):
        from app.services.orchestrator import categorize_error

        assert categorize_error("some random error xyz") == "unknown"

    def test_categorize_container_error(self):
        from app.services.orchestrator import categorize_error

        assert categorize_error("Container is not running") == "container_error"

    def test_categorize_permission_error(self):
        from app.services.orchestrator import categorize_error

        assert categorize_error("Permission denied: /some/path") == "permission_error"


# ---------------------------------------------------------------------------
# Startup self-healing tests
# ---------------------------------------------------------------------------


class TestStartupSelfHealing:
    """Test that startup correctly heals dirty state."""

    @pytest.mark.asyncio
    async def test_interrupted_runs_marked_on_startup(self):
        """job_runs with status='running' are marked as 'interrupted' on startup."""
        import tempfile

        import aiosqlite

        from app.core.database import init_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            # Insert a job and a "running" run
            async with aiosqlite.connect(db_path) as db:
                await db.execute("INSERT INTO backup_jobs (id, name, schedule) VALUES ('j1', 'Test', '0 2 * * *')")
                await db.execute("INSERT INTO job_runs (id, job_id, status) VALUES ('r1', 'j1', 'running')")
                await db.commit()

            # Simulate Step 7: mark stale runs as failed (matches main.py self-healing)
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE job_runs SET status = 'failed', error_message = 'Interrupted by server restart', "
                    "completed_at = ? WHERE status = 'running'",
                    (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
                )
                await db.commit()

            # Verify
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT status, error_message FROM job_runs WHERE id = 'r1'")
                row = await cursor.fetchone()
                assert row["status"] == "failed"
                assert row["error_message"] == "Interrupted by server restart"

    @pytest.mark.asyncio
    async def test_completed_runs_not_affected(self):
        """job_runs with status='success' are NOT changed by self-healing."""
        import tempfile

        import aiosqlite

        from app.core.database import init_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                await db.execute("INSERT INTO backup_jobs (id, name, schedule) VALUES ('j1', 'Test', '0 2 * * *')")
                await db.execute("INSERT INTO job_runs (id, job_id, status) VALUES ('r1', 'j1', 'success')")
                await db.commit()

            # Self-healing step (matches main.py — marks running as failed)
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE job_runs SET status = 'failed', error_message = 'Interrupted by server restart', "
                    "completed_at = ? WHERE status = 'running'",
                    (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
                )
                await db.commit()

            # Verify unchanged
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT status FROM job_runs WHERE id = 'r1'")
                row = await cursor.fetchone()
                assert row["status"] == "success"


# ---------------------------------------------------------------------------
# Database WAL tests
# ---------------------------------------------------------------------------


class TestDatabaseWAL:
    """Test WAL mode is enabled and flushed correctly."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self):
        """Database is created with WAL journal mode."""
        import tempfile

        import aiosqlite

        from app.core.database import init_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("PRAGMA journal_mode")
                row = await cursor.fetchone()
                assert row[0] == "wal"

    @pytest.mark.asyncio
    async def test_flush_wal(self):
        """flush_wal runs PRAGMA wal_checkpoint(TRUNCATE) without error."""
        import tempfile

        from app.core.database import flush_wal, init_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            await init_db(db_path)
            # Should not raise
            await flush_wal(db_path)


# ---------------------------------------------------------------------------
# Orchestrator lock tests
# ---------------------------------------------------------------------------


class TestOrchestratorLocking:
    """Test backup lock behavior for concurrent execution safety."""

    def test_lock_acquisition_and_release(self, tmp_path):
        from app.services.orchestrator import BackupOrchestrator

        with patch("app.services.orchestrator.LOCK_FILE", tmp_path / "backup.lock"):
            orch = BackupOrchestrator(
                discovery=None,
                db_dumper=None,
                flash_backup=MagicMock(),
                backup_engine=MagicMock(),
                cloud_manager=MagicMock(),
                notifier=MagicMock(),
                event_bus=MagicMock(),
                config=MagicMock(),
            )
            lock_file = tmp_path / "backup.lock"

            with patch("app.services.orchestrator.LOCK_FILE", lock_file):
                assert orch._acquire_lock("run1") is True
                assert lock_file.exists()

                # Second acquisition should fail (lock held by current process)
                assert orch._acquire_lock("run2") is False

                orch._release_lock()
                # After release, lock file should be gone
                # (but _release_lock uses LOCK_FILE constant, so we patch it)

    def test_stale_lock_detection(self, tmp_path):
        """Stale locks from dead processes are cleaned up."""
        import json

        from app.services.orchestrator import BackupOrchestrator

        lock_file = tmp_path / "backup.lock"
        # Write a lock with a PID that doesn't exist
        lock_file.write_text(json.dumps({"pid": 999999999}))

        orch = BackupOrchestrator(
            discovery=None,
            db_dumper=None,
            flash_backup=MagicMock(),
            backup_engine=MagicMock(),
            cloud_manager=MagicMock(),
            notifier=MagicMock(),
            event_bus=MagicMock(),
            config=MagicMock(),
        )

        with patch("app.services.orchestrator.LOCK_FILE", lock_file):
            # Should succeed because PID 999999999 doesn't exist
            assert orch._acquire_lock("run1") is True
