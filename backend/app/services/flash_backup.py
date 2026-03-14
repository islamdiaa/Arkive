"""Flash backup engine for Unraid boot config."""

import glob
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import ArkiveConfig
from app.core.platform import Platform
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.flash_backup")


@dataclass
class FlashBackupResult:
    status: str  # success, failed, skipped
    path: str = ""
    size_bytes: int = 0
    error: str | None = None


class FlashBackup:
    """Backs up Unraid flash drive config."""

    def __init__(self, config: ArkiveConfig, platform: Platform):
        self.config = config
        self.platform = platform
        self.dump_dir = config.dump_dir

    async def backup(self) -> FlashBackupResult:
        """Create a tar.gz backup of /boot-config."""
        if self.platform != Platform.UNRAID:
            return FlashBackupResult(status="skipped", error="Not running on Unraid")

        boot_config = self.config.boot_config_path
        if not boot_config.exists():
            return FlashBackupResult(
                status="failed",
                error=f"Flash config not found: {boot_config}",
            )

        self.dump_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_path = str(self.dump_dir / f"flash_{timestamp}.tar.gz")

        result = await run_command(["tar", "-czf", output_path, "-C", str(boot_config), "."])

        if result.returncode != 0:
            return FlashBackupResult(status="failed", error=result.stderr)

        # Verify archive
        verify = await run_command(["tar", "-tzf", output_path])
        if verify.returncode != 0:
            return FlashBackupResult(status="failed", path=output_path, error="Archive verification failed")

        size = os.path.getsize(output_path) if os.path.exists(output_path) else 0

        # Retention: keep last N
        self._cleanup_old_backups()

        return FlashBackupResult(status="success", path=output_path, size_bytes=size)

    def _cleanup_old_backups(self) -> None:
        """Remove old flash backups beyond retention limit."""
        pattern = str(self.dump_dir / "flash_*.tar.gz")
        files = sorted(glob.glob(pattern), reverse=True)
        for old_file in files[self.config.flash_retention :]:
            try:
                os.remove(old_file)
                logger.info("Removed old flash backup: %s", old_file)
            except OSError as e:
                logger.warning("Failed to remove old flash backup %s: %s", old_file, e)
