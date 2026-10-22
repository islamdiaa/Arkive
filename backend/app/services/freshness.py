"""Backup freshness: is every scheduled job still producing good backups?

A job is *stale* when its last good run is older than its schedule allows.
This catches what per-run notifications cannot: runs that never happen
(scheduler stopped, container down) and runs that "complete" without backing
up the databases they exist for.

A run counts as good when it succeeded, or when it was partial but still
wrote a snapshot — and, for jobs that dump databases on a system that has
had databases before, dumped at least one.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite
from croniter import croniter

from app.services.settings_store import get_int_setting

logger = logging.getLogger("arkive.freshness")

# Job types whose runs dump databases (mirrors orchestrator.DATABASE_JOB_TYPES).
DATABASE_JOB_TYPES = {"full", "db_dump", "custom"}

# Allowed lateness on top of the longest gap between scheduled runs.
GRACE_MIN_HOURS = 2.0
GRACE_FRACTION = 0.25

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_TS_FORMAT)


def schedule_gap_hours(schedule: str, now: datetime | None = None) -> float | None:
    """Longest interval between consecutive runs of a cron schedule, in hours.

    Measured over the past week so uneven schedules ("0 6,18 * * *",
    weekdays only) are judged by their longest gap. Returns None for an
    invalid schedule.
    """
    now = (now or datetime.now(timezone.utc)).astimezone()
    try:
        if not croniter.is_valid(schedule):
            return None
        for window_days, max_fires in ((8, 3000), (400, 600)):
            it = croniter(schedule, now - timedelta(days=window_days))
            fires: list[datetime] = []
            while len(fires) < max_fires:
                fire = it.get_next(datetime)
                if fire > now:
                    fires.append(fire)
                    break
                fires.append(fire)
            if len(fires) >= 3:
                gaps = [(b - a).total_seconds() for a, b in zip(fires, fires[1:])]
                return max(gaps) / 3600
    except (ValueError, KeyError, TypeError) as exc:
        logger.debug("Cannot compute schedule gap for %r: %s", schedule, exc)
    return None


def threshold_hours_for(schedule: str, override_hours: int = 0, now: datetime | None = None) -> float | None:
    """Hours a job may go without a good run before it is stale."""
    if override_hours and override_hours > 0:
        return float(override_hours)
    gap = schedule_gap_hours(schedule, now)
    if gap is None:
        return None
    return round(gap + max(GRACE_MIN_HOURS, gap * GRACE_FRACTION), 2)


_GOOD_RUN_SQL = """
    SELECT jr.started_at
    FROM job_runs jr
    WHERE jr.job_id = :job_id
      AND (
        jr.status = 'success'
        OR (
          jr.status = 'partial'
          AND EXISTS (SELECT 1 FROM job_run_targets t
                      WHERE t.run_id = jr.id AND t.status = 'success')
          AND (
            :needs_databases = 0
            OR jr.databases_dumped > 0
            OR NOT EXISTS (SELECT 1 FROM job_runs p
                           WHERE p.job_id = jr.job_id AND p.databases_dumped > 0)
          )
        )
      )
    ORDER BY jr.started_at DESC
    LIMIT 1
"""

_LAST_FINISHED_SQL = """
    SELECT id, status, started_at
    FROM job_runs
    WHERE job_id = ? AND status IN ('success', 'partial', 'failed')
    ORDER BY started_at DESC
    LIMIT 1
"""


async def evaluate_freshness(db: aiosqlite.Connection, now: datetime | None = None) -> dict:
    """Assess every enabled job.

    Returns ``{"stale", "healthy", "checked_at", "stale_jobs", "jobs"}``.
    ``healthy`` is True when nothing is stale and no job's latest finished
    run was bad — the condition for sending the heartbeat.
    """
    now = now or datetime.now(timezone.utc)
    result = {"stale": False, "healthy": True, "checked_at": _fmt(now), "stale_jobs": [], "jobs": []}
    try:
        cursor = await db.execute(
            "SELECT id, name, type, schedule, include_databases, created_at "
            "FROM backup_jobs WHERE enabled = 1 ORDER BY name"
        )
        jobs = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return result

    override = await get_int_setting(db, "stale_backup_hours", minimum=0)

    for job_id, name, job_type, schedule, include_databases, created_at in jobs:
        needs_databases = 1 if (include_databases and job_type in DATABASE_JOB_TYPES) else 0
        threshold = threshold_hours_for(schedule, override, now)

        cursor = await db.execute(_GOOD_RUN_SQL, {"job_id": job_id, "needs_databases": needs_databases})
        row = await cursor.fetchone()
        last_good_at = row[0] if row else None

        cursor = await db.execute(_LAST_FINISHED_SQL, (job_id,))
        last_run = await cursor.fetchone()
        last_status = last_run[1] if last_run else None
        # The latest finished run is bad when it failed, or was partial without
        # meeting the good-run bar (its start is newer than the last good one).
        last_run_bad = bool(last_run) and (
            last_status == "failed" or (last_run[2] or "") > (last_good_at or "")
        )

        reference = _parse_ts(last_good_at) or _parse_ts(created_at)
        age_hours = (now - reference).total_seconds() / 3600 if reference else None
        stale = threshold is not None and age_hours is not None and age_hours > threshold

        entry = {
            "job_id": job_id,
            "job_name": name,
            "schedule": schedule,
            "last_good_at": last_good_at,
            "last_status": last_status,
            "threshold_hours": threshold,
            "age_hours": round(age_hours, 2) if age_hours is not None else None,
            "stale": stale,
            "last_run_bad": last_run_bad,
        }
        result["jobs"].append(entry)
        if stale:
            result["stale_jobs"].append({
                "job_id": job_id,
                "job_name": name,
                "last_good_at": last_good_at,
                "threshold_hours": threshold,
                "overdue_hours": round(age_hours - threshold, 2),
            })
        if stale or last_run_bad:
            result["healthy"] = False

    result["stale"] = bool(result["stale_jobs"])
    return result


def describe_stale_job(job: dict) -> str:
    """One-line human description, used for notifications and activity."""
    threshold = job.get("threshold_hours")
    window = f"{threshold:g} h" if isinstance(threshold, (int, float)) else "its schedule"
    if job.get("last_good_at"):
        return (f"{job['job_name']} has not produced a good backup since "
                f"{job['last_good_at']} (expected within {window}).")
    return f"{job['job_name']} has never produced a good backup (expected within {window})."


HEARTBEAT_TIMEOUT_SECONDS = 10


async def ping_heartbeat(url: str, timeout: float = HEARTBEAT_TIMEOUT_SECONDS) -> dict:
    """GET a dead man's switch URL (Uptime Kuma push monitor, healthchecks.io).

    Returns ``{"ok", "status_code", "error"}``; never raises.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Arkive heartbeat"})
        ok = 200 <= response.status_code < 300
        return {"ok": ok, "status_code": response.status_code,
                "error": None if ok else f"HTTP {response.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
