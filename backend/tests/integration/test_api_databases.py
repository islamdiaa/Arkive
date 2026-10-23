"""
API integration tests for database management endpoints.

Tests: list databases, list with data, dump not found.
"""
import json

import pytest
import aiosqlite

from tests.conftest import do_setup, auth_headers
from app.core.dependencies import get_config

pytestmark = pytest.mark.asyncio


async def _seed_container_with_db(client):
    """Run setup and insert a discovered container with databases into the DB."""
    data = await do_setup(client)
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        databases_json = json.dumps([
            {"type": "mysql", "name": "mydb", "db_name": "mydb", "db_type": "mysql",
             "source": "env", "host_path": "/data/mysql"},
        ])
        await db.execute(
            """INSERT INTO discovered_containers (name, image, status, databases, profile, priority)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("testcontainer", "mysql:8", "running", databases_json, "mysql", "high"),
        )
        await db.commit()
    return data["api_key"]


async def test_list_databases_empty(client):
    """GET /api/databases with no containers should return empty list."""
    data = await do_setup(client)
    resp = await client.get(
        "/api/databases", headers=auth_headers(data["api_key"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_list_databases_with_data(client):
    """GET /api/databases with seeded container should return database entries."""
    api_key = await _seed_container_with_db(client)
    resp = await client.get("/api/databases", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    item = body["items"][0]
    assert item["container_name"] == "testcontainer"
    assert item["type"] == "mysql"
    assert item["name"] == "mydb"


async def test_dump_database_not_found(client):
    """POST /api/databases/nonexistent/none/dump should return 404."""
    data = await do_setup(client)
    resp = await client.post(
        "/api/databases/nonexistent/none/dump",
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 404


async def test_manual_dump_waits_for_no_backup(client):
    """A manual dump while a backup holds the lock is refused (it would race
    the backup's dump-directory cleanup); otherwise it runs and releases."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.lock_manager import BackupLockManager

    api_key = await _seed_container_with_db(client)
    app = client._transport.app
    lock_manager = BackupLockManager(get_config().config_dir)
    app.state.orchestrator = SimpleNamespace(lock_manager=lock_manager)
    dumper = SimpleNamespace(dump_single=AsyncMock(return_value=SimpleNamespace(
        container_name="testcontainer", db_name="mydb", db_type="mysql", dump_size_bytes=10,
        integrity_check="ok", dump_path="/config/dumps/x.sql", duration_seconds=0.1,
        status="success", error=None)))
    app.state.db_dumper = dumper

    assert lock_manager.acquire_backup_lock("scheduled-run")
    resp = await client.post("/api/databases/testcontainer/mydb/dump", headers=auth_headers(api_key))
    assert resp.status_code == 409
    dumper.dump_single.assert_not_awaited()
    lock_manager.release_backup_lock()

    resp = await client.post("/api/databases/testcontainer/mydb/dump", headers=auth_headers(api_key))
    assert resp.status_code == 200 and resp.json()["status"] == "success"
    assert not lock_manager.is_backup_running()
