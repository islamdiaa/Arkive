"""
API integration tests for database management endpoints.

Tests: list databases, list with data, dump not found.
"""

import json

import aiosqlite
import pytest

from app.core.dependencies import get_config
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def _seed_container_with_db(client):
    """Run setup and insert a discovered container with databases into the DB."""
    data = await do_setup(client)
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        databases_json = json.dumps(
            [
                {
                    "type": "mysql",
                    "name": "mydb",
                    "db_name": "mydb",
                    "db_type": "mysql",
                    "source": "env",
                    "host_path": "/data/mysql",
                },
            ]
        )
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
    resp = await client.get("/api/databases", headers=auth_headers(data["api_key"]))
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
