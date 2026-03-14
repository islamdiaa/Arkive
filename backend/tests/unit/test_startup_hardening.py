"""Tests for startup hardening: body size limit middleware and stale job run cleanup."""

from datetime import UTC, datetime
from unittest.mock import patch

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def startup_client():
    """Reuse one app/client harness for body-size middleware checks."""
    from app.main import create_app

    fastapi_app = create_app()
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as client:
        yield client


async def _setup_db_with_job_run(db_path: str, status: str) -> str:
    """Create schema, insert a backup_job and one job_run. Returns the job_run id."""
    from app.core.database import init_db

    await init_db(db_path)
    job_id = "job-test-1"
    run_id = "run-test-1"
    async with aiosqlite.connect(db_path) as db:
        # Insert parent backup_job first (FK enforced)
        await db.execute(
            "INSERT INTO backup_jobs (id, name, type, schedule) VALUES (?, ?, ?, ?)",
            (job_id, "Test Job", "full", "0 2 * * *"),
        )
        await db.execute(
            "INSERT INTO job_runs (id, job_id, status, started_at) VALUES (?, ?, ?, ?)",
            (run_id, job_id, status, "2024-01-01T00:00:00Z"),
        )
        await db.commit()
    return run_id


async def _setup_db_with_restore_run(db_path: str, status: str) -> str:
    """Create schema and insert one restore_run. Returns the restore_run id."""
    from app.core.database import init_db

    await init_db(db_path)
    restore_run_id = "restore-run-1"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO restore_runs
               (id, target_id, snapshot_id, status, restore_to, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                restore_run_id,
                "target-test-1",
                "snap-test-1",
                status,
                "/tmp/restore-target",
                "2024-01-01T00:00:00Z",
            ),
        )
        await db.commit()
    return restore_run_id


def _do_stale_cleanup(db):
    """Mirror the cleanup logic from main.py step 7 using correct column names."""
    return db.execute(
        "UPDATE job_runs SET status = 'failed', "
        "error_message = 'Interrupted by server restart', "
        "completed_at = ? WHERE status = 'running'",
        (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )


def _do_stale_restore_cleanup(db):
    """Mirror the restore cleanup logic from main.py."""
    return db.execute(
        "UPDATE restore_runs SET status = 'failed', "
        "error_message = 'Interrupted by server restart', "
        "completed_at = ? WHERE status = 'running'",
        (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )


# ---------------------------------------------------------------------------
# Body size limit middleware tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_size_limit_rejects_large_payload(startup_client):
    """Middleware returns 413 when Content-Length exceeds 1MB."""
    from app.main import MAX_BODY_SIZE

    headers = {"content-length": str(MAX_BODY_SIZE + 1)}
    response = await startup_client.post("/api/v1/jobs", headers=headers, content=b"x")
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"
    assert response.json()["message"] == "Request body too large"


@pytest.mark.asyncio
async def test_body_size_limit_allows_normal_payload(startup_client):
    """Middleware passes through requests within the size limit."""
    headers = {"content-length": "100"}
    response = await startup_client.post("/api/v1/jobs", headers=headers, content=b"x" * 100)
    # Should NOT be 413; any other status is fine (401, 422, etc.)
    assert response.status_code != 413


@pytest.mark.asyncio
async def test_body_size_limit_rejects_invalid_content_length(startup_client):
    """Middleware returns 400 when Content-Length is non-numeric."""
    headers = {"content-length": "notanumber"}
    response = await startup_client.post("/api/v1/jobs", headers=headers, content=b"x")
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_stale_cleanup_sql_matches_production():
    """Regression guard: main.py startup SQL must use the correct column names."""
    import inspect

    from app import main as main_mod

    source = inspect.getsource(main_mod.lifespan)
    assert "status = 'failed'" in source
    assert "error_message" in source
    assert "completed_at" in source
    assert "WHERE status = 'running'" in source
    assert "UPDATE restore_runs SET status = 'failed'" in source
    assert "cleanup_stale_backup_lock(config.config_dir)" in source


@pytest.mark.asyncio
async def test_body_size_limit_allows_request_without_content_length(startup_client):
    """Middleware does not reject requests that omit Content-Length."""
    response = await startup_client.get("/api/v1/health")
    assert response.status_code != 413


# ---------------------------------------------------------------------------
# Stale job_run cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cleanup_marks_running_as_failed(tmp_path):
    """Running job_runs are set to 'failed' on startup."""
    db_path = str(tmp_path / "test.db")
    run_id = await _setup_db_with_job_run(db_path, "running")

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT id FROM job_runs WHERE status = 'running'")
        stale_runs = await cursor.fetchall()
        assert len(stale_runs) == 1
        await _do_stale_cleanup(db)
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT status FROM job_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()

    assert row[0] == "failed"


@pytest.mark.asyncio
async def test_stale_cleanup_sets_correct_error_message(tmp_path):
    """Cleaned-up job_runs have the expected error_message."""
    db_path = str(tmp_path / "test.db")
    run_id = await _setup_db_with_job_run(db_path, "running")

    async with aiosqlite.connect(db_path) as db:
        await _do_stale_cleanup(db)
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT error_message, completed_at FROM job_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()

    assert row[0] == "Interrupted by server restart"
    assert row[1] is not None  # completed_at was set


@pytest.mark.asyncio
async def test_stale_cleanup_noop_when_no_running_jobs(tmp_path):
    """Cleanup is a no-op when no running job_runs exist."""
    db_path = str(tmp_path / "test.db")
    run_id = await _setup_db_with_job_run(db_path, "completed")

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT id FROM job_runs WHERE status = 'running'")
        stale_runs = await cursor.fetchall()
        assert len(stale_runs) == 0
        # Run cleanup anyway — should touch nothing
        await _do_stale_cleanup(db)
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT status FROM job_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()

    assert row[0] == "completed"


@pytest.mark.asyncio
async def test_stale_cleanup_sets_ended_at_timestamp(tmp_path):
    """completed_at is populated with an ISO-8601 UTC timestamp."""
    db_path = str(tmp_path / "test.db")
    run_id = await _setup_db_with_job_run(db_path, "running")

    before = datetime.now(UTC).replace(microsecond=0)

    async with aiosqlite.connect(db_path) as db:
        await _do_stale_cleanup(db)
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT completed_at FROM job_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()

    completed_at_str = row[0]
    assert completed_at_str is not None
    # Must parse as valid ISO-8601 UTC
    completed_at = datetime.strptime(completed_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert completed_at >= before


@pytest.mark.asyncio
async def test_restore_cleanup_marks_running_as_failed(tmp_path):
    """Running restore_runs are set to 'failed' on startup."""
    db_path = str(tmp_path / "test.db")
    run_id = await _setup_db_with_restore_run(db_path, "running")

    async with aiosqlite.connect(db_path) as db:
        await _do_stale_restore_cleanup(db)
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT status, error_message, completed_at FROM restore_runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()

    assert row[0] == "failed"
    assert row[1] == "Interrupted by server restart"
    assert row[2] is not None


def test_cleanup_stale_backup_lock_removes_dead_process_lock(tmp_path):
    """Startup/manual-run cleanup removes backup.lock from a dead process."""
    from app.services.orchestrator import cleanup_stale_backup_lock

    lock_file = tmp_path / "backup.lock"
    lock_file.write_text('{"pid": 999999, "proc_start_time": "12345", "run_id": "run-dead-1"}')

    removed = cleanup_stale_backup_lock(tmp_path)

    assert removed is True
    assert not lock_file.exists()


def test_cleanup_stale_backup_lock_keeps_live_process_lock(tmp_path):
    """Cleanup leaves a live-process backup lock in place."""
    import os

    from app.services.orchestrator import _get_proc_start_time, cleanup_stale_backup_lock

    pid = os.getpid()
    proc_start_time = _get_proc_start_time(pid)
    assert proc_start_time is not None

    lock_file = tmp_path / "backup.lock"
    lock_file.write_text(f'{{"pid": {pid}, "proc_start_time": "{proc_start_time}", "run_id": "run-live-1"}}')

    removed = cleanup_stale_backup_lock(tmp_path)

    assert removed is False
    assert lock_file.exists()


def test_unraid_runtime_warning_when_not_root(caplog):
    """Unraid should warn when Arkive is not running as root."""
    from app.core.platform import Platform
    from app.main import _warn_unraid_runtime_permissions

    with patch("app.main.os.geteuid", return_value=99), patch("app.main.os.getegid", return_value=100):
        with caplog.at_level("WARNING", logger="arkive.main"):
            _warn_unraid_runtime_permissions(Platform.UNRAID)

    assert "Use --user 0:0 for full coverage" in caplog.text


def test_unraid_runtime_warning_skipped_for_root(caplog):
    """Root runtime on Unraid should not emit the permissions warning."""
    from app.core.platform import Platform
    from app.main import _warn_unraid_runtime_permissions

    with patch("app.main.os.geteuid", return_value=0):
        with caplog.at_level("WARNING", logger="arkive.main"):
            _warn_unraid_runtime_permissions(Platform.UNRAID)

    assert "full coverage" not in caplog.text
