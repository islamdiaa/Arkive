"""
Shared fixtures for Arkive v3 test suite.

Provides:
- Async test client with isolated temp database and config directory
- Helper to run initial setup (returns API key)
- Auth header builder
- Mock Docker client fixture
- Mock config fixture
- Daemon thread cleanup to prevent aiosqlite hangs
"""

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

SLOW_DIR_MARKERS = {
    "tests/cloud_providers/": ("provider", "slow"),
    "tests/docker_integration/": ("docker", "slow"),
}

LIVE_PATH_MARKERS = {
    "tests/runtime_qa_api.py": ("live", "slow"),
    "tests/runtime_qa_cli.py": ("live", "slow"),
}


# ---------------------------------------------------------------------------
# Async test client — isolated DB, no scheduler/Docker dependencies
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(tmp_path):
    """
    Create a fresh test client with isolated database and config dir.
    Bypasses the lifespan to avoid scheduler/Docker dependencies.
    """
    async with build_test_client(tmp_path) as ac:
        yield ac


@asynccontextmanager
async def build_test_client(config_dir: Path):
    """Build an isolated test client for a given config dir."""
    db_path = config_dir / "arkive.db"
    previous_config_dir = os.environ.get("ARKIVE_CONFIG_DIR")
    os.environ["ARKIVE_CONFIG_DIR"] = str(config_dir)

    # Patch config before importing anything that reads it
    import app.core.config as cfg_mod

    original_config = cfg_mod.ArkiveConfig

    class TestConfig(original_config):
        """Config override for tests with temp paths."""

        def __init__(self, **kwargs):
            kwargs.setdefault("config_dir", config_dir)
            super().__init__(**kwargs)

    # Patch the database module to use our temp DB path
    import app.core.database as db_mod

    await db_mod.init_db(db_path)

    # Ensure encryption keyfile exists in the temp config dir and set module cache
    from app.core.security import _load_fernet_from_dir, _reset_fernet

    _reset_fernet()  # Clear any cached instance from previous test
    _load_fernet_from_dir(str(config_dir))

    # Patch dependencies module config
    import app.core.dependencies as deps_mod

    original_deps_config = deps_mod._config
    test_config = TestConfig()
    deps_mod._config = test_config

    # Patch scheduler functions to no-ops for tests
    with (
        patch("app.services.scheduler.ArkiveScheduler", MagicMock()),
        patch("app.core.config.ArkiveConfig", TestConfig),
    ):
        from app.api.auth import _reset_setup_rate_limit
        from app.main import create_app

        _reset_setup_rate_limit()

        test_app = create_app()
        # Override lifespan by skipping it — we already initialized DB
        test_app.router.lifespan_context = _noop_lifespan

        # Attach state that routes may need
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
        test_app.state.platform = "unraid"

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    # Reset rate limiter state between tests
    deps_mod._failed_attempts.clear()
    deps_mod._lockouts.clear()
    deps_mod._config = original_deps_config

    # Reset setup endpoint rate-limit state between tests
    import app.api.auth as auth_mod

    auth_mod._setup_attempts.clear()

    # Reset security module cache
    from app.core.security import _reset_fernet

    _reset_fernet()

    if previous_config_dir is None:
        os.environ.pop("ARKIVE_CONFIG_DIR", None)
    else:
        os.environ["ARKIVE_CONFIG_DIR"] = previous_config_dir


@asynccontextmanager
async def _noop_lifespan(app):
    """No-op lifespan for tests — DB already initialized."""
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def do_setup(client: AsyncClient, keep_session: bool = False, **kwargs) -> dict:
    """Helper to run setup and return the response JSON including api_key.

    Most tests exercise header-based API auth and should not inherit the
    browser-session cookie emitted by setup. Opt in with keep_session=True
    when testing the browser session flow explicitly.
    """
    body = {"run_first_backup": False, "encryption_password": "test-password", **kwargs}
    resp = await client.post("/api/auth/setup", json=body)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    if not keep_session:
        client.cookies.clear()
    return data


def auth_headers(api_key: str) -> dict:
    """Return auth headers dict."""
    return {"X-API-Key": api_key}


# ---------------------------------------------------------------------------
# Mock Docker client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client with common container methods."""
    client = MagicMock()
    client.containers = MagicMock()
    client.containers.list = MagicMock(return_value=[])
    client.containers.get = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Mock config fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config(tmp_path):
    """Create a test ArkiveConfig with temp directories."""
    from app.core.config import ArkiveConfig

    config = ArkiveConfig(config_dir=tmp_path)
    config.ensure_dirs()
    return config


# ---------------------------------------------------------------------------
# Test API key fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def test_api_key():
    """Generate a test API key."""
    from app.core.security import generate_api_key

    return generate_api_key()


# ---------------------------------------------------------------------------
# Daemon thread cleanup — prevents aiosqlite hangs on exit
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):
    """Force exit if orphaned aiosqlite daemon threads would block shutdown.

    aiosqlite spawns a daemon thread per connection that blocks on
    SimpleQueue.get(). When connections are GC'd without explicit close(),
    the thread never receives the stop sentinel and blocks process exit.
    """
    alive = [t for t in threading.enumerate() if t.is_alive() and t != threading.main_thread() and t.daemon]
    if alive:
        os._exit(exitstatus)


def pytest_collection_modifyitems(config, items):
    """Auto-assign cost-tier markers based on test path.

    This keeps CI/local suite splitting declarative without requiring every
    expensive test file to repeat marker boilerplate.
    """
    for item in items:
        rel_path = item.nodeid.split("::", 1)[0].replace("\\", "/")

        for prefix, marker_names in SLOW_DIR_MARKERS.items():
            if rel_path.startswith(prefix):
                for marker_name in marker_names:
                    item.add_marker(getattr(pytest.mark, marker_name))
                break

        marker_names = LIVE_PATH_MARKERS.get(rel_path)
        if marker_names:
            for marker_name in marker_names:
                item.add_marker(getattr(pytest.mark, marker_name))
