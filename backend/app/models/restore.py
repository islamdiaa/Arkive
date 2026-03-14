"""Pydantic models for restore operations."""

import os
import re
from pathlib import Path

from pydantic import BaseModel, field_validator, model_validator


def _allowed_restore_roots() -> tuple[Path, ...]:
    config_dir = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
    return (
        config_dir / "restores",
        Path("/tmp/arkive-restore"),  # nosec B108 - explicit allowlisted restore root
        Path("/var/tmp/arkive-restore"),  # nosec B108 - explicit allowlisted restore root
    )


def _is_within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


class RestoreRequest(BaseModel):
    snapshot_id: str
    target: str
    paths: list[str]
    restore_to: str | None = None
    overwrite: bool = False
    dry_run: bool = False

    @field_validator("snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("Invalid snapshot ID format: only alphanumeric, hyphens, and underscores allowed")
        return v

    @field_validator("restore_to")
    @classmethod
    def validate_restore_to(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Must be absolute path
        if not v.startswith("/"):
            raise ValueError("restore_to must be an absolute path")
        # Block path traversal
        import os

        normalized = os.path.normpath(v)
        # Block writes to sensitive system directories
        blocked_prefixes = (
            "/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/boot",
            "/proc",
            "/sys",
            "/dev",
            "/var/run",
            "/root",
        )
        for prefix in blocked_prefixes:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                raise ValueError(f"restore_to cannot target system directory: {prefix}")
        candidate = Path(normalized)
        if not any(_is_within_root(candidate, root) for root in _allowed_restore_roots()):
            allowed_roots = ", ".join(str(root) for root in _allowed_restore_roots())
            raise ValueError(f"restore_to must be inside a safe restore root: {allowed_roots}")
        return normalized

    @model_validator(mode="after")
    def validate_restore_mode(self) -> "RestoreRequest":
        if not self.dry_run and not self.restore_to:
            raise ValueError("restore_to is required for non-dry-run restores")
        return self

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, v: list[str]) -> list[str]:
        for p in v:
            # Block path traversal sequences in restore paths
            if ".." in p:
                raise ValueError("Path traversal (..) not allowed in restore paths")
        return v


class RestoreResponse(BaseModel):
    restore_id: str
    status: str
    snapshot_id: str
    files_restored: int = 0
    message: str = ""


class RestoreTestResult(BaseModel):
    status: str  # pass, fail
    file: str
    expected_hash: str
    actual_hash: str
    tested_at: str
    snapshot_id: str


class BrowseResponse(BaseModel):
    path: str
    entries: list["FileEntry"]


class FileEntry(BaseModel):
    name: str
    type: str  # file, directory
    size: int | None = None
    modified: str | None = None
