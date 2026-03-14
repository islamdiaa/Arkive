"""Integration tests for first-boot bootstrap behavior."""

import pytest

from tests.conftest import build_test_client

pytestmark = pytest.mark.asyncio


async def test_status_handles_uninitialized_db_file(tmp_path_factory):
    """Status should degrade gracefully if the SQLite file exists before schema init completes."""
    config_dir = tmp_path_factory.mktemp("api-bootstrap-status")
    async with build_test_client(config_dir) as client:
        app = client._transport.app
        app.state.config.db_path.unlink(missing_ok=True)
        app.state.config.db_path.touch()

        resp = await client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded", "error")
        assert body["setup_completed"] is False
        assert body["targets"] == {"total": 0, "healthy": 0}


async def test_session_returns_setup_required_for_uninitialized_db_file(tmp_path_factory):
    """Session bootstrap should force setup if the SQLite file exists without schema."""
    config_dir = tmp_path_factory.mktemp("api-bootstrap-session")
    async with build_test_client(config_dir) as client:
        app = client._transport.app
        app.state.config.db_path.unlink(missing_ok=True)
        app.state.config.db_path.touch()

        resp = await client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["setup_required"] is True
        assert body["authenticated"] is False
