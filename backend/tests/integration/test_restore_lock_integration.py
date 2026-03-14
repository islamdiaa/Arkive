"""
Integration tests for the restore lock mechanism.

Covers conflict detection, stale-lock recovery, and lock cleanup
on both success and failure paths.

NOTE: Lock paths are resolved at call time from ARKIVE_CONFIG_DIR (set to
tmp_path by the client fixture), so all helpers derive paths at runtime
rather than importing the module-level constants which default to /config/.
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.services.orchestrator import _get_proc_start_time
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Path helpers — always resolved at runtime from ARKIVE_CONFIG_DIR
# ---------------------------------------------------------------------------


def _lock_file() -> Path:
    """Return the backup lock path for the current test's config dir."""
    return Path(os.environ["ARKIVE_CONFIG_DIR"]) / "backup.lock"


def _restore_lock_file() -> Path:
    """Return the restore lock path for the current test's config dir."""
    return Path(os.environ["ARKIVE_CONFIG_DIR"]) / "restore.lock"


def _write_lock(path: Path, payload: dict) -> None:
    """Write a lock file with the given payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, json.dumps(payload).encode())
    finally:
        os.close(fd)


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


# ---------------------------------------------------------------------------
# 1. Backup running blocks restore
# ---------------------------------------------------------------------------


async def test_restore_blocked_when_backup_running(client, tmp_path):
    """POST /api/restore returns 409 when backup.lock exists."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    lock = _lock_file()
    restore_lock = _restore_lock_file()
    _write_lock(lock, {"pid": os.getpid(), "started_at": "2026-01-01T00:00:00Z"})

    try:
        restore_to = str(tmp_path / "restores" / "blocked-by-backup")
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap001",
                "paths": ["/data"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 409, resp.text
        message = resp.json().get("message", "")
        assert "backup" in message.lower(), f"Expected 'backup' in message, got: {message!r}"
    finally:
        lock.unlink(missing_ok=True)
        restore_lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Another restore running blocks new restore
# ---------------------------------------------------------------------------


async def test_restore_blocked_when_restore_running(client, tmp_path):
    """POST /api/restore returns 409 when restore.lock exists with a live PID."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    lock = _lock_file()
    restore_lock = _restore_lock_file()

    # Write a restore lock that looks like the current live process
    pid = os.getpid()
    proc_start = _get_proc_start_time(pid) or ""
    _write_lock(
        restore_lock,
        {
            "pid": pid,
            "proc_start_time": proc_start,
            "started_at": "2026-01-01T00:00:00Z",
        },
    )

    try:
        restore_to = str(tmp_path / "restores" / "blocked-by-restore")
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap002",
                "paths": ["/data"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 409, resp.text
        message = resp.json().get("message", "")
        assert "restore" in message.lower(), f"Expected 'restore' in message, got: {message!r}"
    finally:
        lock.unlink(missing_ok=True)
        restore_lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Backup blocked when restore is running
# ---------------------------------------------------------------------------


async def test_backup_blocked_when_restore_running(tmp_path):
    """BackupOrchestrator._acquire_lock returns False when restore.lock exists."""
    from app.core.config import ArkiveConfig
    from app.services.orchestrator import BackupOrchestrator

    config = ArkiveConfig(config_dir=tmp_path)
    orchestrator = BackupOrchestrator(
        discovery=None,
        db_dumper=None,
        flash_backup=MagicMock(),
        backup_engine=MagicMock(),
        cloud_manager=MagicMock(),
        notifier=MagicMock(),
        event_bus=MagicMock(),
        config=config,
    )

    # Patch the module-level constants so orchestrator sees our tmp_path
    restore_lock_path = tmp_path / "restore.lock"
    lock_file_path = tmp_path / "backup.lock"

    _write_lock(
        restore_lock_path,
        {
            "pid": os.getpid(),
            "started_at": "2026-01-01T00:00:00Z",
        },
    )

    try:
        with (
            patch("app.services.orchestrator.RESTORE_LOCK_FILE", restore_lock_path),
            patch("app.services.orchestrator.LOCK_FILE", lock_file_path),
        ):
            result = orchestrator._acquire_lock("test-run-001")

        assert result is False, "Expected _acquire_lock to return False while restore is running"
        assert not lock_file_path.exists(), "Backup lock file should not have been created"
    finally:
        lock_file_path.unlink(missing_ok=True)
        restore_lock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Stale restore lock (dead PID) is cleaned up automatically
# ---------------------------------------------------------------------------


async def test_restore_lock_stale_recovery(client, tmp_path):
    """POST /api/restore succeeds when restore.lock holds a dead PID."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(
        return_value={
            "status": "success",
            "output": "restored 1 file",
        }
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    lock = _lock_file()
    restore_lock = _restore_lock_file()

    # Dead PID that definitely does not exist
    dead_pid = 999999
    _write_lock(
        restore_lock,
        {
            "pid": dead_pid,
            "proc_start_time": "9999999999",  # bogus — won't match any recycled PID
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    assert not os.path.exists(f"/proc/{dead_pid}"), f"PID {dead_pid} unexpectedly alive"

    try:
        restore_to = str(tmp_path / "restores" / "stale")
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap-stale",
                "paths": ["/data"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        mock_engine.restore.assert_called_once()
    finally:
        app.state.backup_engine = original_engine
        lock.unlink(missing_ok=True)
        restore_lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Lock released after successful restore
# ---------------------------------------------------------------------------


async def test_restore_lock_released_after_success(client, tmp_path):
    """RESTORE_LOCK_FILE is removed after a successful restore operation."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(
        return_value={
            "status": "success",
            "output": "restored 5 files",
        }
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    lock = _lock_file()
    restore_lock = _restore_lock_file()
    lock.unlink(missing_ok=True)
    restore_lock.unlink(missing_ok=True)

    try:
        restore_to = str(tmp_path / "restores" / "success")
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap-success",
                "paths": ["/data"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"

        assert not restore_lock.exists(), "restore.lock should be removed after successful restore"
    finally:
        app.state.backup_engine = original_engine
        lock.unlink(missing_ok=True)
        restore_lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. Lock released after failed restore (try/finally guarantee)
# ---------------------------------------------------------------------------


async def test_restore_lock_released_after_failure(client, tmp_path):
    """RESTORE_LOCK_FILE is removed even when the restore engine raises an exception.

    Uses raise_app_exceptions=False so the unhandled RuntimeError is returned
    as a 500 response rather than re-raised into the test.
    """
    from httpx import ASGITransport, AsyncClient

    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(side_effect=RuntimeError("restic crashed"))

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    lock = _lock_file()
    restore_lock = _restore_lock_file()
    lock.unlink(missing_ok=True)
    restore_lock.unlink(missing_ok=True)

    # Use raise_app_exceptions=False so the 500 is returned rather than raised
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            restore_to = str(tmp_path / "restores" / "fail")
            resp = await ac.post(
                "/api/restore",
                json={
                    "target": target_id,
                    "snapshot_id": "snap-fail",
                    "paths": ["/data"],
                    "restore_to": restore_to,
                },
                headers=auth_headers(api_key),
            )
        assert resp.status_code == 500, resp.text

        assert not restore_lock.exists(), "restore.lock should be removed even after a failed restore"
    finally:
        app.state.backup_engine = original_engine
        lock.unlink(missing_ok=True)
        restore_lock.unlink(missing_ok=True)


async def test_restore_run_persisted_on_success(client, tmp_path):
    """Successful restores should create a persisted restore_runs record."""
    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(
        return_value={
            "status": "success",
            "output": "restored 3 files",
        }
    )

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    restore_to = str(tmp_path / "restores" / "persisted-success")
    try:
        resp = await client.post(
            "/api/restore",
            json={
                "target": target_id,
                "snapshot_id": "snap-persist-success",
                "paths": ["/data"],
                "restore_to": restore_to,
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text

        async with aiosqlite.connect(app.state.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, snapshot_id, target_id, restore_to,"
                " error_message FROM restore_runs"
                " ORDER BY started_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row["status"] == "success"
        assert row["snapshot_id"] == "snap-persist-success"
        assert row["target_id"] == target_id
        assert row["restore_to"] == restore_to
        assert row["error_message"] in (None, "")
    finally:
        app.state.backup_engine = original_engine


async def test_restore_run_persisted_on_failure(client, tmp_path):
    """Failed restores should persist a failed restore_runs record."""
    from httpx import ASGITransport, AsyncClient

    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_target(client, api_key, tmp_path)

    mock_engine = AsyncMock()
    mock_engine.restore = AsyncMock(side_effect=RuntimeError("restore interrupted"))

    app = client._transport.app
    original_engine = app.state.backup_engine
    app.state.backup_engine = mock_engine

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    restore_to = str(tmp_path / "restores" / "persisted-failure")
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/restore",
                json={
                    "target": target_id,
                    "snapshot_id": "snap-persist-fail",
                    "paths": ["/data"],
                    "restore_to": restore_to,
                },
                headers=auth_headers(api_key),
            )
        assert resp.status_code == 500, resp.text

        async with aiosqlite.connect(app.state.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, snapshot_id, target_id, restore_to,"
                " error_message FROM restore_runs"
                " ORDER BY started_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row["status"] == "failed"
        assert row["snapshot_id"] == "snap-persist-fail"
        assert row["target_id"] == target_id
        assert row["restore_to"] == restore_to
        assert "restore interrupted" in (row["error_message"] or "")
    finally:
        app.state.backup_engine = original_engine
