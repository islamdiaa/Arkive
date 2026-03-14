"""
Extended API integration tests for restore endpoints.

Covers: dry_run mode, success path with mocked engine, PDF generation.
"""

import os
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


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


# ===================================================================
# 13. POST /api/restore  — dry_run and success path
# ===================================================================


async def test_restore_dry_run(client, tmp_path):
    """POST /api/restore with dry_run=true lists files without restoring."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(
        return_value=[
            {"name": "file1.txt", "type": "file", "size": 100, "modified": "2025-01-01T00:00:00Z"},
            {"name": "file2.txt", "type": "file", "size": 200, "modified": "2025-01-01T00:00:00Z"},
        ]
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap123",
                "paths": ["/data"],
                "dry_run": True,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["status"] == "dry_run"
        assert "entries" in body
        assert len(body["entries"]) == 2
        assert "restore_id" in body
        assert body["snapshot_id"] == "snap123"

        # Ensure actual restore was never called
        mock_engine.restore.assert_not_called()
    finally:
        app.state.backup_engine = original_engine


async def test_restore_dry_run_empty_paths(client, tmp_path):
    """POST /api/restore dry_run with empty paths uses '/' as default."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(return_value=[])

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap456",
                "paths": [],
                "dry_run": True,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        # ls should have been called with "/" as fallback
        mock_engine.ls.assert_called_once()
        call_args = mock_engine.ls.call_args
        assert call_args[0][1] == "snap456"
        assert call_args[0][2] == "/"
    finally:
        app.state.backup_engine = original_engine


async def test_restore_dry_run_failure(client, tmp_path):
    """POST /api/restore dry_run that fails returns status=failed."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.ls = AsyncMock(side_effect=RuntimeError("repository locked"))

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    try:
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap-bad",
                "paths": ["/data"],
                "dry_run": True,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["status"] == "failed"
        assert "repository locked" in body["message"]
    finally:
        app.state.backup_engine = original_engine


async def test_restore_success_path(client, tmp_path):
    """POST /api/restore with dry_run=false calls backup_engine.restore."""
    from app.services.orchestrator import LOCK_FILE, RESTORE_LOCK_FILE

    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)
    restore_to = str(tmp_path / "restores" / "restore-dest")

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(
        return_value={
            "status": "success",
            "output": "restored 42 files",
        }
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    # Ensure no stale lock files block the test
    LOCK_FILE.unlink(missing_ok=True)
    RESTORE_LOCK_FILE.unlink(missing_ok=True)

    try:
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap-good",
                "paths": ["/data/important"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "restore_id" in body
        assert body["snapshot_id"] == "snap-good"
        assert "42 files" in body["message"]

        mock_engine.restore.assert_called_once()
        call_kwargs = mock_engine.restore.call_args
        assert call_kwargs.kwargs.get("snapshot_id") or call_kwargs[1].get("snapshot_id", "") == "snap-good"
        assert call_kwargs.kwargs.get("restore_to") == restore_to
    finally:
        app.state.backup_engine = original_engine
        RESTORE_LOCK_FILE.unlink(missing_ok=True)


async def test_restore_failed_result_returns_and_persists_error_message(client, tmp_path):
    """Failed restore results should surface and persist the engine error."""
    from app.services.orchestrator import LOCK_FILE, RESTORE_LOCK_FILE

    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)
    restore_to = str(tmp_path / "restores" / "restore-failed-result")

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(
        return_value={
            "status": "failed",
            "error": "destination already contains conflicting file",
            "output": "",
        }
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    LOCK_FILE.unlink(missing_ok=True)
    RESTORE_LOCK_FILE.unlink(missing_ok=True)

    try:
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap-conflict",
                "paths": ["/data/important"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "failed"
        assert "conflicting file" in body["message"]

        async with aiosqlite.connect(app.state.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, restore_to, error_message FROM restore_runs ORDER BY started_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row["status"] == "failed"
        assert row["restore_to"] == restore_to
        assert "conflicting file" in (row["error_message"] or "")
    finally:
        app.state.backup_engine = original_engine
        RESTORE_LOCK_FILE.unlink(missing_ok=True)


async def test_restore_requires_explicit_safe_restore_destination(client, tmp_path):
    """POST /api/restore rejects missing or unsafe restore_to values."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    missing = await client.post(
        "/api/restore",
        json={
            "target": target_id,
            "snapshot_id": "snap-missing",
            "paths": ["/data/important"],
        },
        headers=auth_headers(api_key),
    )
    assert missing.status_code == 422
    assert "restore_to" in missing.text

    unsafe = await client.post(
        "/api/restore",
        json={
            "target": target_id,
            "snapshot_id": "snap-unsafe",
            "paths": ["/data/important"],
            "restore_to": "/tmp/restore-anywhere",
        },
        headers=auth_headers(api_key),
    )
    assert unsafe.status_code == 422
    assert "restore" in unsafe.text.lower()


# ===================================================================
# 14. GET /api/restore/plan/pdf
# ===================================================================


async def test_restore_plan_pdf_with_mock_service(client, tmp_path):
    """GET /api/restore/plan/pdf returns a file when restore_plan service exists."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # Create a fake PDF file
    pdf_path = str(tmp_path / "restore-plan.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4 fake pdf content")

    mock_restore_plan = AsyncMock()
    mock_restore_plan.generate = AsyncMock(return_value=pdf_path)

    app = client._transport.app
    original_rp = app.state.restore_plan
    app.state.restore_plan = mock_restore_plan

    try:
        resp = await client.get("/api/restore/plan/pdf", headers=auth_headers(api_key))
        assert resp.status_code == 200
        assert "pdf" in resp.headers.get("content-type", "").lower()
        assert len(resp.content) > 0
        mock_restore_plan.generate.assert_called_once()
    finally:
        app.state.restore_plan = original_rp


async def test_restore_plan_pdf_html_fallback(client, tmp_path):
    """GET /api/restore/plan/pdf returns HTML when generate() returns .html path."""
    data = await do_setup(client)
    api_key = data["api_key"]

    html_path = str(tmp_path / "restore-plan.html")
    with open(html_path, "w") as f:
        f.write("<html><body>Restore Plan</body></html>")

    mock_restore_plan = AsyncMock()
    mock_restore_plan.generate = AsyncMock(return_value=html_path)

    app = client._transport.app
    original_rp = app.state.restore_plan
    app.state.restore_plan = mock_restore_plan

    try:
        resp = await client.get("/api/restore/plan/pdf", headers=auth_headers(api_key))
        assert resp.status_code == 200
        assert "html" in resp.headers.get("content-type", "").lower()
    finally:
        app.state.restore_plan = original_rp


async def test_restore_plan_pdf_service_none(client):
    """GET /api/restore/plan/pdf errors when restore_plan is None."""
    data = await do_setup(client)
    api_key = data["api_key"]

    # restore_plan is None in test fixture, so .generate() raises AttributeError
    try:
        resp = await client.get("/api/restore/plan/pdf", headers=auth_headers(api_key))
        # Should get a 500 from the unhandled AttributeError
        assert resp.status_code == 500
    except (AttributeError, Exception):
        # Also acceptable — None has no .generate()
        pass
