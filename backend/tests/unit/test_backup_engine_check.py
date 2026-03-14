"""Tests for BackupEngine.check() method."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_config(tmp_path):
    """Minimal ArkiveConfig mock."""
    config = MagicMock()
    config.db_path = tmp_path / "test.db"
    config.rclone_config = tmp_path / "rclone.conf"
    return config


@pytest.fixture
def engine(mock_config):
    """Create a BackupEngine with mock config."""
    from app.services.backup_engine import BackupEngine

    return BackupEngine(mock_config)


@pytest.mark.asyncio
async def test_check_success(engine):
    """check() returns status=success when restic exits with returncode 0."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '{"message_type":"summary","errors_found":false}'
    fake_result.stderr = ""

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", AsyncMock(return_value=fake_result)),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        result = await engine.check(local_target)

    assert result["status"] == "success"
    assert result["error"] is None
    assert '{"message_type":"summary"' in result["output"]


@pytest.mark.asyncio
async def test_check_failure(engine):
    """check() returns status=failed when restic exits with non-zero returncode."""
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "Fatal: unable to open repository"

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", AsyncMock(return_value=fake_result)),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        result = await engine.check(local_target)

    assert result["status"] == "failed"
    assert result["error"] is not None
    assert "Fatal" in result["error"]


@pytest.mark.asyncio
async def test_check_no_password(engine):
    """check() returns status=failed immediately when no password is set."""
    with patch.object(engine, "_get_password", AsyncMock(return_value="")):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        result = await engine.check(local_target)

    assert result["status"] == "failed"
    assert "password" in result["error"].lower()


@pytest.mark.asyncio
async def test_check_non_local_sets_rclone_config(engine, mock_config):
    """check() sets RCLONE_CONFIG in env for non-local targets."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    captured_env = {}

    async def capture_run_command(cmd, env=None, timeout=None):
        if env:
            captured_env.update(env)
        return fake_result

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", side_effect=capture_run_command),
    ):
        remote_target = {"id": "t-b2", "type": "b2"}
        result = await engine.check(remote_target)

    assert result["status"] == "success"
    assert "RCLONE_CONFIG" in captured_env
    assert captured_env["RCLONE_CONFIG"] == str(mock_config.rclone_config)


@pytest.mark.asyncio
async def test_check_local_target_no_rclone_config(engine):
    """check() does NOT set RCLONE_CONFIG for local targets."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    captured_env = {}

    async def capture_run_command(cmd, env=None, timeout=None):
        if env:
            captured_env.update(env)
        return fake_result

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", side_effect=capture_run_command),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        result = await engine.check(local_target)

    assert result["status"] == "success"
    assert "RCLONE_CONFIG" not in captured_env


@pytest.mark.asyncio
async def test_check_output_truncated_to_1000_chars(engine):
    """check() truncates stdout output to 1000 characters."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "x" * 2000  # 2000-char stdout
    fake_result.stderr = ""

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", AsyncMock(return_value=fake_result)),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        result = await engine.check(local_target)

    assert len(result["output"]) == 1000


@pytest.mark.asyncio
async def test_check_error_truncated_to_500_chars(engine):
    """check() truncates stderr error to 500 characters."""
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "e" * 1000  # 1000-char stderr

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", AsyncMock(return_value=fake_result)),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        result = await engine.check(local_target)

    assert result["status"] == "failed"
    assert len(result["error"]) == 500


@pytest.mark.asyncio
async def test_check_uses_correct_restic_command(engine):
    """check() invokes restic check with --json flag and correct repo path."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    captured_cmd = []

    async def capture_run_command(cmd, env=None, timeout=None):
        captured_cmd.extend(cmd)
        return fake_result

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", side_effect=capture_run_command),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        await engine.check(local_target)

    assert "restic" in captured_cmd
    assert "check" in captured_cmd
    assert "--json" in captured_cmd
    assert "-r" in captured_cmd


@pytest.mark.asyncio
async def test_check_timeout_is_600_seconds(engine):
    """check() uses a 600-second timeout for potentially long integrity checks."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    captured_kwargs = {}

    async def capture_run_command(cmd, env=None, timeout=None):
        captured_kwargs["timeout"] = timeout
        return fake_result

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", side_effect=capture_run_command),
    ):
        local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}
        await engine.check(local_target)

    assert captured_kwargs.get("timeout") == 600


@pytest.mark.asyncio
async def test_restore_requires_explicit_destination(engine):
    """restore() refuses to fall back to restoring into / when no destination is supplied."""
    local_target = {"id": "t-local", "type": "local", "config": {"path": "/data/local"}}

    with (
        patch.object(engine, "_get_password", AsyncMock(return_value="secret")),
        patch("app.services.backup_engine.run_command", AsyncMock()) as run_command_mock,
    ):
        result = await engine.restore(local_target, "snap123", paths=["/data"], restore_to=None)

    assert result["status"] == "failed"
    assert "restore_to" in result["error"]
    run_command_mock.assert_not_called()
