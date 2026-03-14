"""Snapshot browsing API routes."""

import json
from datetime import date

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import get_backup_engine, get_config, get_db, require_auth
from app.core.security import decrypt_config

router = APIRouter(prefix="/snapshots", tags=["snapshots"], dependencies=[Depends(require_auth)])


@router.get("")
async def list_snapshots(
    target_id: str | None = None,
    target: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: aiosqlite.Connection = Depends(get_db),
    backup_engine=Depends(get_backup_engine),
):
    """List cached snapshots, optionally filtered by target."""
    # Accept both 'target_id' and 'target' query params for compatibility
    effective_target = target_id or target

    if effective_target:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM snapshots WHERE target_id = ?",
            (effective_target,),
        )
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT * FROM snapshots WHERE target_id = ? ORDER BY time DESC LIMIT ? OFFSET ?",
            (effective_target, limit, offset),
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM snapshots")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT * FROM snapshots ORDER BY time DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    rows = await cursor.fetchall()
    snapshots = []
    for row in rows:
        s = dict(row)
        s["paths"] = json.loads(s.get("paths", "[]"))
        s["tags"] = json.loads(s.get("tags", "[]"))
        snapshots.append(s)
    return {
        "items": snapshots,
        "snapshots": snapshots,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.post("/refresh")
async def refresh_snapshots(
    target_id: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    backup_engine=Depends(get_backup_engine),
    config=Depends(get_config),
):
    """Refresh snapshot cache from restic repositories."""
    if target_id:
        cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        targets = [dict(row)] if row else []
    else:
        cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
        targets = [dict(t) for t in await cursor.fetchall()]

    total = 0
    for target in targets:
        target["config"] = decrypt_config(target.get("config", "{}"), str(config.config_dir))
        snapshots = await backup_engine.snapshots(target)
        total_size = 0
        # Upsert into cache
        for snap in snapshots:
            short_id = snap.get("short_id", snap.get("id", "")[:8])
            snapshot_size = snap.get("size", 0)
            await db.execute(
                """INSERT OR REPLACE INTO snapshots (id, target_id, full_id, time, hostname, paths, tags, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    short_id,
                    target["id"],
                    snap.get("id", ""),
                    snap.get("time", ""),
                    snap.get("hostname", ""),
                    json.dumps(snap.get("paths", [])),
                    json.dumps(snap.get("tags", [])),
                    snapshot_size,
                ),
            )
            total_size += snapshot_size
        total += len(snapshots)

        # Update target stats
        await db.execute(
            "UPDATE storage_targets SET snapshot_count = ?, total_size_bytes = ? WHERE id = ?",
            (len(snapshots), total_size, target["id"]),
        )
        await db.execute(
            """INSERT OR REPLACE INTO size_history
               (date, target_id, total_size_bytes, snapshot_count)
               VALUES (?, ?, ?, ?)""",
            (date.today().isoformat(), target["id"], total_size, len(snapshots)),
        )

    await db.commit()
    return {"refreshed": total}


@router.get("/{snapshot_id}", response_model=None)
async def get_snapshot(
    snapshot_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get a single snapshot by short ID or full ID."""
    cursor = await db.execute(
        "SELECT * FROM snapshots WHERE id = ? OR full_id = ?",
        (snapshot_id, snapshot_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Snapshot not found")
    s = dict(row)
    s["paths"] = json.loads(s.get("paths", "[]"))
    s["tags"] = json.loads(s.get("tags", "[]"))
    return s


@router.get("/{snapshot_id}/browse")
async def browse_snapshot(
    snapshot_id: str,
    path: str = "/",
    target_id: str = "",
    db: aiosqlite.Connection = Depends(get_db),
    backup_engine=Depends(get_backup_engine),
    config=Depends(get_config),
):
    """Browse files in a snapshot."""
    cursor = await db.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
    snap = await cursor.fetchone()
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    snap = dict(snap)

    tid = target_id or snap["target_id"]
    tc = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (tid,))
    target = await tc.fetchone()
    if not target:
        raise HTTPException(404, "Target not found")
    target = dict(target)
    target["config"] = decrypt_config(target.get("config", "{}"), str(config.config_dir))

    entries = await backup_engine.ls(target, snap["full_id"], path)
    return {"path": path, "entries": entries}
