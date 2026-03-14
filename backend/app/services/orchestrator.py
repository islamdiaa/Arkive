"""Backup orchestrator — coordinates the full backup pipeline."""

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
from app.core.event_bus import EventBus
from app.core.security import decrypt_config
from app.services.backup_engine import BackupEngine
from app.services.cloud_manager import CloudManager
from app.services.db_dumper import DBDumper
from app.services.discovery import DiscoveryEngine
from app.services.discovery_persistence import persist_discovery_results
from app.services.flash_backup import FlashBackup
from app.services.notifier import Notifier

logger = logging.getLogger("arkive.orchestrator")

LOCK_FILE = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / "backup.lock"
RESTORE_LOCK_FILE = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / "restore.lock"


def _get_proc_start_time(pid: int) -> str | None:
    """Read process start time (field 22) from /proc/{pid}/stat.

    This value (in clock ticks since boot) uniquely identifies a process
    instance even after PID recycling in Docker containers.
    Returns None if the process doesn't exist or /proc is unavailable.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split(")")[-1].split()
            # Field 22 in stat is starttime (0-indexed from after the comm field)
            # After splitting on ")", fields[0] is state, fields[19] is starttime
            return fields[19] if len(fields) > 19 else None
    except (OSError, IndexError):
        return None


def cleanup_stale_backup_lock(config_dir: Path | None = None) -> bool:
    """Remove a stale backup.lock proactively on startup or before manual runs."""
    config_root = config_dir or Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
    lock_file = config_root / "backup.lock"
    if not lock_file.exists():
        return False

    try:
        lock_data = json.loads(lock_file.read_text())
        pid = lock_data.get("pid")
        stored_start = lock_data.get("proc_start_time")
        if pid and stored_start:
            current_start = _get_proc_start_time(int(pid))
            if current_start is not None and current_start == stored_start:
                return False
        lock_file.unlink(missing_ok=True)
        return True
    except Exception:
        lock_file.unlink(missing_ok=True)
        return True


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
        self._cancel_requested = False
        self._active_runs: dict[str, bool] = {}

    def _lock_conflict_message(self) -> str:
        """Return the best available explanation for a lock acquisition failure."""
        if RESTORE_LOCK_FILE.exists():
            return "Restore operation in progress"
        if LOCK_FILE.exists():
            return "Another backup is already running"
        return "Backup could not start because the lock could not be acquired"

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
        """Acquire backup lock atomically. Returns False if already locked.

        Uses O_CREAT | O_EXCL for atomic creation to prevent TOCTOU race
        conditions between concurrent backup triggers.
        """
        # Check for stale lock first
        if LOCK_FILE.exists():
            try:
                lock_data = json.loads(LOCK_FILE.read_text())
                pid = lock_data.get("pid")
                if pid and os.path.exists(f"/proc/{pid}"):
                    # PID exists — but is it the SAME process that took the lock?
                    stored_start = lock_data.get("proc_start_time")
                    if stored_start:
                        current_start = _get_proc_start_time(pid)
                        if current_start == stored_start:
                            return False  # Same process still running
                        # Different start time → PID was recycled
                        logger.warning(
                            "Lock PID %s recycled (start %s→%s), removing stale lock",
                            pid,
                            stored_start,
                            current_start,
                        )
                    else:
                        # Legacy lock without start time — fall back to PID-only check
                        return False
                # Process dead or PID recycled — remove stale lock
                if pid:
                    logger.warning("Removing stale lock from PID %s", pid)
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    logger.debug("Could not remove stale lock file", exc_info=True)
            except Exception:
                # Corrupt lock file — remove and retry
                logger.warning("Corrupt lock file detected, removing", exc_info=True)
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    logger.debug("Could not remove corrupt lock file", exc_info=True)

        # Refuse backup if a restore is in progress
        if RESTORE_LOCK_FILE.exists():
            # Check if restore lock is stale
            try:
                lock_data = json.loads(RESTORE_LOCK_FILE.read_text())
                pid = lock_data.get("pid")
                if pid:
                    stored_start = lock_data.get("proc_start_time")
                    if not stored_start:
                        # Legacy lock without proc_start_time — treat as live (conservative)
                        logger.warning("Cannot start backup — restore operation in progress")
                        return False
                    current_start = _get_proc_start_time(int(pid))
                    if current_start is not None and current_start == stored_start:
                        # Process genuinely alive — restore is running
                        logger.warning("Cannot start backup — restore operation in progress")
                        return False
                    # Process dead or PID recycled — stale lock
                    logger.warning("Removing stale restore lock from PID %s", pid)
                RESTORE_LOCK_FILE.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError, ValueError):
                RESTORE_LOCK_FILE.unlink(missing_ok=True)
            # Fall through to acquire backup lock normally

        lock_payload: dict[str, Any] = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        lock_payload["proc_start_time"] = _get_proc_start_time(os.getpid()) or ""
        if run_id:
            lock_payload["run_id"] = run_id

        # Atomic file creation — O_EXCL fails if file already exists,
        # preventing race between concurrent callers.
        try:
            LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(LOCK_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, json.dumps(lock_payload).encode())
            finally:
                os.close(fd)
        except FileExistsError:
            # Another process beat us to the lock
            return False
        except OSError as e:
            logger.error("Failed to acquire lock: %s", e)
            return False

        # Double-check: did a restore lock appear between our pre-check and O_EXCL create?
        if RESTORE_LOCK_FILE.exists():
            LOCK_FILE.unlink(missing_ok=True)
            return False

        return True

    def _release_lock(self) -> None:
        """Release backup lock."""
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to release backup lock file", exc_info=True)

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

    async def run_backup(
        self,
        job_id: str,
        trigger: str = "manual",
        skip_databases: bool = False,
        skip_flash: bool = False,
        dry_run: bool = False,
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        """Execute the full backup pipeline.

        Pipeline steps:
        1. Acquire lock
        2. Create job_run record
        3. Discovery scan
        4. Dump databases
        5. Flash backup (Unraid only)
        6. Self-backup (Arkive DB)
        7. Upload to each target via restic
        8. Retention cleanup
        9. Notify + release lock
        """
        if run_id is None:
            run_id = str(uuid.uuid4())[:8]
        if not self._acquire_lock(run_id):
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
            return {"status": "conflict", "message": message, "run_id": run_id}

        self._cancel_requested = False
        self._active_runs[run_id] = False
        job: dict = {}
        start_time = time.monotonic()

        try:
            # Load job config
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
                job = await cursor.fetchone()
                if not job:
                    return {"status": "error", "message": f"Job {job_id} not found"}
                job = dict(job)

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

            # Step 3: Discovery (skip if Docker not available)
            await self._update_progress(run_id, "discovering")
            containers = []
            all_databases = []
            if self.discovery:
                containers = await self.discovery.scan()
                for c in containers:
                    all_databases.extend(c.databases)
                async with aiosqlite.connect(self.config.db_path) as db:
                    await persist_discovery_results(db, containers)
                    await db.commit()
            else:
                logger.info("Docker not available — skipping container discovery")

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
            if not skip_databases and job.get("include_databases", True) and all_databases and self.db_dumper:
                await self._update_progress(run_id, "dumping_databases")
                dump_results = await self.db_dumper.dump_all(all_databases)

                # Record dump results
                async with aiosqlite.connect(self.config.db_path) as db:
                    for dr in dump_results:
                        await db.execute(
                            """INSERT INTO job_run_databases
                            (run_id, container_name, db_type, db_name,
                            dump_size_bytes, integrity_check, status,
                            host_path, error)
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
                    dumped = sum(1 for d in dump_results if d.status == "success")
                    failed = sum(1 for d in dump_results if d.status == "failed")
                    await db.execute(
                        """UPDATE job_runs SET databases_discovered = ?, databases_dumped = ?, databases_failed = ?
                        WHERE id = ?""",
                        (len(all_databases), dumped, failed, run_id),
                    )
                    await db.commit()

            if self._is_cancelled(run_id):
                return await self._cancel(run_id)

            # Step 5: Flash backup
            flash_result = None
            if not skip_flash and job.get("include_flash", True):
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
            targets_json = json.loads(job.get("targets", "[]"))
            if not targets_json:
                # Get all enabled targets
                async with aiosqlite.connect(self.config.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
                    targets_rows = await cursor.fetchall()
                    targets_json = []
                    for t in targets_rows:
                        td = dict(t)
                        td["config"] = decrypt_config(td.get("config", "{}"), str(self.config.config_dir))
                        targets_json.append(td)

            total_bytes = 0
            target_failures = []
            resolved_targets: list[dict] = []
            for target in targets_json:
                if self._is_cancelled(run_id):
                    return await self._cancel(run_id)

                if isinstance(target, str):
                    async with aiosqlite.connect(self.config.db_path) as db:
                        db.row_factory = aiosqlite.Row
                        cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target,))
                        t = await cursor.fetchone()
                        if t:
                            target = dict(t)
                            target["config"] = decrypt_config(target.get("config", "{}"), str(self.config.config_dir))
                        else:
                            continue

                await self._update_progress(run_id, f"uploading:{target.get('name', target.get('id', ''))}")

                # Init repo if needed
                await self.backup_engine.init_repo(target)

                # Collect paths to backup
                backup_paths = [str(self.config.dump_dir)]
                dirs_json = json.loads(job.get("directories", "[]"))
                async with aiosqlite.connect(self.config.db_path) as db:
                    db.row_factory = aiosqlite.Row
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

                excludes = json.loads(job.get("exclude_patterns", "[]"))
                tags = [f"job:{job_id}", f"run:{run_id}"]

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
                if target_status != "success":
                    target_name = target.get("name", target.get("id", "unknown"))
                    if target_error:
                        target_failures.append(f"{target_name}: {target_error}")
                    else:
                        target_failures.append(target_name)
                async with aiosqlite.connect(self.config.db_path) as db:
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
                resolved_targets.append(target)

            # Step 8: Retention cleanup
            await self._update_progress(run_id, "retention_cleanup")
            # Read retention settings from DB
            keep_daily = 7
            keep_weekly = 4
            keep_monthly = 6
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT key, value FROM settings WHERE key IN ('keep_daily', 'keep_weekly', 'keep_monthly')"
                )
                for row in await cursor.fetchall():
                    try:
                        val = int(row["value"])
                        if row["key"] == "keep_daily":
                            keep_daily = val
                        elif row["key"] == "keep_weekly":
                            keep_weekly = val
                        elif row["key"] == "keep_monthly":
                            keep_monthly = val
                    except (ValueError, TypeError):
                        pass
            for target in resolved_targets:
                await self.backup_engine.forget(
                    target,
                    keep_daily=keep_daily,
                    keep_weekly=keep_weekly,
                    keep_monthly=keep_monthly,
                )

            # Step 8b: Refresh snapshot cache
            await self._update_progress(run_id, "refreshing_snapshots")
            for target in resolved_targets:
                try:
                    snapshots = await self.backup_engine.snapshots(target)
                    target_id = target.get("id", "")
                    async with aiosqlite.connect(self.config.db_path) as db:
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

                        # Remove stale snapshots from DB that restic no longer has
                        cursor = await db.execute("SELECT id FROM snapshots WHERE target_id = ?", (target_id,))
                        db_ids = {row[0] for row in await cursor.fetchall()}
                        stale_ids = db_ids - current_ids
                        if stale_ids:
                            placeholders = ",".join("?" for _ in stale_ids)  # nosec B608
                            await db.execute(
                                f"DELETE FROM snapshots WHERE target_id = ? AND id IN ({placeholders})",  # nosec B608
                                [target_id, *stale_ids],
                            )
                            logger.info("Removed %d stale snapshot records for target %s", len(stale_ids), target_id)

                        total_size = sum(s.get("size", 0) for s in snapshots)
                        await db.execute(
                            "UPDATE storage_targets SET snapshot_count = ?, total_size_bytes = ? WHERE id = ?",
                            (len(snapshots), total_size, target_id),
                        )
                        await db.execute(
                            "INSERT OR REPLACE INTO size_history"
                            " (date, target_id, total_size_bytes, snapshot_count)"
                            " VALUES (?, ?, ?, ?)",
                            (date.today().isoformat(), target_id, total_size, len(snapshots)),
                        )
                        await db.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to refresh snapshots for target %s: %s", target.get("name", target.get("id", "")), e
                    )

            # Step 9: Complete — check if any targets failed
            duration = int(time.monotonic() - start_time)
            database_failures = sum(1 for d in dump_results if d.status != "success")
            flash_failed = flash_result is not None and flash_result.status == "failed"
            substep_failures = []
            if database_failures:
                substep_failures.append(f"{database_failures} database dump(s)")
            if flash_failed:
                flash_error = (flash_result.error or "").strip() if flash_result is not None else ""
                if flash_error:
                    substep_failures.append(f"flash backup: {flash_error}")
                else:
                    substep_failures.append("flash backup")

            if target_failures:
                final_status = "partial" if total_bytes > 0 else "failed"
                status_msg = f"Backup completed with failures ({', '.join(target_failures)})"
            elif substep_failures:
                final_status = "partial"
                status_msg = f"Backup completed with failures ({', '.join(substep_failures)})"
            else:
                final_status = "success"
                status_msg = f"Backup completed in {duration}s"

            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    """UPDATE job_runs SET status = ?, completed_at = ?,
                    duration_seconds = ?, total_size_bytes = ? WHERE id = ?""",
                    (final_status, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), duration, total_bytes, run_id),
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

            event_type = "backup.success" if final_status == "success" else "backup.failed"
            try:
                await self.notifier.send(
                    event_type,
                    "Backup Completed" if final_status == "success" else "Backup Completed with Failures",
                    status_msg,
                    severity="success" if final_status == "success" else "warning",
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
                )
            except Exception as notify_err:
                logger.warning("Failure notification failed (non-critical): %s", notify_err)

            return {"status": "failed", "run_id": run_id, "error": str(e), "error_category": error_category}

        finally:
            # Clean up old dump files even on failure to prevent cascading disk-full issues
            if self.db_dumper:
                try:
                    self.db_dumper.cleanup_old_dumps(keep_last=3)
                except Exception as e:
                    logger.warning("Dump cleanup failed: %s", e)
            self._release_lock()
            self._active_runs.pop(run_id, None)

    async def cancel(self) -> None:
        """Request cancellation of the current backup."""
        self._cancel_requested = True
        for run_id in list(self._active_runs.keys()):
            self._active_runs[run_id] = True

    async def _cancel(self, run_id: str) -> dict:
        """Handle backup cancellation."""
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "UPDATE job_runs SET status = 'cancelled', completed_at = ? WHERE id = ?",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), run_id),
            )
            await db.commit()
        self._release_lock()
        await self.event_bus.publish("backup:cancelled", {"run_id": run_id})
        return {"status": "cancelled", "run_id": run_id}

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
        return LOCK_FILE.exists()

    def is_restore_running(self) -> bool:
        """Check if a restore operation is in progress."""
        return RESTORE_LOCK_FILE.exists()
