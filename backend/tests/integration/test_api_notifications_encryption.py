"""
Integration tests for notification channel URL encryption.

Tests:
- Creating a channel encrypts the URL in the DB (enc:v1: prefix)
- List endpoint always redacts URL as "••••••"
- Updating a channel re-encrypts the new URL
- Test endpoint can decrypt and use the URL
"""

import json
from unittest.mock import MagicMock

import aiosqlite
import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio

_WEBHOOK_URL = "https://discord.com/api/webhooks/9876543210/secret-token-value"
_UPDATED_URL = "https://hooks.slack.com/services/T000/B000/xxxx-updated-url"


async def _setup_and_create_channel(client, url: str = _WEBHOOK_URL) -> tuple[str, str]:
    """Do setup, create a channel, return (api_key, channel_id)."""
    data = await do_setup(client)
    api_key = data["api_key"]
    resp = await client.post(
        "/api/notifications",
        json={
            "type": "discord",
            "name": "Encrypt Test",
            "url": url,
            "events": ["backup.completed"],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    return api_key, resp.json()["id"]


async def test_create_channel_encrypts_url_in_db(client, tmp_path):
    """Creating a channel stores the URL encrypted (enc:v1: prefix) in the DB."""
    import app.core.dependencies as deps_mod
    from app.core.security import is_encrypted

    api_key, channel_id = await _setup_and_create_channel(client)

    # Read raw value from DB
    db_path = deps_mod._config.db_path
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT config FROM notification_channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()

    assert row is not None, "Channel not found in DB"
    config_data = json.loads(row["config"])
    raw_url = config_data.get("url", "")
    assert is_encrypted(raw_url), f"Expected encrypted URL, got: {raw_url!r}"


async def test_list_channel_redacts_url(client):
    """List endpoint shows '••••••' for the URL regardless of plaintext length."""
    api_key, _ = await _setup_and_create_channel(client)

    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    url_in_response = items[0]["config"]["url"]
    assert url_in_response == "••••••", f"Expected redacted URL, got: {url_in_response!r}"


async def test_update_channel_encrypts_new_url(client):
    """Updating a channel with a new URL re-encrypts it in the DB."""
    import app.core.dependencies as deps_mod
    from app.core.security import decrypt_value, is_encrypted

    api_key, channel_id = await _setup_and_create_channel(client)

    resp = await client.put(
        f"/api/notifications/{channel_id}",
        json={"url": _UPDATED_URL},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200

    # Verify DB contains encrypted updated URL
    db_path = deps_mod._config.db_path
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT config FROM notification_channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()

    config_data = json.loads(row["config"])
    raw_url = config_data.get("url", "")
    assert is_encrypted(raw_url), f"Expected encrypted URL after update, got: {raw_url!r}"
    assert decrypt_value(raw_url) == _UPDATED_URL


async def test_test_channel_decrypts_url(client):
    """Test endpoint decrypts the stored URL and passes it to the notifier."""
    api_key, channel_id = await _setup_and_create_channel(client)

    # Inject a mock notifier that captures the URL passed to it
    captured = {}

    async def mock_test_channel(url: str):
        captured["url"] = url
        return {"status": "ok", "message": "Test sent"}

    mock_notifier = MagicMock()
    mock_notifier.test_channel = mock_test_channel

    transport = client._transport
    app = transport.app
    app.state.notifier = mock_notifier

    try:
        resp = await client.post(
            f"/api/notifications/{channel_id}/test",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        # The notifier must receive the plaintext URL, not the encrypted form
        assert captured.get("url") == _WEBHOOK_URL, f"Expected plaintext URL, got: {captured.get('url')!r}"
    finally:
        app.state.notifier = None


async def test_list_redaction_consistent_with_encrypted_storage(client):
    """After encryption, list still shows exactly '••••••' not a partial prefix."""
    api_key, _ = await _setup_and_create_channel(client, url="https://x.com/short")

    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    items = resp.json()["items"]
    url_in_response = items[0]["config"]["url"]
    # Must always be exactly the redaction marker, never partial plaintext
    assert url_in_response == "••••••"
