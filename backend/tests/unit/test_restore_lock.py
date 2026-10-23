"""Tests for restore lock functionality."""


def test_restore_lock_file_constants():
    """RESTORE_LOCK_FILE should be importable."""
    from app.services.orchestrator import RESTORE_LOCK_FILE

    assert "restore.lock" in str(RESTORE_LOCK_FILE)


def test_backup_lock_file_constants():
    """LOCK_FILE should be importable."""
    from app.services.orchestrator import LOCK_FILE

    assert "backup.lock" in str(LOCK_FILE)
