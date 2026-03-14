"""Tests for Redis BGSAVE dump implementation."""

import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper


@pytest.fixture
def redis_db():
    return DiscoveredDatabase(
        container_name="test-redis",
        db_type="redis",
        db_name="redis",
    )


def test_redis_dump_uses_bgsave_not_save(tmp_path, redis_db):
    """Redis dump should use BGSAVE (non-blocking), not SAVE."""
    config = MagicMock()
    config.dump_dir = tmp_path

    # Track which commands were called
    commands_called = []
    call_count = {"LASTSAVE": 0}

    def fake_exec_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        commands_called.append(cmd_str)
        if "BGSAVE" in cmd_str:
            return (0, b"Background saving started\n")
        if "LASTSAVE" in cmd_str:
            call_count["LASTSAVE"] += 1
            if call_count["LASTSAVE"] <= 1:
                return (0, b"(integer) 1709000000\n")
            return (0, b"(integer) 1709000001\n")
        if "CONFIG GET dir" in cmd_str:
            return (0, b"dir\n/data\n")
        if "CONFIG GET dbfilename" in cmd_str:
            return (0, b"dbfilename\ndump.rdb\n")
        return (0, b"OK\n")

    # Create a fake RDB file
    rdb_source = tmp_path / "redis-data"
    rdb_source.mkdir()
    (rdb_source / "dump.rdb").write_bytes(b"REDIS0009" + b"\x00" * 100)

    fake_container = MagicMock()
    fake_container.exec_run = fake_exec_run
    fake_container.attrs = {"Mounts": [{"Source": str(rdb_source), "Destination": "/data"}]}

    docker_client = MagicMock()
    docker_client.containers.get.return_value = fake_container

    dumper = DBDumper(docker_client, config)
    with patch("time.sleep"):  # Skip polling delay
        result = dumper._dump_redis_blocking(redis_db)

    assert result.status == "success"
    assert result.dump_size_bytes > 0
    assert any("BGSAVE" in c for c in commands_called), "Should use BGSAVE"
    assert not any("redis-cli SAVE" == c for c in commands_called), "Should NOT use blocking SAVE"


def test_redis_bgsave_timeout(tmp_path, redis_db):
    """Should fail gracefully if BGSAVE doesn't complete in time."""
    config = MagicMock()
    config.dump_dir = tmp_path

    def fake_exec_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "BGSAVE" in cmd_str:
            return (0, b"Background saving started\n")
        if "LASTSAVE" in cmd_str:
            return (0, b"(integer) 1709000000\n")  # Never changes
        return (0, b"OK\n")

    fake_container = MagicMock()
    fake_container.exec_run = fake_exec_run
    fake_container.attrs = {"Mounts": []}

    docker_client = MagicMock()
    docker_client.containers.get.return_value = fake_container

    dumper = DBDumper(docker_client, config)
    with patch("time.sleep"):
        result = dumper._dump_redis_blocking(redis_db)

    assert result.status == "failed"
    assert "did not complete" in result.error


def test_redis_dump_falls_back_to_container_archive_without_bind_mount(tmp_path, redis_db):
    """Redis dump should succeed when dump.rdb exists in the container but not in bind mounts."""
    config = MagicMock()
    config.dump_dir = tmp_path

    call_count = {"LASTSAVE": 0}

    def fake_exec_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "BGSAVE" in cmd_str:
            return (0, b"Background saving started\n")
        if "LASTSAVE" in cmd_str:
            call_count["LASTSAVE"] += 1
            if call_count["LASTSAVE"] <= 1:
                return (0, b"(integer) 1709000000\n")
            return (0, b"(integer) 1709000001\n")
        if "CONFIG GET dir" in cmd_str:
            return (0, b"dir\n/data\n")
        if "CONFIG GET dbfilename" in cmd_str:
            return (0, b"dbfilename\ndump.rdb\n")
        return (0, b"OK\n")

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        payload = b"REDIS0009" + b"\x00" * 128
        info = tarfile.TarInfo(name="dump.rdb")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    archive_bytes.seek(0)

    fake_container = MagicMock()
    fake_container.exec_run = fake_exec_run
    fake_container.get_archive.return_value = ([archive_bytes.getvalue()], {})
    fake_container.attrs = {"Mounts": []}

    docker_client = MagicMock()
    docker_client.containers.get.return_value = fake_container

    dumper = DBDumper(docker_client, config)
    with patch("time.sleep"):
        result = dumper._dump_redis_blocking(redis_db)

    assert result.status == "success"
    assert result.dump_size_bytes > 0
    fake_container.get_archive.assert_called_once_with("/data/dump.rdb")
