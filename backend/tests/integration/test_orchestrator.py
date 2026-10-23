"""
Integration tests for app.services.orchestrator — backup pipeline lifecycle,
state transitions, cancellation, error categorization, lock management.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import ArkiveConfig
from app.core.event_bus import EventBus
from app.services.orchestrator import (
    ERROR_CATEGORIES,
    LOCK_FILE,
    BackupOrchestrator,
    categorize_error,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path) -> BackupOrchestrator:
    """Create an orchestrator with all services mocked."""
    config = ArkiveConfig(config_dir=tmp_path)
    config.ensure_dirs()

    discovery = MagicMock()
    discovery.scan = AsyncMock(return_value=[])

    db_dumper = MagicMock()
    db_dumper.dump_all = AsyncMock(return_value=[])

    flash_backup = MagicMock()
    flash_backup.backup = AsyncMock(return_value={"status": "success", "size_bytes": 0})

    backup_engine = MagicMock()
    backup_engine.backup = AsyncMock(return_value={"snapshot_id": "abc123", "size_bytes": 1024})

    cloud_manager = MagicMock()
    cloud_manager.sync = AsyncMock(return_value=True)

    notifier = MagicMock()
    notifier.send = AsyncMock(return_value=[])

    event_bus = EventBus()

    return BackupOrchestrator(
        discovery=discovery,
        db_dumper=db_dumper,
        flash_backup=flash_backup,
        backup_engine=backup_engine,
        cloud_manager=cloud_manager,
        notifier=notifier,
        event_bus=event_bus,
        config=config,
    )


# ---------------------------------------------------------------------------
# Error categorization
# ---------------------------------------------------------------------------


class TestCategorizeError:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("401 Unauthorized from target", "auth_error"),
            ("403 forbidden", "auth_error"),
            ("token expired for user", "auth_error"),
            ("Connection refused by server", "network_error"),
            ("DNS resolution failed", "network_error"),
            ("timeout waiting for response", "network_error"),
            ("quota exceeded", "storage_full"),
            ("no space left on device", "storage_full"),
            ("disk quota reached", "storage_full"),
            ("permission denied writing to /mnt/user", "permission_error"),
            ("access denied to directory", "permission_error"),
            ("container not found", "container_error"),
            ("container is not running", "container_error"),
            ("database disk image is malformed", "dump_error"),
            ("integrity_check failed", "dump_error"),
            ("corrupt data detected", "dump_error"),
            ("repository is already locked by PID", "restic_error"),
            ("unable to create lock in repo", "restic_error"),
            ("Something completely unknown happened", "unknown"),
        ],
    )
    def test_categorization(self, text, expected):
        assert categorize_error(text) == expected

    def test_case_insensitive(self):
        assert categorize_error("TOKEN EXPIRED") == "auth_error"
        assert categorize_error("DISK QUOTA") == "storage_full"

    def test_empty_string_returns_unknown(self):
        # categorize_error doesn't check for empty in orchestrator version
        result = categorize_error("")
        assert result == "unknown"


class TestErrorCategories:
    def test_all_categories_have_required_fields(self):
        """Each error category should have patterns, severity, auto_retry, user_action."""
        for name, info in ERROR_CATEGORIES.items():
            assert "patterns" in info, f"Category {name} missing 'patterns'"
            assert "severity" in info, f"Category {name} missing 'severity'"
            assert "auto_retry" in info, f"Category {name} missing 'auto_retry'"
            assert "user_action" in info, f"Category {name} missing 'user_action'"


# ---------------------------------------------------------------------------
# Lock management
# ---------------------------------------------------------------------------


class TestLockManagement:
    def test_acquire_lock_creates_file(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        lock = tmp_path / "backup.lock"
        with (
            patch.object(type(LOCK_FILE), "exists", return_value=False),
            patch.object(type(LOCK_FILE), "parent", tmp_path),
            patch("app.services.orchestrator.LOCK_FILE", lock),
        ):
            result = orch._acquire_lock("run-1")

        assert result is True
        assert lock.exists()
        data = json.loads(lock.read_text())
        assert data["run_id"] == "run-1"
        assert "pid" in data

    def test_acquire_lock_fails_when_locked(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        lock = tmp_path / "backup.lock"
        lock.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),  # Our own PID (definitely alive)
                    "run_id": "old-run",
                }
            )
        )

        with patch("app.services.orchestrator.LOCK_FILE", lock):
            result = orch._acquire_lock("run-2")

        assert result is False

    def test_release_lock_removes_file(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        lock = tmp_path / "backup.lock"
        lock.write_text("content")

        with patch("app.services.orchestrator.LOCK_FILE", lock):
            orch._release_lock()

        assert not lock.exists()

    def test_release_lock_noop_when_no_lock(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        lock = tmp_path / "backup.lock"

        with patch("app.services.orchestrator.LOCK_FILE", lock):
            orch._release_lock()  # Should not raise


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_active_run(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        orch._active_runs["r1"] = False
        result = orch.cancel_run("r1")
        assert result is True
        assert orch._active_runs["r1"] is True

    def test_cancel_unknown_run(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        result = orch.cancel_run("nonexistent")
        assert result is False

    def test_is_cancelled_true(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        orch._active_runs["r1"] = True
        assert orch._is_cancelled("r1") is True

    def test_is_cancelled_false(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        orch._active_runs["r1"] = False
        assert orch._is_cancelled("r1") is False

    def test_is_cancelled_missing_run(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        assert orch._is_cancelled("nonexistent") is False

    def test_check_cancelled_raises(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        orch._active_runs["r1"] = True
        from app.services.orchestrator import _CancelledError

        with pytest.raises(_CancelledError):
            orch._check_cancelled("r1")


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


class TestServiceWiring:
    def test_orchestrator_has_all_services(self, tmp_path):
        """Orchestrator should have all required services wired."""
        orch = _make_orchestrator(tmp_path)
        assert orch.discovery is not None
        assert orch.db_dumper is not None
        assert orch.flash_backup is not None
        assert orch.backup_engine is not None
        assert orch.cloud_manager is not None
        assert orch.notifier is not None
        assert orch.event_bus is not None
        assert orch.config is not None
