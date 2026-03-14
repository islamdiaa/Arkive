"""
Security tests — SQL injection, command injection, path traversal.

Verifies that all user inputs are properly sanitized/parameterized.
"""

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# SQL Injection Tests
# ---------------------------------------------------------------------------


async def test_sql_injection_in_job_name(client):
    """Job name with SQL injection payload should be stored safely."""
    data = await do_setup(client)
    api_key = data["api_key"]

    payload = "'; DROP TABLE backup_jobs; --"
    resp = await client.post(
        "/api/jobs",
        json={
            "name": payload,
            "type": "full",
            "schedule": "0 2 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201

    # Verify the name was stored literally, not executed
    job_id = resp.json()["id"]
    resp = await client.get(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["name"] == payload

    # Verify jobs table still works (wasn't dropped)
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


async def test_sql_injection_in_target_name(client, tmp_path):
    """Target name with UNION SELECT payload should be stored safely."""
    import os

    data = await do_setup(client)
    api_key = data["api_key"]

    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    payload = "test' UNION SELECT * FROM settings --"
    resp = await client.post(
        "/api/targets",
        json={
            "name": payload,
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == payload


async def test_sql_injection_in_log_filter(client):
    """Activity filter with injection should not execute."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Try injecting via level/source params
    resp = await client.get(
        "/api/logs?level=' OR '1'='1",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    # Should return empty since that's not a valid level
    assert resp.json()["total"] == 0


async def test_sql_injection_in_snapshot_id(client):
    """Snapshot ID with injection payload should return 404, not error."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get(
        "/api/snapshots/' OR '1'='1",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 404


async def test_sql_injection_in_settings_key(client):
    """Settings key with injection should be handled safely."""
    data = await do_setup(client)
    api_key = data["api_key"]

    payload = "test'; DROP TABLE settings; --"
    resp = await client.put(
        f"/api/settings/{payload}",
        json={"value": "injected"},
        headers=auth_headers(api_key),
    )
    # Should succeed (stores key literally) or fail gracefully
    assert resp.status_code in (200, 422)

    # Verify settings table still works
    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Path Traversal Tests
# ---------------------------------------------------------------------------


async def test_path_traversal_in_snapshot_browse(client):
    """Snapshot browse with path traversal should not leak files."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get(
        "/api/snapshots/test123/browse?path=../../../etc/passwd",
        headers=auth_headers(api_key),
    )
    # Should be 404 (snapshot doesn't exist), not a file disclosure
    assert resp.status_code == 404


async def test_path_traversal_in_directory(client, tmp_path):
    """Directory creation with traversal should be rejected."""

    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/directories",
        json={
            "path": "../../../etc/shadow",
            "label": "Evil",
        },
        headers=auth_headers(api_key),
    )
    # Should fail — path doesn't exist as a directory
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Input Validation Tests
# ---------------------------------------------------------------------------


async def test_extremely_long_job_name(client):
    """Very long job name should be handled gracefully."""
    data = await do_setup(client)
    api_key = data["api_key"]

    long_name = "A" * 10000
    resp = await client.post(
        "/api/jobs",
        json={
            "name": long_name,
            "type": "full",
            "schedule": "0 2 * * *",
        },
        headers=auth_headers(api_key),
    )
    # Should either accept or reject gracefully (not crash)
    assert resp.status_code in (201, 422)


async def test_unicode_in_job_name(client):
    """Unicode/emoji in job names should work."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Backup Job 🚀",
            "type": "full",
            "schedule": "0 2 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    resp = await client.get(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert "🚀" in resp.json()["name"]


async def test_invalid_cron_expression(client):
    """Invalid cron expression should be rejected."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get(
        "/api/settings/cron-preview?cron=not-a-cron",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400


async def test_null_in_required_fields(client):
    """Null values in required fields should be rejected."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.post(
        "/api/jobs",
        json={
            "name": None,
            "type": "full",
            "schedule": "0 2 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422


async def test_numeric_where_uuid_expected(client):
    """Numeric string for job ID should return 404, not crash."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/jobs/12345", headers=auth_headers(api_key))
    assert resp.status_code == 404
