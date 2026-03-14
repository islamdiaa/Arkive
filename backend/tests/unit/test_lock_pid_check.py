"""Tests for lock file PID check with proc_start_time."""

from unittest.mock import mock_open, patch

from app.services.orchestrator import _get_proc_start_time


def test_get_proc_start_time_returns_field_22(tmp_path):
    """Should parse field 22 (starttime) from /proc/pid/stat."""
    # Simulated /proc/1/stat content (simplified)
    stat_content = (
        "1 (python) S 0 1 1 0 -1 4194304 1000 0 0 0 10 5 0 0 20 0 1 0"
        " 12345 1000000 100 18446744073709551615"
        " 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0"
    )
    with patch("builtins.open", mock_open(read_data=stat_content)):
        result = _get_proc_start_time(1)
    assert result == "12345"


def test_get_proc_start_time_returns_none_for_missing_proc():
    """Should return None if /proc/pid/stat doesn't exist."""
    result = _get_proc_start_time(999999999)
    assert result is None
