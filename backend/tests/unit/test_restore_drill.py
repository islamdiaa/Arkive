"""Unit tests for restore drills (services/restore_drill.py).

The engine is exercised end to end with fakes: a fake backup engine that
"restores" realistic dump files into the restore dir and a fake Docker client
that records every container/exec call.
"""

import gzip
import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.core.database import init_db, run_migrations
from app.services.backup_engine import ResticError
import app.services.restore_drill as rd
from app.services.restore_drill import (
    DRILL_MARKER,
    DUMP_MANIFEST_NAME,
    MIB,
    DrillAlreadyRunningError,
    DrillFailure,
    DumpCandidate,
    RestoreDrillEngine,
    candidates_from_manifest,
    infer_candidates,
    parse_pg_connect,
    prepare_sql_dump,
    select_candidates,
    valid_pg_dbname,
    verify_sqlite_file,
)

# ---------------------------------------------------------------------------
# Realistic dump content (with completion trailers)
# ---------------------------------------------------------------------------

PG_DUMP = b"""--
-- PostgreSQL database dump
--

-- Dumped from database version 16.4
-- Dumped by pg_dump version 16.4

SET statement_timeout = 0;
SET client_encoding = 'UTF8';
DROP DATABASE IF EXISTS appdb;
CREATE DATABASE appdb WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE_PROVIDER = libc LOCALE = 'en_US.utf8';
\\connect appdb
SET statement_timeout = 0;
CREATE TABLE public.users (
    id integer NOT NULL,
    name text
);
CREATE TABLE public.notes (
    body text
);
CREATE UNLOGGED TABLE public.cache (
    k text
);
COPY public.users (id, name) FROM stdin;
1\talice
2\tbob
\\.
COPY public.notes (body) FROM stdin;
CREATE TABLE this_is_row_data_not_ddl
\\.
--
-- PostgreSQL database dump complete
--

"""

MYSQL_DUMP = b"""-- MariaDB dump 10.19  Distrib 10.11.6-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: appdb
-- ------------------------------------------------------
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `appdb` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;
USE `appdb`;
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id` int(11) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `users` VALUES (1),(2);
-- Dump completed on 2026-10-22  3:00:01
"""


def _sqlite_bytes(tmp_path: Path, name: str = "src.db", tables: int = 2) -> bytes:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    for i in range(tables):
        conn.execute(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute(f"INSERT INTO t{i} (v) VALUES ('x')")
    conn.commit()
    conn.close()
    return path.read_bytes()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Cfg:
    def __init__(self, root: Path):
        self.config_dir = root
        self.db_path = root / "arkive.db"
        self.dump_dir = root / "dumps"
        self.restore_dir = root / "restores"


class FakeBackupEngine:
    def __init__(self, dump_dir: Path, files: dict[str, bytes], sizes: dict[str, int] | None = None,
                 snapshots: list | None = None):
        self.dump_dir = str(dump_dir)
        self.files = files
        self.sizes = sizes or {}
        self.restore_calls: list[tuple[list[str], str]] = []
        self.restore_status = "success"
        self.restore_fail_for: set[str] = set()  # basenames whose restore fails
        self.list_error: dict[str, str] = {}  # target id -> restic error
        self.ls_fails = False
        self._snapshots = snapshots if snapshots is not None else [
            {"id": "b" * 64, "short_id": "bbbbbbbb", "paths": ["/mnt/user/appdata"]},
            {"id": "a" * 64, "short_id": "aaaaaaaa", "paths": [self.dump_dir, "/mnt/user/appdata"]},
        ]

    async def list_snapshots(self, target):
        if target["id"] in self.list_error:
            raise ResticError(self.list_error[target["id"]])
        return self._snapshots

    async def snapshots(self, target):
        try:
            return await self.list_snapshots(target)
        except ResticError:
            return []

    async def ls(self, target, snapshot_id, path, strict=False):
        assert path == self.dump_dir
        if self.ls_fails:
            if strict:
                raise ResticError("restic ls failed: unable to read pack")
            return []  # what BackupEngine.ls returns when restic fails
        entries = [{"name": "dumps", "type": "directory", "size": None}]
        for name, data in self.files.items():
            entries.append({"name": name, "type": "file", "size": self.sizes.get(name, len(data))})
        return entries

    async def restore(self, target, snapshot_id, paths=None, restore_to=None):
        self.restore_calls.append((list(paths or []), restore_to))
        if self.restore_status != "success" or any(os.path.basename(p) in self.restore_fail_for
                                                   for p in paths or []):
            return {"status": "failed", "error": "repository unreachable"}
        for p in paths or []:
            dest = Path(restore_to) / p.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.files[os.path.basename(p)])
        return {"status": "success"}


class ExecResult(NamedTuple):
    exit_code: int | None
    output: bytes


class FakeContainer:
    def __init__(self, docker: "FakeDocker", kwargs: dict):
        self.docker = docker
        self.kwargs = kwargs
        self.id = f"cid{len(docker.created)}"
        self.name = kwargs.get("name")
        self.labels = kwargs.get("labels", {})
        self.status = "created"
        self.removed = None
        self.execs: list[tuple[list, dict]] = []
        self.archives: list[tuple[str, dict]] = []

    def start(self):
        self.status = self.docker.start_status(self)

    def reload(self):
        pass

    def exec_run(self, cmd, environment=None):
        self.execs.append((list(cmd), dict(environment or {})))
        code, out = self.docker.behavior(self, list(cmd), environment or {})
        return ExecResult(code, out)

    def put_archive(self, path, data):
        raw = data.read() if hasattr(data, "read") else data
        members = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for m in tar.getmembers():
                members[m.name] = tar.extractfile(m).read() if m.isfile() else None
        self.archives.append((path, members))
        return True

    def remove(self, force=False, v=False):
        self.removed = {"force": force, "v": v}
        self.docker.removed.append(self)

    def logs(self, tail=None):
        return b"initdb: done\nFATAL:  could not load library \"vectors.so\"\n"


class FakeDocker:
    def __init__(self):
        self.created: list[FakeContainer] = []
        self.removed: list[FakeContainer] = []
        self.sources: dict[str, dict] = {}
        self.image_configs: dict[str, dict] = {
            "postgres:16": {"Env": ["PGDATA=/var/lib/postgresql/data"], "Cmd": ["postgres"],
                            "Entrypoint": ["docker-entrypoint.sh"],
                            "Volumes": {"/var/lib/postgresql/data": {}}},
            "mariadb:11": {"Env": [], "Cmd": ["mariadbd"], "Entrypoint": ["docker-entrypoint.sh"],
                           "Volumes": {"/var/lib/mysql": {}}},
        }
        self.leftovers: list[FakeContainer] = []
        self.load_code = 0
        self.table_count = 3
        self.exec_error: Exception | None = None
        self.containers = SimpleNamespace(get=self._get, create=self._create, list=self._list)
        self.images = SimpleNamespace(get=self._image_get, pull=self._image_pull)

    def _get(self, name):
        if name not in self.sources:
            raise RuntimeError(f"No such container: {name}")
        return SimpleNamespace(attrs=self.sources[name])

    def _create(self, **kwargs):
        container = FakeContainer(self, kwargs)
        self.created.append(container)
        return container

    def _list(self, all=False, filters=None):
        return list(self.leftovers)

    def _image_get(self, image):
        if image not in self.image_configs:
            raise RuntimeError("image not found")
        return SimpleNamespace(attrs={"Config": self.image_configs[image]})

    def _image_pull(self, image):
        raise RuntimeError("pull access denied")

    def start_status(self, container):
        return "running"

    def behavior(self, container, cmd, env):
        if self.exec_error is not None:
            raise self.exec_error
        if cmd[0] == "pg_isready":
            return 0, b""
        if cmd[0] in ("mariadb", "mariadb-admin") and cmd[1:] == ["--version"]:
            return 0, b"mariadb from 11.4"
        if cmd[0] == "mariadb-admin":
            return 0, b"mysqld is alive"
        if cmd[:2] == ["sh", "-c"]:
            return self.load_code, b""
        if cmd[0] == "tail":
            return 0, b'psql:/arkive-drill/dump.sql:42: ERROR:  extension "vectors" is not available\n'
        if cmd[0] in ("psql", "mariadb"):
            return 0, f"{self.table_count}\n".encode()
        return 127, b"executable file not found"


@pytest.fixture(autouse=True)
def _no_host_lookup(monkeypatch):
    monkeypatch.setattr(rd, "running_in_container", lambda: True)
    monkeypatch.setattr(rd, "host_path_for", lambda path, client: None)


_TEMPLATE_DB: dict[str, bytes] = {}


async def _make_cfg(tmp_path: Path, targets=(("t1", "Local NAS", "local"),)) -> Cfg:
    cfg = Cfg(tmp_path)
    cfg.dump_dir.mkdir(parents=True, exist_ok=True)
    cfg.restore_dir.mkdir(parents=True, exist_ok=True)
    # Building the schema is slow on some filesystems; do it once and copy.
    if "db" not in _TEMPLATE_DB:
        template = tmp_path / "template.db"
        await init_db(template)
        await run_migrations(template)
        conn = sqlite3.connect(template)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.close()
        _TEMPLATE_DB["db"] = template.read_bytes()
        template.unlink()
    cfg.db_path.write_bytes(_TEMPLATE_DB["db"])
    async with aiosqlite.connect(cfg.db_path) as db:
        for tid, name, ttype in targets:
            await db.execute(
                "INSERT INTO storage_targets (id, name, type, config) VALUES (?, ?, ?, ?)",
                (tid, name, ttype, json.dumps({"path": "/backups"})),
            )
        await db.commit()
    return cfg


def _manifest(entries: list[dict]) -> bytes:
    return json.dumps({"version": 1, "generated_at": "2026-10-22T03:00:00Z",
                       "arkive_version": "0.4.0", "dumps": entries}).encode()


def _entry(file, container, db_type, db_name, image=None, kind="database", size=1000):
    return {"file": file, "container_name": container, "image": image, "db_type": db_type,
            "db_name": db_name, "host_path": None, "kind": kind,
            "compression": "gzip" if file.endswith(".gz") else "none",
            "size_bytes": size, "created_at": "2026-10-22T03:00:00Z", "integrity": "ok"}


def _engine(cfg, backup, docker=None, notifier=None, event_bus=None) -> RestoreDrillEngine:
    lock = MagicMock()
    lock.is_backup_running.return_value = False
    lock.is_restore_running.return_value = False
    engine = RestoreDrillEngine(cfg, backup_engine=backup, docker_client=docker,
                                notifier=notifier, event_bus=event_bus, lock_manager=lock)
    engine.poll_seconds = 0
    engine.idle_poll_seconds = 0
    engine.ready_timeout_seconds = 1
    engine.min_free_bytes = 0
    return engine


async def _rows(cfg, sql, args=()):
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, args)
        return [dict(r) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _cand(container, db_type, db_name, size=100, **kw):
    return DumpCandidate(file=f"{container}_{db_name}_20261022_030000.sql", container_name=container,
                         db_type=db_type, db_name=db_name, size_bytes=size, **kw)


class TestSelection:
    def test_rotation_prefers_failed_then_never_then_oldest(self):
        cands = [_cand("a", "sqlite", "a.db"), _cand("b", "sqlite", "b.db"),
                 _cand("c", "sqlite", "c.db"), _cand("d", "postgres", "d")]
        history = {
            ("a", "a.db"): ("2026-10-01T07:00:00Z", "passed"),
            ("b", "b.db"): ("2026-10-15T07:00:00Z", "failed"),
            ("d", "d"): ("2026-09-01T07:00:00Z", "passed"),
        }
        chosen, excluded = select_candidates(cands, history, 3, 2048 * MIB)
        assert excluded == []
        assert [c.container_name for c in chosen] == ["b", "c", "d"]

    def test_size_cap_and_untested_types_are_skipped_with_reasons(self):
        cands = [
            _cand("big", "postgres", "big", size=3000 * MIB),
            _cand("cache", "redis", "redis"),
            _cand("docs", "mongodb", "docs"),
            DumpCandidate(file="pg_globals_20261022_030000.sql", container_name="pg", db_type="postgres",
                          db_name="globals", kind="globals"),
            _cand("ok", "postgres", "ok"),
        ]
        chosen, excluded = select_candidates(cands, {}, 3, 2048 * MIB)
        assert [c.container_name for c in chosen] == ["ok"]
        assert {status for _, status, _ in excluded} == {"skipped"}
        reasons = {c.container_name: r for c, _, r in excluded}
        assert reasons["big"] == "dump is 3000 MB; limit is 2048 MB"
        assert reasons["cache"] == "redis dumps are not drill-tested"
        assert reasons["docs"] == "mongodb dumps are not drill-tested"
        assert "globals" in reasons["pg"]

    def test_at_least_one_sql_server_database(self):
        cands = [_cand(f"s{i}", "sqlite", f"s{i}.db") for i in range(5)] + [_cand("z", "mariadb", "z")]
        chosen, _ = select_candidates(cands, {}, 3, 2048 * MIB)
        assert len(chosen) == 3
        assert any(c.db_type == "mariadb" for c in chosen)
        chosen, _ = select_candidates(cands, {}, 1, 2048 * MIB)
        assert [c.db_type for c in chosen] == ["mariadb"]

    def test_mix_includes_sqlite_when_two_slots(self):
        cands = [_cand(f"p{i}", "postgres", f"p{i}") for i in range(4)] + [_cand("z", "sqlite", "z.db")]
        chosen, _ = select_candidates(cands, {}, 2, 2048 * MIB)
        assert {c.db_type for c in chosen} == {"postgres", "sqlite"}

    def test_missing_from_snapshot_is_a_failure(self):
        cand = _cand("a", "postgres", "a", in_snapshot=False)
        chosen, excluded = select_candidates([cand], {}, 3, 2048 * MIB)
        assert chosen == []
        assert excluded == [(cand, "failed",
                             "listed in the snapshot's dump manifest but missing from the snapshot")]


class TestCandidates:
    def test_from_manifest_uses_listing_size(self):
        manifest = {"dumps": [_entry("pg_appdb_20261022_030000.sql", "pg", "postgres", "appdb",
                                     image="postgres:16", size=5),
                              _entry("../evil.sql", "x", "postgres", "x")]}
        files = {"pg_appdb_20261022_030000.sql": {"name": "pg_appdb_20261022_030000.sql", "size": 4242}}
        cands = candidates_from_manifest(manifest, files)
        assert len(cands) == 2
        assert cands[0].size_bytes == 4242 and cands[0].image == "postgres:16" and cands[0].in_snapshot
        # basename only — and it isn't in the snapshot
        assert cands[1].file == "evil.sql" and not cands[1].in_snapshot

    def test_infer_without_manifest(self):
        files = {name: {"name": name, "size": 10} for name in [
            "immich_postgres_immich_20261021_030000.sql.gz",
            "immich_postgres_immich_20261022_030000.sql.gz",  # newer — wins
            "immich_postgres_globals_20261022_030000.sql",
            "vaultwarden_db.sqlite3-1a2b3c4d_20261022_030000.sqlite3",
            "paperless-redis-1_redis_20261022_030000.rdb",
            "mystery_thing_20261022_030000.sql",
            "flash_20261022_030000.tar.gz",
            "arkive_self.db",
            DUMP_MANIFEST_NAME,
        ]}
        known = [
            {"container_name": "immich_postgres", "db_type": "postgres", "db_name": "immich"},
            {"container_name": "vaultwarden", "db_type": "sqlite", "db_name": "db.sqlite3"},
        ]
        cands = infer_candidates(files, known, {"immich_postgres": "ghcr.io/example/postgres:14"})
        by_file = {c.file: c for c in cands}
        pg = by_file["immich_postgres_immich_20261022_030000.sql.gz"]
        assert (pg.container_name, pg.db_type, pg.db_name, pg.image) == (
            "immich_postgres", "postgres", "immich", "ghcr.io/example/postgres:14")
        assert "immich_postgres_immich_20261021_030000.sql.gz" not in by_file
        assert by_file["immich_postgres_globals_20261022_030000.sql"].kind == "globals"
        vw = by_file["vaultwarden_db.sqlite3-1a2b3c4d_20261022_030000.sqlite3"]
        assert (vw.container_name, vw.db_type) == ("vaultwarden", "sqlite")
        assert by_file["paperless-redis-1_redis_20261022_030000.rdb"].db_type == "redis"
        assert by_file["mystery_thing_20261022_030000.sql"].db_type == "unknown"
        assert not any(f.startswith("flash_") or f in ("arkive_self.db", DUMP_MANIFEST_NAME) for f in by_file)


# ---------------------------------------------------------------------------
# File checks
# ---------------------------------------------------------------------------


class TestSqliteVerify:
    def test_pass(self, tmp_path):
        path = tmp_path / "ok.db"
        path.write_bytes(_sqlite_bytes(tmp_path, tables=3))
        status, detail = verify_sqlite_file(path)
        assert status == "passed" and "3 tables" in detail

    def test_corrupt_file_fails(self, tmp_path):
        path = tmp_path / "bad.db"
        path.write_bytes(b"this is not sqlite" * 400)
        status, detail = verify_sqlite_file(path)
        assert status == "failed" and "not a valid SQLite database" in detail

    def test_zero_tables_fails(self, tmp_path):
        path = tmp_path / "empty.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        status, detail = verify_sqlite_file(path)
        assert status == "failed" and "no tables" in detail

    def test_unknown_collation_is_unverifiable_but_passes(self, tmp_path):
        path = tmp_path / "plex.db"
        conn = sqlite3.connect(path)
        conn.create_collation("icu_root", lambda a, b: (a > b) - (a < b))
        conn.execute("CREATE TABLE items (title TEXT COLLATE icu_root)")
        conn.execute("CREATE INDEX idx_title ON items(title COLLATE icu_root)")
        conn.execute("INSERT INTO items VALUES ('b'), ('a')")
        conn.commit()
        conn.close()
        status, detail = verify_sqlite_file(path)
        assert status == "passed"
        assert "not verifiable" in detail and "icu_root" in detail

    def test_empty_file_fails(self, tmp_path):
        path = tmp_path / "zero.db"
        path.write_bytes(b"")
        assert verify_sqlite_file(path)[0] == "failed"


class TestPrepareSqlDump:
    def test_plain_postgres_dump(self, tmp_path):
        src = tmp_path / "in.sql"
        src.write_bytes(PG_DUMP)
        prepared = prepare_sql_dump(src, tmp_path / "out.sql", "postgres")
        assert prepared.create_tables == 3  # COPY row data is not counted
        assert prepared.database == "appdb"
        assert prepared.trailer_ok
        assert (tmp_path / "out.sql").read_bytes() == PG_DUMP

    def test_gzip_mysql_dump(self, tmp_path):
        src = tmp_path / "in.sql.gz"
        src.write_bytes(gzip.compress(MYSQL_DUMP))
        prepared = prepare_sql_dump(src, tmp_path / "out.sql", "mariadb")
        assert prepared.create_tables == 1 and prepared.trailer_ok
        assert prepared.size_bytes == len(MYSQL_DUMP)
        assert not src.exists()

    def test_truncated_dump_detected(self, tmp_path):
        src = tmp_path / "in.sql"
        src.write_bytes(PG_DUMP.split(b"COPY public.users")[0])
        assert prepare_sql_dump(src, tmp_path / "out.sql", "postgres").trailer_ok is False

    def test_corrupt_gzip_raises_failure(self, tmp_path):
        src = tmp_path / "in.sql.gz"
        src.write_bytes(gzip.compress(PG_DUMP)[:-20])
        with pytest.raises(DrillFailure, match="corrupt gzip"):
            prepare_sql_dump(src, tmp_path / "out.sql", "postgres")


# ---------------------------------------------------------------------------
# Engine end to end (fakes)
# ---------------------------------------------------------------------------

PG_FILE = "immich_postgres_appdb_20261022_030000.sql"
SQLITE_FILE = "vaultwarden_db.sqlite3_20261022_030000.sqlite3"


def _standard_files(tmp_path, pg_bytes=PG_DUMP):
    return {
        PG_FILE: pg_bytes,
        SQLITE_FILE: _sqlite_bytes(tmp_path),
        "immich_postgres_globals_20261022_030000.sql": b"-- roles\nCREATE ROLE alice;\n",
        DUMP_MANIFEST_NAME: _manifest([
            _entry(PG_FILE, "immich_postgres", "postgres", "appdb", image="postgres:16"),
            _entry(SQLITE_FILE, "vaultwarden", "sqlite", "db.sqlite3"),
            _entry("immich_postgres_globals_20261022_030000.sql", "immich_postgres", "postgres",
                   "globals", image="postgres:16", kind="globals"),
        ]),
    }


def _docker_with_source():
    docker = FakeDocker()
    docker.sources["immich_postgres"] = {"Config": {
        "Image": "postgres:16",
        "Env": ["POSTGRES_USER=alice", "POSTGRES_DB=appdb", "POSTGRES_PASSWORD=hunter2", "TZ=UTC"],
        "Entrypoint": ["docker-entrypoint.sh"],
        "Cmd": ["postgres", "-c", "shared_preload_libraries=vectors.so"],
    }}
    return docker


@pytest.mark.asyncio
async def test_drill_passes_and_is_recorded(tmp_path, monkeypatch):
    # Running as root: no user override on the throwaway container (the
    # non-root case is covered separately; CI runners are not root).
    monkeypatch.setattr(rd.os, "geteuid", lambda: 0)
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    notifier = MagicMock()
    notifier.send = AsyncMock()
    bus = MagicMock()
    bus.publish = AsyncMock()
    engine = _engine(cfg, backup, docker, notifier, bus)

    summary = await engine.run_drill(trigger="scheduled")

    assert summary["status"] == "passed"
    assert summary["databases_tested"] == 2 and summary["databases_passed"] == 2
    drills = await _rows(cfg, "SELECT * FROM restore_drills")
    assert len(drills) == 1
    d = drills[0]
    assert (d["status"], d["trigger"], d["target_id"], d["snapshot_id"]) == ("passed", "scheduled", "t1", "aaaaaaaa")
    assert (d["databases_tested"], d["databases_passed"], d["databases_failed"]) == (2, 2, 0)
    assert d["completed_at"]
    results = {r["dump_file"]: r for r in await _rows(cfg, "SELECT * FROM restore_drill_results")}
    assert results[PG_FILE]["status"] == "passed" and "3 tables restored" in results[PG_FILE]["detail"]
    assert results[SQLITE_FILE]["status"] == "passed"
    assert results["immich_postgres_globals_20261022_030000.sql"]["status"] == "skipped"

    # Throwaway container: isolated, capped, labelled, source user/command, random password.
    assert len(docker.created) == 1
    kw = docker.created[0].kwargs
    assert kw["image"] == "postgres:16"
    assert kw["network_mode"] == "none"
    assert kw["labels"] == {"arkive.drill": d["id"]}
    assert kw["nano_cpus"] == 2_000_000_000 and kw["mem_limit"] == "2g"
    assert kw["command"] == ["postgres", "-c", "shared_preload_libraries=vectors.so"]
    assert kw["environment"]["POSTGRES_USER"] == "alice"
    assert kw["environment"]["POSTGRES_DB"] == "appdb"
    assert kw["environment"]["POSTGRES_PASSWORD"] not in ("", "hunter2")
    assert "TZ" not in kw["environment"]
    assert "user" not in kw
    # No host path known -> dump copied in with put_archive, default volume removed with v=True.
    assert "volumes" not in kw
    path, members = docker.created[0].archives[0]
    assert path == "/" and members["arkive-drill/dump.sql"] == PG_DUMP
    assert docker.created[0].removed == {"force": True, "v": True}
    load = [c for c in docker.created[0].execs if c[0][:2] == ["sh", "-c"]][0]
    assert "ON_ERROR_STOP=1" in load[0][2]
    assert load[1]["ARKIVE_DRILL_USER"] == "alice" and load[1]["ARKIVE_DRILL_MAINTDB"] == "postgres"
    count = [c for c in docker.created[0].execs if c[0][0] == "psql"][0]
    assert count[0][count[0].index("-d") + 1] == "appdb"

    # Events, activity, no failure notification, scratch dir gone.
    events = [c.args[0] for c in bus.publish.await_args_list]
    assert events == ["drill:started", "drill:completed"]
    assert bus.publish.await_args_list[1].args[1]["databases_passed"] == 2
    notifier.send.assert_not_awaited()
    activity = await _rows(cfg, "SELECT * FROM activity_log WHERE action = 'restore_drill'")
    assert activity and activity[0]["type"] == "system" and activity[0]["severity"] == "info"
    assert list(cfg.restore_dir.iterdir()) == []
    assert not engine.is_running()


@pytest.mark.asyncio
async def test_load_failure_fails_drill_and_notifies(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    docker.load_code = 3
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, backup, docker, notifier)

    summary = await engine.run_drill()

    assert summary["status"] == "failed" and summary["databases_failed"] == 1
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "failed"
    assert "psql load exited with code 3" in result["detail"]
    assert 'extension "vectors" is not available' in result["detail"]
    assert docker.created[0].removed == {"force": True, "v": True}
    notifier.send.assert_awaited_once()
    args, kwargs = notifier.send.await_args
    assert args[0] == "drill.failed" and kwargs["severity"] == "error"
    assert "immich_postgres/appdb" in args[2]
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert drill["status"] == "failed" and "immich_postgres/appdb" in drill["error_message"]


@pytest.mark.asyncio
async def test_table_count_mismatch_fails(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    docker.table_count = 1
    engine = _engine(cfg, backup, docker)
    await engine.run_drill()
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "failed" and "only 1 of 3 tables" in result["detail"]


@pytest.mark.asyncio
async def test_container_removed_when_exec_raises(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    engine = _engine(cfg, backup, docker)
    original = docker.behavior

    def boom(container, cmd, env):
        if cmd[:2] == ["sh", "-c"]:
            raise RuntimeError("docker exec exploded")
        return original(container, cmd, env)

    docker.behavior = boom
    summary = await engine.run_drill()
    # The server was up and the load had started: an error now means the restore
    # did not complete, which must not be hidden behind the SQLite dump passing.
    assert summary["status"] == "failed"
    assert docker.created and all(c.removed == {"force": True, "v": True} for c in docker.created)
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "failed"
    assert "docker exec exploded" in result["detail"] and "loading or verifying" in result["detail"]
    assert list(cfg.restore_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_truncated_dump_fails_without_container(tmp_path):
    cfg = await _make_cfg(tmp_path)
    truncated = PG_DUMP.split(b"COPY public.users")[0]
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path, pg_bytes=truncated))
    docker = _docker_with_source()
    engine = _engine(cfg, backup, docker)
    await engine.run_drill()
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "failed" and "truncated" in result["detail"]
    assert docker.created == []


@pytest.mark.asyncio
async def test_retries_with_image_default_when_source_command_dies(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    docker.start_status = lambda c: "exited" if "command" in c.kwargs else "running"
    engine = _engine(cfg, backup, docker)
    await engine.run_drill()
    assert len(docker.created) == 2
    assert "command" in docker.created[0].kwargs and "command" not in docker.created[1].kwargs
    assert all(c.removed for c in docker.created)
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "passed" and "retried with the image default command" in result["detail"]


@pytest.mark.asyncio
async def test_host_path_binds_data_and_dump_dirs(tmp_path, monkeypatch):
    cfg = await _make_cfg(tmp_path)
    monkeypatch.setattr(rd, "host_path_for", lambda path, client: "/mnt/user/appdata/arkive/restores")
    monkeypatch.setattr(rd.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(rd.os, "getegid", lambda: 100)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    engine = _engine(cfg, backup, docker)
    summary = await engine.run_drill()
    assert summary["status"] == "passed"
    kw = docker.created[0].kwargs
    binds = {spec["bind"]: (host, spec["mode"]) for host, spec in kw["volumes"].items()}
    data_host, data_mode = binds["/var/lib/postgresql/data"]
    dump_host, dump_mode = binds["/arkive-drill"]
    drill_id = kw["labels"]["arkive.drill"]
    assert data_host.startswith(f"/mnt/user/appdata/arkive/restores/drill-{drill_id}/")
    assert data_host.endswith("/db-0/data") and data_mode == "rw"
    assert dump_host.endswith("/db-0/dump") and dump_mode == "ro"
    assert kw["user"] == "1000:100"
    assert docker.created[0].archives == []  # bind mount instead of put_archive


@pytest.mark.asyncio
async def test_docker_storage_volume_path_counts_as_unresolved(tmp_path, monkeypatch):
    cfg = await _make_cfg(tmp_path)
    monkeypatch.setattr(rd, "host_path_for", lambda path, client: "/var/lib/docker/volumes/abc/_data/restores")
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    engine = _engine(cfg, backup, docker)
    await engine.run_drill()
    assert "volumes" not in docker.created[0].kwargs and docker.created[0].archives


@pytest.mark.asyncio
async def test_large_dump_without_host_path_is_skipped(tmp_path, monkeypatch):
    cfg = await _make_cfg(tmp_path)
    monkeypatch.setattr(rd, "NO_HOST_PATH_MAX_BYTES", 100)  # PG_DUMP is larger than this
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    docker = _docker_with_source()
    engine = _engine(cfg, backup, docker)
    summary = await engine.run_drill()
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "skipped"
    assert "could not be resolved" in result["detail"]
    assert docker.created == []
    assert summary["status"] == "passed"  # the SQLite dump still passed


@pytest.mark.asyncio
async def test_mariadb_drill(tmp_path):
    cfg = await _make_cfg(tmp_path)
    name = "nextcloud-db_nextcloud_20261022_030000.sql.gz"
    files = {name: gzip.compress(MYSQL_DUMP),
             DUMP_MANIFEST_NAME: _manifest([_entry(name, "nextcloud-db", "mariadb", "nextcloud",
                                                   image="mariadb:11")])}
    backup = FakeBackupEngine(cfg.dump_dir, files)
    docker = FakeDocker()
    docker.table_count = 1
    engine = _engine(cfg, backup, docker)
    summary = await engine.run_drill()
    assert summary["status"] == "passed"
    kw = docker.created[0].kwargs
    assert kw["environment"]["MARIADB_ROOT_PASSWORD"] == kw["environment"]["MYSQL_ROOT_PASSWORD"]
    assert "command" not in kw  # source container gone: image defaults
    load = [c for c in docker.created[0].execs if c[0][:2] == ["sh", "-c"]][0]
    assert load[1]["ARKIVE_DRILL_CLIENT"] == "mariadb"
    members = docker.created[0].archives[0][1]
    assert members["arkive-drill/dump.sql"] == MYSQL_DUMP  # gunzipped before the copy


@pytest.mark.asyncio
async def test_manifestless_snapshot_uses_history(tmp_path):
    cfg = await _make_cfg(tmp_path)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("INSERT INTO backup_jobs (id, name, type, schedule) VALUES ('j1', 'DB', 'db_dump', '0 3 * * *')")
        await db.execute("INSERT INTO job_runs (id, job_id, status, trigger, started_at) "
                         "VALUES ('r1', 'j1', 'success', 'scheduled', '2026-10-22T03:00:00Z')")
        await db.execute("INSERT INTO job_run_databases (run_id, container_name, db_type, db_name, status) "
                         "VALUES ('r1', 'immich_postgres', 'postgres', 'appdb', 'success')")
        await db.execute("INSERT INTO discovered_containers (name, image) VALUES ('immich_postgres', 'postgres:16')")
        await db.commit()
    files = {PG_FILE: PG_DUMP}
    backup = FakeBackupEngine(cfg.dump_dir, files)
    docker = FakeDocker()  # source container gone; image from discovered_containers
    engine = _engine(cfg, backup, docker)
    summary = await engine.run_drill()
    assert summary["status"] == "passed"
    assert docker.created[0].kwargs["image"] == "postgres:16"
    # Only the dump itself was restored: no manifest restore attempted.
    assert all(DUMP_MANIFEST_NAME not in p for paths, _ in backup.restore_calls for p in paths)


@pytest.mark.asyncio
async def test_prefers_local_target_and_skips_without_dump_snapshot(tmp_path):
    cfg = await _make_cfg(tmp_path, targets=(("cloud", "B2", "b2"), ("t1", "Local NAS", "local")))
    backup = FakeBackupEngine(cfg.dump_dir, {}, snapshots=[{"id": "c" * 64, "paths": ["/mnt/user/media"]}])
    seen = []
    original = backup.list_snapshots

    async def list_snapshots(target):
        seen.append(target["id"])
        return await original(target)

    backup.list_snapshots = list_snapshots
    engine = _engine(cfg, backup, FakeDocker())
    summary = await engine.run_drill()
    assert seen == ["t1", "cloud"]
    assert summary["status"] == "skipped"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert drill["status"] == "skipped" and drill["error_message"] == "no snapshot contains database dumps"


@pytest.mark.asyncio
async def test_repository_restore_failure_fails_database(tmp_path):
    cfg = await _make_cfg(tmp_path)
    files = _standard_files(tmp_path)
    del files[DUMP_MANIFEST_NAME]
    files = {SQLITE_FILE: files[SQLITE_FILE]}
    backup = FakeBackupEngine(cfg.dump_dir, files)
    backup.restore_status = "failed"
    engine = _engine(cfg, backup, FakeDocker())
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results"))[0]
    assert "restore from the repository failed" in result["detail"]


@pytest.mark.asyncio
async def test_waits_for_running_backup_then_skips(tmp_path):
    cfg = await _make_cfg(tmp_path)
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, {}), FakeDocker())
    engine.lock_manager.is_backup_running.return_value = True
    engine.idle_wait_seconds = 0
    summary = await engine.run_drill()
    assert summary["status"] == "skipped"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert "still running" in drill["error_message"]


@pytest.mark.asyncio
async def test_rotation_across_drills(tmp_path):
    cfg = await _make_cfg(tmp_path)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('restore_drill_max_databases', '1')")
        await db.commit()
    files = {}
    entries = []
    for name in ("a", "b"):
        fname = f"{name}_app.db_20261022_030000.sqlite3"
        files[fname] = _sqlite_bytes(tmp_path, f"{name}.db")
        entries.append(_entry(fname, name, "sqlite", "app.db"))
    files[DUMP_MANIFEST_NAME] = _manifest(entries)
    backup = FakeBackupEngine(cfg.dump_dir, files)
    engine = _engine(cfg, backup, FakeDocker())
    await engine.run_drill()
    await engine.run_drill()
    rows = await _rows(cfg, "SELECT r.container_name FROM restore_drill_results r "
                            "JOIN restore_drills d ON d.id = r.drill_id ORDER BY d.started_at, r.id")
    assert [r["container_name"] for r in rows] == ["a", "b"]


@pytest.mark.asyncio
async def test_start_drill_runs_in_background_and_rejects_second(tmp_path):
    cfg = await _make_cfg(tmp_path)
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, {}), FakeDocker())
    drill_id = await engine.start_drill(trigger="manual")
    assert engine.is_running() and engine.running_drill_id == drill_id
    with pytest.raises(DrillAlreadyRunningError):
        await engine.start_drill()
    assert (await engine.run_drill())["reason"] == "already_running"
    await engine._task
    assert not engine.is_running()
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert drill["trigger"] == "manual" and drill["status"] == "skipped"


# ---------------------------------------------------------------------------
# Postgres database names
# ---------------------------------------------------------------------------


class TestPostgresNames:
    @pytest.mark.parametrize("arg, expected", [
        ("appdb", "appdb"),
        ('"MixedCase"', "MixedCase"),
        ('"we""ird"', 'we"ird'),
        ('-reuse-previous=on "dbname=\'my db\'"', "my db"),
        ('-reuse-previous=on "dbname=\'café-db\'"', "café-db"),
        ('-reuse-previous=on "dbname=\'a\\\'b\'"', "a'b"),
        ('-reuse-previous=on "dbname=\'say ""hi""\'"', 'say "hi"'),
        ('-reuse-previous=on "dbname=\'it\'\'s\'"', "it's"),
        ('-reuse-previous=on "dbname=\'unterminated', None),
    ])
    def test_parse_connect(self, arg, expected):
        assert parse_pg_connect(arg) == expected

    def test_name_rule_matches_dumper(self):
        for ok in ("appdb", "my db", "café", "Immich_DB", "a.b-c"):
            assert valid_pg_dbname(ok), ok
        for bad in ("", "-x", "a=b", "postgres://h/db", "a\nb", "a\x00b", "tab\there", None):
            assert not valid_pg_dbname(bad), bad

    def test_prepare_reads_quoted_connect(self, tmp_path):
        dump = PG_DUMP.replace(b"\\connect appdb",
                               b"\\encoding SQL_ASCII\n\\connect -reuse-previous=on \"dbname='my db'\"")
        src = tmp_path / "in.sql"
        src.write_bytes(dump)
        assert prepare_sql_dump(src, tmp_path / "out.sql", "postgres").database == "my db"


@pytest.mark.asyncio
async def test_database_name_with_spaces_is_counted_in_that_database(tmp_path):
    cfg = await _make_cfg(tmp_path)
    dump = PG_DUMP.replace(b"\\connect appdb",
                           b"\\encoding SQL_ASCII\n\\connect -reuse-previous=on \"dbname='my db'\"")
    name = "pg_my_db_20261022_030000.sql"
    files = {name: dump, DUMP_MANIFEST_NAME: _manifest([_entry(name, "immich_postgres", "postgres", "my db",
                                                               image="postgres:16")])}
    docker = _docker_with_source()
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, files), docker)
    summary = await engine.run_drill()
    assert summary["status"] == "passed"
    count = [c for c in docker.created[0].execs if c[0][0] == "psql"][0][0]
    assert count[count.index("-d") + 1] == "my db"


# ---------------------------------------------------------------------------
# Repository errors are failures, not "nothing to test"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_entry_missing_from_snapshot_fails_drill(tmp_path):
    cfg = await _make_cfg(tmp_path)
    files = _standard_files(tmp_path)
    manifest = json.loads(files[DUMP_MANIFEST_NAME])
    manifest["dumps"].append(_entry("gone_db_20261022_030000.sql", "gone", "postgres", "db", image="postgres:16"))
    files[DUMP_MANIFEST_NAME] = json.dumps(manifest).encode()
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, files), _docker_with_source(), notifier)
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE container_name = 'gone'"))[0]
    assert result["status"] == "failed"
    assert result["detail"] == "listed in the snapshot's dump manifest but missing from the snapshot"
    notifier.send.assert_awaited_once()
    assert "gone/db" in notifier.send.await_args.args[2]


@pytest.mark.asyncio
async def test_unlistable_repository_fails_drill(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    backup.list_error["t1"] = "Fatal: wrong password or no key found"
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, backup, _docker_with_source(), notifier)
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert drill["status"] == "failed"
    assert "could not list snapshots" in drill["error_message"]
    assert "wrong password or no key found" in drill["error_message"]
    notifier.send.assert_awaited_once()
    assert notifier.send.await_args.args[0] == "drill.failed"


@pytest.mark.asyncio
async def test_listing_error_on_one_target_uses_the_next(tmp_path):
    cfg = await _make_cfg(tmp_path, targets=(("t1", "Local NAS", "local"), ("cloud", "B2", "b2")))
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    backup.list_error["t1"] = "repository is already locked exclusively"
    engine = _engine(cfg, backup, _docker_with_source())
    summary = await engine.run_drill()
    assert summary["status"] == "passed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert drill["target_id"] == "cloud"


@pytest.mark.asyncio
async def test_listing_errors_without_dump_snapshot_elsewhere_fail(tmp_path):
    cfg = await _make_cfg(tmp_path, targets=(("t1", "Local NAS", "local"), ("cloud", "B2", "b2")))
    backup = FakeBackupEngine(cfg.dump_dir, {}, snapshots=[{"id": "c" * 64, "paths": ["/mnt/user/media"]}])
    backup.list_error["t1"] = "unable to open config file"
    engine = _engine(cfg, backup, FakeDocker())
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert "Local NAS: unable to open config file" in drill["error_message"]


@pytest.mark.asyncio
async def test_ls_failure_fails_drill(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    backup.ls_fails = True
    engine = _engine(cfg, backup, _docker_with_source())
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert "restic ls failed" in drill["error_message"]
    assert (drill["target_id"], drill["snapshot_id"]) == ("t1", "aaaaaaaa")


@pytest.mark.asyncio
async def test_ls_failure_fails_drill_when_snapshot_covers_a_parent(tmp_path):
    """The snapshot covers /config (a parent of the dump dir); a failed listing
    must still fail the drill rather than read as 'no dumps'."""
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    backup.ls_fails = True
    original = backup.list_snapshots

    async def parent_snapshots(target):
        snaps = await original(target)
        for snap in snaps:
            snap["paths"] = [str(cfg.config_dir)]
        return snaps

    backup.list_snapshots = parent_snapshots
    engine = _engine(cfg, backup, _docker_with_source())
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert "restic ls failed" in drill["error_message"]


@pytest.mark.asyncio
async def test_manifest_restore_failure_fails_drill(tmp_path):
    cfg = await _make_cfg(tmp_path)
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    backup.restore_fail_for = {DUMP_MANIFEST_NAME}
    engine = _engine(cfg, backup, _docker_with_source())
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert "restoring the dump manifest" in drill["error_message"]


@pytest.mark.asyncio
async def test_corrupt_manifest_fails_drill(tmp_path):
    cfg = await _make_cfg(tmp_path)
    files = _standard_files(tmp_path)
    files[DUMP_MANIFEST_NAME] = b'{"version": 1, "dumps": [ {"file": '
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, files), _docker_with_source())
    summary = await engine.run_drill()
    assert summary["status"] == "failed"
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert "unreadable" in drill["error_message"]


# ---------------------------------------------------------------------------
# A server that never becomes ready means "not tested", not "broken dump"
# ---------------------------------------------------------------------------


def _pg_only_files():
    return {PG_FILE: PG_DUMP,
            DUMP_MANIFEST_NAME: _manifest([_entry(PG_FILE, "immich_postgres", "postgres", "appdb",
                                                  image="postgres:16")])}


async def _assert_not_tested(cfg, summary, notifier, needle):
    assert summary["status"] == "skipped"
    result = (await _rows(cfg, "SELECT * FROM restore_drill_results WHERE dump_file = ?", (PG_FILE,)))[0]
    assert result["status"] == "skipped" and needle in result["detail"], result["detail"]
    drill = (await _rows(cfg, "SELECT * FROM restore_drills"))[0]
    assert drill["status"] == "skipped"
    assert "no database could be tested" in drill["error_message"]
    assert "immich_postgres/appdb" in drill["error_message"] and needle in drill["error_message"]
    notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_that_keeps_exiting_is_skipped(tmp_path):
    cfg = await _make_cfg(tmp_path)
    docker = _docker_with_source()
    docker.start_status = lambda c: "exited"
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, _pg_only_files()), docker, notifier)
    summary = await engine.run_drill()
    await _assert_not_tested(cfg, summary, notifier, "did not start")
    assert len(docker.created) == 2 and all(c.removed for c in docker.created)


@pytest.mark.asyncio
async def test_readiness_timeout_is_skipped(tmp_path):
    cfg = await _make_cfg(tmp_path)
    docker = FakeDocker()  # source gone -> one attempt with the image defaults
    original = docker.behavior
    docker.behavior = lambda c, cmd, env: (2, b"no response") if cmd[0] == "pg_isready" else original(c, cmd, env)
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, _pg_only_files()), docker, notifier)
    engine.ready_timeout_seconds = 0
    summary = await engine.run_drill()
    await _assert_not_tested(cfg, summary, notifier, "not ready after")
    assert docker.created[0].removed


@pytest.mark.asyncio
async def test_container_create_error_is_skipped(tmp_path):
    cfg = await _make_cfg(tmp_path)
    docker = _docker_with_source()

    def refuse(**kwargs):
        raise RuntimeError("409 Conflict: invalid mount config")

    docker.containers.create = refuse
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, _pg_only_files()), docker, notifier)
    summary = await engine.run_drill()
    await _assert_not_tested(cfg, summary, notifier, "invalid mount config")


@pytest.mark.asyncio
async def test_unavailable_image_is_skipped(tmp_path):
    cfg = await _make_cfg(tmp_path)
    docker = _docker_with_source()
    del docker.image_configs["postgres:16"]
    notifier = MagicMock()
    notifier.send = AsyncMock()
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, _pg_only_files()), docker, notifier)
    summary = await engine.run_drill()
    await _assert_not_tested(cfg, summary, notifier, "is not available")
    assert docker.created == []


@pytest.mark.asyncio
async def test_drill_dir_has_ownership_marker_while_running(tmp_path):
    cfg = await _make_cfg(tmp_path)
    seen = {}
    backup = FakeBackupEngine(cfg.dump_dir, _standard_files(tmp_path))
    original = backup.restore

    async def restore(**kwargs):
        drill_dir = Path(kwargs["restore_to"]).parents[1]
        if drill_dir.name.startswith("drill-"):
            seen[drill_dir.name] = (drill_dir / DRILL_MARKER).read_text()
        return await original(**kwargs)

    backup.restore = restore
    engine = _engine(cfg, backup, _docker_with_source())
    summary = await engine.run_drill()
    assert seen == {f"drill-{summary['drill_id']}": summary["drill_id"]}
    assert list(cfg.restore_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Startup sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_after_restart_and_container_sweep(tmp_path):
    cfg = await _make_cfg(tmp_path)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("INSERT INTO restore_drills (id, status) VALUES ('old', 'running')")
        await db.execute("INSERT INTO restore_drills (id, status) VALUES ('done', 'passed')")
        await db.commit()
    old_id = "0" * 31 + "1"
    stale = cfg.restore_dir / f"drill-{old_id}" / "db-0" / "data"
    stale.mkdir(parents=True)
    (stale / "PG_VERSION").write_text("16")
    (cfg.restore_dir / f"drill-{old_id}" / DRILL_MARKER).write_text(old_id)
    # Not ours: user restores, wrong name format, no marker, mismatching marker.
    kept = []
    for name, marker in (("restore-123", None), ("drill-recovery", "drill-recovery"),
                         ("drill-" + "a" * 32, None), ("drill-" + "b" * 32, "c" * 32)):
        d = cfg.restore_dir / name
        (d / "data").mkdir(parents=True)
        (d / "data" / "important.txt").write_text("user data")
        if marker:
            (d / DRILL_MARKER).write_text(marker)
        kept.append(d)
    docker = FakeDocker()
    leftover = FakeContainer(docker, {"name": "arkive-drill-old-0-0", "labels": {"arkive.drill": "old"}})
    docker.leftovers = [leftover]
    engine = _engine(cfg, FakeBackupEngine(cfg.dump_dir, {}), docker)

    await engine.recover_after_restart()
    removed = await engine.remove_leftover_containers()

    rows = {r["id"]: r for r in await _rows(cfg, "SELECT * FROM restore_drills")}
    assert rows["old"]["status"] == "failed" and rows["old"]["error_message"] == "Interrupted by restart"
    assert rows["done"]["status"] == "passed"
    assert not (cfg.restore_dir / f"drill-{old_id}").exists()
    for d in kept:  # user restores are left alone
        assert (d / "data" / "important.txt").read_text() == "user data", d
    assert removed == 1 and leftover.removed == {"force": True, "v": True}


@pytest.mark.asyncio
async def test_container_sweep_without_docker_is_noop(tmp_path):
    cfg = await _make_cfg(tmp_path)
    engine = _engine(cfg, None, None)
    assert await engine.remove_leftover_containers() == 0


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_registers_and_reloads_drill_job(tmp_path):
    from app.services.scheduler import SYSTEM_JOB_RESTORE_DRILL, ArkiveScheduler

    cfg = await _make_cfg(tmp_path)
    drill = MagicMock()
    drill.run_drill = AsyncMock(return_value={"status": "passed"})
    sched = ArkiveScheduler(MagicMock(), cfg, restore_drill=drill)

    await sched.reload_restore_drill_job()
    job = sched.scheduler.get_job(SYSTEM_JOB_RESTORE_DRILL)
    assert job is not None
    assert "day_of_week='0'" in str(job.trigger) and "hour='7'" in str(job.trigger)

    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('restore_drill_schedule', '30 2 * * 3')")
        await db.commit()
    await sched.reload_restore_drill_job()
    job = sched.scheduler.get_job(SYSTEM_JOB_RESTORE_DRILL)
    assert "hour='2'" in str(job.trigger) and "minute='30'" in str(job.trigger)

    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('restore_drill_enabled', 'false')")
        await db.commit()
    await sched.reload_restore_drill_job()
    assert sched.scheduler.get_job(SYSTEM_JOB_RESTORE_DRILL) is None

    await sched._run_restore_drill()
    drill.run_drill.assert_awaited_once_with(trigger="scheduled")


@pytest.mark.asyncio
async def test_scheduler_without_engine_registers_nothing(tmp_path):
    from app.services.scheduler import SYSTEM_JOB_RESTORE_DRILL, ArkiveScheduler

    cfg = await _make_cfg(tmp_path)
    sched = ArkiveScheduler(MagicMock(), cfg)
    await sched.reload_restore_drill_job()
    assert sched.scheduler.get_job(SYSTEM_JOB_RESTORE_DRILL) is None
    await sched._run_restore_drill()  # no engine: no-op, no exception


@pytest.mark.asyncio
async def test_scheduler_job_swallows_engine_errors(tmp_path):
    from app.services.scheduler import ArkiveScheduler

    cfg = await _make_cfg(tmp_path)
    drill = MagicMock()
    drill.run_drill = AsyncMock(side_effect=RuntimeError("kaboom"))
    sched = ArkiveScheduler(MagicMock(), cfg, restore_drill=drill)
    await sched._run_restore_drill()
