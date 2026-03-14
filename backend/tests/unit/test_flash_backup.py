"""Unit tests for app.services.flash_backup — FlashBackup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.platform import Platform
from app.services.flash_backup import FlashBackup


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.dump_dir = tmp_path / "dumps"
    config.boot_config_path = tmp_path / "boot-config"
    config.flash_retention = 3
    return config


class TestFlashBackupSkips:
    """Test conditions that cause backup to be skipped."""

    async def test_skip_non_unraid(self, mock_config):
        """Backup is skipped when platform is not UNRAID."""
        fb = FlashBackup(config=mock_config, platform=Platform.LINUX)
        result = await fb.backup()
        assert result.status == "skipped"
        assert "Unraid" in (result.error or "")

    async def test_fail_missing_boot_config_on_unraid(self, mock_config):
        """Missing boot_config_path on Unraid should be a real failure."""
        fb = FlashBackup(config=mock_config, platform=Platform.UNRAID)
        # boot_config_path points to a directory that doesn't exist
        assert not mock_config.boot_config_path.exists()
        result = await fb.backup()
        assert result.status == "failed"
        assert "not found" in (result.error or "").lower()


class TestFlashBackupExecution:
    """Test backup execution and failure paths."""

    @patch("app.services.flash_backup.run_command", new_callable=AsyncMock)
    async def test_successful_backup(self, mock_run_cmd, mock_config):
        """Successful backup returns status='success' with a path containing 'flash_'."""
        # Create the boot config directory so the existence check passes
        mock_config.boot_config_path.mkdir(parents=True, exist_ok=True)

        # First call: tar -czf (success)
        tar_result = MagicMock()
        tar_result.returncode = 0
        tar_result.stdout = ""
        tar_result.stderr = ""

        # Second call: tar -tzf verify (success)
        verify_result = MagicMock()
        verify_result.returncode = 0
        verify_result.stdout = "./file1\n./file2\n"
        verify_result.stderr = ""

        mock_run_cmd.side_effect = [tar_result, verify_result]

        fb = FlashBackup(config=mock_config, platform=Platform.UNRAID)
        result = await fb.backup()

        assert result.status == "success"
        assert "flash_" in result.path

    @patch("app.services.flash_backup.run_command", new_callable=AsyncMock)
    async def test_failed_tar(self, mock_run_cmd, mock_config):
        """Failed tar returns status='failed'."""
        mock_config.boot_config_path.mkdir(parents=True, exist_ok=True)

        tar_result = MagicMock()
        tar_result.returncode = 1
        tar_result.stderr = "tar: error creating archive"

        mock_run_cmd.return_value = tar_result

        fb = FlashBackup(config=mock_config, platform=Platform.UNRAID)
        result = await fb.backup()

        assert result.status == "failed"
        assert mock_run_cmd.call_count == 1


class TestFlashBackupCleanup:
    """Test old backup cleanup logic."""

    @patch("app.services.flash_backup.os.remove")
    @patch("app.services.flash_backup.glob.glob")
    def test_cleanup_old_backups(self, mock_glob, mock_remove, mock_config):
        """Cleanup removes files beyond the retention limit."""
        mock_config.flash_retention = 3
        # Simulate 5 backup files (sorted newest first by glob + reverse=True)
        fake_files = [
            str(mock_config.dump_dir / f"flash_2026010{i}_000000.tar.gz")
            for i in range(5, 0, -1)  # 5, 4, 3, 2, 1 — newest first
        ]
        mock_glob.return_value = fake_files

        fb = FlashBackup(config=mock_config, platform=Platform.UNRAID)
        fb._cleanup_old_backups()

        # Should keep 3 newest, remove 2 oldest
        assert mock_remove.call_count == 2
        removed_files = [call.args[0] for call in mock_remove.call_args_list]
        # The 2 oldest files (index 3 and 4 in the list) should be removed
        assert fake_files[3] in removed_files
        assert fake_files[4] in removed_files
