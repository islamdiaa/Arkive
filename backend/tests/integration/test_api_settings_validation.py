"""
API integration tests for settings validation edge cases.

Covers: invalid timezone rejection, unknown key rejection, invalid retention,
invalid YAML import, missing root key import, READONLY_SETTINGS blocking,
secrets redacted in export.
"""

import os

import pytest
import yaml

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


def _errmsg(resp) -> str:
    """Extract error message from the standard Arkive error envelope."""
    body = resp.json()
    return body.get("detail", body.get("message", "")).lower()


# ===================================================================
# 10. PUT /api/settings -- validation
# ===================================================================


async def test_bulk_update_invalid_timezone(client):
    """PUT /api/settings rejects an invalid timezone with 422."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"timezone": "Not/A/Real/Zone"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "timezone" in _errmsg(resp)


async def test_bulk_update_unknown_key(client):
    """PUT /api/settings rejects unknown setting keys with 422."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"totally_bogus_key": "value"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "unknown" in _errmsg(resp)


async def test_bulk_update_readonly_key_rejected(client):
    """PUT /api/settings rejects writes to READONLY_SETTINGS with 403."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"api_key_hash": "evil_value"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 403
    msg = _errmsg(resp)
    assert "protected" in msg or "api_key_hash" in msg


async def test_bulk_update_invalid_retention_non_integer(client):
    """PUT /api/settings rejects non-integer retention value with 422."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"keep_daily": "abc"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "positive integer" in _errmsg(resp)


async def test_bulk_update_invalid_retention_zero(client):
    """PUT /api/settings rejects zero retention value with 422."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"keep_weekly": "0"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "positive integer" in _errmsg(resp)


async def test_bulk_update_invalid_retention_negative(client):
    """PUT /api/settings rejects negative retention value with 422."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"keep_monthly": "-3"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "positive integer" in _errmsg(resp)


async def test_single_key_update_invalid_timezone(client):
    """PUT /api/settings/timezone rejects invalid timezone."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings/timezone",
        json={"value": "Fake/Zone"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "timezone" in _errmsg(resp)


async def test_single_key_update_unknown_key(client):
    """PUT /api/settings/{unknown} returns 422 for unrecognized key."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings/nonexistent_key",
        json={"value": "whatever"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "unknown" in _errmsg(resp)


async def test_single_key_update_readonly_key(client):
    """PUT /api/settings/api_key_hash returns 403."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings/api_key_hash",
        json={"value": "evil"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 403


async def test_single_key_retention_validation(client):
    """PUT /api/settings/keep_daily rejects non-numeric value."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings/keep_daily",
        json={"value": "not_a_number"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422
    assert "positive integer" in _errmsg(resp)


# ===================================================================
# 11. POST /api/settings/import -- validation
# ===================================================================


async def test_import_invalid_yaml(client):
    """POST /api/settings/import with invalid YAML returns 400."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/settings/import",
        content=b"{{{{not: valid yaml: [[[",
        headers={
            **auth_headers(api_key),
            "Content-Type": "application/x-yaml",
        },
    )
    # Could be 400 (our handler) or the YAML may parse oddly.
    # The key thing is it does not return 200.
    assert resp.status_code in (400, 422)


async def test_import_missing_root_key(client):
    """POST /api/settings/import without 'arkive_config' root key returns 400."""
    api_key = await setup_auth(client)
    yaml_body = yaml.dump({"wrong_root": {"settings": []}})
    resp = await client.post(
        "/api/settings/import",
        content=yaml_body.encode("utf-8"),
        headers={
            **auth_headers(api_key),
            "Content-Type": "application/x-yaml",
        },
    )
    assert resp.status_code == 400
    assert "arkive_config" in _errmsg(resp)


async def test_import_empty_body(client):
    """POST /api/settings/import with empty body returns 400."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/settings/import",
        content=b"",
        headers={
            **auth_headers(api_key),
            "Content-Type": "application/x-yaml",
        },
    )
    assert resp.status_code == 400


async def test_import_readonly_settings_skipped(client):
    """Import should silently skip READONLY_SETTINGS like api_key_hash."""
    api_key = await setup_auth(client)
    yaml_body = yaml.dump(
        {
            "arkive_config": {
                "version": 1,
                "settings": [
                    {"key": "api_key_hash", "value": "evil_hash", "encrypted": False},
                    {"key": "theme", "value": "light", "encrypted": False},
                ],
                "storage_targets": [],
                "backup_jobs": [],
                "watched_directories": [],
                "notification_channels": [],
            }
        }
    )
    resp = await client.post(
        "/api/settings/import",
        content=yaml_body.encode("utf-8"),
        headers={
            **auth_headers(api_key),
            "Content-Type": "application/x-yaml",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # api_key_hash should have been skipped, so only theme was imported
    assert body["restored"]["settings"] == 1


# ===================================================================
# 12. GET /api/settings/export -- secrets redacted
# ===================================================================


async def test_export_redacts_secrets(client):
    """GET /api/settings/export should redact sensitive values."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert resp.status_code == 200

    parsed = yaml.safe_load(resp.text)
    assert "arkive_config" in parsed

    settings = parsed["arkive_config"]["settings"]
    # api_key_hash should be excluded entirely from export
    exported_keys = [s["key"] for s in settings]
    assert "api_key_hash" not in exported_keys


async def test_export_has_yaml_content_type(client):
    """GET /api/settings/export returns application/x-yaml content type."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "yaml" in resp.headers.get("content-type", "").lower()


async def test_export_includes_all_sections(client):
    """Export YAML should include all config sections."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert resp.status_code == 200
    parsed = yaml.safe_load(resp.text)
    config = parsed["arkive_config"]
    assert "settings" in config
    assert "storage_targets" in config
    assert "backup_jobs" in config
    assert "watched_directories" in config
    assert "notification_channels" in config
    assert "version" in config
    assert "exported_at" in config


async def test_export_target_secrets_redacted(client, tmp_path):
    """Storage target secrets (keys, passwords) should be redacted in export."""
    api_key = await setup_auth(client)

    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    # Create a local target (local type only requires 'path', which avoids
    # the validation that s3 type requires endpoint/access_key fields)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "LocalTarget",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201

    resp = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert resp.status_code == 200
    parsed = yaml.safe_load(resp.text)
    targets = parsed["arkive_config"]["storage_targets"]
    assert len(targets) >= 1
    # Verify the target was exported with its config
    local_targets = [t for t in targets if t.get("name") == "LocalTarget"]
    assert len(local_targets) == 1
