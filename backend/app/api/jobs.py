"""
Backup Jobs API endpoints.
CRUD operations for backup job configurations, trigger runs, and run history.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime

import aiosqlite
from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_config, get_db, get_event_bus, get_orchestrator, get_scheduler, require_auth
from app.models.jobs import BackupJobCreate, BackupJobUpdate
from app.services.orchestrator import cleanup_stale_backup_lock

logger = logging.getLogger("arkive.api.jobs")
router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_job(row: aiosqlite.Row) -> dict:
    """Convert a backup_jobs Row to a dict with parsed JSON fields."""
    d = dict(row)
    for field in ("targets", "directories", "exclude_patterns"):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = []
    for field in ("enabled", "include_databases", "include_flash"):
        if field in d and isinstance(d[field], int):
            d[field] = bool(d[field])
    return d


async def _enrich_job(db: aiosqlite.Connection, job: dict) -> dict:
    """Add last_run and next_run to a job dict."""
    cursor = await db.execute(
        "SELECT started_at, status FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT 1",
        (job["id"],),
    )
    last_run_row = await cursor.fetchone()
    job["last_run"] = (
        {"started_at": last_run_row["started_at"], "status": last_run_row["status"]} if last_run_row else None
    )

    next_run = None
    if job.get("schedule") and job.get("enabled"):
        try:
            cron = croniter(job["schedule"], datetime.now(UTC))
            next_dt = cron.get_next(datetime)
            next_run = next_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, KeyError) as exc:
            logger.warning("Failed to compute next_run for job %s: %s", job["id"], exc)
    job["next_run"] = next_run
    return job


def _severity_to_level(severity: str) -> str:
    return {
        "debug": "DEBUG",
        "info": "INFO",
        "warning": "WARN",
        "warn": "WARN",
        "error": "ERROR",
        "critical": "ERROR",
        "success": "INFO",
    }.get((severity or "info").lower(), "INFO")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_jobs(
    limit: int = 200,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all backup jobs with computed last_run and next_run."""
    cursor = await db.execute("SELECT COUNT(*) FROM backup_jobs")
    total = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT * FROM backup_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()

    jobs = [_row_to_job(row) for row in rows]
    for job in jobs:
        await _enrich_job(db, job)

    return {
        "items": jobs,
        "jobs": jobs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.get("/runs")
async def list_all_runs(
    db: aiosqlite.Connection = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    days: int | None = None,
    sort_by: str = "started_at",
    sort_dir: str = "desc",
):
    """List all job runs across all jobs with pagination, filtering, and sorting.

    Query params:
        status: Filter by run status (success/partial/failed/running/cancelled)
        days: Filter to runs within the last N days
    """
    allowed_sort = {
        "id",
        "started_at",
        "status",
        "duration_seconds",
        "databases_dumped",
        "total_size_bytes",
        "job_name",
        "target_count",
    }
    if sort_by not in allowed_sort:
        sort_by = "started_at"
    if sort_dir.lower() not in ("asc", "desc"):
        sort_dir = "desc"

    # Build WHERE clause for filters
    conditions: list[str] = []
    params: list = []
    if status:
        conditions.append("r.status = ?")
        params.append(status)
    if days is not None and days > 0:
        conditions.append("r.started_at >= datetime('now', ?)")
        params.append(f"-{days} days")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""  # nosec B608

    cursor = await db.execute(
        f"SELECT COUNT(*) FROM job_runs r {where_clause}",
        params,  # nosec B608
    )
    total = (await cursor.fetchone())[0]

    col_map = {
        "job_name": "b.name",
        "target_count": "target_count",
    }
    sort_col = col_map.get(sort_by, f"r.{sort_by}")

    query = f"""
        SELECT r.*,
               b.name AS job_name,
               b.type AS job_type,
               COALESCE(t.target_count, 0) AS target_count
        FROM job_runs r
        LEFT JOIN backup_jobs b ON r.job_id = b.id
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS target_count
            FROM job_run_targets
            GROUP BY run_id
        ) t ON r.id = t.run_id
        {where_clause}
        ORDER BY {sort_col} {sort_dir.upper()} NULLS LAST
        LIMIT ? OFFSET ?
    """  # nosec B608
    cursor = await db.execute(query, (*params, limit, offset))
    rows = await cursor.fetchall()

    paginated = [dict(row) for row in rows]
    return {
        "items": paginated,
        "runs": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get a single job run with related databases, targets, and job info."""
    cursor = await db.execute(
        """SELECT r.*, b.name AS job_name, b.type AS job_type
           FROM job_runs r
           LEFT JOIN backup_jobs b ON r.job_id = b.id
           WHERE r.id = ?""",
        (run_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    run = dict(row)

    cursor = await db.execute(
        """SELECT id, container_name, db_type, db_name, dump_size_bytes,
                  integrity_check, status, host_path, error
           FROM job_run_databases WHERE run_id = ?
           ORDER BY container_name""",
        (run_id,),
    )
    run["databases"] = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute(
        """SELECT jrt.id, jrt.target_id, jrt.status, jrt.snapshot_id,
                  jrt.upload_bytes, jrt.duration_seconds, jrt.error,
                  st.name AS target_name, st.type AS target_provider
           FROM job_run_targets jrt
           LEFT JOIN storage_targets st ON jrt.target_id = st.id
           WHERE jrt.run_id = ?
           ORDER BY jrt.id""",
        (run_id,),
    )
    run["targets"] = [dict(r) for r in await cursor.fetchall()]

    return run


@router.get("/runs/{run_id}/logs")
async def get_run_logs(
    run_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    limit: int = 500,
):
    """Return activity-derived log entries for a specific run."""
    cursor = await db.execute("SELECT id FROM job_runs WHERE id = ?", (run_id,))
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Run not found")

    # There is no dedicated run_logs table yet; use activity log entries that
    # include matching run_id in the details payload.
    cursor = await db.execute(
        "SELECT type, message, details, severity, timestamp FROM activity_log ORDER BY timestamp DESC LIMIT ?",
        (max(limit * 4, limit),),
    )
    rows = await cursor.fetchall()

    entries: list[dict] = []
    for row in rows:
        details_raw = row["details"] if isinstance(row, aiosqlite.Row) else row[2]
        try:
            details = json.loads(details_raw or "{}")
        except (json.JSONDecodeError, TypeError):
            details = {}
        if details.get("run_id") != run_id:
            continue

        type_value = row["type"] if isinstance(row, aiosqlite.Row) else row[0]
        message_value = row["message"] if isinstance(row, aiosqlite.Row) else row[1]
        severity_value = row["severity"] if isinstance(row, aiosqlite.Row) else row[3]
        timestamp_value = row["timestamp"] if isinstance(row, aiosqlite.Row) else row[4]
        entries.append(
            {
                "timestamp": timestamp_value,
                "level": _severity_to_level(severity_value),
                "component": f"job.{type_value}",
                "message": message_value,
            }
        )
        if len(entries) >= limit:
            break

    entries.reverse()
    return {"items": entries, "total": len(entries), "run_id": run_id}


@router.get("/{job_id}")
async def get_job(job_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get a single backup job by ID."""
    cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _row_to_job(row)
    await _enrich_job(db, job)
    return job


@router.post("", status_code=201)
async def create_job(
    body: BackupJobCreate,
    db: aiosqlite.Connection = Depends(get_db),
    scheduler=Depends(get_scheduler),
    event_bus=Depends(get_event_bus),
):
    """Create a new backup job."""
    valid_types = {"full", "db_dump", "flash"}
    if body.type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid job type. Must be one of: {', '.join(valid_types)}",
        )

    # Validate directory paths — reject paths starting with '-' to prevent CLI injection
    for path in body.directories:
        if path.startswith("-"):
            raise HTTPException(422, "Invalid path: paths cannot start with '-'")

    # Validate cron expression
    cron_parts = body.schedule.strip().split()
    if len(cron_parts) != 5:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid cron expression: expected 5 fields, got {len(cron_parts)}",
        )
    try:
        croniter(body.schedule)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {exc}")

    job_id = str(uuid.uuid4())[:8]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    await db.execute(
        """INSERT INTO backup_jobs
           (id, name, type, schedule, enabled, targets, directories,
            exclude_patterns, include_databases, include_flash,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id,
            body.name,
            body.type,
            body.schedule,
            json.dumps(body.targets),
            json.dumps(body.directories),
            json.dumps(body.exclude_patterns),
            1 if body.include_databases else 0,
            1 if body.include_flash else 0,
            now,
            now,
        ),
    )

    # Activity log
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "job",
            "created",
            f"Backup job '{body.name}' created",
            json.dumps({"job_id": job_id, "job_name": body.name, "job_type": body.type}),
            "info",
            now,
        ),
    )
    await db.commit()

    # Register with scheduler
    if scheduler is not None:
        try:
            if callable(getattr(scheduler, "add_job", None)):
                await scheduler.add_job(
                    {
                        "id": job_id,
                        "name": body.name,
                        "schedule": body.schedule,
                        "enabled": True,
                        "type": body.type,
                    }
                )
            elif callable(getattr(scheduler, "reschedule_job", None)):
                # Current scheduler implementation supports reschedule/add semantics.
                await scheduler.reschedule_job(job_id, body.schedule)
        except Exception as exc:
            logger.warning("Scheduler registration failed for %s: %s", job_id, exc)

    # Publish event
    if event_bus is not None:
        await event_bus.publish("job.created", {"job_id": job_id, "name": body.name})

    logger.info("Created backup job: %s (%s)", body.name, body.type)

    return {
        "id": job_id,
        "name": body.name,
        "type": body.type,
        "schedule": body.schedule,
        "enabled": True,
        "targets": body.targets,
        "directories": body.directories,
        "exclude_patterns": body.exclude_patterns,
        "include_databases": body.include_databases,
        "include_flash": body.include_flash,
        "created_at": now,
        "updated_at": now,
    }


@router.put("/{job_id}")
async def update_job(
    job_id: str,
    body: BackupJobUpdate,
    db: aiosqlite.Connection = Depends(get_db),
    scheduler=Depends(get_scheduler),
    event_bus=Depends(get_event_bus),
):
    """Update a backup job."""
    cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    # Validate directory paths — reject paths starting with '-' to prevent CLI injection
    if body.directories is not None:
        for path in body.directories:
            if path.startswith("-"):
                raise HTTPException(422, "Invalid path: paths cannot start with '-'")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    updates: list[str] = []
    params: list = []

    if body.name is not None:
        updates.append("name = ?")
        params.append(body.name)
    if body.schedule is not None:
        updates.append("schedule = ?")
        params.append(body.schedule)
    if body.enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if body.enabled else 0)
    if body.targets is not None:
        updates.append("targets = ?")
        params.append(json.dumps(body.targets))
    if body.directories is not None:
        updates.append("directories = ?")
        params.append(json.dumps(body.directories))
    if body.exclude_patterns is not None:
        updates.append("exclude_patterns = ?")
        params.append(json.dumps(body.exclude_patterns))
    if body.include_databases is not None:
        updates.append("include_databases = ?")
        params.append(1 if body.include_databases else 0)
    if body.include_flash is not None:
        updates.append("include_flash = ?")
        params.append(1 if body.include_flash else 0)

    updates.append("updated_at = ?")
    params.append(now)
    params.append(job_id)

    await db.execute(
        f"UPDATE backup_jobs SET {', '.join(updates)} WHERE id = ?",
        params,  # nosec B608
    )

    # Activity log
    changed = [u.split(" = ")[0] for u in updates if u != "updated_at = ?"]
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "job",
            "updated",
            f"Backup job '{row['name']}' updated",
            json.dumps({"job_id": job_id, "changed_fields": changed}),
            "info",
            now,
        ),
    )
    await db.commit()

    # Re-read updated job
    updated = await get_job(job_id, db)

    # Update scheduler
    if scheduler is not None:
        try:
            if callable(getattr(scheduler, "update_job", None)):
                await scheduler.update_job(
                    job_id,
                    schedule=updated.get("schedule", row["schedule"]),
                    name=updated.get("name", row["name"]),
                    enabled=updated.get("enabled", True),
                )
            else:
                if body.schedule is not None and callable(getattr(scheduler, "reschedule_job", None)):
                    await scheduler.reschedule_job(job_id, body.schedule)
                if body.enabled is False and callable(getattr(scheduler, "pause_job", None)):
                    await scheduler.pause_job(job_id)
                elif body.enabled is True and callable(getattr(scheduler, "resume_job", None)):
                    await scheduler.resume_job(job_id)
        except Exception as exc:
            logger.warning("Scheduler update failed for %s: %s", job_id, exc)

    if event_bus is not None:
        await event_bus.publish("job.updated", {"job_id": job_id, "changed_fields": changed})

    return updated


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    scheduler=Depends(get_scheduler),
    event_bus=Depends(get_event_bus),
):
    """Delete a backup job. Cascades to job_runs via ON DELETE CASCADE."""
    cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _row_to_job(row)
    job_name = job.get("name", job_id)

    # Remove from scheduler
    if scheduler is not None:
        try:
            await scheduler.remove_job(job_id)
        except Exception as exc:
            logger.warning("Scheduler removal failed for %s: %s", job_id, exc)

    await db.execute("DELETE FROM backup_jobs WHERE id = ?", (job_id,))

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "job",
            "deleted",
            f"Backup job '{job_name}' deleted",
            json.dumps({"job_id": job_id, "job_name": job_name, "job_type": job.get("type", "unknown")}),
            "warning",
            now,
        ),
    )
    await db.commit()

    if event_bus is not None:
        await event_bus.publish("job.deleted", {"job_id": job_id, "name": job_name})

    logger.info("Deleted backup job: %s (%s)", job_id, job_name)
    return {"message": "Backup job deleted."}


@router.post("/{job_id}/run", status_code=202)
async def run_job(
    job_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config=Depends(get_config),
    orchestrator=Depends(get_orchestrator),
    event_bus=Depends(get_event_bus),
):
    """Trigger a manual backup run for a job. Returns 202 Accepted with run_id."""
    # Concurrency guard via lock file
    lock_file = os.path.join(str(config.config_dir), "backup.lock")
    cleanup_stale_backup_lock(config.config_dir)
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                lock_info = json.loads(f.read())
            raise HTTPException(
                status_code=409,
                detail=f"Another backup is already running (run_id={lock_info.get('run_id', 'unknown')})",
            )
        except (json.JSONDecodeError, OSError):
            raise HTTPException(status_code=409, detail="Another backup is already running")

    cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _row_to_job(row)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = str(uuid.uuid4())[:8]

    # Create the run row immediately so list/detail endpoints can return it
    # before the background pipeline advances.
    await db.execute(
        """INSERT INTO job_runs (id, job_id, status, trigger, started_at)
           VALUES (?, ?, 'running', 'manual', ?)""",
        (run_id, job_id, now),
    )

    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "backup",
            "run_started",
            f"Manual run started for job '{job['name']}'",
            json.dumps({"job_id": job_id, "run_id": run_id, "trigger": "manual"}),
            "info",
            now,
        ),
    )
    await db.commit()

    # Launch pipeline in background — orchestrator creates its own run record
    if orchestrator is not None:

        async def _run_backup():
            async with aiosqlite.connect(config.db_path) as bg_db:
                bg_db.row_factory = aiosqlite.Row
                await orchestrator.run_backup(
                    job_id=job_id,
                    trigger="manual",
                    run_id=run_id,
                )

        asyncio.create_task(_run_backup())

    if event_bus is not None:
        await event_bus.publish("job.run_started", {"job_id": job_id})

    logger.info("Manual run triggered for job %s", job_id)

    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": "running",
        "trigger": "manual",
        "started_at": now,
        "message": f"Manual backup run started for '{job['name']}'",
    }


@router.delete("/{job_id}/run")
async def cancel_job_run(
    job_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    orchestrator=Depends(get_orchestrator),
):
    """Cancel an in-progress backup run for a job."""
    cursor = await db.execute(
        "SELECT id FROM job_runs WHERE job_id = ? AND status = 'running' ORDER BY started_at DESC LIMIT 1",
        (job_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No running backup found for this job")

    run_id = row["id"]

    cancelled = False
    if orchestrator is not None:
        try:
            cancelled = orchestrator.cancel_run(run_id)
        except Exception as exc:
            logger.warning("Cancel failed for run %s: %s", run_id, exc)

    if cancelled:
        logger.info("Cancellation requested for run %s", run_id)
        return {"message": f"Cancellation requested for run {run_id}", "run_id": run_id}
    else:
        logger.warning("Run %s not found in active runs", run_id)
        return {"message": f"Run {run_id} may have already completed", "run_id": run_id}


@router.get("/{job_id}/runs")
@router.get("/{job_id}/history")
async def get_job_history(
    job_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """Get run history for a specific job."""
    cursor = await db.execute("SELECT COUNT(*) FROM job_runs WHERE job_id = ?", (job_id,))
    total = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
        (job_id, limit, offset),
    )
    rows = await cursor.fetchall()

    paginated = [dict(row) for row in rows]
    return {
        "items": paginated,
        "runs": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }
