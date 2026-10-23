"""Database management API routes."""

import json

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.dependencies import get_db, get_db_dumper, require_auth
from app.services.lock_manager import BackupLockManager

router = APIRouter(prefix="/databases", tags=["databases"], dependencies=[Depends(require_auth)])


@router.get("")
async def list_databases(
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all discovered databases across containers."""
    cursor = await db.execute("SELECT name, databases FROM discovered_containers")
    rows = await cursor.fetchall()
    all_dbs = []
    for row in rows:
        try:
            dbs = json.loads(row["databases"]) if row["databases"] else []
        except (json.JSONDecodeError, TypeError):
            dbs = []
        for d in dbs:
            if isinstance(d, dict):
                d["container_name"] = d.get("container_name", row["name"])
                all_dbs.append(d)

    total = len(all_dbs)
    paginated = all_dbs[offset:offset + limit]
    return {
        "items": paginated,
        "databases": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.post("/{container_name}/{db_name}/dump")
async def dump_database(
    container_name: str,
    db_name: str,
    request: Request,
    verify_integrity: bool = True,
    db: aiosqlite.Connection = Depends(get_db),
    db_dumper=Depends(get_db_dumper),
):
    """Dump a specific database on-demand."""
    # Find the database info
    cursor = await db.execute(
        "SELECT databases FROM discovered_containers WHERE name = ?",
        (container_name,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, f"Container {container_name} not found")

    dbs = json.loads(row["databases"]) if row["databases"] else []
    target_db = None
    for d in dbs:
        if isinstance(d, dict) and d.get("db_name") == db_name:
            target_db = d
            break

    if not target_db:
        raise HTTPException(404, f"Database {db_name} not found in {container_name}")

    # A dump writes into the shared dump directory; running it while a backup
    # is dumping, pruning and uploading that directory races its cleanup.
    lock_manager = getattr(getattr(request.app.state, "orchestrator", None), "lock_manager", None)
    if not isinstance(lock_manager, BackupLockManager):
        lock_manager = None
    if lock_manager is not None and not lock_manager.acquire_backup_lock(f"manual-dump-{container_name}"):
        raise HTTPException(409, "A backup or restore is running; try the dump again when it finishes")
    try:
        result = await db_dumper.dump_single(
            container_name=container_name,
            db_name=db_name,
            db_type=target_db.get("db_type", ""),
            host_path=target_db.get("host_path"),
            verify_integrity=verify_integrity,
        )
    finally:
        if lock_manager is not None:
            lock_manager.release_backup_lock()

    return {
        "container_name": result.container_name,
        "db_name": result.db_name,
        "db_type": result.db_type,
        "dump_size_bytes": result.dump_size_bytes,
        "integrity_check": result.integrity_check,
        "dump_path": result.dump_path,
        "duration_seconds": result.duration_seconds,
        "status": result.status,
        "error": result.error,
    }
