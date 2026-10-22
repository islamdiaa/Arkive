"""Pydantic models for notification channels."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.notification_events import DEFAULT_EVENTS, EVENT_TYPES

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(value: str | None) -> str | None:
    if value is not None and _CONTROL_CHARS_RE.search(value):
        raise ValueError("must not contain control characters")
    return value


def _validate_events(events: list[str] | None) -> list[str] | None:
    if events is None:
        return None
    unknown = sorted({e for e in events if e not in EVENT_TYPES and e != "*"})
    if unknown:
        raise ValueError(f"unknown event types: {', '.join(unknown)}")
    return list(dict.fromkeys(events))


VALID_NOTIFICATION_TYPES = {"slack", "discord", "telegram", "email", "ntfy", "gotify", "pushover", "webhook", "uptimekuma"}


class NotificationChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    type: Literal["slack", "discord", "telegram", "email", "ntfy", "gotify", "pushover", "webhook", "uptimekuma"]
    url: str = Field(..., min_length=1)
    events: list[str] = Field(default_factory=lambda: list(DEFAULT_EVENTS))
    enabled: bool = True

    @field_validator("name", "url")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        return _reject_control_chars(v.strip())

    @field_validator("events")
    @classmethod
    def _known_events(cls, v: list[str]) -> list[str]:
        return _validate_events(v)


class NotificationChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    url: str | None = Field(default=None, min_length=1)
    events: list[str] | None = None
    enabled: bool | None = None

    @field_validator("name", "url")
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        return _reject_control_chars(v.strip() if v is not None else None)

    @field_validator("events")
    @classmethod
    def _known_events(cls, v: list[str] | None) -> list[str] | None:
        return _validate_events(v)


class NotificationChannelResponse(BaseModel):
    id: str
    type: str
    name: str
    enabled: bool
    events: list[str]
    last_sent: str | None = None
    last_status: str | None = None
    created_at: str
