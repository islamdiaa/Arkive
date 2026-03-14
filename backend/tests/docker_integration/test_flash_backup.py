"""Phase 3: Flash backup tests with fake Unraid platform."""

import glob
import tarfile

import pytest

from app.core.platform import Platform
from app.services.flash_backup import FlashBackup


@pytest.mark.asyncio
class TestFlashBackup:
    """Flash backup with fake boot-config directory."""

    async def test_flash_backup_success(self, tmp_config, fake_boot_config):
        """Unraid platform + valid boot dir → success with valid tar.gz."""
        tmp_config.boot_config_path = fake_boot_config
        fb = FlashBackup(tmp_config, Platform.UNRAID)

        result = await fb.backup()

        assert result.status == "success"
        assert result.size_bytes > 0
        assert result.path.endswith(".tar.gz")
        # Verify the archive is valid and contains our files
        with tarfile.open(result.path, "r:gz") as tar:
            names = tar.getnames()
            assert any("ident.cfg" in n for n in names)
            assert any("go" in n for n in names)
            assert any("syslinux" in n for n in names)

    async def test_flash_backup_skipped_not_unraid(self, tmp_config, fake_boot_config):
        """LINUX platform → skipped."""
        tmp_config.boot_config_path = fake_boot_config
        fb = FlashBackup(tmp_config, Platform.LINUX)

        result = await fb.backup()

        assert result.status == "skipped"
        assert "Unraid" in (result.error or "")

    async def test_flash_backup_failed_no_boot_dir(self, tmp_config):
        """UNRAID but missing boot dir → failed for operator visibility."""
        tmp_config.boot_config_path = tmp_config.config_dir / "nonexistent-boot"
        fb = FlashBackup(tmp_config, Platform.UNRAID)

        result = await fb.backup()

        assert result.status == "failed"
        assert "not found" in (result.error or "")

    async def test_flash_backup_retention(self, tmp_config, fake_boot_config):
        """Create more backups than retention limit, verify old ones cleaned up."""
        from datetime import datetime
        from unittest.mock import patch

        tmp_config.boot_config_path = fake_boot_config
        tmp_config.flash_retention = 3
        fb = FlashBackup(tmp_config, Platform.UNRAID)

        # Create 5 backups with mocked unique timestamps
        paths = []
        for i in range(5):
            fake_dt = datetime(2026, 1, 1, 0, 0, i)
            with patch("app.services.flash_backup.datetime") as mock_dt:
                mock_dt.utcnow.return_value = fake_dt
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                result = await fb.backup()
            assert result.status == "success"
            paths.append(result.path)

        # Only 3 should remain (retention = 3)
        remaining = sorted(glob.glob(str(tmp_config.dump_dir / "flash_*.tar.gz")))
        assert len(remaining) == 3
