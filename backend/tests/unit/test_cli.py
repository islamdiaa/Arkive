"""Unit tests for the Arkive CLI."""

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def tmp_config(tmp_path):
    """Set up a temporary config directory with ARKIVE_CONFIG_DIR env var.

    Also patches the module-level DEFAULT_CONFIG_DIR in cli.py, which is
    evaluated once at import time and used by _write_api_key_file /
    _read_api_key_file for the .api_key file path.
    """
    import cli as cli_mod

    old_env = os.environ.get("ARKIVE_CONFIG_DIR")
    old_default = cli_mod.DEFAULT_CONFIG_DIR
    old_db_path = cli_mod.DEFAULT_DB_PATH

    os.environ["ARKIVE_CONFIG_DIR"] = str(tmp_path)
    cli_mod.DEFAULT_CONFIG_DIR = Path(tmp_path)
    cli_mod.DEFAULT_DB_PATH = Path(tmp_path) / "arkive.db"

    yield tmp_path

    # Restore
    cli_mod.DEFAULT_CONFIG_DIR = old_default
    cli_mod.DEFAULT_DB_PATH = old_db_path
    if old_env is None:
        os.environ.pop("ARKIVE_CONFIG_DIR", None)
    else:
        os.environ["ARKIVE_CONFIG_DIR"] = old_env


@pytest.fixture
def tmp_db(tmp_config):
    """Create a real SQLite database with the Arkive schema at the expected path."""
    from app.core.database import SCHEMA_SQL

    db_path = tmp_config / "arkive.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    # Mark setup as completed so _require_setup() doesn't exit(4)
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, '2025-01-01T00:00:00Z')",
        ("api_key_hash", "test_hash"),
    )
    conn.commit()
    conn.close()
    return db_path


# ---- 1. version command ----


def test_version(runner):
    """version command exits 0 and includes the version string."""
    # The version command wraps restic/docker/platform calls in try/except,
    # so they will gracefully degrade. We only need to mock subprocess.run
    # to prevent it from actually calling the restic binary.
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="")
        result = runner.invoke(cli, ["version"])

    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}\n{result.output}"
    assert "0.1.0" in result.output


# ---- 2. db init ----


def test_db_init(runner, tmp_config):
    """db init creates the database and exits 0."""
    db_path = tmp_config / "arkive.db"

    result = runner.invoke(cli, ["db", "init", "--db-path", str(db_path)])

    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}\n{result.output}"
    assert "initialized" in result.output.lower()
    # Verify the DB file was actually created
    assert db_path.exists()


# ---- 3. db check ----


def test_db_check(runner, tmp_db):
    """db check runs integrity check on existing DB and exits 0."""
    result = runner.invoke(cli, ["db", "check", "--db-path", str(tmp_db)])

    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}\n{result.output}"
    # Output should contain the integrity check result
    assert "ok" in result.output.lower() or "OK" in result.output


# ---- 4. key generate ----


def test_key_generate(runner, tmp_config, tmp_db):
    """key generate produces an ark_-prefixed key and exits 0."""
    # Ensure no existing key file so --force is not needed
    key_file = tmp_config / ".api_key"
    if key_file.exists():
        key_file.unlink()

    # Mock os.chmod to avoid PermissionError in test environments
    with patch("cli.os.chmod"):
        result = runner.invoke(cli, ["key", "generate", "--db-path", str(tmp_db)])

    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}\n{result.output}"
    assert "ark_" in result.output


# ---- 5. key show-hash ----


def test_key_show_hash(runner, tmp_config, tmp_db):
    """key show-hash handles gracefully when no key exists."""
    # Ensure no api_key file exists
    key_file = tmp_config / ".api_key"
    if key_file.exists():
        key_file.unlink()

    result = runner.invoke(cli, ["key", "show-hash", "--db-path", str(tmp_db)])

    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}\n{result.output}"
    # Should report None/Missing for the hash since no key is set
    assert "None" in result.output or "Missing" in result.output


# ---- 6. job list ----


def test_job_list(runner, tmp_db):
    """job list on an empty database exits 0 with 'No backup jobs' message."""
    result = runner.invoke(cli, ["job", "list", "--db-path", str(tmp_db)])

    assert result.exit_code == 0, f"Expected exit_code 0, got {result.exit_code}\n{result.output}"
    assert "no backup jobs" in result.output.lower()
