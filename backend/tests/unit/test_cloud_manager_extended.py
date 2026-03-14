"""
Extended unit tests for app.services.cloud_manager.

Covers rclone config generation details for all providers, test_target edge
cases, and remove_target_config idempotency.
"""

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


# ===========================================================================
# B2 rclone config
# ===========================================================================


class TestRcloneConfigB2:
    @pytest.mark.asyncio
    async def test_rclone_config_b2_has_all_fields(self, cloud_mgr):
        """B2 config section must contain account (key_id), key (app_key), and type."""
        await cloud_mgr.write_target_config(
            {
                "id": "b2-full",
                "type": "b2",
                "config": {"key_id": "my-key-id", "app_key": "my-app-key", "bucket": "bkt"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[b2-full]" in content
        assert "type = b2" in content
        assert "account = my-key-id" in content
        assert "key = my-app-key" in content

    @pytest.mark.asyncio
    async def test_rclone_config_b2_bucket_not_in_rclone(self, cloud_mgr):
        """B2 bucket is used by restic, not stored in rclone config section."""
        await cloud_mgr.write_target_config(
            {
                "id": "b2-nobkt",
                "type": "b2",
                "config": {"key_id": "kid", "app_key": "ak", "bucket": "my-bucket"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        # bucket is not a rclone config key for b2
        assert "bucket" not in content.lower() or "my-bucket" not in content


# ===========================================================================
# S3 rclone config
# ===========================================================================


class TestRcloneConfigS3:
    @pytest.mark.asyncio
    async def test_rclone_config_s3_with_custom_endpoint(self, cloud_mgr):
        """S3 config should include the custom endpoint."""
        await cloud_mgr.write_target_config(
            {
                "id": "s3-ep",
                "type": "s3",
                "config": {
                    "access_key": "AK",
                    "secret_key": "SK",
                    "endpoint": "https://custom-s3.example.com",
                },
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[s3-ep]" in content
        assert "type = s3" in content
        assert "endpoint = https://custom-s3.example.com" in content

    @pytest.mark.asyncio
    async def test_rclone_config_s3_default_region(self, cloud_mgr):
        """S3 config without provider defaults to 'Other'."""
        await cloud_mgr.write_target_config(
            {
                "id": "s3-def",
                "type": "s3",
                "config": {"access_key": "AK", "secret_key": "SK"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "provider = Other" in content

    @pytest.mark.asyncio
    async def test_rclone_config_s3_env_auth_false(self, cloud_mgr):
        """S3 config should explicitly set env_auth = false."""
        await cloud_mgr.write_target_config(
            {
                "id": "s3-env",
                "type": "s3",
                "config": {"access_key": "AK", "secret_key": "SK"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "env_auth = false" in content

    @pytest.mark.asyncio
    async def test_rclone_config_s3_maps_key_names(self, cloud_mgr):
        """S3 maps access_key -> access_key_id and secret_key -> secret_access_key."""
        await cloud_mgr.write_target_config(
            {
                "id": "s3-keys",
                "type": "s3",
                "config": {"access_key": "MY_AK", "secret_key": "MY_SK"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "access_key_id = MY_AK" in content
        assert "secret_access_key = MY_SK" in content


# ===========================================================================
# Wasabi rclone config
# ===========================================================================


class TestRcloneConfigWasabi:
    @pytest.mark.asyncio
    async def test_rclone_config_wasabi_endpoint_matches_region(self, cloud_mgr):
        """Wasabi endpoint should contain the region in the hostname."""
        await cloud_mgr.write_target_config(
            {
                "id": "wasabi-eu",
                "type": "wasabi",
                "config": {
                    "access_key": "WAK",
                    "secret_key": "WSK",
                    "bucket": "b",
                    "region": "eu-central-1",
                },
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "endpoint = s3.eu-central-1.wasabisys.com" in content
        assert "region = eu-central-1" in content
        assert "provider = Wasabi" in content

    @pytest.mark.asyncio
    async def test_rclone_config_wasabi_default_region_us_east_1(self, cloud_mgr):
        """Wasabi without explicit region defaults to us-east-1."""
        await cloud_mgr.write_target_config(
            {
                "id": "wasabi-def",
                "type": "wasabi",
                "config": {"access_key": "WAK", "secret_key": "WSK"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "endpoint = s3.us-east-1.wasabisys.com" in content
        assert "region = us-east-1" in content

    @pytest.mark.asyncio
    async def test_rclone_config_wasabi_uses_s3_type(self, cloud_mgr):
        """Wasabi is S3-compatible, so rclone type should be 's3'."""
        await cloud_mgr.write_target_config(
            {
                "id": "wasabi-type",
                "type": "wasabi",
                "config": {"access_key": "WAK", "secret_key": "WSK"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "type = s3" in content


# ===========================================================================
# SFTP rclone config
# ===========================================================================


class TestRcloneConfigSFTP:
    @pytest.mark.asyncio
    async def test_rclone_config_sftp_with_port(self, cloud_mgr):
        """SFTP config should include the port."""
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OBS_PW", stderr="")
            await cloud_mgr.write_target_config(
                {
                    "id": "sftp-port",
                    "type": "sftp",
                    "config": {"host": "nas.local", "username": "root", "password": "pw", "port": "2222"},
                }
            )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[sftp-port]" in content
        assert "type = sftp" in content
        assert "port = 2222" in content
        assert "host = nas.local" in content

    @pytest.mark.asyncio
    async def test_rclone_config_sftp_default_port(self, cloud_mgr):
        """SFTP config without port defaults to 22."""
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OBS_PW", stderr="")
            await cloud_mgr.write_target_config(
                {
                    "id": "sftp-defport",
                    "type": "sftp",
                    "config": {"host": "nas.local", "username": "root", "password": "pw"},
                }
            )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "port = 22" in content

    @pytest.mark.asyncio
    async def test_rclone_config_sftp_with_key_file(self, cloud_mgr):
        """SFTP config should include the key_file path if provided.

        Note: The current _sftp_section does not write key_file to config.
        This test documents the current behavior -- key_file is passed in
        config but not written to the rclone section.
        """
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            await cloud_mgr.write_target_config(
                {
                    "id": "sftp-key",
                    "type": "sftp",
                    "config": {
                        "host": "nas.local",
                        "username": "root",
                        "key_file": "/root/.ssh/id_rsa",
                    },
                }
            )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[sftp-key]" in content
        assert "type = sftp" in content
        assert "host = nas.local" in content

    @pytest.mark.asyncio
    async def test_rclone_config_sftp_obscures_password(self, cloud_mgr):
        """SFTP password should be obscured via rclone obscure command."""
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OBSCURED_PW_VALUE", stderr="")
            await cloud_mgr.write_target_config(
                {
                    "id": "sftp-obs",
                    "type": "sftp",
                    "config": {"host": "h", "username": "u", "password": "cleartext"},
                }
            )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "pass = OBSCURED_PW_VALUE" in content
        # Verify rclone obscure was called with stdin mode and password via stdin
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["rclone", "obscure", "-"]
        # Password should be passed via input_data (stdin), not as a CLI argument
        assert mock_run.call_args[1].get("input_data") == "cleartext"


# ===========================================================================
# Dropbox rclone config
# ===========================================================================


class TestRcloneConfigDropbox:
    @pytest.mark.asyncio
    async def test_rclone_config_dropbox_token_in_config(self, cloud_mgr):
        """Dropbox config should store the token in rclone OAuth JSON format."""
        await cloud_mgr.write_target_config(
            {
                "id": "dbx-tok",
                "type": "dropbox",
                "config": {"token": "sl.fake-dropbox-long-token"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[dbx-tok]" in content
        assert "type = dropbox" in content
        # _format_oauth_token wraps bare tokens in rclone JSON format
        assert "sl.fake-dropbox-long-token" in content
        assert "access_token" in content


# ===========================================================================
# Google Drive rclone config
# ===========================================================================


class TestRcloneConfigGDrive:
    @pytest.mark.asyncio
    async def test_rclone_config_gdrive_token_in_config(self, cloud_mgr):
        """Google Drive config should store the token in rclone OAuth JSON format."""
        await cloud_mgr.write_target_config(
            {
                "id": "gd-tok",
                "type": "gdrive",
                "config": {"token": "ya29.fake-google-token"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[gd-tok]" in content
        assert "type = drive" in content
        # _format_oauth_token wraps bare tokens in rclone JSON format
        assert "ya29.fake-google-token" in content
        assert "access_token" in content

    @pytest.mark.asyncio
    async def test_rclone_config_gdrive_with_folder_id(self, cloud_mgr):
        """Google Drive config with folder_id should set root_folder_id."""
        await cloud_mgr.write_target_config(
            {
                "id": "gd-fld",
                "type": "gdrive",
                "config": {"token": "tok", "folder_id": "1A2B3C"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "root_folder_id = 1A2B3C" in content

    @pytest.mark.asyncio
    async def test_rclone_config_gdrive_without_folder_id(self, cloud_mgr):
        """Google Drive config without folder_id should not set root_folder_id."""
        await cloud_mgr.write_target_config(
            {
                "id": "gd-nofld",
                "type": "gdrive",
                "config": {"token": "tok"},
            }
        )
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "root_folder_id" not in content


# ===========================================================================
# test_target edge cases
# ===========================================================================


class TestTestTargetEdgeCases:
    @pytest.mark.asyncio
    async def test_test_target_local_nonexistent(self, cloud_mgr):
        """test_target for a local path that does not exist returns error."""
        result = await cloud_mgr.test_target(
            {
                "id": "local-bad",
                "type": "local",
                "config": {"path": "/nonexistent/path/abc123xyz"},
            }
        )
        assert result["status"] == "error"
        assert "not accessible" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_test_target_local_writable(self, cloud_mgr, tmp_path):
        """test_target for a writable local path returns ok."""
        target_dir = tmp_path / "backups"
        target_dir.mkdir()
        result = await cloud_mgr.test_target(
            {
                "id": "local-ok",
                "type": "local",
                "config": {"path": str(target_dir)},
            }
        )
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_test_target_cloud_command_failure(self, cloud_mgr):
        """test_target for a cloud target with rclone failure returns error."""
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Failed to create file system: authentication failed",
                duration_seconds=2.0,
            )
            result = await cloud_mgr.test_target(
                {
                    "id": "cloud-fail",
                    "type": "b2",
                    "config": {},
                }
            )
        assert result["status"] == "error"
        assert "authentication failed" in result["message"]

    @pytest.mark.asyncio
    async def test_test_target_cloud_command_success(self, cloud_mgr):
        """test_target for a cloud target with rclone success returns ok."""
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="directory listing output",
                stderr="",
                duration_seconds=0.3,
            )
            result = await cloud_mgr.test_target(
                {
                    "id": "cloud-ok",
                    "type": "s3",
                    "config": {},
                }
            )
        assert result["status"] == "ok"
        assert "successful" in result["message"].lower()
        assert result["latency_ms"] == 300

    @pytest.mark.asyncio
    async def test_test_target_cloud_uses_rclone_lsd(self, cloud_mgr):
        """test_target for cloud targets should invoke 'rclone lsd <id>:'."""
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="", duration_seconds=0.1)
            await cloud_mgr.test_target(
                {
                    "id": "test-cmd",
                    "type": "wasabi",
                    "config": {},
                }
            )
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["rclone", "lsd", "test-cmd:"]


# ===========================================================================
# remove_target_config idempotency
# ===========================================================================


class TestRemoveConfigIdempotent:
    @pytest.mark.asyncio
    async def test_remove_config_section_idempotent(self, cloud_mgr):
        """Removing a non-existent section should not raise any error."""
        # First call on empty config -- should not raise
        await cloud_mgr.remove_target_config("does-not-exist")

        # Add a target, then remove a different non-existent one
        await cloud_mgr.write_target_config(
            {
                "id": "keep-me",
                "type": "b2",
                "config": {"key_id": "k", "app_key": "a"},
            }
        )
        await cloud_mgr.remove_target_config("also-not-here")

        # The existing target should still be present
        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[keep-me]" in content

    @pytest.mark.asyncio
    async def test_remove_config_twice(self, cloud_mgr):
        """Removing the same section twice should be harmless."""
        await cloud_mgr.write_target_config(
            {
                "id": "rm-twice",
                "type": "b2",
                "config": {"key_id": "k", "app_key": "a"},
            }
        )
        await cloud_mgr.remove_target_config("rm-twice")
        # Second removal -- no error
        await cloud_mgr.remove_target_config("rm-twice")

        content = _read_rclone_conf(cloud_mgr.rclone_config)
        assert "[rm-twice]" not in content
