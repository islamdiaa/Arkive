"""Tests for OAuth authentication flows: start, complete, state management.

Covers:
- POST /api/targets/oauth/start (Dropbox + Google Drive)
- POST /api/targets/oauth/complete (token exchange, target creation)
- OAuth state token lifecycle (TTL expiry, max pending limit, cleanup)
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

import app.api.targets as targets_mod
from app.core.security import decrypt_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_auth_url(url: str) -> tuple[str, dict[str, list[str]]]:
    """Split an authorization URL into base + parsed query parameters."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    params = parse_qs(parsed.query)
    return base, params


def _get_error_message(resp) -> str:
    """Extract the error message from an API error response.

    The global exception handler wraps HTTPException into:
      {"error": "...", "message": "...", "details": {}}
    """
    data = resp.json()
    return data.get("message", "") or data.get("detail", "")


@pytest.fixture(autouse=True)
def _clear_oauth_state():
    """Ensure the in-memory OAuth pending dict is clean before each test."""
    targets_mod._oauth_pending.clear()
    yield
    targets_mod._oauth_pending.clear()


def _mock_httpx_client(mock_response):
    """Build a mock httpx.AsyncClient context manager returning mock_response on post()."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _make_mock_httpx_response(status_code=200, json_data=None):
    """Create a mock httpx.Response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data or {}
    mock_resp.text = json.dumps(json_data or {})
    return mock_resp


# ===========================================================================
# OAuth Start -- Dropbox
# ===========================================================================


class TestOAuthStartDropbox:
    """POST /api/targets/oauth/start for Dropbox provider."""

    async def test_returns_authorization_url_with_correct_params(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "test-dropbox-client-id",
                "client_secret": "test-dropbox-secret",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert "authorization_url" in data
        assert data["provider"] == "dropbox"
        assert "state" in data
        assert "redirect_uri" in data

        base, params = _parse_auth_url(data["authorization_url"])
        assert base == "https://www.dropbox.com/oauth2/authorize"
        assert params["client_id"] == ["test-dropbox-client-id"]
        assert params["response_type"] == ["code"]
        assert params["token_access_type"] == ["offline"]
        assert params["state"] == [data["state"]]

    async def test_state_token_is_stored(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "test-client",
            },
        )
        assert resp.status_code == 200
        state = resp.json()["state"]

        # The state should exist in the module-level pending dict
        assert state in targets_mod._oauth_pending
        pending = targets_mod._oauth_pending[state]
        assert pending["provider"] == "dropbox"
        assert pending["client_id"] == "test-client"
        assert "_created_ts" in pending

    async def test_default_redirect_uri(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "test-client",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["redirect_uri"] == "http://localhost:8200/oauth/callback"

    async def test_custom_redirect_uri(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "test-client",
                "redirect_uri": "https://my.app/callback",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["redirect_uri"] == "https://my.app/callback"
        _, params = _parse_auth_url(data["authorization_url"])
        assert params["redirect_uri"] == ["https://my.app/callback"]

    async def test_unsupported_provider_returns_400(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "onedrive",
                "client_id": "x",
            },
        )
        assert resp.status_code == 400

    async def test_missing_client_id_returns_400(self, provider_client):
        """No client_id in request body and none in DB settings."""
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
            },
        )
        assert resp.status_code == 400
        msg = _get_error_message(resp).lower()
        assert "client_id" in msg


# ===========================================================================
# OAuth Start -- Google Drive
# ===========================================================================


class TestOAuthStartGoogleDrive:
    """POST /api/targets/oauth/start for Google Drive (gdrive) provider."""

    async def test_returns_google_authorization_url(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "gdrive",
                "client_id": "123456.apps.googleusercontent.com",
                "client_secret": "goog-secret",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["provider"] == "gdrive"

        base, params = _parse_auth_url(data["authorization_url"])
        assert base == "https://accounts.google.com/o/oauth2/v2/auth"
        assert params["client_id"] == ["123456.apps.googleusercontent.com"]
        assert params["response_type"] == ["code"]

    async def test_includes_google_specific_scopes(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "gdrive",
                "client_id": "goog-client",
            },
        )
        assert resp.status_code == 200
        _, params = _parse_auth_url(resp.json()["authorization_url"])
        assert params["scope"] == ["https://www.googleapis.com/auth/drive.file"]

    async def test_includes_access_type_offline_and_prompt_consent(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "gdrive",
                "client_id": "goog-client",
            },
        )
        assert resp.status_code == 200
        _, params = _parse_auth_url(resp.json()["authorization_url"])
        assert params["access_type"] == ["offline"]
        assert params["prompt"] == ["consent"]

    async def test_dropbox_url_does_not_have_gdrive_params(self, provider_client):
        """Verify Dropbox requests do NOT carry scope/access_type/prompt."""
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "db-client",
            },
        )
        assert resp.status_code == 200
        _, params = _parse_auth_url(resp.json()["authorization_url"])
        assert "scope" not in params
        assert "access_type" not in params
        assert "prompt" not in params


# ===========================================================================
# OAuth Start -- Rate Limiting / State Limits
# ===========================================================================


class TestOAuthStateLimits:
    """Enforce max pending states (50) and TTL expiry (600s)."""

    async def test_max_pending_states_returns_429(self, provider_client):
        """Fill up _oauth_pending to the limit, then the next request gets 429."""
        # Pre-fill the pending dict with 50 entries
        for i in range(targets_mod._OAUTH_MAX_PENDING):
            targets_mod._oauth_pending[f"fake-state-{i}"] = {
                "provider": "dropbox",
                "client_id": "x",
                "_created_ts": time.time(),
            }

        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "overflow-client",
            },
        )
        assert resp.status_code == 429
        msg = _get_error_message(resp).lower()
        assert "too many" in msg

    async def test_expired_states_cleaned_before_limit_check(self, provider_client):
        """Expired states are removed before the limit check, so a new flow succeeds."""
        # Fill with 50 entries that are all expired (created > 600s ago)
        old_ts = time.time() - targets_mod._OAUTH_STATE_TTL - 10
        for i in range(targets_mod._OAUTH_MAX_PENDING):
            targets_mod._oauth_pending[f"expired-state-{i}"] = {
                "provider": "dropbox",
                "client_id": "x",
                "_created_ts": old_ts,
            }

        # This should succeed because expired states get cleaned
        resp = await provider_client.post(
            "/api/targets/oauth/start",
            json={
                "provider": "dropbox",
                "client_id": "fresh-client",
            },
        )
        assert resp.status_code == 200
        # Expired entries should have been removed
        assert len(targets_mod._oauth_pending) == 1


# ===========================================================================
# OAuth State Cleanup
# ===========================================================================


class TestOAuthStateCleanup:
    """_cleanup_expired_oauth_states helper behavior."""

    def test_cleanup_removes_expired_states(self):
        old_ts = time.time() - targets_mod._OAUTH_STATE_TTL - 1
        targets_mod._oauth_pending["old"] = {"_created_ts": old_ts, "provider": "dropbox"}
        targets_mod._oauth_pending["fresh"] = {"_created_ts": time.time(), "provider": "gdrive"}

        targets_mod._cleanup_expired_oauth_states()

        assert "old" not in targets_mod._oauth_pending
        assert "fresh" in targets_mod._oauth_pending

    def test_cleanup_keeps_non_expired_states(self):
        now = time.time()
        targets_mod._oauth_pending["a"] = {"_created_ts": now, "provider": "dropbox"}
        targets_mod._oauth_pending["b"] = {"_created_ts": now - 100, "provider": "gdrive"}

        targets_mod._cleanup_expired_oauth_states()

        assert len(targets_mod._oauth_pending) == 2

    def test_cleanup_handles_empty_dict(self):
        targets_mod._cleanup_expired_oauth_states()
        assert len(targets_mod._oauth_pending) == 0


# ===========================================================================
# OAuth Complete
# ===========================================================================


class TestOAuthComplete:
    """POST /api/targets/oauth/complete -- token exchange + target creation."""

    async def test_invalid_state_returns_400(self, provider_client):
        resp = await provider_client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "dropbox",
                "code": "auth-code-123",
                "state": "nonexistent-state-token",
            },
        )
        assert resp.status_code == 400
        msg = _get_error_message(resp).lower()
        assert "invalid" in msg or "expired" in msg

    async def test_expired_state_returns_400(self, provider_client):
        """A state that was already cleaned up (expired) is simply not found."""
        old_ts = time.time() - targets_mod._OAUTH_STATE_TTL - 100
        targets_mod._oauth_pending["stale-state"] = {
            "provider": "dropbox",
            "client_id": "x",
            "client_secret": "s",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": old_ts,
        }

        # Simulate the state having been cleaned up before complete is called
        del targets_mod._oauth_pending["stale-state"]

        resp = await provider_client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "dropbox",
                "code": "auth-code",
                "state": "stale-state",
            },
        )
        assert resp.status_code == 400

    async def test_provider_mismatch_returns_400(self, provider_client):
        """State was created for dropbox, but complete says gdrive."""
        targets_mod._oauth_pending["mismatch-state"] = {
            "provider": "dropbox",
            "client_id": "x",
            "client_secret": "s",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        resp = await provider_client.post(
            "/api/targets/oauth/complete",
            json={
                "provider": "gdrive",
                "code": "auth-code",
                "state": "mismatch-state",
            },
        )
        assert resp.status_code == 400
        msg = _get_error_message(resp).lower()
        assert "dropbox" in msg

    async def test_success_creates_target_dropbox(self, provider_client):
        """Successful token exchange creates a Dropbox storage target."""
        state = "valid-dropbox-state"
        targets_mod._oauth_pending[state] = {
            "provider": "dropbox",
            "client_id": "db-client-id",
            "client_secret": "db-client-secret",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        token_response = {
            "access_token": "sl.access-token-dropbox",
            "token_type": "bearer",
            "refresh_token": "refresh-tok-dropbox",
            "expires_in": 14400,
        }
        mock_resp = _make_mock_httpx_response(200, token_response)
        mock_client = _mock_httpx_client(mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "dropbox",
                    "code": "auth-code-from-dropbox",
                    "state": state,
                    "name": "My Dropbox Backup",
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["type"] == "dropbox"
        assert data["name"] == "My Dropbox Backup"
        assert data["oauth_complete"] is True
        assert data["has_refresh_token"] is True
        assert data["enabled"] is True
        assert "id" in data

        # State should have been consumed
        assert state not in targets_mod._oauth_pending

        # Verify the httpx call was made to the correct token URL
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://api.dropboxapi.com/oauth2/token"

    async def test_success_creates_target_gdrive(self, provider_client):
        """Successful token exchange creates a Google Drive target with extra config."""
        state = "valid-gdrive-state"
        targets_mod._oauth_pending[state] = {
            "provider": "gdrive",
            "client_id": "goog-client-id",
            "client_secret": "goog-client-secret",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        token_response = {
            "access_token": "ya29.google-access",
            "token_type": "Bearer",
            "refresh_token": "1//google-refresh",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/drive.file",
        }
        mock_resp = _make_mock_httpx_response(200, token_response)
        mock_client = _mock_httpx_client(mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "gdrive",
                    "code": "auth-code-from-google",
                    "state": state,
                    "client_id": "goog-client-id",
                    "client_secret": "goog-client-secret",
                },
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["type"] == "gdrive"
        assert data["oauth_complete"] is True
        assert data["has_refresh_token"] is True

        # Verify the httpx call went to the Google token URL
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://oauth2.googleapis.com/token"

    async def test_target_config_is_fernet_encrypted(self, provider_client):
        """After OAuth target creation, the config stored in DB must be encrypted."""
        state = "enc-test-state"
        targets_mod._oauth_pending[state] = {
            "provider": "dropbox",
            "client_id": "enc-client",
            "client_secret": "enc-secret",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        token_response = {
            "access_token": "sl.encrypted-check",
            "refresh_token": "refresh-enc",
        }
        mock_resp = _make_mock_httpx_response(200, token_response)
        mock_client = _mock_httpx_client(mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "dropbox",
                    "code": "code",
                    "state": state,
                },
            )

        assert resp.status_code == 200
        target_id = resp.json()["id"]

        # Read the raw DB row to check encryption
        import aiosqlite

        db_path = provider_client._tmp_path / "arkive.db"
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT config FROM storage_targets WHERE id = ?", (target_id,))
            row = await cursor.fetchone()

        raw_config = row["config"]
        assert raw_config.startswith("enc:v1:"), "Config should be Fernet-encrypted"

        # Decrypt and verify content
        decrypted = decrypt_config(raw_config, str(provider_client._tmp_path))
        assert "token" in decrypted
        assert decrypted["oauth_provider"] == "dropbox"

    async def test_token_exchange_failure_returns_502(self, provider_client):
        """If the provider returns a non-200, the endpoint returns 502."""
        state = "fail-token-state"
        targets_mod._oauth_pending[state] = {
            "provider": "dropbox",
            "client_id": "x",
            "client_secret": "s",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        mock_resp = _make_mock_httpx_response(401, {"error": "invalid_grant"})
        mock_client = _mock_httpx_client(mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "dropbox",
                    "code": "bad-code",
                    "state": state,
                },
            )

        assert resp.status_code == 502
        msg = _get_error_message(resp).lower()
        assert "token exchange failed" in msg

    async def test_token_exchange_network_error_returns_502(self, provider_client):
        """If httpx raises a RequestError, the endpoint returns 502."""
        import httpx

        state = "network-fail-state"
        targets_mod._oauth_pending[state] = {
            "provider": "dropbox",
            "client_id": "x",
            "client_secret": "s",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.RequestError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "dropbox",
                    "code": "code",
                    "state": state,
                },
            )

        assert resp.status_code == 502
        msg = _get_error_message(resp).lower()
        assert "could not reach" in msg

    async def test_default_name_when_none_provided(self, provider_client):
        """When no name is given, a default name like 'Dropbox (abc123)' is used."""
        state = "no-name-state"
        targets_mod._oauth_pending[state] = {
            "provider": "dropbox",
            "client_id": "x",
            "client_secret": "s",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        token_response = {"access_token": "tok", "refresh_token": "ref"}
        mock_resp = _make_mock_httpx_response(200, token_response)
        mock_client = _mock_httpx_client(mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "dropbox",
                    "code": "code",
                    "state": state,
                },
            )

        assert resp.status_code == 200
        name = resp.json()["name"]
        assert name.startswith("Dropbox (")

    async def test_gdrive_target_stores_client_id_and_secret(self, provider_client):
        """Google Drive targets store client_id and client_secret in config."""
        state = "gdrive-config-state"
        targets_mod._oauth_pending[state] = {
            "provider": "gdrive",
            "client_id": "goog-cid",
            "client_secret": "goog-csec",
            "redirect_uri": "http://localhost:8200/oauth/callback",
            "_created_ts": time.time(),
        }

        token_response = {
            "access_token": "ya29.tok",
            "refresh_token": "1//ref",
        }
        mock_resp = _make_mock_httpx_response(200, token_response)
        mock_client = _mock_httpx_client(mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await provider_client.post(
                "/api/targets/oauth/complete",
                json={
                    "provider": "gdrive",
                    "code": "google-code",
                    "state": state,
                    "client_id": "goog-cid",
                    "client_secret": "goog-csec",
                },
            )

        assert resp.status_code == 200
        target_id = resp.json()["id"]

        # Read DB and decrypt to verify client_id / client_secret are stored
        import aiosqlite

        db_path = provider_client._tmp_path / "arkive.db"
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT config FROM storage_targets WHERE id = ?", (target_id,))
            row = await cursor.fetchone()

        decrypted = decrypt_config(row["config"], str(provider_client._tmp_path))
        assert decrypted["client_id"] == "goog-cid"
        assert decrypted["client_secret"] == "goog-csec"
        assert decrypted["oauth_provider"] == "gdrive"
