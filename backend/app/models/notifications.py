"""Pydantic models for notification channels."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(value: str | None) -> str | None:
    if value is not None and _CONTROL_CHARS_RE.search(value):
        raise ValueError("must not contain control characters")
    return value

VALID_NOTIFICATION_TYPES = {"slack", "discord", "telegram", "email", "ntfy", "gotify", "pushover", "webhook", "uptimekuma"}


class NotificationChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    type: Literal["slack", "discord", "telegram", "email", "ntfy", "gotify", "pushover", "webhook", "uptimekuma"]
    url: str = Field(..., min_length=1)
    events: list[str] = ["backup.success", "backup.failed"]
    enabled: bool = True

    @field_validator("name", "url")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        return _reject_control_chars(v.strip())


class NotificationChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    url: str | None = Field(default=None, min_length=1)
    events: list[str] | None = None
    enabled: bool | None = None

    @field_validator("name", "url")
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        return _reject_control_chars(v.strip() if v is not None else None)


class NotificationChannelResponse(BaseModel):
    id: str
    type: str
    name: str
    enabled: bool
    events: list[str]
    last_sent: str | None = None
    last_status: str | None = None
    created_at: str
