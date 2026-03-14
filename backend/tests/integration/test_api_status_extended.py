"""
Extended integration tests for the /api/status endpoint.

Tests:
- Response includes `next_backup` field
- `next_backup` is None when no scheduler or no jobs
- `next_backup` returns an ISO string when scheduler has jobs
"""

from unittest.mock import MagicMock

import aiosqlite
import pytest

from tests.conftest import do_setup

pytestmark = pytest.mark.asyncio


async def test_status_includes_next_backup_field(client):
    """GET /api/status always includes `next_backup` key in response."""
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "next_backup" in body


async def test_next_backup_is_none_when_no_scheduler(client):
    """next_backup is None when app.state.scheduler is None (test default)."""
    # The conftest fixture sets scheduler = None
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    assert resp.json()["next_backup"] is None


async def test_next_backup_is_none_when_no_jobs(client):
    """next_backup is None when scheduler exists but has no jobs."""
    mock_scheduler = MagicMock()
    mock_scheduler.get_all_next_runs.return_value = {}

    # Inject the mock scheduler into app state
    transport = client._transport
    app = transport.app
    app.state.scheduler = mock_scheduler

    try:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["next_backup"] is None
    finally:
        app.state.scheduler = None


async def test_next_backup_returns_soonest_iso_string(client):
    """next_backup returns the soonest ISO timestamp when scheduler has jobs."""
    mock_scheduler = MagicMock()
    mock_scheduler.get_all_next_runs.return_value = {
        "job1": "2026-03-02T06:00:00",
        "job2": "2026-03-01T18:00:00",  # soonest
        "job3": "2026-03-03T07:00:00",
    }

    transport = client._transport
    app = transport.app
    app.state.scheduler = mock_scheduler

    try:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["next_backup"] == "2026-03-01T18:00:00"
    finally:
        app.state.scheduler = None


async def test_next_backup_handles_scheduler_exception(client):
    """next_backup is None if scheduler.get_all_next_runs() raises."""
    mock_scheduler = MagicMock()
    mock_scheduler.get_all_next_runs.side_effect = RuntimeError("scheduler error")

    transport = client._transport
    app = transport.app
    app.state.scheduler = mock_scheduler

    try:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["next_backup"] is None
    finally:
        app.state.scheduler = None


async def test_status_platform_falls_back_to_live_runtime_when_setting_missing(client):
    """Status should report live runtime platform even if settings.platform is absent."""
    transport = client._transport
    app = transport.app

    async with aiosqlite.connect(app.state.config.db_path) as db:
        await db.execute("DELETE FROM settings WHERE key = 'platform'")
        await db.commit()

    resp = await client.get("/api/status")
    assert resp.status_code == 200
    expected_platform = app.state.platform if isinstance(app.state.platform, str) else app.state.platform.value
    assert resp.json()["platform"] == expected_platform


async def test_status_marks_unraid_migration_ready_when_appdata_and_flash_are_covered(client):
    """Coverage should become migration-ready when Unraid appdata and flash are protected."""
    import aiosqlite

    await do_setup(client, directories=["/mnt/user/appdata"])
    transport = client._transport
    app = transport.app
    app.state.platform = "unraid"

    async with aiosqlite.connect(app.state.config.db_path) as db:
        # Coverage now requires a successful backup after the watched directory
        # configuration exists, so pin the directory timestamp before the fake run.
        await db.execute(
            "UPDATE watched_directories SET created_at = ? WHERE path = ?",
            ("2026-03-08T00:00:00Z", "/mnt/user/appdata"),
        )
        await db.execute(
            """INSERT INTO backup_jobs
               (id, name, type, schedule, enabled, targets, directories, exclude_patterns,
                include_databases, include_flash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "flash-job",
                "Flash Backup",
                "flash",
                "0 0 * * *",
                1,
                "[]",
                "[]",
                "[]",
                0,
                1,
                "2026-03-08T00:00:00Z",
                "2026-03-08T00:00:00Z",
            ),
        )
        await db.execute(
            """INSERT INTO job_runs
               (id, job_id, status, trigger, started_at, completed_at, flash_backed_up, flash_size_bytes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "flash-run",
                "flash-job",
                "success",
                "manual",
                "2026-03-08T00:00:00Z",
                "2026-03-08T00:02:00Z",
                1,
                1234,
            ),
        )
        await db.execute(
            """INSERT INTO backup_jobs
               (id, name, type, schedule, enabled, targets, directories, exclude_patterns,
                include_databases, include_flash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "dir-job",
                "Cloud Sync",
                "full",
                "0 1 * * *",
                1,
                "[]",
                "[]",
                "[]",
                0,
                0,
                "2026-03-08T00:00:00Z",
                "2026-03-08T00:00:00Z",
            ),
        )
        await db.execute(
            """INSERT INTO job_runs
               (id, job_id, status, trigger, started_at, completed_at, total_size_bytes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "dir-run",
                "dir-job",
                "success",
                "manual",
                "2026-03-12T00:00:00Z",
                "2026-03-12T00:03:00Z",
                1024,
            ),
        )
        await db.commit()

    resp = await client.get("/api/status")
    assert resp.status_code == 200
    coverage = resp.json()["coverage"]
    assert coverage["appdata_protected"] is True
    assert coverage["flash_protected"] is True
    assert coverage["migration_ready"] is True
    assert coverage["warnings"] == []
