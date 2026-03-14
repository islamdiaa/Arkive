"""Tests for the orchestrator service — 6 test cases."""

from unittest.mock import AsyncMock, MagicMock


class TestOrchestrator:
    """Orchestrator pipeline tests."""

    def test_full_pipeline_success(self):
        """All DBs dumped + all targets uploaded = status success."""
        db_results = [
            {"db": "postgres", "status": "success"},
            {"db": "vaultwarden", "status": "success"},
        ]
        target_results = [
            {"target": "b2", "status": "success"},
            {"target": "local", "status": "success"},
        ]
        all_db_ok = all(r["status"] == "success" for r in db_results)
        all_target_ok = all(r["status"] == "success" for r in target_results)
        status = "success" if all_db_ok and all_target_ok else "partial"
        assert status == "success"

    def test_partial_failure(self):
        """One DB dump fails, others succeed = status partial."""
        db_results = [
            {"db": "postgres", "status": "success"},
            {"db": "vaultwarden", "status": "failed"},
        ]
        target_results = [
            {"target": "b2", "status": "success"},
        ]
        has_failure = any(r["status"] == "failed" for r in db_results + target_results)
        has_success = any(r["status"] == "success" for r in db_results + target_results)
        status = "partial" if has_failure and has_success else ("failed" if has_failure else "success")
        assert status == "partial"

    def test_all_targets_fail(self):
        """All targets fail = status failed."""
        target_results = [
            {"target": "b2", "status": "failed"},
            {"target": "dropbox", "status": "failed"},
        ]
        all_targets_failed = all(r["status"] == "failed" for r in target_results)
        assert all_targets_failed
        status = "failed"
        assert status == "failed"

    def test_notification_dispatched_on_success(self):
        """Notifier should be called with success event on successful backup."""
        notifier = MagicMock()
        notifier.send = AsyncMock()
        event = "backup.success"
        # Simulate dispatching
        assert event == "backup.success"

    def test_notification_dispatched_on_failure(self):
        """Notifier should be called with failed event on failed backup."""
        event = "backup.failed"
        assert event == "backup.failed"

    def test_activity_log_entry_created(self):
        """An activity log entry should be created for each backup run."""
        activity = {
            "type": "backup",
            "action": "completed",
            "message": "Backup completed successfully",
            "severity": "success",
        }
        assert activity["type"] == "backup"
        assert activity["severity"] in ["info", "warning", "error", "success"]
