"""
Extended end-to-end journey tests — multi-step user workflows.

Tests simulate complete user journeys across multiple endpoints.
"""

import json
import os

import pytest
import pytest_asyncio

from tests.conftest import auth_headers, build_test_client, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_with_target(client, tmp_path):
    """Run setup and create a local storage target."""
    data = await do_setup(client, encryption_password="test-password")
    api_key = data["api_key"]

    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)

    resp = await client.post(
        "/api/targets",
        json={
            "name": "Local",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    target_id = resp.json()["id"]

    return api_key, target_id


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def journey_authed_client(tmp_path_factory):
    """Reuse one initialized client for journey tests that only need setup."""
    config_dir = tmp_path_factory.mktemp("journey-authed")
    async with build_test_client(config_dir) as client:
        data = await do_setup(client)
        yield client, data["api_key"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def journey_target_client(tmp_path_factory):
    """Reuse one initialized client plus a stable local target for target-based journeys."""
    config_dir = tmp_path_factory.mktemp("journey-target")
    async with build_test_client(config_dir) as client:
        data = await do_setup(client, encryption_password="test-password")
        api_key = data["api_key"]
        target_path = str(config_dir / "backups")
        os.makedirs(target_path, exist_ok=True)
        resp = await client.post(
            "/api/targets",
            json={
                "name": "Local",
                "type": "local",
                "config": {"path": target_path},
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 201
        yield client, api_key, resp.json()["id"]


async def _seed_snapshot(client, api_key):
    """Insert a test snapshot row via API-accessible DB."""
    import aiosqlite

    from app.core.dependencies import get_config

    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO snapshots (id, target_id, full_id, time, hostname, paths, tags, size_bytes, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "snap1234",
                "tgt1",
                "snap1234fullid",
                "2024-06-15T10:00:00Z",
                "testhost",
                "[]",
                "[]",
                5000,
                "2024-06-15T10:00:00Z",
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Journey 1: Setup → Create Target → Create Job → Trigger → Verify Run
# ---------------------------------------------------------------------------


async def test_journey_setup_to_run_history(journey_target_client):
    """Full workflow: setup → create target → create job → trigger → check runs."""
    client, api_key, target_id = journey_target_client

    # Create a custom job linked to the target
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Journey Job",
            "type": "full",
            "schedule": "0 4 * * *",
            "targets": [target_id],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    # Trigger the job
    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    # Verify run appears in history
    resp = await client.get("/api/jobs/runs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    run_ids = [r["id"] for r in resp.json()["items"]]
    assert run_id in run_ids


# ---------------------------------------------------------------------------
# Journey 2: Setup → Discovery → Database List
# ---------------------------------------------------------------------------


async def test_journey_discovery_to_database_list(journey_target_client):
    """Setup → list containers → list databases."""
    client, api_key, _target_id = journey_target_client

    # List discovered containers (scan requires Docker which is mocked as None)
    resp = await client.get("/api/discover/containers", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "items" in resp.json()

    # List databases (may be empty since discovery is mocked)
    resp = await client.get("/api/databases", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "items" in resp.json()
    assert "total" in resp.json()


# ---------------------------------------------------------------------------
# Journey 3: Setup → Notification → Test Send
# ---------------------------------------------------------------------------


async def test_journey_notification_create_and_test(journey_target_client):
    """Setup → create notification channel → test send."""
    client, api_key, _target_id = journey_target_client

    # Create notification channel
    resp = await client.post(
        "/api/notifications",
        json={
            "type": "webhook",
            "name": "Test Webhook",
            "url": "https://example.com/webhook/test",
            "events": ["backup.completed"],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    resp.json()["id"]

    # Verify it appears in list with redacted URL
    resp = await client.get("/api/notifications", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    config = resp.json()["items"][0]["config"]
    assert "••••••" in config.get("url", "")


# ---------------------------------------------------------------------------
# Journey 4: Setup → Settings Update → Export → Import
# ---------------------------------------------------------------------------


async def test_journey_settings_export_import(journey_target_client):
    """Setup → update settings → export → import."""
    client, api_key, _target_id = journey_target_client

    # Update settings
    resp = await client.put(
        "/api/settings",
        json={
            "settings": {"theme": "light", "keep_daily": "30"},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200

    # Export config
    resp = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert resp.status_code == 200
    yaml_content = resp.content

    # Import config back
    resp = await client.post(
        "/api/settings/import",
        content=yaml_content,
        headers={**auth_headers(api_key), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 200
    assert "restored" in resp.json()


# ---------------------------------------------------------------------------
# Journey 5: Concurrent Backup → 409 Conflict
# ---------------------------------------------------------------------------


async def test_journey_concurrent_backup_conflict(journey_target_client):
    """Triggering backup while lock file exists returns 409."""
    from app.services.orchestrator import _get_proc_start_time

    client, api_key, _target_id = journey_target_client

    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    # Create a lock file to simulate an in-progress backup
    from app.core.dependencies import get_config

    config = get_config()
    lock_file = os.path.join(str(config.config_dir), "backup.lock")
    pid = os.getpid()
    proc_start_time = _get_proc_start_time(pid)
    assert proc_start_time is not None
    with open(lock_file, "w") as f:
        f.write(
            json.dumps(
                {
                    "pid": pid,
                    "proc_start_time": proc_start_time,
                    "run_id": "fake-run",
                    "started_at": "now",
                }
            )
        )

    try:
        # Should get 409 because lock file exists
        resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
        assert resp.status_code == 409
    finally:
        if os.path.exists(lock_file):
            os.unlink(lock_file)


# ---------------------------------------------------------------------------
# Journey 6: API Key Regeneration Flow
# ---------------------------------------------------------------------------


async def test_journey_key_regeneration(client):
    """Setup → use key → regenerate → old key fails → new key works."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Use the key
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 200

    # Regenerate
    resp = await client.post("/api/auth/regenerate", headers=auth_headers(api_key))
    new_key = resp.json()["api_key"]

    # Old key fails
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 401

    # New key works
    resp = await client.get("/api/jobs", headers=auth_headers(new_key))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Journey 7: Snapshot Browse & Restore
# ---------------------------------------------------------------------------


async def test_journey_snapshot_list_after_seed(journey_target_client):
    """Seed snapshots → list → verify data returned."""
    client, api_key, _target_id = journey_target_client
    await _seed_snapshot(client, api_key)

    resp = await client.get("/api/snapshots", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    # Verify snapshot detail via the list response
    snap = resp.json()["items"][0]
    assert snap["id"] == "snap1234"
    assert snap["target_id"] == "tgt1"


# ---------------------------------------------------------------------------
# Journey 8: Restore Plan Preview
# ---------------------------------------------------------------------------


async def test_journey_restore_plan_preview(journey_target_client):
    """Setup with targets → preview restore plan → verify structure."""
    client, api_key, _target_id = journey_target_client

    resp = await client.get("/api/restore/plan/preview", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert "hostname" in data
    assert "generated_at" in data
    assert data["targets"] >= 1  # We created one target
