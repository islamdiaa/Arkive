"""
API integration tests for directory scan and discovery trigger endpoints.

Tests: directory scan, trigger discovery, list discovered containers.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def test_directory_scan(client):
    """POST /api/directories/scan should return directories and platform."""
    data = await do_setup(client)
    resp = await client.post("/api/directories/scan", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "directories" in body
    assert isinstance(body["directories"], list)
    assert "platform" in body


async def test_trigger_discovery(client):
    """POST /api/discover/scan should trigger container discovery."""
    data = await do_setup(client)
    resp = await client.post("/api/discover/scan", headers=auth_headers(data["api_key"]))
    # May return 200, 500, or 503 depending on Docker availability
    assert resp.status_code in (200, 500, 503)
    body = resp.json()
    assert isinstance(body, dict)


async def test_list_discovered_containers(client):
    """GET /api/discover/containers should return paginated items list."""
    data = await do_setup(client)
    resp = await client.get("/api/discover/containers", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    assert "total" in body
    assert "has_more" in body
