"""
API authentication security tests — rate limiting, token auth, setup bypass.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def test_rate_limiting_lockout(client):
    """5+ bad keys triggers lockout (429)."""
    await do_setup(client)

    for _ in range(5):
        resp = await client.get("/api/jobs", headers=auth_headers("ark_bad"))
        assert resp.status_code == 401

    # Should be locked out now
    resp = await client.get("/api/jobs", headers=auth_headers("ark_bad"))
    assert resp.status_code == 429


async def test_token_query_param_auth_rejected(client):
    """API key via ?token= query param is no longer accepted (use X-API-Key header)."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Query param auth must be rejected — use SSE tokens for EventSource instead
    resp = await client.get(f"/api/jobs?token={api_key}")
    assert resp.status_code == 401


async def test_setup_mode_bypass(client):
    """Before setup, protected endpoints are accessible (setup mode)."""
    # Before setup, endpoints should be accessible (setup mode bypass)
    resp = await client.get("/api/jobs")
    assert resp.status_code == 200


async def test_no_auth_after_setup_returns_401(client):
    """After setup, endpoints require auth."""
    await do_setup(client)

    resp = await client.get("/api/jobs")
    assert resp.status_code == 401
