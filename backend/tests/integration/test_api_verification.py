"""Integration tests for the Verification API endpoints."""

import os
import uuid

import aiosqlite
import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def _create_target(client, api_key, tmp_path, name="TestTarget"):
    """Helper: create a local storage target and return its ID."""
    target_path = str(tmp_path / f"backups-{uuid.uuid4().hex[:6]}")
    os.makedirs(target_path, exist_ok=True)
    resp = await client.post(
        "/api/targets",
        json={"name": name, "type": "local", "config": {"path": target_path}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _get_db_path(client):
    """Extract DB path from the test client's app config."""
    transport = client._transport
    app = transport.app
    return str(app.state.config.db_path)


# ---------------------------------------------------------------------------
# GET /api/verification
# ---------------------------------------------------------------------------


async def test_verification_get_empty(client):
    """GET /api/verification returns empty results when no runs exist."""
    data = await do_setup(client)
    resp = await client.get(
        "/api/verification", headers=auth_headers(data["api_key"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trust_score"] == 0
    assert body["results"] == []


async def test_verification_get_with_completed_run(client, tmp_path):
    """GET /api/verification returns latest results per target."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score,
                files_checked, files_passed, databases_checked, databases_passed,
                restic_check_passed)
               VALUES (?, ?, '2026-03-01T00:00:00Z', '2026-03-01T00:05:00Z',
                       'passed', 85, 100, 85, 3, 3, 1)""",
            ("vr-1", target_id),
        )
        await db.commit()

    resp = await client.get(
        "/api/verification", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trust_score"] == 85
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["target_id"] == target_id
    assert result["status"] == "passed"
    assert result["files_checked"] == 100
    assert result["files_passed"] == 85


async def test_verification_get_latest_per_target(client, tmp_path):
    """GET /api/verification returns only the latest run per target."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        # Older run
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score)
               VALUES (?, ?, '2026-03-01T00:00:00Z', '2026-03-01T00:05:00Z',
                       'failed', 30)""",
            ("vr-old", target_id),
        )
        # Newer run
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score)
               VALUES (?, ?, '2026-03-02T00:00:00Z', '2026-03-02T00:05:00Z',
                       'passed', 95)""",
            ("vr-new", target_id),
        )
        await db.commit()

    resp = await client.get(
        "/api/verification", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trust_score"] == 95
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == "vr-new"


async def test_verification_get_multi_target_average(client, tmp_path):
    """Trust score is the average of per-target latest scores."""
    data = await do_setup(client)
    api_key = data["api_key"]
    t1 = await _create_target(client, api_key, tmp_path, name="Target1")
    t2 = await _create_target(client, api_key, tmp_path, name="Target2")

    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score)
               VALUES (?, ?, '2026-03-01T00:00:00Z', '2026-03-01T00:05:00Z',
                       'passed', 80)""",
            ("vr-t1", t1),
        )
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score)
               VALUES (?, ?, '2026-03-01T00:00:00Z', '2026-03-01T00:05:00Z',
                       'passed', 100)""",
            ("vr-t2", t2),
        )
        await db.commit()

    resp = await client.get(
        "/api/verification", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trust_score"] == 90  # average of 80 and 100


# ---------------------------------------------------------------------------
# POST /api/verification/run
# ---------------------------------------------------------------------------


async def test_verification_run_no_targets(client):
    """POST /api/verification/run with no enabled targets returns 404."""
    data = await do_setup(client)
    resp = await client.post(
        "/api/verification/run", headers=auth_headers(data["api_key"])
    )
    assert resp.status_code == 404


async def test_verification_run_creates_runs(client, tmp_path):
    """POST /api/verification/run creates verification_runs entries."""
    data = await do_setup(client)
    api_key = data["api_key"]
    t1 = await _create_target(client, api_key, tmp_path, name="Target1")
    t2 = await _create_target(client, api_key, tmp_path, name="Target2")

    resp = await client.post(
        "/api/verification/run", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["target_count"] == 2
    assert len(body["run_ids"]) == 2

    # Verify rows exist in database
    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM verification_runs ORDER BY target_id"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 2
        for row in rows:
            assert dict(row)["status"] == "running"
            assert dict(row)["target_id"] in (t1, t2)


async def test_verification_run_creates_activity_log(client, tmp_path):
    """POST /api/verification/run creates activity_log entries."""
    data = await do_setup(client)
    api_key = data["api_key"]
    await _create_target(client, api_key, tmp_path)

    resp = await client.post(
        "/api/verification/run", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200

    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM activity_log WHERE type = 'verification'"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        assert dict(rows[0])["action"] == "started"


# ---------------------------------------------------------------------------
# GET /api/verification requires auth
# ---------------------------------------------------------------------------


async def test_verification_requires_auth(client):
    """Verification endpoints require auth when setup is complete."""
    await do_setup(client)
    resp = await client.get("/api/verification")
    assert resp.status_code == 401


async def test_verification_run_requires_auth(client):
    """POST /api/verification/run requires auth when setup is complete."""
    await do_setup(client)
    resp = await client.post("/api/verification/run")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Status endpoint includes verification fields
# ---------------------------------------------------------------------------


async def test_status_includes_verification_fields(client):
    """GET /api/status response includes trust_score, last_verified_at, verification_status."""
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "trust_score" in body
    assert "last_verified_at" in body
    assert "verification_status" in body
    assert body["trust_score"] == 0
    assert body["verification_status"] is None


async def test_status_reflects_verification_results(client, tmp_path):
    """GET /api/status reflects latest verification data."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score)
               VALUES (?, ?, '2026-03-15T10:00:00Z', '2026-03-15T10:05:00Z',
                       'passed', 92)""",
            ("vr-status", target_id),
        )
        await db.commit()

    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trust_score"] == 92
    assert body["last_verified_at"] == "2026-03-15T10:05:00Z"
    assert body["verification_status"] is not None
    assert body["verification_status"]["trust_score"] == 92
    assert body["verification_status"]["last_verified_at"] == "2026-03-15T10:05:00Z"
    assert body["verification_status"]["verification_passing"] is True


# ---------------------------------------------------------------------------
# Schema migration test
# ---------------------------------------------------------------------------


async def test_verification_runs_table_exists(client):
    """The verification_runs table should exist after DB init."""
    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='verification_runs'"
        )
        row = await cursor.fetchone()
    assert row is not None, "verification_runs table not found"


async def test_verification_running_excluded_from_trust_score(client, tmp_path):
    """Running verification runs are excluded from trust score computation."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    db_path = await _get_db_path(client)
    async with aiosqlite.connect(db_path) as db:
        # Only a running run exists - trust score should stay 0
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, status, trust_score)
               VALUES (?, ?, '2026-03-15T10:00:00Z', 'running', 0)""",
            ("vr-running", target_id),
        )
        await db.commit()

    resp = await client.get(
        "/api/verification", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trust_score"] == 0
