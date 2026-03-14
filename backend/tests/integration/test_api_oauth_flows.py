"""
Integration tests for OAuth authorization flows (Dropbox and Google Drive).

Tests the /api/targets/oauth/start and /api/targets/oauth/complete endpoints
with mocked external HTTP calls to token exchange services.

Note: The app uses a custom exception handler that wraps HTTPException into
{"error": ..., "message": ..., "details": {}} — so we check resp.json()["message"]
rather than resp.json()["detail"].
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


def _clear_oauth_pending():
    """Clear the module-level OAuth pending state between tests."""
    from app.api.targets import _oauth_pending

    _oauth_pending.clear()


def _mock_token_response(access_token="mock_access_token", refresh_token="mock_refresh"):
    """Create a mock httpx response for a successful token exchange."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": 3600,
    }
    return mock_resp


def _mock_failed_token_response():
    """Create a mock httpx response for a failed token exchange."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {"error": "invalid_grant"}
    mock_resp.text = '{"error": "invalid_grant"}'
    return mock_resp


def _build_mock_httpx_client(token_response):
    """Build a mock httpx.AsyncClient context manager returning the given
    token response for any POST call."""
    mock_client_instance = AsyncMock()
    mock_client_instance.post.return_value = token_response
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)
    return mock_client_instance


# ---------------------------------------------------------------------------
# Dropbox OAuth -- /api/targets/oauth/start
# ---------------------------------------------------------------------------


async def test_oauth_start_dropbox_returns_auth_url(client):
    """POST /api/targets/oauth/start with provider=dropbox returns authorization_url."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_dropbox_client_id",
            "client_secret": "test_dropbox_secret",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "authorization_url" in data
    assert "state" in data
    assert data["provider"] == "dropbox"
    assert "dropbox.com/oauth2/authorize" in data["authorization_url"]
    assert "test_dropbox_client_id" in data["authorization_url"]
    assert "token_access_type=offline" in data["authorization_url"]


async def test_oauth_start_dropbox_requires_auth(client):
    """POST /api/targets/oauth/start without API key returns 401."""
    _clear_oauth_pending()
    await do_setup(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_id",
        },
    )

    assert resp.status_code == 401


async def test_oauth_start_dropbox_generates_unique_state(client):
    """Two OAuth start calls generate different state tokens."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp1 = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_client_id",
        },
        headers=auth_headers(api_key),
    )

    resp2 = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_client_id",
        },
        headers=auth_headers(api_key),
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    state1 = resp1.json()["state"]
    state2 = resp2.json()["state"]
    assert state1 != state2


# ---------------------------------------------------------------------------
# Dropbox OAuth -- /api/targets/oauth/complete
# ---------------------------------------------------------------------------


async def test_oauth_complete_dropbox_creates_target(client):
    """POST /api/targets/oauth/complete with valid code creates a target."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    # Step 1: Start the OAuth flow to get a valid state token
    start_resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
        },
        headers=auth_headers(api_key),
    )
    assert start_resp.status_code == 200
    state = start_resp.json()["state"]

    # Step 2: Mock the httpx token exchange and complete
    mock_client = _build_mock_httpx_client(_mock_token_response())

    with patch("httpx.AsyncClient", return_value=mock_client):
        complete_resp = await client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "dropbox",
                "code": "valid_auth_code_123",
                "state": state,
                "name": "My Dropbox",
            },
            headers=auth_headers(api_key),
        )

    assert complete_resp.status_code == 200
    data = complete_resp.json()
    assert data["type"] == "dropbox"
    assert data["name"] == "My Dropbox"
    assert "id" in data
    assert data["oauth_complete"] is True
    assert data["has_refresh_token"] is True


async def test_oauth_complete_dropbox_invalid_state(client):
    """Invalid/expired state token returns an error."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/complete",
        json={
            "provider": "dropbox",
            "code": "some_code",
            "state": "totally_invalid_state_token",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 400
    message = resp.json()["message"].lower()
    assert "invalid" in message or "expired" in message


async def test_oauth_complete_dropbox_invalid_code(client):
    """Bad authorization code causes the token exchange to fail with 502."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    # Start OAuth flow first
    start_resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
        },
        headers=auth_headers(api_key),
    )
    state = start_resp.json()["state"]

    # Mock a failed token exchange (400 response from provider)
    mock_client = _build_mock_httpx_client(_mock_failed_token_response())

    with patch("httpx.AsyncClient", return_value=mock_client):
        complete_resp = await client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "dropbox",
                "code": "bad_code",
                "state": state,
            },
            headers=auth_headers(api_key),
        )

    # The endpoint returns 502 when the upstream token exchange fails
    assert complete_resp.status_code == 502
    message = complete_resp.json()["message"].lower()
    assert "token exchange failed" in message
    assert "400" in message


# ---------------------------------------------------------------------------
# Google Drive OAuth -- /api/targets/oauth/start
# ---------------------------------------------------------------------------


async def test_oauth_start_gdrive_returns_auth_url(client):
    """POST /api/targets/oauth/start with provider=gdrive returns authorization_url."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_gdrive_client_id",
            "client_secret": "test_gdrive_secret",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "authorization_url" in data
    assert "state" in data
    assert data["provider"] == "gdrive"
    assert "accounts.google.com" in data["authorization_url"]
    assert "test_gdrive_client_id" in data["authorization_url"]


async def test_oauth_start_gdrive_requires_auth(client):
    """POST /api/targets/oauth/start for gdrive without API key returns 401."""
    _clear_oauth_pending()
    await do_setup(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_id",
        },
    )

    assert resp.status_code == 401


async def test_oauth_start_gdrive_includes_scope(client):
    """Google Drive auth URL should include the drive.file scope."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_gdrive_client_id",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 200
    auth_url = resp.json()["authorization_url"]
    assert "drive.file" in auth_url


async def test_oauth_start_gdrive_generates_unique_state(client):
    """Two gdrive OAuth start calls generate different state tokens."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp1 = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_client_id",
        },
        headers=auth_headers(api_key),
    )

    resp2 = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_client_id",
        },
        headers=auth_headers(api_key),
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["state"] != resp2.json()["state"]


# ---------------------------------------------------------------------------
# Google Drive OAuth -- /api/targets/oauth/complete
# ---------------------------------------------------------------------------


async def test_oauth_complete_gdrive_creates_target(client):
    """POST /api/targets/oauth/complete with valid gdrive code creates a target."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    # Start the OAuth flow
    start_resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_gdrive_client",
            "client_secret": "test_gdrive_secret",
        },
        headers=auth_headers(api_key),
    )
    assert start_resp.status_code == 200
    state = start_resp.json()["state"]

    # Mock the token exchange
    mock_client = _build_mock_httpx_client(_mock_token_response())

    with patch("httpx.AsyncClient", return_value=mock_client):
        complete_resp = await client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "gdrive",
                "code": "valid_gdrive_code",
                "state": state,
                "name": "My Google Drive",
            },
            headers=auth_headers(api_key),
        )

    assert complete_resp.status_code == 200
    data = complete_resp.json()
    assert data["type"] == "gdrive"
    assert data["name"] == "My Google Drive"
    assert data["oauth_complete"] is True
    assert data["has_refresh_token"] is True


async def test_oauth_complete_gdrive_invalid_state(client):
    """Invalid state token for gdrive returns an error."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/complete",
        json={
            "provider": "gdrive",
            "code": "some_code",
            "state": "invalid_gdrive_state_xyz",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 400
    message = resp.json()["message"].lower()
    assert "invalid" in message or "expired" in message


async def test_oauth_complete_gdrive_invalid_code(client):
    """Bad authorization code for gdrive causes token exchange to fail with 502."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    # Start OAuth
    start_resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "gdrive",
            "client_id": "test_client_id",
            "client_secret": "test_secret",
        },
        headers=auth_headers(api_key),
    )
    state = start_resp.json()["state"]

    # Mock failed token exchange
    mock_client = _build_mock_httpx_client(_mock_failed_token_response())

    with patch("httpx.AsyncClient", return_value=mock_client):
        complete_resp = await client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "gdrive",
                "code": "bad_code",
                "state": state,
            },
            headers=auth_headers(api_key),
        )

    # The endpoint returns 502 when the upstream token exchange fails
    assert complete_resp.status_code == 502
    message = complete_resp.json()["message"].lower()
    assert "token exchange failed" in message


# ---------------------------------------------------------------------------
# General OAuth error cases
# ---------------------------------------------------------------------------


async def test_oauth_start_invalid_provider(client):
    """Provider that doesn't support OAuth returns 400."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "s3",
            "client_id": "test_id",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 400
    message = resp.json()["message"].lower()
    assert "unsupported" in message


async def test_oauth_complete_provider_mismatch(client):
    """State initiated for dropbox but completing for gdrive returns error."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    # Start OAuth for dropbox
    start_resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
            "client_id": "test_client_id",
            "client_secret": "test_secret",
        },
        headers=auth_headers(api_key),
    )
    assert start_resp.status_code == 200
    state = start_resp.json()["state"]

    # Try to complete as gdrive
    complete_resp = await client.post(
        "/api/targets/oauth/complete",
        json={
            "provider": "gdrive",
            "code": "some_code",
            "state": state,
        },
        headers=auth_headers(api_key),
    )

    assert complete_resp.status_code == 400
    message = complete_resp.json()["message"].lower()
    assert "dropbox" in message or "mismatch" in message


async def test_oauth_start_requires_client_id(client):
    """OAuth start without client_id and no stored client_id returns 400."""
    _clear_oauth_pending()
    api_key = await setup_auth(client)

    resp = await client.post(
        "/api/targets/oauth/start",
        json={
            "provider": "dropbox",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 400
    message = resp.json()["message"].lower()
    assert "client_id" in message
