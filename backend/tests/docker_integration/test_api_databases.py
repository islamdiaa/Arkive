"""Phase 8: API integration tests for database dumps with FakeDockerClient."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.db_dumper import DBDumper
from app.services.discovery import DiscoveryEngine
from tests.fakes.fake_docker import create_fake_docker_client


@pytest_asyncio.fixture
async def db_client(tmp_path):
    """Create a test client with FakeDockerClient for discovery + dumps."""
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

        fake_docker = create_fake_docker_client()
        discovery = DiscoveryEngine(fake_docker, test_config)
        db_dumper = DBDumper(fake_docker, test_config)

        test_app.state.config = test_config
        test_app.state.event_bus = MagicMock()
        test_app.state.docker_client = fake_docker
        test_app.state.discovery = discovery
        test_app.state.db_dumper = db_dumper
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
            # Pre-scan to populate discovered_containers table
            await ac.post("/api/discover/scan")
            yield ac

    deps_mod._failed_attempts.clear()
    deps_mod._lockouts.clear()
    deps_mod._config = original_deps_config
    _reset_fernet()
    del os.environ["ARKIVE_CONFIG_DIR"]


@pytest.mark.asyncio
class TestDatabasesAPI:
    """Database API tests with FakeDockerClient."""

    async def test_list_databases_after_scan(self, db_client):
        """GET /api/databases → all discovered DBs."""
        resp = await db_client.get("/api/databases")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0
        db_types = {d["db_type"] for d in data["items"]}
        assert "postgres" in db_types

    async def test_dump_postgres_via_api(self, db_client):
        """POST /api/databases/fake-postgres/testdb/dump → success."""
        resp = await db_client.post("/api/databases/fake-postgres/testdb/dump")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["db_type"] == "postgres"
        assert data["dump_size_bytes"] > 0

    async def test_dump_nonexistent_container(self, db_client):
        """POST /api/databases/nope/x/dump → 404."""
        resp = await db_client.post("/api/databases/nope/x/dump")
        assert resp.status_code == 404
