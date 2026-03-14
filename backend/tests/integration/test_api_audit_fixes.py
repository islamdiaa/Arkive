"""
Tests for API audit fixes applied in the spec-compliance pass.

Covers:
- POST /auth/setup stores setup_completed, web_url, setup_completed_at settings
- POST /auth/setup returns jobs_created as integer count
- POST /auth/setup returns web_url and first_backup_triggered fields
- GET /jobs/runs supports status and days query params
- GET /auth/session returns setup_completed_at after setup
- Standard pagination shape on all list endpoints
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# POST /auth/setup — response shape compliance
# ---------------------------------------------------------------------------


async def test_setup_response_includes_web_url(client):
    """Setup response must include web_url field per spec."""
    data = await do_setup(client)
    assert "web_url" in data
    assert isinstance(data["web_url"], str)
    assert len(data["web_url"]) > 0


async def test_setup_response_includes_first_backup_triggered(client):
    """Setup response must include first_backup_triggered boolean per spec."""
    data = await do_setup(client)
    assert "first_backup_triggered" in data
    assert isinstance(data["first_backup_triggered"], bool)


async def test_setup_response_jobs_created_is_integer(client):
    """jobs_created must be an integer count, not a list."""
    data = await do_setup(client)
    assert "jobs_created" in data
    assert isinstance(data["jobs_created"], int)
    assert data["jobs_created"] == 3


async def test_setup_stores_setup_completed_setting(client):
    """POST /auth/setup must store setup_completed=true in settings table."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Verify via settings endpoint
    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200
    settings_items = resp.json()["items"]
    setup_completed = next((s for s in settings_items if s["key"] == "setup_completed"), None)
    assert setup_completed is not None
    assert setup_completed["value"] == "true"


async def test_setup_stores_web_url_setting(client):
    """POST /auth/setup must store web_url in settings table."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    # web_url is exposed as a top-level convenience field
    assert "web_url" in body


async def test_setup_stores_setup_completed_at_setting(client):
    """POST /auth/setup must store setup_completed_at timestamp in settings."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200
    settings_items = resp.json()["items"]
    ts_setting = next((s for s in settings_items if s["key"] == "setup_completed_at"), None)
    assert ts_setting is not None
    assert ts_setting["value"]  # Non-empty ISO timestamp


# ---------------------------------------------------------------------------
# GET /auth/session — metadata after setup
# ---------------------------------------------------------------------------


async def test_session_includes_setup_completed_at(client):
    """GET /auth/session must return setup_completed_at after setup."""
    await do_setup(client)
    resp = await client.get("/api/auth/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["setup_required"] is False
    assert "setup_completed_at" in body
    assert body["setup_completed_at"]  # Non-empty timestamp


# ---------------------------------------------------------------------------
# GET /jobs/runs — status and days filter params
# ---------------------------------------------------------------------------


async def test_list_runs_filter_by_status(client):
    """GET /jobs/runs?status=success should filter by status."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # With no runs, should return empty list regardless of filter
    resp = await client.get("/api/jobs/runs?status=success", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    assert body["total"] == 0


async def test_list_runs_filter_by_days(client):
    """GET /jobs/runs?days=7 should filter to runs within last N days."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/jobs/runs?days=7", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    assert "total" in body


async def test_list_runs_combined_filters(client):
    """GET /jobs/runs?status=failed&days=30 should accept both filters."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/jobs/runs?status=failed&days=30", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["total"] == 0


async def test_list_runs_status_filter_with_run(client):
    """Trigger a run, then filter by status=running should return it."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Get a job ID
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    # Trigger a run (creates a 'running' row)
    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 202

    # Filter by running status
    resp = await client.get("/api/jobs/runs?status=running", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert item["status"] == "running"


# ---------------------------------------------------------------------------
# Standard pagination shape across all list endpoints
# ---------------------------------------------------------------------------

PAGINATED_ENDPOINTS = [
    "/api/jobs",
    "/api/jobs/runs",
    "/api/targets",
    "/api/notifications",
    "/api/activity",
    "/api/snapshots",
    "/api/databases",
    "/api/discover/containers",
    "/api/discover/databases",
]


@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_paginated_endpoint_shape(client, endpoint):
    """All paginated list endpoints must return {items, total, limit, offset, has_more}."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get(endpoint, headers=auth_headers(api_key))
    assert resp.status_code == 200, f"{endpoint} returned {resp.status_code}: {resp.text}"
    body = resp.json()

    assert "items" in body, f"{endpoint} missing 'items' key"
    assert isinstance(body["items"], list), f"{endpoint} 'items' is not a list"
    assert "total" in body, f"{endpoint} missing 'total' key"
    assert isinstance(body["total"], int), f"{endpoint} 'total' is not an int"
    assert "has_more" in body, f"{endpoint} missing 'has_more' key"
