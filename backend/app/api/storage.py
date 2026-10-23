"""Storage statistics API routes."""

import aiosqlite
from fastapi import APIRouter, Depends

from app.core.dependencies import get_db, require_auth

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_auth)])


@router.get("")
async def get_storage_stats(db: aiosqlite.Connection = Depends(get_db)):
    """Get aggregate storage statistics."""
    # Targets
    cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
    targets = []
    total_size = 0
    total_snapshots = 0
    for row in await cursor.fetchall():
        t = dict(row)
        t["config"] = {}  # Don't expose config in stats
        total_size += t.get("total_size_bytes", 0)
        total_snapshots += t.get("snapshot_count", 0)
        targets.append(
            {
                "id": t["id"],
                "name": t["name"],
                "type": t["type"],
                "total_size_bytes": t.get("total_size_bytes", 0),
                "snapshot_count": t.get("snapshot_count", 0),
            }
        )

    # Size history
    cursor = await db.execute("SELECT * FROM size_history ORDER BY date DESC LIMIT 90")
    history = [dict(row) for row in await cursor.fetchall()]

    return {
        "total_size_bytes": total_size,
        "target_count": len(targets),
        "snapshot_count": total_snapshots,
        "targets": targets,
        "size_history": history,
    }
