"""Backup orchestrator — coordinates the full backup pipeline."""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.config import ArkiveConfig
from app.core.context import current_run_id
from app.core.event_bus import EventBus
from app.core.security import decrypt_config
from app.services.backup_engine import EXCLUDE_MARKER, BackupEngine
from app.services.cloud_manager import CloudManager
from app.services.db_dumper import (
    PARTIAL_SUFFIX,
    DBDumper,
    DumpResult,
    cleanup_dump_dir,
    dump_prefix,
    legacy_dump_prefix,
)
from app.services.discovery import DiscoveryEngine
from app.services.discovery_persistence import persist_discovery_results
from app.services.flash_backup import FlashBackup
from app.services.lock_manager import (
    BackupLockManager,
    _get_proc_start_time,  # noqa: F401 — re-export for backward compat
)
from app.services.notifier import Notifier
from app.services.repo_paths import build_repo_path
from app.services.retention import read_retention_settings
from app.utils.mounts import host_path_for

logger = logging.getLogger("arkive.orchestrator")

# Job types that refresh each artifact. Unknown/custom types behave like "full".
DATABASE_JOB_TYPES = {"full", "db_dump", "custom"}
FLASH_JOB_TYPES = {"full", "flash", "custom"}
# Dump methods that exec into the database container (need it running).
EXEC_DUMP_TYPES = {"postgres", "mariadb", "mysql", "mongodb", "redis"}
_EXCLUDE_MARKER_TEXT = "Arkive storage directory — excluded from Arkive backups.\n"

LOCK_FILE = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / "backup.lock"
RESTORE_LOCK_FILE = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / "restore.lock"


def cleanup_stale_backup_lock(config_dir: Path | None = None) -> bool:
    """Remove a stale backup.lock proactively on startup or before manual runs."""
    mgr = BackupLockManager(config_dir)
    return mgr.cleanup_stale_backup_lock()


DEFAULT_MIN_DISK_BYTES = 1 * 1024**3  # 1 GB
DEFAULT_WARN_DISK_BYTES = 5 * 1024**3  # 5 GB


ERROR_CATEGORIES: dict[str, dict[str, Any]] = {
    "auth_error": {
        "patterns": [
            "401",
            "403",
            "token expired",
            "unauthorized",
            "forbidden",
            "auth",
            "credential",
        ],
        "severity": "HIGH",
        "auto_retry": False,
        "user_action": "Re-authenticate the storage target",
    },
    "network_error": {
        "patterns": [
            "connection refused",
            "timeout",
            "dns",
            "network",
            "unreachable",
            "connect",
            "resolve",
        ],
        "severity": "MEDIUM",
        "auto_retry": True,
        "user_action": "Check network connectivity",
    },
    "storage_full": {
        "patterns": [
            "quota",
            "storage cap",
            "exceeded",
            "no space left on device",
            "disk full",
            "enospc",
            "disk quota",
        ],
        "severity": "CRITICAL",
        "auto_retry": False,
        "user_action": "Free disk space or increase storage quota",
    },
    "permission_error": {
        "patterns": [
            "permission denied",
            "access denied",
            "eperm",
            "eacces",
        ],
        "severity": "HIGH",
        "auto_retry": False,
        "user_action": "Check file/directory permissions",
    },
    "container_error": {
        "patterns": [
            "not running",
            "exec_run",
            "container not found",
            "no such container",
            "is not running",
        ],
        "severity": "MEDIUM",
        "auto_retry": False,
        "user_action": "Start the container",
    },
    "dump_error": {
        "patterns": [
            "integrity_check",
            "corrupt",
            "malformed",
            "database disk image",
            "dump failed",
        ],
        "severity": "CRITICAL",
        "auto_retry": False,
        "user_action": "Investigate database corruption",
    },
    "restic_error": {
        "patterns": [
            "repository is already locked",
            "unable to create lock",
            "restic",
            "snapshot",
        ],
        "severity": "LOW",
        "auto_retry": True,
        "user_action": None,
    },
    "unknown": {
        "patterns": [],
        "severity": "MEDIUM",
        "auto_retry": False,
        "user_action": "Check the error details",
    },
}


def categorize_error(error_str: str) -> str:
    """Classify an error string into one of the known error categories."""
    error_lower = error_str.lower()
    for category, info in ERROR_CATEGORIES.items():
        if category == "unknown":
            continue
        for pattern in info["patterns"]:
            if pattern in error_lower:
                return category
    return "unknown"


class _CancelledError(Exception):
    """Raised internally when a specific run is cancelled."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Run {run_id} was cancelled")


_PHASE_ORDER = [
    "discovering",
    "dumping_databases",
    "flash_backup",
    "uploading",
    "retention_cleanup",
    "refreshing_snapshots",
]


class BackupOrchestrator:
    """Coordinates the full backup pipeline."""

    # How long a scheduled run waits for a running backup/restore to finish
    # before it is recorded as skipped.
    scheduled_lock_wait_seconds: float = 3 * 3600
    lock_poll_seconds: float = 15
    # Discovery retries while the Docker daemon is unreachable.
    discovery_attempts: int = 3
    discovery_retry_seconds: float = 20
    # Dumps older than this are removed (containers that no longer exist).
    dump_max_age_days: int = 7

    def __init__(
        self,
        discovery: DiscoveryEngine | None,
        db_dumper: DBDumper | None,
        flash_backup: FlashBackup,
        backup_engine: BackupEngine,
        cloud_manager: CloudManager,
        notifier: Notifier,
        event_bus: EventBus,
        config: ArkiveConfig,
    ):
        self.discovery = discovery
        self.db_dumper = db_dumper
        self.flash_backup = flash_backup
        self.backup_engine = backup_engine
        self.cloud_manager = cloud_manager
        self.notifier = notifier
        self.event_bus = event_bus
        self.config = config
        self.lock_manager = BackupLockManager(config.config_dir)
        self._cancel_requested = False
        self._active_runs: dict[str, bool] = {}

    def _lock_conflict_message(self) -> str:
        """Return the best available explanation for a lock acquisition failure."""
        return self.lock_manager.lock_conflict_message()

    async def _mark_run_conflict(self, run_id: str, job_id: str, trigger: str, message: str) -> str:
        """Persist a terminal run when startup is blocked by a lock conflict."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        terminal_status = "skipped" if trigger == "scheduled" else "failed"
        severity = "info" if trigger == "scheduled" else "warning"
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO job_runs
                   (id, job_id, status, trigger, started_at, completed_at, duration_seconds, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (run_id, job_id, terminal_status, trigger, now, now, message),
            )
            await db.execute(
                """UPDATE job_runs
                   SET status = ?, completed_at = ?, duration_seconds = 0, error_message = ?
                   WHERE id = ?""",
                (terminal_status, now, message, run_id),
            )
            await db.execute(
                """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "backup",
                    "completed",
                    message,
                    json.dumps({"run_id": run_id, "job_id": job_id, "status": terminal_status}),
                    severity,
                    now,
                ),
            )
            await db.commit()
        return terminal_status

    def _acquire_lock(self, run_id: str | None = None) -> bool:
        """Acquire backup lock atomically. Returns False if already locked."""
        return self.lock_manager.acquire_backup_lock(run_id)

    def _release_lock(self) -> None:
        """Release backup lock."""
        self.lock_manager.release_backup_lock()

    def _is_cancelled(self, run_id: str) -> bool:
        return self._cancel_requested or self._active_runs.get(run_id, False)

    def _check_cancelled(self, run_id: str) -> None:
        if self._is_cancelled(run_id):
            raise _CancelledError(run_id)

    def cancel_run(self, run_id: str) -> bool:
        """Request cancellation for a specific run ID."""
        if run_id in self._active_runs:
            self._active_runs[run_id] = True
            return True
        return False

    async def _acquire_lock_waiting(self, run_id: str, wait_seconds: float) -> bool:
        """Acquire the backup lock, waiting up to ``wait_seconds`` for it.

        Scheduled jobs that fire while another backup (or a restore) is running
        queue behind it instead of being skipped — with the default schedules
        the flash and DB jobs both fire at 06:00 and one of them used to be
        skipped every single day.
        """
        if self._acquire_lock(run_id):
            return True
        if wait_seconds <= 0:
            return False
        logger.info(
            "Backup run %s waiting up to %ds for the running operation to finish: %s",
            run_id,
            int(wait_seconds),
            self._lock_conflict_message(),
        )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(self.lock_poll_seconds, max(deadline - time.monotonic(), 0)))
            if self._acquire_lock(run_id):
                logger.info("Backup run %s acquired the lock after waiting", run_id)
                return True
        return False

    async def _discover_with_retry(self) -> list:
        """Run discovery, retrying while the Docker daemon is unreachable."""
        last_error: Exception | None = None
        for attempt in range(1, self.discovery_attempts + 1):
            try:
                return await self.discovery.scan()
            except Exception as e:
                last_error = e
                if attempt < self.discovery_attempts:
                    logger.warning(
                        "Discovery attempt %d/%d failed, retrying in %ds: %s",
                        attempt,
                        self.discovery_attempts,
                        self.discovery_retry_seconds,
                        e,
                    )
                    await asyncio.sleep(self.discovery_retry_seconds)
        assert last_error is not None
        raise last_error

    # A drop to zero databases keeps warning for this long after the last run
    # that found some (then it's taken as intentional, e.g. containers removed).
    database_loss_window_days = 30

    async def _previous_database_count(self, job_id: str, run_id: str) -> int:
        """Databases found by this job's most recent run that found any.

        Comparing only with the immediately preceding run would warn once and
        then accept 5 -> 0 -> 0 as normal.
        """
        async with aiosqlite.connect(self.config.db_path) as db:
            cursor = await db.execute(
                """SELECT databases_discovered FROM job_runs
                   WHERE job_id = ? AND id != ? AND status IN ('success', 'partial')
                     AND databases_discovered > 0
                     AND started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
                   ORDER BY started_at DESC LIMIT 1""",
                (job_id, run_id, f"-{self.database_loss_window_days} days"),
            )
            row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    async def run_backup(
        self,
        job_id: str,
        trigger: str = "manual",
        skip_databases: bool = False,
        skip_flash: bool = False,
        dry_run: bool = False,
        run_id: str | None = None,
        lock_wait_seconds: float | None = None,
        **kwargs,
    ) -> dict:
        """Execute the full backup pipeline.

        Pipeline steps:
        1. Acquire lock (scheduled runs wait for a running backup to finish)
        2. Create job_run record
        3. Discovery scan
        4. Dump databases
        5. Flash backup (Unraid only)
        6. Self-backup (Arkive DB)
        7. Upload to each target via restic
        8. Retention cleanup
        9. Notify + release lock

        The job type selects which artifacts are refreshed: ``db_dump`` jobs
        re-dump databases, ``flash`` jobs re-archive the flash drive, ``full``
        jobs do both. Every snapshot covers the same paths (dump directory +
        watched directories), so each one is a complete restore point and
        restic retention groups them together.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())[:8]
        if lock_wait_seconds is None:
            lock_wait_seconds = self.scheduled_lock_wait_seconds if trigger == "scheduled" else 0

        # Set correlation ID so every log line in this run is tagged.
        # Initialize token to None so the finally block never hits NameError
        # if an exception occurs before current_run_id.set() completes.
        _run_id_token = None
        _run_id_token = current_run_id.set(run_id)

        if not await self._acquire_lock_waiting(run_id, lock_wait_seconds):
            message = self._lock_conflict_message()
            logger.warning("Backup run %s could not start: %s", run_id, message)
            terminal_status = await self._mark_run_conflict(run_id, job_id, trigger, message)
            event_name = "backup:cancelled" if terminal_status == "skipped" else "backup:failed"
            await self.event_bus.publish(
                event_name,
                {
                    "run_id": run_id,
                    "job_id": job_id,
                    "status": terminal_status,
                    "error": message,
                    "error_category": "conflict",
                    "user_action": "Wait for the current backup/restore operation to finish",
                },
            )
            current_run_id.reset(_run_id_token)
            return {"status": "conflict", "message": message, "run_id": run_id}

        self._cancel_requested = False
        self._active_runs[run_id] = False
        job: dict = {}
        start_time = time.monotonic()
        # Problems that don't abort the run but must not be reported as success.
        warnings: list[str] = []
        # Dump cleanup only ages out dumps of vanished databases when this run's
        # discovery saw the whole picture (see _dump_cleanup_policy).
        cleanup_max_age: int | None = None
        protect_prefixes: set[str] = set()
        legacy_prefixes: dict[str, str] = {}

        try:
            # Load job config
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
                job = await cursor.fetchone()
                if not job:
                    return {"status": "error", "message": f"Job {job_id} not found"}
                job = dict(job)

            job_type = job.get("type") or "full"
            include_databases = (
                not skip_databases and bool(job.get("include_databases", True)) and job_type in DATABASE_JOB_TYPES
            )
            include_flash = not skip_flash and bool(job.get("include_flash", True)) and job_type in FLASH_JOB_TYPES

            # Create run record (skip if already created by caller)
            async with aiosqlite.connect(self.config.db_path) as db:
                cursor = await db.execute("SELECT id FROM job_runs WHERE id = ?", (run_id,))
                existing = await cursor.fetchone()
                if not existing:
                    await db.execute(
                        "INSERT INTO job_runs (id, job_id, status, trigger) VALUES (?, ?, 'running', ?)",
                        (run_id, job_id, trigger),
                    )
                    await db.commit()

            await self.event_bus.publish(
                "backup:started",
                {
                    "run_id": run_id,
                    "job_id": job_id,
                    "trigger": trigger,
                },
            )

            # Step 3: Discovery
            await self._update_progress(run_id, "discovering")
            containers = []
            all_databases = []
            discovery_ok = False
            if include_databases:
                if self.discovery:
                    try:
                        containers = await self._discover_with_retry()
                    except Exception as e:
                        logger.error("Container discovery failed: %s", e)
                        warnings.append(f"container discovery failed, databases were not dumped ({e})")
                        await self._record_discovery_outcome(run_id, False)
                    else:
                        discovery_ok = True
                        for c in containers:
                            all_databases.extend(c.databases)
                            warnings.extend(getattr(c, "warnings", None) or [])
                        all_databases = DBDumper.dedupe(all_databases)
                        async with aiosqlite.connect(self.config.db_path) as db:
                            await persist_discovery_results(db, containers)
                else:
                    # Only possible in dev mode — production refuses to start
                    # without the Docker socket.
                    logger.info("Docker not available — skipping container discovery")

            if discovery_ok:
                async with aiosqlite.connect(self.config.db_path) as db:
                    await db.execute(
                        "UPDATE job_runs SET databases_discovered = ? WHERE id = ?",
                        (len(all_databases), run_id),
                    )
                    await db.commit()
                await self._record_discovery_outcome(run_id, True)
                if not all_databases:
                    previous = await self._previous_database_count(job_id, run_id)
                    if previous:
                        warnings.append(f"no databases discovered (a recent run found {previous})")

            cleanup_max_age, protect_prefixes, legacy_prefixes = self._dump_cleanup_policy(
                discovery_ok, containers, all_databases
            )

            if self._is_cancelled(run_id):
                return await self._cancel(run_id)

            # Pre-flight: disk space check
            min_disk = DEFAULT_MIN_DISK_BYTES
            warn_disk = DEFAULT_WARN_DISK_BYTES
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT key, value FROM settings WHERE key IN ('min_disk_space_bytes', 'warn_disk_space_bytes')"
                )
                for row in await cursor.fetchall():
                    try:
                        val = int(row["value"])
                        if row["key"] == "min_disk_space_bytes":
                            min_disk = val
                        elif row["key"] == "warn_disk_space_bytes":
                            warn_disk = val
                    except (ValueError, TypeError):
                        pass
            await self._check_disk_space_for_backup(run_id, min_disk, warn_disk)

            # Step 4: Dump databases
            dump_results = []
            skipped_dbs: list[str] = []
            if include_databases and all_databases and self.db_dumper:
                await self._update_progress(run_id, "dumping_databases")
                # Exec-based dumps need a running container. Stopped containers
                # are skipped (their on-disk data is static) rather than failed.
                container_status = {c.name: c.status for c in containers}
                to_dump = []
                for d in all_databases:
                    status = container_status.get(d.container_name, "running")
                    if d.db_type in EXEC_DUMP_TYPES and status != "running":
                        skipped_dbs.append(f"{d.container_name}/{d.db_name}")
                        dump_results.append(
                            DumpResult(
                                container_name=d.container_name,
                                db_type=d.db_type,
                                db_name=d.db_name,
                                dump_path="",
                                dump_size_bytes=0,
                                integrity_check="skipped",
                                status="skipped",
                                error=f"Container is {status}, not running",
                            )
                        )
                    else:
                        to_dump.append(d)
                if to_dump:
                    dump_results.extend(await self.db_dumper.dump_all(to_dump))

                # Record dump results
                async with aiosqlite.connect(self.config.db_path) as db:
                    for dr in dump_results:
                        await db.execute(
                            """INSERT INTO job_run_databases
                            (run_id, container_name, db_type, db_name, dump_size_bytes, integrity_check, status, host_path, error)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                run_id,
                                dr.container_name,
                                dr.db_type,
                                dr.db_name,
                                dr.dump_size_bytes,
                                dr.integrity_check,
                                dr.status,
                                dr.dump_path,
                                dr.error,
                            ),
                        )
                    # Roles/globals dumps are recorded per run but aren't databases.
                    database_results = [d for d in dump_results if d.kind == "database"]
                    dumped = sum(1 for d in database_results if d.status == "success")
                    failed = sum(1 for d in database_results if d.status == "failed")
                    await db.execute(
                        """UPDATE job_runs SET databases_discovered = ?, databases_dumped = ?, databases_failed = ?
                        WHERE id = ?""",
                        (len(all_databases), dumped, failed, run_id),
                    )
                    await db.commit()
                if skipped_dbs:
                    logger.info(
                        "Skipped %d database(s) in stopped containers: %s", len(skipped_dbs), ", ".join(skipped_dbs)
                    )
                # A stopped database is only covered by a dump made while it
                # ran; one that never had a dump isn't backed up at all.
                undumped = [
                    f"{d.container_name}/{d.db_name}"
                    for d in all_databases
                    if f"{d.container_name}/{d.db_name}" in skipped_dbs and not self._has_dump(d)
                ]
                if undumped:
                    warnings.append("database(s) in stopped containers have never been dumped: " + ", ".join(undumped))

            # Keep only the newest dump per database (older generations live
            # in earlier snapshots) and drop dumps of containers that are gone,
            # so each snapshot reflects the current state.
            try:
                cleanup_dump_dir(
                    self.config.dump_dir,
                    keep_last=1,
                    max_age_days=cleanup_max_age,
                    protect_prefixes=protect_prefixes,
                    legacy_prefixes=legacy_prefixes,
                )
            except Exception as e:
                logger.warning("Pre-upload dump cleanup failed: %s", e)

            if self._is_cancelled(run_id):
                return await self._cancel(run_id)

            # Step 5: Flash backup
            flash_result = None
            if include_flash:
                await self._update_progress(run_id, "flash_backup")
                flash_result = await self.flash_backup.backup()
                if flash_result.status == "success":
                    async with aiosqlite.connect(self.config.db_path) as db:
                        await db.execute(
                            "UPDATE job_runs SET flash_backed_up = 1, flash_size_bytes = ? WHERE id = ?",
                            (flash_result.size_bytes, run_id),
                        )
                        await db.commit()

            if self._is_cancelled(run_id):
                return await self._cancel(run_id)

            # Step 6: Self-backup
            await self._self_backup()

            # Step 7: Upload to targets
            # Open a single DB connection for the entire upload + retention +
            # snapshot-refresh phase to avoid opening a new connection per target.
            targets_json = json.loads(job.get("targets", "[]"))
            total_bytes = 0
            target_failures = []
            resolved_targets: list[dict] = []

            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row

                if not targets_json:
                    # Get all enabled targets
                    cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
                    targets_rows = await cursor.fetchall()
                    targets_json = []
                    for t in targets_rows:
                        td = dict(t)
                        td["config"] = decrypt_config(td.get("config", "{}"), str(self.config.config_dir))
                        targets_json.append(td)

                if not targets_json:
                    warnings.append("no enabled storage targets, nothing was uploaded")

                # Resolve backup paths once (same for every target)
                backup_paths = [str(self.config.dump_dir)]
                dirs_json = json.loads(job.get("directories", "[]"))
                resolved_job_paths: list[str] = []
                for entry in dirs_json:
                    if isinstance(entry, str) and os.path.isabs(entry):
                        resolved_job_paths.append(entry)
                        continue

                    cursor = await db.execute(
                        "SELECT path FROM watched_directories WHERE id = ? AND enabled = 1",
                        (entry,),
                    )
                    row = await cursor.fetchone()
                    if row and row["path"] not in resolved_job_paths:
                        resolved_job_paths.append(row["path"])

                backup_paths.extend(resolved_job_paths)

                # Also include enabled watched directories
                cursor = await db.execute("SELECT path FROM watched_directories WHERE enabled = 1")
                for row in await cursor.fetchall():
                    if row["path"] not in backup_paths:
                        backup_paths.append(row["path"])

                # restic silently drops source paths that don't exist; surface
                # them instead of reporting a backup that misses a directory.
                missing_paths = [p for p in backup_paths if not os.path.exists(p)]
                if missing_paths:
                    backup_paths = [p for p in backup_paths if p not in missing_paths]
                    warnings.append(f"backup path(s) not found: {', '.join(missing_paths)}")

                excludes = json.loads(job.get("exclude_patterns", "[]"))
                excludes.extend(await self._internal_excludes(db))
                tags = [f"job:{job_id}", f"run:{run_id}"]

                for target in targets_json:
                    if self._is_cancelled(run_id):
                        return await self._cancel(run_id)

                    if isinstance(target, str):
                        cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target,))
                        t = await cursor.fetchone()
                        if t:
                            target = dict(t)
                            target["config"] = decrypt_config(target.get("config", "{}"), str(self.config.config_dir))
                        else:
                            warnings.append(f"storage target {target} no longer exists")
                            continue

                    await self._update_progress(run_id, f"uploading:{target.get('name', target.get('id', ''))}")

                    if target.get("type") != "local":
                        # rclone reads credentials from rclone.conf; regenerate
                        # the section so a missing/stale entry can't break uploads.
                        try:
                            await self.cloud_manager.write_target_config(target)
                        except Exception as e:
                            logger.warning(
                                "Could not write rclone config for %s: %s", target.get("name", target.get("id", "")), e
                            )

                    # Init repo if needed
                    await self.backup_engine.init_repo(target)

                    result = await self.backup_engine.backup(
                        target,
                        backup_paths,
                        excludes,
                        tags,
                        cancel_check=lambda rid=run_id: self._is_cancelled(rid),
                    )

                    if result.get("status") == "cancelled":
                        return await self._cancel(run_id)

                    # Record target result
                    upload_bytes = result.get("total_bytes_processed", 0)
                    total_bytes += upload_bytes
                    target_status = result.get("status", "failed")
                    target_error = result.get("error") or result.get("output") or ""
                    target_name = target.get("name", target.get("id", "unknown"))
                    if target_status == "partial":
                        # Snapshot was written but some files could not be read.
                        warnings.append(f"{target_name}: some files could not be read ({target_error[:200]})")
                    elif target_status != "success":
                        if target_error:
                            target_failures.append(f"{target_name}: {target_error}")
                        else:
                            target_failures.append(target_name)
                    await db.execute(
                        """INSERT INTO job_run_targets (run_id, target_id, status, snapshot_id, upload_bytes, error)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            target.get("id", ""),
                            target_status,
                            result.get("snapshot_id", ""),
                            upload_bytes,
                            target_error or None,
                        ),
                    )
                    await db.commit()
                    if target_status in ("success", "partial"):
                        resolved_targets.append(target)

                # Step 8: Retention cleanup
                await self._update_progress(run_id, "retention_cleanup")
                keep_daily, keep_weekly, keep_monthly = await read_retention_settings(db)

            for target in resolved_targets:
                # Forget only; the weekly system retention job prunes. Pruning
                # after every run rewrites pack files, which is slow and costly
                # on cloud targets.
                forget_result = await self.backup_engine.forget(
                    target,
                    keep_daily=keep_daily,
                    keep_weekly=keep_weekly,
                    keep_monthly=keep_monthly,
                    prune=False,
                )
                if forget_result.get("status") != "success":
                    logger.warning(
                        "Retention (forget) failed for target %s: %s",
                        target.get("name", target.get("id", "")),
                        forget_result.get("error") or forget_result.get("output"),
                    )

            # Step 8b: Refresh snapshot cache
            await self._update_progress(run_id, "refreshing_snapshots")
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                for target in resolved_targets:
                    try:
                        snapshots = await self.backup_engine.snapshots(target)
                        target_id = target.get("id", "")
                        # Collect current snapshot short IDs from restic
                        current_ids = set()
                        for snap in snapshots:
                            short_id = snap.get("short_id", snap.get("id", "")[:8])
                            current_ids.add(short_id)
                            await db.execute(
                                """INSERT OR REPLACE INTO snapshots
                                (id, target_id, full_id, time, hostname, paths, tags, size_bytes)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    short_id,
                                    target_id,
                                    snap.get("id", ""),
                                    snap.get("time", ""),
                                    snap.get("hostname", ""),
                                    json.dumps(snap.get("paths", [])),
                                    json.dumps(snap.get("tags", [])),
                                    snap.get("size", 0),
                                ),
                            )

                        # Remove stale snapshots from DB that restic no longer has.
                        # An empty listing usually means restic failed, so keep
                        # the cache rather than wiping it.
                        cursor = await db.execute("SELECT id FROM snapshots WHERE target_id = ?", (target_id,))
                        db_ids = {row[0] for row in await cursor.fetchall()}
                        stale_ids = db_ids - current_ids if current_ids else set()
                        if stale_ids:
                            placeholders = ",".join("?" for _ in stale_ids)  # nosec B608
                            await db.execute(
                                f"DELETE FROM snapshots WHERE target_id = ? AND id IN ({placeholders})",  # nosec B608
                                [target_id, *stale_ids],
                            )
                            logger.info("Removed %d stale snapshot records for target %s", len(stale_ids), target_id)

                        if snapshots:
                            total_size = sum(s.get("size", 0) for s in snapshots)
                            await db.execute(
                                "UPDATE storage_targets SET snapshot_count = ?, total_size_bytes = ? WHERE id = ?",
                                (len(snapshots), total_size, target_id),
                            )
                            await db.execute(
                                "INSERT OR REPLACE INTO size_history (date, target_id, total_size_bytes, snapshot_count) VALUES (?, ?, ?, ?)",
                                (date.today().isoformat(), target_id, total_size, len(snapshots)),
                            )
                        await db.commit()
                    except Exception as e:
                        logger.warning(
                            "Failed to refresh snapshots for target %s: %s", target.get("name", target.get("id", "")), e
                        )

            # Step 9: Complete — check if any targets failed
            duration = int(time.monotonic() - start_time)
            database_failures = sum(1 for d in dump_results if d.status == "failed" and d.kind == "database")
            roles_failures = [d.container_name for d in dump_results if d.status == "failed" and d.kind != "database"]
            flash_failed = flash_result is not None and flash_result.status == "failed"
            substep_failures = []
            if database_failures:
                substep_failures.append(f"{database_failures} database dump(s) failed")
            if roles_failures:
                substep_failures.append(f"roles dump failed for {', '.join(roles_failures)}")
            if flash_failed:
                flash_error = (flash_result.error or "").strip() if flash_result is not None else ""
                if flash_error:
                    substep_failures.append(f"flash backup: {flash_error}")
                else:
                    substep_failures.append("flash backup failed")
            substep_failures.extend(warnings)

            if target_failures:
                final_status = "partial" if resolved_targets else "failed"
                status_msg = f"Backup completed with failures ({'; '.join(target_failures + substep_failures)})"
            elif substep_failures:
                final_status = "partial"
                status_msg = f"Backup completed with problems ({'; '.join(substep_failures)})"
            else:
                final_status = "success"
                status_msg = f"Backup completed in {duration}s"
                if skipped_dbs:
                    status_msg += f" ({len(skipped_dbs)} database(s) in stopped containers skipped)"

            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    """UPDATE job_runs SET status = ?, completed_at = ?,
                    duration_seconds = ?, total_size_bytes = ?, error_message = ? WHERE id = ?""",
                    (
                        final_status,
                        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        duration,
                        total_bytes,
                        None if final_status == "success" else status_msg[:2000],
                        run_id,
                    ),
                )
                # Activity log
                severity = "info" if final_status == "success" else "warning"
                await db.execute(
                    """INSERT INTO activity_log (type, action, message, details, severity)
                    VALUES ('backup', 'completed', ?, ?, ?)""",
                    (
                        status_msg,
                        json.dumps(
                            {
                                "run_id": run_id,
                                "job_id": job_id,
                                "duration": duration,
                                "status": final_status,
                                "failed_targets": target_failures,
                                "warnings": substep_failures,
                            }
                        ),
                        severity,
                    ),
                )
                await db.commit()

            await self.event_bus.publish(
                "backup:completed",
                {
                    "run_id": run_id,
                    "job_id": job_id,
                    "duration": duration,
                    "status": final_status,
                    "total_bytes": total_bytes,
                },
            )

            event_type, title, severity = {
                "success": ("backup.success", "Backup Completed", "success"),
                "partial": ("backup.partial", "Backup Incomplete", "warning"),
            }.get(final_status, ("backup.failed", "Backup Failed", "error"))
            try:
                await self.notifier.send(
                    event_type,
                    title,
                    f"{job.get('name', job_id)}: {status_msg}",
                    severity=severity,
                    job_id=job_id,
                )
            except Exception as notify_err:
                logger.warning("Post-backup notification failed (non-critical): %s", notify_err)

            return {"status": final_status, "run_id": run_id, "duration": duration}

        except Exception as e:
            logger.error("Backup pipeline failed: %s", e, exc_info=True)
            duration = int(time.monotonic() - start_time)

            # Categorize error for user-actionable feedback
            error_category = categorize_error(str(e))
            error_info = ERROR_CATEGORIES.get(error_category, ERROR_CATEGORIES["unknown"])

            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    """UPDATE job_runs SET status = 'failed', completed_at = ?,
                    duration_seconds = ?, error_message = ? WHERE id = ?""",
                    (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), duration, str(e), run_id),
                )
                await db.commit()

            await self.event_bus.publish(
                "backup:failed",
                {
                    "run_id": run_id,
                    "job_id": job_id,
                    "error": str(e),
                    "error_category": error_category,
                    "user_action": error_info.get("user_action"),
                },
            )

            try:
                await self.notifier.send(
                    "backup.failed",
                    "Backup Failed",
                    f"Backup '{job.get('name', job_id)}' failed: {str(e)[:200]}",
                    severity="error",
                    job_id=job_id,
                )
            except Exception as notify_err:
                logger.warning("Failure notification failed (non-critical): %s", notify_err)

            return {"status": "failed", "run_id": run_id, "error": str(e), "error_category": error_category}

        finally:
            # Clean up old dump files even on failure (or when Docker is down)
            # to prevent cascading disk-full issues.
            try:
                cleanup_dump_dir(
                    self.config.dump_dir,
                    keep_last=1,
                    max_age_days=cleanup_max_age,
                    protect_prefixes=protect_prefixes,
                    legacy_prefixes=legacy_prefixes,
                )
            except Exception as e:
                logger.warning("Dump cleanup failed: %s", e)
            self._release_lock()
            self._active_runs.pop(run_id, None)
            if _run_id_token is not None:
                current_run_id.reset(_run_id_token)

    async def cancel(self) -> None:
        """Request cancellation of the current backup."""
        self._cancel_requested = True
        for run_id in list(self._active_runs.keys()):
            self._active_runs[run_id] = True

    async def _cancel(self, run_id: str) -> dict:
        """Handle backup cancellation.

        Lock release and run cleanup are handled by the finally block
        in run_backup() -- do NOT release the lock here.
        """
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "UPDATE job_runs SET status = 'cancelled', completed_at = ? WHERE id = ?",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
            await db.commit()
        await self.event_bus.publish("backup:cancelled", {"run_id": run_id})
        return {"status": "cancelled", "run_id": run_id}

    def _dump_cleanup_policy(
        self, discovery_ok: bool, containers: list, all_databases: list
    ) -> tuple[int | None, set[str], dict[str, str]]:
        """(max_age_days, protect_prefixes, legacy_prefixes) for dump cleanup.

        Dumps are aged out only for databases that are gone, which this run can
        only know when discovery saw everything: it ran, found databases,
        reported no warnings, and every container with exec-dumped databases
        was running (a stopped Postgres can't list its databases). Otherwise
        cleanup only keeps the newest dump per database, so a stopped
        container, a weekly database job or an outage never costs the last
        dump of anything.
        """
        protect: set[str] = set()
        legacy: dict[str, str] = {}
        for d in all_databases:
            prefix = dump_prefix(d.container_name, d.db_type, d.db_name, d.host_path)
            protect.add(prefix)
            legacy[prefix] = legacy_dump_prefix(d.container_name, d.db_name)
            if d.db_type == "postgres":
                protect.add(dump_prefix(d.container_name, "postgres", "globals", kind="globals"))
        stopped_exec = any(
            getattr(c, "status", "running") != "running"
            and any(db.db_type in EXEC_DUMP_TYPES for db in getattr(c, "databases", []) or [])
            for c in containers
        )
        complete = (
            discovery_ok
            and bool(all_databases)
            and not stopped_exec
            and not any(getattr(c, "warnings", None) for c in containers)
        )
        return (self.dump_max_age_days if complete else None), protect, legacy

    def _has_dump(self, db) -> bool:
        """Whether the dump directory holds a finished dump of ``db``."""
        prefixes = (
            dump_prefix(db.container_name, db.db_type, db.db_name, db.host_path) + "_",
            legacy_dump_prefix(db.container_name, db.db_name) + "_",
        )
        try:
            return any(
                p.name.startswith(prefixes) and not p.name.endswith(PARTIAL_SUFFIX)
                for p in self.config.dump_dir.iterdir()
            )
        except OSError:
            return False

    async def _record_discovery_outcome(self, run_id: str, ok: bool) -> None:
        """Best effort: freshness bookkeeping must never fail a backup."""
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute("UPDATE job_runs SET discovery_ok = ? WHERE id = ?", (1 if ok else 0, run_id))
                await db.commit()
        except Exception as e:
            logger.debug("Could not record discovery outcome: %s", e)

    async def _internal_excludes(self, db) -> list[str]:
        """Exclusions that keep Arkive's own storage out of its snapshots.

        A watched parent directory (e.g. /mnt/user/appdata holding Arkive's
        config and a local repository) must not re-ingest a repository or the
        restore scratch space. Three layers, because the same folder is often
        visible under two paths (/config/restores is also
        /mnt/user/appdata/arkive/restores through the /mnt/user mount):
        exact --exclude paths, their Docker host-path aliases, and an
        exclusion marker inside each Arkive-owned directory (the repository
        folder itself, never the target's configured parent, which may hold
        real data).
        """
        storage = [str(self.config.restore_dir)]
        try:
            cursor = await db.execute("SELECT id, type, config FROM storage_targets WHERE type = 'local'")
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning("Could not list local targets for exclusion: %s", e)
            rows = []
        for row in rows:
            try:
                config = decrypt_config(row[2] or "{}", str(self.config.config_dir))
            except Exception:
                continue
            storage.append(build_repo_path({"id": row[0], "type": row[1], "config": config}))
        # Dumps still being written (crash leftovers) are never uploaded.
        partials = f"{self.config.dump_dir}/*{PARTIAL_SUFFIX}"
        aliases = await asyncio.to_thread(self._prepare_storage_exclusions, storage, partials)
        return list(dict.fromkeys(storage + [partials] + aliases))

    def _prepare_storage_exclusions(self, storage: list[str], partials: str) -> list[str]:
        """Write exclusion markers into Arkive-owned dirs; return host-path
        aliases of the excluded paths that are visible inside the container.
        Blocking (filesystem + Docker API)."""
        for path in storage:
            directory = Path(path)
            try:
                if directory.is_dir() and not (directory / EXCLUDE_MARKER).exists():
                    (directory / EXCLUDE_MARKER).write_text(_EXCLUDE_MARKER_TEXT)
            except OSError as e:
                logger.debug("Could not write exclusion marker in %s: %s", path, e)
        # Pre-release builds wrote the marker into a local target's configured
        # directory, which may be a parent of real data; remove it when it is
        # provably ours (exact text), so it can't hide that data.
        for path in storage[1:]:
            marker = Path(path).parent / EXCLUDE_MARKER
            try:
                if marker.is_file() and marker.read_text() == _EXCLUDE_MARKER_TEXT:
                    marker.unlink()
                    logger.info("Removed Arkive exclusion marker from %s", marker.parent)
            except OSError as e:
                logger.debug("Could not inspect %s: %s", marker, e)

        docker_client = getattr(self.backup_engine, "docker_client", None)
        aliases: list[str] = []
        for path in storage + [partials]:
            base, pattern = (
                (path[: -len(f"/*{PARTIAL_SUFFIX}")], f"/*{PARTIAL_SUFFIX}")
                if path.endswith(f"/*{PARTIAL_SUFFIX}")
                else (path, "")
            )
            try:
                host_path = host_path_for(base, docker_client)
            except Exception:
                host_path = None
            if host_path and host_path != base and os.path.exists(host_path):
                aliases.append(host_path + pattern)
        return aliases

    async def _update_progress(self, run_id: str, phase: str) -> None:
        """Publish progress update via SSE."""
        normalized = phase.split(":")[0] if ":" in phase else phase
        idx = _PHASE_ORDER.index(normalized) if normalized in _PHASE_ORDER else 0
        percent = round(((idx + 1) / len(_PHASE_ORDER)) * 100)
        await self.event_bus.publish(
            "backup:progress",
            {
                "run_id": run_id,
                "phase": phase,
                "percent": percent,
            },
        )

    async def _check_disk_space_for_backup(
        self, run_id: str, min_bytes: int = DEFAULT_MIN_DISK_BYTES, warn_bytes: int = DEFAULT_WARN_DISK_BYTES
    ) -> None:
        """Check available disk space on dump directory. Raises RuntimeError if below min_bytes."""
        dump_dir = str(self.config.dump_dir)
        try:
            usage = shutil.disk_usage(dump_dir)
        except OSError as e:
            logger.warning("Could not check disk space on %s: %s", dump_dir, e)
            return
        free = usage.free
        free_gb = free / (1024**3)
        if free < min_bytes:
            msg = (
                f"Insufficient disk space on dump directory ({dump_dir}): "
                f"{free_gb:.1f} GB free, {min_bytes // (1024**3)} GB required. "
                "Free disk space or lower the min_disk_space_bytes setting."
            )
            logger.error("Pre-backup disk check FAILED: %s", msg)
            raise RuntimeError(msg)
        if free < warn_bytes:
            logger.warning(
                "Low disk space on dump directory (%s): %.1f GB free. Backup will proceed but consider freeing space.",
                dump_dir,
                free_gb,
            )
        else:
            logger.info("Disk space check OK: %.1f GB free on %s", free_gb, dump_dir)

    async def _self_backup(self) -> None:
        """Backup Arkive's own SQLite database."""
        try:
            from app.utils.subprocess_runner import run_command

            self_backup_path = str(self.config.dump_dir / "arkive_self.db")
            result = await run_command(["sqlite3", str(self.config.db_path), f".backup {self_backup_path}"])
            if result.returncode == 0:
                logger.info("Self-backup completed: %s", self_backup_path)
            else:
                logger.warning("Self-backup failed: %s", result.stderr)
        except Exception as e:
            logger.warning("Self-backup error: %s", e)

    def is_running(self) -> bool:
        """Check if a backup is currently running."""
        return self.lock_manager.is_backup_running()

    def is_restore_running(self) -> bool:
        """Check if a restore operation is in progress."""
        return self.lock_manager.is_restore_running()
