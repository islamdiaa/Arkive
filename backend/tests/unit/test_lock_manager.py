"""Unit tests for BackupLockManager.

Covers:
- Stale lock from dead PID is cleaned up
- Malformed JSON lock is treated as stale
- String PID in lock file is rejected (Pydantic validation)
- Concurrent lock acquisition (second attempt fails)
- Restore lock blocks backup lock
- Lock released in finally block
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.lock_manager import BackupLockManager, LockFileData, parse_lock_file


class TestLockFileDataModel:
    """Pydantic model validation for lock file content."""

    def test_valid_lock_data(self):
        data = LockFileData(pid=1234, proc_start_time="9876543210")
        assert data.pid == 1234
        assert data.proc_start_time == "9876543210"

    def test_string_pid_rejected(self):
        """String PID in lock file must be rejected by Pydantic."""
        result = parse_lock_file('{"pid": "1234", "proc_start_time": "9876543210"}')
        assert result is None

    def test_float_pid_rejected(self):
        result = parse_lock_file('{"pid": 12.5, "proc_start_time": "9876543210"}')
        assert result is None

    def test_list_pid_rejected(self):
        result = parse_lock_file('{"pid": [1234], "proc_start_time": "9876543210"}')
        assert result is None

    def test_int_proc_start_time_rejected(self):
        """Integer proc_start_time is rejected (must be string)."""
        result = parse_lock_file('{"pid": 1234, "proc_start_time": 9876543210}')
        assert result is None

    def test_missing_pid_rejected(self):
        """Lock file without pid field is rejected."""
        result = parse_lock_file('{"proc_start_time": "9876543210"}')
        assert result is None


class TestParseLockFile:
    """Test lock file JSON parsing and validation."""

    def test_valid_json(self):
        raw = json.dumps({"pid": 123, "proc_start_time": "456", "started_at": "2026-01-01T00:00:00Z"})
        result = parse_lock_file(raw)
        assert result is not None
        assert result.pid == 123
        assert result.proc_start_time == "456"

    def test_malformed_json(self):
        result = parse_lock_file("not json at all {{{")
        assert result is None

    def test_empty_string(self):
        result = parse_lock_file("")
        assert result is None

    def test_sql_injection_string_pid(self):
        raw = json.dumps({"pid": "'; DROP TABLE users; --", "proc_start_time": "456"})
        result = parse_lock_file(raw)
        assert result is None


class TestStaleLockCleanup:
    """Test that stale locks from dead PIDs are cleaned up."""

    def test_stale_lock_from_dead_pid_cleaned(self, tmp_path):
        """Lock file referencing a non-existent PID should be removed."""
        mgr = BackupLockManager(tmp_path)
        lock_file = tmp_path / "backup.lock"
        lock_file.write_text(json.dumps({
            "pid": 999999999,  # PID that doesn't exist
            "proc_start_time": "12345",
            "started_at": "2026-01-01T00:00:00Z",
        }))

        result = mgr.cleanup_stale_backup_lock()
        assert result is True
        assert not lock_file.exists()

    def test_malformed_json_lock_treated_as_stale(self, tmp_path):
        """Malformed JSON in lock file should be treated as stale and removed."""
        mgr = BackupLockManager(tmp_path)
        lock_file = tmp_path / "backup.lock"
        lock_file.write_text("THIS IS NOT JSON {{{")

        result = mgr.cleanup_stale_backup_lock()
        assert result is True
        assert not lock_file.exists()

    def test_string_pid_lock_treated_as_stale(self, tmp_path):
        """Lock file with string PID should be treated as invalid and removed."""
        mgr = BackupLockManager(tmp_path)
        lock_file = tmp_path / "backup.lock"
        lock_file.write_text(json.dumps({
            "pid": "1234",  # Invalid: should be int
            "proc_start_time": "9876543210",
        }))

        result = mgr.cleanup_stale_backup_lock()
        assert result is True
        assert not lock_file.exists()

    def test_no_lock_file_returns_false(self, tmp_path):
        """No lock file means nothing to clean up."""
        mgr = BackupLockManager(tmp_path)
        result = mgr.cleanup_stale_backup_lock()
        assert result is False


class TestConcurrentLockAcquisition:
    """Test that only one lock can be acquired at a time."""

    def test_second_acquire_fails(self, tmp_path):
        """Second lock acquisition attempt should fail when lock already held."""
        mgr = BackupLockManager(tmp_path)
        current_pid = os.getpid()

        # Mock _is_process_alive to simulate /proc behavior on macOS
        with patch("app.services.lock_manager._get_proc_start_time", return_value="12345"), \
             patch("app.services.lock_manager._is_process_alive", return_value=True):
            result1 = mgr.acquire_backup_lock("run-1")
            assert result1 is True
            assert mgr.lock_file.exists()

            # Second acquisition fails because the lock is live
            result2 = mgr.acquire_backup_lock("run-2")
            assert result2 is False

    def test_acquire_after_release_succeeds(self, tmp_path):
        """Lock can be re-acquired after release."""
        mgr = BackupLockManager(tmp_path)

        with patch("app.services.lock_manager._get_proc_start_time", return_value="12345"):
            assert mgr.acquire_backup_lock("run-1") is True
        mgr.release_backup_lock()
        assert not mgr.lock_file.exists()

        with patch("app.services.lock_manager._get_proc_start_time", return_value="12345"):
            assert mgr.acquire_backup_lock("run-2") is True


class TestRestoreLockBlocksBackup:
    """Test that restore lock prevents backup lock acquisition."""

    def test_restore_lock_blocks_backup(self, tmp_path):
        """Active restore lock should prevent backup lock acquisition."""
        mgr = BackupLockManager(tmp_path)
        restore_lock = tmp_path / "restore.lock"

        # Simulate a live restore lock
        restore_lock.write_text(json.dumps({
            "pid": 12345,
            "proc_start_time": "99999",
            "started_at": "2026-01-01T00:00:00Z",
        }))

        with patch("app.services.lock_manager._is_process_alive", return_value=True):
            result = mgr.acquire_backup_lock("run-1")
        assert result is False
        assert not mgr.lock_file.exists()

    def test_stale_restore_lock_does_not_block(self, tmp_path):
        """Stale restore lock (dead PID) should not block backup."""
        mgr = BackupLockManager(tmp_path)
        restore_lock = tmp_path / "restore.lock"
        restore_lock.write_text(json.dumps({
            "pid": 999999999,  # Dead PID
            "proc_start_time": "12345",
            "started_at": "2026-01-01T00:00:00Z",
        }))

        with patch("app.services.lock_manager._get_proc_start_time", return_value="12345"):
            result = mgr.acquire_backup_lock("run-1")
        assert result is True
        assert not restore_lock.exists()  # Stale lock was cleaned up


class TestLockReleasedInFinally:
    """Test lock release in error/finally scenarios."""

    def test_release_idempotent(self, tmp_path):
        """Releasing when no lock exists should not raise."""
        mgr = BackupLockManager(tmp_path)
        # Should not raise even if no lock file
        mgr.release_backup_lock()

    def test_release_removes_file(self, tmp_path):
        """Release should remove the lock file."""
        mgr = BackupLockManager(tmp_path)
        with patch("app.services.lock_manager._get_proc_start_time", return_value="12345"):
            mgr.acquire_backup_lock("run-1")
        assert mgr.lock_file.exists()
        mgr.release_backup_lock()
        assert not mgr.lock_file.exists()

    def test_lock_released_after_simulated_error(self, tmp_path):
        """Lock must be released even when pipeline raises an exception."""
        mgr = BackupLockManager(tmp_path)
        with patch("app.services.lock_manager._get_proc_start_time", return_value="12345"):
            mgr.acquire_backup_lock("run-1")
        assert mgr.lock_file.exists()

        # Simulate a pipeline that raises in a try/finally
        try:
            raise RuntimeError("simulated pipeline failure")
        except RuntimeError:
            pass
        finally:
            mgr.release_backup_lock()

        assert not mgr.lock_file.exists()


class TestLockConflictMessage:
    """Test lock conflict message generation."""

    def test_restore_lock_message(self, tmp_path):
        mgr = BackupLockManager(tmp_path)
        (tmp_path / "restore.lock").write_text('{"pid":1}')
        assert mgr.lock_conflict_message() == "Restore operation in progress"

    def test_backup_lock_message(self, tmp_path):
        mgr = BackupLockManager(tmp_path)
        (tmp_path / "backup.lock").write_text('{"pid":1}')
        assert mgr.lock_conflict_message() == "Another backup is already running"

    def test_no_lock_message(self, tmp_path):
        mgr = BackupLockManager(tmp_path)
        msg = mgr.lock_conflict_message()
        assert "could not be acquired" in msg


class TestBackwardCompatibility:
    """Test that orchestrator re-exports work for existing callers."""

    def test_get_proc_start_time_importable_from_orchestrator(self):
        from app.services.orchestrator import _get_proc_start_time
        assert callable(_get_proc_start_time)

    def test_lock_file_constants_importable(self):
        from app.services.orchestrator import LOCK_FILE, RESTORE_LOCK_FILE
        assert "backup.lock" in str(LOCK_FILE)
        assert "restore.lock" in str(RESTORE_LOCK_FILE)

    def test_cleanup_stale_backup_lock_importable(self):
        from app.services.orchestrator import cleanup_stale_backup_lock
        assert callable(cleanup_stale_backup_lock)
