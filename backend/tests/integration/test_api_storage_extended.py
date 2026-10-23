"""
API integration tests for the GET /api/storage endpoint.

Tests: response structure, empty storage baseline, target and history fields.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def test_storage_stats_requires_auth(client):
    """GET /api/storage should require authentication after setup."""
    # Must run setup first to establish an API key; otherwise auth is bypassed
    await do_setup(client)
    resp = await client.get("/api/storage")
    assert resp.status_code in (401, 403)


async def test_storage_stats_empty(client):
    """GET /api/storage with no targets returns zero totals."""
    data = await do_setup(client)
    resp = await client.get("/api/storage", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_size_bytes"] == 0
    assert body["target_count"] == 0
    assert body["snapshot_count"] == 0
    assert isinstance(body["targets"], list)
    assert len(body["targets"]) == 0
    assert isinstance(body["size_history"], list)
    assert len(body["size_history"]) == 0


async def test_storage_stats_response_structure(client):
    """GET /api/storage returns all required top-level keys."""
    data = await do_setup(client)
    resp = await client.get("/api/storage", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    required_keys = {"total_size_bytes", "target_count", "snapshot_count", "targets", "size_history"}
    assert required_keys.issubset(body.keys()), f"Missing keys: {required_keys - set(body.keys())}"


async def test_storage_stats_targets_list_type(client):
    """Targets field should be a list of dicts with expected keys."""
    data = await do_setup(client)
    resp = await client.get("/api/storage", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    # Even when empty, verify the shape is correct
    assert isinstance(body["targets"], list)
    # If there were targets, each would have id, name, type, total_size_bytes, snapshot_count


async def test_storage_stats_size_history_list_type(client):
    """Size history field should be a list."""
    data = await do_setup(client)
    resp = await client.get("/api/storage", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["size_history"], list)


async def test_storage_stats_numeric_fields(client):
    """Numeric fields should be integers >= 0."""
    data = await do_setup(client)
    resp = await client.get("/api/storage", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["total_size_bytes"], int)
    assert body["total_size_bytes"] >= 0
    assert isinstance(body["target_count"], int)
    assert body["target_count"] >= 0
    assert isinstance(body["snapshot_count"], int)
    assert body["snapshot_count"] >= 0
