"""Phase 6: Discovery engine tests with FakeDockerClient."""

import sqlite3

import pytest
import yaml

from app.services.discovery import DiscoveryEngine
from tests.fakes.fake_docker import (
    FakeContainer,
    FakeDockerClient,
    create_empty_docker_client,
)


@pytest.mark.asyncio
class TestDiscoveryScan:
    """Discovery engine with FakeDockerClient."""

    async def test_discovery_scan_all(self, tmp_config, fake_docker):
        """6 containers → detects postgres, mariadb, mongodb, redis from image patterns."""
        engine = DiscoveryEngine(fake_docker, tmp_config)
        containers = await engine.scan()

        assert len(containers) == 6
        names = {c.name for c in containers}
        assert names == {
            "fake-postgres",
            "fake-mariadb",
            "fake-mongo",
            "fake-redis",
            "fake-vaultwarden",
            "fake-adguard",
        }

        # Collect all DB types detected
        db_types = set()
        for c in containers:
            for db in c.databases:
                db_types.add(db.db_type)

        assert "postgres" in db_types
        assert "mariadb" in db_types
        assert "mongodb" in db_types
        assert "redis" in db_types

    async def test_discovery_profile_match(self, tmp_config, fake_docker):
        """fake-adguard matches the adguard profile after Phase 1 fix."""
        engine = DiscoveryEngine(fake_docker, tmp_config)
        containers = await engine.scan()

        adguard = next(c for c in containers if c.name == "fake-adguard")
        assert adguard.profile == "adguard"

    async def test_discovery_image_regex_postgres(self, tmp_config, fake_docker):
        """postgres:16 → detected as postgres."""
        engine = DiscoveryEngine(fake_docker, tmp_config)
        containers = await engine.scan()

        pg = next(c for c in containers if c.name == "fake-postgres")
        assert any(db.db_type == "postgres" for db in pg.databases)

    async def test_discovery_image_regex_mariadb(self, tmp_config, fake_docker):
        """mariadb:11 → detected as mariadb."""
        engine = DiscoveryEngine(fake_docker, tmp_config)
        containers = await engine.scan()

        maria = next(c for c in containers if c.name == "fake-mariadb")
        assert any(db.db_type == "mariadb" for db in maria.databases)

    async def test_discovery_image_regex_redis(self, tmp_config, fake_docker):
        """redis:7 → detected as redis."""
        engine = DiscoveryEngine(fake_docker, tmp_config)
        containers = await engine.scan()

        redis_c = next(c for c in containers if c.name == "fake-redis")
        assert any(db.db_type == "redis" for db in redis_c.databases)

    async def test_discovery_sqlite_from_mounts(self, tmp_config, tmp_path):
        """Vaultwarden with bind mount + real db.sqlite3 → profile-based SQLite detection."""
        # Create a real sqlite3 file matching the vaultwarden profile path
        # Profile says: path: "/data/db.sqlite3", mount destination: "/data"
        # So host_path = mount_source + "db.sqlite3"
        vw_data = tmp_path / "vaultwarden-data"
        vw_data.mkdir()
        db_file = vw_data / "db.sqlite3"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        vw_container = FakeContainer(
            name="test-vaultwarden",
            image_tags=["vaultwarden/server:latest"],
            mounts=[
                {
                    "Type": "bind",
                    "Source": str(vw_data),
                    "Destination": "/data",
                    "RW": True,
                }
            ],
        )

        client = FakeDockerClient([vw_container])
        engine = DiscoveryEngine(client, tmp_config)
        containers = await engine.scan()

        vw = next((c for c in containers if c.name == "test-vaultwarden"), None)
        assert vw is not None
        assert vw.profile == "vaultwarden"
        sqlite_dbs = [db for db in vw.databases if db.db_type == "sqlite"]
        assert len(sqlite_dbs) == 1
        assert sqlite_dbs[0].db_name == "db.sqlite3"
        assert sqlite_dbs[0].host_path == str(db_file)

    async def test_discovery_empty(self, tmp_config):
        """No containers → empty list."""
        client = create_empty_docker_client()
        engine = DiscoveryEngine(client, tmp_config)
        containers = await engine.scan()
        assert containers == []


class TestProfileMatching:
    """Verify the Phase 1 profile matching fix works correctly."""

    def test_match_profile_root_level_image_patterns(self, tmp_config):
        """Profile with image_patterns at root should match."""
        engine = DiscoveryEngine.__new__(DiscoveryEngine)
        engine.profiles = [
            {"name": "adguard", "image_patterns": ["adguard/adguardhome"]},
            {"name": "_fallback", "image_patterns": []},
        ]

        result = engine._match_profile("adguard/adguardhome")
        assert result is not None
        assert result["name"] == "adguard"

    def test_match_profile_no_match(self, tmp_config):
        """Image that doesn't match any profile returns None."""
        engine = DiscoveryEngine.__new__(DiscoveryEngine)
        engine.profiles = [
            {"name": "adguard", "image_patterns": ["adguard/adguardhome"]},
        ]

        result = engine._match_profile("nginx:latest")
        assert result is None

    def test_get_fallback_profile(self, tmp_config):
        """Fallback profile identified by name == '_fallback'."""
        engine = DiscoveryEngine.__new__(DiscoveryEngine)
        engine.profiles = [
            {"name": "adguard", "image_patterns": ["adguard/adguardhome"]},
            {"name": "_fallback", "image_patterns": []},
        ]

        result = engine._get_fallback_profile()
        assert result is not None
        assert result["name"] == "_fallback"

    def test_real_profile_yaml_matches(self, tmp_config):
        """Load a real YAML profile and verify it matches."""
        profiles_dir = tmp_config.profiles_dir
        adguard_path = profiles_dir / "adguard.yaml"
        if not adguard_path.exists():
            pytest.skip("Profiles directory not available")

        profile = yaml.safe_load(adguard_path.read_text())
        engine = DiscoveryEngine.__new__(DiscoveryEngine)
        engine.profiles = [profile]

        result = engine._match_profile("adguard/adguardhome")
        assert result is not None
        assert result["name"] == "adguard"
