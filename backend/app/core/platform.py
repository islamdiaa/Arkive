"""Platform detection — Unraid vs generic Linux."""

import os
from enum import Enum
from pathlib import Path


class Platform(Enum):
    UNRAID = "unraid"
    LINUX = "linux"
    UNKNOWN = "unknown"


def _looks_like_unraid_flash(boot_config_path: Path) -> bool:
    """Heuristically identify an Unraid flash mount exposed into the container."""
    config_dir = boot_config_path / "config"
    if not config_dir.is_dir():
        return False
    return any((config_dir / marker).exists() for marker in ("super.dat", "go", "plugins"))


def detect_platform() -> Platform:
    if os.path.exists("/etc/unraid-version"):
        return Platform.UNRAID
    boot_config_path = Path(os.environ.get("ARKIVE_BOOT_CONFIG_PATH", "/boot-config"))
    if _looks_like_unraid_flash(boot_config_path):
        return Platform.UNRAID
    if os.path.exists("/etc/os-release"):
        return Platform.LINUX
    return Platform.UNKNOWN


def get_platform_features(platform: Platform) -> dict:
    return {
        Platform.UNRAID: {"flash_backup": True, "share_detection": True, "tmpfs_root": True},
        Platform.LINUX: {"flash_backup": False, "share_detection": False, "tmpfs_root": False},
        Platform.UNKNOWN: {"flash_backup": False, "share_detection": False, "tmpfs_root": False},
    }[platform]
