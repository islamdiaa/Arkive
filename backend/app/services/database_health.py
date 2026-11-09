"""Per-database backup health.

For every discovered database: when it was last dumped successfully, how big
its dumps are and how that changed over recent runs, the last restore-drill
result for it, and the newest point in time it can be restored to from a
snapshot ("restorable as of").

Also the dump-shrink check (``shrink_warnings``): a run whose dump of a
database is much smaller than that database's recent dumps is ``partial``.

Shrink threshold
----------------
A dump is flagged when it is at least ``dump_shrink_alert_percent`` (default
50) percent smaller than the median of the database's previous
``BASELINE_DUMPS`` (5) successful dumps in the same format, provided there are
at least ``MIN_BASELINE_DUMPS`` (3) of them and that median is at least
``dump_shrink_min_mb`` (default 1 MB).

Why these numbers: a flagged dump makes the run ``partial`` and sends a
notification, so the rule must almost never fire on healthy databases or
people learn to ignore partial runs (the failure mode v0.3.0 fixed). Logical
dumps (pg_dump, mysqldump, mongodump) measure live data and do not shrink with
VACUUM/OPTIMIZE; day to day they move by a few percent. Losing half of a
database between two runs is what an app re-initialising an empty database, a
container pointed at the wrong database after an update, a botched migration
or a mass deletion look like — cases a backup must not report as a plain
success. (The small dump is still uploaded; earlier snapshots keep the
previous one, and the warning says where to look.) The median of several dumps (not just the
previous one) keeps one odd dump from setting the baseline; requiring three
avoids judging a database on its first runs; the size floor keeps tiny
settings/session databases, where a few kilobytes are a large fraction, from
alerting. Comparing only same-format dumps keeps a ``dump_compression`` change
from looking like data loss.

SQLite dumps are page copies that do shrink after a VACUUM; only a VACUUM that
reclaims more than half the file crosses the threshold. A real, permanent
shrink warns until it becomes the baseline (at most three runs with the
defaults) or until it is accepted on the Databases page
(``accept_current_size``). ``dump_shrink_alert_percent = 0`` turns the check
off.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from app.services.db_dumper import _DUMP_TS_PATTERN, _HASHED_PREFIX, dump_prefix
from app.services.db_exclusions import excluded_identities, identity
from app.services.settings_store import get_int_setting, get_setting, set_setting

logger = logging.getLogger("arkive.database_health")

SHRINK_PERCENT_KEY = "dump_shrink_alert_percent"
SHRINK_MIN_MB_KEY = "dump_shrink_min_mb"
# JSON {db_key: ISO time}: dumps before that time are ignored for the size
# baseline (set when the user accepts a database's new size).
SIZE_BASELINES_KEY = "dump_size_baselines"

BASELINE_DUMPS = 5
MIN_BASELINE_DUMPS = 3
MAX_SHRINK_PERCENT = 95
TREND_POINTS = 14
# Runs (with database records) read for history; plenty for the trend and the
# baseline, and bounded so the Databases page stays fast after years of runs.
HISTORY_RUNS = 200
MIB = 1024 * 1024

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(UTC).strftime(_TS_FORMAT)


def human_size(size_bytes: int) -> str:
    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# Dump file names and identities
# ---------------------------------------------------------------------------


def _norm_type(db_type: str | None) -> str:
    db_type = (db_type or "").lower()
    return "mariadb" if db_type == "mysql" else db_type


def legacy_identity(container_name: str | None, db_type: str | None, db_name: str | None) -> tuple[str, str, str]:
    """(container, type, name): how records without a db_key are matched."""
    return (container_name or "", _norm_type(db_type), db_name or "")


def dump_file_key(path: str | None) -> str | None:
    """Database identity encoded in a dump file name (``<dump_prefix>_<ts>.<ext>``).

    None for names without the identity hash (dumps made before it existed).
    """
    match = _DUMP_TS_PATTERN.match(os.path.basename(path or ""))
    if not match:
        return None
    prefix = match.group(1)
    return prefix if _HASHED_PREFIX.match(prefix) else None


def dump_file_time(path: str | None) -> str | None:
    """When a dump began, from the UTC timestamp in its file name."""
    match = _DUMP_TS_PATTERN.match(os.path.basename(path or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2), "%Y%m%d_%H%M%S").strftime(_TS_FORMAT)
    except ValueError:
        return None


def dump_format(path: str | None) -> str:
    """Extension after a dump file's timestamp (``.sql``, ``.sql.gz``, ``.db``...)."""
    name = os.path.basename(path or "")
    match = _DUMP_TS_PATTERN.match(name)
    return name[match.end() - 1 :].lower() if match else ""


def result_key(result) -> str:
    """Identity of a DumpResult: its db_key, else the key in its file name."""
    key = getattr(result, "db_key", None) or dump_file_key(getattr(result, "dump_path", ""))
    if key:
        return key
    return dump_prefix(
        result.container_name, result.db_type, result.db_name or "", None, kind=getattr(result, "kind", "database")
    )


# ---------------------------------------------------------------------------
# Shrink detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShrinkPolicy:
    percent: int = 50
    min_bytes: int = MIB

    @property
    def enabled(self) -> bool:
        return self.percent > 0

    def as_dict(self) -> dict:
        return {
            "shrink_alert_percent": self.percent,
            "shrink_min_mb": self.min_bytes // MIB,
            "baseline_dumps": BASELINE_DUMPS,
            "min_baseline_dumps": MIN_BASELINE_DUMPS,
        }


@dataclass(frozen=True)
class ShrinkCheck:
    size_bytes: int
    baseline_bytes: int
    drop_percent: int


async def read_shrink_policy(db: aiosqlite.Connection) -> ShrinkPolicy:
    percent = await get_int_setting(db, SHRINK_PERCENT_KEY, minimum=0)
    min_mb = await get_int_setting(db, SHRINK_MIN_MB_KEY, minimum=0)
    return ShrinkPolicy(percent=min(percent, MAX_SHRINK_PERCENT), min_bytes=min_mb * MIB)


def shrink_baseline(previous: list[int]) -> int | None:
    """Median of the newest BASELINE_DUMPS sizes (newest first); None if too few."""
    sizes = [s for s in previous[:BASELINE_DUMPS] if s > 0]
    if len(sizes) < MIN_BASELINE_DUMPS:
        return None
    return int(statistics.median(sizes))


def detect_shrink(size_bytes: int, previous: list[int], policy: ShrinkPolicy) -> ShrinkCheck | None:
    """A ShrinkCheck when ``size_bytes`` is a sharp drop from ``previous``.

    ``previous`` holds earlier successful dump sizes of the same database and
    format, newest first.
    """
    if not policy.enabled:
        return None
    baseline = shrink_baseline(previous)
    if baseline is None or baseline < max(policy.min_bytes, 1):
        return None
    drop = (baseline - max(size_bytes, 0)) * 100 / baseline
    if drop < policy.percent:
        return None
    return ShrinkCheck(size_bytes=max(size_bytes, 0), baseline_bytes=baseline, drop_percent=int(drop))


def shrink_message(label: str, check: ShrinkCheck) -> str:
    return (
        f"{label}: dump is {check.drop_percent}% smaller than its recent median "
        f"({human_size(check.size_bytes)} vs {human_size(check.baseline_bytes)}); check the database, "
        "or accept the new size on the Databases page"
    )


async def read_size_baselines(db: aiosqlite.Connection) -> dict[str, str]:
    raw = await get_setting(db, SIZE_BASELINES_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@dataclass
class DumpRecord:
    """One database's dump attempt in one run (a job_run_databases row)."""

    row_id: int
    run_id: str
    run_status: str
    key: str | None
    legacy: tuple[str, str, str]
    status: str
    size_bytes: int
    dumped_at: str
    dump_file: str
    integrity_check: str | None
    error: str | None

    @property
    def format(self) -> str:
        return dump_format(self.dump_file)


async def load_dump_history(
    db: aiosqlite.Connection,
    *,
    run_limit: int = HISTORY_RUNS,
    exclude_run_id: str | None = None,
    containers: Iterable[str] | None = None,
) -> list[DumpRecord]:
    """Dump records of the newest ``run_limit`` runs that dumped anything, newest first.

    job_run_databases.host_path holds the dump file path (a historical name).
    """
    params: list = [run_limit]
    extra = ""
    if exclude_run_id is not None:
        extra += " AND jrd.run_id != ?"
        params.append(exclude_run_id)
    container_list = sorted(set(containers)) if containers is not None else None
    if container_list is not None:
        if not container_list:
            return []
        extra += f" AND jrd.container_name IN ({','.join('?' for _ in container_list)})"  # nosec B608
        params.extend(container_list)
    # ``extra`` only adds fixed conditions with "?" placeholders.
    try:
        cursor = await db.execute(
            f"""SELECT jrd.id, jrd.run_id, jrd.container_name, jrd.db_type, jrd.db_name,
                       jrd.dump_size_bytes, jrd.status, jrd.host_path, jrd.integrity_check,
                       jrd.error, jrd.db_key, jrd.dumped_at, jr.started_at, jr.status
                FROM job_run_databases jrd
                JOIN job_runs jr ON jr.id = jrd.run_id
                WHERE jrd.run_id IN (
                    SELECT r.id FROM job_runs r
                    WHERE EXISTS (SELECT 1 FROM job_run_databases d WHERE d.run_id = r.id)
                    ORDER BY r.started_at DESC LIMIT ?){extra}""",  # nosec B608
            params,
        )
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError) as exc:
        logger.warning("Could not read dump history: %s", exc)
        return []

    records = []
    for (
        row_id,
        run_id,
        container,
        db_type,
        db_name,
        size,
        status,
        dump_path,
        integrity,
        error,
        db_key,
        dumped_at,
        run_started,
        run_status,
    ) in rows:
        dump_file = os.path.basename(dump_path or "")
        records.append(
            DumpRecord(
                row_id=row_id,
                run_id=run_id,
                run_status=run_status or "",
                key=db_key or dump_file_key(dump_file),
                legacy=legacy_identity(container, db_type, db_name),
                status=status or "",
                size_bytes=int(size or 0),
                dumped_at=dumped_at or dump_file_time(dump_file) or run_started or "",
                dump_file=dump_file,
                integrity_check=integrity,
                error=error,
            )
        )
    records.sort(key=lambda r: (r.dumped_at, r.row_id), reverse=True)
    return records


class _Matcher:
    """Maps records to known database identities.

    A record with an identity matches only that identity; one without (rows
    and files from before identities were recorded) matches by container,
    type and name when exactly one known database has them.
    """

    def __init__(self, known: Iterable[tuple[str, tuple[str, str, str]]]):
        self.keys: set[str] = set()
        legacy: dict[tuple[str, str, str], set[str]] = {}
        for key, legacy_id in known:
            self.keys.add(key)
            legacy.setdefault(legacy_id, set()).add(key)
        self.legacy = {k: next(iter(v)) for k, v in legacy.items() if len(v) == 1}

    def match(self, key: str | None, legacy_id: tuple[str, str, str]) -> str | None:
        if key:
            return key if key in self.keys else None
        return self.legacy.get(legacy_id)


def _previous_sizes(records: list[DumpRecord], fmt: str, since: str | None) -> list[int]:
    """Successful sizes in ``fmt``, newest first, not older than ``since``."""
    return [
        r.size_bytes
        for r in records
        if r.status == "success" and r.format == fmt and (not since or r.dumped_at >= since)
    ]


async def shrink_warnings(db: aiosqlite.Connection, run_id: str, dump_results: list) -> list[str]:
    """Run warnings for this run's database dumps that shrank sharply."""
    policy = await read_shrink_policy(db)
    if not policy.enabled:
        return []
    current = [r for r in dump_results if getattr(r, "kind", "database") == "database" and r.status == "success"]
    if not current:
        return []
    keyed = [(result_key(r), r) for r in current]
    matcher = _Matcher(
        (result_key(r), legacy_identity(r.container_name, r.db_type, r.db_name))
        for r in dump_results
        if getattr(r, "kind", "database") == "database"
    )
    history = await load_dump_history(db, exclude_run_id=run_id, containers={r.container_name for r in current})
    by_key: dict[str, list[DumpRecord]] = {}
    for record in history:
        key = matcher.match(record.key, record.legacy)
        if key:
            by_key.setdefault(key, []).append(record)
    baselines = await read_size_baselines(db)

    warnings = []
    for key, result in keyed:
        previous = _previous_sizes(by_key.get(key, []), dump_format(result.dump_path), baselines.get(key))
        check = detect_shrink(int(result.dump_size_bytes or 0), previous, policy)
        if check:
            label = f"{result.container_name}/{result.db_name}"
            logger.warning(
                "Dump of %s shrank %d%% (%d bytes vs median %d)",
                label,
                check.drop_percent,
                check.size_bytes,
                check.baseline_bytes,
            )
            warnings.append(shrink_message(label, check))
    return warnings


# ---------------------------------------------------------------------------
# Per-database health
# ---------------------------------------------------------------------------


@dataclass
class _Known:
    key: str
    entry: dict
    excluded: bool
    records: list[DumpRecord] = field(default_factory=list)
    drills: list[dict] = field(default_factory=list)


async def _discovered(db: aiosqlite.Connection) -> list[dict]:
    try:
        cursor = await db.execute("SELECT name, databases FROM discovered_containers ORDER BY name")
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for name, raw in rows:
        try:
            databases = json.loads(raw or "[]")
        except (TypeError, ValueError):
            databases = []
        for entry in databases:
            if not isinstance(entry, dict) or not entry.get("db_type"):
                continue
            entry = {
                "container_name": entry.get("container_name") or name,
                "db_type": entry.get("db_type"),
                "db_name": entry.get("db_name") or "",
                "host_path": entry.get("host_path"),
            }
            key = identity(entry)
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


async def _latest_upload(db: aiosqlite.Connection) -> str | None:
    """When the newest snapshot that still counts was written.

    A snapshot always holds the dump directory, and the dump directory keeps
    each database's newest successful dump until a newer one replaces it, so
    every dump that existed when a snapshot was written is in it. Runs never
    overlap (backup lock), so a dump that began before a run finished existed
    when that run uploaded. Targets that were deleted don't count.
    """
    try:
        cursor = await db.execute(
            """SELECT MAX(COALESCE(jr.completed_at, jr.started_at))
               FROM job_run_targets t JOIN job_runs jr ON jr.id = t.run_id
               WHERE t.status IN ('success', 'partial')
                 AND COALESCE(t.snapshot_id, '') != ''
                 AND t.target_id IN (SELECT id FROM storage_targets)"""
        )
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return None
    return row[0] if row and row[0] else None


async def _drill_results(db: aiosqlite.Connection, limit: int = 5000) -> list[dict]:
    try:
        cursor = await db.execute(
            """SELECT r.container_name, r.db_type, r.db_name, r.dump_file, r.status, r.detail,
                      d.id, COALESCE(d.completed_at, d.started_at), d.snapshot_id
               FROM restore_drill_results r JOIN restore_drills d ON d.id = r.drill_id
               WHERE d.status != 'running'
               ORDER BY d.started_at DESC, r.id DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return []
    keys = ("container_name", "db_type", "db_name", "dump_file", "status", "detail", "drill_id", "at", "snapshot_id")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _last_drill(drills: list[dict]) -> dict | None:
    """Newest passed/failed result; else the newest skip (why it is never tested)."""
    chosen = next((d for d in drills if d["status"] in ("passed", "failed")), None)
    chosen = chosen or (drills[0] if drills else None)
    if chosen is None:
        return None
    return {
        "status": chosen["status"],
        "at": chosen["at"],
        "drill_id": chosen["drill_id"],
        "detail": chosen["detail"],
        "dump_file": chosen["dump_file"],
        "dump_dumped_at": dump_file_time(chosen["dump_file"]),
        "snapshot_id": chosen["snapshot_id"],
    }


def _point(record: DumpRecord, shrunk: bool) -> dict:
    return {
        "run_id": record.run_id,
        "at": record.dumped_at,
        "status": record.status,
        "size_bytes": record.size_bytes if record.status == "success" else None,
        "shrunk": shrunk,
    }


def _health(known: _Known, policy: ShrinkPolicy, since: str | None, latest_upload: str | None) -> dict:
    records = known.records  # newest first
    attempts = [r for r in records if r.status in ("success", "failed")]
    successes = [r for r in records if r.status == "success"]
    last_attempt = records[0] if records else None
    last_success = successes[0] if successes else None

    baseline = shrink = None
    if last_success:
        previous = _previous_sizes(successes[1:], last_success.format, since)
        baseline = shrink_baseline(previous)
        shrink = detect_shrink(last_success.size_bytes, previous, policy)

    trend = []
    for record in attempts[:TREND_POINTS]:
        flagged = False
        if record.status == "success":
            older = [r for r in successes if (r.dumped_at, r.row_id) < (record.dumped_at, record.row_id)]
            previous = _previous_sizes(older, record.format, since)
            flagged = detect_shrink(record.size_bytes, previous, policy) is not None
        trend.append(_point(record, flagged))
    trend.reverse()  # oldest first, for charts

    restorable_as_of = None
    if latest_upload:
        restorable_as_of = next((r.dumped_at for r in successes if r.dumped_at <= latest_upload), None)

    last_drill = _last_drill(known.drills)
    # issues: something is wrong (status "warning" at least); notes: context.
    issues: list[str] = []
    notes: list[str] = []
    if known.excluded:
        status = "excluded"
    elif last_attempt is None:
        status = "never_dumped"
        issues.append("No dump recorded yet")
    elif last_attempt.status == "failed":
        status = "failed"
        issues.append(f"Last dump failed: {(last_attempt.error or 'no error recorded')[:300]}")
    elif last_success is None:
        status = "never_dumped"
        issues.append("No successful dump recorded")
    else:
        status = "ok"
    if not known.excluded:
        if last_attempt is not None and last_attempt.status == "skipped":
            notes.append(f"Last run skipped it: {(last_attempt.error or 'skipped')[:300]}")
        if shrink:
            issues.append(
                f"Latest dump is {shrink.drop_percent}% smaller than its recent median "
                f"({human_size(shrink.size_bytes)} vs {human_size(shrink.baseline_bytes)})"
            )
        if last_drill and last_drill["status"] == "failed":
            issues.append("Last restore drill failed")
        # A run that is still going uploads after it dumps.
        if last_success and last_success.run_status != "running":
            if restorable_as_of is None:
                issues.append("No snapshot holds a successful dump yet")
            elif restorable_as_of < last_success.dumped_at:
                issues.append("The newest dump is not in a snapshot yet")
        if status == "ok" and issues:
            status = "warning"

    entry = known.entry
    return {
        "key": known.key,
        "container_name": entry["container_name"],
        "db_type": entry["db_type"],
        "db_name": entry["db_name"],
        "host_path": entry.get("host_path"),
        "excluded": known.excluded,
        "status": status,
        "issues": issues,
        "notes": notes,
        "last_attempt": (
            {
                "status": last_attempt.status,
                "at": last_attempt.dumped_at,
                "run_id": last_attempt.run_id,
                "error": last_attempt.error,
            }
            if last_attempt
            else None
        ),
        "last_success_at": last_success.dumped_at if last_success else None,
        "last_success_run_id": last_success.run_id if last_success else None,
        "size_bytes": last_success.size_bytes if last_success else None,
        "integrity_check": last_success.integrity_check if last_success else None,
        "baseline_bytes": baseline,
        "size_change_percent": _change_percent(successes),
        "shrunk": shrink is not None,
        "shrink_percent": shrink.drop_percent if shrink else None,
        "trend": trend,
        "last_drill": last_drill,
        "restorable_as_of": restorable_as_of,
    }


def _change_percent(successes: list[DumpRecord]) -> float | None:
    """Change of the newest successful dump versus the one before (same format)."""
    if len(successes) < 2:
        return None
    newest = successes[0]
    previous = next((r for r in successes[1:] if r.format == newest.format), None)
    if previous is None or previous.size_bytes <= 0:
        return None
    return round((newest.size_bytes - previous.size_bytes) * 100 / previous.size_bytes, 1)


async def database_health(db: aiosqlite.Connection) -> dict:
    """Health of every discovered database (see module docstring)."""
    entries = await _discovered(db)
    excluded = await excluded_identities(db)
    known = {identity(e): _Known(key=identity(e), entry=e, excluded=identity(e) in excluded) for e in entries}
    matcher = _Matcher(
        (key, legacy_identity(k.entry["container_name"], k.entry["db_type"], k.entry["db_name"]))
        for key, k in known.items()
    )

    if known:
        for record in await load_dump_history(db):
            key = matcher.match(record.key, record.legacy)
            if key:
                known[key].records.append(record)
        for drill in await _drill_results(db):
            key = matcher.match(
                dump_file_key(drill["dump_file"]),
                legacy_identity(drill["container_name"], drill["db_type"], drill["db_name"]),
            )
            if key:
                known[key].drills.append(drill)

    policy = await read_shrink_policy(db)
    baselines = await read_size_baselines(db)
    latest_upload = await _latest_upload(db) if known else None
    items = [_health(k, policy, baselines.get(key), latest_upload) for key, k in known.items()]

    summary = {"ok": 0, "warning": 0, "failed": 0, "never_dumped": 0, "excluded": 0}
    for item in items:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    return {
        "items": items,
        "databases": items,
        "total": len(items),
        "summary": summary,
        "policy": policy.as_dict(),
        "latest_upload_at": latest_upload,
        "generated_at": _now(),
    }


async def accept_current_size(db: aiosqlite.Connection, key: str) -> dict | None:
    """Make a database's newest successful dump the start of its size baseline.

    Returns the database (container_name, db_type, db_name, host_path) and
    ``accepted_from``, the dump time the baseline now starts from; None when
    the database is not discovered or has no successful dump. Entries of
    databases that are no longer discovered are dropped.
    """
    entries = await _discovered(db)
    known_keys = {identity(e) for e in entries}
    if key not in known_keys:
        return None
    target = next(e for e in entries if identity(e) == key)
    matcher = _Matcher((identity(e), legacy_identity(e["container_name"], e["db_type"], e["db_name"])) for e in entries)
    history = await load_dump_history(db, containers={target["container_name"]})
    latest = next(
        (r for r in history if r.status == "success" and matcher.match(r.key, r.legacy) == key),
        None,
    )
    if latest is None:
        return None
    baselines = {k: v for k, v in (await read_size_baselines(db)).items() if k in known_keys}
    baselines[key] = latest.dumped_at
    await set_setting(db, SIZE_BASELINES_KEY, json.dumps(baselines, sort_keys=True))
    return {**target, "accepted_from": latest.dumped_at}
