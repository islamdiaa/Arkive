"""Pydantic models for container/database discovery."""

from pydantic import BaseModel


class DiscoveredContainer(BaseModel):
    name: str
    image: str
    status: str
    databases: list["DiscoveredDatabase"] = []
    profile: str | None = None
    priority: str = "medium"
    ports: list[str] = []
    mounts: list[dict] = []
    compose_project: str | None = None
    # Coverage problems found while scanning (e.g. Postgres databases could not
    # be listed, so only the configured one is backed up). Not persisted.
    warnings: list[str] = []


class DiscoveredDatabase(BaseModel):
    container_name: str
    db_type: str  # postgres, sqlite, mysql, mariadb, mongodb, redis
    db_name: str
    host_path: str | None = None


class DiscoverResponse(BaseModel):
    total_containers: int
    running_containers: int
    stopped_containers: int
    containers: list[DiscoveredContainer]
    databases: list[DiscoveredDatabase]
    flash_config_found: bool
    shares: list[str]
    scan_duration_seconds: float
    scanned_at: str


class DirectoryScanResult(BaseModel):
    path: str
    label: str
    size_bytes: int | None = None
    file_count: int | None = None
    priority: str = "optional"
    recommended_excludes: list[str] = []


class DirectoryCreate(BaseModel):
    path: str
    label: str
    exclude_patterns: list[str] = []
    enabled: bool = True


class DirectoryResponse(BaseModel):
    id: str
    path: str
    label: str
    exclude_patterns: list[str]
    enabled: bool
    size_bytes: int | None = None
    file_count: int | None = None
    last_scanned: str | None = None


class DumpDatabaseRequest(BaseModel):
    verify_integrity: bool = True


class DumpDatabaseResponse(BaseModel):
    container_name: str
    db_name: str
    db_type: str
    dump_size_bytes: int
    integrity_check: str  # ok, failed, skipped
    dump_path: str
    duration_seconds: float
