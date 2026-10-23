"""
Security tests — authentication bypass, brute force, privilege escalation.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Auth Bypass
# ---------------------------------------------------------------------------


async def test_no_header_returns_401(client):
    """Protected endpoint without auth header → 401."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.get("/api/jobs")
    assert resp.status_code == 401


async def test_empty_key_returns_401(client):
    """Empty API key → 401."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.get("/api/jobs", headers={"X-API-Key": ""})
    assert resp.status_code == 401


async def test_malformed_key_returns_401(client):
    """Malformed key (not ark_ prefix) → 401."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.get("/api/jobs", headers={"X-API-Key": "not-a-valid-key"})
    assert resp.status_code == 401


async def test_setup_after_setup_returns_409(client):
    """Cannot call setup twice to create second admin."""
    await do_setup(client)
    resp = await client.post("/api/auth/setup", json={"run_first_backup": False})
    assert resp.status_code == 409
    message = (resp.json().get("detail") or resp.json().get("message") or "").lower()
    assert "already completed" in message


async def test_regenerate_requires_valid_key(client):
    """Cannot regenerate API key without valid current key."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.post("/api/auth/regenerate")
    assert resp.status_code == 401


async def test_regenerate_with_bad_key(client):
    """Cannot regenerate API key with invalid key."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.post(
        "/api/auth/regenerate",
        headers=auth_headers("ark_invalid_key"),
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Brute Force
# ---------------------------------------------------------------------------


async def test_rate_limiting_triggers_after_5_attempts(client):
    """Rate limiting kicks in after 5 failed attempts."""
    await do_setup(client)
    client.cookies.clear()

    for i in range(5):
        resp = await client.get("/api/jobs", headers=auth_headers(f"ark_bad_{i}"))
        assert resp.status_code == 401

    resp = await client.get("/api/jobs", headers=auth_headers("ark_bad_6"))
    assert resp.status_code == 429


async def test_rate_limiting_message(client):
    """Rate limit response has descriptive message."""
    await do_setup(client)
    client.cookies.clear()

    for _ in range(6):
        resp = await client.get("/api/jobs", headers=auth_headers("ark_bad"))

    assert resp.status_code == 429
    assert "too many" in resp.json()["message"].lower()


# ---------------------------------------------------------------------------
# Status endpoint (no auth required)
# ---------------------------------------------------------------------------


async def test_status_accessible_without_auth(client):
    """GET /api/status works without auth even after setup."""
    await do_setup(client)
    resp = await client.get("/api/status")
    assert resp.status_code == 200
