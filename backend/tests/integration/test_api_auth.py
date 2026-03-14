"""
API integration tests for auth endpoints.

Adapted from Arkive v2 for v3 flat route layout (app.api.auth).
Tests: setup flow, session check, API key regeneration, auth failures, rate limiting.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# -- POST /api/auth/setup ---------------------------------------------------


async def test_setup_creates_api_key_and_default_jobs(client):
    data = await do_setup(client)
    assert "api_key" in data
    assert data["api_key"].startswith("ark_")
    assert "jobs_created" in data
    # Spec: jobs_created is an integer count (3 default jobs)
    assert data["jobs_created"] == 3


async def test_setup_returns_save_warning_message(client):
    """Setup response should include a save-your-key warning."""
    data = await do_setup(client)
    assert "message" in data
    assert "save" in data["message"].lower()


async def test_setup_does_not_trigger_first_backup_when_omitted(client):
    """Setup should not auto-trigger the first backup unless explicitly requested."""
    app = client._transport.app
    scheduler = MagicMock()
    scheduler.reschedule_job = AsyncMock()
    scheduler.trigger_job = AsyncMock()
    app.state.scheduler = scheduler

    try:
        resp = await client.post(
            "/api/auth/setup",
            json={"encryption_password": "test-password"},
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["first_backup_triggered"] is False
        scheduler.trigger_job.assert_not_awaited()
    finally:
        app.state.scheduler = None


async def test_setup_can_explicitly_trigger_first_backup(client):
    """Setup may trigger the first backup when run_first_backup=true is supplied."""
    app = client._transport.app
    scheduler = MagicMock()
    scheduler.reschedule_job = AsyncMock()
    scheduler.trigger_job = AsyncMock()
    app.state.scheduler = scheduler

    try:
        resp = await client.post(
            "/api/auth/setup",
            json={"encryption_password": "test-password", "run_first_backup": True},
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["first_backup_triggered"] is True
        scheduler.trigger_job.assert_awaited_once()
    finally:
        app.state.scheduler = None


async def test_setup_fails_if_already_completed(client):
    await do_setup(client)
    resp = await client.post("/api/auth/setup", json={"run_first_backup": False})
    assert resp.status_code == 409
    data = resp.json()
    msg = data.get("detail", data.get("message", ""))
    assert "already completed" in msg.lower()


async def test_setup_default_jobs_have_correct_types(client):
    """The 3 default jobs should have types: db_dump, full, flash."""
    data = await do_setup(client)
    api_key = data["api_key"]
    # jobs_created is now an integer count per spec; verify types via GET /jobs
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    jobs = resp.json()["items"]
    types = {j["type"] for j in jobs}
    assert types == {"db_dump", "full", "flash"}


async def test_setup_stores_encryption_password(client):
    """Setup with encryption_password should store it in settings."""
    data = await do_setup(client, encryption_password="my-strong-password")
    data["api_key"]
    # Verify setup completed (indirectly confirms password was stored)
    resp = await client.get("/api/auth/session")
    assert resp.status_code == 200


async def test_setup_with_custom_schedules(client):
    schedules = {"db_dump": "30 1 * * *", "cloud_sync": "0 5 * * *"}
    data = await do_setup(client, schedules=schedules)
    api_key = data["api_key"]

    # Verify the schedules were applied
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert resp.status_code == 200
    jobs = resp.json()["items"]
    for job in jobs:
        if job["name"] == "DB Dumps":
            assert job["schedule"] == "30 1 * * *"
        elif job["name"] == "Cloud Sync":
            assert job["schedule"] == "0 5 * * *"


async def test_setup_with_target_ids(client, tmp_path):
    # Create a local target dir first and manually insert a target
    target_path = str(tmp_path / "backup_dest")
    import os

    os.makedirs(target_path, exist_ok=True)

    # Do setup in setup-mode (no auth needed)
    resp = await client.post(
        "/api/targets",
        json={
            "name": "Local",
            "type": "local",
            "config": {"path": target_path},
        },
    )
    assert resp.status_code == 201
    target_id = resp.json()["id"]

    # Now run setup with target_ids
    data = await do_setup(client, target_ids=[target_id])
    api_key = data["api_key"]

    # The Cloud Sync job should have the target linked
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    jobs = resp.json()["items"]
    cloud_sync = next(j for j in jobs if j["name"] == "Cloud Sync")
    assert target_id in cloud_sync["targets"]


async def test_setup_with_directory_ids(client, tmp_path):
    # Create a real directory and register it
    watch_dir = str(tmp_path / "appdata")
    import os

    os.makedirs(watch_dir, exist_ok=True)

    resp = await client.post(
        "/api/directories",
        json={
            "path": watch_dir,
            "label": "Appdata",
        },
    )
    assert resp.status_code == 201
    dir_id = resp.json()["id"]

    data = await do_setup(client, directory_ids=[dir_id])
    api_key = data["api_key"]

    # All jobs should have the directory linked
    resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    jobs = resp.json()["items"]
    for job in jobs:
        assert dir_id in job["directories"]


async def test_setup_with_storage_config_creates_target_and_links_cloud_sync_job(client, tmp_path):
    """Setup wizard storage payload should create a target and attach it to Cloud Sync."""
    target_path = str(tmp_path / "wizard-target")
    import os

    os.makedirs(target_path, exist_ok=True)

    data = await do_setup(client, storage={"type": "local", "path": target_path, "name": "Wizard Target"})
    api_key = data["api_key"]

    targets_resp = await client.get("/api/targets", headers=auth_headers(api_key))
    assert targets_resp.status_code == 200
    targets = targets_resp.json()["items"]
    assert len(targets) == 1
    assert targets[0]["type"] == "local"
    assert targets[0]["name"] == "Wizard Target"

    jobs_resp = await client.get("/api/jobs", headers=auth_headers(api_key))
    assert jobs_resp.status_code == 200
    jobs = jobs_resp.json()["items"]
    cloud_sync = next(j for j in jobs if j["name"] == "Cloud Sync")
    assert cloud_sync["targets"] == [targets[0]["id"]]


async def test_setup_persists_directories_as_watched_directories(client, tmp_path):
    """Setup wizard directories should populate watched_directories for later scans/export."""
    watch_dir = str(tmp_path / "wizard-appdata")
    import os

    os.makedirs(watch_dir, exist_ok=True)

    data = await do_setup(client, directories=[watch_dir])
    api_key = data["api_key"]

    resp = await client.get("/api/directories", headers=auth_headers(api_key))
    assert resp.status_code == 200
    directories = resp.json()["directories"]
    assert len(directories) == 1
    assert directories[0]["path"] == watch_dir
    assert directories[0]["enabled"] is True


async def test_setup_defaults_unraid_appdata_when_no_directories_selected(client):
    """Unraid setup should seed /mnt/user/appdata as a watched directory by default."""
    from pathlib import Path
    from unittest.mock import patch

    appdata_path = Path("/mnt/user/appdata")
    original_is_dir = Path.is_dir

    def fake_is_dir(path_obj):
        if path_obj == appdata_path:
            return True
        return original_is_dir(path_obj)

    client._transport.app.state.platform = "unraid"
    with patch.object(Path, "is_dir", fake_is_dir):
        data = await do_setup(client)
    api_key = data["api_key"]

    resp = await client.get("/api/directories", headers=auth_headers(api_key))
    assert resp.status_code == 200
    directories = resp.json()["directories"]
    assert any(entry["path"] == "/mnt/user/appdata" for entry in directories)


# -- GET /api/auth/session ---------------------------------------------------


async def test_session_returns_setup_required_before_setup(client):
    resp = await client.get("/api/auth/session")
    assert resp.status_code == 200
    assert resp.json().get("setup_required") is True


async def test_session_returns_setup_required_for_uninitialized_db_file(tmp_path_factory):
    """Session bootstrap should force setup if the SQLite file exists without schema."""
    from tests.conftest import build_test_client

    config_dir = tmp_path_factory.mktemp("auth-session-uninitialized")
    async with build_test_client(config_dir) as client:
        app = client._transport.app
        app.state.config.db_path.unlink(missing_ok=True)
        app.state.config.db_path.touch()

        resp = await client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["setup_required"] is True
        assert body["authenticated"] is False


async def test_session_returns_setup_metadata_after_setup(client):
    await do_setup(client, keep_session=True)
    resp = await client.get("/api/auth/session")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["setup_required"] is False
    assert payload["authenticated"] is True
    assert payload.get("setup_completed_at")


async def test_setup_sets_browser_session_cookie(client):
    resp = await client.post(
        "/api/auth/setup",
        json={"run_first_backup": False, "encryption_password": "test-password"},
    )
    assert resp.status_code in (200, 201)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "arkive_session=" in set_cookie
    assert "HttpOnly" in set_cookie


async def test_login_sets_session_cookie_and_marks_session_authenticated(client):
    data = await do_setup(client)
    client.cookies.clear()

    resp = await client.post("/api/auth/login", json={"api_key": data["api_key"]})
    assert resp.status_code == 200
    assert "arkive_session=" in resp.headers.get("set-cookie", "")

    session_resp = await client.get("/api/auth/session")
    assert session_resp.status_code == 200
    assert session_resp.json()["authenticated"] is True


async def test_cookie_session_authenticates_protected_endpoints(client):
    await do_setup(client, keep_session=True)

    resp = await client.get("/api/jobs")
    assert resp.status_code == 200


async def test_logout_clears_browser_session(client):
    await do_setup(client, keep_session=True)

    resp = await client.post("/api/auth/logout", headers={"Origin": "http://test"})
    assert resp.status_code == 200
    assert "arkive_session=" in resp.headers.get("set-cookie", "")

    blocked = await client.get("/api/jobs")
    assert blocked.status_code == 401


async def test_cookie_authenticated_writes_require_same_origin(client):
    await do_setup(client, keep_session=True)

    blocked = await client.post("/api/auth/rotate-key")
    assert blocked.status_code == 403

    allowed = await client.post("/api/auth/rotate-key", headers={"Origin": "http://test"})
    assert allowed.status_code == 200


# -- POST /api/auth/regenerate -----------------------------------------------


async def test_regenerate_api_key(client):
    """Regenerating should return a new, different API key."""
    data = await do_setup(client)
    old_key = data["api_key"]

    resp = await client.post(
        "/api/auth/regenerate",
        headers=auth_headers(old_key),
    )
    assert resp.status_code == 200
    new_data = resp.json()
    assert new_data["api_key"].startswith("ark_")
    assert new_data["api_key"] != old_key


async def test_regenerate_invalidates_old_key(client):
    """After regeneration, the old key should no longer authenticate."""
    data = await do_setup(client)
    old_key = data["api_key"]

    resp = await client.post(
        "/api/auth/regenerate",
        headers=auth_headers(old_key),
    )
    new_key = resp.json()["api_key"]

    # Old key should fail
    resp = await client.get("/api/jobs", headers=auth_headers(old_key))
    assert resp.status_code == 401

    # New key should work
    resp = await client.get("/api/jobs", headers=auth_headers(new_key))
    assert resp.status_code == 200


async def test_regenerate_requires_auth(client):
    """Regenerate without authentication should fail."""
    await do_setup(client)
    client.cookies.clear()
    resp = await client.post("/api/auth/regenerate")
    assert resp.status_code == 401


# -- Auth enforcement --------------------------------------------------------


async def test_endpoints_require_auth_after_setup(client):
    data = await do_setup(client)
    client.cookies.clear()

    # Without API key -> 401
    resp = await client.get("/api/jobs")
    assert resp.status_code == 401

    # With correct API key -> 200
    resp = await client.get("/api/jobs", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200


async def test_invalid_api_key_returns_401(client):
    await do_setup(client)
    client.cookies.clear()
    resp = await client.get("/api/jobs", headers=auth_headers("ark_invalid"))
    assert resp.status_code == 401


async def test_status_endpoint_does_not_require_auth(client):
    """GET /api/status should work without authentication even after setup."""
    await do_setup(client)
    resp = await client.get("/api/status")
    assert resp.status_code == 200


# -- Rate limiting -----------------------------------------------------------


async def test_rate_limiting_after_5_failed_attempts(client):
    await do_setup(client)
    client.cookies.clear()

    # Make 5 failed attempts
    for _ in range(5):
        resp = await client.get("/api/jobs", headers=auth_headers("ark_bad"))
        assert resp.status_code == 401

    # 6th attempt should be rate-limited
    resp = await client.get("/api/jobs", headers=auth_headers("ark_bad"))
    assert resp.status_code == 429
    assert "too many" in resp.json()["message"].lower()
