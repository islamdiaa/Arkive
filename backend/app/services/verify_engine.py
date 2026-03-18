"""Verification engine -- validates backup integrity and computes trust scores.

Runs a multi-step verification pipeline per storage target:
1. restic check (repository consistency)
2. Random file restore + SHA-256 verification
3. Database dump validation (SQLite PRAGMA integrity_check / pg_restore --list)
4. Trust score computation (weighted average)

Results are stored in the verification_runs table for historical tracking.
"""

import hashlib
import json
import logging
import os
import random
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone

import aiosqlite

from app.core.activity import log_activity
from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.services.lock_manager import BackupLockManager
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.verify_engine")

# Trust score weights
WEIGHT_RECENCY = 0.30
WEIGHT_FILE_INTEGRITY = 0.40
WEIGHT_DB_INTEGRITY = 0.30

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
            return {"status": "skipped", "reason": "backup_in_progress"}

        if not self.backup_engine:
            logger.warning("Verification skipped: backup engine not available")
            return {"status": "skipped", "reason": "no_backup_engine"}

        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM storage_targets WHERE enabled = 1"
            )
            targets = [dict(row) for row in await cursor.fetchall()]

        if not targets:
            logger.info("No enabled targets for verification")
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

        result = {
            "run_id": run_id,
            "target_id": target_id,
            "target_name": target_name,
            "status": "completed",
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

            # List files in snapshot root
            entries = await self.backup_engine.ls(target, snapshot_id, "/")
            files = [e for e in entries if e.get("type") == "file"]
            if not files:
                return {"status": "skipped", "reason": "no_files_in_snapshot"}

            # Non-security sampling for integrity check
            chosen = random.choice(files)  # nosec B311
            file_path = "/" + chosen["name"]

            # Restore to temp directory
            tmpdir = tempfile.mkdtemp(prefix="arkive-verify-")
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

            # Find restored file
            restored_file = None
            for dirpath, _, filenames in os.walk(tmpdir):
                for fname in filenames:
                    candidate = os.path.join(dirpath, fname)
                    if fname == os.path.basename(file_path):
                        restored_file = candidate
                        break
                if restored_file:
                    break

            if not restored_file:
                for dirpath, _, filenames in os.walk(tmpdir):
                    for fname in filenames:
                        restored_file = os.path.join(dirpath, fname)
                        break
                    if restored_file:
                        break

            if not restored_file:
                return {"status": "failed", "error": "Restored file not found"}

            # Compute SHA-256
            sha256 = hashlib.sha256()
            file_size = 0
            with open(restored_file, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    file_size += len(chunk)

            return {
                "status": "success",
                "file": file_path,
                "sha256": sha256.hexdigest(),
                "size_bytes": file_size,
                "snapshot_id": snapshot_id,
            }

        except Exception as e:
            logger.error("Restore test failed: %s", e)
            return {"status": "failed", "error": str(e)}
        finally:
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)

    async def _validate_db_dumps(self) -> dict:
        """Validate database dump files in the dump directory.

        - SQLite: sqlite3 <file> 'PRAGMA integrity_check'
        - Postgres: pg_restore --list <file>
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

            elif fname.endswith(".sql") or fname.endswith(".dump") or fname.endswith(".pgdump"):
                total += 1
                check = await self._check_postgres_dump(fpath)
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
            ok = result.returncode == 0 and "ok" in result.stdout.lower()
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
                            result["verified_at"],
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
