"""Lock manager for backup/restore mutual exclusion.

Encapsulates lock acquisition, release, stale detection, and JSON validation
for the backup and restore lock files used by orchestrator.py and restore.py.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictInt, ValidationError

logger = logging.getLogger("arkive.lock_manager")


class LockFileData(BaseModel):
    """Validated schema for lock file JSON content."""
    pid: StrictInt
    proc_start_time: str = ""
    started_at: str = ""
    run_id: str = ""


def _get_proc_start_time(pid: int) -> str | None:
    """Read process start time (field 22) from /proc/{pid}/stat.

    This value (in clock ticks since boot) uniquely identifies a process
    instance even after PID recycling in Docker containers.
    Returns None if the process doesn't exist or /proc is unavailable.
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            fields = f.read().split(")")[-1].split()
            # Field 22 in stat is starttime (0-indexed from after the comm field)
            # After splitting on ")", fields[0] is state, fields[19] is starttime
            return fields[19] if len(fields) > 19 else None
    except (OSError, IndexError):
        return None


def parse_lock_file(raw: str) -> LockFileData | None:
    """Parse and validate lock file JSON. Returns None if invalid."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Lock file contains malformed JSON, treating as stale")
        return None

    try:
        return LockFileData.model_validate(data)
    except ValidationError as e:
        logger.warning("Lock file failed validation: %s", e)
        return None


def _is_process_alive(lock_data: LockFileData) -> bool:
    """Check if the process described in lock_data is still alive.

    Returns True only when the PID exists AND its start time matches
    the stored value (ruling out PID recycling).
    """
    pid = lock_data.pid
    if not os.path.exists(f"/proc/{pid}"):
        return False

    stored_start = lock_data.proc_start_time
    if not stored_start:
        # Legacy lock without proc_start_time -- treat as live (conservative)
        return True

    current_start = _get_proc_start_time(pid)
    if current_start is not None and current_start == stored_start:
        return True

    # PID recycled or dead
    logger.warning(
        "Lock PID %s recycled (start %s -> %s), treating as stale",
        pid, stored_start, current_start,
    )
    return False


class BackupLockManager:
    """Manages backup.lock and restore.lock files for mutual exclusion."""

    def __init__(self, config_dir: Path | None = None):
        self._config_dir = config_dir or Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
        self.lock_file = self._config_dir / "backup.lock"
        self.restore_lock_file = self._config_dir / "restore.lock"

    def _read_and_validate_lock(self, path: Path) -> LockFileData | None:
        """Read a lock file, validate its JSON, and return parsed data.

        Returns None if the file doesn't exist, is unreadable, or contains
        invalid JSON/types. Removes the file if it's malformed.
        """
        if not path.exists():
            return None
        try:
            raw = path.read_text()
        except OSError:
            return None

        lock_data = parse_lock_file(raw)
        if lock_data is None:
            # Malformed -- remove it
            logger.warning("Removing malformed lock file: %s", path)
            path.unlink(missing_ok=True)
        return lock_data

    def _is_lock_stale(self, lock_data: LockFileData) -> bool:
        """Return True if the lock holder process is no longer alive."""
        return not _is_process_alive(lock_data)

    def cleanup_stale_backup_lock(self) -> bool:
        """Remove a stale backup.lock proactively on startup or before manual runs."""
        if not self.lock_file.exists():
            return False

        lock_data = self._read_and_validate_lock(self.lock_file)
        if lock_data is None:
            # Was malformed and already removed by _read_and_validate_lock
            return True
        if self._is_lock_stale(lock_data):
            logger.warning("Removing stale backup lock from PID %s", lock_data.pid)
            self.lock_file.unlink(missing_ok=True)
            return True
        return False

    def acquire_backup_lock(self, run_id: str | None = None) -> bool:
        """Acquire backup lock atomically. Returns False if already locked.

        Uses O_CREAT | O_EXCL for atomic creation to prevent TOCTOU race
        conditions between concurrent backup triggers.

        The restore lock check happens BEFORE the O_EXCL creation so we
        don't create a backup lock file that we'd immediately have to remove.
        """
        # Check for stale backup lock first
        if self.lock_file.exists():
            lock_data = self._read_and_validate_lock(self.lock_file)
            if lock_data is not None and not self._is_lock_stale(lock_data):
                return False  # Valid, live lock -- cannot acquire
            # Stale or malformed -- already cleaned up by _read_and_validate_lock
            # or needs explicit removal for stale
            if lock_data is not None:
                self.lock_file.unlink(missing_ok=True)

        # Check restore lock BEFORE attempting O_EXCL (fixes TOCTOU)
        if self.restore_lock_file.exists():
            restore_data = self._read_and_validate_lock(self.restore_lock_file)
            if restore_data is None:
                # Malformed restore lock was removed, fall through
                pass
            elif not self._is_lock_stale(restore_data):
                logger.warning("Cannot start backup -- restore operation in progress")
                return False
            else:
                # Stale restore lock
                logger.warning("Removing stale restore lock from PID %s", restore_data.pid)
                self.restore_lock_file.unlink(missing_ok=True)

        # Build lock payload
        lock_payload: dict[str, Any] = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "proc_start_time": _get_proc_start_time(os.getpid()) or "",
        }
        if run_id:
            lock_payload["run_id"] = run_id

        # Atomic file creation -- O_EXCL fails if file already exists
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.lock_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, json.dumps(lock_payload).encode())
            finally:
                os.close(fd)
        except FileExistsError:
            return False
        except OSError as e:
            logger.error("Failed to acquire lock: %s", e)
            return False

        # Double-check: did a restore lock appear between our pre-check and O_EXCL create?
        if self.restore_lock_file.exists():
            restore_data = self._read_and_validate_lock(self.restore_lock_file)
            if restore_data is not None and not self._is_lock_stale(restore_data):
                self.lock_file.unlink(missing_ok=True)
                return False

        return True

    def release_backup_lock(self) -> None:
        """Release backup lock."""
        try:
            self.lock_file.unlink(missing_ok=True)
        except Exception:
            pass

    def is_backup_running(self) -> bool:
        """Check if a backup is currently running."""
        return self.lock_file.exists()

    def is_restore_running(self) -> bool:
        """Check if a restore operation is in progress."""
        return self.restore_lock_file.exists()

    def lock_conflict_message(self) -> str:
        """Return the best available explanation for a lock acquisition failure."""
        if self.restore_lock_file.exists():
            return "Restore operation in progress"
        if self.lock_file.exists():
            return "Another backup is already running"
        return "Backup could not start because the lock could not be acquired"
