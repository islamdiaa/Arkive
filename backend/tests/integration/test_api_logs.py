"""
API integration tests for log endpoints.

Tests: list logs, clear logs.
Note: /api/logs/sources and /api/logs/levels endpoints do not exist.
Note: SSE stream testing requires special event loop handling.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def test_list_logs_empty(client):
    """GET /api/logs should return 200 with items list."""
    data = await do_setup(client)
    resp = await client.get("/api/logs", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_list_logs_has_items_key(client):
    """GET /api/logs should return 'items' key with log entries."""
    data = await do_setup(client)
    resp = await client.get("/api/logs", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_clear_logs(client):
    """DELETE /api/logs should clear the log file."""
    data = await do_setup(client)
    resp = await client.delete("/api/logs", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cleared"


async def test_logs_filter_by_level(client):
    """GET /api/logs?level=INFO should accept level filter."""
    data = await do_setup(client)
    resp = await client.get("/api/logs?level=INFO", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
