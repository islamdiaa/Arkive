"""
Extended API integration tests for notification endpoints.

Covers: update 404, URL update triggers re-encryption, test failure path.
"""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.core.dependencies import get_config
from app.core.security import decrypt_value
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


def _errmsg(resp) -> str:
    """Extract error message from the standard Arkive error envelope."""
    body = resp.json()
    return body.get("detail", body.get("message", "")).lower()


async def _create_channel(client, api_key, name="TestChannel"):
    """Create a test notification channel and return its ID."""
    resp = await client.post(
        "/api/notifications",
        json={
            "type": "discord",
            "name": name,
            "url": "https://discord.com/api/webhooks/1234567890/original-token",
            "events": ["backup.completed"],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# ===================================================================
# 15. PUT /api/notifications/{id}
# ===================================================================


async def test_update_nonexistent_channel_returns_404(client):
    """PUT /api/notifications/{id} returns 404 for nonexistent channel."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/notifications/nonexistent",
        json={"name": "Updated"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 404
    assert "not found" in _errmsg(resp)


async def test_update_channel_name(client):
    """PUT /api/notifications/{id} successfully updates the name."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    resp = await client.put(
        f"/api/notifications/{channel_id}",
        json={"name": "RenamedChannel"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"

    # Verify the name was actually changed in the DB
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT name FROM notification_channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()
        assert row["name"] == "RenamedChannel"


async def test_update_channel_url_triggers_reencryption(client):
    """PUT /api/notifications/{id} with a new URL re-encrypts it."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    new_url = "https://discord.com/api/webhooks/9999999/new-token-here"
    resp = await client.put(
        f"/api/notifications/{channel_id}",
        json={"url": new_url},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200

    # Verify the URL was re-encrypted in the DB
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT config FROM notification_channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()
        config_data = json.loads(row["config"])
        encrypted_url = config_data["url"]
        # Should be encrypted (starts with enc:v1:)
        assert encrypted_url.startswith("enc:v1:")
        # Decrypted value should match the new URL
        assert decrypt_value(encrypted_url) == new_url


async def test_update_channel_enabled_toggle(client):
    """PUT /api/notifications/{id} can toggle enabled state."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    resp = await client.put(
        f"/api/notifications/{channel_id}",
        json={"enabled": False},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200

    # Verify in DB
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT enabled FROM notification_channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()
        assert row["enabled"] == 0


async def test_update_channel_events(client):
    """PUT /api/notifications/{id} can update event subscriptions."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    new_events = ["backup.failed", "restore.completed"]
    resp = await client.put(
        f"/api/notifications/{channel_id}",
        json={"events": new_events},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200

    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT events FROM notification_channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()
        stored_events = json.loads(row["events"])
        assert stored_events == new_events


# ===================================================================
# 16. POST /api/notifications/{id}/test -- failure path
# ===================================================================


async def test_notification_test_notifier_failure(client):
    """POST /api/notifications/{id}/test when notifier raises returns error."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    mock_notifier = AsyncMock()
    mock_notifier.test_channel = AsyncMock(return_value={"status": "failed", "error": "Connection timed out"})

    app = client._transport.app
    original_notifier = app.state.notifier
    app.state.notifier = mock_notifier

    try:
        resp = await client.post(
            f"/api/notifications/{channel_id}/test",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "timed out" in body["error"].lower()
        mock_notifier.test_channel.assert_called_once()
    finally:
        app.state.notifier = original_notifier


async def test_notification_test_success(client):
    """POST /api/notifications/{id}/test returns success when notifier succeeds."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    mock_notifier = AsyncMock()
    mock_notifier.test_channel = AsyncMock(return_value={"status": "success", "message": "Test notification sent"})

    app = client._transport.app
    original_notifier = app.state.notifier
    app.state.notifier = mock_notifier

    try:
        resp = await client.post(
            f"/api/notifications/{channel_id}/test",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
    finally:
        app.state.notifier = original_notifier


async def test_notification_test_nonexistent_channel(client):
    """POST /api/notifications/{id}/test returns 404 for nonexistent channel."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/notifications/nonexistent/test",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 404
