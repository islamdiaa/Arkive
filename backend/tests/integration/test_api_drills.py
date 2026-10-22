"""Integration tests for the restore drill API and the drill fields in /api/status."""

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.services.restore_drill import DrillAlreadyRunningError
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


def _app(client):
    return client._transport.app


async def _seed(client):
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, config) VALUES ('t1', 'Local NAS', 'local', '{}')"
        )
        await db.execute(
            """INSERT INTO restore_drills (id, trigger, status, target_id, snapshot_id, started_at,
                   completed_at, duration_seconds, databases_tested, databases_passed, databases_failed)
               VALUES ('d-old', 'scheduled', 'passed', 't1', 'aaaaaaaa', '2026-10-11T07:00:00Z',
                       '2026-10-11T07:02:00Z', 120, 2, 2, 0)"""
        )
        await db.execute(
            """INSERT INTO restore_drills (id, trigger, status, target_id, snapshot_id, started_at,
                   completed_at, duration_seconds, databases_tested, databases_passed, databases_failed,
                   error_message)
               VALUES ('d-new', 'manual', 'failed', 't1', 'bbbbbbbb', '2026-10-18T07:00:00Z',
                       '2026-10-18T07:03:00Z', 180, 2, 1, 1, '1 database(s) did not restore: pg/appdb')"""
        )
        await db.executemany(
            """INSERT INTO restore_drill_results (drill_id, container_name, db_type, db_name, dump_file,
                   dump_size_bytes, status, detail, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("d-new", "pg", "postgres", "appdb", "pg_appdb_20261018_030000.sql", 4096, "failed",
                 "psql load exited with code 3: ERROR: extension missing", 12.5),
                ("d-new", "vaultwarden", "sqlite", "db.sqlite3", "vaultwarden_db.sqlite3_20261018_030000.sqlite3",
                 2048, "passed", "integrity_check ok; 9 tables", 0.4),
            ],
        )
        await db.commit()


async def test_requires_auth(client):
    await do_setup(client)
    assert (await client.get("/api/drills")).status_code == 401
    assert (await client.post("/api/drills/run")).status_code == 401


async def test_list_empty(client):
    data = await do_setup(client)
    resp = await client.get("/api/drills", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


async def test_list_and_detail(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    await _seed(client)

    resp = await client.get("/api/drills", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [d["id"] for d in body["items"]] == ["d-new", "d-old"]
    newest = body["items"][0]
    assert newest["target_name"] == "Local NAS"
    assert set(newest) == {
        "id", "trigger", "status", "target_id", "target_name", "snapshot_id", "started_at",
        "completed_at", "duration_seconds", "databases_tested", "databases_passed",
        "databases_failed", "error_message",
    }
    assert "results" not in newest

    paged = (await client.get("/api/drills?limit=1&offset=1", headers=headers)).json()
    assert paged["total"] == 2 and [d["id"] for d in paged["items"]] == ["d-old"]

    detail = (await client.get("/api/drills/d-new", headers=headers)).json()
    assert detail["status"] == "failed" and detail["target_name"] == "Local NAS"
    assert [r["status"] for r in detail["results"]] == ["failed", "passed"]
    assert set(detail["results"][0]) == {
        "container_name", "db_type", "db_name", "dump_file", "dump_size_bytes", "status",
        "detail", "duration_seconds",
    }

    assert (await client.get("/api/drills/nope", headers=headers)).status_code == 404


async def test_run_without_engine_is_503(client):
    data = await do_setup(client)
    resp = await client.post("/api/drills/run", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 503


async def test_run_starts_drill(client):
    data = await do_setup(client)
    engine = MagicMock()
    engine.docker_client = object()
    engine.is_running = MagicMock(return_value=False)
    engine.start_drill = AsyncMock(return_value="abc123")
    _app(client).state.restore_drill = engine
    try:
        resp = await client.post("/api/drills/run", headers=auth_headers(data["api_key"]))
        assert resp.status_code == 202
        assert resp.json() == {"drill_id": "abc123", "status": "running"}
        engine.start_drill.assert_awaited_once_with(trigger="manual")

        engine.is_running.return_value = True
        resp = await client.post("/api/drills/run", headers=auth_headers(data["api_key"]))
        assert resp.status_code == 409

        engine.is_running.return_value = False
        engine.start_drill = AsyncMock(side_effect=DrillAlreadyRunningError("busy"))
        resp = await client.post("/api/drills/run", headers=auth_headers(data["api_key"]))
        assert resp.status_code == 409
    finally:
        _app(client).state.restore_drill = None


async def test_run_without_docker_is_503(client):
    data = await do_setup(client)
    engine = MagicMock()
    engine.docker_client = None
    _app(client).state.restore_drill = engine
    try:
        resp = await client.post("/api/drills/run", headers=auth_headers(data["api_key"]))
        assert resp.status_code == 503
    finally:
        _app(client).state.restore_drill = None


async def test_status_reports_last_drill_and_degrades_on_failure(client, monkeypatch):
    import app.api.status as status_mod

    monkeypatch.setattr(status_mod, "_check_binaries", lambda: {"ok": True, "message": "ok"})
    monkeypatch.setattr(status_mod, "_check_scheduler_health", lambda request: {"ok": True, "message": "running"})
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])

    body = (await client.get("/api/status", headers=headers)).json()
    assert body["last_drill"] is None
    baseline = body["status"]
    assert baseline in ("ok", "degraded", "error")

    await _seed(client)
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        # Target reachable so only the drill can degrade the status.
        await db.execute("UPDATE storage_targets SET status = 'online'")
        # A drill in progress doesn't hide the last finished one.
        await db.execute("INSERT INTO restore_drills (id, status, started_at) "
                         "VALUES ('d-running', 'running', '2026-10-25T07:00:00Z')")
        await db.commit()

    body = (await client.get("/api/status", headers=headers)).json()
    assert body["last_drill"] == {
        "id": "d-new", "status": "failed", "started_at": "2026-10-18T07:00:00Z",
        "completed_at": "2026-10-18T07:03:00Z", "databases_tested": 2,
        "databases_passed": 1, "databases_failed": 1,
    }
    assert body["status"] == ("degraded" if baseline == "ok" else baseline)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE restore_drills SET status = 'passed' WHERE id = 'd-new'")
        await db.commit()
    body = (await client.get("/api/status", headers=headers)).json()
    assert body["last_drill"]["status"] == "passed"
    assert body["status"] == baseline

    # Unauthenticated callers get the minimal payload without drill details.
    minimal = (await client.get("/api/status")).json()
    assert "last_drill" not in minimal
