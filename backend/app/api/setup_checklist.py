"""Setup checklist: what is left between "Arkive is installed" and "this
server is protected".

The setup wizard creates jobs and (optionally) one target. It cannot prove
that a backup ran, that it restores, that a copy lives off this server, that
anyone hears about failures, or that the encryption password exists anywhere
but here. Each step below is judged from evidence already in the database,
never from a flag the UI sets:

- ``first_backup``: a backup run finished with status ``success``. The
  freshness watchdog also accepts some partial runs (a snapshot was written),
  but a partial run may be missing a database or a folder, and this
  checklist ends in "this server is protected", so it waits for a clean run.
- ``restore_test``: the latest decisive restore drill passed. When there is
  nothing a drill can test (no databases, or only types drills skip such as
  Redis and MongoDB, or the last drill found no dump it could test), the
  latest backup verification whose random-file restore test actually ran
  counts instead.
- ``offsite_target``: an enabled, non-local storage target exists. A local
  target never counts, even when its path happens to be a network mount:
  Arkive can't tell that apart from a disk in this server.
- ``notifications``: an enabled notification channel exists.
- ``recovery_kit``: a recovery kit was confirmed after the current
  encryption password was set.

Once every step is done the dashboard card can be dismissed; dismissal is
stored server-side so it holds across browsers. Later regressions are not the
checklist's job: stale backups, failed drills and an outdated recovery kit
already have their own dashboard signals.
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.api.status import recovery_kit_confirmed
from app.core.activity import log_activity
from app.core.dependencies import get_db, require_auth
from app.services.db_exclusions import excluded_identities
from app.services.db_exclusions import identity as db_identity
from app.services.restore_drill import DRILLABLE_TYPES, NOTHING_TO_DRILL
from app.services.settings_store import get_setting, set_setting
from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.setup_checklist")

router = APIRouter(
    prefix="/setup-checklist",
    tags=["setup"],
    dependencies=[Depends(require_auth)],
)

STEP_IDS = ("first_backup", "restore_test", "offsite_target", "notifications", "recovery_kit")
# Per-server state: settings export leaves it out (EXPORT_EXCLUDED_KEYS), so a
# config imported onto a rebuilt server doesn't hide that server's checklist.
DISMISSED_SETTING = "setup_checklist_dismissed_at"

# Step states. ``attention`` means the last attempt went wrong (a failed run,
# a failed drill, a recovery kit made for an old password).
DONE = "done"
TODO = "todo"
RUNNING = "running"
ATTENTION = "attention"

_DETAIL_MAX = 300

_DB_ERRORS = (sqlite3.OperationalError, aiosqlite.OperationalError)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _step(step_id: str, state: str, detail: str | None = None, completed_at: str | None = None, **extra) -> dict:
    return {
        "id": step_id,
        "state": state,
        "done": state == DONE,
        "detail": detail,
        "completed_at": completed_at,
        **extra,
    }


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    text = redact_credentials(str(text)).strip()
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def _names(rows: list, limit: int = 3) -> str:
    names = [str(row[0]) for row in rows[:limit]]
    extra = len(rows) - len(names)
    return ", ".join(names) + (f" and {extra} more" if extra > 0 else "")


async def _first_backup(db: aiosqlite.Connection) -> dict:
    """The latest backup run that finished cleanly, from any job."""
    try:
        cursor = await db.execute(
            """SELECT COALESCE(completed_at, started_at) FROM job_runs
               WHERE status = 'success'
               ORDER BY started_at DESC LIMIT 1"""
        )
        row = await cursor.fetchone()
        if row:
            return _step("first_backup", DONE, completed_at=row[0])

        cursor = await db.execute("SELECT 1 FROM job_runs WHERE status = 'running' LIMIT 1")
        if await cursor.fetchone():
            return _step("first_backup", RUNNING, "A backup is running now.")

        # No clean run yet: say why the latest attempt didn't count. A partial
        # run's error_message names what was left out (a database, a folder).
        cursor = await db.execute(
            """SELECT status, error_message FROM job_runs
               WHERE status IN ('partial', 'failed')
               ORDER BY started_at DESC LIMIT 1"""
        )
        last = await cursor.fetchone()
        cursor = await db.execute("SELECT 1 FROM backup_jobs LIMIT 1")
        has_jobs = await cursor.fetchone() is not None
    except _DB_ERRORS:
        return _step("first_backup", TODO)
    if last:
        status, error = last
        if status == "failed":
            return _step("first_backup", ATTENTION, f"The last backup failed: {_clip(error) or 'no reason recorded'}")
        reason = _clip(error) or "no reason recorded"
        return _step("first_backup", ATTENTION, f"The last backup finished with warnings: {reason}")
    if not has_jobs:
        return _step("first_backup", TODO, "No backup jobs are configured.")
    return _step("first_backup", TODO)


async def _has_drillable_databases(db: aiosqlite.Connection) -> bool:
    """True when there is a database a restore drill can test.

    Drills load Postgres, MariaDB/MySQL and SQLite dumps; Redis and MongoDB
    dumps are never drilled, so a server with only those can't pass one.
    """
    drillable = tuple(sorted(DRILLABLE_TYPES))
    placeholders = ", ".join("?" for _ in drillable)
    try:
        # Discovery may be down right now; dumps this system made still count.
        cursor = await db.execute(
            f"SELECT 1 FROM job_run_databases WHERE status = 'success' AND db_type IN ({placeholders}) LIMIT 1",  # nosec B608
            drillable,
        )
        if await cursor.fetchone():
            return True
        cursor = await db.execute("SELECT databases FROM discovered_containers")
        rows = await cursor.fetchall()
    except _DB_ERRORS:
        return False
    excluded = await excluded_identities(db)
    for (raw,) in rows:
        try:
            entries = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for entry in entries if isinstance(entries, list) else []:
            if (
                isinstance(entry, dict)
                and entry.get("db_name")
                and str(entry.get("db_type") or "").lower() in DRILLABLE_TYPES
                and db_identity(entry) not in excluded
            ):
                return True
    return False


async def _nothing_to_drill(db: aiosqlite.Connection) -> str | None:
    """Why drills can't test anything here, or None.

    Looks at the latest skipped drill that got as far as choosing dumps (it
    recorded per-dump results). When it found dumps but none it can test
    (only untested types, or every dump over restore_drill_max_dump_mb), no
    drill will pass until that changes. Drills skipped for a transient reason
    (a backup was running, no target) record no results and are ignored.
    """
    try:
        cursor = await db.execute(
            """SELECT error_message FROM restore_drills d
               WHERE status = 'skipped'
                 AND EXISTS (SELECT 1 FROM restore_drill_results r WHERE r.drill_id = d.id)
               ORDER BY started_at DESC, rowid DESC LIMIT 1"""
        )
        row = await cursor.fetchone()
    except _DB_ERRORS:
        return None
    message = row[0] if row else None
    return message if message and NOTHING_TO_DRILL in message else None


async def _restore_test_by_drill(db: aiosqlite.Connection) -> dict:
    try:
        cursor = await db.execute(
            """SELECT status, completed_at, started_at, databases_tested, databases_passed, error_message
               FROM restore_drills WHERE status IN ('passed', 'failed')
               ORDER BY started_at DESC, rowid DESC LIMIT 1"""
        )
        decisive = await cursor.fetchone()
        if decisive and decisive[0] == "passed":
            _status, completed_at, started_at, tested, passed, _error = decisive
            return _step(
                "restore_test",
                DONE,
                f"{passed} of {tested} database{'s' if tested != 1 else ''} restored in the last drill",
                completed_at or started_at,
                method="drill",
            )
        cursor = await db.execute("SELECT 1 FROM restore_drills WHERE status = 'running' LIMIT 1")
        if await cursor.fetchone():
            return _step("restore_test", RUNNING, "A restore drill is running now.", method="drill")
        if decisive:
            return _step(
                "restore_test",
                ATTENTION,
                _clip(decisive[5]) or "The last restore drill failed.",
                method="drill",
            )
        cursor = await db.execute(
            """SELECT error_message FROM restore_drills WHERE status = 'skipped'
               ORDER BY started_at DESC, rowid DESC LIMIT 1"""
        )
        skipped = await cursor.fetchone()
    except _DB_ERRORS:
        return _step("restore_test", TODO, method="drill")
    detail = f"The last drill was skipped: {_clip(skipped[0])}" if skipped and skipped[0] else None
    return _step("restore_test", TODO, detail, method="drill")


async def _restore_test_by_verification(db: aiosqlite.Connection) -> dict:
    """Nothing to drill: a verification whose file restore test ran is the proof."""
    try:
        cursor = await db.execute(
            """SELECT status, completed_at, started_at, error_message FROM verification_runs
               WHERE target_id IN (SELECT id FROM storage_targets)
                 AND (status = 'failed' OR (status = 'passed' AND files_checked > 0))
               ORDER BY started_at DESC, rowid DESC LIMIT 1"""
        )
        decisive = await cursor.fetchone()
        if decisive and decisive[0] == "passed":
            return _step(
                "restore_test",
                DONE,
                "A file restored from the latest snapshot matched its checksum",
                decisive[1] or decisive[2],
                method="verification",
            )
        cursor = await db.execute("SELECT 1 FROM verification_runs WHERE status = 'running' LIMIT 1")
        if await cursor.fetchone():
            return _step("restore_test", RUNNING, "A restore test is running now.", method="verification")
    except _DB_ERRORS:
        return _step("restore_test", TODO, method="verification")
    if decisive:
        return _step(
            "restore_test",
            ATTENTION,
            _clip(decisive[3]) or "The last restore test failed.",
            method="verification",
        )
    return _step("restore_test", TODO, method="verification")


async def _offsite_target(db: aiosqlite.Connection) -> dict:
    try:
        cursor = await db.execute("SELECT name, type, enabled FROM storage_targets ORDER BY created_at")
        targets = await cursor.fetchall()
    except _DB_ERRORS:
        return _step("offsite_target", TODO)
    offsite = [t for t in targets if t[1] != "local"]
    enabled = [t for t in offsite if t[2]]
    if enabled:
        return _step("offsite_target", DONE, _names(enabled))
    if offsite:
        return _step("offsite_target", TODO, f"Disabled: {_names(offsite)}")
    if targets:
        return _step("offsite_target", TODO, f"Only local targets are configured: {_names(targets)}")
    return _step("offsite_target", TODO)


async def _notifications(db: aiosqlite.Connection) -> dict:
    try:
        cursor = await db.execute("SELECT name, enabled FROM notification_channels ORDER BY created_at")
        channels = await cursor.fetchall()
    except _DB_ERRORS:
        return _step("notifications", TODO)
    enabled = [c for c in channels if c[1]]
    if enabled:
        return _step("notifications", DONE, _names(enabled))
    if channels:
        return _step("notifications", TODO, f"Disabled: {_names(channels)}")
    return _step("notifications", TODO)


async def _recovery_kit(db: aiosqlite.Connection) -> dict:
    confirmed_at = await get_setting(db, "recovery_kit_confirmed_at", "")
    if await recovery_kit_confirmed(db):
        return _step("recovery_kit", DONE, completed_at=confirmed_at or None)
    if confirmed_at:
        return _step(
            "recovery_kit",
            ATTENTION,
            "The encryption password changed after the last kit was saved; save a new one.",
        )
    return _step("recovery_kit", TODO)


async def _restore_test(db: aiosqlite.Connection) -> dict:
    """A passed drill when a drill can test something here, else a file restore test."""
    if not await _has_drillable_databases(db):
        return await _restore_test_by_verification(db)
    step = await _restore_test_by_drill(db)
    if step["state"] != TODO:
        return step
    reason = await _nothing_to_drill(db)
    if reason is None:
        return step
    fallback = await _restore_test_by_verification(db)
    if fallback["state"] == TODO:
        fallback["detail"] = f"The last drill found nothing it can test: {_clip(reason)}"
    return fallback


async def evaluate_setup_checklist(db: aiosqlite.Connection) -> dict:
    """The checklist as served by GET /api/setup-checklist."""
    steps = [
        await _first_backup(db),
        await _restore_test(db),
        await _offsite_target(db),
        await _notifications(db),
        await _recovery_kit(db),
    ]
    completed = sum(1 for step in steps if step["done"])
    dismissed_at = await get_setting(db, DISMISSED_SETTING, "") or None
    return {
        "complete": completed == len(steps),
        "completed_count": completed,
        "total": len(steps),
        "dismissed": dismissed_at is not None,
        "dismissed_at": dismissed_at,
        "steps": steps,
    }


@router.get("")
async def get_setup_checklist(db: aiosqlite.Connection = Depends(get_db)):
    """What is left to do before this server counts as protected."""
    return await evaluate_setup_checklist(db)


@router.post("/dismiss")
async def dismiss_setup_checklist(db: aiosqlite.Connection = Depends(get_db)):
    """Hide the checklist for good. Only allowed once every step is done."""
    checklist = await evaluate_setup_checklist(db)
    if checklist["dismissed"]:
        return checklist
    if not checklist["complete"]:
        remaining = [step["id"] for step in checklist["steps"] if not step["done"]]
        raise HTTPException(
            status_code=409,
            detail=f"Finish every step before dismissing the checklist (remaining: {', '.join(remaining)})",
        )
    now = _now()
    await set_setting(db, DISMISSED_SETTING, now)
    await log_activity(db, "system", "setup_checklist", "Setup checklist completed: this server is protected")
    logger.info("Setup checklist dismissed")
    return {**checklist, "dismissed": True, "dismissed_at": now}
