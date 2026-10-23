"""Integration tests for target ID validation in the targets API.

Verifies that path traversal and injection attempts are rejected with 400,
and that valid target IDs are accepted (returning 404 when not found in DB).

Uses a module-scoped client to avoid hitting the setup rate limiter.
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio(loop_scope="module")


# ---------------------------------------------------------------------------
# Module-scoped fixtures — one setup call for all tests in this module
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def module_client(tmp_path_factory):
    """Module-scoped test client — setup is called only once."""
    tmp_path = tmp_path_factory.mktemp("targets_validation")
    config_dir = tmp_path
    db_path = config_dir / "arkive.db"

    os.environ["ARKIVE_CONFIG_DIR"] = str(config_dir)

    import app.core.config as cfg_mod
    import app.core.database as db_mod
    import app.core.dependencies as deps_mod

    original_config = cfg_mod.ArkiveConfig
    original_deps_config = deps_mod._config

    class TestConfig(original_config):
        def __init__(self, **kwargs):
            kwargs.setdefault("config_dir", config_dir)
            super().__init__(**kwargs)

    await db_mod.init_db(db_path)

    from app.core.security import _load_fernet_from_dir, _reset_fernet

    _reset_fernet()
    _load_fernet_from_dir(str(config_dir))

    test_config = TestConfig()
    deps_mod._config = test_config

    with (
        patch("app.services.scheduler.ArkiveScheduler", MagicMock()),
        patch("app.core.config.ArkiveConfig", TestConfig),
    ):
        from app.main import create_app

        test_app = create_app()
        test_app.router.lifespan_context = _noop_lifespan
        test_app.state.config = test_config
        test_app.state.event_bus = MagicMock()
        test_app.state.docker_client = None
        test_app.state.discovery = None
        test_app.state.db_dumper = None
        test_app.state.flash_backup = None
        test_app.state.backup_engine = None
        test_app.state.cloud_manager = None
        test_app.state.notifier = None
        test_app.state.restore_plan = None
        test_app.state.orchestrator = None
        test_app.state.scheduler = None
        test_app.state.platform = "linux"

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    deps_mod._failed_attempts.clear()
    deps_mod._lockouts.clear()
    deps_mod._config = original_deps_config

    from app.core.security import _reset_fernet

    _reset_fernet()

    if "ARKIVE_CONFIG_DIR" in os.environ:
        del os.environ["ARKIVE_CONFIG_DIR"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def api_key(module_client):
    """Module-scoped api_key — do_setup runs once for all tests."""
    data = await do_setup(module_client)
    return data["api_key"]


# ---------------------------------------------------------------------------
# GET /{target_id} — ID validation
# ---------------------------------------------------------------------------


class TestGetTargetIdValidation:
    async def test_path_traversal_encoded_slash_returns_400_or_404(self, module_client, api_key):
        """URL-encoded slash in target ID — FastAPI may decode it; should be rejected."""
        resp = await module_client.get("/api/targets/..%2Fetc%2Fpasswd", headers=auth_headers(api_key))
        assert resp.status_code in (400, 404)

    async def test_special_chars_semicolon_returns_400(self, module_client, api_key):
        resp = await module_client.get("/api/targets/foo;rm+-rf", headers=auth_headers(api_key))
        assert resp.status_code == 400

    async def test_valid_id_not_found_returns_404(self, module_client, api_key):
        """A well-formed target ID that doesn't exist should return 404, not 400."""
        resp = await module_client.get("/api/targets/my-target_1", headers=auth_headers(api_key))
        assert resp.status_code == 404

    async def test_valid_alphanumeric_id_not_found_returns_404(self, module_client, api_key):
        resp = await module_client.get("/api/targets/abc12345", headers=auth_headers(api_key))
        assert resp.status_code == 404

    async def test_id_too_long_returns_400(self, module_client, api_key):
        """IDs longer than 64 chars should be rejected."""
        long_id = "a" * 65
        resp = await module_client.get(f"/api/targets/{long_id}", headers=auth_headers(api_key))
        assert resp.status_code == 400

    async def test_id_with_space_returns_400(self, module_client, api_key):
        """IDs with spaces (URL-encoded) should be rejected."""
        resp = await module_client.get("/api/targets/foo%20bar", headers=auth_headers(api_key))
        assert resp.status_code == 400

    async def test_id_with_dot_returns_400(self, module_client, api_key):
        """IDs with dots (e.g. relative path components) should be rejected."""
        resp = await module_client.get("/api/targets/foo.bar", headers=auth_headers(api_key))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /{target_id} — ID validation
# ---------------------------------------------------------------------------


class TestPutTargetIdValidation:
    async def test_special_chars_in_put_returns_400(self, module_client, api_key):
        resp = await module_client.put(
            "/api/targets/foo;bar",
            json={"name": "test"},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_id_too_long_in_put_returns_400(self, module_client, api_key):
        long_id = "b" * 65
        resp = await module_client.put(
            f"/api/targets/{long_id}",
            json={"name": "test"},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_valid_id_in_put_returns_404(self, module_client, api_key):
        resp = await module_client.put(
            "/api/targets/valid-id_1",
            json={"name": "test"},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /{target_id} — ID validation
# ---------------------------------------------------------------------------


class TestDeleteTargetIdValidation:
    async def test_special_chars_in_delete_returns_400(self, module_client, api_key):
        resp = await module_client.delete(
            "/api/targets/foo;rm+-rf",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_id_too_long_in_delete_returns_400(self, module_client, api_key):
        long_id = "c" * 65
        resp = await module_client.delete(
            f"/api/targets/{long_id}",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_valid_id_in_delete_returns_404(self, module_client, api_key):
        resp = await module_client.delete(
            "/api/targets/valid-id_99",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{target_id}/test — ID validation
# ---------------------------------------------------------------------------


class TestTestTargetIdValidation:
    async def test_special_chars_in_test_endpoint_returns_400(self, module_client, api_key):
        resp = await module_client.post(
            "/api/targets/foo;bar/test",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_id_too_long_in_test_endpoint_returns_400(self, module_client, api_key):
        long_id = "d" * 65
        resp = await module_client.post(
            f"/api/targets/{long_id}/test",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_valid_id_in_test_endpoint_returns_404(self, module_client, api_key):
        resp = await module_client.post(
            "/api/targets/valid-id_1/test",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{target_id}/usage — ID validation
# ---------------------------------------------------------------------------


class TestUsageTargetIdValidation:
    async def test_special_chars_in_usage_returns_400(self, module_client, api_key):
        resp = await module_client.get(
            "/api/targets/foo;bar/usage",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_id_too_long_in_usage_returns_400(self, module_client, api_key):
        long_id = "e" * 65
        resp = await module_client.get(
            f"/api/targets/{long_id}/usage",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 400

    async def test_valid_id_in_usage_returns_404(self, module_client, api_key):
        resp = await module_client.get(
            "/api/targets/valid-id_1/usage",
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 404
