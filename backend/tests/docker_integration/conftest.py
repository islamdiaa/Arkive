"""Shared fixtures for Docker integration tests."""

import os
import sqlite3
from pathlib import Path

import pytest

from app.core.config import ArkiveConfig
from tests.fakes.fake_docker import (
    FakeDockerClient,
    create_fake_docker_client,
)


@pytest.fixture
def tmp_config(tmp_path):
    """Create an ArkiveConfig pointing at a temp directory."""
    os.environ["ARKIVE_CONFIG_DIR"] = str(tmp_path)
    config = ArkiveConfig(
        config_dir=tmp_path,
        profiles_dir=Path(__file__).resolve().parents[3] / "profiles",
        boot_config_path=tmp_path / "boot-config",
    )
    config.ensure_dirs()
    yield config
    os.environ.pop("ARKIVE_CONFIG_DIR", None)


@pytest.fixture
def fake_docker(tmp_path) -> FakeDockerClient:
    """Create a FakeDockerClient with preconfigured containers.

    The Redis container's mount source is set to a real tmpdir
    so dump.rdb tests can work with real files.
    """
    redis_dir = tmp_path / "redis-data"
    redis_dir.mkdir()
    return create_fake_docker_client(redis_rdb_dir=str(redis_dir))


@pytest.fixture
def redis_rdb_dir(fake_docker) -> str:
    """Return the Redis mount source directory path."""
    redis_container = fake_docker.containers.get("fake-redis")
    return redis_container.attrs["Mounts"][0]["Source"]


@pytest.fixture
def fake_boot_config(tmp_path):
    """Create a fake Unraid boot-config directory with representative files."""
    boot_dir = tmp_path / "boot-config"
    boot_dir.mkdir()
    (boot_dir / "ident.cfg").write_text("NAME=TestUnraid\n")
    (boot_dir / "go").write_text("#!/bin/bash\necho 'boot script'\n")
    (boot_dir / "network.cfg").write_text("IFACE=eth0\nIPADDR=192.168.1.100\n")
    syslinux = boot_dir / "syslinux"
    syslinux.mkdir()
    (syslinux / "syslinux.cfg").write_text("DEFAULT unraid\nLABEL unraid\n")
    return boot_dir


@pytest.fixture
def fake_sqlite_db(tmp_path):
    """Create a real SQLite database with test data in a tmpdir."""
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'alpha')")
    conn.execute("INSERT INTO items VALUES (2, 'beta')")
    conn.execute("INSERT INTO items VALUES (3, 'gamma')")
    conn.commit()
    conn.close()
    return db_path
