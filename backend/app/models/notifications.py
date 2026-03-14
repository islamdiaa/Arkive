"""Pydantic models for notification channels."""

from typing import Literal

from pydantic import BaseModel, Field

VALID_NOTIFICATION_TYPES = {
    "slack",
    "discord",
    "telegram",
    "email",
    "ntfy",
    "gotify",
    "pushover",
    "webhook",
    "uptimekuma",
}


class NotificationChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    type: Literal["slack", "discord", "telegram", "email", "ntfy", "gotify", "pushover", "webhook", "uptimekuma"]
    url: str = Field(..., min_length=1)
    events: list[str] = ["backup.success", "backup.failed"]
    enabled: bool = True


class NotificationChannelUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


class NotificationChannelResponse(BaseModel):
    id: str
    type: str
    name: str
    enabled: bool
    events: list[str]
    last_sent: str | None = None
    last_status: str | None = None
    created_at: str
