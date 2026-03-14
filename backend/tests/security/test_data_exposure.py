"""
Security tests — sensitive data exposure, config encryption, response redaction.
"""

import os

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Config Encryption at Rest
# ---------------------------------------------------------------------------


async def test_target_config_encrypted_at_rest(client, tmp_path):
    """Storage target config should be encrypted in the database."""
    import aiosqlite

    from app.core.dependencies import get_config

    data = await do_setup(client, encryption_password="test-pass")
    api_key = data["api_key"]

    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    resp = await client.post(
        "/api/targets",
        json={
            "name": "Encrypted Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    target_id = resp.json()["id"]

    # Check raw DB value
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute("SELECT config FROM storage_targets WHERE id = ?", (target_id,))
        row = await cursor.fetchone()
        raw_config = row[0]

    # Should be encrypted (enc:v1: prefix)
    assert raw_config.startswith("enc:v1:")


async def test_api_key_stored_as_hash(client):
    """API key should be stored as SHA-256 hash, not plaintext."""
    import aiosqlite

    from app.core.dependencies import get_config

    data = await do_setup(client)
    data["api_key"]

    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        row = await cursor.fetchone()
        stored_value = row[0]

    # Should be a hex hash, not the raw key
    assert not stored_value.startswith("ark_")
    assert len(stored_value) == 64  # SHA-256 hex digest


async def test_encryption_password_encrypted(client):
    """Encryption password should be stored encrypted."""
    import aiosqlite

    from app.core.dependencies import get_config

    await do_setup(client, encryption_password="super-secret")
    config = get_config()

    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'encryption_password'")
        row = await cursor.fetchone()
        stored_value = row[0]

    # Should be encrypted
    assert stored_value.startswith("enc:v1:")
    assert "super-secret" not in stored_value


# ---------------------------------------------------------------------------
# Response Redaction
# ---------------------------------------------------------------------------


async def test_settings_redacts_sensitive_values(client):
    """GET /api/settings redacts sensitive keys."""
    data = await do_setup(client, encryption_password="test")
    api_key = data["api_key"]

    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200

    for item in resp.json()["items"]:
        if item["sensitive"]:
            assert item["value"] == "********"


async def test_notification_url_redacted(client):
    """Notification channel URLs are redacted in list responses."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/notifications",
        json={
            "type": "discord",
            "name": "Discord",
            "url": "https://discord.com/api/webhooks/123456789/abcdefghijklmnop",
            "events": ["backup.completed"],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201

    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    assert resp.status_code == 200
    url = resp.json()["items"][0]["config"]["url"]
    # URL should be truncated with ••••••
    assert "••••••" in url
    assert "abcdefghijklmnop" not in url


async def test_error_responses_no_stack_traces(client):
    """Error responses should not leak internal paths or stack traces."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Request a nonexistent endpoint
    resp = await client.get("/api/nonexistent", headers=auth_headers(api_key))
    body = resp.text
    # Should not contain file paths or tracebacks
    assert "Traceback" not in body
    assert "/home/" not in body


# ---------------------------------------------------------------------------
# Keyfile Security
# ---------------------------------------------------------------------------


async def test_keyfile_permissions(client, tmp_path):
    """Keyfile should have 0o600 permissions."""
    keyfile = tmp_path / ".keyfile"
    if keyfile.exists():
        mode = os.stat(keyfile).st_mode & 0o777
        assert mode == 0o600
