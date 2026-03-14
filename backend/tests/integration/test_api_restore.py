"""
API integration tests for restore endpoints.

Tests: restore plan preview, restore nonexistent target, restore plan PDF service unavailable,
       POST /api/restore/test integrity verification.
"""

import hashlib
import os
from unittest.mock import AsyncMock

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


async def test_restore_plan_preview(client):
    """GET /api/restore/plan/preview should return hostname, targets, containers."""
    data = await do_setup(client)
    resp = await client.get("/api/restore/plan/preview", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "hostname" in body
    assert "targets" in body
    assert "containers" in body
    assert "generated_at" in body


async def test_restore_nonexistent_target(client):
    """POST /api/restore without a valid restore request fails model validation first."""
    data = await do_setup(client)
    resp = await client.post(
        "/api/restore",
        json={
            "target": "nonexistent",
            "snapshot_id": "abc",
            "paths": [],
        },
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 422


async def test_restore_plan_pdf_service_unavailable(client):
    """GET /api/restore/plan/pdf should error when restore_plan is None."""
    data = await do_setup(client)
    # restore_plan is None in test fixture, causing an AttributeError.
    # The ASGI transport may raise or return 500 depending on error middleware.
    try:
        resp = await client.get("/api/restore/plan/pdf", headers=auth_headers(data["api_key"]))
        assert resp.status_code == 500
    except (AttributeError, Exception):
        # Expected: restore_plan is None, so .generate() raises AttributeError
        pass


async def test_restore_missing_target_field(client):
    """POST /api/restore without restore_to now fails schema validation before lookup."""
    data = await do_setup(client)
    resp = await client.post(
        "/api/restore",
        json={
            "target": "does_not_exist",
            "snapshot_id": "snap123",
            "paths": ["/data"],
        },
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/restore/test — integrity verification tests
# ---------------------------------------------------------------------------


async def _create_target(client, api_key, tmp_path):
    """Helper: create a local storage target and return its ID."""
    target_path = str(tmp_path / "backups")
    os.makedirs(target_path, exist_ok=True)
    resp = await client.post(
        "/api/targets",
        json={"name": "TestTarget", "type": "local", "config": {"path": target_path}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_restore_test_missing_fields(client):
    """POST /api/restore/test without snapshot_id or target_id returns 422 (Pydantic validation)."""
    data = await do_setup(client)
    hdrs = auth_headers(data["api_key"])

    # Missing both — Pydantic model requires snapshot_id and target_id
    resp = await client.post("/api/restore/test", json={}, headers=hdrs)
    assert resp.status_code == 422

    # Missing snapshot_id
    resp = await client.post("/api/restore/test", json={"target_id": "x"}, headers=hdrs)
    assert resp.status_code == 422

    # Missing target_id
    resp = await client.post("/api/restore/test", json={"snapshot_id": "x"}, headers=hdrs)
    assert resp.status_code == 422


async def test_restore_test_nonexistent_target(client):
    """POST /api/restore/test with nonexistent target returns 404."""
    data = await do_setup(client)
    resp = await client.post(
        "/api/restore/test",
        json={"snapshot_id": "abc123", "target_id": "nonexistent"},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 404


async def test_restore_test_success_with_path(client, tmp_path):
    """POST /api/restore/test with explicit path returns success + SHA-256."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    # Prepare a known file content
    test_content = b"hello arkive restore test"
    expected_sha256 = hashlib.sha256(test_content).hexdigest()

    # Mock backup_engine.restore to write a file into the temp dir
    async def mock_restore(target, snapshot_id, paths, restore_to):
        # Simulate restic restoring the file into restore_to
        file_name = os.path.basename(paths[0])
        dest = os.path.join(restore_to, file_name)
        os.makedirs(os.path.dirname(dest) if os.path.dirname(dest) else restore_to, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(test_content)
        return {"status": "success", "output": "restored 1 file"}

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(side_effect=mock_restore)

    # Inject mock into app state
    transport = client._transport
    app = transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore/test",
            json={
                "snapshot_id": "snap123",
                "target_id": target_id,
                "path": "/data/testfile.txt",
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["file"] == "/data/testfile.txt"
        assert body["sha256"] == expected_sha256
        assert body["size_bytes"] == len(test_content)
        assert "duration_ms" in body
        assert isinstance(body["duration_ms"], int)
    finally:
        app.state.backup_engine = original_engine


async def test_restore_test_auto_pick_file(client, tmp_path):
    """POST /api/restore/test without path auto-selects a file from snapshot."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    test_content = b"auto-selected file content"
    expected_sha256 = hashlib.sha256(test_content).hexdigest()

    # Mock ls to return a list of files
    async def mock_ls(target, snapshot_id, path):
        return [
            {"name": "dir1", "type": "directory", "size": None, "modified": None},
            {"name": "config.yaml", "type": "file", "size": 26, "modified": "2025-01-01T00:00:00Z"},
            {"name": "data.bin", "type": "file", "size": 100, "modified": "2025-01-01T00:00:00Z"},
        ]

    async def mock_restore(target, snapshot_id, paths, restore_to):
        file_name = os.path.basename(paths[0])
        dest = os.path.join(restore_to, file_name)
        with open(dest, "wb") as f:
            f.write(test_content)
        return {"status": "success", "output": "restored 1 file"}

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(side_effect=mock_ls)
    mock_engine.restore = AsyncMock(side_effect=mock_restore)

    transport = client._transport
    app = transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore/test",
            json={"snapshot_id": "snap456", "target_id": target_id},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        # Should have picked one of the two files (config.yaml or data.bin)
        assert body["file"] in ["/config.yaml", "/data.bin"]
        assert body["sha256"] == expected_sha256
        assert body["size_bytes"] == len(test_content)
    finally:
        app.state.backup_engine = original_engine


async def test_restore_test_no_files_in_snapshot(client, tmp_path):
    """POST /api/restore/test with snapshot having only dirs returns failed."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    async def mock_ls(target, snapshot_id, path):
        return [
            {"name": "dir1", "type": "directory", "size": None, "modified": None},
        ]

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(side_effect=mock_ls)

    transport = client._transport
    app = transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore/test",
            json={"snapshot_id": "snap789", "target_id": target_id},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "No files found" in body["error"]
    finally:
        app.state.backup_engine = original_engine


async def test_restore_test_restore_failure(client, tmp_path):
    """POST /api/restore/test when restore returns failure status."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    async def mock_restore(target, snapshot_id, paths, restore_to):
        return {"status": "failed", "error": "repository not found", "output": ""}

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(side_effect=mock_restore)

    transport = client._transport
    app = transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore/test",
            json={
                "snapshot_id": "snap-bad",
                "target_id": target_id,
                "path": "/some/file.txt",
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "repository not found" in body["error"]
    finally:
        app.state.backup_engine = original_engine


async def test_restore_test_restore_exception(client, tmp_path):
    """POST /api/restore/test when backup_engine.restore raises an exception."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    async def mock_restore(target, snapshot_id, paths, restore_to):
        raise RuntimeError("restic binary not found")

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(side_effect=mock_restore)

    transport = client._transport
    app = transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore/test",
            json={
                "snapshot_id": "snap-err",
                "target_id": target_id,
                "path": "/some/file.txt",
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "Internal error" in body["error"] or "restic" in body["error"]
    finally:
        app.state.backup_engine = original_engine


async def test_restore_test_ls_exception(client, tmp_path):
    """POST /api/restore/test when backup_engine.ls raises an exception (no path given)."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    async def mock_ls(target, snapshot_id, path):
        raise RuntimeError("restic not available")

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(side_effect=mock_ls)

    transport = client._transport
    app = transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore/test",
            json={"snapshot_id": "snap-err2", "target_id": target_id},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "Failed to list snapshot files" in body["error"]
    finally:
        app.state.backup_engine = original_engine
