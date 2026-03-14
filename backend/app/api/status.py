"""System status API routes — no auth required for healthcheck."""

import json
import logging
import shutil
import sqlite3
import time

import aiosqlite
from fastapi import APIRouter, Depends, Request

from app import __version__
from app.core.dependencies import get_db
from app.services.backup_coverage import evaluate_backup_coverage
from app.services.host_identity import resolve_hostname

logger = logging.getLogger("arkive.status")

router = APIRouter(prefix="/status", tags=["status"])

_start_time = time.time()

# Disk space thresholds (bytes)
_DISK_WARN_THRESHOLD = 500 * 1024 * 1024  # 500 MB
_DISK_CRIT_THRESHOLD = 100 * 1024 * 1024  # 100 MB


def _health_alias(overall_status: str) -> str:
    """Return the legacy dashboard-compatible health label."""
    return "healthy" if overall_status == "ok" else overall_status


def _get_next_backup(request: Request) -> str | None:
    """Return the soonest next backup time from the scheduler, or None."""
    try:
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is None:
            return None
        next_runs = scheduler.get_all_next_runs()
        if not next_runs:
            return None
        # get_all_next_runs() returns dict[str, str] of ISO strings
        return min(next_runs.values())
    except Exception:
        logger.debug("Could not determine next scheduled run", exc_info=True)
        return None


def _check_scheduler_health(request: Request) -> dict:
    """Check if the scheduler is running."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return {"ok": False, "message": "Scheduler not initialized"}
    inner = getattr(scheduler, "scheduler", None)
    if inner is None:
        return {"ok": False, "message": "APScheduler instance missing"}
    if not getattr(inner, "running", False):
        return {"ok": False, "message": "Scheduler not running"}
    return {"ok": True, "message": "running"}


def _check_disk_space(config_dir: str = "/config") -> dict:
    """Check available disk space on the config volume."""
    try:
        usage = shutil.disk_usage(config_dir)
        free_bytes = usage.free
        total_bytes = usage.total
        used_pct = round((usage.used / total_bytes) * 100, 1) if total_bytes > 0 else 0
        result = {
            "ok": True,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "used_percent": used_pct,
            "message": f"{used_pct}% used, {free_bytes // (1024 * 1024)} MB free",
        }
        if free_bytes < _DISK_CRIT_THRESHOLD:
            result["ok"] = False
            result["message"] = f"CRITICAL: Only {free_bytes // (1024 * 1024)} MB free"
        elif free_bytes < _DISK_WARN_THRESHOLD:
            result["message"] = f"WARNING: Only {free_bytes // (1024 * 1024)} MB free"
        return result
    except Exception as e:
        return {"ok": False, "free_bytes": 0, "total_bytes": 0, "used_percent": 0, "message": f"Cannot check disk: {e}"}


def _check_binaries() -> dict:
    """Check that restic and rclone are available on PATH."""
    missing = []
    for binary in ["restic", "rclone"]:
        if not shutil.which(binary):
            missing.append(binary)
    if missing:
        return {"ok": False, "message": f"Missing binaries: {', '.join(missing)}"}
    return {"ok": True, "message": "restic and rclone available"}


async def _fetch_setting(db: aiosqlite.Connection, key: str) -> str | None:
    """Return a setting value, tolerating first-boot schema races."""
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return None
    return row["value"] if row and row["value"] is not None else None


async def _check_db_health(db: aiosqlite.Connection) -> dict:
    """Check DB connectivity with a simple query."""
    try:
        cursor = await db.execute("SELECT 1")
        await cursor.fetchone()
        return {"ok": True, "message": "connected"}
    except Exception as e:
        return {"ok": False, "message": f"DB error: {e}"}


async def _database_stats(db: aiosqlite.Connection) -> tuple[int, int, bool]:
    """Return discovered database totals and latest dump health.

    healthy counts databases that succeeded in the most recent run that
    actually produced database dump records. If no such run exists yet,
    healthy remains 0 and has_health_sample is False.
    """
    try:
        cursor = await db.execute("SELECT databases FROM discovered_containers")
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return 0, 0, False

    discovered_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        raw = row["databases"] if isinstance(row, aiosqlite.Row) else row[0]
        try:
            databases = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            databases = []

        for entry in databases:
            if not isinstance(entry, dict):
                continue
            container_name = str(entry.get("container_name") or "").strip()
            db_name = str(entry.get("db_name") or "").strip()
            db_type = str(entry.get("db_type") or "").strip()
            if container_name and db_name and db_type:
                discovered_keys.add((container_name, db_name, db_type))

    total_databases = len(discovered_keys)
    if total_databases == 0:
        return 0, 0, False

    try:
        cursor = await db.execute(
            """SELECT jr.id
               FROM job_runs jr
               WHERE EXISTS (
                   SELECT 1 FROM job_run_databases jrd WHERE jrd.run_id = jr.id
               )
               ORDER BY jr.started_at DESC
               LIMIT 1"""
        )
        latest_run = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return total_databases, 0, False
    if not latest_run:
        return total_databases, 0, False

    run_id = latest_run["id"] if isinstance(latest_run, aiosqlite.Row) else latest_run[0]
    try:
        cursor = await db.execute(
            """SELECT container_name, db_name, db_type, status
               FROM job_run_databases
               WHERE run_id = ?""",
            (run_id,),
        )
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return total_databases, 0, False

    successful_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        row_dict = (
            dict(row)
            if isinstance(row, aiosqlite.Row)
            else {
                "container_name": row[0],
                "db_name": row[1],
                "db_type": row[2],
                "status": row[3],
            }
        )
        if row_dict["status"] == "success":
            successful_keys.add((row_dict["container_name"], row_dict["db_name"], row_dict["db_type"]))

    healthy_databases = sum(1 for key in discovered_keys if key in successful_keys)
    return total_databases, healthy_databases, True


@router.get("")
async def get_status(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """Health check and system status — no auth required.

    Returns status as one of: ok, degraded, error.
    - ok: all checks pass
    - degraded: non-critical checks failing (disk warning, targets unhealthy)
    - error: critical checks failing (DB down, scheduler stopped, disk critical)
    """
    checks = {}
    issues = []

    # 1. DB connectivity (critical -- nothing works without the DB)
    checks["database"] = await _check_db_health(db)
    if not checks["database"]["ok"]:
        issues.append(("critical", "database"))

    # 2. Scheduler health (degraded -- app can still serve UI/setup)
    checks["scheduler"] = _check_scheduler_health(request)
    if not checks["scheduler"]["ok"]:
        issues.append(("warning", "scheduler"))

    # 3. Disk space
    config_dir = str(getattr(getattr(request.app.state, "config", None), "config_dir", "/config"))
    checks["disk"] = _check_disk_space(config_dir)
    if not checks["disk"]["ok"]:
        issues.append(("critical", "disk"))
    elif checks["disk"].get("free_bytes", 0) < _DISK_WARN_THRESHOLD:
        issues.append(("warning", "disk"))

    # 4. Binary availability (degraded -- setup wizard works without them)
    checks["binaries"] = _check_binaries()
    if not checks["binaries"]["ok"]:
        issues.append(("warning", "binaries"))

    # Determine overall status
    critical_issues = [i for i in issues if i[0] == "critical"]
    warning_issues = [i for i in issues if i[0] == "warning"]

    if critical_issues:
        overall_status = "error"
    elif warning_issues:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    # Setup check
    api_key_hash = await _fetch_setting(db, "api_key_hash")
    setup_completed = api_key_hash is not None

    # Platform
    runtime_platform = getattr(getattr(request.app, "state", None), "platform", None)
    if isinstance(runtime_platform, str):
        runtime_platform_value = runtime_platform
    else:
        runtime_platform_value = getattr(runtime_platform, "value", "linux")
    platform = (await _fetch_setting(db, "platform")) or runtime_platform_value
    server_name = await _fetch_setting(db, "server_name")

    # Last backup
    try:
        cursor = await db.execute("SELECT * FROM job_runs ORDER BY started_at DESC LIMIT 1")
        last_run = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        last_run = None
    last_backup = None
    if last_run:
        last_backup = {
            "status": last_run["status"],
            "started_at": last_run["started_at"],
            "completed_at": last_run["completed_at"],
            "duration_seconds": last_run["duration_seconds"],
        }

    # Targets
    try:
        cursor = await db.execute("SELECT COUNT(*) as total FROM storage_targets")
        total_targets = (await cursor.fetchone())["total"]
        cursor = await db.execute(
            "SELECT COUNT(*) as healthy FROM storage_targets WHERE status IN ('healthy', 'online', 'ok')"
        )
        healthy_targets = (await cursor.fetchone())["healthy"]
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        total_targets = 0
        healthy_targets = 0
        if overall_status == "ok":
            overall_status = "degraded"

    # Target health as a warning if some targets are unhealthy
    if total_targets > 0 and healthy_targets < total_targets and overall_status == "ok":
        overall_status = "degraded"

    # Databases
    total_databases, healthy_databases, has_database_health_sample = await _database_stats(db)

    if (
        has_database_health_sample
        and total_databases > 0
        and healthy_databases < total_databases
        and overall_status == "ok"
    ):
        overall_status = "degraded"

    # Storage
    try:
        cursor = await db.execute("SELECT COALESCE(SUM(total_size_bytes), 0) as total FROM storage_targets")
        total_bytes = (await cursor.fetchone())["total"]
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        total_bytes = 0

    # Snapshot count
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM snapshots")
        total_snapshots = (await cursor.fetchone())["cnt"]
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        total_snapshots = 0

    user_shares_path = str(getattr(getattr(request.app.state, "config", None), "user_shares_path", "/mnt/user"))
    coverage = await evaluate_backup_coverage(
        db,
        platform=platform,
        user_shares_path=user_shares_path,
    )

    return {
        "status": overall_status,
        "health": _health_alias(overall_status),
        "version": __version__,
        "hostname": resolve_hostname(app=request.app, settings={"server_name": server_name or ""}),
        "uptime_seconds": int(time.time() - _start_time),
        "platform": platform,
        "setup_completed": setup_completed,
        "checks": checks,
        "last_backup": last_backup,
        "next_backup": _get_next_backup(request),
        "targets": {"total": total_targets, "healthy": healthy_targets},
        "databases": {"total": total_databases, "healthy": healthy_databases},
        "storage": {"total_bytes": total_bytes},
        "total_snapshots": total_snapshots,
        "coverage": coverage,
    }
