"""Phase 8: API integration tests for discovery with FakeDockerClient."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.discovery import DiscoveryEngine
from tests.fakes.fake_docker import create_fake_docker_client


@pytest_asyncio.fixture
async def discovery_client(tmp_path):
    """Create a test client with FakeDockerClient injected for discovery."""
    config_dir = tmp_path
    os.environ["ARKIVE_CONFIG_DIR"] = str(config_dir)

    import app.core.config as cfg_mod
    import app.core.database as db_mod
    import app.core.dependencies as deps_mod
    from app.core.security import _load_fernet_from_dir, _reset_fernet

    _reset_fernet()
    _load_fernet_from_dir(str(config_dir))

    await db_mod.init_db(config_dir / "arkive.db")

    original_config = cfg_mod.ArkiveConfig

    class TestConfig(original_config):
        def __init__(self, **kwargs):
            kwargs.setdefault("config_dir", config_dir)
            kwargs.setdefault("profiles_dir", Path(__file__).resolve().parents[3] / "profiles")
            super().__init__(**kwargs)

    original_deps_config = deps_mod._config
    test_config = TestConfig()
    test_config.ensure_dirs()
    deps_mod._config = test_config

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    with (
        patch("app.services.scheduler.ArkiveScheduler", MagicMock()),
        patch("app.core.config.ArkiveConfig", TestConfig),
    ):
        from app.main import create_app

        test_app = create_app()
        test_app.router.lifespan_context = _noop_lifespan

        # Inject FakeDockerClient
        fake_docker = create_fake_docker_client()
        discovery = DiscoveryEngine(fake_docker, test_config)

        test_app.state.config = test_config
        test_app.state.event_bus = MagicMock()
        test_app.state.docker_client = fake_docker
        test_app.state.discovery = discovery
        test_app.state.db_dumper = None
        test_app.state.flash_backup = None
        test_app.state.backup_engine = None
        test_app.state.cloud_manager = None
        test_app.state.notifier = None
        test_app.state.restore_plan = None
        test_app.state.orchestrator = None
        test_app.state.scheduler = None
        test_app.state.platform = "unraid"

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    deps_mod._failed_attempts.clear()
    deps_mod._lockouts.clear()
    deps_mod._config = original_deps_config
    _reset_fernet()
    del os.environ["ARKIVE_CONFIG_DIR"]


@pytest.mark.asyncio
class TestDiscoveryAPI:
    """Discovery API tests with FakeDockerClient."""

    async def test_post_scan_returns_containers(self, discovery_client):
        """POST /api/discover/scan → containers with databases."""
        resp = await discovery_client.post("/api/discover/scan")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_containers"] == 6
        assert data["running_containers"] == 6
        assert len(data["containers"]) == 6
        assert len(data["databases"]) > 0

    async def test_get_containers_after_scan(self, discovery_client):
        """GET /api/discover/containers → cached results after scan."""
        # First scan
        await discovery_client.post("/api/discover/scan")

        # Then list
        resp = await discovery_client.get("/api/discover/containers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 6

    async def test_scan_discovers_all_db_types(self, discovery_client):
        """Verify postgres, mariadb, mongodb, redis in scan results."""
        resp = await discovery_client.post("/api/discover/scan")
        data = resp.json()

        db_types = {d["db_type"] for d in data["databases"]}
        assert "postgres" in db_types
        assert "mariadb" in db_types
        assert "mongodb" in db_types
        assert "redis" in db_types

    async def test_scan_removes_stale_cached_containers(self, discovery_client):
        """A later scan should remove containers that are no longer present."""
        resp = await discovery_client.post("/api/discover/scan")
        assert resp.status_code == 200
        assert resp.json()["total_containers"] == 6

        app = discovery_client._transport.app
        fake_docker = app.state.docker_client
        fake_docker.containers._containers = {"fake-postgres": fake_docker.containers._containers["fake-postgres"]}

        resp = await discovery_client.post("/api/discover/scan")
        assert resp.status_code == 200
        assert resp.json()["total_containers"] == 1

        resp = await discovery_client.get("/api/discover/containers")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "fake-postgres"
