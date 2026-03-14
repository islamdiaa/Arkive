"""
API integration tests for notification channel endpoints.

Tests: list, create, list after create, URL redaction, update, delete, not found.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


async def _create_channel(client, api_key):
    """Create a test notification channel and return the channel id."""
    resp = await client.post(
        "/api/notifications",
        json={
            "type": "discord",
            "name": "Test",
            "url": "https://discord.com/api/webhooks/1234567890/token-value-here",
            "events": ["backup.completed"],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_list_notifications_empty(client):
    """GET /api/notifications should return empty list after setup."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_create_notification(client):
    """POST /api/notifications should create a channel and return 201."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/notifications",
        json={
            "type": "discord",
            "name": "Test",
            "url": "https://discord.com/api/webhooks/1234567890/token-value-here",
            "events": ["backup.completed"],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["name"] == "Test"


async def test_list_after_create(client):
    """After creating a channel, GET should return 1 item."""
    api_key = await setup_auth(client)
    await _create_channel(client, api_key)

    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


async def test_url_redacted_in_list(client):
    """Notification URLs should be redacted in list responses."""
    api_key = await setup_auth(client)
    await _create_channel(client, api_key)

    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    items = resp.json()["items"]
    url = items[0]["config"]["url"]
    # Long URLs get truncated with dots
    assert url.endswith("\u2022\u2022\u2022\u2022\u2022\u2022")


async def test_update_notification(client):
    """PUT /api/notifications/{id} should update the channel."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    resp = await client.put(
        f"/api/notifications/{channel_id}",
        json={"name": "Updated"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


async def test_delete_notification(client):
    """DELETE /api/notifications/{id} should remove the channel."""
    api_key = await setup_auth(client)
    channel_id = await _create_channel(client, api_key)

    resp = await client.delete(f"/api/notifications/{channel_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Verify it is gone
    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    assert resp.json()["total"] == 0


async def test_notification_not_found(client):
    """POST /api/notifications/{id}/test for nonexistent channel returns 404."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/notifications/nonexistent/test",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 404
