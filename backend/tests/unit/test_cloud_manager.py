"""
Tests for app.services.cloud_manager — rclone config management.

Adapted from Arkive v2. The v3 CloudManager is class-based, so tests
instantiate CloudManager(config) instead of calling module-level functions.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import ArkiveConfig
from app.services.cloud_manager import CloudManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_mgr(tmp_path):
    """Create a CloudManager with a temp rclone config path."""
    config = ArkiveConfig(config_dir=tmp_path)
    return CloudManager(config)


def _read_rclone_conf(path: Path) -> str:
    """Read rclone config file as text."""
    if path.exists():
        return path.read_text()
    return ""


# ---------------------------------------------------------------------------
# write_target_config
# ---------------------------------------------------------------------------


class TestWriteTargetConfig:
    @pytest.mark.asyncio
    async def test_writes_b2_config(self, cloud_mgr, tmp_path):
        await cloud_mgr.write_target_config(
            {
                "id": "my-b2",
                "type": "b2",
                "config": {"key_id": "kid", "app_key": "ak"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[my-b2]" in content
        assert "type = b2" in content
        assert "account = kid" in content
        assert "key = ak" in content

    @pytest.mark.asyncio
    async def test_writes_s3_config(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "my-s3",
                "type": "s3",
                "config": {
                    "access_key": "AK",
                    "secret_key": "SK",
                    "endpoint": "https://s3.example.com",
                },
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[my-s3]" in content
        assert "type = s3" in content
        assert "access_key_id = AK" in content
        assert "secret_access_key = SK" in content
        assert "endpoint = https://s3.example.com" in content

    @pytest.mark.asyncio
    async def test_multiple_targets(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "t1",
                "type": "b2",
                "config": {"key_id": "k1", "app_key": "a1"},
            }
        )
        await cloud_mgr.write_target_config(
            {
                "id": "t2",
                "type": "s3",
                "config": {"access_key": "ak", "secret_key": "sk"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[t1]" in content
        assert "[t2]" in content

    @pytest.mark.asyncio
    async def test_idempotent_update(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "t",
                "type": "b2",
                "config": {"key_id": "old", "app_key": "old"},
            }
        )
        await cloud_mgr.write_target_config(
            {
                "id": "t",
                "type": "b2",
                "config": {"key_id": "new", "app_key": "new"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "account = new" in content
        # Old values should be gone
        assert "account = old" not in content

    @pytest.mark.asyncio
    async def test_local_target_skips_rclone_config(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "local1",
                "type": "local",
                "config": {"path": "/backups"},
            }
        )
        # No rclone config file should be created for local targets
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[local1]" not in content

    @pytest.mark.asyncio
    async def test_unknown_provider_warns(self, cloud_mgr):
        """Unknown provider type should log warning and skip."""
        await cloud_mgr.write_target_config(
            {
                "id": "unknown-test",
                "type": "unknown_provider",
                "config": {},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[unknown-test]" not in content

    @pytest.mark.asyncio
    async def test_dropbox_section(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "dbx",
                "type": "dropbox",
                "config": {"token": "my-token"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[dbx]" in content
        assert "type = dropbox" in content
        # Bare tokens are wrapped in rclone JSON format
        assert '"access_token": "my-token"' in content
        assert '"token_type": "bearer"' in content

    @pytest.mark.asyncio
    async def test_dropbox_section_json_token(self, cloud_mgr):
        """Pre-formatted JSON tokens are passed through as-is."""
        import json

        token_json = json.dumps({"access_token": "tk", "token_type": "bearer", "refresh_token": "rt"})
        await cloud_mgr.write_target_config(
            {
                "id": "dbx2",
                "type": "dropbox",
                "config": {"token": token_json},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert f"token = {token_json}" in content

    @pytest.mark.asyncio
    async def test_gdrive_section(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "gd",
                "type": "gdrive",
                "config": {"token": "gtoken", "folder_id": "fid", "client_id": "cid", "client_secret": "csec"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[gd]" in content
        assert "type = drive" in content
        assert "root_folder_id = fid" in content
        assert "client_id = cid" in content
        assert "client_secret = csec" in content
        # Bare tokens are wrapped in rclone JSON format
        assert '"access_token": "gtoken"' in content

    @pytest.mark.asyncio
    async def test_sftp_section(self, cloud_mgr):
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OBSCURED", stderr="")
            await cloud_mgr.write_target_config(
                {
                    "id": "sftp1",
                    "type": "sftp",
                    "config": {"host": "h", "username": "u", "password": "pw"},
                }
            )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[sftp1]" in content
        assert "type = sftp" in content
        assert "host = h" in content
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == ["rclone", "obscure", "-"]

    @pytest.mark.asyncio
    async def test_wasabi_section(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "wasabi1",
                "type": "wasabi",
                "config": {"access_key": "WAK", "secret_key": "WSK", "bucket": "my-bucket", "region": "eu-central-1"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[wasabi1]" in content
        assert "type = s3" in content
        assert "provider = Wasabi" in content
        assert "access_key_id = WAK" in content
        assert "secret_access_key = WSK" in content
        assert "endpoint = s3.eu-central-1.wasabisys.com" in content
        assert "region = eu-central-1" in content

    @pytest.mark.asyncio
    async def test_wasabi_section_default_region(self, cloud_mgr):
        await cloud_mgr.write_target_config(
            {
                "id": "wasabi2",
                "type": "wasabi",
                "config": {"access_key": "AK", "secret_key": "SK"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[wasabi2]" in content
        assert "endpoint = s3.us-east-1.wasabisys.com" in content
        assert "region = us-east-1" in content


# ---------------------------------------------------------------------------
# remove_target_config
# ---------------------------------------------------------------------------


class TestRemoveTargetConfig:
    @pytest.mark.asyncio
    async def test_removes_section(self, cloud_mgr):
        # Pre-populate with two targets
        await cloud_mgr.write_target_config(
            {
                "id": "keep",
                "type": "b2",
                "config": {"key_id": "k", "app_key": "a"},
            }
        )
        await cloud_mgr.write_target_config(
            {
                "id": "remove_me",
                "type": "s3",
                "config": {"access_key": "ak", "secret_key": "sk"},
            }
        )

        await cloud_mgr.remove_target_config("remove_me")

        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[remove_me]" not in content
        assert "[keep]" in content

    @pytest.mark.asyncio
    async def test_remove_nonexistent_succeeds(self, cloud_mgr):
        """Removing a non-existent target should not raise."""
        await cloud_mgr.remove_target_config("nope")


# ---------------------------------------------------------------------------
# test_target
# ---------------------------------------------------------------------------


class TestTestTarget:
    @pytest.mark.asyncio
    async def test_local_accessible(self, cloud_mgr, tmp_path):
        target_path = str(tmp_path / "backups")
        os.makedirs(target_path, exist_ok=True)

        result = await cloud_mgr.test_target(
            {
                "id": "local1",
                "type": "local",
                "config": {"path": target_path},
            }
        )
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_local_not_accessible(self, cloud_mgr):
        result = await cloud_mgr.test_target(
            {
                "id": "local1",
                "type": "local",
                "config": {"path": "/nonexistent/path/xyz"},
            }
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_cloud_target_success(self, cloud_mgr):
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="", duration_seconds=0.5)
            result = await cloud_mgr.test_target(
                {
                    "id": "my-remote",
                    "type": "b2",
                    "config": {},
                }
            )
        assert result["status"] == "ok"
        assert "successful" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_cloud_target_failure(self, cloud_mgr):
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error", duration_seconds=1.0)
            result = await cloud_mgr.test_target(
                {
                    "id": "my-remote",
                    "type": "b2",
                    "config": {},
                }
            )
        assert result["status"] == "error"
        assert "auth error" in result["message"]
