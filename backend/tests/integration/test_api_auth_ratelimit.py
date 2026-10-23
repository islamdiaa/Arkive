"""
Integration tests for setup endpoint rate limiting.

Covers:
- Normal setup within rate limit succeeds
- 6th attempt within window returns 429
- Rate limit resets after window expires (time-mocked)
- Different IPs have independent rate limits
- Rate limiting does not break normal first-call setup flow
- Rate limit fires BEFORE the "already set up" 409 check
"""

import time

import pytest

import app.api.auth as auth_mod
from tests.conftest import do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixture: clear the module-level _setup_attempts dict between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_setup_rate_limit():
    """Wipe in-module rate-limit state before each test."""
    auth_mod._setup_attempts.clear()
    yield
    auth_mod._setup_attempts.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SETUP_BODY = {"run_first_backup": False, "encryption_password": "test-password"}


async def _post_setup(client, **overrides):
    body = {**SETUP_BODY, **overrides}
    return await client.post("/api/auth/setup", json=body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_first_setup_attempt_succeeds(client):
    """First attempt within the rate-limit window must succeed (201)."""
    resp = await _post_setup(client)
    assert resp.status_code == 201
    data = resp.json()
    assert "api_key" in data


async def test_sixth_attempt_returns_429(client):
    """After 5 attempts, the 6th returns 429 Too Many Requests."""
    # The first call will succeed (201); subsequent calls within the same
    # fresh DB will return 409 (already set up) — but the rate limiter
    # must fire on the 6th attempt BEFORE reaching the 409 logic.
    for i in range(5):
        resp = await _post_setup(client)
        # First call: 201; subsequent calls: 409 (already set up)
        assert resp.status_code in (201, 409), f"Unexpected {resp.status_code} on attempt {i + 1}"

    # 6th attempt must be rate-limited
    resp = await _post_setup(client)
    assert resp.status_code == 429
    body = resp.json()
    # Custom exception handler wraps HTTPException — body uses "message" key
    assert "Too many setup attempts" in body.get("message", body.get("detail", ""))


async def test_rate_limit_resets_after_window(client, monkeypatch):
    """After the 15-minute window passes, attempts reset and requests succeed again."""
    # Exhaust the rate limit
    for _ in range(5):
        await _post_setup(client)

    # Confirm we are now rate-limited
    resp = await _post_setup(client)
    assert resp.status_code == 429

    # Fast-forward time past the window by patching time.time in the auth module
    real_time = time.time()
    future = real_time + auth_mod.SETUP_RATE_WINDOW + 1

    monkeypatch.setattr(auth_mod.time, "time", lambda: future)

    # The rate limiter should prune old entries and allow this attempt.
    # The DB is already set up, so expect 409 (not 429).
    resp = await _post_setup(client)
    assert resp.status_code == 409, (
        f"Expected 409 (already set up, rate limit reset) but got {resp.status_code}: {resp.text}"
    )


async def test_different_ips_have_independent_limits(client, monkeypatch):
    """Exhausting rate limit for one IP must not block a different IP."""
    # Simulate requests from IP "1.2.3.4" by patching the module dict directly
    for _ in range(5):
        # Manually inject timestamps for ip1 to simulate exhaustion
        auth_mod._setup_attempts["1.2.3.4"].append(time.time())

    # Confirm 1.2.3.4 is blocked via direct state inspection
    assert len(auth_mod._setup_attempts["1.2.3.4"]) >= auth_mod.SETUP_RATE_LIMIT

    # A fresh IP ("127.0.0.1" — httpx ASGITransport default) should still work fine.
    resp = await _post_setup(client)
    assert resp.status_code == 201, f"Different IP should not be rate-limited; got {resp.status_code}: {resp.text}"


async def test_rate_limit_fires_before_already_set_up_check(client):
    """
    Rate limiting must happen BEFORE the 'already set up' 409 check.

    Pre-populate the setup dict to the limit, then verify the next call
    returns 429 — not 409 — even though the DB may (or may not) be set up.
    """
    # httpx ASGITransport presents as 127.0.0.1 — inject under that key
    now = time.time()
    for _ in range(auth_mod.SETUP_RATE_LIMIT):
        auth_mod._setup_attempts["127.0.0.1"].append(now)

    # First do a real setup so the DB is "already set up"
    # (we don't want a fresh 201 to reset our manually-injected state)
    # Actually: with the counter already at limit, even this should 429.
    resp = await _post_setup(client)
    assert resp.status_code == 429, f"Rate limit should fire before 409 check; got {resp.status_code}: {resp.text}"


async def test_normal_setup_flow_unaffected(client):
    """Rate limiting must not break a clean first-call setup."""
    data = await do_setup(client)
    assert "api_key" in data
    assert data["api_key"].startswith("ark_")


async def test_rate_limit_window_constants():
    """Sanity-check that the module constants are as specified."""
    assert auth_mod.SETUP_RATE_LIMIT == 5
    assert auth_mod.SETUP_RATE_WINDOW == 900  # 15 minutes
