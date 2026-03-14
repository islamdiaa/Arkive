"""Phase 5: Docker DB dump tests using FakeDockerClient exec_run."""

import gzip
import os

import pytest

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper
from tests.fakes.fake_docker import (
    FakeContainer,
    FakeDockerClient,
)


@pytest.mark.asyncio
class TestPostgresDump:
    """Postgres dump via fake exec_run."""

    async def test_postgres_dump_success(self, tmp_config, fake_docker):
        """Fake pg_dump yields SQL → gzipped file > 0 bytes."""
        dumper = DBDumper(docker_client=fake_docker, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-postgres",
            db_type="postgres",
            db_name="testdb",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "success"
        assert result.dump_size_bytes > 0
        assert result.dump_path.endswith(".sql.gz")
        # Verify gzip content
        with gzip.open(result.dump_path, "rb") as f:
            content = f.read()
        assert b"PostgreSQL database dump" in content
        assert b"CREATE TABLE" in content

    async def test_postgres_dump_empty(self, tmp_config):
        """Empty exec_run output → status=failed."""
        empty_pg = FakeContainer(
            name="empty-postgres",
            image_tags=["postgres:16"],
            exec_handlers={"pg_dump": (0, b"")},
        )
        client = FakeDockerClient([empty_pg])
        dumper = DBDumper(docker_client=client, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="empty-postgres",
            db_type="postgres",
            db_name="testdb",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "failed"
        assert "empty" in (result.error or "").lower()


@pytest.mark.asyncio
class TestMariaDBDump:
    """MariaDB dump via fake exec_run."""

    async def test_mariadb_dump_success(self, tmp_config, fake_docker):
        """Fake mysqldump yields SQL → gzipped file > 0 bytes."""
        dumper = DBDumper(docker_client=fake_docker, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-mariadb",
            db_type="mariadb",
            db_name="appdb",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "success"
        assert result.dump_size_bytes > 0
        with gzip.open(result.dump_path, "rb") as f:
            content = f.read()
        assert b"MySQL dump" in content

    async def test_mariadb_dump_empty(self, tmp_config):
        """Empty mysqldump output → status=failed."""
        empty_maria = FakeContainer(
            name="empty-mariadb",
            image_tags=["mariadb:11"],
            exec_handlers={"mariadb-dump": (0, b"")},
        )
        client = FakeDockerClient([empty_maria])
        dumper = DBDumper(docker_client=client, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="empty-mariadb",
            db_type="mariadb",
            db_name="appdb",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "failed"


@pytest.mark.asyncio
class TestMongoDBDump:
    """MongoDB dump via fake exec_run."""

    async def test_mongodb_dump_success(self, tmp_config, fake_docker):
        """Fake mongodump yields binary → gzipped file > 0 bytes."""
        dumper = DBDumper(docker_client=fake_docker, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-mongo",
            db_type="mongodb",
            db_name="myapp",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "success"
        assert result.dump_size_bytes > 0
        assert result.dump_path.endswith(".archive.gz")


@pytest.mark.asyncio
class TestRedisDump:
    """Redis dump via fake exec_run + file copy."""

    async def test_redis_dump_success(self, tmp_config, fake_docker, redis_rdb_dir):
        """SAVE returns OK + dump.rdb exists in mount → success."""
        # Create the fake dump.rdb file
        rdb_path = os.path.join(redis_rdb_dir, "dump.rdb")
        with open(rdb_path, "wb") as f:
            f.write(b"REDIS0011" + os.urandom(64))

        dumper = DBDumper(docker_client=fake_docker, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-redis",
            db_type="redis",
            db_name="redis",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "success"
        assert result.dump_size_bytes > 0
        assert result.dump_path.endswith(".rdb")

    async def test_redis_dump_no_rdb(self, tmp_config, fake_docker, redis_rdb_dir):
        """SAVE OK but no dump.rdb found → status=failed."""
        # Don't create dump.rdb — should fail
        dumper = DBDumper(docker_client=fake_docker, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-redis",
            db_type="redis",
            db_name="redis",
            host_path=None,
        )

        result = await dumper._dump_one(db)

        assert result.status == "failed"
        assert "not found" in (result.error or "").lower()
