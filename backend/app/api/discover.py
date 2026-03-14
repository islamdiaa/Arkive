"""Container discovery API routes."""

import json
from datetime import UTC, datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_db, get_discovery, require_auth
from app.services.discovery_persistence import persist_discovery_results

router = APIRouter(prefix="/discover", tags=["discover"], dependencies=[Depends(require_auth)])


@router.post("/scan")
async def run_scan(
    db: aiosqlite.Connection = Depends(get_db),
    discovery=Depends(get_discovery),
):
    """Run container discovery scan."""
    if discovery is None:
        raise HTTPException(503, "Discovery unavailable — Docker is required for container scanning")
    import time

    start = time.monotonic()
    containers = await discovery.scan()
    duration = round(time.monotonic() - start, 2)

    all_databases = []
    running = 0
    stopped = 0
    for c in containers:
        if c.status == "running":
            running += 1
        else:
            stopped += 1

        all_databases.extend(c.databases)
    await persist_discovery_results(db, containers)
    await db.commit()

    return {
        "total_containers": len(containers),
        "running_containers": running,
        "stopped_containers": stopped,
        "containers": [c.model_dump() for c in containers],
        "databases": [d.model_dump() for d in all_databases],
        "flash_config_found": False,
        "shares": [],
        "scan_duration_seconds": duration,
        "scanned_at": datetime.now(UTC).isoformat(),
    }


@router.get("/containers")
async def list_discovered_containers(
    limit: int = 50,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List previously discovered containers."""
    cursor = await db.execute("SELECT COUNT(*) FROM discovered_containers")
    total = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT * FROM discovered_containers ORDER BY priority, name LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    containers = []
    for row in rows:
        c = dict(row)
        c["ports"] = json.loads(c.get("ports", "[]"))
        c["mounts"] = json.loads(c.get("mounts", "[]"))
        c["databases"] = json.loads(c.get("databases", "[]"))
        containers.append(c)
    return {
        "items": containers,
        "containers": containers,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.get("/databases")
async def list_discovered_databases(
    limit: int = 50,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all discovered databases across all containers."""
    cursor = await db.execute("SELECT name, databases FROM discovered_containers ORDER BY priority, name")
    rows = await cursor.fetchall()
    all_databases = []
    for row in rows:
        container_name = row[0]
        databases = json.loads(row[1] or "[]")
        for db_entry in databases:
            entry = dict(db_entry) if isinstance(db_entry, dict) else db_entry
            entry["container"] = container_name
            all_databases.append(entry)

    total = len(all_databases)
    paginated = all_databases[offset : offset + limit]
    return {
        "items": paginated,
        "databases": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.post("")
async def run_scan_alias(
    db: aiosqlite.Connection = Depends(get_db),
    discovery=Depends(get_discovery),
):
    """Alias for POST /discover/scan — run container discovery scan."""
    return await run_scan(db=db, discovery=discovery)
