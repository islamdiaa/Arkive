"""
API integration tests for the discover endpoints.

Tests: POST /api/discover/scan, GET /api/discover/containers,
       GET /api/discover/databases.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Auth guard tests
# ---------------------------------------------------------------------------


async def test_discover_scan_requires_auth(client):
    """POST /api/discover/scan should require authentication after setup."""
    await do_setup(client)
    resp = await client.post("/api/discover/scan")
    assert resp.status_code in (401, 403)


async def test_discover_containers_requires_auth(client):
    """GET /api/discover/containers should require authentication after setup."""
    await do_setup(client)
    resp = await client.get("/api/discover/containers")
    assert resp.status_code in (401, 403)


async def test_discover_databases_requires_auth(client):
    """GET /api/discover/databases should require authentication after setup."""
    await do_setup(client)
    resp = await client.get("/api/discover/databases")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /api/discover/scan
# ---------------------------------------------------------------------------


async def test_discover_scan_no_docker(client):
    """POST /api/discover/scan returns 503 when Docker is unavailable."""
    data = await do_setup(client)
    resp = await client.post("/api/discover/scan", headers=auth_headers(data["api_key"]))
    # Discovery is None in test fixtures so should get 503
    assert resp.status_code == 503
    body = resp.json()
    assert isinstance(body, dict)
    # Custom exception handler returns {"error": ..., "message": ..., "details": {}}
    assert "message" in body
    assert "error" in body


# ---------------------------------------------------------------------------
# GET /api/discover/containers
# ---------------------------------------------------------------------------


async def test_discover_containers_empty(client):
    """GET /api/discover/containers returns empty paginated list when none discovered."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/containers", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 0
    assert body["total"] == 0
    assert "has_more" in body


async def test_discover_containers_has_legacy_key(client):
    """GET /api/discover/containers response includes 'containers' legacy alias."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/containers", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "containers" in body
    assert body["items"] == body["containers"]


async def test_discover_containers_has_pagination(client):
    """GET /api/discover/containers response uses standard pagination."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/containers", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert "has_more" in body


# ---------------------------------------------------------------------------
# GET /api/discover/databases
# ---------------------------------------------------------------------------


async def test_discover_databases_empty(client):
    """GET /api/discover/databases returns empty paginated list when none discovered."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/databases", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 0
    assert body["total"] == 0
    assert "has_more" in body


async def test_discover_databases_has_legacy_key(client):
    """GET /api/discover/databases response includes 'databases' legacy alias."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/databases", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "databases" in body
    assert body["items"] == body["databases"]


async def test_discover_databases_has_items_key(client):
    """GET /api/discover/databases response includes standard 'items' key."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/databases", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_discover_databases_response_structure(client):
    """GET /api/discover/databases returns standard paginated response keys."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/databases", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    required_keys = {"items", "total", "limit", "offset", "has_more"}
    assert required_keys.issubset(body.keys())
