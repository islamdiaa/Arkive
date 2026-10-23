"""
Extended API integration tests for job endpoints.

Covers: run nonexistent job, run job with orchestrator mock.
"""

from unittest.mock import AsyncMock, patch

import pytest

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
# 17. POST /api/jobs/{job_id}/run -- 404 when job doesn't exist
# ===================================================================


async def test_run_nonexistent_job_returns_404(client):
    """POST /api/jobs/{job_id}/run returns 404 for nonexistent job."""
    api_key = await setup_auth(client)
    resp = await client.post("/api/jobs/nonexistent/run", headers=auth_headers(api_key))
    assert resp.status_code == 404
    assert "not found" in _errmsg(resp)


async def test_run_deleted_job_returns_404(client):
    """POST /api/jobs/{job_id}/run returns 404 after the job has been deleted."""
    api_key = await setup_auth(client)

    # Create a job, then delete it
    create_resp = await client.post(
        "/api/jobs",
        json={
            "name": "Ephemeral Job",
            "type": "full",
            "schedule": "0 0 * * *",
        },
        headers=auth_headers(api_key),
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    del_resp = await client.delete(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert del_resp.status_code == 200

    # Now try to run the deleted job
    resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_run_job_returns_correct_shape(client):
    """POST /api/jobs/{job_id}/run returns the expected response fields."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    with patch("app.api.jobs.get_orchestrator") as mock_get_orch:
        mock_orch = AsyncMock()
        mock_orch.run_pipeline = AsyncMock()
        mock_get_orch.return_value = mock_orch
        resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))

    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body
    assert "run_id" in body
    assert "status" in body
    assert "trigger" in body
    assert "started_at" in body
    assert "message" in body
    assert body["job_id"] == job_id
    assert body["status"] == "running"
    assert body["trigger"] == "manual"


async def test_run_job_creates_run_record(client):
    """POST /api/jobs/{job_id}/run creates a retrievable run record."""
    api_key = await setup_auth(client)
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    with patch("app.api.jobs.get_orchestrator") as mock_get_orch:
        mock_orch = AsyncMock()
        mock_orch.run_pipeline = AsyncMock()
        mock_get_orch.return_value = mock_orch
        run_resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))

    run_id = run_resp.json()["run_id"]

    # The run should now be retrievable via the run detail endpoint
    detail_resp = await client.get(f"/api/jobs/runs/{run_id}", headers=auth_headers(api_key))
    assert detail_resp.status_code == 200
    assert detail_resp.json()["id"] == run_id
    assert detail_resp.json()["job_id"] == job_id
    assert detail_resp.json()["status"] == "running"
    assert detail_resp.json()["trigger"] == "manual"
