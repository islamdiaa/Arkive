"""
Integration tests for SSE token authentication.

Covers:
1. POST /api/auth/sse-token without auth → 401
2. POST /api/auth/sse-token with valid X-API-Key → {token, expires_in: 60}
3. SSE token is URL-safe string
4. GET /api/events/stream?token=<valid> → not 401
5. Same token used twice → 401 on second use (single-use)
6. GET /api/events/stream without token → 401
7. GET /api/events/stream with X-API-Key header (not SSE token) → 401
8. REST endpoint with query string API key → 401 (no longer accepted)
9. REST endpoint with X-API-Key header → works
10. Expired token is rejected
11. prune_sse_tokens removes only expired tokens
12. Garbage token is rejected
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def reset_sse_tokens():
    """Reset SSE token store before each test."""
    from app.core.security import _reset_sse_tokens

    _reset_sse_tokens()
    yield
    _reset_sse_tokens()


async def test_sse_token_endpoint_requires_auth(client):
    """POST /api/auth/sse-token without auth → 401."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.post("/api/auth/sse-token")
    assert resp.status_code == 401


async def test_sse_token_endpoint_returns_token(client):
    """POST /api/auth/sse-token with valid X-API-Key → {token, expires_in: 60}."""
    data = await do_setup(client)
    api_key = data["api_key"]
    resp = await client.post("/api/auth/sse-token", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["expires_in"] == 60


async def test_sse_token_is_url_safe_string(client):
    """SSE token must be a non-empty URL-safe string."""
    import re

    data = await do_setup(client)
    api_key = data["api_key"]
    resp = await client.post("/api/auth/sse-token", headers=auth_headers(api_key))
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert isinstance(token, str)
    assert len(token) > 0
    # URL-safe base64 charset: alphanumeric, hyphen, underscore
    assert re.match(r"^[A-Za-z0-9_\-]+$", token), f"Token not URL-safe: {token}"


async def test_sse_stream_with_valid_token(client):
    """GET /api/events/stream?token=<valid> → auth passes (not 401).

    Note: sse_starlette has event-loop binding issues in the httpx test client,
    so we verify auth succeeds by checking the token is consumed (single-use)
    rather than inspecting the HTTP status of the streaming response.
    """

    data = await do_setup(client)
    api_key = data["api_key"]
    # Issue SSE token
    resp = await client.post("/api/auth/sse-token", headers=auth_headers(api_key))
    assert resp.status_code == 200
    token = resp.json()["token"]

    # Verify the token exists before the request
    from app.core.security import _sse_tokens

    assert token in _sse_tokens

    # Make the request — auth layer runs and consumes the token before streaming starts.
    # We expect either a non-401 status (streaming started) or an exception from
    # sse_starlette's event-loop binding. Either way, auth passed.
    try:
        resp = await client.get(f"/api/events/stream?token={token}")
        # If we got a response, auth passed (no 401)
        assert resp.status_code != 401
    except Exception:
        # sse_starlette crashed the task group after auth passed —
        # confirm the token was consumed (auth ran successfully)
        assert token not in _sse_tokens


async def test_sse_token_single_use(client):
    """Same SSE token used twice → 401 on second use."""
    from app.core.security import _sse_tokens

    data = await do_setup(client)
    api_key = data["api_key"]
    # Issue token
    resp = await client.post("/api/auth/sse-token", headers=auth_headers(api_key))
    assert resp.status_code == 200
    token = resp.json()["token"]

    # First use — auth passes, token is consumed
    assert token in _sse_tokens
    try:
        resp1 = await client.get(f"/api/events/stream?token={token}")
        assert resp1.status_code != 401
    except Exception:
        pass  # sse_starlette crash after auth — expected in test env

    # Token must be consumed after first use
    assert token not in _sse_tokens

    # Second use — token gone → 401
    resp2 = await client.get(f"/api/events/stream?token={token}")
    assert resp2.status_code == 401


async def test_sse_stream_without_token_is_401(client):
    """GET /api/events/stream without token → 401."""
    await do_setup(client)
    resp = await client.get("/api/events/stream")
    assert resp.status_code == 401


async def test_sse_stream_with_api_key_header_is_401(client):
    """GET /api/events/stream with X-API-Key header (not SSE token) → 401."""
    data = await do_setup(client)
    api_key = data["api_key"]
    resp = await client.get("/api/events/stream", headers=auth_headers(api_key))
    assert resp.status_code == 401


async def test_rest_endpoint_query_string_key_rejected(client):
    """REST endpoint with ?token= query string API key → 401 (no longer accepted)."""
    data = await do_setup(client)
    api_key = data["api_key"]
    client.cookies.clear()
    # Try using the raw API key as a query param on a REST endpoint
    resp = await client.post(f"/api/auth/rotate-key?token={api_key}")
    assert resp.status_code == 401


async def test_rest_endpoint_header_key_accepted(client):
    """REST endpoint with X-API-Key header → works."""
    data = await do_setup(client)
    api_key = data["api_key"]
    resp = await client.post("/api/auth/rotate-key", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "api_key" in resp.json()


async def test_expired_sse_token_rejected(client):
    """Token with past expiry timestamp is rejected."""
    from app.core.security import _sse_tokens, _sse_tokens_lock

    await do_setup(client)
    # Insert a token that's already expired
    with _sse_tokens_lock:
        _sse_tokens["expired-token"] = 0.0  # epoch — already expired
    resp = await client.get("/api/events/stream?token=expired-token")
    assert resp.status_code == 401


async def test_prune_removes_only_expired_tokens(client):
    """prune_sse_tokens removes expired entries, keeps live ones."""
    from app.core.security import _sse_tokens, _sse_tokens_lock, generate_sse_token, prune_sse_tokens

    await do_setup(client)
    # Generate a live token
    live_token = generate_sse_token()
    # Insert an expired token and verify prune behaviour inside the lock
    with _sse_tokens_lock:
        _sse_tokens["old-token"] = 0.0
        assert "old-token" in _sse_tokens
        assert live_token in _sse_tokens
        prune_sse_tokens()
        assert "old-token" not in _sse_tokens
        assert live_token in _sse_tokens


async def test_garbage_token_rejected(client):
    """Random string as SSE token is rejected."""
    await do_setup(client)
    resp = await client.get("/api/events/stream?token=not-a-real-token")
    assert resp.status_code == 401
