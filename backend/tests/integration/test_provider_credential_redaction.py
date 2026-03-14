"""
Integration tests for credential redaction across all provider types.

Verifies that GET /api/targets properly redacts sensitive fields
(keys, secrets, passwords, tokens) while preserving non-sensitive
fields (bucket names, regions, hosts, paths, endpoints).
"""

import os

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


# ---------------------------------------------------------------------------
# Provider configs: each entry defines the config to create and expectations
# for which fields should be redacted vs. preserved.
# ---------------------------------------------------------------------------

PROVIDER_CONFIGS = {
    "b2": {
        "config": {
            "key_id": "B2_KEY_ID_abc123",
            "app_key": "B2_APP_KEY_secret456",
            "bucket": "my-b2-bucket",
            "region": "us-west-001",
        },
        "redacted_fields": ["key_id", "app_key"],
        "visible_fields": {"bucket": "my-b2-bucket", "region": "us-west-001"},
    },
    "s3": {
        "config": {
            "endpoint": "https://s3.amazonaws.com",
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "bucket": "my-s3-bucket",
            "region": "us-east-1",
        },
        "redacted_fields": ["access_key", "secret_key"],
        "visible_fields": {
            "endpoint": "https://s3.amazonaws.com",
            "bucket": "my-s3-bucket",
            "region": "us-east-1",
        },
    },
    "wasabi": {
        "config": {
            "access_key": "WASABI_ACCESS_KEY_123",
            "secret_key": "WASABI_SECRET_KEY_456",
            "bucket": "my-wasabi-bucket",
            "region": "us-east-2",
            "endpoint": "https://s3.wasabisys.com",
        },
        "redacted_fields": ["access_key", "secret_key"],
        "visible_fields": {
            "bucket": "my-wasabi-bucket",
            "region": "us-east-2",
            "endpoint": "https://s3.wasabisys.com",
        },
    },
    "sftp": {
        "config": {
            "host": "backup.example.com",
            "port": 22,
            "user": "backupuser",
            "password": "super_secret_password",
            "path": "/mnt/backups",
        },
        "redacted_fields": ["password"],
        "visible_fields": {
            "host": "backup.example.com",
            "port": 22,
            "user": "backupuser",
            "path": "/mnt/backups",
        },
    },
    "dropbox": {
        "config": {
            "token": "sl.DROPBOX_ACCESS_TOKEN_ABCDEF",
            "refresh_token": "DROPBOX_REFRESH_TOKEN_XYZ",
            "folder": "/Backups/Arkive",
        },
        "redacted_fields": ["token", "refresh_token"],
        "visible_fields": {"folder": "/Backups/Arkive"},
    },
    "gdrive": {
        "config": {
            "client_id": "123456789.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret-value",
            "token": "ya29.gdrive_access_token",
            "refresh_token": "1//gdrive_refresh_token",
            "folder_id": "1AbCdEfGhIjKlMnOp",
        },
        "redacted_fields": ["client_secret", "token", "refresh_token"],
        "visible_fields": {
            "client_id": "123456789.apps.googleusercontent.com",
            "folder_id": "1AbCdEfGhIjKlMnOp",
        },
    },
}

# Local target needs a real directory path, handled separately
LOCAL_CONFIG = {
    "config_fn": lambda tmp_path: {
        "path": str(tmp_path / "local_backups"),
        "secret_key": "should_be_redacted",
    },
    "redacted_fields": ["secret_key"],
    "visible_fields_fn": lambda tmp_path: {
        "path": str(tmp_path / "local_backups"),
    },
}


# ---------------------------------------------------------------------------
# Parametrized tests for non-local providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    [
        "b2",
        "s3",
        "wasabi",
        "sftp",
        "dropbox",
        "gdrive",
    ],
)
async def test_list_redacts_sensitive_fields(client, provider):
    """Sensitive config fields are masked with '***' in list response."""
    api_key = await setup_auth(client)
    prov_spec = PROVIDER_CONFIGS[provider]

    # Create target
    create_resp = await client.post(
        "/api/targets",
        json={
            "name": f"Redact Test {provider}",
            "type": provider,
            "config": prov_spec["config"],
        },
        headers=auth_headers(api_key),
    )
    assert create_resp.status_code == 201, f"Failed to create {provider} target: {create_resp.text}"

    # List targets and find ours
    list_resp = await client.get("/api/targets", headers=auth_headers(api_key))
    assert list_resp.status_code == 200
    targets = list_resp.json()["items"]
    target = next(
        (t for t in targets if t["name"] == f"Redact Test {provider}"),
        None,
    )
    assert target is not None, f"Target 'Redact Test {provider}' not found in list"

    config = target["config"]

    # Verify sensitive fields are redacted
    for field in prov_spec["redacted_fields"]:
        assert config.get(field) == "***", (
            f"Provider {provider}: field '{field}' should be redacted but got '{config.get(field)}'"
        )

    # Verify non-sensitive fields are NOT redacted
    for field, expected_value in prov_spec["visible_fields"].items():
        assert config.get(field) == expected_value, (
            f"Provider {provider}: field '{field}' should be '{expected_value}' but got '{config.get(field)}'"
        )


async def test_list_redacts_local_target_sensitive_fields(client, tmp_path):
    """Local target sensitive fields are redacted, path is preserved."""
    api_key = await setup_auth(client)
    local_path = str(tmp_path / "local_backups")
    os.makedirs(local_path, exist_ok=True)

    config = {
        "path": local_path,
        "secret_key": "should_be_redacted",
    }

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Redact Test local",
            "type": "local",
            "config": config,
        },
        headers=auth_headers(api_key),
    )
    assert create_resp.status_code == 201

    list_resp = await client.get("/api/targets", headers=auth_headers(api_key))
    assert list_resp.status_code == 200
    targets = list_resp.json()["items"]
    target = next(
        (t for t in targets if t["name"] == "Redact Test local"),
        None,
    )
    assert target is not None

    # secret_key should be redacted
    assert target["config"]["secret_key"] == "***"
    # path should be visible
    assert target["config"]["path"] == local_path


# ---------------------------------------------------------------------------
# Redaction logic edge cases
# ---------------------------------------------------------------------------


async def test_redaction_is_case_insensitive_for_field_names(client):
    """Fields with 'Key', 'SECRET', 'Token', 'Password' in any case are redacted."""
    api_key = await setup_auth(client)

    config = {
        "key_id": "should_redact",
        "app_key": "should_redact",
        "bucket": "visible-bucket",
        "API_KEY_EXTRA": "should_redact",
    }

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Case Sensitivity Test",
            "type": "b2",
            "config": config,
        },
        headers=auth_headers(api_key),
    )
    assert create_resp.status_code == 201

    list_resp = await client.get("/api/targets", headers=auth_headers(api_key))
    targets = list_resp.json()["items"]
    target = next(t for t in targets if t["name"] == "Case Sensitivity Test")

    # All fields with "key" should be redacted (case-insensitive)
    assert target["config"]["key_id"] == "***"
    assert target["config"]["app_key"] == "***"
    assert target["config"]["API_KEY_EXTRA"] == "***"
    # bucket is safe
    assert target["config"]["bucket"] == "visible-bucket"


async def test_get_single_target_also_redacts_sensitive_fields(client):
    """GET /api/targets/{id} also redacts sensitive config fields."""
    api_key = await setup_auth(client)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Single Get Redaction Test",
            "type": "b2",
            "config": {"key_id": "real_key_id", "app_key": "real_app_key", "bucket": "bkt"},
        },
        headers=auth_headers(api_key),
    )
    assert create_resp.status_code == 201
    target_id = create_resp.json()["id"]

    # Single target GET also redacts sensitive fields
    get_resp = await client.get(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert get_resp.status_code == 200
    config = get_resp.json()["config"]
    # Sensitive fields containing "key" are redacted
    assert config["key_id"] == "***"
    assert config["app_key"] == "***"
    # Non-sensitive fields are preserved
    assert config["bucket"] == "bkt"


async def test_redaction_does_not_affect_non_credential_fields(client, tmp_path):
    """Fields like endpoint, host, user, path, region, bucket are never redacted."""
    api_key = await setup_auth(client)

    non_sensitive_fields = {
        "endpoint": "https://s3.example.com",
        "bucket": "test-bucket",
        "region": "eu-west-1",
        "host": "backup.example.com",
        "user": "backupuser",
        "path": "/data/backups",
        "folder": "/My Folder",
        "port": 2222,
    }
    # Add one required sensitive field so creation passes validation
    config = {**non_sensitive_fields, "access_key": "AK", "secret_key": "SK"}

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Non-sensitive Fields Test",
            "type": "s3",
            "config": config,
        },
        headers=auth_headers(api_key),
    )
    assert create_resp.status_code == 201

    list_resp = await client.get("/api/targets", headers=auth_headers(api_key))
    targets = list_resp.json()["items"]
    target = next(t for t in targets if t["name"] == "Non-sensitive Fields Test")

    for field, expected in non_sensitive_fields.items():
        assert target["config"].get(field) == expected, (
            f"Non-sensitive field '{field}' should be '{expected}' but got '{target['config'].get(field)}'"
        )
