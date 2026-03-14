"""FakeDockerClient — drop-in replacement for docker.from_env() in tests.

Simulates the docker-py API surface used by Arkive's DiscoveryEngine and DBDumper:
- containers.list(all=True)
- containers.get(name)
- container.exec_run(cmd, stream=False, demux=False)
- container.image.tags, .name, .status, .labels, .attrs
"""

import os
from collections.abc import Iterator
from typing import Any

# ---------------------------------------------------------------------------
# Fake streaming helpers
# ---------------------------------------------------------------------------


def _stream_chunks(data: bytes, chunk_size: int = 512) -> Iterator[tuple[bytes, None]]:
    """Yield (stdout_chunk, None) tuples mimicking demux=True streaming."""
    for i in range(0, len(data), chunk_size):
        yield (data[i : i + chunk_size], None)


# ---------------------------------------------------------------------------
# Realistic dump payloads
# ---------------------------------------------------------------------------

POSTGRES_DUMP = b"""\
-- PostgreSQL database dump
-- Dumped from database version 16.2
SET statement_timeout = 0;
SET lock_timeout = 0;
SET client_encoding = 'UTF8';

CREATE TABLE users (
    id serial PRIMARY KEY,
    name varchar(100) NOT NULL,
    email varchar(255) UNIQUE NOT NULL
);

INSERT INTO users VALUES (1, 'alice', 'alice@example.com');
INSERT INTO users VALUES (2, 'bob', 'bob@example.com');

-- PostgreSQL database dump complete
"""

MARIADB_DUMP = b"""\
-- MySQL dump 10.19  Distrib 10.11.6-MariaDB
-- Server version	11.2.2-MariaDB

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;

CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `users` VALUES (1,'alice'),(2,'bob');

-- Dump completed
"""

MONGODB_ARCHIVE = b"\x00\x01\x02\x03" + b"mongodump-archive-v1" + os.urandom(256)


# ---------------------------------------------------------------------------
# FakeImage
# ---------------------------------------------------------------------------


class FakeImage:
    """Simulates docker.models.images.Image."""

    def __init__(self, tags: list[str]):
        self._tags = tags

    @property
    def tags(self) -> list[str]:
        return self._tags


# ---------------------------------------------------------------------------
# FakeContainer
# ---------------------------------------------------------------------------


class FakeContainer:
    """Simulates docker.models.containers.Container."""

    def __init__(
        self,
        name: str,
        image_tags: list[str],
        status: str = "running",
        env: list[str] | None = None,
        mounts: list[dict] | None = None,
        labels: dict[str, str] | None = None,
        ports: dict[str, list[dict]] | None = None,
        exec_handlers: dict[str, Any] | None = None,
    ):
        self.name = name
        self.image = FakeImage(image_tags)
        self.status = status
        self.labels = labels or {}

        self.attrs = {
            "Config": {
                "Env": env or [],
                "Image": image_tags[0] if image_tags else "unknown",
                "Labels": self.labels,
            },
            "Mounts": mounts or [],
            "NetworkSettings": {
                "Ports": ports or {},
            },
        }

        # exec_handlers: command-prefix -> (exit_code, data_bytes)
        self._exec_handlers = exec_handlers or {}

    def exec_run(self, cmd, stream: bool = False, demux: bool = False, **kwargs) -> tuple[int, Any]:
        """Simulate docker exec_run. Accepts both string and list commands."""
        # Normalize list commands to string for prefix matching
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        for prefix, (exit_code, data) in self._exec_handlers.items():
            if cmd_str.startswith(prefix) or prefix in cmd_str:
                if stream and demux:
                    return (exit_code, _stream_chunks(data))
                elif stream:
                    return (exit_code, iter([data]))
                else:
                    return (exit_code, data)

        # Unknown command — return failure
        return (1, b"command not found" if not stream else iter([]))


# ---------------------------------------------------------------------------
# FakeContainerCollection
# ---------------------------------------------------------------------------


class FakeContainerCollection:
    """Simulates docker.models.containers.ContainerCollection."""

    def __init__(self, containers: list[FakeContainer]):
        self._containers = {c.name: c for c in containers}

    def list(self, all: bool = False) -> list[FakeContainer]:
        if all:
            return list(self._containers.values())
        return [c for c in self._containers.values() if c.status == "running"]

    def get(self, name: str) -> FakeContainer:
        if name not in self._containers:
            raise Exception(f"No such container: {name}")
        return self._containers[name]


# ---------------------------------------------------------------------------
# FakeDockerClient
# ---------------------------------------------------------------------------


class FakeDockerClient:
    """Drop-in replacement for docker.DockerClient."""

    def __init__(self, containers: list[FakeContainer] | None = None):
        self.containers = FakeContainerCollection(containers or [])

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Preconfigured containers factory
# ---------------------------------------------------------------------------


def create_preconfigured_containers(redis_rdb_dir: str | None = None) -> list[FakeContainer]:
    """Create the 6 preconfigured fake containers covering all DB types.

    Args:
        redis_rdb_dir: Path to a temp directory where a fake dump.rdb will be created.
                       If None, redis mount source will be /tmp/fake-redis-data.
    """
    redis_mount_source = redis_rdb_dir or "/tmp/fake-redis-data"

    # Redis — stateful exec_run to simulate BGSAVE + LASTSAVE polling
    _redis_lastsave_calls = [0]

    def _redis_exec_run(cmd, stream: bool = False, demux: bool = False, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "BGSAVE" in cmd_str:
            return (0, b"Background saving started\n")
        if "LASTSAVE" in cmd_str:
            _redis_lastsave_calls[0] += 1
            if _redis_lastsave_calls[0] <= 1:
                return (0, b"(integer) 1709000000\n")
            return (0, b"(integer) 1709000001\n")
        if "CONFIG GET dir" in cmd_str:
            return (0, b"dir\n/data\n")
        if "CONFIG GET dbfilename" in cmd_str:
            return (0, b"dbfilename\ndump.rdb\n")
        # Legacy SAVE and any other redis-cli commands
        return (0, b"OK\n")

    _redis_container = FakeContainer(
        name="fake-redis",
        image_tags=["redis:7"],
        mounts=[
            {
                "Type": "bind",
                "Source": redis_mount_source,
                "Destination": "/data",
                "RW": True,
            }
        ],
    )
    _redis_container.exec_run = _redis_exec_run

    containers = [
        # Postgres
        FakeContainer(
            name="fake-postgres",
            image_tags=["postgres:16"],
            env=[
                "POSTGRES_DB=testdb",
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=secret",
            ],
            exec_handlers={
                "pg_dump": (0, POSTGRES_DUMP),
            },
        ),
        # MariaDB
        FakeContainer(
            name="fake-mariadb",
            image_tags=["mariadb:11"],
            env=[
                "MYSQL_DATABASE=appdb",
                "MYSQL_ROOT_PASSWORD=secret",
            ],
            exec_handlers={
                "mariadb-dump": (0, MARIADB_DUMP),
            },
        ),
        # MongoDB
        FakeContainer(
            name="fake-mongo",
            image_tags=["mongo:7"],
            env=[
                "MONGO_INITDB_DATABASE=myapp",
            ],
            exec_handlers={
                "mongodump": (0, MONGODB_ARCHIVE),
            },
        ),
        _redis_container,
        # Vaultwarden (SQLite via bind mount)
        FakeContainer(
            name="fake-vaultwarden",
            image_tags=["vaultwarden/server:latest"],
            mounts=[
                {
                    "Type": "bind",
                    "Source": "/mnt/user/appdata/vaultwarden",
                    "Destination": "/data",
                    "RW": True,
                }
            ],
        ),
        # AdGuard Home (profile match, no DB)
        FakeContainer(
            name="fake-adguard",
            image_tags=["adguard/adguardhome"],
            mounts=[
                {
                    "Type": "bind",
                    "Source": "/mnt/user/appdata/adguard",
                    "Destination": "/opt/adguardhome/conf",
                    "RW": True,
                }
            ],
        ),
    ]

    return containers


def create_fake_docker_client(redis_rdb_dir: str | None = None) -> FakeDockerClient:
    """Create a FakeDockerClient with all 6 preconfigured containers."""
    return FakeDockerClient(create_preconfigured_containers(redis_rdb_dir))


def create_empty_docker_client() -> FakeDockerClient:
    """Create a FakeDockerClient with no containers."""
    return FakeDockerClient([])
