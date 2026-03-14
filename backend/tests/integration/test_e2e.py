"""
End-to-end integration tests — full setup -> create job -> trigger backup flow.

Tests the complete user journey with mocked Docker and restic dependencies.
These tests exercise the full API surface from setup through backup execution.
"""

import os

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _full_setup(client, tmp_path):
    """Run setup and create a local storage target."""
    # Run setup
    data = await do_setup(client, encryption_password="test-password")
    api_key = data["api_key"]

    # Create a local target
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


# ---------------------------------------------------------------------------
# Full lifecycle: setup -> verify status -> create job -> trigger -> check
# ---------------------------------------------------------------------------


async def test_full_setup_to_status_flow(client):
    """After setup, status should report setup_completed and system info."""
    await do_setup(client)

    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded", "error")
    assert data["setup_completed"] is True
    assert "version" in data
    assert "hostname" in data


async def test_setup_creates_default_jobs_accessible_via_api(client):
    """After setup, the 3 default jobs should be listable via the jobs API."""
    data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    jobs = resp.json()["items"]
    assert len(jobs) == 3

    names = {j["name"] for j in jobs}
    assert names == {"DB Dumps", "Cloud Sync", "Flash Backup"}

    types = {j["type"] for j in jobs}
    assert types == {"db_dump", "full", "flash"}


async def test_setup_then_create_target_then_link_to_job(client, tmp_path):
    """Full flow: setup -> create target -> link target to a job."""
    api_key, target_id = await _full_setup(client, tmp_path)

    # Get the Cloud Sync job
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    cloud_sync = next(j for j in resp.json()["items"] if j["name"] == "Cloud Sync")
    job_id = cloud_sync["id"]

    # Link target to job
    resp = await client.put(
        f"/api/jobs/{job_id}",
        json={
            "targets": [target_id],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert target_id in resp.json()["targets"]


async def test_setup_then_trigger_manual_backup(client, tmp_path):
    """Full flow: setup -> create target -> trigger manual backup -> verify run exists."""
    api_key, target_id = await _full_setup(client, tmp_path)

    # Get the first job
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    # Trigger manual backup
    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert resp.json()["trigger"] == "manual"

    # Verify the run exists in the runs list
    resp = await client.get("/api/jobs/runs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    run_ids = [r["id"] for r in resp.json()["items"]]
    assert run_id in run_ids


async def test_setup_then_trigger_then_check_run_detail(client, tmp_path):
    """Full flow: trigger a run and verify the run detail endpoint returns data."""
    api_key, _ = await _full_setup(client, tmp_path)

    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    run_id = resp.json()["run_id"]

    resp = await client.get(f"/api/jobs/runs/{run_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == run_id
    assert data["job_id"] == job_id
    assert data["status"] == "running"
    assert "databases" in data
    assert "targets" in data


async def test_setup_then_check_activity_log(client, tmp_path):
    """After setup + job creation, activity log should have entries."""
    api_key, _ = await _full_setup(client, tmp_path)

    # Create a custom job to generate activity
    await client.post(
        "/api/jobs",
        json={
            "name": "E2E Job",
            "type": "full",
            "schedule": "0 3 * * *",
        },
        headers=auth_headers(api_key),
    )

    resp = await client.get("/api/activity", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0

    # Should include the job creation entry
    actions = [item["action"] for item in data["items"]]
    assert "created" in actions


async def test_setup_then_check_storage_with_target(client, tmp_path):
    """After creating a target, storage endpoint should list it."""
    api_key, target_id = await _full_setup(client, tmp_path)

    resp = await client.get("/api/storage", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_count"] >= 1

    target_ids = [t["id"] for t in data["targets"]]
    assert target_id in target_ids


async def test_session_metadata_persists_across_requests(client):
    """The session endpoint should return stable setup metadata."""
    await do_setup(client)

    resp1 = await client.get("/api/auth/session")
    resp2 = await client.get("/api/auth/session")
    data1 = resp1.json()
    data2 = resp2.json()
    assert data1["setup_required"] is False
    assert data1.get("setup_completed_at")
    assert data1["setup_completed_at"] == data2["setup_completed_at"]


async def test_full_job_crud_lifecycle(client, tmp_path):
    """Full CRUD lifecycle: create -> read -> update -> delete."""
    api_key, _ = await _full_setup(client, tmp_path)

    # Create
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Lifecycle Job",
            "type": "full",
            "schedule": "0 6 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    # Read
    resp = await client.get(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["name"] == "Lifecycle Job"

    # Update
    resp = await client.put(
        f"/api/jobs/{job_id}",
        json={
            "name": "Updated Job",
            "schedule": "30 3 * * *",
            "enabled": False,
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Job"
    assert resp.json()["schedule"] == "30 3 * * *"
    assert resp.json()["enabled"] is False

    # Delete
    resp = await client.delete(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200

    # Verify deleted
    resp = await client.get(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_full_target_crud_lifecycle(client, tmp_path):
    """Full CRUD lifecycle for targets: create -> read -> update -> delete."""
    api_key, _ = await _full_setup(client, tmp_path)

    target_path2 = str(tmp_path / "backups2")
    os.makedirs(target_path2, exist_ok=True)

    # Create
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Lifecycle Target",
            "type": "local",
            "config": {"path": target_path2},
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    target_id = resp.json()["id"]

    # Read
    resp = await client.get(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["name"] == "Lifecycle Target"

    # Update
    resp = await client.put(
        f"/api/targets/{target_id}",
        json={
            "name": "Renamed Target",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Target"

    # Delete
    resp = await client.delete(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200

    # Verify deleted
    resp = await client.get(f"/api/targets/{target_id}", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_directory_lifecycle_with_job_linkage(client, tmp_path):
    """Create a directory, link it to a job, then verify linkage."""
    api_key, _ = await _full_setup(client, tmp_path)

    # Create a watched directory
    watch_dir = str(tmp_path / "appdata")
    os.makedirs(watch_dir, exist_ok=True)

    resp = await client.post(
        "/api/directories",
        json={
            "path": watch_dir,
            "label": "App Data",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    dir_id = resp.json()["id"]

    # Get a job and link the directory
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.put(
        f"/api/jobs/{job_id}",
        json={
            "directories": [dir_id],
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert dir_id in resp.json()["directories"]


async def test_regenerate_key_then_use_new_key(client):
    """Full flow: setup -> regenerate key -> old key fails -> new key works."""
    data = await do_setup(client)
    old_key = data["api_key"]

    # Regenerate
    resp = await client.post("/api/auth/regenerate", headers=auth_headers(old_key))
    assert resp.status_code == 200
    new_key = resp.json()["api_key"]

    # Old key should fail
    resp = await client.get("/api/jobs", headers=auth_headers(old_key))
    assert resp.status_code == 401

    # New key should work
    resp = await client.get("/api/jobs", headers=auth_headers(new_key))
    assert resp.status_code == 200
    assert resp.json()["total"] == 3
