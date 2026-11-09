"""Request models for guided (in-place) database restores."""

import re

from pydantic import BaseModel, Field, field_validator

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SNAPSHOT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _no_control_chars(value: str | None) -> str | None:
    if value is not None and _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    return value


class GuidedRestoreSelection(BaseModel):
    """One discovered database and the snapshot to restore it from."""

    container_name: str = Field(min_length=1, max_length=255)
    db_type: str = Field(min_length=1, max_length=32)
    db_name: str = Field(min_length=1, max_length=255)
    host_path: str | None = Field(default=None, max_length=4096)
    snapshot_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)

    @field_validator("container_name", "db_type", "db_name", "host_path", "target_id")
    @classmethod
    def _plain_text(cls, value: str | None) -> str | None:
        return _no_control_chars(value)

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        # Reaches restic as an argument: no leading "-" (option), no specials.
        if not _SNAPSHOT_ID.match(value):
            raise ValueError("Invalid snapshot ID format: only letters, digits, '-' and '_' allowed")
        return value


class GuidedRestoreStart(GuidedRestoreSelection):
    # Must equal the confirmation phrase from the preview (the database name).
    confirm: str = Field(default="", max_length=512)
    # Containers (apps using the database) to stop during the load and start again after.
    stop_containers: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("stop_containers")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        for name in value:
            _no_control_chars(name)
        return list(dict.fromkeys(value))


class GuidedRestoreUndo(BaseModel):
    confirm: str = Field(default="", max_length=512)
    # None: stop the same apps the original restore stopped.
    stop_containers: list[str] | None = Field(default=None, max_length=50)

    @field_validator("stop_containers")
    @classmethod
    def _names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for name in value:
            _no_control_chars(name)
        return list(dict.fromkeys(value))
