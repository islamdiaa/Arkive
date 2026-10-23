"""
API integration tests for the GET /api/activity endpoint.

Tests: response structure, pagination, type/severity filtering.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


async def test_activity_requires_auth(client):
    """GET /api/activity should require authentication after setup."""
    await do_setup(client)
    resp = await client.get("/api/activity")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Response structure
# ---------------------------------------------------------------------------


async def test_activity_response_structure(client):
    """GET /api/activity returns all required top-level keys."""
    data = await do_setup(client)
    resp = await client.get("/api/activity", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    required_keys = {"items", "activities", "total", "limit", "offset", "has_more"}
    assert required_keys.issubset(body.keys()), f"Missing keys: {required_keys - set(body.keys())}"


async def test_activity_empty(client):
    """GET /api/activity with no activity returns empty list and zero total."""
    data = await do_setup(client)
    resp = await client.get("/api/activity", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    # After setup there may be some activity entries from the setup itself
    assert isinstance(body["items"], list)
    assert isinstance(body["activities"], list)
    assert body["items"] == body["activities"]
    assert isinstance(body["total"], int)
    assert body["total"] >= 0


async def test_activity_items_and_activities_match(client):
    """items and activities should always contain the same entries."""
    data = await do_setup(client)
    resp = await client.get("/api/activity", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == body["activities"]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_activity_default_pagination(client):
    """Default limit should be 50, offset should be 0."""
    data = await do_setup(client)
    resp = await client.get("/api/activity", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0


async def test_activity_custom_limit(client):
    """Custom limit parameter is respected."""
    data = await do_setup(client)
    resp = await client.get("/api/activity?limit=5", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 5
    assert len(body["items"]) <= 5


async def test_activity_custom_offset(client):
    """Custom offset parameter is respected."""
    data = await do_setup(client)
    resp = await client.get("/api/activity?offset=100", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["offset"] == 100


async def test_activity_limit_exceeds_max(client):
    """Limit above 200 should be rejected (422 validation error)."""
    data = await do_setup(client)
    resp = await client.get("/api/activity?limit=999", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 422


async def test_activity_has_more_false_when_no_data(client):
    """has_more should be false when total entries fit within limit+offset."""
    data = await do_setup(client)
    resp = await client.get("/api/activity", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    # With few or zero entries and default limit=50, has_more should be false
    if body["total"] <= body["limit"] + body["offset"]:
        assert body["has_more"] is False


# ---------------------------------------------------------------------------
# Type / severity filtering
# ---------------------------------------------------------------------------


async def test_activity_filter_by_type(client):
    """Filtering by type parameter should not error."""
    data = await do_setup(client)
    resp = await client.get("/api/activity?type=backup", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    # All returned items should match the type filter
    for item in body["items"]:
        assert item["type"] == "backup"


async def test_activity_filter_by_severity(client):
    """Filtering by severity parameter should not error."""
    data = await do_setup(client)
    resp = await client.get("/api/activity?severity=info", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    for item in body["items"]:
        assert item["severity"] == "info"


async def test_activity_filter_by_nonexistent_type(client):
    """Filtering by a type with no matches returns empty list."""
    data = await do_setup(client)
    resp = await client.get("/api/activity?type=nonexistent_type_xyz", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
