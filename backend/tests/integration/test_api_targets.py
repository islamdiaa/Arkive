"""
API integration tests for storage target endpoints.

Adapted from Arkive v2 for v3 flat route layout (app.api.targets).
Tests: target CRUD, cloud storage provider validation, config encryption, test-connection.
"""

import os

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# -- Helpers -----------------------------------------------------------------


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


# -- POST /api/targets -------------------------------------------------------


async def test_create_local_target(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    resp = await client.post(
        "/api/targets",
        json={
            "name": "Local Backup",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Local Backup"
    assert data["type"] == "local"
    assert data["config"]["path"] == target_path
    assert "id" in data


async def test_create_local_target_rejects_symlink_to_blocked_system_path(client, tmp_path):
    api_key = await setup_auth(client)
    blocked_link = tmp_path / "sys-link"
    blocked_link.symlink_to("/sys")

    resp = await client.post(
        "/api/targets",
        json={
            "name": "Symlink Trap",
            "type": "local",
            "config": {"path": str(blocked_link)},
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    assert "system directory" in resp.text.lower()


async def test_create_b2_target_with_valid_config(client):
    """B2 target with all required fields should be created."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "B2 Backup",
            "type": "b2",
            "config": {"key_id": "abc", "app_key": "def", "bucket": "my-bucket"},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "b2"


async def test_create_b2_target_trims_whitespace_from_bucket_config(client):
    """Bucket and credential fields should be normalized before persistence."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "B2 Trim",
            "type": "b2",
            "config": {
                "key_id": "  abc  ",
                "app_key": "  def  ",
                "bucket": "  Arkive-backup  ",
            },
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 201, resp.text
    target_id = resp.json()["id"]

    get_resp = await client.get(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert get_resp.status_code == 200
    assert get_resp.json()["config"]["bucket"] == "Arkive-backup"


async def test_create_target_validates_required_fields_b2(client):
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Bad B2",
            "type": "b2",
            "config": {},
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    detail = resp.json().get("detail", resp.json().get("message", ""))
    assert "errors" in detail


async def test_create_target_validates_required_fields_s3(client):
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Bad S3",
            "type": "s3",
            "config": {},
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    detail = resp.json().get("detail", resp.json().get("message", ""))
    assert "errors" in detail


async def test_create_target_rejects_invalid_type(client):
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Bad Type",
            "type": "ftp",
            "config": {},
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    assert "invalid target type" in resp.json().get("detail", resp.json().get("message", "")).lower()


async def test_create_target_rejects_empty_name(client):
    """Target name cannot be empty."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "",
            "type": "local",
            "config": {"path": "/tmp"},
        },
        headers=auth_headers(api_key),
    )
    # Pydantic validation or custom validation should reject
    assert resp.status_code in (422, 400)


async def test_create_wasabi_target_with_valid_config(client):
    """Wasabi target with all required fields should be created."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Wasabi Backup",
            "type": "wasabi",
            "config": {"access_key": "WAK", "secret_key": "WSK", "bucket": "my-bucket"},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "wasabi"


async def test_create_target_validates_required_fields_wasabi(client):
    """Wasabi target without required fields should fail validation."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Bad Wasabi",
            "type": "wasabi",
            "config": {},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    detail = resp.json().get("detail", resp.json().get("message", ""))
    assert "errors" in detail


async def test_create_sftp_target_validates_host(client):
    """SFTP target without host should fail validation."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Bad SFTP",
            "type": "sftp",
            "config": {"user": "backup"},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422


# -- GET /api/targets --------------------------------------------------------


async def test_list_targets_redacts_sensitive_fields(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    # Create a target with a sensitive config field
    await client.post(
        "/api/targets",
        json={
            "name": "S3 Target",
            "type": "s3",
            "config": {
                "endpoint": "https://s3.example.com",
                "access_key": "AKID123",
                "secret_key": "super_secret",
                "bucket": "my-bucket",
            },
        },
        headers=auth_headers(api_key),
    )

    resp = await client.get("/api/targets", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1

    # Find the S3 target
    s3 = next(t for t in data["items"] if t["name"] == "S3 Target")
    # secret_key should be redacted in list view
    assert s3["config"]["secret_key"] == "***"
    # endpoint should NOT be redacted
    assert s3["config"]["endpoint"] == "https://s3.example.com"


async def test_list_targets_returns_total_and_items(client):
    """Target list response should include items and total fields."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/targets", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


# -- GET /api/targets/{id} ---------------------------------------------------


async def test_get_target_returns_full_decrypted_config(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Local Test",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.get(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["config"]["path"] == target_path


async def test_get_nonexistent_target_returns_404(client):
    api_key = await setup_auth(client)
    resp = await client.get("/api/targets/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


# -- PUT /api/targets/{id} ---------------------------------------------------


async def test_update_target_name(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Original Name",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/targets/{target_id}",
        json={
            "name": "Updated Name",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"


async def test_update_target_enabled(client, tmp_path):
    """Should be able to disable a target."""
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Disable Me",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/targets/{target_id}",
        json={
            "enabled": False,
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] == 0 or resp.json()["enabled"] is False


async def test_update_local_target_rejects_symlink_to_blocked_system_path(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)
    blocked_link = tmp_path / "sys-link"
    blocked_link.symlink_to("/sys")

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Safe Local Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/targets/{target_id}",
        json={
            "config": {"path": str(blocked_link)},
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    assert "system directory" in resp.text.lower()


async def test_update_nonexistent_target_returns_404(client):
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/targets/nonexistent",
        json={
            "name": "Ghost",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 404


async def test_update_remote_target_rewrites_live_remote_config(client):
    """Updating a remote target should refresh the live rclone config entry."""
    from unittest.mock import AsyncMock

    api_key = await setup_auth(client)
    app = client._transport.app
    original_cloud_manager = app.state.cloud_manager
    cloud_manager = AsyncMock()
    app.state.cloud_manager = cloud_manager

    try:
        create_resp = await client.post(
            "/api/targets",
            json={
                "name": "SFTP Target",
                "type": "sftp",
                "config": {
                    "host": "example.test",
                    "port": 22,
                    "username": "user",
                    "password": "old-pass",
                    "remote_path": "upload",
                },
            },
            headers=auth_headers(api_key),
        )
        assert create_resp.status_code == 201, create_resp.text
        target_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/api/targets/{target_id}",
            json={
                "config": {
                    "host": "example.test",
                    "port": 22,
                    "username": "user",
                    "password": "new-pass",
                    "remote_path": "upload",
                },
            },
            headers=auth_headers(api_key),
        )
        assert update_resp.status_code == 200, update_resp.text

        cloud_manager.write_target_config.assert_awaited()
        rewritten = cloud_manager.write_target_config.await_args.args[0]
        assert rewritten["id"] == target_id
        assert rewritten["type"] == "sftp"
        assert rewritten["config"]["password"] == "new-pass"
    finally:
        app.state.cloud_manager = original_cloud_manager


# -- DELETE /api/targets/{id} ------------------------------------------------


async def test_delete_target_cascade(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Delete Me",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "deleted" in resp.json()["message"].lower()

    # Verify it's gone
    resp = await client.get(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_delete_nonexistent_target_returns_404(client):
    api_key = await setup_auth(client)
    resp = await client.delete("/api/targets/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


# -- POST /api/targets/{id}/test ---------------------------------------------


async def test_test_local_target(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Test Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.post(f"/api/targets/{target_id}/test", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "accessible" in data["message"].lower() or "writable" in data["message"].lower()


async def test_test_local_target_missing_path_fails(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "missing-backups")

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Missing Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.post(f"/api/targets/{target_id}/test", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "does not exist" in data["message"].lower()
    assert not os.path.exists(target_path)


async def test_test_nonexistent_target_returns_404(client):
    api_key = await setup_auth(client)
    resp = await client.post("/api/targets/nonexistent/test", headers=auth_headers(api_key))
    assert resp.status_code == 404


# -- POST /api/targets/test-connection ----------------------------------------


async def test_test_connection_inline_local(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "test_dest")
    os.makedirs(target_path, exist_ok=True)

    resp = await client.post(
        "/api/targets/test-connection",
        json={
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_test_connection_inline_local_missing_path(client):
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets/test-connection",
        json={
            "type": "local",
            "config": {"path": "/nonexistent/path/12345"},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False


async def test_test_connection_rejects_invalid_type(client):
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/targets/test-connection",
        json={
            "type": "ftp",
            "config": {},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400


# -- GET /api/targets/{id}/usage ---------------------------------------------


async def test_get_local_target_usage(client, tmp_path):
    api_key = await setup_auth(client)
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    create_resp = await client.post(
        "/api/targets",
        json={
            "name": "Usage Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    target_id = create_resp.json()["id"]

    resp = await client.get(f"/api/targets/{target_id}/usage", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_id"] == target_id
    assert "total" in data
    assert "used" in data
    assert "free" in data
