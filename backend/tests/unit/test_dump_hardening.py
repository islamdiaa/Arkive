"""DB dumper hardening: exit codes, truncation, naming collisions, cleanup."""

import os
import time
from unittest.mock import MagicMock, patch

import docker
import pytest

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper, cleanup_dump_dir
from app.utils.subprocess_runner import CommandResult

PG_OK = b"--\n-- PostgreSQL database dump\n--\nCREATE TABLE t (id int);\n" + b"x" * 200 + \
    b"\n--\n-- PostgreSQL database dump complete\n--\n\n"


def _dumper(tmp_path):
    config = MagicMock()
    config.dump_dir = tmp_path
    return DBDumper(MagicMock(), config)


def _api_container(api, env=None):
    container = MagicMock()
    container.id = "cid"
    container.attrs = {"Config": {"Env": env or ["POSTGRES_USER=postgres"]}}
    container.client.api = api
    return container


def _api(stdout_chunks, exit_code):
    api = MagicMock(spec=docker.APIClient)
    api.exec_create.return_value = {"Id": "exec-1"}
    api.exec_start.return_value = iter([(c, None) for c in stdout_chunks])
    api.exec_inspect.return_value = {"ExitCode": exit_code}
    return api


class TestLowLevelExecExitCode:
    def test_nonzero_exit_detected_via_exec_inspect(self, tmp_path):
        dumper = _dumper(tmp_path)
        api = _api([PG_OK], exit_code=1)
        dumper.docker.containers.get.return_value = _api_container(api)

        result = dumper._dump_postgres_blocking(DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app"))

        assert result.status == "failed"
        assert "exited with code 1" in result.error
        api.exec_inspect.assert_called_once_with("exec-1")

    def test_success_requires_trailer(self, tmp_path):
        dumper = _dumper(tmp_path)
        dumper.docker.containers.get.return_value = _api_container(_api([PG_OK], exit_code=0))

        result = dumper._dump_postgres_blocking(DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app"))

        assert result.status == "success"
        assert result.integrity_check == "ok"
        # dump_compression defaults to "none": a plain .sql restic can deduplicate.
        assert result.dump_path.endswith(".sql")
        with open(result.dump_path, "rb") as f:
            assert f.read() == PG_OK

    def test_truncated_dump_with_zero_exit_is_failed(self, tmp_path):
        dumper = _dumper(tmp_path)
        truncated = PG_OK[: len(PG_OK) // 2]
        dumper.docker.containers.get.return_value = _api_container(_api([truncated], exit_code=0))

        result = dumper._dump_postgres_blocking(DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app"))

        assert result.status == "failed"
        assert "incomplete" in result.error

    def test_mysql_password_passed_via_environment(self, tmp_path):
        dumper = _dumper(tmp_path)
        api = _api([b"-- dump\n" + b"y" * 200 + b"\n-- Dump completed on 2026-01-01\n"], exit_code=0)
        container = _api_container(api, env=["MYSQL_ROOT_PASSWORD=s3cret", "MYSQL_DATABASE=app"])
        container.image.tags = ["mariadb:11"]
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mariadb_blocking(DiscoveredDatabase(container_name="db", db_type="mariadb", db_name="app"))

        assert result.status == "success"
        args, kwargs = api.exec_create.call_args
        assert "s3cret" not in " ".join(args[1])
        assert kwargs["environment"] == {"MYSQL_PWD": "s3cret"}
        assert "--single-transaction" in args[1]


class TestDumpAll:
    @pytest.mark.asyncio
    async def test_failed_dump_file_is_removed(self, tmp_path):
        dumper = _dumper(tmp_path)
        partial = tmp_path / "pg_app_20260101_000000.sql.gz"

        async def fake_dump(db):
            partial.write_bytes(b"half")
            from app.services.db_dumper import DumpResult
            return DumpResult(container_name="pg", db_type="postgres", db_name="app", dump_path=str(partial),
                              dump_size_bytes=0, integrity_check="failed", status="failed", error="cut")

        dumper._dump_one = fake_dump
        results = await dumper.dump_all([DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app")])

        assert results[0].status == "failed"
        assert not partial.exists()

    @pytest.mark.asyncio
    async def test_same_basename_sqlite_files_get_distinct_dump_paths(self, tmp_path):
        dumper = _dumper(tmp_path)
        dbs = [
            DiscoveredDatabase(container_name="app", db_type="sqlite", db_name="creds.db", host_path="/a/creds.db"),
            DiscoveredDatabase(container_name="app", db_type="sqlite", db_name="creds.db", host_path="/b/creds.db"),
            DiscoveredDatabase(container_name="app", db_type="sqlite", db_name="other.db", host_path="/a/other.db"),
        ]
        seen = []

        async def fake_dump(db):
            seen.append(dumper._dump_path(db, "20260101_000000", ".sqlite3"))
            from app.services.db_dumper import DumpResult
            return DumpResult(container_name=db.container_name, db_type="sqlite", db_name=db.db_name,
                              dump_path="", dump_size_bytes=1, integrity_check="ok", status="success")

        dumper._dump_one = fake_dump
        await dumper.dump_all(dbs + [dbs[0]])  # exact duplicate is dropped

        assert len(seen) == 3
        assert len(set(seen)) == 3
        assert seen[2].endswith("app_other.db_20260101_000000.sqlite3")  # no suffix when unique


class TestSqliteIntegrity:
    @pytest.mark.asyncio
    async def test_unknown_collation_is_unverifiable_not_failed(self, tmp_path):
        dumper = _dumper(tmp_path)
        src = tmp_path / "src.db"
        src.write_bytes(b"x")

        async def fake_run(cmd, **kwargs):
            if ".backup" in cmd[-1]:
                dest = cmd[-1].split(" ", 1)[1].strip("'")
                with open(dest, "wb") as f:
                    f.write(b"SQLite format 3\x00" + b"\x00" * 100)
                return CommandResult(0, "", "", 0.1, "")
            if cmd[-1].startswith("PRAGMA integrity_check"):
                return CommandResult(0, "", "Error: in prepare, no such collation sequence: icu_root", 0.1, "")
            return CommandResult(0, "delete", "", 0.1, "")

        with patch("app.services.db_dumper.run_command", side_effect=fake_run):
            result = await dumper._dump_sqlite(
                DiscoveredDatabase(container_name="plex", db_type="sqlite", db_name="lib.db", host_path=str(src))
            )

        assert result.status == "success"
        assert result.integrity_check == "skipped"


def _touch(path, age_days=0):
    path.write_bytes(b"x")
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


class TestCleanupDumpDir:
    def test_keeps_newest_per_database(self, tmp_path):
        for ts in ("20260101_000000", "20260102_000000", "20260103_000000"):
            _touch(tmp_path / f"pg_app_{ts}.sql.gz")
        removed = cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert removed == 2
        assert [p.name for p in tmp_path.iterdir()] == ["pg_app_20260103_000000.sql.gz"]

    def test_removes_dumps_of_vanished_containers_by_age(self, tmp_path):
        _touch(tmp_path / "gone_db_20260101_000000.sql.gz", age_days=30)
        _touch(tmp_path / "live_db_20260301_000000.sql.gz")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7)
        assert [p.name for p in tmp_path.iterdir()] == ["live_db_20260301_000000.sql.gz"]

    def test_leaves_flash_archives_and_self_backup(self, tmp_path):
        for name in ("flash_20260101_000000.tar.gz", "flash_20260102_000000.tar.gz", "arkive_self.db"):
            _touch(tmp_path / name, age_days=30)
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=7)
        assert len(list(tmp_path.iterdir())) == 3

    def test_removes_orphaned_sqlite_sidecars(self, tmp_path):
        _touch(tmp_path / "plex_lib.db_20260101_000000.sqlite3")
        _touch(tmp_path / "plex_lib.db_20260102_000000.sqlite3")
        _touch(tmp_path / "plex_lib.db_20260101_000000.sqlite3-wal")
        _touch(tmp_path / "plex_lib.db_20260101_000000.sqlite3-shm")
        _touch(tmp_path / "plex_lib.db_20260102_000000.sqlite3-shm")
        cleanup_dump_dir(tmp_path, keep_last=1, max_age_days=None)
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "plex_lib.db_20260102_000000.sqlite3",
            "plex_lib.db_20260102_000000.sqlite3-shm",
        ]

    def test_missing_dir_is_noop(self, tmp_path):
        assert cleanup_dump_dir(tmp_path / "nope") == 0
