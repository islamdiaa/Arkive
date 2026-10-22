"""Pydantic models for settings."""

from typing import Any

from pydantic import BaseModel


class SettingsResponse(BaseModel):
    server_name: str = ""
    timezone: str = "UTC"
    retention_days: int = 30
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6
    log_level: str = "INFO"
    web_url: str = ""
    bandwidth_limit: str = ""


class SettingsUpdate(BaseModel):
    server_name: str | None = None
    timezone: str | None = None
    retention_days: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    log_level: str | None = None
    web_url: str | None = None
    bandwidth_limit: str | None = None


class SetupCompleteRequest(BaseModel):
    encryption_password: str = ""
    storage: dict[str, Any] = {}
    db_dump_schedule: str = "0 6,18 * * *"
    cloud_sync_schedule: str = "0 7 * * *"
    flash_schedule: str = "0 5 * * *"
    directories: list[str] = []
    run_first_backup: bool = False
    target_ids: list[str] = []
    directory_ids: list[str] = []
    schedules: dict[str, str] = {}
