"""
API integration tests for backup job endpoints.

Adapted from Arkive v2 for v3 flat route layout (app.api.jobs).
Tests: job CRUD, run history, manual trigger, concurrency guard, pagination.
"""

import json
import os

import pytest
import pytest_asyncio

from tests.conftest import auth_headers, build_test_client, do_setup

pytestmark = pytest.mark.asyncio


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def jobs_authed_client(tmp_path_factory):
    """Reuse one initialized client for auth-heavy job endpoint coverage."""
    config_dir = tmp_path_factory.mktemp("api-jobs")
    async with build_test_client(config_dir) as client:
        data = await do_setup(client)
        yield client, data["api_key"]


# -- GET /api/jobs -----------------------------------------------------------


async def test_list_jobs(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    # Setup creates 3 default jobs
    assert data["total"] == 3


async def test_list_jobs_includes_last_run_and_next_run(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    jobs = resp.json()["items"]
    for job in jobs:
        assert "last_run" in job
        assert "next_run" in job


async def test_list_jobs_default_jobs_have_schedules(jobs_authed_client):
    """All default jobs created by setup should have valid cron schedules."""
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    jobs = resp.json()["items"]
    for job in jobs:
        parts = job["schedule"].strip().split()
        assert len(parts) == 5, f"Job '{job['name']}' has invalid cron: {job['schedule']}"


# -- GET /api/jobs/{id} ------------------------------------------------------


async def test_get_single_job(jobs_authed_client):
    client, api_key = jobs_authed_client
    # Get list first
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.get(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id


async def test_get_nonexistent_job_returns_404(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


# -- POST /api/jobs ----------------------------------------------------------


async def test_create_job_with_valid_cron(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Custom Job",
            "type": "full",
            "schedule": "0 6 * * *",
            "targets": [],
            "directories": [],
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Custom Job"
    assert data["schedule"] == "0 6 * * *"
    assert "id" in data


async def test_create_job_rejects_invalid_cron(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Bad Cron Job",
            "type": "full",
            "schedule": "not a cron",
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    assert "cron" in resp.json().get("detail", resp.json().get("message", "")).lower()


async def test_create_job_rejects_cron_with_wrong_field_count(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Bad Cron",
            "type": "full",
            "schedule": "0 2 * *",  # Only 4 fields
        },
        headers=auth_headers(api_key),
    )

    assert resp.status_code == 422
    assert "5 fields" in resp.json().get("detail", resp.json().get("message", ""))


async def test_create_job_rejects_invalid_type(jobs_authed_client):
    """Job types must be one of: full, db_dump, flash."""
    client, api_key = jobs_authed_client
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Bad Type",
            "type": "incremental",
            "schedule": "0 0 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 400
    assert "invalid job type" in resp.json().get("detail", resp.json().get("message", "")).lower()


async def test_create_db_dump_job(jobs_authed_client):
    """Should be able to create a db_dump type job."""
    client, api_key = jobs_authed_client
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "DB Only",
            "type": "db_dump",
            "schedule": "0 1 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "db_dump"


async def test_create_flash_job(jobs_authed_client):
    """Should be able to create a flash type job."""
    client, api_key = jobs_authed_client
    resp = await client.post(
        "/api/jobs",
        json={
            "name": "Flash Only",
            "type": "flash",
            "schedule": "0 4 * * 0",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201
    assert resp.json()["type"] == "flash"


# -- PUT /api/jobs/{id} ------------------------------------------------------


async def test_update_job(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.put(
        f"/api/jobs/{job_id}",
        json={
            "name": "Renamed Job",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Job"


async def test_update_job_schedule(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.put(
        f"/api/jobs/{job_id}",
        json={
            "schedule": "30 4 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["schedule"] == "30 4 * * *"


async def test_update_job_enable_disable(jobs_authed_client):
    """Should be able to toggle job enabled state."""
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.put(
        f"/api/jobs/{job_id}",
        json={
            "enabled": False,
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


async def test_update_nonexistent_job_returns_404(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.put(
        "/api/jobs/nonexistent",
        json={
            "name": "Ghost",
        },
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 404


# -- DELETE /api/jobs/{id} ---------------------------------------------------


async def test_delete_job(jobs_authed_client):
    client, api_key = jobs_authed_client

    # Create a job to delete
    create_resp = await client.post(
        "/api/jobs",
        json={
            "name": "To Delete",
            "type": "full",
            "schedule": "0 0 * * *",
        },
        headers=auth_headers(api_key),
    )
    job_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "deleted" in resp.json()["message"].lower()

    # Confirm it's gone
    resp = await client.get(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_delete_nonexistent_job_returns_404(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.delete("/api/jobs/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


# -- POST /api/jobs/{id}/run -------------------------------------------------


async def test_run_job_returns_202(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    from unittest.mock import AsyncMock, patch

    with patch("app.api.jobs.get_orchestrator") as mock_get_orch:
        mock_orch = AsyncMock()
        mock_orch.run_pipeline = AsyncMock()
        mock_get_orch.return_value = mock_orch
        resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "running"
    assert data["trigger"] == "manual"
    assert "run_id" in data


async def test_run_job_creates_activity_log_entry(jobs_authed_client):
    """Triggering a run should create an activity log entry."""
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 202

    # Check activity log
    resp = await client.get("/api/activity", headers=auth_headers(api_key))
    items = resp.json()["items"]
    run_events = [i for i in items if i["action"] == "run_started"]
    assert len(run_events) >= 1


async def test_concurrent_backup_returns_409(jobs_authed_client, tmp_path):
    from app.services.orchestrator import _get_proc_start_time

    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    # Create a live-process lock file to simulate a running backup
    config_dir = os.environ.get("ARKIVE_CONFIG_DIR", str(tmp_path))
    lock_file = os.path.join(config_dir, "backup.lock")
    pid = os.getpid()
    proc_start_time = _get_proc_start_time(pid)
    assert proc_start_time is not None
    with open(lock_file, "w") as f:
        f.write(
            json.dumps(
                {
                    "pid": pid,
                    "proc_start_time": proc_start_time,
                    "run_id": "fake_run",
                    "started_at": "2024-01-01T00:00:00Z",
                }
            )
        )

    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 409
    assert "already running" in resp.json().get("detail", resp.json().get("message", "")).lower()

    # Clean up
    os.remove(lock_file)


async def test_run_job_removes_stale_backup_lock_and_starts(jobs_authed_client, tmp_path):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    config_dir = os.environ.get("ARKIVE_CONFIG_DIR", str(tmp_path))
    lock_file = os.path.join(config_dir, "backup.lock")
    with open(lock_file, "w") as f:
        f.write(
            json.dumps(
                {
                    "pid": 999999,
                    "proc_start_time": "12345",
                    "run_id": "stale_run",
                }
            )
        )

    from unittest.mock import AsyncMock, patch

    with patch("app.api.jobs.get_orchestrator") as mock_get_orch:
        mock_orch = AsyncMock()
        mock_orch.run_pipeline = AsyncMock()
        mock_get_orch.return_value = mock_orch
        resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))

    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    assert not os.path.exists(lock_file)


# -- GET /api/jobs/runs ------------------------------------------------------


async def test_list_runs(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs/runs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "has_more" in data


async def test_list_runs_pagination(jobs_authed_client):
    """Runs endpoint should respect limit and offset parameters."""
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs/runs?limit=5&offset=0", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 5
    assert data["offset"] == 0


# -- GET /api/jobs/runs/{runId} ----------------------------------------------


async def test_get_run_detail(jobs_authed_client):
    client, api_key = jobs_authed_client

    # Create a run manually via triggering a job
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    from unittest.mock import AsyncMock, patch

    with patch("app.api.jobs.get_orchestrator") as mock_get_orch:
        mock_orch = AsyncMock()
        mock_orch.run_pipeline = AsyncMock()
        mock_get_orch.return_value = mock_orch
        run_resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    run_id = run_resp.json()["run_id"]

    resp = await client.get(f"/api/jobs/runs/{run_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == run_id
    assert "databases" in data
    assert "targets" in data


async def test_get_nonexistent_run_returns_404(jobs_authed_client):
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs/runs/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


# -- GET /api/jobs/{id}/history ----------------------------------------------


async def test_get_job_history(jobs_authed_client):
    """Job history endpoint should return paginated runs for that job."""
    client, api_key = jobs_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.get(f"/api/jobs/{job_id}/history", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "has_more" in data
