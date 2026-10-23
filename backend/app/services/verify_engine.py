"""Verification engine -- validates backup integrity and computes trust scores.

Runs a multi-step verification pipeline per storage target:
1. restic check (repository consistency)
2. Random file restore + SHA-256 verification
3. Database dump validation (SQLite PRAGMA integrity_check / SQL dump completion trailer)
4. Trust score computation (weighted average)

Results are stored in the verification_runs table for historical tracking.
"""

import asyncio
import gzip
import hashlib
import logging
import os
import random
import shutil
import tempfile
import time
import uuid
import zlib
from datetime import datetime, timezone

import aiosqlite

from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.services.db_dumper import (
    MYSQL_DUMP_TRAILER,
    PG_DUMP_TRAILER,
    PG_DUMPALL_TRAILER,
    is_arkive_dump_name,
)
from app.services.lock_manager import BackupLockManager
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.verify_engine")

# Trust score weights
WEIGHT_RECENCY = 0.30
WEIGHT_FILE_INTEGRITY = 0.40
WEIGHT_DB_INTEGRITY = 0.30

# Restore-test files above this size are skipped: a random multi-GB pick would
# fill the disk and tie up a worker hashing it.
MAX_RESTORE_TEST_BYTES = 256 * 1024 * 1024

# Dump tools write their completion trailer within the last few KB.
_SQL_TAIL_BYTES = 4096

# Recency thresholds (hours since last successful backup)
RECENCY_EXCELLENT = 24      # < 24h = 100%
RECENCY_GOOD = 72           # < 72h = 80%
RECENCY_ACCEPTABLE = 168    # < 7 days = 60%
RECENCY_STALE = 336         # < 14 days = 30%
# > 14 days = 0%


def compute_recency_score(hours_since_backup: float | None) -> float:
    """Compute recency score (0.0-1.0) based on hours since last backup."""
    if hours_since_backup is None:
        return 0.0
    if hours_since_backup < RECENCY_EXCELLENT:
        return 1.0
    if hours_since_backup < RECENCY_GOOD:
        return 0.8
    if hours_since_backup < RECENCY_ACCEPTABLE:
        return 0.6
    if hours_since_backup < RECENCY_STALE:
        return 0.3
    return 0.0


def compute_trust_score(
    recency_score: float,
    file_integrity_score: float,
    db_integrity_score: float,
) -> float:
    """Compute weighted trust score (0.0-100.0)."""
    raw = (
        WEIGHT_RECENCY * recency_score
        + WEIGHT_FILE_INTEGRITY * file_integrity_score
        + WEIGHT_DB_INTEGRITY * db_integrity_score
    )
    return round(raw * 100, 1)


class VerifyEngine:
    """Validates backup integrity across all enabled storage targets."""

    def __init__(self, config: ArkiveConfig, backup_engine=None):
        self.config = config
        self.backup_engine = backup_engine
        self.lock_manager = BackupLockManager(config.config_dir)

    def is_backup_running(self) -> bool:
        """Check if a backup or restore is currently in progress."""
        return (
            self.lock_manager.is_backup_running()
            or self.lock_manager.is_restore_running()
        )

    async def verify_all_targets(self, run_ids: dict[str, str] | None = None) -> dict:
        """Run verification against all enabled storage targets.

        Args:
            run_ids: Optional mapping of target_id -> run_id for pre-created
                     verification_runs rows (from the API trigger). When provided,
                     the engine UPDATEs those rows instead of INSERTing new ones.

        Returns a summary dict with per-target results and overall trust score.
        """
        if self.is_backup_running():
            logger.warning("Skipping verification: backup/restore in progress")
            await self._mark_runs(run_ids, "skipped", "Skipped: a backup or restore was in progress")
            return {"status": "skipped", "reason": "backup_in_progress"}

        if not self.backup_engine:
            logger.warning("Verification skipped: backup engine not available")
            await self._mark_runs(run_ids, "skipped", "Skipped: backup engine not available")
            return {"status": "skipped", "reason": "no_backup_engine"}

        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM storage_targets WHERE enabled = 1"
            )
            targets = [dict(row) for row in await cursor.fetchall()]

        if not targets:
            logger.info("No enabled targets for verification")
            await self._mark_runs(run_ids, "skipped", "Skipped: no enabled storage targets")
            return {"status": "skipped", "reason": "no_targets"}

        # If no run_ids provided, check for pending 'running' rows in the DB
        if run_ids is None:
            run_ids = await self._find_pending_run_ids(targets)

        results = []
        for target in targets:
            try:
                target["config"] = decrypt_config(
                    target.get("config", "{}"),
                    str(self.config.config_dir),
                )
                existing_run_id = run_ids.get(target["id"]) if run_ids else None
                result = await self.verify_target(target, run_id=existing_run_id)
                results.append(result)
            except Exception as e:
                logger.error(
                    "Verification failed for target %s: %s",
                    target.get("name", target["id"]), e,
                )
                failed_run_id = run_ids.get(target["id"]) if run_ids else None
                if failed_run_id:
                    await self._mark_runs({target["id"]: failed_run_id}, "failed", f"Verification error: {e}")
                results.append({
                    "target_id": target["id"],
                    "target_name": target.get("name", ""),
                    "status": "failed",
                    "error": str(e),
                    "trust_score": 0.0,
                })

        overall_score = 0.0
        if results:
            scores = [r.get("trust_score", 0.0) for r in results]
            overall_score = round(sum(scores) / len(scores), 1)

        return {
            "status": "completed",
            "targets": results,
            "overall_trust_score": overall_score,
            "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _mark_runs(self, run_ids: dict[str, str] | None, status: str, reason: str) -> None:
        """Close out pre-created 'running' verification rows that won't be processed.

        Without this, rows inserted by the API trigger stay 'running' forever when
        the engine skips (backup in progress, no targets, ...) or errors.
        """
        if not run_ids:
            return
        ids = [rid for rid in run_ids.values() if rid]
        if not ids:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders = ", ".join("?" for _ in ids)  # nosec B608 - placeholders only
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    f"""UPDATE verification_runs
                        SET status = ?, completed_at = ?, error_message = ?
                        WHERE status = 'running' AND id IN ({placeholders})""",  # nosec B608
                    (status, now, reason[:500], *ids),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to mark verification runs %s: %s", status, exc)

    async def _find_pending_run_ids(self, targets: list[dict]) -> dict[str, str]:
        """Find existing 'running' verification_runs rows for the given targets.

        Returns a mapping of target_id -> run_id for rows to UPDATE.
        """
        result = {}
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='verification_runs'"
                )
                if not await cursor.fetchone():
                    return result
                for target in targets:
                    cursor = await db.execute(
                        """SELECT id FROM verification_runs
                           WHERE target_id = ? AND status = 'running'
                           ORDER BY started_at DESC LIMIT 1""",
                        (target["id"],),
                    )
                    row = await cursor.fetchone()
                    if row:
                        result[target["id"]] = row["id"]
        except Exception as e:
            logger.warning("Failed to find pending verification runs: %s", e)
        return result

    async def verify_target(self, target: dict, run_id: str | None = None) -> dict:
        """Run the full verification pipeline for a single target.

        Args:
            target: Storage target dict with id, name, config, etc.
            run_id: Optional pre-existing run_id from a 'running' verification_runs
                    row (created by the API trigger). If provided, the engine UPDATEs
                    that row. If None, the engine INSERTs a new row.

        Steps:
        1. restic check (repo consistency)
        2. Random file restore + SHA-256 hash
        3. DB dump validation
        4. Trust score computation
        """
        is_update = run_id is not None
        if not run_id:
            run_id = uuid.uuid4().hex
        target_id = target.get("id", "")
        target_name = target.get("name", target_id)
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info("Starting verification for target %s (%s)", target_id, target_name)

        # Step 1: restic check
        restic_check = await self._run_restic_check(target)

        # Step 2: restore test
        restore_test = await self._run_restore_test(target)

        # Step 3: DB dump validation
        db_validation = await self._validate_db_dumps()

        # Step 4: Compute recency
        recency = await self._compute_recency(target_id)

        # Compute component scores
        restic_ok = restic_check["status"] == "success"
        restore_ok = restore_test["status"] == "success"
        if restic_ok and restore_ok:
            file_integrity_score = 1.0
        elif restic_check["status"] == "failed":
            file_integrity_score = 0.0
        else:
            file_integrity_score = 0.5

        db_integrity_score = db_validation.get("score", 1.0)
        recency_score = compute_recency_score(recency.get("hours_since_backup"))

        trust_score = compute_trust_score(
            recency_score, file_integrity_score, db_integrity_score,
        )

        duration = round(time.monotonic() - start_time, 1)

        # Determine pass/fail based on whether key checks succeeded
        all_checks_ok = restic_ok and (restore_ok or restore_test["status"] == "skipped")
        verification_status = "passed" if all_checks_ok else "failed"

        result = {
            "run_id": run_id,
            "target_id": target_id,
            "target_name": target_name,
            "status": verification_status,
            "trust_score": trust_score,
            "restic_check": restic_check,
            "restore_test": restore_test,
            "db_validation": db_validation,
            "recency": recency,
            "scores": {
                "recency": round(recency_score * 100, 1),
                "file_integrity": round(file_integrity_score * 100, 1),
                "db_integrity": round(db_integrity_score * 100, 1),
            },
            "duration_seconds": duration,
            "started_at": started_at,
            "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        # Persist result (UPDATE if pre-existing row, INSERT otherwise)
        stored = await self._store_result(result, update=is_update)
        if not stored:
            result["_storage_failed"] = True

        logger.info(
            "Verification complete for target %s: trust_score=%.1f duration=%.1fs",
            target_name, trust_score, duration,
        )
        return result

    async def _run_restic_check(self, target: dict) -> dict:
        """Run restic check on the target repository."""
        try:
            result = await self.backup_engine.check(target)
            return {
                "status": result.get("status", "failed"),
                "output": result.get("output", ""),
                "error": result.get("error"),
            }
        except Exception as e:
            logger.error("restic check failed for target %s: %s", target.get("id"), e)
            return {"status": "failed", "error": str(e)}

    async def _run_restore_test(self, target: dict) -> dict:
        """Restore a random file from the latest snapshot and verify its SHA-256."""
        tmpdir = None
        try:
            # Get latest snapshot
            snapshots = await self.backup_engine.snapshots(target)
            if not snapshots:
                return {"status": "skipped", "reason": "no_snapshots"}

            latest = snapshots[-1]
            snapshot_id = latest.get("short_id", latest.get("id", "")[:8])

            # List files in snapshot — try root first, then walk into subdirs
            # restic uses "file"/"directory" types (not "dir")
            def _is_file(e):
                if e.get("type") not in ("file",):
                    return False
                size = e.get("size")
                return not (isinstance(size, int) and size > MAX_RESTORE_TEST_BYTES)

            def _is_dir(e):
                return e.get("type") in ("dir", "directory")

            entries = await self.backup_engine.ls(target, snapshot_id, "/")
            files = [e for e in entries if _is_file(e)]
            found_dir = ""  # parent path where files were found
            if not files:
                # Root has only directories — walk into subdirs to find files
                dirs = [e for e in entries if _is_dir(e)]
                for d in random.sample(dirs, min(len(dirs), 5)):  # nosec B311
                    subpath = "/" + d["name"]
                    sub_entries = await self.backup_engine.ls(target, snapshot_id, subpath)
                    sub_files = [e for e in sub_entries if _is_file(e)]
                    if sub_files:
                        files = sub_files
                        found_dir = subpath
                        break
                    # Try one more level deep
                    sub_dirs = [e for e in sub_entries if _is_dir(e)]
                    for sd in random.sample(sub_dirs, min(len(sub_dirs), 3)):  # nosec B311
                        deep_path = subpath + "/" + sd["name"]
                        deep_entries = await self.backup_engine.ls(target, snapshot_id, deep_path)
                        deep_files = [e for e in deep_entries if _is_file(e)]
                        if deep_files:
                            files = deep_files
                            found_dir = deep_path
                            break
                    if files:
                        break
            if not files:
                return {"status": "skipped", "reason": "no_files_in_snapshot"}

            # Non-security sampling for integrity check
            chosen = random.choice(files)  # nosec B311
            file_path = found_dir + "/" + chosen["name"]

            # Restore into the persistent restores/ dir — the container's /tmp lives
            # in docker.img on Unraid and must not absorb restored data.
            restore_root = str(self.config.restore_dir)
            await asyncio.to_thread(os.makedirs, restore_root, exist_ok=True)
            tmpdir = tempfile.mkdtemp(prefix="arkive-verify-", dir=restore_root)
            result = await self.backup_engine.restore(
                target=target,
                snapshot_id=snapshot_id,
                paths=[file_path],
                restore_to=tmpdir,
            )

            if result.get("status") != "success":
                return {
                    "status": "failed",
                    "error": result.get("error") or "Restore returned non-success",
                }

            expected_name = os.path.basename(file_path)
            restored_file, digest, file_size, found_files = await asyncio.to_thread(
                self._find_and_hash, tmpdir, expected_name
            )
            if not restored_file:
                logger.warning(
                    "Expected restored file %r but found: %s",
                    expected_name, found_files or "(empty)",
                )
                return {
                    "status": "failed",
                    "error": f"Expected file {expected_name!r} not found after restore",
                }

            return {
                "status": "success",
                "file": file_path,
                "sha256": digest,
                "size_bytes": file_size,
                "snapshot_id": snapshot_id,
            }

        except Exception as e:
            logger.error("Restore test failed: %s", e)
            return {"status": "failed", "error": str(e)}
        finally:
            if tmpdir and os.path.exists(tmpdir):
                await asyncio.to_thread(shutil.rmtree, tmpdir, True)

    @staticmethod
    def _find_and_hash(tmpdir: str, expected_name: str) -> tuple[str | None, str, int, list[str]]:
        """Locate the restored file (exact basename match) and SHA-256 it. Blocking."""
        found_files: list[str] = []
        restored_file = None
        for dirpath, _, filenames in os.walk(tmpdir):
            for fname in filenames:
                path = os.path.join(dirpath, fname)
                found_files.append(path)
                if fname == expected_name and restored_file is None:
                    restored_file = path
        if not restored_file:
            return None, "", 0, found_files
        sha256 = hashlib.sha256()
        file_size = 0
        with open(restored_file, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha256.update(chunk)
                file_size += len(chunk)
        return restored_file, sha256.hexdigest(), file_size, found_files

    async def _validate_db_dumps(self) -> dict:
        """Validate database dump files in the dump directory.

        - SQLite: sqlite3 <file> 'PRAGMA integrity_check'
        - SQL dumps (.sql / .sql.gz from pg_dump, pg_dumpall, mysqldump): the
          tool's completion trailer must end the (uncompressed) stream
        - other Postgres dumps (.dump / .pgdump, foreign .sql): pg_restore --list
        """
        dump_dir = str(self.config.dump_dir)
        if not os.path.isdir(dump_dir):
            return {"status": "skipped", "reason": "no_dump_dir", "score": 1.0}

        results = []
        total = 0
        passed = 0

        for fname in os.listdir(dump_dir):
            fpath = os.path.join(dump_dir, fname)
            if not os.path.isfile(fpath):
                continue

            if fname.endswith(".db") or fname.endswith(".sqlite") or fname.endswith(".sqlite3"):
                total += 1
                check = await self._check_sqlite_dump(fpath)
                results.append(check)
                if check["status"] == "success":
                    passed += 1

            elif fname.endswith(".sql"):
                # Plain logical dumps written by DBDumper (dump_compression=none).
                total += 1
                check = await asyncio.to_thread(self._check_plain_sql_dump, fpath)
                if check is None:
                    # Not an Arkive dump and no known trailer: structural check.
                    check = await self._check_postgres_dump(fpath)
                results.append(check)
                if check["status"] == "success":
                    passed += 1

            elif fname.endswith(".dump") or fname.endswith(".pgdump"):
                total += 1
                check = await self._check_postgres_dump(fpath)
                results.append(check)
                if check["status"] == "success":
                    passed += 1

            elif fname.endswith(".sql.gz"):
                # Postgres / MySQL logical dumps written by DBDumper.
                total += 1
                check = await asyncio.to_thread(self._check_gzip_sql_dump, fpath)
                results.append(check)
                if check["status"] == "success":
                    passed += 1

        if total == 0:
            return {"status": "skipped", "reason": "no_dumps_found", "score": 1.0, "files": []}

        score = passed / total if total > 0 else 1.0
        return {
            "status": "completed",
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "score": score,
            "files": results,
        }

    async def _check_sqlite_dump(self, fpath: str) -> dict:
        """Validate a SQLite dump file using PRAGMA integrity_check."""
        try:
            result = await run_command(
                ["sqlite3", fpath, "PRAGMA integrity_check;"],
                timeout=60,
            )
            # integrity_check prints exactly "ok" for a healthy DB; error rows can
            # contain "ok" as a substring (e.g. index "idx_bookmarks").
            ok = result.returncode == 0 and result.stdout.strip().lower() == "ok"
            return {
                "file": os.path.basename(fpath),
                "type": "sqlite",
                "status": "success" if ok else "failed",
                "output": result.stdout[:200] if result.stdout else "",
                "error": result.stderr[:200] if not ok and result.stderr else None,
            }
        except Exception as e:
            return {
                "file": os.path.basename(fpath),
                "type": "sqlite",
                "status": "failed",
                "error": str(e),
            }

    @staticmethod
    def _trailer_check(name: str, tail: bytes) -> dict | None:
        """Success result when ``tail`` ends a complete pg_dump/pg_dumpall/mysqldump stream."""
        if PG_DUMP_TRAILER in tail or PG_DUMPALL_TRAILER in tail:
            return {"file": name, "type": "postgres", "status": "success", "output": "Dump trailer present"}
        if MYSQL_DUMP_TRAILER in tail:
            return {"file": name, "type": "mariadb", "status": "success", "output": "Dump trailer present"}
        return None

    @classmethod
    def _check_gzip_sql_dump(cls, fpath: str) -> dict:
        """Validate a gzipped SQL dump: the gzip stream must be intact and the
        dump must end with the tool's completion trailer (pg_dump/mysqldump)."""
        name = os.path.basename(fpath)
        tail = b""
        try:
            with gzip.open(fpath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    tail = (tail + chunk[-_SQL_TAIL_BYTES:])[-_SQL_TAIL_BYTES:]
        except (OSError, EOFError, zlib.error) as e:
            return {"file": name, "type": "sql", "status": "failed", "error": f"Corrupt gzip stream: {e}"}
        return cls._trailer_check(name, tail) or {
            "file": name, "type": "sql", "status": "failed", "error": "Dump is truncated (no completion trailer)",
        }

    @classmethod
    def _check_plain_sql_dump(cls, fpath: str) -> dict | None:
        """Validate a plain .sql dump by its completion trailer, reading only the tail.

        Returns None for a .sql file that has no known trailer and does not
        follow Arkive's dump naming (not ours: the caller falls back to a
        structural check). An Arkive dump without its trailer is truncated.
        """
        name = os.path.basename(fpath)
        try:
            with open(fpath, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - _SQL_TAIL_BYTES))
                tail = f.read()
        except OSError as e:
            return {"file": name, "type": "sql", "status": "failed", "error": f"Cannot read dump: {e}"}
        found = cls._trailer_check(name, tail)
        if found is not None:
            return found
        if not is_arkive_dump_name(name):
            return None
        error = "Empty dump file" if size == 0 else "Dump is truncated (no completion trailer)"
        return {"file": name, "type": "sql", "status": "failed", "error": error}

    async def _check_postgres_dump(self, fpath: str) -> dict:
        """Validate a Postgres dump by checking its structure with pg_restore --list."""
        try:
            result = await run_command(
                ["pg_restore", "--list", fpath],
                timeout=60,
            )
            # pg_restore --list returns 0 on valid custom-format dumps.
            # For plain SQL dumps it returns non-zero, so fall back to
            # checking if the file is non-empty and looks like SQL.
            if result.returncode == 0:
                return {
                    "file": os.path.basename(fpath),
                    "type": "postgres",
                    "status": "success",
                    "output": result.stdout[:200] if result.stdout else "",
                }

            # Plain SQL fallback: check file is non-empty and starts with SQL
            try:
                size = os.path.getsize(fpath)
                if size == 0:
                    return {
                        "file": os.path.basename(fpath),
                        "type": "postgres",
                        "status": "failed",
                        "error": "Empty dump file",
                    }
                with open(fpath, "r", errors="replace") as f:
                    header = f.read(256)
                sql_markers = ["--", "CREATE", "INSERT", "SET ", "BEGIN", "COPY"]
                if any(marker in header.upper() for marker in sql_markers):
                    return {
                        "file": os.path.basename(fpath),
                        "type": "postgres",
                        "status": "success",
                        "output": "Plain SQL dump verified (header check)",
                    }
            except Exception:
                pass

            return {
                "file": os.path.basename(fpath),
                "type": "postgres",
                "status": "failed",
                "error": result.stderr[:200] if result.stderr else "Not a valid dump",
            }
        except Exception as e:
            return {
                "file": os.path.basename(fpath),
                "type": "postgres",
                "status": "failed",
                "error": str(e),
            }

    async def _compute_recency(self, target_id: str) -> dict:
        """Compute hours since the last successful backup for this target."""
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """SELECT jrt.run_id, jr.completed_at
                       FROM job_run_targets jrt
                       JOIN job_runs jr ON jr.id = jrt.run_id
                       WHERE jrt.target_id = ? AND jrt.status = 'success'
                         AND jr.completed_at IS NOT NULL
                       ORDER BY jr.completed_at DESC
                       LIMIT 1""",
                    (target_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    return {"hours_since_backup": None, "last_backup": None}

                last_backup = row["completed_at"]
                last_dt = datetime.fromisoformat(last_backup.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours = (now - last_dt).total_seconds() / 3600
                return {
                    "hours_since_backup": round(hours, 1),
                    "last_backup": last_backup,
                }
        except Exception as e:
            logger.error("Failed to compute recency for target %s: %s", target_id, e)
            return {"hours_since_backup": None, "last_backup": None}

    async def _store_result(self, result: dict, update: bool = False) -> bool:
        """Persist verification result to the verification_runs table.

        Args:
            result: Verification result dict.
            update: If True, UPDATE an existing 'running' row (API trigger path).
                    If False, INSERT a new row (scheduler path).

        Returns True on success, False on failure.

        Uses the schema defined in database.py:
        id, target_id, started_at, completed_at, status, trust_score,
        files_checked, files_passed, databases_checked, databases_passed,
        restic_check_passed, error_message
        """
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                # Check if table exists (schema may not be migrated yet)
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='verification_runs'"
                )
                if not await cursor.fetchone():
                    logger.warning("verification_runs table not found, skipping result storage")
                    return False

                db_val = result.get("db_validation", {})
                restore_test = result.get("restore_test", {})
                files_checked = 1 if restore_test.get("status") != "skipped" else 0
                files_passed = 1 if restore_test.get("status") == "success" else 0
                restic_passed = 1 if result["restic_check"]["status"] == "success" else 0

                if update:
                    await db.execute(
                        """UPDATE verification_runs
                           SET completed_at = ?, status = ?, trust_score = ?,
                               files_checked = ?, files_passed = ?,
                               databases_checked = ?, databases_passed = ?,
                               restic_check_passed = ?, error_message = ?
                           WHERE id = ?""",
                        (
                            result["verified_at"],
                            result["status"],
                            int(result["trust_score"]),
                            files_checked,
                            files_passed,
                            db_val.get("total", 0),
                            db_val.get("passed", 0),
                            restic_passed,
                            None,
                            result["run_id"],
                        ),
                    )
                else:
                    await db.execute(
                        """INSERT INTO verification_runs
                           (id, target_id, started_at, completed_at, status, trust_score,
                            files_checked, files_passed, databases_checked, databases_passed,
                            restic_check_passed, error_message)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            result["run_id"],
                            result["target_id"],
                            result.get("started_at", result["verified_at"]),
                            result["verified_at"],
                            result["status"],
                            int(result["trust_score"]),
                            files_checked,
                            files_passed,
                            db_val.get("total", 0),
                            db_val.get("passed", 0),
                            restic_passed,
                            None,
                        ),
                    )
                await db.commit()
                return True
        except Exception as e:
            logger.error("Failed to store verification result: %s", e)
            return False
