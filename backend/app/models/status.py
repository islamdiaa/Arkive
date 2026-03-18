"""Pydantic models for system status."""

from pydantic import BaseModel, Field


class StatusResponse(BaseModel):
    """Full status response matching all fields the frontend dashboard reads.

    The dashboard reads both structured sub-objects (targets, databases, storage)
    and flat convenience fields (containers_discovered, databases_found, etc.).
    Both must be present so the frontend helper functions can resolve values.
    """

    # Core identity
    version: str = "0.0.0"
    uptime_seconds: int = 0
    setup_completed: bool = False
    platform: str = "linux"
    hostname: str = ""

    # Overall health
    status: str = "ok"
    health: str = "healthy"

    # Flat aggregate counts the dashboard reads directly
    containers_discovered: int = 0
    databases_found: int = 0
    targets_configured: int = 0
    total_snapshots: int = 0
    storage_used_bytes: int = 0

    # Structured sub-objects
    targets: dict = Field(default_factory=lambda: {"total": 0, "healthy": 0})
    databases: dict = Field(default_factory=lambda: {"total": 0, "healthy": 0})
    storage: dict = Field(default_factory=lambda: {"total_bytes": 0})

    # Backup timing
    last_backup: dict | None = None
    last_backup_status: str | None = None
    next_backup: str | None = None

    # Health checks
    checks: dict = Field(default_factory=dict)

    # Verification / Trust Score
    trust_score: int = 0
    last_verified_at: str | None = None
    verification_status: dict | None = None

    # Coverage
    coverage: dict | None = None
