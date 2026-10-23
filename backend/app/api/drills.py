"""Restore drill API: history, details and on-demand runs."""

import logging
import sqlite3

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.dependencies import get_db, require_auth
from app.services.restore_drill import DrillAlreadyRunningError
from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.drills")

router = APIRouter(
    prefix="/drills",
    tags=["drills"],
    dependencies=[Depends(require_auth)],
)

_DRILL_COLUMNS = (
    "id",
    "trigger",
    "status",
    "target_id",
    "target_name",
    "snapshot_id",
    "started_at",
    "completed_at",
    "duration_seconds",
    "databases_tested",
    "databases_passed",
    "databases_failed",
    "error_message",
)
_RESULT_COLUMNS = (
    "container_name",
    "db_type",
    "db_name",
    "dump_file",
    "dump_size_bytes",
    "status",
    "detail",
    "duration_seconds",
)
_SELECT_DRILLS = """
    SELECT d.id, d.trigger, d.status, d.target_id, t.name AS target_name, d.snapshot_id,
           d.started_at, d.completed_at, d.duration_seconds, d.databases_tested,
           d.databases_passed, d.databases_failed, d.error_message
    FROM restore_drills d
    LEFT JOIN storage_targets t ON t.id = d.target_id
"""


def _drill_dict(row) -> dict:
    item = {key: row[key] for key in _DRILL_COLUMNS}
    if item["error_message"]:
        item["error_message"] = redact_credentials(item["error_message"])
    return item


@router.get("")
async def list_drills(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Drill history, newest first."""
    try:
        cursor = await db.execute(
            _SELECT_DRILLS + " ORDER BY d.started_at DESC, d.rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        cursor = await db.execute("SELECT COUNT(*) FROM restore_drills")
        total = (await cursor.fetchone())[0]
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return {"items": [], "total": 0}
    return {"items": [_drill_dict(row) for row in rows], "total": total}


@router.get("/{drill_id}")
async def get_drill(drill_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """One drill with its per-database results."""
    try:
        cursor = await db.execute(_SELECT_DRILLS + " WHERE d.id = ?", (drill_id,))
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        row = None
    if row is None:
        raise HTTPException(status_code=404, detail="Restore drill not found")
    drill = _drill_dict(row)
    cursor = await db.execute(
        "SELECT " + ", ".join(_RESULT_COLUMNS) + " FROM restore_drill_results "  # nosec B608 - constant columns
        "WHERE drill_id = ? ORDER BY id",
        (drill_id,),
    )
    drill["results"] = [{key: r[key] for key in _RESULT_COLUMNS} for r in await cursor.fetchall()]
    return drill


@router.post("/run", status_code=202)
async def run_drill(request: Request):
    """Start a drill now in the background."""
    engine = getattr(request.app.state, "restore_drill", None)
    if engine is None or getattr(engine, "docker_client", None) is None:
        raise HTTPException(
            status_code=503,
            detail="Restore drills are unavailable: Docker or the backup engine is not configured",
        )
    if engine.is_running():
        raise HTTPException(status_code=409, detail="A restore drill is already running")
    try:
        drill_id = await engine.start_drill(trigger="manual")
    except DrillAlreadyRunningError:
        raise HTTPException(status_code=409, detail="A restore drill is already running")
    return {"drill_id": drill_id, "status": "running"}
