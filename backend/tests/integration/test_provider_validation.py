"""
Integration tests for provider-specific credential validation.

Tests every storage provider's required/optional config fields through the
POST /api/targets endpoint, verifying the _validate_provider_config logic
rejects incomplete configs and accepts valid ones.

The app registers a custom exception handler that wraps HTTPException.detail
into: {"error": "<code>", "message": "<str(detail)>", "details": {}}
So validation errors appear in the 'message' field as a stringified dict.
"""

import os

import pytest
import pytest_asyncio

from tests.conftest import auth_headers, build_test_client, do_setup

pytestmark = pytest.mark.asyncio


# -- Helpers ----------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def provider_client(tmp_path_factory):
    """Reuse one initialized client for the whole provider-validation module."""
    config_dir = tmp_path_factory.mktemp("provider-validation")
    async with build_test_client(config_dir) as client:
        data = await do_setup(client)
        yield client, data["api_key"]


async def create_target(client, api_key, name, target_type, config):
    """Helper to POST /api/targets and return the response."""
    return await client.post(
        "/api/targets",
        json={"name": name, "type": target_type, "config": config},
        headers=auth_headers(api_key),
    )


def get_error_message(resp) -> str:
    """Extract the error message string from the response.

    The custom exception handler serializes HTTPException.detail into
    the 'message' field as a string.  For 422 validation errors this
    looks like: "{'errors': ['...'], 'message': 'Validation failed'}"
    """
    body = resp.json()
    return body.get("message", "") or str(body.get("detail", ""))


# ===========================================================================
# B2 Validation
# ===========================================================================


async def test_b2_missing_key_id(provider_client):
    """B2 config without key_id returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "B2 No KeyID",
        "b2",
        {
            "app_key": "secret",
            "bucket": "my-bucket",
        },
    )
    assert resp.status_code == 422
    assert "Application Key ID is required" in get_error_message(resp)


async def test_b2_missing_app_key(provider_client):
    """B2 config without application_key returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "B2 No AppKey",
        "b2",
        {
            "key_id": "keyid123",
            "bucket": "my-bucket",
        },
    )
    assert resp.status_code == 422
    assert "Application Key is required" in get_error_message(resp)


async def test_b2_missing_bucket(provider_client):
    """B2 config without bucket returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "B2 No Bucket",
        "b2",
        {
            "key_id": "keyid123",
            "app_key": "secret",
        },
    )
    assert resp.status_code == 422
    assert "Bucket name is required" in get_error_message(resp)


async def test_b2_valid_config_accepted(provider_client):
    """B2 with all required fields should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "B2 Valid",
        "b2",
        {
            "key_id": "keyid123",
            "app_key": "secret123",
            "bucket": "my-bucket",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "b2"


async def test_b2_empty_strings_rejected(provider_client):
    """B2 config with empty string values should be rejected (422)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "B2 Empty",
        "b2",
        {
            "key_id": "",
            "app_key": "",
            "bucket": "",
        },
    )
    assert resp.status_code == 422
    msg = get_error_message(resp)
    # All three required fields should be flagged
    assert "Application Key ID is required" in msg
    assert "Application Key is required" in msg
    assert "Bucket name is required" in msg


# ===========================================================================
# S3 Validation
# ===========================================================================


async def test_s3_missing_access_key(provider_client):
    """S3 config without access_key returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "S3 No AK",
        "s3",
        {
            "secret_key": "sk",
            "bucket": "b",
            "endpoint": "https://s3.example.com",
        },
    )
    assert resp.status_code == 422
    assert "Access Key is required" in get_error_message(resp)


async def test_s3_missing_secret_key(provider_client):
    """S3 config without secret_key returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "S3 No SK",
        "s3",
        {
            "access_key": "ak",
            "bucket": "b",
            "endpoint": "https://s3.example.com",
        },
    )
    assert resp.status_code == 422
    assert "Secret Key is required" in get_error_message(resp)


async def test_s3_missing_bucket(provider_client):
    """S3 config without bucket returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "S3 No Bucket",
        "s3",
        {
            "access_key": "ak",
            "secret_key": "sk",
            "endpoint": "https://s3.example.com",
        },
    )
    assert resp.status_code == 422
    assert "Bucket name is required" in get_error_message(resp)


async def test_s3_valid_config_accepted(provider_client):
    """S3 with all required fields should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "S3 Valid",
        "s3",
        {
            "access_key": "AKID123",
            "secret_key": "secret123",
            "bucket": "my-bucket",
            "endpoint": "https://s3.amazonaws.com",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "s3"


async def test_s3_endpoint_is_required(provider_client):
    """S3 requires an endpoint URL per the current validation logic."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "S3 No Endpoint",
        "s3",
        {
            "access_key": "ak",
            "secret_key": "sk",
            "bucket": "b",
        },
    )
    assert resp.status_code == 422
    assert "Endpoint URL is required" in get_error_message(resp)


async def test_s3_region_is_optional(provider_client):
    """S3 region field is not validated (optional), only endpoint/access/secret/bucket."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "S3 No Region",
        "s3",
        {
            "access_key": "ak",
            "secret_key": "sk",
            "bucket": "b",
            "endpoint": "https://s3.us-east-1.amazonaws.com",
            # No region field -- should still succeed
        },
    )
    assert resp.status_code == 201


# ===========================================================================
# Wasabi Validation
# ===========================================================================


async def test_wasabi_missing_access_key(provider_client):
    """Wasabi config without access_key returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Wasabi No AK",
        "wasabi",
        {
            "secret_key": "sk",
            "bucket": "b",
        },
    )
    assert resp.status_code == 422
    assert "Access Key is required" in get_error_message(resp)


async def test_wasabi_missing_secret_key(provider_client):
    """Wasabi config without secret_key returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Wasabi No SK",
        "wasabi",
        {
            "access_key": "ak",
            "bucket": "b",
        },
    )
    assert resp.status_code == 422
    assert "Secret Key is required" in get_error_message(resp)


async def test_wasabi_missing_bucket(provider_client):
    """Wasabi config without bucket returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Wasabi No Bucket",
        "wasabi",
        {
            "access_key": "ak",
            "secret_key": "sk",
        },
    )
    assert resp.status_code == 422
    assert "Bucket name is required" in get_error_message(resp)


async def test_wasabi_valid_config_accepted(provider_client):
    """Wasabi with all required fields should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Wasabi Valid",
        "wasabi",
        {
            "access_key": "WAK",
            "secret_key": "WSK",
            "bucket": "my-bucket",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "wasabi"


async def test_wasabi_default_region(provider_client):
    """Wasabi config without explicit region should still be accepted.

    The default region 'us-east-1' is applied at the rclone config layer,
    not at API validation. Validation only checks access_key/secret_key/bucket.
    """
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Wasabi DefReg",
        "wasabi",
        {
            "access_key": "WAK",
            "secret_key": "WSK",
            "bucket": "my-bucket",
            # No region -- default us-east-1 applied by CloudManager._wasabi_section
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "wasabi"


# ===========================================================================
# SFTP Validation
# ===========================================================================


async def test_sftp_missing_host(provider_client):
    """SFTP config without host returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "SFTP No Host",
        "sftp",
        {
            "user": "backup",
        },
    )
    assert resp.status_code == 422
    assert "Host is required" in get_error_message(resp)


async def test_sftp_missing_username(provider_client):
    """SFTP config without user returns 422."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "SFTP No User",
        "sftp",
        {
            "host": "backup.example.com",
        },
    )
    assert resp.status_code == 422
    assert "Username is required" in get_error_message(resp)


async def test_sftp_valid_with_password(provider_client):
    """SFTP with host, user, and password should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "SFTP Pass",
        "sftp",
        {
            "host": "backup.example.com",
            "user": "backupuser",
            "password": "secret",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "sftp"


async def test_sftp_valid_with_key(provider_client):
    """SFTP with host, user, and key_file should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "SFTP Key",
        "sftp",
        {
            "host": "backup.example.com",
            "user": "backupuser",
            "key_file": "/home/user/.ssh/id_rsa",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "sftp"


async def test_sftp_port_defaults(provider_client):
    """SFTP config without port should still be accepted.

    Default port 22 is applied at the rclone config layer, not at API
    validation. Validation only checks host and user.
    """
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "SFTP DefPort",
        "sftp",
        {
            "host": "backup.example.com",
            "user": "backupuser",
            # No port -- default 22 applied by CloudManager._sftp_section
        },
    )
    assert resp.status_code == 201


# ===========================================================================
# Dropbox Validation
# ===========================================================================


async def test_dropbox_missing_token(provider_client):
    """Dropbox config without token returns 422."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "Dropbox No Token", "dropbox", {})
    assert resp.status_code == 422
    assert "Access token is required" in get_error_message(resp)


async def test_dropbox_valid_token_accepted(provider_client):
    """Dropbox with token should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Dropbox Valid",
        "dropbox",
        {
            "token": "sl.fake-dropbox-token",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "dropbox"


# ===========================================================================
# Google Drive Validation
# ===========================================================================


async def test_gdrive_missing_client_id(provider_client):
    """Google Drive config without client_id returns 422."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "GDrive No CID", "gdrive", {})
    assert resp.status_code == 422
    assert "Client ID is required" in get_error_message(resp)


async def test_gdrive_valid_config_accepted(provider_client):
    """Google Drive with client_id should be accepted (201)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "GDrive Valid",
        "gdrive",
        {
            "client_id": "123456.apps.googleusercontent.com",
            "token": "ya29.fake-token",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "gdrive"


# ===========================================================================
# Local Path Validation
# ===========================================================================


async def test_local_missing_path(provider_client):
    """Local config without path returns 422."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "Local No Path", "local", {})
    assert resp.status_code == 422
    assert "Path is required" in get_error_message(resp)


async def test_local_relative_path_rejected(provider_client):
    """Local config with relative path should be rejected (422)."""
    client, api_key = provider_client
    resp = await create_target(
        client,
        api_key,
        "Local Rel Path",
        "local",
        {
            "path": "relative/path/here",
        },
    )
    assert resp.status_code == 422
    msg = get_error_message(resp)
    assert "absolute" in msg.lower() or "relative" in msg.lower() or "traversal" in msg.lower()


async def test_local_valid_path(provider_client, tmp_path):
    """Local config with valid absolute path should be accepted (201)."""
    client, api_key = provider_client
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    resp = await create_target(
        client,
        api_key,
        "Local Valid",
        "local",
        {
            "path": target_path,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "local"
    assert resp.json()["config"]["path"] == target_path


# ===========================================================================
# Cross-cutting validation tests
# ===========================================================================


async def test_invalid_type_rejected(provider_client):
    """Unknown provider type returns 422."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "BadType", "ftp", {})
    assert resp.status_code == 422
    msg = get_error_message(resp)
    assert "invalid target type" in msg.lower()


async def test_empty_name_rejected(provider_client):
    """Target with empty name is rejected by Pydantic min_length=1 (422)."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "", "local", {"path": "/tmp"})
    assert resp.status_code == 422


async def test_whitespace_only_name_rejected(provider_client):
    """Target with whitespace-only name is rejected (422)."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "   ", "local", {"path": "/tmp"})
    assert resp.status_code == 422


async def test_multiple_missing_fields_returns_all_errors(provider_client):
    """When multiple fields are missing, all errors are returned."""
    client, api_key = provider_client
    resp = await create_target(client, api_key, "B2 All Missing", "b2", {})
    assert resp.status_code == 422
    msg = get_error_message(resp)
    # B2 requires key_id, app_key, bucket -- all three should be reported
    assert "Application Key ID is required" in msg
    assert "Application Key is required" in msg
    assert "Bucket name is required" in msg
