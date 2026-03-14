"""Unit tests for cleanup_stale_restore_lock() from app.api.restore."""

import json
from pathlib import Path
from unittest.mock import patch

from app.api.restore import cleanup_stale_restore_lock

# ---------------------------------------------------------------------------
# 1. No lock file present
# ---------------------------------------------------------------------------


def test_no_lock_file_returns_false(tmp_path: Path):
    """When no restore.lock exists, the function returns False immediately."""
    result = cleanup_stale_restore_lock(config_dir=tmp_path)
    assert result is False


# ---------------------------------------------------------------------------
# 2. Stale lock with dead PID (not present in /proc)
# ---------------------------------------------------------------------------


def test_stale_lock_dead_pid_removed(tmp_path: Path):
    """A lock whose PID no longer exists should be removed (returns True)."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 999999,
                "proc_start_time": "123456789",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    with patch("app.api.restore._get_proc_start_time", return_value=None):
        result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is True
    assert not lock_file.exists(), "Stale lock file should have been removed"


# ---------------------------------------------------------------------------
# 3. Stale lock with recycled PID (PID alive but start time doesn't match)
# ---------------------------------------------------------------------------


def test_stale_lock_recycled_pid_removed(tmp_path: Path):
    """A lock whose PID exists but has a different start time (recycled)
    should be removed (returns True)."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 12345,
                "proc_start_time": "111111",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    # _get_proc_start_time returns a different start time -> PID recycled
    with patch("app.api.restore._get_proc_start_time", return_value="999999"):
        result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is True
    assert not lock_file.exists(), "Lock with recycled PID should have been removed"


# ---------------------------------------------------------------------------
# 4. Valid lock (PID alive, start time matches) — must NOT remove
# ---------------------------------------------------------------------------


def test_valid_lock_not_removed(tmp_path: Path):
    """A lock whose PID is alive and start time matches should NOT be
    removed (returns False)."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 12345,
                "proc_start_time": "555555",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    # _get_proc_start_time returns the same start time -> process alive
    with patch("app.api.restore._get_proc_start_time", return_value="555555"):
        result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is False
    assert lock_file.exists(), "Valid lock file must NOT be removed"


# ---------------------------------------------------------------------------
# 5. Corrupt lock file (invalid JSON) — should remove and return True
# ---------------------------------------------------------------------------


def test_corrupt_lock_file_removed(tmp_path: Path):
    """A lock file with invalid JSON should be treated as corrupt,
    removed, and return True."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text("this is not valid json {{{")

    result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is True
    assert not lock_file.exists(), "Corrupt lock file should have been removed"


# ---------------------------------------------------------------------------
# 6. Lock without proc_start_time field — should handle gracefully
# ---------------------------------------------------------------------------


def test_lock_without_proc_start_time_removed(tmp_path: Path):
    """A lock that has a PID but no proc_start_time should be treated as
    stale and removed (returns True).

    The function checks `if pid and stored_start:` — when stored_start is
    falsy (None/missing), it falls through to unlink.
    """
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 12345,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is True
    assert not lock_file.exists(), "Lock without proc_start_time should have been removed"


def test_lock_with_empty_proc_start_time_removed(tmp_path: Path):
    """A lock with an empty-string proc_start_time should also be treated
    as stale and removed."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 12345,
                "proc_start_time": "",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is True
    assert not lock_file.exists(), "Lock with empty proc_start_time should have been removed"


# ---------------------------------------------------------------------------
# 7. Lock without PID field — should remove as stale
# ---------------------------------------------------------------------------


def test_lock_without_pid_removed(tmp_path: Path):
    """A lock that has no PID at all should be treated as stale and removed."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    result = cleanup_stale_restore_lock(config_dir=tmp_path)

    assert result is True
    assert not lock_file.exists(), "Lock without PID should have been removed"


# ---------------------------------------------------------------------------
# 8. Default config_dir from environment variable
# ---------------------------------------------------------------------------


def test_uses_env_config_dir_when_none(tmp_path: Path):
    """When config_dir is None, the function reads ARKIVE_CONFIG_DIR from
    the environment."""
    lock_file = tmp_path / "restore.lock"
    lock_file.write_text(
        json.dumps(
            {
                "pid": 999999,
                "proc_start_time": "123456",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
    )

    with patch.dict("os.environ", {"ARKIVE_CONFIG_DIR": str(tmp_path)}):
        with patch("app.api.restore._get_proc_start_time", return_value=None):
            result = cleanup_stale_restore_lock(config_dir=None)

    assert result is True
    assert not lock_file.exists()
