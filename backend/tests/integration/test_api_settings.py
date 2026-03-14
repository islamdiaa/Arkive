"""
API integration tests for settings endpoints.

Tests: get, bulk update, single key update, forbidden key, reset, export, import, cron preview.
"""

import pytest
import yaml

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


async def test_get_settings_after_setup(client):
    """GET /api/settings should return items array including setup_completed_at."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    keys = [item["key"] for item in body["items"]]
    assert "setup_completed_at" in keys


async def test_bulk_update(client):
    """PUT /api/settings should update multiple settings."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"theme": "dark", "log_level": "DEBUG"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["total"] == 2


async def test_bulk_update_retention_days_round_trip(client):
    """PUT /api/settings should accept retention_days and return it on GET."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings",
        json={"settings": {"retention_days": 45}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200

    get_resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert get_resp.status_code == 200
    assert get_resp.json()["retention_days"] == 45


async def test_single_key_update(client):
    """PUT /api/settings/{key} should update a specific setting."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings/theme",
        json={"value": "light"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "theme"
    assert body["value"] == "light"


async def test_cannot_update_api_key_hash(client):
    """PUT /api/settings/api_key_hash should return 403."""
    api_key = await setup_auth(client)
    resp = await client.put(
        "/api/settings/api_key_hash",
        json={"value": "evil"},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 403
    assert "api_key_hash" in resp.json().get("detail", resp.json().get("message", "")).lower()


async def test_reset_settings(client):
    """POST /api/settings/reset with confirm=true should reset settings."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/settings/reset",
        json={"confirm": True},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "reset" in body["message"].lower()
    assert "api_key_hash" in body["preserved"]


async def test_reset_without_confirm(client):
    """POST /api/settings/reset with confirm=false should return 400."""
    api_key = await setup_auth(client)
    resp = await client.post(
        "/api/settings/reset",
        json={"confirm": False},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400
    assert "confirm" in resp.json().get("detail", resp.json().get("message", "")).lower()


async def test_export_config(client):
    """GET /api/settings/export should return YAML content."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "yaml" in resp.headers.get("content-type", "").lower()
    # Validate the body is parseable YAML
    parsed = yaml.safe_load(resp.text)
    assert "arkive_config" in parsed


async def test_import_config(client):
    """POST /api/settings/import with YAML body should import settings."""
    api_key = await setup_auth(client)
    yaml_body = yaml.dump(
        {
            "arkive_config": {
                "version": 1,
                "settings": [
                    {"key": "theme", "value": "dark", "encrypted": False},
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
    assert "restored" in body
    assert body["restored"]["settings"] >= 1


async def test_cron_preview(client):
    """GET /api/settings/cron-preview with valid cron should return next_runs."""
    api_key = await setup_auth(client)
    resp = await client.get(
        "/api/settings/cron-preview?cron=0 2 * * *",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "next_runs" in body
    assert len(body["next_runs"]) == 3
    assert body["cron"] == "0 2 * * *"


async def test_cron_preview_invalid(client):
    """GET /api/settings/cron-preview with invalid cron should return 400."""
    api_key = await setup_auth(client)
    resp = await client.get(
        "/api/settings/cron-preview?cron=invalid",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400
    assert "invalid" in resp.json().get("detail", resp.json().get("message", "")).lower()
