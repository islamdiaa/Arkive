"""Verification API routes for trust score and backup integrity checks."""

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.api.status import compute_trust_score
from app.core.dependencies import get_db, get_event_bus, get_verify_engine, require_auth
from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.verification")

router = APIRouter(
    prefix="/verification",
    tags=["verification"],
    dependencies=[Depends(require_auth)],
)


@router.get("")
async def get_verification_results(db: aiosqlite.Connection = Depends(get_db)):
    """Return latest verification results per target and an overall trust score."""
    try:
        cursor = await db.execute(
            """SELECT vr.*
               FROM verification_runs vr
               INNER JOIN (
                   SELECT target_id, MAX(rowid) AS latest_rowid
                   FROM verification_runs
                   GROUP BY target_id
               ) latest_per_target
               ON vr.target_id = latest_per_target.target_id
                  AND vr.rowid = latest_per_target.latest_rowid
               ORDER BY vr.started_at DESC"""
        )
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return {"trust_score": 0, "results": []}

    results = []
    for row in rows:
        r = dict(row)
        if r.get("error_message"):
            r["error_message"] = redact_credentials(r["error_message"])
        results.append(r)
    trust_score = await compute_trust_score(db)

    return {
        "trust_score": trust_score,
        "results": results,
    }


@router.post("/run")
async def trigger_verification(
    db: aiosqlite.Connection = Depends(get_db),
    event_bus=Depends(get_event_bus),
    verify_engine=Depends(get_verify_engine),
):
    """Trigger a manual verification run across all enabled targets.

    Creates verification_runs rows, then launches the verify engine in the
    background. The engine picks up the 'running' rows and updates them
    with results when complete.
    """
    # Rate limit: reject if a verification ran in the last 10 minutes
    try:
        cursor = await db.execute(
            """SELECT started_at FROM verification_runs
               ORDER BY started_at DESC LIMIT 1"""
        )
        last_run = await cursor.fetchone()
        if last_run and last_run["started_at"]:
            last_dt = datetime.fromisoformat(
                last_run["started_at"].replace("Z", "+00:00")
            )
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed < 600:
                raise HTTPException(
                    429,
                    f"Verification ran {int(elapsed)}s ago. Please wait {int(600 - elapsed)}s.",
                )
    except HTTPException:
        raise
    except Exception:
        pass  # If rate-limit check fails, allow the run

    try:
        cursor = await db.execute(
            "SELECT id, name FROM storage_targets WHERE enabled = 1"
        )
        targets = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        raise HTTPException(500, "Cannot read storage targets")

    if not targets:
        raise HTTPException(404, "No enabled storage targets found")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_ids = []
    run_id_map: dict[str, str] = {}

    for target in targets:
        target_dict = dict(target)
        run_id = str(uuid.uuid4())
        run_ids.append(run_id)
        run_id_map[target_dict["id"]] = run_id

        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, status)
               VALUES (?, ?, ?, 'running')""",
            (run_id, target_dict["id"], now),
        )

        await db.execute(
            """INSERT INTO activity_log (type, action, message, details, severity)
               VALUES ('verification', 'started', ?, ?, 'info')""",
            (
                f"Verification started for target {target_dict['name']}",
                json.dumps({"run_id": run_id, "target_id": target_dict["id"]}),
            ),
        )

    await db.commit()

    if event_bus:
        await event_bus.publish("verification:started", {
            "run_ids": run_ids,
            "target_count": len(targets),
            "started_at": now,
        })

    # Launch verification in the background so the API returns immediately
    if verify_engine:
        asyncio.create_task(_run_verification(verify_engine, run_id_map, event_bus))

    return {
        "status": "started",
        "run_ids": run_ids,
        "target_count": len(targets),
        "message": f"Verification started for {len(targets)} target(s)",
    }


async def _run_verification(
    verify_engine, run_id_map: dict[str, str], event_bus
) -> None:
    """Background task that runs the verify engine and publishes completion events."""
    try:
        result = await verify_engine.verify_all_targets(run_ids=run_id_map)
        status = result.get("status", "unknown")
        trust_score = result.get("overall_trust_score", 0.0)

        if event_bus:
            await event_bus.publish("verification:completed", {
                "status": status,
                "trust_score": trust_score,
                "target_count": len(result.get("targets", [])),
                "last_verified_at": result.get("verified_at"),
            })

        logger.info(
            "Manual verification complete: status=%s trust_score=%.1f",
            status, trust_score,
        )
    except Exception as e:
        logger.error("Manual verification failed: %s", e)
        if event_bus:
            await event_bus.publish("verification:failed", {
                "error": str(e),
            })
