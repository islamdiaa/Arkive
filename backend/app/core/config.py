"""Arkive configuration with YAML + env var support."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_yaml_config() -> dict[str, Any]:
    """Load config.yaml from config dir (respects ARKIVE_CONFIG_DIR env var)."""
    config_dir = os.environ.get("ARKIVE_CONFIG_DIR", "/config")
    yaml_path = Path(config_dir) / "config.yaml"
    if yaml_path.exists():
        return yaml.safe_load(yaml_path.read_text()) or {}
    return {}


class _YamlSettingsSource:
    """Pydantic-settings compatible YAML source."""

    def __init__(self, settings_cls: type[BaseSettings]):
        pass

    def __call__(self) -> dict[str, Any]:
        return _load_yaml_config()


class ArkiveConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARKIVE_")

    config_dir: Path = Path("/config")
    port: int = 8200
    log_level: str = "INFO"
    dev_mode: bool = False
    puid: int = Field(default=99, validation_alias="PUID")
    pgid: int = Field(default=100, validation_alias="PGID")
    boot_config_path: Path = Path("/boot-config")
    user_shares_path: Path = Path("/mnt/user")
    profiles_dir: Path = Path("/app/profiles")
    flash_retention: int = 7
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:8200",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8200",
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def db_path(self) -> Path:
        return self.config_dir / "arkive.db"

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def rclone_config(self) -> Path:
        return self.config_dir / "rclone.conf"

    @property
    def dump_dir(self) -> Path:
        return self.config_dir / "dumps"

    @property
    def restore_dir(self) -> Path:
        return self.config_dir / "restores"

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["env_settings"],
            _YamlSettingsSource(settings_cls),
            kwargs["init_settings"],
        )

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        for d in [self.config_dir, self.log_dir, self.dump_dir, self.restore_dir]:
            d.mkdir(parents=True, exist_ok=True)
