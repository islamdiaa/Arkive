"""Shared fixtures for cloud provider end-to-end tests.

Provides a test client with a real CloudManager (backed by a temp rclone config)
and a mock `run_command` that simulates rclone/restic subprocess calls.
"""

import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.cloud_manager import CloudManager
from app.utils.subprocess_runner import CommandResult

# ---------------------------------------------------------------------------
# Mock subprocess responses for rclone / restic
# ---------------------------------------------------------------------------


def make_result(rc=0, stdout="", stderr="", cmd="mock"):
    return CommandResult(returncode=rc, stdout=stdout, stderr=stderr, duration_seconds=0.1, command=cmd)


def mock_run_command_factory():
    """Return an AsyncMock that dispatches realistic responses per command."""

    async def _run(
        cmd,
        timeout=300,
        env=None,
        cwd=None,
        input_data=None,
        cancel_check=None,
        **kwargs,
    ):
        cmd_str = " ".join(cmd)

        # rclone lsd → list directories (test-connection)
        if "rclone" in cmd and "lsd" in cmd:
            return make_result(0, "          -1 2026-01-01 00:00:00        -1 arkive-backups\n", cmd=cmd_str)

        # rclone about → usage stats
        if "rclone" in cmd and "about" in cmd:
            return make_result(
                0,
                json.dumps(
                    {
                        "total": 1099511627776,  # 1 TB
                        "used": 107374182400,  # 100 GB
                        "free": 992137445376,  # ~924 GB
                    }
                ),
                cmd=cmd_str,
            )

        # rclone obscure → SFTP password obfuscation
        if "rclone" in cmd and "obscure" in cmd:
            return make_result(0, "OBSCURED_PASSWORD\n", cmd=cmd_str)

        # restic snapshots → check if repo is initialized
        if "restic" in cmd and "snapshots" in cmd:
            return make_result(0, json.dumps([]), cmd=cmd_str)

        # restic init → initialize repo
        if "restic" in cmd and "init" in cmd:
            return make_result(0, "created restic repository\n", cmd=cmd_str)

        # restic backup → run backup with JSON summary
        if "restic" in cmd and "backup" in cmd:
            summary = {
                "message_type": "summary",
                "snapshot_id": "abc12345",
                "total_bytes_processed": 1048576,
                "files_new": 5,
                "files_changed": 2,
                "files_unmodified": 0,
                "total_files_processed": 7,
                "total_duration": 1.5,
            }
            # restic outputs progress lines then summary
            stdout = json.dumps({"message_type": "status", "percent_done": 0.5}) + "\n"
            stdout += json.dumps(summary) + "\n"
            return make_result(0, stdout, cmd=cmd_str)

        # restic forget → retention cleanup
        if "restic" in cmd and "forget" in cmd:
            return make_result(0, json.dumps([]), cmd=cmd_str)

        # restic restore
        if "restic" in cmd and "restore" in cmd:
            return make_result(0, "restoring snapshot abc12345\n", cmd=cmd_str)

        # restic unlock
        if "restic" in cmd and "unlock" in cmd:
            return make_result(0, "", cmd=cmd_str)

        # restic ls → list files in snapshot
        if "restic" in cmd and " ls " in cmd_str:
            entries = [
                json.dumps({"name": "file1.txt", "type": "file", "size": 1024, "mtime": "2026-01-01T00:00:00Z"}),
                json.dumps({"name": "backups", "type": "dir", "size": 0, "mtime": "2026-01-01T00:00:00Z"}),
            ]
            return make_result(0, "\n".join(entries) + "\n", cmd=cmd_str)

        # sqlite3 .backup → self-backup
        if "sqlite3" in cmd:
            return make_result(0, "", cmd=cmd_str)

        # Default: success
        return make_result(0, "", cmd=cmd_str)

    return AsyncMock(side_effect=_run)


# ---------------------------------------------------------------------------
# Provider config fixtures — every supported provider
# ---------------------------------------------------------------------------

ALL_PROVIDERS = {
    "local": {
        "name": "Local Backup",
        "type": "local",
        "config": {},  # path set dynamically in test
    },
    "b2": {
        "name": "Backblaze B2",
        "type": "b2",
        "config": {"key_id": "000abc123", "app_key": "K000secretkey123", "bucket": "test-bucket"},
    },
    "s3": {
        "name": "Amazon S3",
        "type": "s3",
        "config": {
            "endpoint": "https://s3.us-east-1.amazonaws.com",
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "bucket": "my-backup-bucket",
        },
    },
    "wasabi": {
        "name": "Wasabi Hot Storage",
        "type": "wasabi",
        "config": {
            "access_key": "WASABI_AK_12345",
            "secret_key": "WASABI_SK_secret",
            "bucket": "wasabi-backup",
            "region": "eu-central-1",
        },
    },
    "sftp": {
        "name": "SFTP Server",
        "type": "sftp",
        "config": {"host": "backup.example.com", "user": "arkive", "password": "s3cret"},
    },
    "dropbox": {
        "name": "Dropbox",
        "type": "dropbox",
        "config": {"token": "sl.ABCDEFghijklmnopqrstuvwxyz"},
    },
    "gdrive": {
        "name": "Google Drive",
        "type": "gdrive",
        "config": {"client_id": "123456.apps.googleusercontent.com", "token": "gtoken123", "folder_id": "1ABCdef"},
    },
}


# ---------------------------------------------------------------------------
# Test client with real CloudManager + mocked run_command
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def provider_client(tmp_path):
    """Test client with CloudManager injected and run_command mocked."""
    config_dir = tmp_path
    os.environ["ARKIVE_CONFIG_DIR"] = str(config_dir)

    import app.core.config as cfg_mod
    import app.core.database as db_mod
    import app.core.dependencies as deps_mod
    from app.core.security import _load_fernet_from_dir, _reset_fernet

    _reset_fernet()
    _load_fernet_from_dir(str(config_dir))

    await db_mod.init_db(config_dir / "arkive.db")

    original_config = cfg_mod.ArkiveConfig

    class TestConfig(original_config):
        def __init__(self, **kwargs):
            kwargs.setdefault("config_dir", config_dir)
            super().__init__(**kwargs)

    original_deps_config = deps_mod._config
    test_config = TestConfig()
    test_config.ensure_dirs()
    deps_mod._config = test_config

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    mock_run = mock_run_command_factory()

    with (
        patch("app.services.scheduler.ArkiveScheduler", MagicMock()),
        patch("app.core.config.ArkiveConfig", TestConfig),
        patch("app.services.cloud_manager.run_command", mock_run),
        patch("app.utils.subprocess_runner.run_command", mock_run),
        patch("app.services.backup_engine.run_command", mock_run),
        patch("app.api.targets.run_command", mock_run, create=True),
    ):
        from app.api.auth import _reset_setup_rate_limit
        from app.main import create_app

        _reset_setup_rate_limit()

        test_app = create_app()
        test_app.router.lifespan_context = _noop_lifespan

        cloud_manager = CloudManager(test_config)
        from app.services.backup_engine import BackupEngine

        backup_engine = BackupEngine(test_config)

        test_app.state.config = test_config
        test_app.state.event_bus = MagicMock()
        test_app.state.docker_client = None
        test_app.state.discovery = None
        test_app.state.db_dumper = None
        test_app.state.flash_backup = None
        test_app.state.backup_engine = backup_engine
        test_app.state.cloud_manager = cloud_manager
        test_app.state.notifier = None
        test_app.state.restore_plan = None
        test_app.state.orchestrator = None
        test_app.state.scheduler = None
        test_app.state.platform = "unraid"

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Run setup to get API key
            setup_resp = await ac.post(
                "/api/auth/setup",
                json={
                    "run_first_backup": False,
                    "encryption_password": "test-restic-password",
                },
            )
            assert setup_resp.status_code in (200, 201), setup_resp.text
            api_key = setup_resp.json()["api_key"]
            ac.headers["X-API-Key"] = api_key

            # Store references for tests
            ac._test_config = test_config
            ac._mock_run = mock_run
            ac._tmp_path = tmp_path

            yield ac

    deps_mod._failed_attempts.clear()
    deps_mod._lockouts.clear()
    deps_mod._config = original_deps_config
    _reset_fernet()
    del os.environ["ARKIVE_CONFIG_DIR"]
