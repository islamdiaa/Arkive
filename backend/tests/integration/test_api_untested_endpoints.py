"""
API integration tests for previously untested endpoints.

Covers: cancel run, run logs, encryption password, snapshot refresh,
single snapshot detail, restore browse, restore plan markdown,
discover alias, directory scan.
"""

import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from app.core.dependencies import get_config
from tests.conftest import auth_headers, build_test_client, do_setup

pytestmark = pytest.mark.asyncio


async def setup_auth(client):
    data = await do_setup(client)
    return data["api_key"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def untested_authed_client(tmp_path_factory):
    """Shared initialized client for read-heavy untested endpoint coverage."""
    config_dir = tmp_path_factory.mktemp("untested-endpoints")
    async with build_test_client(config_dir) as client:
        data = await do_setup(client)
        yield client, data["api_key"]


def _errmsg(resp) -> str:
    """Extract error message from the standard Arkive error envelope."""
    body = resp.json()
    return body.get("detail", body.get("message", "")).lower()


# ---------------------------------------------------------------------------
# Helper: seed a snapshot row directly in the DB
# ---------------------------------------------------------------------------


async def _seed_snapshot(client, snapshot_id=None, target_id="tgt1"):
    """Seed a snapshot and return its id."""
    if snapshot_id is None:
        snapshot_id = f"snap-{uuid4().hex[:8]}"
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """INSERT INTO snapshots
               (id, target_id, full_id, time, hostname, paths, tags, size_bytes, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                target_id,
                f"{snapshot_id}full0000",
                "2025-01-15T10:00:00Z",
                "testhost",
                '["/" ]',
                '["daily"]',
                5000,
                "2025-01-15",
            ),
        )
        await db.commit()
    return snapshot_id


# ---------------------------------------------------------------------------
# Helper: create a job and trigger a run, return (api_key, job_id, run_id)
# ---------------------------------------------------------------------------


async def _create_running_job(client, api_key):
    """Get first job, trigger a run, return (job_id, run_id)."""
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    from unittest.mock import patch

    with patch("app.api.jobs.get_orchestrator") as mock_get_orch:
        mock_orch = AsyncMock()
        mock_orch.run_pipeline = AsyncMock()
        mock_get_orch.return_value = mock_orch
        run_resp = await client.post(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert run_resp.status_code == 202
    run_id = run_resp.json()["run_id"]
    return job_id, run_id


# ===================================================================
# 1. DELETE /api/jobs/{job_id}/run  -- Cancel in-progress backup
# ===================================================================


async def test_cancel_run_404_when_no_running_backup(untested_authed_client):
    """DELETE /api/jobs/{job_id}/run returns 404 when no run is in progress."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    job_id = resp.json()["items"][0]["id"]

    resp = await client.delete(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 404
    assert "no running" in _errmsg(resp)


async def test_cancel_run_success_when_run_exists(untested_authed_client):
    """DELETE /api/jobs/{job_id}/run succeeds when a run is in progress."""
    client, api_key = untested_authed_client
    job_id, run_id = await _create_running_job(client, api_key)

    resp = await client.delete(f"/api/jobs/{job_id}/run", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert body["run_id"] == run_id


async def test_cancel_run_nonexistent_job(untested_authed_client):
    """DELETE /api/jobs/nonexistent/run returns 404."""
    client, api_key = untested_authed_client
    resp = await client.delete("/api/jobs/nonexistent/run", headers=auth_headers(api_key))
    assert resp.status_code == 404


# ===================================================================
# 2. GET /api/jobs/runs/{run_id}/logs  -- Run log retrieval
# ===================================================================


async def test_run_logs_404_nonexistent_run(untested_authed_client):
    """GET /api/jobs/runs/{run_id}/logs returns 404 for unknown run."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/jobs/runs/nonexistent/logs", headers=auth_headers(api_key))
    assert resp.status_code == 404
    assert "not found" in _errmsg(resp)


async def test_run_logs_empty_for_new_run(untested_authed_client):
    """GET /api/jobs/runs/{run_id}/logs returns empty items for a fresh run."""
    client, api_key = untested_authed_client
    job_id, run_id = await _create_running_job(client, api_key)

    resp = await client.get(f"/api/jobs/runs/{run_id}/logs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert body["run_id"] == run_id
    assert isinstance(body["items"], list)


async def test_run_logs_with_matching_activity(untested_authed_client):
    """Logs endpoint returns entries whose activity details match the run_id."""
    client, api_key = untested_authed_client
    job_id, run_id = await _create_running_job(client, api_key)

    # Seed an activity_log entry with matching run_id in details
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "backup",
                "step_complete",
                "DB dump finished",
                json.dumps({"run_id": run_id, "step": "db_dump"}),
                "info",
                "2025-01-15T10:05:00Z",
            ),
        )
        await db.commit()

    resp = await client.get(f"/api/jobs/runs/{run_id}/logs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    messages = [e["message"] for e in body["items"]]
    assert any("DB dump" in m for m in messages)


# ===================================================================
# 3. Encryption password endpoint removed (security hardening)
# ===================================================================


async def test_encryption_password_endpoint_removed(client):
    """GET /api/settings/encryption-password was removed — must return 404/405."""
    data = await do_setup(client, encryption_password="my-secret-pass")
    api_key = data["api_key"]

    resp = await client.get("/api/settings/encryption-password", headers=auth_headers(api_key))
    # Endpoint no longer exists — expect 404 (no route) or 405 (method not allowed)
    assert resp.status_code in (404, 405)


async def test_settings_does_not_expose_encryption_password(client):
    """GET /api/settings must not expose the encryption password in its response."""
    data = await do_setup(client, encryption_password="my-secret-pass")
    api_key = data["api_key"]

    resp = await client.get("/api/settings", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    # The response should indicate password is set, but never return the value
    assert "encryption_password" not in body
    assert "restic_password" not in body
    assert body.get("encryption_password_set") is True


# ===================================================================
# 4. POST /api/snapshots/refresh
# ===================================================================


async def test_snapshot_refresh_with_mocked_engine(untested_authed_client):
    """POST /api/snapshots/refresh calls backup_engine.snapshots() and caches results."""
    client, api_key = untested_authed_client

    mock_engine = AsyncMock()
    mock_engine.snapshots = AsyncMock(
        return_value=[
            {
                "id": "abc1234567890full",
                "short_id": "abc12345",
                "time": "2025-01-15T10:00:00Z",
                "hostname": "testhost",
                "paths": ["/data"],
                "tags": ["daily"],
                "size": 9999,
            }
        ]
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    # Create a storage target so refresh has something to query
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """INSERT INTO storage_targets
               (id, name, type, enabled, config, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("tgt1", "Local", "local", 1, '{"path": "/tmp/bk"}', "ok", "2025-01-01", "2025-01-01"),
        )
        await db.commit()

    try:
        resp = await client.post("/api/snapshots/refresh", headers=auth_headers(api_key))
        assert resp.status_code == 200
        body = resp.json()
        assert "refreshed" in body
        assert body["refreshed"] == 1

        mock_engine.snapshots.assert_called_once()

        async with aiosqlite.connect(config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT snapshot_count, total_size_bytes FROM storage_targets WHERE id = ?",
                ("tgt1",),
            )
            row = await cursor.fetchone()
            assert row["snapshot_count"] == 1
            assert row["total_size_bytes"] == 9999
    finally:
        app.state.backup_engine = original_engine


async def test_snapshot_refresh_no_targets(client):
    """POST /api/snapshots/refresh with no enabled targets refreshes 0."""
    api_key = await setup_auth(client)

    mock_engine = AsyncMock()
    mock_engine.snapshots = AsyncMock(return_value=[])

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post("/api/snapshots/refresh", headers=auth_headers(api_key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["refreshed"] == 0
    finally:
        app.state.backup_engine = original_engine


# ===================================================================
# 5. GET /api/snapshots/{snapshot_id}  -- Single snapshot detail
# ===================================================================


async def test_get_snapshot_detail(untested_authed_client):
    """GET /api/snapshots/{id} returns a single snapshot."""
    client, api_key = untested_authed_client
    snap_id = await _seed_snapshot(client)
    resp = await client.get(f"/api/snapshots/{snap_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == snap_id
    assert body["hostname"] == "testhost"
    assert isinstance(body["paths"], list)
    assert isinstance(body["tags"], list)


async def test_get_snapshot_detail_404(untested_authed_client):
    """GET /api/snapshots/{id} returns 404 for nonexistent snapshot."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/snapshots/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_get_snapshot_by_full_id(untested_authed_client):
    """GET /api/snapshots/{id} also matches by full_id."""
    client, api_key = untested_authed_client
    snap_id = await _seed_snapshot(client)
    full_id = f"{snap_id}full0000"
    resp = await client.get(f"/api/snapshots/{full_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["full_id"] == full_id


# ===================================================================
# 6. GET /api/restore/browse/{snapshot_id}
# ===================================================================


async def test_restore_browse_snapshot_not_found(untested_authed_client):
    """GET /api/restore/browse/{snapshot_id} returns 404 for unknown snapshot."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/restore/browse/nonexistent", headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_restore_browse_success_with_mocked_engine(untested_authed_client):
    """GET /api/restore/browse/{snapshot_id} returns entries with mocked engine."""
    client, api_key = untested_authed_client
    snap_id = await _seed_snapshot(client, target_id="tgt-browse")

    # Insert a matching storage target
    config = get_config()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO storage_targets
               (id, name, type, enabled, config, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("tgt-browse", "BrowseTarget", "local", 1, '{"path": "/tmp/bk"}', "ok", "2025-01-01", "2025-01-01"),
        )
        await db.commit()

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(
        return_value=[
            {"name": "data.db", "type": "file", "size": 1024, "modified": "2025-01-15T10:00:00Z"},
            {"name": "config", "type": "directory", "size": None, "modified": None},
        ]
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.get(
            f"/api/restore/browse/{snap_id}",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "entries" in body
        assert len(body["entries"]) == 2
        assert body["snapshot_id"] == snap_id
    finally:
        app.state.backup_engine = original_engine


# ===================================================================
# 7. GET /api/restore/plan  -- Markdown restore plan
# ===================================================================


async def test_restore_plan_markdown(untested_authed_client):
    """GET /api/restore/plan returns markdown content."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/restore/plan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    content_type = resp.headers.get("content-type", "")
    assert "text/markdown" in content_type
    assert "# Arkive Disaster Recovery Plan" in resp.text
    assert "Storage Targets" in resp.text
    assert "Recovery Steps" in resp.text


async def test_restore_plan_markdown_has_server_info(untested_authed_client):
    """Restore plan markdown includes server hostname and generation time."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/restore/plan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "**Server:**" in resp.text
    assert "**Generated:**" in resp.text


async def test_restore_plan_markdown_uses_restore_staging_target(untested_authed_client):
    """Restore plan markdown should stage snapshot restores instead of targeting /."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/restore/plan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert "/restore-staging" in resp.text
    assert "staging" in resp.text.lower()
    assert "`restic restore <snapshot-id> --target /`" not in resp.text


async def test_restore_plan_markdown_includes_repo_path_for_remote_path_target(untested_authed_client):
    """Restore plan markdown should show the effective repo path for remote_path targets."""
    client, api_key = untested_authed_client

    create = await client.post(
        "/api/targets",
        json={
            "name": "SFTP",
            "type": "sftp",
            "config": {
                "host": "example.test",
                "port": 22,
                "username": "user",
                "password": "pass",
                "remote_path": "upload",
            },
        },
        headers=auth_headers(api_key),
    )
    assert create.status_code == 201, create.text
    target_id = create.json()["id"]

    resp = await client.get("/api/restore/plan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert f"`rclone:{target_id}:upload/arkive-backups`" in resp.text


async def test_restore_plan_markdown_includes_bucket_for_s3_target(untested_authed_client):
    """Restore plan markdown should show bucket-scoped repo paths for object storage."""
    client, api_key = untested_authed_client

    create = await client.post(
        "/api/targets",
        json={
            "name": "MinIO",
            "type": "s3",
            "config": {
                "access_key": "minio",
                "secret_key": "miniosecret",
                "bucket": "vmx-bucket",
                "endpoint": "http://minio.test:9000",
                "provider": "Other",
            },
        },
        headers=auth_headers(api_key),
    )
    assert create.status_code == 201, create.text
    target_id = create.json()["id"]

    resp = await client.get("/api/restore/plan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert f"`rclone:{target_id}:vmx-bucket/arkive-backups`" in resp.text


async def test_restore_plan_markdown_includes_concrete_snapshot_command(untested_authed_client):
    """Restore plan markdown should include a concrete restic snapshots command for configured targets."""
    client, api_key = untested_authed_client

    target_path = "/tmp/arkive-plan-target"
    os.makedirs(target_path, exist_ok=True)
    create = await client.post(
        "/api/targets",
        json={
            "name": "Local Plan Target",
            "type": "local",
            "config": {"path": target_path},
        },
        headers=auth_headers(api_key),
    )
    assert create.status_code == 201, create.text

    resp = await client.get("/api/restore/plan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert f"`restic -r {target_path}/arkive-repo snapshots`" in resp.text


# ===================================================================
# 8. POST /api/discover  -- Alias for scan
# ===================================================================


async def test_discover_alias_returns_503_without_docker(untested_authed_client):
    """POST /api/discover returns 503 when discovery service is None."""
    client, api_key = untested_authed_client
    resp = await client.post("/api/discover", headers=auth_headers(api_key))
    # Discovery is None in test fixture
    assert resp.status_code == 503
    assert "unavailable" in _errmsg(resp)


async def test_discover_scan_returns_503_without_docker(untested_authed_client):
    """POST /api/discover/scan also returns 503 when discovery is None."""
    client, api_key = untested_authed_client
    resp = await client.post("/api/discover/scan", headers=auth_headers(api_key))
    assert resp.status_code == 503


async def test_discover_alias_matches_scan(untested_authed_client):
    """POST /api/discover and POST /api/discover/scan give same structure when mocked."""
    client, api_key = untested_authed_client

    mock_discovery = AsyncMock()
    mock_discovery.scan = AsyncMock(return_value=[])

    app = client._transport.app
    original_discovery = app.state.discovery
    app.state.discovery = mock_discovery

    try:
        resp1 = await client.post("/api/discover/scan", headers=auth_headers(api_key))
        resp2 = await client.post("/api/discover", headers=auth_headers(api_key))
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Both should have the same structure
        for resp in (resp1, resp2):
            body = resp.json()
            assert "total_containers" in body
            assert "containers" in body
            assert "databases" in body
    finally:
        app.state.discovery = original_discovery


# ===================================================================
# 9. POST /api/directories/scan
# ===================================================================


async def test_directories_scan(untested_authed_client):
    """POST /api/directories/scan returns directory listing with platform."""
    client, api_key = untested_authed_client
    resp = await client.post("/api/directories/scan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "directories" in body
    assert "platform" in body
    assert isinstance(body["directories"], list)


async def test_directories_scan_get(untested_authed_client):
    """GET /api/directories/scan also works (GET alias)."""
    client, api_key = untested_authed_client
    resp = await client.get("/api/directories/scan", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert "directories" in body
    assert "platform" in body
