"""Unit tests for the VerifyEngine and scheduler integration.

Tests verify:
- Trust score algorithm (weighted average with recency, file integrity, DB integrity)
- VerifyEngine.verify_target pipeline (restic check, restore test, DB validation)
- Lock checking (skip verification during active backup)
- Scheduler system job registration and execution
- Error handling (graceful failure without crashes)
- DB dump validation (SQLite and Postgres)
"""

import json
import os
import sqlite3

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Forked to avoid aiosqlite daemon thread leaks across test files.
pytestmark = pytest.mark.forked

import aiosqlite

from app.core.database import SCHEMA_SQL, MIGRATIONS
from app.services.verify_engine import (
    VerifyEngine,
    compute_recency_score,
    compute_trust_score,
    WEIGHT_RECENCY,
    WEIGHT_FILE_INTEGRITY,
    WEIGHT_DB_INTEGRITY,
)
from app.services.scheduler import (
    ArkiveScheduler,
    SYSTEM_JOB_INTEGRITY_VERIFY,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config(tmp_path):
    """Config with real temp DB and dump directory."""
    config = MagicMock()
    db_path = tmp_path / "arkive.db"
    config.db_path = str(db_path)
    config.config_dir = tmp_path
    config.dump_dir = tmp_path / "dumps"
    config.dump_dir.mkdir(exist_ok=True)

    from app.core.security import _load_fernet_from_dir, _reset_fernet
    _reset_fernet()
    _load_fernet_from_dir(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    for version in sorted(MIGRATIONS):
        for sql in MIGRATIONS[version]:
            conn.execute(sql)
    conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                 (max(MIGRATIONS),))
    conn.commit()
    conn.close()
    return config


@pytest.fixture
def mock_backup_engine():
    engine = MagicMock()
    engine.check = AsyncMock(return_value={"status": "success", "output": "no errors found"})
    engine.snapshots = AsyncMock(return_value=[
        {"id": "abcd1234abcd1234", "short_id": "abcd1234", "time": "2026-03-17T10:00:00Z",
         "hostname": "test-server", "paths": ["/config/dumps"], "tags": [], "size": 1024},
    ])
    engine.ls = AsyncMock(return_value=[
        {"name": "testfile.txt", "type": "file", "size": 100, "modified": "2026-03-17T10:00:00Z"},
    ])
    engine.restore = AsyncMock(return_value={"status": "success", "output": "restored 1 file"})
    return engine


@pytest.fixture
def verify_engine(mock_config, mock_backup_engine):
    return VerifyEngine(config=mock_config, backup_engine=mock_backup_engine)


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.run_backup = AsyncMock()
    return orch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_target(db_path, target_id="t1", name="Local", type_="local",
                         enabled=1, config_str="{}"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO storage_targets (id, name, type, enabled, config, status)
               VALUES (?, ?, ?, ?, ?, 'unknown')""",
            (target_id, name, type_, enabled, config_str),
        )
        await db.commit()


async def _insert_successful_run(db_path, target_id="t1", completed_at="2026-03-17T10:00:00Z"):
    """Insert a successful backup job run for recency testing."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO backup_jobs (id, name, schedule) VALUES ('j1', 'Test', '0 2 * * *')"
        )
        await db.execute(
            """INSERT INTO job_runs (id, job_id, status, trigger, completed_at)
               VALUES ('r1', 'j1', 'success', 'scheduled', ?)""",
            (completed_at,),
        )
        await db.execute(
            """INSERT INTO job_run_targets (run_id, target_id, status, upload_bytes)
               VALUES ('r1', ?, 'success', 1024)""",
            (target_id,),
        )
        await db.commit()


# ===========================================================================
# 1. Trust score algorithm
# ===========================================================================


class TestTrustScoreAlgorithm:
    """Verify the weighted trust score computation."""

    def test_perfect_score(self):
        """All components at 1.0 yields 100.0."""
        score = compute_trust_score(1.0, 1.0, 1.0)
        assert score == 100.0

    def test_zero_score(self):
        """All components at 0.0 yields 0.0."""
        score = compute_trust_score(0.0, 0.0, 0.0)
        assert score == 0.0

    def test_weights_add_to_one(self):
        """Weights must sum to 1.0 for proper scaling."""
        assert WEIGHT_RECENCY + WEIGHT_FILE_INTEGRITY + WEIGHT_DB_INTEGRITY == 1.0

    def test_partial_score(self):
        """Mixed component values produce the correct weighted average."""
        # recency=1.0 (30%), file=0.5 (40%), db=1.0 (30%)
        # = 0.30*1.0 + 0.40*0.5 + 0.30*1.0 = 0.30 + 0.20 + 0.30 = 0.80
        score = compute_trust_score(1.0, 0.5, 1.0)
        assert score == 80.0

    def test_file_integrity_dominates(self):
        """File integrity has the highest weight (40%)."""
        # Only file integrity = 1.0, others = 0.0
        score = compute_trust_score(0.0, 1.0, 0.0)
        assert score == WEIGHT_FILE_INTEGRITY * 100

    def test_recency_only(self):
        """Recency at 1.0 with others at 0.0."""
        score = compute_trust_score(1.0, 0.0, 0.0)
        assert score == WEIGHT_RECENCY * 100


class TestRecencyScore:
    """Verify recency score thresholds."""

    def test_none_hours(self):
        assert compute_recency_score(None) == 0.0

    def test_excellent_recency(self):
        assert compute_recency_score(12) == 1.0

    def test_good_recency(self):
        assert compute_recency_score(48) == 0.8

    def test_acceptable_recency(self):
        assert compute_recency_score(120) == 0.6

    def test_stale_recency(self):
        assert compute_recency_score(200) == 0.3

    def test_very_stale(self):
        assert compute_recency_score(500) == 0.0

    def test_boundary_24h(self):
        """Exactly 24h should NOT be excellent (>= 24h boundary)."""
        assert compute_recency_score(24) == 0.8


# ===========================================================================
# 2. VerifyEngine.verify_target pipeline
# ===========================================================================


class TestVerifyTarget:
    """Test the full verification pipeline for a single target."""

    @pytest.mark.asyncio
    async def test_verify_target_success(self, verify_engine, mock_backup_engine, mock_config, tmp_path):
        """Successful verification returns completed status with trust score."""
        # Create a restored file for the restore test to find
        await _insert_successful_run(mock_config.db_path, completed_at="2026-03-18T10:00:00Z")

        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}

        # Patch tempfile to control where restored files go
        restore_dir = tmp_path / "restore_test"
        restore_dir.mkdir()
        test_file = restore_dir / "testfile.txt"
        test_file.write_text("hello world")

        with patch("app.services.verify_engine.tempfile.mkdtemp", return_value=str(restore_dir)):
            result = await verify_engine.verify_target(target)

        assert result["status"] == "passed"
        assert result["target_id"] == "t1"
        assert "trust_score" in result
        assert result["trust_score"] >= 0.0
        assert result["restic_check"]["status"] == "success"
        assert result["restore_test"]["status"] == "success"

    @pytest.mark.asyncio
    async def test_verify_target_restic_check_fails(self, verify_engine, mock_backup_engine, mock_config, tmp_path):
        """Failed restic check reduces the trust score."""
        mock_backup_engine.check.return_value = {"status": "failed", "error": "corruption"}

        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}

        restore_dir = tmp_path / "restore_test"
        restore_dir.mkdir()
        test_file = restore_dir / "testfile.txt"
        test_file.write_text("hello")

        with patch("app.services.verify_engine.tempfile.mkdtemp", return_value=str(restore_dir)):
            result = await verify_engine.verify_target(target)

        assert result["restic_check"]["status"] == "failed"
        assert result["scores"]["file_integrity"] == 0.0

    @pytest.mark.asyncio
    async def test_verify_target_no_snapshots(self, verify_engine, mock_backup_engine):
        """Verify handles targets with no snapshots gracefully."""
        mock_backup_engine.snapshots.return_value = []
        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}

        result = await verify_engine.verify_target(target)

        assert result["status"] == "passed"
        assert result["restore_test"]["status"] == "skipped"


# ===========================================================================
# 3. Lock checking
# ===========================================================================


class TestLockChecking:
    """Verify that verification is skipped during active backups."""

    @pytest.mark.asyncio
    async def test_skips_when_backup_running(self, verify_engine):
        """verify_all_targets returns skipped when backup lock is held."""
        with patch.object(verify_engine.lock_manager, "is_backup_running", return_value=True):
            result = await verify_engine.verify_all_targets()

        assert result["status"] == "skipped"
        assert result["reason"] == "backup_in_progress"

    @pytest.mark.asyncio
    async def test_skips_when_restore_running(self, verify_engine):
        """verify_all_targets returns skipped when restore lock is held."""
        with patch.object(verify_engine.lock_manager, "is_restore_running", return_value=True):
            result = await verify_engine.verify_all_targets()

        assert result["status"] == "skipped"
        assert result["reason"] == "backup_in_progress"

    @pytest.mark.asyncio
    async def test_skips_without_backup_engine(self, mock_config):
        """verify_all_targets returns skipped when backup engine is None."""
        engine = VerifyEngine(config=mock_config, backup_engine=None)
        result = await engine.verify_all_targets()

        assert result["status"] == "skipped"
        assert result["reason"] == "no_backup_engine"


# ===========================================================================
# 4. Scheduler system job registration
# ===========================================================================


class TestSchedulerIntegration:
    """Verify system job registration and execution for integrity_verify."""

    @pytest.fixture
    def mock_verify_engine(self):
        ve = MagicMock()
        ve.verify_all_targets = AsyncMock(return_value={
            "status": "completed",
            "overall_trust_score": 85.0,
            "targets": [],
        })
        return ve

    @pytest.fixture
    def scheduler(self, mock_orchestrator, mock_config, mock_verify_engine):
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            verify_engine=mock_verify_engine,
        )
        yield sched
        if sched.scheduler.running:
            sched.scheduler.shutdown(wait=False)

    def test_integrity_verify_job_registered(self, scheduler):
        """Integrity verify job gets registered on _register_system_jobs."""
        scheduler._register_system_jobs()
        assert scheduler.scheduler.get_job(SYSTEM_JOB_INTEGRITY_VERIFY) is not None

    def test_integrity_verify_job_name(self, scheduler):
        """Integrity verify job has the expected human-readable name."""
        scheduler._register_system_jobs()
        job = scheduler.scheduler.get_job(SYSTEM_JOB_INTEGRITY_VERIFY)
        assert job.name == "Integrity Verify"

    def test_integrity_verify_not_in_job_map(self, scheduler):
        """System jobs should not appear in _job_map (user jobs only)."""
        scheduler._register_system_jobs()
        assert SYSTEM_JOB_INTEGRITY_VERIFY not in scheduler._job_map

    def test_integrity_verify_idempotent(self, scheduler):
        """Calling _register_system_jobs twice does not duplicate the job."""
        scheduler._register_system_jobs()
        scheduler._register_system_jobs()
        count = sum(
            1 for j in scheduler.scheduler.get_jobs()
            if j.id == SYSTEM_JOB_INTEGRITY_VERIFY
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_integrity_verify_runs(self, scheduler, mock_verify_engine, mock_config):
        """Integrity verify job calls verify_all_targets and logs activity."""
        await scheduler._run_integrity_verify()

        mock_verify_engine.verify_all_targets.assert_awaited_once()

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM activity_log WHERE type = 'system' AND action = 'integrity_verify'"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert "trust score" in row["message"]

    @pytest.mark.asyncio
    async def test_integrity_verify_skips_without_engine(self, mock_orchestrator, mock_config):
        """Integrity verify is a no-op when verify_engine is None."""
        sched = ArkiveScheduler(
            orchestrator=mock_orchestrator,
            config=mock_config,
            verify_engine=None,
        )
        await sched._run_integrity_verify()  # should not raise

    def test_constant_value(self):
        """SYSTEM_JOB_INTEGRITY_VERIFY constant has expected string value."""
        assert SYSTEM_JOB_INTEGRITY_VERIFY == "system_integrity_verify"

    @pytest.mark.asyncio
    async def test_integrity_verify_handles_error(self, scheduler, mock_verify_engine):
        """Integrity verify catches exceptions and does not crash."""
        mock_verify_engine.verify_all_targets.side_effect = RuntimeError("engine exploded")
        await scheduler._run_integrity_verify()  # should not raise


# ===========================================================================
# 5. Error handling
# ===========================================================================


class TestErrorHandling:
    """Verify graceful error handling in verification pipeline."""

    @pytest.mark.asyncio
    async def test_restic_check_exception(self, verify_engine, mock_backup_engine):
        """Exception in restic check is caught and returns failed status."""
        mock_backup_engine.check.side_effect = RuntimeError("restic binary missing")
        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}
        result = await verify_engine._run_restic_check(target)
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_restore_test_exception(self, verify_engine, mock_backup_engine):
        """Exception in restore test is caught and returns failed status."""
        mock_backup_engine.snapshots.side_effect = RuntimeError("network error")
        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}
        result = await verify_engine._run_restore_test(target)
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_verify_all_targets_one_fails(self, verify_engine, mock_backup_engine, mock_config, tmp_path):
        """If one target fails, others still get processed and overall result is returned."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local")
        await _insert_target(mock_config.db_path, "t2", "B2", "b2")

        call_count = 0
        original_check = mock_backup_engine.check

        async def check_side_effect(target):
            nonlocal call_count
            call_count += 1
            if target["id"] == "t1":
                raise RuntimeError("check crashed")
            return {"status": "success", "output": "ok"}

        mock_backup_engine.check.side_effect = check_side_effect

        with patch.object(verify_engine.lock_manager, "is_backup_running", return_value=False), \
             patch.object(verify_engine.lock_manager, "is_restore_running", return_value=False):
            result = await verify_engine.verify_all_targets()

        assert result["status"] == "completed"
        assert len(result["targets"]) == 2

    @pytest.mark.asyncio
    async def test_no_targets(self, verify_engine, mock_config):
        """Returns skipped when no enabled targets exist."""
        with patch.object(verify_engine.lock_manager, "is_backup_running", return_value=False), \
             patch.object(verify_engine.lock_manager, "is_restore_running", return_value=False):
            result = await verify_engine.verify_all_targets()

        assert result["status"] == "skipped"
        assert result["reason"] == "no_targets"


# ===========================================================================
# 6. DB dump validation
# ===========================================================================


class TestDBDumpValidation:
    """Test SQLite and Postgres dump validation logic."""

    @pytest.mark.asyncio
    async def test_sqlite_dump_valid(self, verify_engine, mock_config):
        """Valid SQLite dump returns success."""
        dump_file = mock_config.dump_dir / "test.db"
        conn = sqlite3.connect(str(dump_file))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        result = await verify_engine._validate_db_dumps()
        assert result["status"] == "completed"
        assert result["passed"] >= 1
        assert result["score"] > 0

    @pytest.mark.asyncio
    async def test_no_dump_dir(self, mock_config, mock_backup_engine):
        """Missing dump directory returns skipped with score 1.0."""
        import shutil
        shutil.rmtree(str(mock_config.dump_dir), ignore_errors=True)

        engine = VerifyEngine(config=mock_config, backup_engine=mock_backup_engine)
        result = await engine._validate_db_dumps()
        assert result["status"] == "skipped"
        assert result["score"] == 1.0

    @pytest.mark.asyncio
    async def test_no_dumps_found(self, verify_engine, mock_config):
        """Empty dump directory returns skipped with score 1.0."""
        result = await verify_engine._validate_db_dumps()
        assert result["status"] == "skipped"
        assert result["score"] == 1.0

    @pytest.mark.asyncio
    async def test_postgres_plain_sql_dump(self, verify_engine, mock_config):
        """Plain SQL Postgres dump is validated via header check."""
        dump_file = mock_config.dump_dir / "testdb.sql"
        dump_file.write_text("-- PostgreSQL database dump\nCREATE TABLE t (id INTEGER);\n")

        with patch("app.services.verify_engine.run_command", new_callable=AsyncMock) as mock_cmd:
            # pg_restore --list fails on plain SQL, so simulate that
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "not a custom format dump"
            mock_cmd.return_value = mock_result

            result = await verify_engine._validate_db_dumps()

        assert result["status"] == "completed"
        # The plain SQL check should detect the header and mark as success
        sql_file = [f for f in result["files"] if f["file"] == "testdb.sql"]
        assert len(sql_file) == 1
        assert sql_file[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_mixed_dumps_score(self, verify_engine, mock_config):
        """Score is correctly computed with mixed pass/fail dumps."""
        # Create one valid SQLite dump
        good_dump = mock_config.dump_dir / "good.db"
        conn = sqlite3.connect(str(good_dump))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        # Create one corrupt "SQLite" dump (not actually sqlite)
        bad_dump = mock_config.dump_dir / "bad.db"
        bad_dump.write_bytes(b"not a database")

        result = await verify_engine._validate_db_dumps()
        assert result["status"] == "completed"
        assert result["total"] == 2
        # At least the good one should pass
        assert result["passed"] >= 1
        assert 0.0 < result["score"] <= 1.0


# ===========================================================================
# 7. Recency computation
# ===========================================================================


class TestRecencyComputation:
    """Test the _compute_recency method against real DB data."""

    @pytest.mark.asyncio
    async def test_no_backups(self, verify_engine):
        """No backup runs returns None hours."""
        result = await verify_engine._compute_recency("t1")
        assert result["hours_since_backup"] is None
        assert result["last_backup"] is None

    @pytest.mark.asyncio
    async def test_recent_backup(self, verify_engine, mock_config):
        """Recent successful backup returns computed hours."""
        await _insert_successful_run(
            mock_config.db_path, "t1", "2026-03-18T10:00:00Z"
        )
        result = await verify_engine._compute_recency("t1")
        assert result["hours_since_backup"] is not None
        assert result["last_backup"] == "2026-03-18T10:00:00Z"


# ===========================================================================
# 8. Result storage
# ===========================================================================


class TestResultStorage:
    """Test persistence of verification results."""

    @pytest.mark.asyncio
    async def test_store_skipped_when_table_missing(self, mock_backup_engine, tmp_path):
        """Storing results returns False when verification_runs table doesn't exist."""
        # Create a minimal DB without the verification_runs table
        empty_db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(empty_db))
        conn.execute("CREATE TABLE dummy (id TEXT)")
        conn.commit()
        conn.close()

        config = MagicMock()
        config.db_path = str(empty_db)
        config.config_dir = tmp_path
        config.dump_dir = tmp_path / "dumps"

        engine = VerifyEngine(config=config, backup_engine=mock_backup_engine)

        result = {
            "run_id": "test123",
            "target_id": "t1",
            "status": "passed",
            "trust_score": 85.0,
            "restic_check": {"status": "success"},
            "restore_test": {"status": "success"},
            "db_validation": {"status": "completed"},
            "duration_seconds": 10.0,
            "verified_at": "2026-03-18T12:00:00Z",
        }
        stored = await engine._store_result(result)
        assert stored is False

    @pytest.mark.asyncio
    async def test_store_insert(self, verify_engine, mock_config):
        """INSERT path: new row is created when update=False."""
        result = {
            "run_id": "test123",
            "target_id": "t1",
            "status": "passed",
            "trust_score": 85.0,
            "restic_check": {"status": "success"},
            "restore_test": {"status": "success"},
            "db_validation": {"status": "completed", "total": 3, "passed": 2},
            "duration_seconds": 10.0,
            "verified_at": "2026-03-18T12:00:00Z",
        }
        stored = await verify_engine._store_result(result, update=False)
        assert stored is True

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM verification_runs WHERE id = 'test123'")
            row = await cursor.fetchone()

        assert row is not None
        assert row["trust_score"] == 85
        assert row["target_id"] == "t1"
        assert row["restic_check_passed"] == 1
        assert row["files_checked"] == 1
        assert row["files_passed"] == 1
        assert row["databases_checked"] == 3
        assert row["databases_passed"] == 2

    @pytest.mark.asyncio
    async def test_store_update(self, verify_engine, mock_config):
        """UPDATE path: existing 'running' row is completed when update=True."""
        # Create a 'running' row (as the API trigger would)
        await _insert_target(mock_config.db_path, "t1", "Local", "local")
        async with aiosqlite.connect(mock_config.db_path) as db:
            await db.execute(
                """INSERT INTO verification_runs (id, target_id, started_at, status)
                   VALUES ('api-run-1', 't1', '2026-03-18T11:00:00Z', 'running')"""
            )
            await db.commit()

        result = {
            "run_id": "api-run-1",
            "target_id": "t1",
            "status": "passed",
            "trust_score": 92.0,
            "restic_check": {"status": "success"},
            "restore_test": {"status": "success"},
            "db_validation": {"status": "completed", "total": 1, "passed": 1},
            "duration_seconds": 5.0,
            "verified_at": "2026-03-18T11:05:00Z",
        }
        stored = await verify_engine._store_result(result, update=True)
        assert stored is True

        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM verification_runs WHERE id = 'api-run-1'")
            row = await cursor.fetchone()

        assert row is not None
        assert row["status"] == "passed"
        assert row["trust_score"] == 92
        assert row["completed_at"] == "2026-03-18T11:05:00Z"
        assert row["restic_check_passed"] == 1

    @pytest.mark.asyncio
    async def test_verify_target_uses_provided_run_id(self, verify_engine, mock_backup_engine, mock_config, tmp_path):
        """When run_id is provided, verify_target uses it instead of generating a new one."""
        await _insert_target(mock_config.db_path, "t1", "Local", "local")
        async with aiosqlite.connect(mock_config.db_path) as db:
            await db.execute(
                """INSERT INTO verification_runs (id, target_id, started_at, status)
                   VALUES ('pre-created-id', 't1', '2026-03-18T11:00:00Z', 'running')"""
            )
            await db.commit()

        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}

        restore_dir = tmp_path / "restore_test"
        restore_dir.mkdir()
        (restore_dir / "testfile.txt").write_text("hello")

        with patch("app.services.verify_engine.tempfile.mkdtemp", return_value=str(restore_dir)):
            result = await verify_engine.verify_target(target, run_id="pre-created-id")

        assert result["run_id"] == "pre-created-id"
        assert result["status"] == "passed"

        # The existing row should be UPDATED, not a new one INSERTed
        async with aiosqlite.connect(mock_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM verification_runs WHERE id = 'pre-created-id'")
            row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "passed"

    @pytest.mark.asyncio
    async def test_verify_target_generates_full_uuid(self, verify_engine, mock_backup_engine, tmp_path):
        """When no run_id is provided, verify_target generates a full hex UUID."""
        target = {"id": "t1", "name": "Local", "type": "local", "config": "{}"}

        restore_dir = tmp_path / "restore_test"
        restore_dir.mkdir()
        (restore_dir / "testfile.txt").write_text("hello")

        with patch("app.services.verify_engine.tempfile.mkdtemp", return_value=str(restore_dir)):
            result = await verify_engine.verify_target(target)

        assert len(result["run_id"]) == 32  # uuid4().hex is 32 chars
