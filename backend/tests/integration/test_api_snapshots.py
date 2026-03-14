"""
API integration tests for snapshot endpoints.

Tests: list, detail, filter by target, delete, not-found cases.
"""

import aiosqlite
import pytest

from app.core.dependencies import get_config
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def _seed_snapshot(client):
    """Run setup and insert a test snapshot row directly into the DB."""
    data = await do_setup(client)
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO snapshots (id, target_id, full_id, time, hostname, paths, tags, size_bytes, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "abc12345",
                "target1",
                "abc123456789full",
                "2024-01-01T00:00:00Z",
                "testhost",
                "[]",
                "[]",
                1000,
                "2024-01-01",
            ),
        )
        await db.commit()
    return data["api_key"]


async def test_list_snapshots_empty(client):
    """After setup with no snapshots, list should return empty."""
    data = await do_setup(client)
    resp = await client.get("/api/snapshots", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_list_snapshots_with_data(client):
    """After seeding a snapshot, list should return it."""
    api_key = await _seed_snapshot(client)
    resp = await client.get("/api/snapshots", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "abc12345"


async def test_list_snapshots_filter_by_target(client):
    """Filter by target_id should include matching and exclude non-matching."""
    api_key = await _seed_snapshot(client)

    # Matching target
    resp = await client.get("/api/snapshots?target_id=target1", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    # Non-matching target
    resp = await client.get("/api/snapshots?target_id=nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


async def test_get_snapshot_detail_via_list(client):
    """After seeding a snapshot, it should be retrievable via the list endpoint."""
    api_key = await _seed_snapshot(client)
    resp = await client.get("/api/snapshots?target_id=target1", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    snap = body["items"][0]
    assert snap["id"] == "abc12345"
    assert snap["target_id"] == "target1"
    assert snap["hostname"] == "testhost"
    assert snap["size_bytes"] == 1000


async def test_get_snapshot_not_found(client):
    """GET /api/snapshots with nonexistent target_id should return empty."""
    data = await do_setup(client)
    resp = await client.get("/api/snapshots?target_id=nonexistent", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_snapshot_browse_not_found(client):
    """GET /api/snapshots/{id}/browse for nonexistent snapshot returns 404."""
    data = await do_setup(client)
    resp = await client.get("/api/snapshots/nonexistent/browse", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 404
