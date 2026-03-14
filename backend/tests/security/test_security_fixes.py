"""
Security tests for audit fixes — command injection, path traversal,
config import bypass, OAuth DoS, input validation, security headers.
"""

import os

import pytest

from tests.conftest import auth_headers, do_setup

# ---------------------------------------------------------------------------
# Command Injection Prevention (db_dumper)
# ---------------------------------------------------------------------------


def test_sanitize_identifier_blocks_injection():
    """_sanitize_identifier should block shell metacharacters."""
    from app.services.db_dumper import DBDumper

    # Valid identifiers
    assert DBDumper._sanitize_identifier("mydb") == "mydb"
    assert DBDumper._sanitize_identifier("my_db-2.0") == "my_db-2.0"
    assert DBDumper._sanitize_identifier("postgres") == "postgres"

    # Invalid — injection attempts
    with pytest.raises(ValueError):
        DBDumper._sanitize_identifier("db; rm -rf /")
    with pytest.raises(ValueError):
        DBDumper._sanitize_identifier("db$(whoami)")
    with pytest.raises(ValueError):
        DBDumper._sanitize_identifier("db`id`")
    with pytest.raises(ValueError):
        DBDumper._sanitize_identifier("db' OR '1'='1")
    with pytest.raises(ValueError):
        DBDumper._sanitize_identifier("")
    with pytest.raises(ValueError):
        DBDumper._sanitize_identifier("user name with spaces")


# ---------------------------------------------------------------------------
# Snapshot ID Validation (backup_engine)
# ---------------------------------------------------------------------------


def test_validate_snapshot_id_accepts_valid():
    """Valid snapshot IDs should pass validation."""
    from app.services.backup_engine import _validate_snapshot_id

    assert _validate_snapshot_id("abc123") == "abc123"
    assert _validate_snapshot_id("latest") == "latest"
    assert _validate_snapshot_id("a1b2c3d4") == "a1b2c3d4"
    assert _validate_snapshot_id("snapshot-with-dashes") == "snapshot-with-dashes"
    assert _validate_snapshot_id("snap_underscores") == "snap_underscores"


def test_validate_snapshot_id_blocks_injection():
    """Invalid snapshot IDs with injection payloads should be rejected."""
    from app.services.backup_engine import _validate_snapshot_id

    with pytest.raises(ValueError):
        _validate_snapshot_id("; rm -rf /")
    with pytest.raises(ValueError):
        _validate_snapshot_id("$(whoami)")
    with pytest.raises(ValueError):
        _validate_snapshot_id("snap id with spaces")
    with pytest.raises(ValueError):
        _validate_snapshot_id("../../../etc/passwd")
    with pytest.raises(ValueError):
        _validate_snapshot_id("")


# ---------------------------------------------------------------------------
# Path Validation (backup_engine)
# ---------------------------------------------------------------------------


def test_validate_path_blocks_traversal():
    """Path traversal sequences should be rejected."""
    from app.services.backup_engine import _validate_path

    # Valid paths
    assert _validate_path("/") == "/"
    assert _validate_path("/home/user") == "/home/user"
    assert _validate_path("some/relative/path") == "some/relative/path"

    # Invalid — traversal
    with pytest.raises(ValueError):
        _validate_path("../../../etc/passwd")
    with pytest.raises(ValueError):
        _validate_path("/safe/../../etc/shadow")
    with pytest.raises(ValueError):
        _validate_path("/../root")


# ---------------------------------------------------------------------------
# Restore Path Validation (model)
# ---------------------------------------------------------------------------


def test_restore_request_validates_restore_to():
    """RestoreRequest should reject dangerous restore_to paths."""
    from app.models.restore import RestoreRequest

    safe_restore_root = os.path.join(os.environ.get("ARKIVE_CONFIG_DIR", "/config"), "restores", "job-1")

    # Valid
    req = RestoreRequest(
        snapshot_id="abc123",
        target="t1",
        paths=["/data"],
        restore_to=safe_restore_root,
    )
    assert req.restore_to == safe_restore_root

    # Reject relative paths
    with pytest.raises(Exception):
        RestoreRequest(
            snapshot_id="abc123",
            target="t1",
            paths=["/data"],
            restore_to="relative/path",
        )

    # Reject system directories
    for blocked in ["/etc", "/etc/shadow", "/usr/bin", "/bin", "/sbin", "/boot", "/proc", "/sys", "/dev", "/root"]:
        with pytest.raises(Exception):
            RestoreRequest(
                snapshot_id="abc123",
                target="t1",
                paths=["/data"],
                restore_to=blocked,
            )

    # Reject paths outside the allowlisted restore roots
    for unsafe in ["/tmp/restore", "/mnt/user/appdata", "/home/ubuntu/restore"]:
        with pytest.raises(Exception):
            RestoreRequest(
                snapshot_id="abc123",
                target="t1",
                paths=["/data"],
                restore_to=unsafe,
            )

    # Reject path traversal in paths
    with pytest.raises(Exception):
        RestoreRequest(
            snapshot_id="abc123",
            target="t1",
            paths=["../../etc/passwd"],
        )

    # Reject missing restore_to entirely for user-initiated restores
    with pytest.raises(Exception):
        RestoreRequest(
            snapshot_id="abc123",
            target="t1",
            paths=["/data"],
        )


def test_restore_request_validates_snapshot_id():
    """RestoreRequest should reject malformed snapshot IDs."""
    from app.models.restore import RestoreRequest

    with pytest.raises(Exception):
        RestoreRequest(
            snapshot_id="; rm -rf /",
            target="t1",
            paths=["/data"],
        )


# ---------------------------------------------------------------------------
# Config Import Security
# ---------------------------------------------------------------------------


async def test_config_import_blocks_protected_settings(client):
    """Config import should not allow overwriting protected settings."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Try importing a YAML that attempts to overwrite api_key_hash
    import yaml

    malicious_config = yaml.dump(
        {
            "arkive_config": {
                "version": 1,
                "settings": [
                    {"key": "api_key_hash", "value": "malicious_hash", "encrypted": False},
                    {"key": "encryption_password", "value": "malicious_pw", "encrypted": True},
                    {"key": "platform", "value": "hacked", "encrypted": False},
                    {"key": "server_name", "value": "test-server", "encrypted": False},
                ],
            }
        }
    )

    resp = await client.post(
        "/api/settings/import",
        content=malicious_config.encode(),
        headers={**auth_headers(api_key), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 200

    # Verify protected settings were NOT overwritten
    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200
    settings = resp.json()

    # api_key should still work (not overwritten with malicious_hash)
    # server_name should be updated (it's writable)
    found_server_name = False
    for item in settings["items"]:
        if item["key"] == "server_name":
            assert item["value"] == "test-server"
            found_server_name = True
    assert found_server_name


# ---------------------------------------------------------------------------
# Local Path Validation for Targets
# ---------------------------------------------------------------------------


async def test_target_local_path_rejects_traversal(client, tmp_path):
    """Local target path with traversal should be rejected."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/targets",
        json={
            "name": "Evil Target",
            "type": "local",
            "config": {"path": "/tmp/../etc"},
        },
        headers=auth_headers(api_key),
    )
    # Should be rejected due to traversal or blocked directory
    assert resp.status_code == 422


async def test_target_local_path_rejects_system_dirs(client):
    """Local target pointing to system directories should be rejected."""
    data = await do_setup(client)
    api_key = data["api_key"]

    for blocked_path in ["/etc", "/usr", "/bin", "/boot", "/proc", "/sys"]:
        resp = await client.post(
            "/api/targets",
            json={
                "name": f"System Target {blocked_path}",
                "type": "local",
                "config": {"path": blocked_path},
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 422, f"Expected 422 for {blocked_path}, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Directory Path Validation
# ---------------------------------------------------------------------------


async def test_directory_rejects_relative_path(client):
    """Adding a directory with relative path should be rejected."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/directories",
        json={
            "path": "relative/path",
            "label": "Evil",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400


async def test_directory_rejects_system_paths(client):
    """Adding system directories should be rejected."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/directories",
        json={
            "path": "/etc",
            "label": "System",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Security Headers
# ---------------------------------------------------------------------------


async def test_security_headers_present(client):
    """All required security headers should be set."""
    resp = await client.get("/api/status")
    assert resp.status_code == 200

    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
    assert "camera=()" in resp.headers.get("Permissions-Policy", "")
    assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")


# ---------------------------------------------------------------------------
# Fernet Keyfile Security
# ---------------------------------------------------------------------------


def test_fernet_keyfile_atomic_creation(tmp_path):
    """Keyfile should be created atomically with correct permissions."""
    import os

    os.environ["ARKIVE_CONFIG_DIR"] = str(tmp_path)

    from app.core.security import _get_fernet, _reset_fernet

    _reset_fernet()
    _get_fernet()

    keyfile = tmp_path / ".keyfile"
    assert keyfile.exists()
    mode = os.stat(keyfile).st_mode & 0o777
    assert mode == 0o600, f"Expected 0600 permissions, got {oct(mode)}"

    # Clean up
    _reset_fernet()
    del os.environ["ARKIVE_CONFIG_DIR"]


# ---------------------------------------------------------------------------
# Error Message Sanitization
# ---------------------------------------------------------------------------


async def test_error_responses_no_internal_details(client):
    """Error responses should not leak internal paths or implementation details."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # 404 response
    resp = await client.get("/api/jobs/nonexistent", headers=auth_headers(api_key))
    body = resp.text
    assert "Traceback" not in body
    assert "/home/" not in body
    assert "File " not in body  # Python stack trace indicator

    # 422 response (validation error)
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "",
            "type": "invalid_type",
            "schedule": "not-cron",
        },
        headers=auth_headers(api_key),
    )
    body = resp.text
    assert "Traceback" not in body


# ---------------------------------------------------------------------------
# XSS in Target Names
# ---------------------------------------------------------------------------


async def test_target_name_sanitized(client, tmp_path):
    """Target names should have HTML tags stripped."""
    data = await do_setup(client)
    api_key = data["api_key"]

    target_path = str(tmp_path / "xss_test")
    os.makedirs(target_path, exist_ok=True)

    resp = await client.post(
        "/api/targets",
        json={
            "name": "<script>alert(1)</script>My Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    # Verify HTML was stripped
    assert "<script>" not in resp.json()["name"]
    assert "alert(1)" in resp.json()["name"]
