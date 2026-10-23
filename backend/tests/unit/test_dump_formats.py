"""Dump formats, Postgres cluster coverage and the dump manifest.

Covers: dump_compression none/gzip (plain .sql vs gzip --rsyncable .sql.gz),
trailer/truncation detection, cross-extension cleanup, verify_engine for plain
.sql, Postgres database enumeration in discovery, pg_dumpall --globals-only,
arkive-dumps-manifest.json, and Redis AUTH via REDISCLI_AUTH.
"""

import gzip
import json
import os
import re
import sqlite3
import time
import tracemalloc
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import __version__
from app.models.discovery import DiscoveredDatabase
from app.services import db_dumper as dumper_module
from app.services.db_dumper import (
    DUMP_MANIFEST_NAME,
    DBDumper,
    cleanup_dump_dir,
    dump_prefix,
    read_dump_manifest,
    write_dump_manifest,
    legacy_dump_prefix,
)
from app.services.verify_engine import VerifyEngine
from app.utils.subprocess_runner import CommandResult

TS_RE = r"\d{8}_\d{6}"


def pg_dump_output(db_name: str) -> bytes:
    return (
        b"--\n-- PostgreSQL database dump\n--\n\n"
        b"\\restrict k3y\n\nSET statement_timeout = 0;\nSET client_encoding = 'UTF8';\n"
        + f"CREATE DATABASE {db_name} WITH TEMPLATE = template0 ENCODING = 'UTF8';\n".encode()
        + b"COPY public.items (id, name) FROM stdin;\n"
        + b"".join(f"{i}\titem-{i}\n".encode() for i in range(200))
        + b"\\.\n\n--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict k3y\n\n"
    )


PG_GLOBALS = (
    b"--\n-- PostgreSQL database cluster dump\n--\n\n\\restrict k3y\n\n"
    b"SET default_transaction_read_only = off;\n\n--\n-- Roles\n--\n\n"
    b"CREATE ROLE alice;\nALTER ROLE alice WITH SUPERUSER INHERIT LOGIN PASSWORD 'SCRAM-SHA-256$4096:abc';\n\n"
    b"\\unrestrict k3y\n\n--\n-- PostgreSQL database cluster dump complete\n--\n\n"
)

MYSQL_OK = (
    b"-- MariaDB dump 10.19  Distrib 10.11.6-MariaDB, for debian-linux-gnu (x86_64)\n--\n"
    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `appdb`;\nUSE `appdb`;\n"
    + b"INSERT INTO `t` VALUES (1),(2),(3);\n" * 10
    + b"/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;\n\n-- Dump completed on 2026-10-22 22:00:00\n"
)


def _chunks(data: bytes, size: int = 97) -> list[bytes]:
    return [data[i:i + size] for i in range(0, len(data), size)]


def _container(name: str, env: list[str], handler, tags=("postgres:16",), status="running"):
    """Docker container double. ``handler(cmd) -> (exit_code, stdout, stderr)``."""
    container = MagicMock()
    container.name = name
    container.status = status
    container.image.tags = list(tags)
    container.attrs = {"Config": {"Env": env, "Image": tags[0] if tags else "sha256:abc"}, "Mounts": []}
    container.client.api = None  # not a docker.APIClient -> exec_run path
    container.calls = []

    def exec_run(cmd, demux=False, stream=False, environment=None, **kwargs):
        container.calls.append({"cmd": list(cmd), "environment": environment})
        code, out, err = handler(list(cmd))
        if stream:
            items = [(chunk, None) for chunk in _chunks(out)] + ([(None, err)] if err else [])
            return code, iter(items)
        return code, (out or None, err or None)

    container.exec_run.side_effect = exec_run
    return container


def _pg_handler(cmd):
    if cmd[0] == "pg_dump":
        return 0, pg_dump_output(cmd[-1]), b""
    if cmd[0] == "pg_dumpall":
        return 0, PG_GLOBALS, b""
    raise AssertionError(f"unexpected command {cmd}")


def _docker(*containers):
    client = MagicMock()
    by_name = {c.name: c for c in containers}

    def get(name):
        if name not in by_name:
            raise Exception(f"No such container: {name}")
        return by_name[name]

    client.containers.get.side_effect = get
    client.containers.list.return_value = list(containers)
    return client


def _set_compression(config, value: str | None, create_table: bool = True):
    with sqlite3.connect(config.db_path) as conn:
        if create_table:
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, "
                         "encrypted INTEGER DEFAULT 0, updated_at TEXT)")
            if value is not None:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dump_compression', ?)", (value,))
        conn.commit()


def _pg_db(container="pg", db="app"):
    return DiscoveredDatabase(container_name=container, db_type="postgres", db_name=db)


def _files(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


# ---------------------------------------------------------------------------
# dump_compression setting
# ---------------------------------------------------------------------------

class TestCompressionSetting:
    @pytest.mark.asyncio
    async def test_reads_gzip_from_settings(self, mock_config):
        _set_compression(mock_config, "gzip")
        assert await DBDumper(_docker(), mock_config)._load_compression() == "gzip"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value,create_table", [(None, True), (None, False), ("zstd", True), (" GZIP ", True)])
    async def test_missing_or_invalid_values_fall_back(self, mock_config, value, create_table):
        _set_compression(mock_config, value, create_table)
        expected = "gzip" if value == " GZIP " else "none"
        assert await DBDumper(_docker(), mock_config)._load_compression() == expected

    @pytest.mark.asyncio
    async def test_missing_database_file_is_not_created(self, mock_config):
        assert not mock_config.db_path.exists()
        assert await DBDumper(_docker(), mock_config)._load_compression() == "none"
        assert not mock_config.db_path.exists()

    @pytest.mark.asyncio
    async def test_mock_config_without_db_path_defaults(self, tmp_path):
        config = MagicMock()
        config.dump_dir = tmp_path
        assert await DBDumper(_docker(), config)._load_compression() == "none"


# ---------------------------------------------------------------------------
# Plain and gzip dumps
# ---------------------------------------------------------------------------

class TestDumpFormats:
    @pytest.mark.asyncio
    async def test_default_writes_plain_sql(self, mock_config):
        pg = _container("pg", ["POSTGRES_USER=alice", "POSTGRES_DB=app"], _pg_handler)
        dumper = DBDumper(_docker(pg), mock_config)

        results = await dumper.dump_all([_pg_db()])

        db_result = next(r for r in results if r.kind == "database")
        assert db_result.status == "success" and db_result.integrity_check == "ok"
        assert db_result.dump_path.endswith(".sql")
        assert Path(db_result.dump_path).read_bytes() == pg_dump_output("app")
        assert db_result.dump_size_bytes == len(pg_dump_output("app"))

    @pytest.mark.asyncio
    async def test_gzip_setting_writes_rsyncable_gzip(self, mock_config):
        _set_compression(mock_config, "gzip")
        pg = _container("pg", ["POSTGRES_USER=alice"], _pg_handler)
        dumper = DBDumper(_docker(pg), mock_config)

        with patch.object(dumper_module, "_RsyncableGzipDumpWriter",
                          wraps=dumper_module._RsyncableGzipDumpWriter) as rsyncable:
            results = await dumper.dump_all([_pg_db()])

        db_result = next(r for r in results if r.kind == "database")
        assert db_result.status == "success" and db_result.integrity_check == "ok"
        assert db_result.dump_path.endswith(".sql.gz")
        with gzip.open(db_result.dump_path) as f:
            assert f.read() == pg_dump_output("app")
        assert db_result.dump_size_bytes == os.path.getsize(db_result.dump_path)
        assert rsyncable.called, "gzip mode should pipe through gzip --rsyncable when available"

    @pytest.mark.asyncio
    async def test_gzip_falls_back_to_python_gzip_without_binary(self, mock_config):
        _set_compression(mock_config, "gzip")
        pg = _container("pg", ["POSTGRES_USER=alice"], _pg_handler)
        dumper = DBDumper(_docker(pg), mock_config)

        with patch.object(dumper_module, "_rsyncable_gzip_binary", return_value=None):
            results = await dumper.dump_all([_pg_db()])

        db_result = next(r for r in results if r.kind == "database")
        assert db_result.status == "success"
        with gzip.open(db_result.dump_path) as f:
            assert f.read() == pg_dump_output("app")

    def test_rsyncable_probe_rejects_gzip_without_flag(self, tmp_path):
        fake = tmp_path / "gzip"
        fake.write_text("#!/bin/sh\necho 'gzip: unrecognized option' >&2\nexit 1\n")
        fake.chmod(0o755)
        dumper_module._rsyncable_gzip_binary.cache_clear()
        try:
            with patch.object(dumper_module.shutil, "which", return_value=str(fake)):
                assert dumper_module._rsyncable_gzip_binary() is None
        finally:
            dumper_module._rsyncable_gzip_binary.cache_clear()

    def test_failing_gzip_process_fails_the_dump(self, tmp_path):
        fake = tmp_path / "fake-gzip"
        fake.write_text("#!/bin/sh\ncat > /dev/null\necho 'gzip: No space left on device' >&2\nexit 1\n")
        fake.chmod(0o755)
        dump_dir = tmp_path / "dumps"
        config = MagicMock()
        config.dump_dir = dump_dir
        dumper = DBDumper(_docker(_container("pg", [], _pg_handler)), config)
        dumper.compression = "gzip"

        with patch.object(dumper_module, "_rsyncable_gzip_binary", return_value=str(fake)):
            result = dumper._dump_postgres_blocking(_pg_db())

        assert result.status == "failed"
        assert "No space left" in result.error

    @pytest.mark.asyncio
    async def test_mariadb_follows_the_setting(self, mock_config):
        def handler(cmd):
            assert cmd[0] == "mariadb-dump"
            return 0, MYSQL_OK, b""

        db = DiscoveredDatabase(container_name="mdb", db_type="mariadb", db_name="appdb")
        maria = _container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], handler, tags=("mariadb:11",))

        plain = (await DBDumper(_docker(maria), mock_config).dump_all([db]))[0]
        assert plain.status == "success" and plain.dump_path.endswith(".sql")
        assert Path(plain.dump_path).read_bytes() == MYSQL_OK

        _set_compression(mock_config, "gzip")
        gz = (await DBDumper(_docker(maria), mock_config).dump_all([db]))[0]
        assert gz.status == "success" and gz.dump_path.endswith(".sql.gz")
        with gzip.open(gz.dump_path) as f:
            assert f.read() == MYSQL_OK

    @pytest.mark.parametrize("compression,ext", [("none", ".archive"), ("gzip", ".archive.gz")])
    def test_mongodb_archive_follows_the_setting(self, tmp_path, compression, ext):
        archive = b"\x6d\xe2\x99\x81" + b"m" * 500
        mongo = _container("mongo", [], lambda cmd: (0, archive, b""), tags=("mongo:7",))
        config = MagicMock()
        config.dump_dir = tmp_path
        dumper = DBDumper(_docker(mongo), config)
        dumper.compression = compression

        result = dumper._dump_mongodb_blocking(
            DiscoveredDatabase(container_name="mongo", db_type="mongodb", db_name="admin"))

        assert result.status == "success" and result.dump_path.endswith(ext)
        opener = gzip.open if compression == "gzip" else open
        with opener(result.dump_path, "rb") as f:
            assert f.read() == archive


class TestPlainTruncation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("compression", ["none", "gzip"])
    async def test_truncated_dump_fails_and_is_removed(self, mock_config, compression):
        if compression == "gzip":
            _set_compression(mock_config, "gzip")
        full = pg_dump_output("app")

        def handler(cmd):
            if cmd[0] == "pg_dump":
                return 0, full[: len(full) // 2], b""
            return 0, PG_GLOBALS, b""

        dumper = DBDumper(_docker(_container("pg", [], handler)), mock_config)
        results = await dumper.dump_all([_pg_db()])

        db_result = next(r for r in results if r.kind == "database")
        assert db_result.status == "failed"
        assert "incomplete" in db_result.error
        assert not os.path.exists(db_result.dump_path)
        app_prefix = dump_prefix("pg", "postgres", "app")
        assert not [n for n in _files(mock_config.dump_dir) if n.startswith(app_prefix) or n.endswith(".partial")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("compression", ["none", "gzip"])
    async def test_nonzero_exit_fails_and_is_removed(self, mock_config, compression):
        if compression == "gzip":
            _set_compression(mock_config, "gzip")

        def handler(cmd):
            if cmd[0] == "pg_dump":
                return 1, pg_dump_output("app"), b"pg_dump: error: connection lost"
            return 0, PG_GLOBALS, b""

        dumper = DBDumper(_docker(_container("pg", [], handler)), mock_config)
        db_result = next(r for r in await dumper.dump_all([_pg_db()]) if r.kind == "database")

        assert db_result.status == "failed"
        assert "exited with code 1" in db_result.error
        assert not os.path.exists(db_result.dump_path)


class TestStreaming:
    def test_plain_dump_is_written_while_streaming(self, tmp_path):
        """Chunks reach the file as they arrive; nothing is buffered whole."""
        chunk = os.urandom(256 * 1024)
        sizes = []
        config = MagicMock()
        config.dump_dir = tmp_path
        dumper = DBDumper(MagicMock(), config)
        dump_path = str(tmp_path / "pg_app_20261022_220000.sql")

        def output():
            for i in range(20):
                if i:
                    sizes.append(os.path.getsize(dump_path))
                yield (chunk, None)
            yield (b"\n-- PostgreSQL database dump complete\n", None)

        container = MagicMock()
        container.client.api = None
        container.exec_run.return_value = (0, output())
        stream = dumper._exec_stream_to_file(container, ["pg_dump"], dump_path)

        assert stream.bytes_written == 20 * len(chunk) + 38
        assert sizes == [i * len(chunk) for i in range(1, 20)]
        assert stream.tail.endswith(b"dump complete\n")

    @pytest.mark.parametrize("compression", ["none", "gzip"])
    def test_memory_stays_bounded(self, tmp_path, compression):
        chunk_size, chunks = 1024 * 1024, 48
        config = MagicMock()
        config.dump_dir = tmp_path
        dumper = DBDumper(MagicMock(), config)
        dumper.compression = compression
        dump_path = str(tmp_path / f"pg_app_20261022_220000.sql{'.gz' if compression == 'gzip' else ''}")

        def output():
            for _ in range(chunks):
                yield (os.urandom(chunk_size), None)

        container = MagicMock()
        container.client.api = None
        container.exec_run.return_value = (0, output())
        tracemalloc.start()
        try:
            stream = dumper._exec_stream_to_file(container, ["pg_dump"], dump_path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert stream.bytes_written == chunks * chunk_size
        assert peak < 8 * chunk_size, f"peak {peak} bytes suggests the dump was buffered"


# ---------------------------------------------------------------------------
# Cleanup across extensions
# ---------------------------------------------------------------------------

def _touch(path: Path, age_days: float = 0, data: bytes = b"x"):
    path.write_bytes(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


class TestCleanupAcrossExtensions:
    def test_plain_newer_than_gzip(self, tmp_path):
        _touch(tmp_path / "x_20261021_100000.sql.gz")
        _touch(tmp_path / "x_20261022_220000.sql")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert _files(tmp_path) == ["x_20261022_220000.sql"]

    def test_gzip_newer_than_plain(self, tmp_path):
        _touch(tmp_path / "x_20261021_100000.sql")
        _touch(tmp_path / "x_20261022_220000.sql.gz")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert _files(tmp_path) == ["x_20261022_220000.sql.gz"]

    def test_globals_and_databases_are_separate_groups(self, tmp_path):
        for name in ("pg_globals_20261021_100000.sql.gz", "pg_globals_20261022_220000.sql",
                     "pg_app_20261022_220000.sql", "pg_other_20261022_220000.sql"):
            _touch(tmp_path / name)
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert _files(tmp_path) == ["pg_app_20261022_220000.sql", "pg_globals_20261022_220000.sql",
                                    "pg_other_20261022_220000.sql"]

    def test_manifest_is_never_deleted(self, tmp_path):
        _touch(tmp_path / DUMP_MANIFEST_NAME, age_days=30, data=b'{"version": 1, "dumps": []}')
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7)
        assert (tmp_path / DUMP_MANIFEST_NAME).exists()

    def test_stale_manifest_temp_files_are_removed(self, tmp_path):
        _touch(tmp_path / f".{DUMP_MANIFEST_NAME}.old.tmp", age_days=1)
        _touch(tmp_path / f".{DUMP_MANIFEST_NAME}.fresh.tmp")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7)
        assert _files(tmp_path) == [f".{DUMP_MANIFEST_NAME}.fresh.tmp"]


# ---------------------------------------------------------------------------
# verify_engine: plain .sql
# ---------------------------------------------------------------------------

class TestVerifyPlainSql:
    @pytest.mark.parametrize("name,data,dump_type", [
        ("pg_app_20261022_220000.sql", pg_dump_output("app"), "postgres"),
        ("pg_globals_20261022_220000.sql", PG_GLOBALS, "postgres"),
        ("mdb_appdb_20261022_220000.sql", MYSQL_OK, "mariadb"),
    ])
    def test_complete_dump_passes(self, tmp_path, name, data, dump_type):
        path = tmp_path / name
        path.write_bytes(data)
        result = VerifyEngine._check_plain_sql_dump(str(path))
        assert result["status"] == "success" and result["type"] == dump_type

    def test_truncated_arkive_dump_fails(self, tmp_path):
        path = tmp_path / "pg_app_20261022_220000.sql"
        data = pg_dump_output("app")
        path.write_bytes(data[: len(data) - 80])
        result = VerifyEngine._check_plain_sql_dump(str(path))
        assert result["status"] == "failed" and "truncated" in result["error"]

    def test_empty_arkive_dump_fails(self, tmp_path):
        path = tmp_path / "pg_app_20261022_220000.sql"
        path.write_bytes(b"")
        assert VerifyEngine._check_plain_sql_dump(str(path))["status"] == "failed"

    def test_foreign_sql_file_is_left_to_structural_check(self, tmp_path):
        path = tmp_path / "notes.sql"
        path.write_text("-- hand-written\nCREATE TABLE t (id int);\n")
        assert VerifyEngine._check_plain_sql_dump(str(path)) is None

    def test_reads_only_the_tail_of_large_dumps(self, tmp_path):
        path = tmp_path / "pg_big_20261022_220000.sql"
        with open(path, "wb") as f:
            f.truncate(1024 ** 3)  # sparse 1 GiB
            f.seek(0, os.SEEK_END)
            f.write(b"\n--\n-- PostgreSQL database dump complete\n--\n\n")
        tracemalloc.start()
        try:
            result = VerifyEngine._check_plain_sql_dump(str(path))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result["status"] == "success"
        assert peak < 1024 * 1024

    def test_gzip_globals_dump_passes(self, tmp_path):
        path = tmp_path / "pg_globals_20261022_220000.sql.gz"
        with gzip.open(path, "wb") as f:
            f.write(PG_GLOBALS)
        assert VerifyEngine._check_gzip_sql_dump(str(path))["status"] == "success"

    @pytest.mark.asyncio
    async def test_validate_db_dumps_mixed_formats(self, mock_config, mock_backup_engine):
        d = mock_config.dump_dir
        (d / "pg_app_20261022_220000.sql").write_bytes(pg_dump_output("app"))
        (d / "pg_cut_20261022_220000.sql").write_bytes(pg_dump_output("cut")[:300])
        with gzip.open(d / "mdb_appdb_20261021_100000.sql.gz", "wb") as f:
            f.write(MYSQL_OK)
        (d / DUMP_MANIFEST_NAME).write_text('{"version": 1, "dumps": []}')

        engine = VerifyEngine(config=mock_config, backup_engine=mock_backup_engine)
        result = await engine._validate_db_dumps()

        by_file = {f["file"]: f["status"] for f in result["files"]}
        assert by_file == {
            "pg_app_20261022_220000.sql": "success",
            "pg_cut_20261022_220000.sql": "failed",
            "mdb_appdb_20261021_100000.sql.gz": "success",
        }
        assert result["total"] == 3 and result["passed"] == 2


@pytest.fixture
def mock_backup_engine():
    return MagicMock()


# ---------------------------------------------------------------------------
# Postgres database enumeration (discovery)
# ---------------------------------------------------------------------------

def _pg_cluster(name, env, databases, postgres_has_tables=False, list_exit=0, status="running",
                tags=("postgres:16",)):
    """Postgres container whose psql answers the enumeration queries."""
    def handler(cmd):
        assert cmd[0] == "psql", cmd
        sql = cmd[-1]
        if "FROM pg_database" in sql:
            if list_exit:
                return list_exit, b"", b'psql: error: FATAL:  database "x" does not exist'
            return 0, "".join(f"{d}\n" for d in databases).encode(), b""
        if "pg_class" in sql:
            assert cmd[cmd.index("-d") + 1] == "postgres"
            return 0, b"t\n" if postgres_has_tables else b"f\n", b""
        raise AssertionError(sql)

    return _container(name, env, handler, tags=tags, status=status)


def _engine(mock_config, profiles=None):
    from app.services.discovery import DiscoveryEngine

    engine = DiscoveryEngine.__new__(DiscoveryEngine)
    engine.config = mock_config
    engine.profiles = profiles or []
    engine._pg_database_cache = {}
    return engine


class TestPostgresEnumeration:
    def test_all_cluster_databases_are_discovered(self, mock_config):
        pg = _pg_cluster("pg", ["POSTGRES_USER=alice", "POSTGRES_DB=app"], ["app", "metrics", "postgres", "wiki"])
        found = _engine(mock_config)._detect_postgres(pg, {"POSTGRES_USER": "alice", "POSTGRES_DB": "app"}, [])

        assert [d.db_name for d in found] == ["app", "metrics", "wiki"]
        assert {d.container_name for d in found} == {"pg"}
        list_call = pg.calls[0]
        assert list_call["cmd"][:2] == ["psql", "-X"]
        assert list_call["cmd"][list_call["cmd"].index("-U") + 1] == "alice"
        assert list_call["cmd"][list_call["cmd"].index("-d") + 1] == "app"
        assert list_call["environment"]["PGCONNECT_TIMEOUT"]

    def test_maintenance_db_with_user_tables_is_kept(self, mock_config):
        pg = _pg_cluster("pg", [], ["app", "postgres"], postgres_has_tables=True)
        found = _engine(mock_config)._detect_postgres(pg, {"POSTGRES_DB": "app"}, [])
        assert [d.db_name for d in found] == ["app", "postgres"]

    def test_configured_maintenance_db_is_kept_without_table_check(self, mock_config):
        pg = _pg_cluster("pg", [], ["alice", "postgres"])
        found = _engine(mock_config)._detect_postgres(pg, {"POSTGRES_USER": "alice"}, [])
        assert [d.db_name for d in found] == ["postgres", "alice"]
        assert not any("pg_class" in c["cmd"][-1] for c in pg.calls)

    def test_enumeration_failure_falls_back_to_env_database(self, mock_config):
        pg = _pg_cluster("pg", [], [], list_exit=2)
        found = _engine(mock_config)._detect_postgres(pg, {"POSTGRES_DB": "app"}, [])

        assert [d.db_name for d in found] == ["app"]
        tried = [c["cmd"][c["cmd"].index("-d") + 1] for c in pg.calls]
        assert tried == ["app", "postgres"]

    def test_exec_errors_fall_back(self, mock_config):
        pg = _pg_cluster("pg", [], ["app"])
        pg.exec_run.side_effect = Exception("container is restarting")
        found = _engine(mock_config)._detect_postgres(pg, {"POSTGRES_DB": "app"}, [])
        assert [d.db_name for d in found] == ["app"]

    def test_stopped_container_is_not_queried(self, mock_config):
        pg = _pg_cluster("pg", [], ["app", "other"], status="exited")
        found = _engine(mock_config)._detect_postgres(pg, {"POSTGRES_DB": "app"}, [])
        assert [d.db_name for d in found] == ["app"]
        assert pg.calls == []

    @pytest.mark.asyncio
    async def test_companion_resolution_and_dedupe_with_enumeration(self, mock_config):
        """Immich-style app -> companion DB container: one set of DBs, keyed by the DB container."""
        immich_profile = {
            "name": "immich",
            "image_patterns": ["ghcr.io/immich-app/immich-server"],
            "databases": [{"type": "postgres", "env_vars": {
                "db_name": ["DB_DATABASE_NAME", "POSTGRES_DB"],
                "db_host": ["DB_HOSTNAME", "DB_HOST"],
            }}],
        }
        labels = {"com.docker.compose.project": "immich"}
        app = _container("immich_server", ["DB_HOSTNAME=database", "DB_DATABASE_NAME=immich"],
                         lambda cmd: (_ for _ in ()).throw(AssertionError("no exec in app container")),
                         tags=("ghcr.io/immich-app/immich-server:release",))
        app.labels = {**labels, "com.docker.compose.service": "immich-server"}
        db = _pg_cluster("immich_postgres", ["POSTGRES_USER=postgres", "POSTGRES_DB=immich"],
                         ["immich", "immich_audit", "postgres"],
                         tags=("ghcr.io/immich-app/postgres:14-vectorchord0.4.3",))
        db.labels = {**labels, "com.docker.compose.service": "database"}
        for c in (app, db):
            c.attrs["NetworkSettings"] = {"Networks": {}}
            c.id = c.name

        from app.services.discovery import DiscoveryEngine
        engine = DiscoveryEngine.__new__(DiscoveryEngine)
        engine.config = mock_config
        engine.docker = _docker(app, db)
        engine.profiles = [immich_profile]

        containers = engine._scan_blocking()

        by_name = {c.name: c for c in containers}
        assert [(d.container_name, d.db_name) for d in by_name["immich_server"].databases] == [
            ("immich_postgres", "immich"), ("immich_postgres", "immich_audit")]
        assert [(d.container_name, d.db_name) for d in by_name["immich_postgres"].databases] == [
            ("immich_postgres", "immich"), ("immich_postgres", "immich_audit")]
        all_dbs = [d for c in containers for d in c.databases]
        assert [(d.container_name, d.db_name) for d in DBDumper.dedupe(all_dbs)] == [
            ("immich_postgres", "immich"), ("immich_postgres", "immich_audit")]
        # Enumerated once per scan even though two containers resolve to it.
        assert sum(1 for c in db.calls if "FROM pg_database" in c["cmd"][-1]) == 1


# ---------------------------------------------------------------------------
# Roles / globals
# ---------------------------------------------------------------------------

class TestGlobals:
    @pytest.mark.asyncio
    async def test_globals_dumped_once_per_postgres_container(self, mock_config):
        pg = _container("pg", ["POSTGRES_USER=alice"], _pg_handler)
        pg2 = _container("pg2", [], _pg_handler)
        dumper = DBDumper(_docker(pg, pg2), mock_config)

        results = await dumper.dump_all([_pg_db("pg", "app"), _pg_db("pg", "wiki"), _pg_db("pg2", "x")])

        globals_results = [r for r in results if r.kind == "globals"]
        assert [(r.container_name, r.db_name, r.db_type, r.status) for r in globals_results] == [
            ("pg", "globals", "postgres", "success"), ("pg2", "globals", "postgres", "success")]
        assert globals_results[0].integrity_check == "ok"
        assert os.path.basename(globals_results[0].dump_path).startswith(
            dump_prefix("pg", "postgres", "globals", kind="globals") + "_")
        assert globals_results[0].dump_path.endswith(".sql")
        assert Path(globals_results[0].dump_path).read_bytes() == PG_GLOBALS
        dumpall = [c["cmd"] for c in pg.calls if c["cmd"][0] == "pg_dumpall"]
        assert dumpall == [["pg_dumpall", "--globals-only", "-U", "alice", "-l", "app"]]

    @pytest.mark.asyncio
    async def test_globals_connect_to_a_database_that_dumped(self, mock_config):
        def handler(cmd):
            if cmd[0] == "pg_dump" and cmd[-1] == "gone":
                return 1, b"", b'pg_dump: error: database "gone" does not exist'
            return _pg_handler(cmd)

        pg = _container("pg", [], handler)
        results = await DBDumper(_docker(pg), mock_config).dump_all([_pg_db("pg", "gone"), _pg_db("pg", "app")])

        dumpall = [c["cmd"] for c in pg.calls if c["cmd"][0] == "pg_dumpall"]
        assert dumpall[0][-1] == "app"
        assert next(r for r in results if r.kind == "globals").status == "success"

    @pytest.mark.asyncio
    async def test_permission_denied_retries_without_role_passwords(self, mock_config):
        def handler(cmd):
            if cmd[0] == "pg_dumpall" and "--no-role-passwords" not in cmd:
                return 1, b"", b"pg_dumpall: error: query failed: ERROR:  permission denied for table pg_authid"
            return _pg_handler(cmd)

        pg = _container("pg", ["POSTGRES_USER=bob"], handler)
        results = await DBDumper(_docker(pg), mock_config).dump_all([_pg_db()])

        result = next(r for r in results if r.kind == "globals")
        assert result.status == "success"
        dumpall = [c["cmd"] for c in pg.calls if c["cmd"][0] == "pg_dumpall"]
        assert dumpall[1] == ["pg_dumpall", "--globals-only", "--no-role-passwords", "-U", "bob", "-l", "app"]

    @pytest.mark.asyncio
    async def test_globals_failure_is_reported_and_file_removed(self, mock_config):
        def handler(cmd):
            if cmd[0] == "pg_dumpall":
                return 1, b"", b"pg_dumpall: error: permission denied for table pg_authid"
            return _pg_handler(cmd)

        pg = _container("pg", [], handler)
        results = await DBDumper(_docker(pg), mock_config).dump_all([_pg_db()])

        result = next(r for r in results if r.kind == "globals")
        assert (result.status, result.db_name, result.db_type) == ("failed", "globals", "postgres")
        assert "permission denied" in result.error
        assert not [n for n in _files(mock_config.dump_dir) if "globals" in n or n.endswith(".partial")]

    @pytest.mark.asyncio
    async def test_truncated_globals_fail(self, mock_config):
        def handler(cmd):
            if cmd[0] == "pg_dumpall":
                return 0, PG_GLOBALS[:120], b""
            return _pg_handler(cmd)

        results = await DBDumper(_docker(_container("pg", [], handler)), mock_config).dump_all([_pg_db()])
        assert next(r for r in results if r.kind == "globals").status == "failed"

    @pytest.mark.asyncio
    async def test_no_globals_without_postgres(self, mock_config):
        maria = _container("mdb", [], lambda cmd: (0, MYSQL_OK, b""), tags=("mariadb:11",))
        results = await DBDumper(_docker(maria), mock_config).dump_all(
            [DiscoveredDatabase(container_name="mdb", db_type="mariadb", db_name="appdb")])
        assert [r.kind for r in results] == ["database"]

    def test_database_named_globals_does_not_collide(self, tmp_path):
        config = MagicMock()
        config.dump_dir = tmp_path
        dumper = DBDumper(MagicMock(), config)
        db_path = dumper._dump_path(_pg_db("pg", "globals"), "20261022_220000", ".sql")
        roles_path = dumper._dump_path(_pg_db("pg", "globals"), "20261022_220000", ".sql", kind="globals")
        assert db_path != roles_path
        assert os.path.basename(db_path).startswith(dump_prefix("pg", "postgres", "globals") + "_")
        assert os.path.basename(roles_path).startswith(dump_prefix("pg", "postgres", "globals", kind="globals") + "_")


class TestPgDbnameValidation:
    @pytest.mark.parametrize("name", ["app", "My App", "immich_audit", "Ünïcode"])
    def test_accepts_enumerated_names(self, name):
        assert DBDumper._sanitize_pg_dbname(name) == name

    @pytest.mark.parametrize("name", ["", "host=evil dbname=x", "postgresql://evil/x", "-x", "a\nb", "x" * 64])
    def test_rejects_connection_strings_and_options(self, name):
        with pytest.raises(ValueError):
            DBDumper._sanitize_pg_dbname(name)


# ---------------------------------------------------------------------------
# Dump manifest
# ---------------------------------------------------------------------------

class TestManifest:
    @pytest.mark.asyncio
    async def test_manifest_written_after_dump_all(self, mock_config):
        pg = _container("pg", ["POSTGRES_USER=alice"], _pg_handler, tags=("postgres:16.4",))
        results = await DBDumper(_docker(pg), mock_config).dump_all([_pg_db("pg", "app")])

        manifest = json.loads((mock_config.dump_dir / DUMP_MANIFEST_NAME).read_text())
        assert manifest["version"] == 1
        assert manifest["arkive_version"] == __version__
        assert manifest["generated_at"].endswith("Z")
        by_kind = {e["kind"]: e for e in manifest["dumps"]}
        db_entry, globals_entry = by_kind["database"], by_kind["globals"]
        db_result = next(r for r in results if r.kind == "database")
        assert db_entry == {
            "file": os.path.basename(db_result.dump_path),
            "container_name": "pg",
            "image": "postgres:16.4",
            "db_type": "postgres",
            "db_name": "app",
            "host_path": None,
            "kind": "database",
            "compression": "none",
            "size_bytes": os.path.getsize(db_result.dump_path),
            "created_at": db_entry["created_at"],
            "integrity": "ok",
        }
        assert db_entry["created_at"].endswith("Z") and len(db_entry["created_at"]) == 20
        assert globals_entry["db_name"] == "globals" and globals_entry["db_type"] == "postgres"
        assert read_dump_manifest(mock_config.dump_dir) == manifest

    @pytest.mark.asyncio
    async def test_gzip_entries_and_failed_dumps(self, mock_config):
        _set_compression(mock_config, "gzip")

        def handler(cmd):
            if cmd[0] == "pg_dump" and cmd[-1] == "broken":
                return 0, pg_dump_output("broken")[:100], b""
            return _pg_handler(cmd)

        await DBDumper(_docker(_container("pg", [], handler)), mock_config).dump_all(
            [_pg_db("pg", "app"), _pg_db("pg", "broken")])

        entries = read_dump_manifest(mock_config.dump_dir)["dumps"]
        assert {e["db_name"] for e in entries} == {"app", "globals"}
        assert {e["compression"] for e in entries} == {"gzip"}
        assert all(e["file"].endswith(".sql.gz") for e in entries)

    @pytest.mark.asyncio
    async def test_sqlite_entry_carries_host_path_and_unverifiable(self, mock_config, tmp_path):
        src = tmp_path / "src" / "library.db"
        src.parent.mkdir()
        src.write_bytes(b"x")

        async def fake_run(cmd, **kwargs):
            if ".backup" in cmd[-1]:
                Path(cmd[-1].split(" ", 1)[1].strip("'")).write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
                return CommandResult(0, "", "", 0.1, "")
            if cmd[-1].startswith("PRAGMA integrity_check"):
                return CommandResult(1, "", "Error: no such collation sequence: icu_root", 0.1, "")
            return CommandResult(0, "delete", "", 0.1, "")

        plex = _container("plex", [], lambda cmd: (0, b"", b""), tags=("plexinc/pms-docker:latest",))
        db = DiscoveredDatabase(container_name="plex", db_type="sqlite", db_name="library.db", host_path=str(src))
        with patch("app.services.db_dumper.run_command", side_effect=fake_run):
            await DBDumper(_docker(plex), mock_config).dump_all([db])

        (entry,) = read_dump_manifest(mock_config.dump_dir)["dumps"]
        assert entry["db_type"] == "sqlite" and entry["host_path"] == str(src)
        assert entry["integrity"] == "unverifiable" and entry["image"] == "plexinc/pms-docker:latest"

    def test_merge_keeps_existing_files_and_drops_missing(self, tmp_path):
        old_entry = {"file": "pg_wiki_20261021_100000.sql", "container_name": "pg", "image": "postgres:16",
                     "db_type": "postgres", "db_name": "wiki", "host_path": None, "kind": "database",
                     "compression": "none", "size_bytes": 1, "created_at": "2026-10-21T10:00:00Z",
                     "integrity": "ok"}
        gone_entry = {**old_entry, "file": "pg_gone_20261020_100000.sql", "db_name": "gone"}
        (tmp_path / DUMP_MANIFEST_NAME).write_text(json.dumps({"version": 1, "dumps": [old_entry, gone_entry]}))
        (tmp_path / "pg_wiki_20261021_100000.sql").write_bytes(pg_dump_output("wiki"))
        (tmp_path / "pg_app_20261022_220000.sql").write_bytes(pg_dump_output("app"))
        new_entry = {**old_entry, "file": "pg_app_20261022_220000.sql", "db_name": "app",
                     "created_at": "2026-10-22T22:00:00Z"}

        manifest = write_dump_manifest(tmp_path, [new_entry])

        by_file = {e["file"]: e for e in manifest["dumps"]}
        assert set(by_file) == {"pg_wiki_20261021_100000.sql", "pg_app_20261022_220000.sql"}
        assert by_file["pg_wiki_20261021_100000.sql"]["db_name"] == "wiki"
        assert by_file["pg_wiki_20261021_100000.sql"]["size_bytes"] == len(pg_dump_output("wiki"))
        assert by_file["pg_app_20261022_220000.sql"]["db_name"] == "app"

    def test_unknown_files_get_inferred_entries(self, tmp_path):
        with gzip.open(tmp_path / "immich_postgres_immich_20261020_100000.sql.gz", "wb") as f:
            f.write(pg_dump_output("immich"))
        (tmp_path / "cache_redis_20261020_100000.rdb").write_bytes(b"REDIS0011")
        (tmp_path / "pg_globals_20261020_100000.sql").write_bytes(PG_GLOBALS)
        (tmp_path / "flash_20261020_100000.tar").write_bytes(b"x")
        (tmp_path / "arkive_self.db").write_bytes(b"x")
        (tmp_path / "plex_lib.db_20261020_100000.sqlite3-wal").write_bytes(b"x")

        manifest = write_dump_manifest(tmp_path, known_containers={"immich_postgres"})

        by_file = {e["file"]: e for e in manifest["dumps"]}
        assert set(by_file) == {"immich_postgres_immich_20261020_100000.sql.gz",
                                "cache_redis_20261020_100000.rdb", "pg_globals_20261020_100000.sql"}
        immich = by_file["immich_postgres_immich_20261020_100000.sql.gz"]
        assert (immich["container_name"], immich["db_name"], immich["db_type"], immich["compression"]) == (
            "immich_postgres", "immich", "postgres", "gzip")
        assert immich["created_at"] == "2026-10-20T10:00:00Z"
        redis = by_file["cache_redis_20261020_100000.rdb"]
        assert (redis["container_name"], redis["db_type"], redis["db_name"]) == ("cache", "redis", "redis")
        glob = by_file["pg_globals_20261020_100000.sql"]
        assert (glob["container_name"], glob["kind"], glob["db_name"]) == ("pg", "globals", "globals")

    def test_atomic_write_keeps_previous_manifest_on_failure(self, tmp_path):
        original = json.dumps({"version": 1, "dumps": []})
        (tmp_path / DUMP_MANIFEST_NAME).write_text(original)
        (tmp_path / "pg_app_20261022_220000.sql").write_bytes(pg_dump_output("app"))

        with patch.object(dumper_module.os, "replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                write_dump_manifest(tmp_path)

        assert (tmp_path / DUMP_MANIFEST_NAME).read_text() == original
        assert _files(tmp_path) == ["arkive-dumps-manifest.json", "pg_app_20261022_220000.sql"]

    def test_atomic_write_uses_temp_file_in_same_directory(self, tmp_path):
        real_replace = os.replace
        seen = []

        def spy(src, dst):
            seen.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        with patch.object(dumper_module.os, "replace", side_effect=spy):
            write_dump_manifest(tmp_path)

        ((src, dst),) = seen
        assert src.parent == dst.parent == tmp_path
        assert src.name.startswith(f".{DUMP_MANIFEST_NAME}.") and dst.name == DUMP_MANIFEST_NAME

    def test_read_missing_or_corrupt_manifest(self, tmp_path):
        assert read_dump_manifest(tmp_path)["dumps"] == []
        (tmp_path / DUMP_MANIFEST_NAME).write_text("{not json")
        assert read_dump_manifest(tmp_path)["dumps"] == []
        (tmp_path / DUMP_MANIFEST_NAME).write_text(json.dumps({"version": 1, "dumps": [{"nofile": 1}, "x"]}))
        assert read_dump_manifest(tmp_path)["dumps"] == []

    @pytest.mark.asyncio
    async def test_cleanup_prunes_manifest_and_keeps_it(self, mock_config):
        d = mock_config.dump_dir
        pg = _container("pg", [], _pg_handler)
        dumper = DBDumper(_docker(pg), mock_config)
        with patch.object(dumper, "_timestamp", return_value="20261021_100000"):
            await dumper.dump_all([_pg_db("pg", "app")])
        with patch.object(dumper, "_timestamp", return_value="20261022_220000"):
            await dumper.dump_all([_pg_db("pg", "app")])
        assert len(read_dump_manifest(d)["dumps"]) == 4
        os.utime(d / DUMP_MANIFEST_NAME, (time.time() - 30 * 86400,) * 2)

        removed = cleanup_dump_dir(d, keep_last=1, max_age_days=7)

        assert removed == 2
        assert (d / DUMP_MANIFEST_NAME).exists()
        files = sorted(e["file"] for e in read_dump_manifest(d)["dumps"])
        assert files == sorted([f"{dump_prefix('pg', 'postgres', 'app')}_20261022_220000.sql",
                                f"{dump_prefix('pg', 'postgres', 'globals', kind='globals')}_20261022_220000.sql"])
        assert files == [n for n in _files(d) if n != DUMP_MANIFEST_NAME]

    def test_cleanup_without_manifest_does_not_create_one(self, tmp_path):
        _touch(tmp_path / "x_20261021_100000.sql")
        _touch(tmp_path / "x_20261022_220000.sql")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert not (tmp_path / DUMP_MANIFEST_NAME).exists()

    @pytest.mark.asyncio
    async def test_dump_single_updates_manifest(self, mock_config):
        pg = _container("pg", [], _pg_handler)
        result = await DBDumper(_docker(pg), mock_config).dump_single("pg", "app", "postgres")

        (entry,) = read_dump_manifest(mock_config.dump_dir)["dumps"]
        assert entry["file"] == os.path.basename(result.dump_path) and entry["kind"] == "database"

    @pytest.mark.asyncio
    async def test_manifest_failure_does_not_fail_dumps(self, mock_config):
        pg = _container("pg", [], _pg_handler)
        with patch.object(dumper_module, "write_dump_manifest", side_effect=OSError("read-only")):
            results = await DBDumper(_docker(pg), mock_config).dump_all([_pg_db()])
        assert all(r.status == "success" for r in results)


# ---------------------------------------------------------------------------
# Redis AUTH
# ---------------------------------------------------------------------------

class TestRedisAuth:
    def _redis(self, tmp_path, env=None, cmd=None):
        data = tmp_path / "redis-data"
        data.mkdir()
        (data / "dump.rdb").write_bytes(b"REDIS0011" + b"\x00" * 50)
        calls = []
        lastsave = iter([b"1709000000\n", b"1709000001\n"])

        def exec_run(command, **kwargs):
            calls.append((command, kwargs))
            joined = " ".join(command)
            if "LASTSAVE" in joined:
                return 0, next(lastsave, b"1709000001\n")
            if "CONFIG GET dir" in joined:
                return 0, b"dir\n/data\n"
            if "CONFIG GET dbfilename" in joined:
                return 0, b"dbfilename\ndump.rdb\n"
            return 0, b"Background saving started\n"

        container = MagicMock()
        container.exec_run.side_effect = exec_run
        container.attrs = {"Config": {"Env": env or [], "Cmd": cmd}, "Mounts": [
            {"Source": str(data), "Destination": "/data"}]}
        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        dumper = DBDumper(_docker(), config)
        dumper.docker.containers.get.side_effect = None
        dumper.docker.containers.get.return_value = container
        with patch("time.sleep"):
            result = dumper._dump_redis_blocking(
                DiscoveredDatabase(container_name="cache", db_type="redis", db_name="redis"))
        return result, calls

    def test_redis_password_env_is_passed_via_rediscli_auth(self, tmp_path):
        result, calls = self._redis(tmp_path, env=["REDIS_PASSWORD=s3cret"])
        assert result.status == "success"
        assert all(kwargs.get("environment") == {"REDISCLI_AUTH": "s3cret"} for _, kwargs in calls)
        assert not any("s3cret" in " ".join(cmd) for cmd, _ in calls)

    def test_requirepass_in_command(self, tmp_path):
        result, calls = self._redis(tmp_path, cmd=["redis-server", "--requirepass", "hunter2", "--appendonly", "yes"])
        assert result.status == "success"
        assert calls[0][1]["environment"] == {"REDISCLI_AUTH": "hunter2"}

    def test_requirepass_in_shell_command(self, tmp_path):
        _, calls = self._redis(tmp_path, cmd=["sh", "-c", "redis-server --requirepass 'pa ss'"])
        assert calls[0][1]["environment"] == {"REDISCLI_AUTH": "pa ss"}

    def test_no_password_no_environment(self, tmp_path):
        _, calls = self._redis(tmp_path, cmd=["redis-server"])
        assert all("environment" not in kwargs for _, kwargs in calls)


# ---------------------------------------------------------------------------
# Review fixes: identity-hashed names, .partial writes, SQLite integrity,
# protected cleanup, discovery coverage warnings
# ---------------------------------------------------------------------------

class TestDumpPrefix:
    @pytest.mark.parametrize("a,b", [
        (("pg", "postgres", "sales data"), ("pg", "postgres", "sales_data")),
        (("a", "postgres", "b_c"), ("a_b", "postgres", "c")),
        (("app", "sqlite", "creds.db", "/a/creds.db"), ("app", "sqlite", "creds.db", "/b/creds.db")),
        (("pg", "postgres", "app"), ("pg", "mariadb", "app")),
    ])
    def test_lossy_names_get_distinct_prefixes(self, a, b):
        assert dump_prefix(*a) != dump_prefix(*b)

    def test_roles_dump_never_collides_with_database_named_globals(self):
        assert dump_prefix("pg", "postgres", "globals") != dump_prefix("pg", "postgres", "globals", kind="globals")

    def test_stable_and_readable(self):
        prefix = dump_prefix("immich_postgres", "postgres", "immich")
        assert prefix == dump_prefix("immich_postgres", "postgres", "immich", None)
        assert re.fullmatch(r"immich_postgres_immich-[0-9a-f]{8}", prefix)
        assert dump_prefix("db", "mysql", "app") == dump_prefix("db", "mariadb", "app")

    @pytest.mark.asyncio
    async def test_dump_files_group_under_dump_prefix(self, mock_config):
        """Cleanup's grouping prefix of every written file is exactly dump_prefix()."""
        pg = _container("pg", [], _pg_handler)
        await DBDumper(_docker(pg), mock_config).dump_all([_pg_db("pg", "sales data"), _pg_db("pg", "sales_data")])

        prefixes = {dumper_module._DUMP_TS_PATTERN.match(n).group(1)
                    for n in _files(mock_config.dump_dir) if n != DUMP_MANIFEST_NAME}
        assert prefixes == {dump_prefix("pg", "postgres", "sales data"), dump_prefix("pg", "postgres", "sales_data"),
                            dump_prefix("pg", "postgres", "globals", kind="globals")}
        # Both colliding-name databases survive cleanup.
        cleanup_dump_dir(mock_config.dump_dir, keep_last=1, max_age_days=None)
        assert len([n for n in _files(mock_config.dump_dir) if "sales" in n]) == 2


class TestPartialFiles:
    def test_dump_is_streamed_to_partial_and_renamed_when_valid(self, tmp_path):
        seen_during_stream = []
        config = MagicMock()
        config.dump_dir = tmp_path
        dumper = DBDumper(MagicMock(), config)

        def output():
            seen_during_stream.append(_files(tmp_path))
            yield (pg_dump_output("app"), None)
            seen_during_stream.append(_files(tmp_path))

        container = MagicMock()
        container.client.api = None
        container.attrs = {"Config": {"Env": []}}
        container.exec_run.return_value = (0, output())
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_postgres_blocking(_pg_db())

        assert result.status == "success"
        assert all(len(names) == 1 and names[0].endswith(".partial") for names in seen_during_stream)
        assert _files(tmp_path) == [os.path.basename(result.dump_path)]

    @pytest.mark.parametrize("method,db,tags", [
        ("_dump_postgres_blocking", _pg_db(), ("postgres:16",)),
        ("_dump_mariadb_blocking", DiscoveredDatabase(container_name="pg", db_type="mariadb", db_name="app"),
         ("mariadb:11",)),
    ])
    def test_failed_stream_leaves_no_files(self, tmp_path, method, db, tags):
        config = MagicMock()
        config.dump_dir = tmp_path
        cut = _container("pg", [], lambda cmd: (0, b"-- partial output without trailer\n" * 20, b""), tags=tags)
        result = getattr(DBDumper(_docker(cut), config), method)(db)
        assert result.status == "failed"
        assert _files(tmp_path) == []

    def test_exception_mid_stream_leaves_no_files(self, tmp_path):
        config = MagicMock()
        config.dump_dir = tmp_path

        def output():
            yield (b"-- PostgreSQL database dump\n" * 100, None)
            raise ConnectionError("docker stream reset")

        container = MagicMock()
        container.client.api = None
        container.attrs = {"Config": {"Env": []}}
        container.exec_run.return_value = (0, output())
        dumper = DBDumper(MagicMock(), config)
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_postgres_blocking(_pg_db())

        assert result.status == "failed" and "stream reset" in result.error
        assert _files(tmp_path) == []

    def test_cleanup_ignores_fresh_partials_and_removes_stale_ones(self, tmp_path):
        prefix = dump_prefix("pg", "postgres", "app")
        _touch(tmp_path / f"{prefix}_20261021_100000.sql")
        _touch(tmp_path / f"{prefix}_20261022_220000.sql.1a2b3c4d.partial")  # in progress, "newer"
        _touch(tmp_path / f"{prefix}_20261020_100000.sql.deadbeef.partial", age_days=0.5)  # crashed 12h ago
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert _files(tmp_path) == [f"{prefix}_20261021_100000.sql", f"{prefix}_20261022_220000.sql.1a2b3c4d.partial"]

    def test_manifest_lists_only_final_files(self, tmp_path):
        prefix = dump_prefix("pg", "postgres", "app")
        (tmp_path / f"{prefix}_20261021_100000.sql").write_bytes(pg_dump_output("app"))
        (tmp_path / f"{prefix}_20261022_220000.sql.1a2b3c4d.partial").write_bytes(b"-- half")
        manifest = write_dump_manifest(tmp_path, known_containers={"pg"})
        assert [e["file"] for e in manifest["dumps"]] == [f"{prefix}_20261021_100000.sql"]

    def test_redis_copy_goes_through_partial(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "dump.rdb").write_bytes(b"REDIS0011" + b"\x00" * 50)
        replies = iter([b"1709000000\n", b"1709000001\n"])

        def exec_run(command, **kwargs):
            joined = " ".join(command)
            if "LASTSAVE" in joined:
                return 0, next(replies, b"1709000001\n")
            if "CONFIG GET dir" in joined:
                return 0, b"dir\n/data\n"
            if "CONFIG GET dbfilename" in joined:
                return 0, b"dbfilename\ndump.rdb\n"
            return 0, b"OK\n"

        container = MagicMock()
        container.exec_run.side_effect = exec_run
        container.attrs = {"Config": {"Env": []}, "Mounts": [{"Source": str(data), "Destination": "/data"}]}
        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        dumper = DBDumper(MagicMock(), config)
        dumper.docker.containers.get.return_value = container
        replaced = []
        real_replace = os.replace
        with patch("time.sleep"), patch.object(dumper_module.os, "replace",
                                               side_effect=lambda a, b: (replaced.append(a), real_replace(a, b))):
            result = dumper._dump_redis_blocking(
                DiscoveredDatabase(container_name="cache", db_type="redis", db_name="redis"))

        assert result.status == "success"
        assert replaced and replaced[0].endswith(".partial")
        assert _files(config.dump_dir) == [os.path.basename(result.dump_path)]
        assert os.path.basename(result.dump_path).startswith(dump_prefix("cache", "redis", "redis") + "_")


class TestSqliteIntegrityFailure:
    def _fake_run(self, integrity_stdout, integrity_stderr="", returncode=0):
        async def fake_run(cmd, **kwargs):
            if ".backup" in cmd[-1]:
                Path(cmd[-1].split(" ", 1)[1].strip("'")).write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
                return CommandResult(0, "", "", 0.1, "")
            if cmd[-1].startswith("PRAGMA integrity_check"):
                return CommandResult(returncode, integrity_stdout, integrity_stderr, 0.1, "")
            return CommandResult(0, "delete", "", 0.1, "")
        return fake_run

    @pytest.mark.asyncio
    async def test_corrupt_copy_is_a_failed_dump_and_previous_survives(self, mock_config, tmp_path):
        src = tmp_path / "src" / "vault.db"
        src.parent.mkdir()
        src.write_bytes(b"x")
        db = DiscoveredDatabase(container_name="vw", db_type="sqlite", db_name="vault.db", host_path=str(src))
        previous = mock_config.dump_dir / f"{dump_prefix('vw', 'sqlite', 'vault.db', str(src))}_20261021_100000.sqlite3"
        previous.write_bytes(b"SQLite format 3\x00good")
        dumper = DBDumper(_docker(), mock_config)

        corrupt = self._fake_run("*** in database main ***\nPage 42: btree page is not a leaf")
        with patch("app.services.db_dumper.run_command", side_effect=corrupt):
            (result,) = await dumper.dump_all([db])

        assert result.status == "failed" and result.integrity_check == "failed"
        assert "btree page is not a leaf" in result.error
        cleanup_dump_dir(mock_config.dump_dir, keep_last=1, max_age_days=None)
        assert _files(mock_config.dump_dir) == sorted([previous.name, DUMP_MANIFEST_NAME])

    @pytest.mark.asyncio
    async def test_unverifiable_schema_is_still_success(self, tmp_path):
        src = tmp_path / "lib.db"
        src.write_bytes(b"x")
        config = MagicMock()
        config.dump_dir = tmp_path / "dumps"
        unverifiable = self._fake_run("", "Error: no such collation sequence: icu_root", returncode=1)
        with patch("app.services.db_dumper.run_command", side_effect=unverifiable):
            result = await DBDumper(MagicMock(), config)._dump_sqlite(
                DiscoveredDatabase(container_name="plex", db_type="sqlite", db_name="lib.db", host_path=str(src)))
        assert result.status == "success" and result.integrity_detail == "unverifiable"
        assert os.path.exists(result.dump_path)


class TestProtectedCleanup:
    def test_protected_newest_dump_survives_age(self, tmp_path):
        live = dump_prefix("pg", "postgres", "app")
        gone = dump_prefix("old", "postgres", "app")
        _touch(tmp_path / f"{live}_20260901_000000.sql", age_days=50)
        _touch(tmp_path / f"{live}_20260801_000000.sql", age_days=80)
        _touch(tmp_path / f"{gone}_20260901_000000.sql", age_days=50)

        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7, protect_prefixes={live})

        assert _files(tmp_path) == [f"{live}_20260901_000000.sql"]

    def test_only_the_newest_protected_dump_is_exempt(self, tmp_path):
        live = dump_prefix("pg", "postgres", "app")
        _touch(tmp_path / f"{live}_20260901_000000.sql", age_days=50)
        _touch(tmp_path / f"{live}_20260801_000000.sql", age_days=80)
        cleanup_dump_dir(tmp_path, keep_last=2, max_age_days=7, protect_prefixes={live})
        assert _files(tmp_path) == [f"{live}_20260901_000000.sql"]

    def test_max_age_none_never_deletes_for_age(self, tmp_path):
        gone = dump_prefix("old", "postgres", "app")
        _touch(tmp_path / f"{gone}_20200101_000000.sql", age_days=900)
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None, protect_prefixes=set())
        assert _files(tmp_path) == [f"{gone}_20200101_000000.sql"]

    def test_legacy_dump_is_protected_until_a_hashed_dump_exists(self, tmp_path):
        live = dump_prefix("pg", "postgres", "app")
        legacy = {live: legacy_dump_prefix("pg", "app")}
        _touch(tmp_path / "pg_app_20260901_000000.sql.gz", age_days=50)
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7, protect_prefixes={live}, legacy_prefixes=legacy)
        assert _files(tmp_path) == ["pg_app_20260901_000000.sql.gz"]

        _touch(tmp_path / f"{live}_20261022_220000.sql")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7, protect_prefixes={live}, legacy_prefixes=legacy)
        assert _files(tmp_path) == [f"{live}_20261022_220000.sql"]

    def test_shared_sanitised_name_alone_never_retires_a_legacy_file(self, tmp_path):
        """A new database whose hashed prefix shares the base of an unrelated
        legacy file (e.g. an old roles dump) must not retire it."""
        db_named_globals = dump_prefix("pg", "postgres", "globals")
        _touch(tmp_path / "pg_globals_20260901_000000.sql", age_days=50)  # old roles dump
        _touch(tmp_path / f"{db_named_globals}_20261022_220000.sql")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None, protect_prefixes={db_named_globals})
        assert "pg_globals_20260901_000000.sql" in _files(tmp_path)

    def test_legacy_file_shared_by_two_databases_retires_only_when_both_moved_on(self, tmp_path):
        a = dump_prefix("c", "postgres", "sales data")
        b = dump_prefix("c", "postgres", "sales_data")
        legacy = {a: legacy_dump_prefix("c", "sales data"), b: legacy_dump_prefix("c", "sales_data")}
        assert legacy[a] == legacy[b] == "c_sales_data"
        _touch(tmp_path / "c_sales_data_20260901_000000.sql", age_days=50)
        _touch(tmp_path / f"{a}_20261022_220000.sql")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7, protect_prefixes={a, b}, legacy_prefixes=legacy)
        assert "c_sales_data_20260901_000000.sql" in _files(tmp_path)  # b has no new dump yet
        _touch(tmp_path / f"{b}_20261022_220000.sql")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7, protect_prefixes={a, b}, legacy_prefixes=legacy)
        assert "c_sales_data_20260901_000000.sql" not in _files(tmp_path)

    def test_legacy_dumps_still_group_and_age_out(self, tmp_path):
        _touch(tmp_path / "pg_app_20260101_000000.sql.gz", age_days=30)
        _touch(tmp_path / "pg_app_20260102_000000.sql.gz", age_days=29)
        assert cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7) == 2
        assert _files(tmp_path) == []

    def test_legacy_newer_than_hashed_is_kept(self, tmp_path):
        """A downgrade/upgrade round trip must not delete the newest dump."""
        live = dump_prefix("pg", "postgres", "app")
        _touch(tmp_path / f"{live}_20261020_000000.sql")
        _touch(tmp_path / "pg_app_20261022_000000.sql.gz")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None, protect_prefixes={live},
                         legacy_prefixes={live: legacy_dump_prefix("pg", "app")})
        assert _files(tmp_path) == sorted(["pg_app_20261022_000000.sql.gz", f"{live}_20261020_000000.sql"])


class TestHashedNameInference:
    def test_manifest_infers_hashed_names(self, tmp_path):
        app = dump_prefix("immich_postgres", "postgres", "immich")
        roles = dump_prefix("immich_postgres", "postgres", "globals", kind="globals")
        (tmp_path / f"{app}_20261022_220000.sql").write_bytes(pg_dump_output("immich"))
        (tmp_path / f"{roles}_20261022_220000.sql").write_bytes(PG_GLOBALS)

        manifest = write_dump_manifest(tmp_path, known_containers={"immich_postgres"})

        by_db = {e["db_name"]: e for e in manifest["dumps"]}
        assert by_db["immich"]["container_name"] == "immich_postgres"
        assert by_db["globals"]["kind"] == "globals" and by_db["globals"]["container_name"] == "immich_postgres"

    def test_verify_accepts_hashed_names(self, tmp_path):
        path = tmp_path / f"{dump_prefix('pg', 'postgres', 'app')}_20261022_220000.sql"
        path.write_bytes(pg_dump_output("app")[:200])
        assert VerifyEngine._check_plain_sql_dump(str(path))["status"] == "failed"  # Arkive-named, truncated


class TestDiscoveryCoverageWarnings:
    def _scan(self, mock_config, *containers, profiles=None):
        from app.services.discovery import DiscoveryEngine

        engine = DiscoveryEngine.__new__(DiscoveryEngine)
        engine.config = mock_config
        engine.docker = _docker(*containers)
        engine.profiles = profiles or []
        for c in containers:
            c.id = c.name
            c.labels = getattr(c, "labels", None) if isinstance(getattr(c, "labels", None), dict) else {}
            c.attrs.setdefault("NetworkSettings", {"Networks": {}})
        return {c.name: c for c in engine._scan_blocking()}

    def test_enumeration_failure_warns_once_and_keeps_fallback(self, mock_config):
        pg = _pg_cluster("pg", ["POSTGRES_DB=app"], [], list_exit=2)
        found = self._scan(mock_config, pg)
        assert [d.db_name for d in found["pg"].databases] == ["app"]
        (warning,) = found["pg"].warnings
        assert warning.startswith("could not list databases in pg (")
        assert warning.endswith("only app is being backed up")
        assert "does not exist" in warning

    def test_companion_failure_warns_once(self, mock_config):
        profile = {"name": "immich", "image_patterns": ["ghcr.io/immich-app/immich-server"],
                   "databases": [{"type": "postgres", "env_vars": {"db_name": ["POSTGRES_DB"],
                                                                    "db_host": ["DB_HOSTNAME"]}}]}
        app = _container("immich_server", ["DB_HOSTNAME=immich_postgres"], lambda cmd: (1, b"", b""),
                         tags=("ghcr.io/immich-app/immich-server:release",))
        db = _pg_cluster("immich_postgres", ["POSTGRES_DB=immich"], [], list_exit=2,
                         tags=("ghcr.io/immich-app/postgres:14",))
        found = self._scan(mock_config, app, db, profiles=[profile])
        assert len(found["immich_postgres"].warnings) == 1
        assert found["immich_server"].warnings == []

    def test_no_warning_when_listing_works_or_container_stopped(self, mock_config):
        ok = _pg_cluster("pg", ["POSTGRES_DB=app"], ["app", "wiki"])
        stopped = _pg_cluster("pg2", ["POSTGRES_DB=app"], [], list_exit=2, status="exited")
        found = self._scan(mock_config, ok, stopped)
        assert found["pg"].warnings == [] and found["pg2"].warnings == []
        assert [d.db_name for d in found["pg"].databases] == ["app", "wiki"]

    def test_maintenance_check_failure_warns(self, mock_config):
        def handler(cmd):
            if "FROM pg_database" in cmd[-1]:
                return 0, b"app\npostgres\n", b""
            return 2, b"", b"psql: error: permission denied for database postgres"

        pg = _container("pg", ["POSTGRES_DB=app"], handler)
        found = self._scan(mock_config, pg)
        assert [d.db_name for d in found["pg"].databases] == ["app"]
        assert "permission denied" in found["pg"].warnings[0]

    def test_warnings_are_not_persisted_and_serialise(self, mock_config):
        from app.models.discovery import DiscoveredContainer

        container = DiscoveredContainer(name="pg", image="postgres:16", status="running", warnings=["x"])
        assert container.model_dump()["warnings"] == ["x"]
        assert DiscoveredContainer(name="pg", image="postgres:16", status="running").warnings == []


class TestRestorePlanFileNames:
    def test_commands_name_hashed_and_legacy_files(self, mock_config):
        from app.services.restore_plan import RestorePlanGenerator

        gen = RestorePlanGenerator(mock_config)
        _, pg_full = gen._get_restore_commands("postgres", "app", "pg")
        assert f"< {dump_prefix('pg', 'postgres', 'app')}_[0-9]*.sql\n" in pg_full
        assert f"gunzip -c {dump_prefix('pg', 'postgres', 'app')}_[0-9]*.sql.gz |" in pg_full
        assert "pg_app_[0-9]*.sql.gz" in pg_full  # Arkive 0.3 name

        _, roles_full = gen._get_globals_restore_commands("pg")
        assert dump_prefix("pg", "postgres", "globals", kind="globals") in roles_full

        _, sqlite_full = gen._get_restore_commands("sqlite", "db.sqlite3", "vw", "/mnt/user/appdata/vw/db.sqlite3")
        assert dump_prefix("vw", "sqlite", "db.sqlite3", "/mnt/user/appdata/vw/db.sqlite3") in sqlite_full
