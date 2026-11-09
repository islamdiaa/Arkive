"""Guided in-place database restore (services/guided_restore.py).

The engine runs end to end against fakes that behave like the real thing where
Arkive can observe it: a backup engine that "restores" realistic dump files
from a snapshot, and database containers (tests/fakes/fake_db_server.py) that
stream dumps with completion trailers, receive loads over a real socket with
Docker's stream framing, and keep the rows a restore leaves behind.
"""

import gzip
import json
import os
import sqlite3
import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.core.config import ArkiveConfig
from app.core.database import init_db, run_migrations
from app.models.guided_restore import GuidedRestoreStart, GuidedRestoreUndo
from app.services.backup_engine import ResticError
from app.services.db_dumper import DUMP_MANIFEST_NAME, DumpResult, dump_prefix, legacy_dump_prefix
from app.services.guided_restore import (
    WORK_MARKER,
    DatabaseRef,
    GuidedRestoreEngine,
    GuidedRestoreError,
    exec_with_stdin,
    mysql_dump_databases,
    mysql_prelude,
    pg_ident,
    pg_literal,
    postgres_owner_sql,
    postgres_prelude,
    restore_method,
    sqlite_instructions,
    validate_ref,
)
from app.services.lock_manager import BackupLockManager
from tests.fakes.fake_db_server import (
    FakeAppContainer,
    FakeDockerForRestore,
    FakeMariaDB,
    FakePostgres,
    FakeSnapshotBackupEngine,
    mysql_dump_text,
    pg_dump_text,
)

SNAPSHOT_ID = "abcd1234"
FULL_ID = SNAPSHOT_ID + "0" * 56
TS = "20261101_100000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def pg_file(ext="sql"):
    return f"{dump_prefix('pg', 'postgres', 'appdb')}_{TS}.{ext}"


def maria_file():
    return f"{dump_prefix('mariadb', 'mariadb', 'appdb')}_{TS}.sql"


def manifest(*entries) -> bytes:
    return json.dumps({"version": 1, "dumps": list(entries)}).encode()


def entry(file, container="pg", db_type="postgres", db_name="appdb", host_path=None, kind="database"):
    return {
        "file": file,
        "container_name": container,
        "db_type": db_type,
        "db_name": db_name,
        "host_path": host_path,
        "kind": kind,
        "created_at": "2026-11-01T10:00:00Z",
    }


@pytest_asyncio.fixture
async def config(tmp_path):
    cfg = ArkiveConfig(config_dir=tmp_path)
    cfg.ensure_dirs()
    await init_db(cfg.db_path)
    await run_migrations(cfg.db_path)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, config) VALUES ('t1', 'Local NAS', 'local', '{}')"
        )
        await db.execute(
            "INSERT INTO snapshots (id, target_id, full_id, time, paths) VALUES (?, 't1', ?, ?, ?)",
            (SNAPSHOT_ID, FULL_ID, "2026-11-01T10:05:00Z", json.dumps([str(cfg.dump_dir), "/mnt/user/appdata"])),
        )
        containers = [
            ("pg", "running", [{"container_name": "pg", "db_type": "postgres", "db_name": "appdb"}], "app"),
            ("app-server", "running", [{"container_name": "pg", "db_type": "postgres", "db_name": "appdb"}], "app"),
            ("unrelated", "running", [], None),
            ("mariadb", "running", [{"container_name": "mariadb", "db_type": "mariadb", "db_name": "appdb"}], None),
            ("cache", "running", [{"container_name": "cache", "db_type": "redis", "db_name": "redis"}], None),
            (
                "vaultwarden",
                "running",
                [
                    {
                        "container_name": "vaultwarden",
                        "db_type": "sqlite",
                        "db_name": "db.sqlite3",
                        "host_path": "/mnt/user/appdata/vault warden/db.sqlite3",
                    }
                ],
                None,
            ),
        ]
        for name, status, dbs, project in containers:
            await db.execute(
                "INSERT INTO discovered_containers (name, image, status, databases, compose_project) "
                "VALUES (?, 'img', ?, ?, ?)",
                (name, status, json.dumps(dbs), project),
            )
        await db.commit()
    return cfg


def make_engine(config, files, *containers, **kwargs):
    engine = GuidedRestoreEngine(
        config,
        backup_engine=FakeSnapshotBackupEngine(config.dump_dir, files, FULL_ID),
        docker_client=FakeDockerForRestore(*containers),
        **kwargs,
    )
    engine.min_free_bytes = 0
    engine.progress_interval = 0.05
    return engine


def body(container="pg", db_type="postgres", db_name="appdb", confirm="appdb", stop=(), host_path=None):
    return GuidedRestoreStart(
        container_name=container,
        db_type=db_type,
        db_name=db_name,
        host_path=host_path,
        snapshot_id=SNAPSHOT_ID,
        target_id="t1",
        confirm=confirm,
        stop_containers=list(stop),
    )


async def run_to_end(engine, coro) -> dict:
    started = await coro
    assert started["status"] == "running"
    await engine._task
    return await engine.get(started["id"])


def postgres_setup(config, rows=("current-1",), snapshot_rows=("alice", "bob"), **pg_kwargs):
    pg = FakePostgres(databases={"appdb": {"rows": list(rows)}}, **pg_kwargs)
    app = FakeAppContainer("app-server")
    files = {
        pg_file(): pg_dump_text("appdb", list(snapshot_rows)),
        DUMP_MANIFEST_NAME: manifest(entry(pg_file())),
    }
    return pg, app, files


def step_status(op) -> dict:
    return {s["key"]: s["status"] for s in op["steps"]}


async def activity(config) -> list[tuple]:
    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute("SELECT action, severity, message FROM activity_log ORDER BY id")
        return await cursor.fetchall()


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


async def test_postgres_restore_end_to_end(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body(stop=["app-server"])))

    assert op["status"] == "completed", op
    assert set(step_status(op).values()) == {"done"}
    # The snapshot's data replaced the current data.
    assert pg.databases["appdb"]["rows"] == ["alice", "bob"]
    assert pg.databases["appdb"]["allowconn"] is True
    # The app was stopped for the load and started again.
    assert [c["cmd"][0] for c in app.calls] == ["<stop>", "<start>"]
    assert app.status == "running"

    # One load over stdin: fixed argv (maintenance db, no user input), password in env.
    loads = [e for e in pg.client.api.execs.values() if e["stdin"]]
    assert len(loads) == 1
    assert loads[0]["cmd"] == ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres"]
    assert loads[0]["env"]["PGPASSWORD"] == "pg-secret"
    assert all("pg-secret" not in " ".join(c["cmd"]) for c in pg.calls)
    load_call = next(c for c in pg.calls if c["stdin"])
    assert load_call["stdin"].startswith(
        b'\\set ON_ERROR_STOP on\nALTER DATABASE "appdb" WITH ALLOW_CONNECTIONS false;'
    )
    assert b"pg_terminate_backend" in load_call["stdin"]
    assert load_call["stdin"].endswith(b"PostgreSQL database dump complete\n--\n\n")

    # The safety dump holds the data as it was, and stays for undo.
    assert op["safety_dump"]["available"] is True
    safety = Path(op["safety_dump"]["path"])
    assert b"1\tcurrent-1" in safety.read_bytes()
    assert safety.parent.parent.name == f"guided-{op['id']}"
    assert (safety.parent.parent / WORK_MARKER).read_text() == op["id"]
    # The restored copy itself is not kept (it's in the repository).
    assert not (safety.parent.parent / "load.sql").exists()
    assert not (safety.parent.parent / "snapshot").exists()

    assert op["database_changed"] is True and op["can_undo"] is True
    assert op["dump_file"] == pg_file() and op["snapshot_id"] == SNAPSHOT_ID
    assert op["target_name"] == "Local NAS"
    assert "2 tables in appdb" in next(s["detail"] for s in op["steps"] if s["key"] == "verify")
    # Lock released; activity recorded.
    assert not BackupLockManager(config.config_dir).is_backup_running()
    log = await activity(config)
    assert [a[0] for a in log] == ["database_restore_started", "database_restore_completed"]
    assert log[-1][1] == "info"


async def test_undo_loads_the_safety_dump_back(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)
    first = await run_to_end(engine, engine.start(body(stop=["app-server"])))
    assert pg.databases["appdb"]["rows"] == ["alice", "bob"]

    with pytest.raises(GuidedRestoreError) as exc:
        await engine.start_undo(first["id"], GuidedRestoreUndo(confirm="nope"))
    assert exc.value.status_code == 400

    undo = await run_to_end(engine, engine.start_undo(first["id"], GuidedRestoreUndo(confirm="appdb")))

    assert undo["status"] == "completed", undo
    assert undo["kind"] == "undo" and undo["parent_id"] == first["id"]
    assert pg.databases["appdb"]["rows"] == ["current-1"]
    # Same apps stopped and started again by default.
    assert [c["cmd"][0] for c in app.calls] == ["<stop>", "<start>", "<stop>", "<start>"]
    # The undo kept its own safety dump (the restored state), and the original's is untouched.
    assert b"1\talice" in Path(undo["safety_dump"]["path"]).read_bytes()
    assert Path(first["safety_dump"]["path"]).is_file()
    assert (await engine.get(first["id"]))["can_undo"] is True


async def test_confirmation_must_match_database_name(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)
    for wrong in ("", "APPDB", "appdb ", "pg/appdb"):
        with pytest.raises(GuidedRestoreError) as exc:
            await engine.start(body(confirm=wrong))
        assert exc.value.status_code == 400
    assert engine._task is None
    assert not BackupLockManager(config.config_dir).is_backup_running()
    assert (await engine.history())["total"] == 0


async def test_refused_while_backup_lock_is_held(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)
    lock = BackupLockManager(config.config_dir)
    assert lock.acquire_backup_lock("job-run")
    try:
        with pytest.raises(GuidedRestoreError) as exc:
            await engine.start(body())
        assert exc.value.status_code == 409
        assert "backup" in str(exc.value).lower()
    finally:
        lock.release_backup_lock()
    assert (await engine.history())["total"] == 0
    assert not list(config.restore_dir.glob("guided-*"))


async def test_backup_lock_held_for_the_whole_run(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)
    seen = []
    original = engine._step_load

    async def spy(op, driver, prepared):
        seen.append(BackupLockManager(config.config_dir).is_backup_running())
        return await original(op, driver, prepared)

    engine._step_load = spy
    await run_to_end(engine, engine.start(body()))
    assert seen == [True]
    assert not BackupLockManager(config.config_dir).is_backup_running()


async def test_drop_refused_leaves_database_unchanged_and_restarts_apps(config):
    pg, app, files = postgres_setup(config)
    pg.fail_drop = True
    original_oid = pg.databases["appdb"]["oid"]
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body(stop=["app-server"])))

    assert op["status"] == "failed"
    assert "being accessed by other users" in op["error_message"]
    assert "appdb was not changed" in op["error_message"]
    assert op["database_changed"] is False and op["can_undo"] is False
    assert pg.databases["appdb"]["oid"] == original_oid
    assert pg.databases["appdb"]["rows"] == ["current-1"]
    # Connections are allowed again and the app is back up.
    assert pg.databases["appdb"]["allowconn"] is True
    assert app.status == "running"
    statuses = step_status(op)
    assert statuses["load"] == "failed" and statuses["verify"] == "skipped" and statuses["start_apps"] == "done"
    assert not BackupLockManager(config.config_dir).is_backup_running()
    assert (await activity(config))[-1][1] == "error"


async def test_failure_after_drop_keeps_apps_stopped_and_offers_undo(config):
    pg, app, files = postgres_setup(config)
    pg.fail_after_drop = True
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body(stop=["app-server"])))

    assert op["status"] == "failed"
    assert 'extension "vectors" is not available' in op["error_message"]
    assert "may be partially restored" in op["error_message"] and "Undo" in op["error_message"]
    assert "Apps left stopped: app-server" in op["error_message"]
    assert op["database_changed"] is True and op["can_undo"] is True
    assert app.status == "exited"
    start = next(s for s in op["steps"] if s["key"] == "start_apps")
    assert start["status"] == "skipped" and "left stopped" in start["detail"]

    # Undo puts the old data back and starts the app again.
    pg.fail_after_drop = False
    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="appdb")))
    assert undo["status"] == "completed", undo
    assert pg.databases["appdb"]["rows"] == ["current-1"]
    assert app.status == "running"


async def test_undo_runs_even_if_the_broken_database_cannot_be_dumped(config):
    pg, app, files = postgres_setup(config)
    pg.fail_after_drop = True
    engine = make_engine(config, files, pg, app)
    op = await run_to_end(engine, engine.start(body()))
    assert op["status"] == "failed"

    pg.fail_after_drop = False
    pg.dump_truncated = True  # the half-restored database can't be dumped
    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="appdb")))

    assert undo["status"] == "completed_with_warnings", undo
    assert "No copy of the database as it was before the undo" in undo["warnings"][0]
    assert step_status(undo)["safety_dump"] == "failed" and step_status(undo)["load"] == "done"
    assert pg.databases["appdb"]["rows"] == ["current-1"]


async def test_undo_after_failed_load_gives_ownership_back(config):
    pg, app, files = postgres_setup(config)
    pg.databases["appdb"]["owner"] = "app"
    pg.fail_after_drop = True  # leaves appdb re-created and owned by the connecting role
    engine = make_engine(config, files, pg, app)
    op = await run_to_end(engine, engine.start(body()))
    assert op["status"] == "failed" and pg.databases["appdb"]["owner"] == "postgres"

    pg.fail_after_drop = False
    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="appdb")))

    assert undo["status"] == "completed", undo
    assert pg.databases["appdb"]["owner"] == "app"
    assert pg.owner_fix_sql[-1].startswith('ALTER DATABASE "appdb" OWNER TO "app";')


async def test_messages_keep_container_names_intact(config):
    # The credential redactor's "-p<value>" rule must not turn "immich-postgres" into "immich-p[REDACTED]".
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO discovered_containers (name, image, status, databases) VALUES (?, 'img', 'running', ?)",
            (
                "immich-postgres",
                json.dumps([{"container_name": "immich-postgres", "db_type": "postgres", "db_name": "immich"}]),
            ),
        )
        await db.commit()
    pg = FakePostgres(name="immich-postgres", databases={"immich": {"rows": ["a"]}})
    name = f"{dump_prefix('immich-postgres', 'postgres', 'immich')}_{TS}.sql"
    engine = make_engine(config, {name: pg_dump_text("immich", ["b"])}, pg)
    pg.fail_drop = True

    op = await run_to_end(engine, engine.start(body(container="immich-postgres", db_name="immich", confirm="immich")))

    assert "[REDACTED]" not in json.dumps(op)
    assert op["dump_file"] == name
    assert name in next(s["detail"] for s in op["steps"] if s["key"] == "locate_dump")


async def test_safety_dump_failure_stops_before_anything_changes(config):
    pg, app, files = postgres_setup(config)
    pg.dump_truncated = True
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body(stop=["app-server"])))

    assert op["status"] == "failed"
    assert step_status(op)["safety_dump"] == "failed"
    assert "nothing was changed" in op["error_message"]
    assert not [e for e in pg.client.api.execs.values() if e["stdin"]]  # never loaded
    assert app.calls == []  # never stopped
    assert pg.databases["appdb"]["rows"] == ["current-1"]
    assert op["can_undo"] is False


async def test_original_owner_is_restored(config):
    pg, app, files = postgres_setup(config)
    pg.databases["appdb"]["owner"] = "app"
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "completed", op
    assert pg.databases["appdb"]["owner"] == "app"
    sql = pg.owner_fix_sql[0]
    assert sql.startswith('ALTER DATABASE "appdb" OWNER TO "app";')
    assert "prokind" in sql  # PostgreSQL 16: procedures and aggregates handled by kind
    # The ownership fix ran inside the restored database, not the maintenance one.
    fix_call = next(c for c in pg.calls if c["cmd"][0] == "psql" and "OWNER TO" in " ".join(c["cmd"]))
    assert fix_call["cmd"][fix_call["cmd"].index("-d") + 1] == "appdb"


async def test_owner_fix_failure_is_a_warning(config):
    pg, app, files = postgres_setup(config)
    pg.databases["appdb"]["owner"] = "app"
    pg.fail_owner_fix = True
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "completed_with_warnings"
    assert "not app as before" in op["warnings"][0]
    assert pg.databases["appdb"]["rows"] == ["alice", "bob"]


async def test_role_without_privileges_is_refused_before_restoring(config):
    pg, app, files = postgres_setup(config, superuser=False)
    pg.databases["appdb"]["owner"] = "someone-else"
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert step_status(op)["check_container"] == "failed"
    assert "cannot drop and re-create" in op["error_message"]
    assert engine.backup_engine.restores == []


async def test_dump_of_another_database_is_refused(config):
    pg, app, _files = postgres_setup(config)
    files = {pg_file(): pg_dump_text("otherdb", ["x"]), DUMP_MANIFEST_NAME: manifest(entry(pg_file()))}
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert step_status(op)["verify_dump"] == "failed"
    assert "re-creates database 'otherdb'" in op["error_message"]
    assert pg.databases["appdb"]["rows"] == ["current-1"]


async def test_truncated_dump_in_snapshot_is_refused(config):
    pg, app, _files = postgres_setup(config)
    text = pg_dump_text("appdb", ["alice"])
    files = {pg_file(): text[: len(text) - 60], DUMP_MANIFEST_NAME: manifest(entry(pg_file()))}
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert "truncated" in op["error_message"]
    assert not [e for e in pg.client.api.execs.values() if e["stdin"]]


async def test_snapshot_without_this_database_fails_to_locate(config):
    pg, app, _files = postgres_setup(config)
    other = f"{dump_prefix('pg', 'postgres', 'other')}_{TS}.sql"
    files = {other: pg_dump_text("other", []), DUMP_MANIFEST_NAME: manifest(entry(other, db_name="other"))}
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert step_status(op)["locate_dump"] == "failed"
    assert "has no dump of pg/appdb" in op["error_message"]


async def test_snapshot_without_manifest_matches_file_names(config):
    pg, app, _files = postgres_setup(config)
    legacy = f"{legacy_dump_prefix('pg', 'appdb')}_20261001_100000.sql"
    files = {
        legacy: pg_dump_text("appdb", ["old"]),
        pg_file(): pg_dump_text("appdb", ["newest"]),
        f"{dump_prefix('pg', 'postgres', 'appdb_2')}_{TS}.sql": pg_dump_text("appdb_2", ["wrong"]),
    }
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "completed", op
    assert op["dump_file"] == pg_file()
    assert pg.databases["appdb"]["rows"] == ["newest"]


async def test_gzip_dump_is_decompressed_before_loading(config):
    pg, app, _files = postgres_setup(config)
    name = pg_file("sql.gz")
    files = {name: gzip.compress(pg_dump_text("appdb", ["zipped"])), DUMP_MANIFEST_NAME: manifest(entry(name))}
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "completed", op
    assert pg.databases["appdb"]["rows"] == ["zipped"]


async def test_stop_list_is_limited_to_apps_using_the_database(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app, FakeAppContainer("unrelated"))
    for name in ("unrelated", "pg", "arkive"):
        with pytest.raises(GuidedRestoreError) as exc:
            await engine.start(body(stop=[name]))
        assert exc.value.status_code == 400
    assert engine._task is None


async def test_container_not_running_fails_the_check(config):
    pg, app, files = postgres_setup(config)
    pg.status = "exited"
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert "pg is exited; start it first" in op["error_message"]


async def test_early_exit_of_the_client_is_reported(config):
    pg, app, files = postgres_setup(config, snapshot_rows=[f"row-{i}" for i in range(20000)])
    pg.read_limit = 4096  # psql "dies" after reading 4 KB
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert "psql exited with code 3" in op["error_message"]


async def test_client_exiting_zero_before_the_end_is_a_failure(config):
    class QuitsEarly(FakePostgres):
        def _load(self, script):
            return 0, b"", b""  # e.g. a stray \q: "success" without reading everything

    pg = QuitsEarly(databases={"appdb": {"rows": ["current-1"]}})
    pg.read_limit = 1024
    app = FakeAppContainer("app-server")
    files = {pg_file(): pg_dump_text("appdb", [f"row-{i}" for i in range(150000)])}
    engine = make_engine(config, files, pg, app)

    op = await run_to_end(engine, engine.start(body()))

    assert op["status"] == "failed"
    assert "stopped reading the dump before its end" in op["error_message"]
    assert op["can_undo"] is True


# ---------------------------------------------------------------------------
# MariaDB / MySQL
# ---------------------------------------------------------------------------


def mariadb_setup(client="mariadb"):
    maria = FakeMariaDB(databases={"appdb": ["current-1"]}, client=client)
    files = {
        maria_file(): mysql_dump_text("appdb", ["alice", "bob"]),
        DUMP_MANIFEST_NAME: manifest(entry(maria_file(), container="mariadb", db_type="mariadb")),
    }
    return maria, files


@pytest.mark.parametrize("client", ["mariadb", "mysql"])
async def test_mariadb_restore_end_to_end(config, client):
    maria, files = mariadb_setup(client)
    engine = make_engine(config, files, maria)

    op = await run_to_end(engine, engine.start(body(container="mariadb", db_type="mysql")))

    assert op["status"] == "completed", op
    assert op["db_type"] == "mariadb"
    assert maria.databases["appdb"]["rows"] == ["alice", "bob"]
    load = next(e for e in maria.client.api.execs.values() if e["stdin"])
    assert load["cmd"] == [client, "-u", "root"]
    assert load["env"] == {"MYSQL_PWD": "root-secret"}
    stdin = next(c for c in maria.calls if c["stdin"])["stdin"]
    assert stdin.startswith(
        b"SET FOREIGN_KEY_CHECKS = 0;\nSET SESSION group_concat_max_len = 1073741824;\nUSE `appdb`;"
    )
    assert b"PREPARE arkive_stmt FROM @arkive_sql;" in stdin
    assert b"-- Dump completed" in stdin
    assert b"1\tcurrent-1" not in stdin
    assert b"(1,'current-1')" in Path(op["safety_dump"]["path"]).read_bytes()
    assert "2 tables in appdb" in next(s["detail"] for s in op["steps"] if s["key"] == "verify")


async def test_mariadb_load_failure_offers_undo(config):
    maria, files = mariadb_setup()
    maria.fail_load = True
    engine = make_engine(config, files, maria)

    op = await run_to_end(engine, engine.start(body(container="mariadb", db_type="mariadb")))

    assert op["status"] == "failed"
    assert "Can't create table" in op["error_message"]
    assert op["database_changed"] is True and op["can_undo"] is True

    maria.fail_load = False
    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="appdb")))
    assert undo["status"] == "completed", undo
    assert maria.databases["appdb"]["rows"] == ["current-1"]


async def test_mysql_system_schema_is_not_restorable(config):
    with pytest.raises(GuidedRestoreError):
        validate_ref(DatabaseRef("mariadb", "mariadb", "mysql"))


# ---------------------------------------------------------------------------
# SQLite: prepared, never written
# ---------------------------------------------------------------------------


def sqlite_bytes(tmp_path: Path, rows=("alice",)) -> bytes:
    path = tmp_path / "src.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (name TEXT)")
    conn.executemany("INSERT INTO users VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()
    data = path.read_bytes()
    path.unlink()
    return data


class FakeSqliteDumper:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def dump_to_directory(self, db, directory):
        self.calls.append((db, directory))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "vaultwarden_db.sqlite3-0123abcd_20261109_120000.sqlite3"
        if not self.ok:
            return DumpResult(db.container_name, "sqlite", db.db_name, "", 0, "failed", "failed", "database is locked")
        path.write_bytes(b"SQLite format 3\x00current")
        return DumpResult(db.container_name, "sqlite", db.db_name, str(path), path.stat().st_size, "ok", "success")


async def test_sqlite_restore_prepares_file_and_instructions(config, tmp_path):
    live = "/mnt/user/appdata/vault warden/db.sqlite3"
    name = f"{dump_prefix('vaultwarden', 'sqlite', 'db.sqlite3', live)}_{TS}.sqlite3"
    files = {
        name: sqlite_bytes(tmp_path),
        DUMP_MANIFEST_NAME: manifest(
            entry(name, container="vaultwarden", db_type="sqlite", db_name="db.sqlite3", host_path=live)
        ),
    }
    dumper = FakeSqliteDumper()
    engine = make_engine(config, files, dumper_factory=lambda: dumper)
    engine.docker_client = None  # SQLite needs no Docker

    op = await run_to_end(
        engine,
        engine.start(body(container="vaultwarden", db_type="sqlite", db_name="db.sqlite3", confirm="", host_path=live)),
    )

    assert op["status"] == "action_required", op
    assert op["method"] == "manual"
    assert set(step_status(op).values()) == {"done"}
    restored = Path(config.restore_dir) / f"guided-{op['id']}" / "restored" / name
    assert restored.is_file()
    assert "integrity_check ok" in next(s["detail"] for s in op["steps"] if s["key"] == "verify_dump")
    commands = [i["command"] for i in op["instructions"]]
    assert commands[0] == "docker stop vaultwarden"
    # Paths are shell-quoted (this one has a space) and the WAL files are removed before the copy.
    assert (
        "rm -f '/mnt/user/appdata/vault warden/db.sqlite3-wal' '/mnt/user/appdata/vault warden/db.sqlite3-shm'"
        in commands
    )
    assert f"cp {restored} '/mnt/user/appdata/vault warden/db.sqlite3'" in commands
    assert commands[-1].endswith("vaultwarden_db.sqlite3-0123abcd_20261109_120000.sqlite3")
    assert op["can_undo"] is False  # Arkive never wrote the live file
    assert dumper.calls[0][0].host_path == live


async def test_sqlite_safety_copy_failure_is_a_warning(config, tmp_path):
    live = "/mnt/user/appdata/vault warden/db.sqlite3"
    name = f"{dump_prefix('vaultwarden', 'sqlite', 'db.sqlite3', live)}_{TS}.sqlite3"
    files = {name: sqlite_bytes(tmp_path)}
    engine = make_engine(config, files, dumper_factory=lambda: FakeSqliteDumper(ok=False))

    op = await run_to_end(
        engine, engine.start(body(container="vaultwarden", db_type="sqlite", db_name="db.sqlite3", host_path=live))
    )

    assert op["status"] == "action_required"
    assert "No safety copy" in op["warnings"][0]
    assert step_status(op)["safety_dump"] == "failed"
    assert len(op["instructions"]) == 5


async def test_corrupt_sqlite_file_is_refused(config):
    live = "/mnt/user/appdata/vault warden/db.sqlite3"
    name = f"{dump_prefix('vaultwarden', 'sqlite', 'db.sqlite3', live)}_{TS}.sqlite3"
    engine = make_engine(config, {name: b"SQLite format 3\x00" + b"\x00" * 50}, dumper_factory=FakeSqliteDumper)

    op = await run_to_end(
        engine, engine.start(body(container="vaultwarden", db_type="sqlite", db_name="db.sqlite3", host_path=live))
    )

    assert op["status"] == "failed"
    assert step_status(op)["verify_dump"] == "failed"
    assert op["instructions"] == []


# ---------------------------------------------------------------------------
# Preview, options, validation
# ---------------------------------------------------------------------------


async def test_preview_reports_dump_and_changes_nothing(config):
    pg, app, files = postgres_setup(config)
    app.status = "running"
    engine = make_engine(config, files, pg, app)

    preview = await engine.preview(body())

    assert preview["method"] == "load"
    assert preview["confirm_phrase"] == "appdb"
    assert preview["dump"]["file"] == pg_file() and preview["dump"]["source"] == "manifest"
    assert preview["snapshot"]["target_name"] == "Local NAS"
    assert preview["dependents"] == [
        {"name": "app-server", "status": "running", "reason": "uses this database; same compose project (app)"}
    ]
    assert preview["blockers"] == []
    assert any("Apps that use this database are running" in w for w in preview["warnings"])
    assert "psql connects as postgres; 2 open session(s)" in preview["database"]["container_detail"]
    assert [s["key"] for s in preview["steps"]][0] == "check_container"
    # Read-only: no load, no stop, no lock, no scratch left behind.
    assert not [e for e in pg.client.api.execs.values() if e["stdin"]]
    assert app.calls == []
    assert list(config.restore_dir.iterdir()) == []
    assert not BackupLockManager(config.config_dir).is_backup_running()


async def test_preview_lists_blockers_and_errors(config):
    pg, app, files = postgres_setup(config)
    pg.status = "exited"
    engine = make_engine(config, files, pg, app)
    preview = await engine.preview(body())
    assert preview["blockers"] == ["pg is exited; start it first"]

    engine.backup_engine.ls_error = ResticError("repository is locked")
    with pytest.raises(GuidedRestoreError) as exc:
        await engine.preview(body())
    assert exc.value.status_code == 422 and "repository is locked" in str(exc.value)


async def test_resolve_errors(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)
    cases = [
        (body(db_name="nope"), 404),
        (body(container="cache", db_type="redis", db_name="redis"), 422),
        (GuidedRestoreStart(**{**body().model_dump(), "target_id": "missing"}), 404),
        (GuidedRestoreStart(**{**body().model_dump(), "snapshot_id": "ffffffff"}), 404),
    ]
    for request, status in cases:
        with pytest.raises(GuidedRestoreError) as exc:
            await engine.preview(request)
        assert exc.value.status_code == status, (request, exc.value)

    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE snapshots SET paths = ?", (json.dumps(["/mnt/user/appdata"]),))
        await db.commit()
    with pytest.raises(GuidedRestoreError) as exc:
        await engine.preview(body())
    assert exc.value.status_code == 422 and "dumps folder" in str(exc.value)


async def test_options_lists_methods_and_dependents(config):
    engine = make_engine(config, {})
    options = {(d["container_name"], d["db_name"]): d for d in await engine.options()}
    assert options[("pg", "appdb")]["method"] == "load"
    assert [d["name"] for d in options[("pg", "appdb")]["dependents"]] == ["app-server"]
    assert options[("mariadb", "appdb")]["method"] == "load"
    assert options[("vaultwarden", "db.sqlite3")]["method"] == "manual"
    assert options[("cache", "redis")]["method"] == "unsupported"
    assert "mongorestore" not in options[("cache", "redis")]["reason"]
    # Unsupported entries sort last.
    assert list(options)[-1] == ("cache", "redis")


# ---------------------------------------------------------------------------
# Restart recovery and retention
# ---------------------------------------------------------------------------


async def test_recover_after_restart_marks_running_as_failed(config):
    engine = make_engine(config, {})
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO guided_restores (id, method, container_name, db_type, db_name, phase,
                   stopped_containers, steps)
               VALUES (?, 'load', 'pg', 'postgres', 'appdb', 'load', ?, ?)""",
            (
                "a" * 32,
                json.dumps(["app-server"]),
                json.dumps([{"key": "load", "status": "running"}, {"key": "verify", "status": "pending"}]),
            ),
        )
        await db.commit()

    await engine.recover_after_restart()

    op = await engine.get("a" * 32)
    assert op["status"] == "failed"
    assert "may be partially restored" in op["error_message"]
    assert "app-server" in op["error_message"]
    assert op["database_changed"] is True
    assert [s["status"] for s in op["steps"]] == ["failed", "skipped"]


async def test_retention_only_removes_old_marked_dirs(config):
    engine = make_engine(config, {})
    root = config.restore_dir
    old_id, new_id, foreign_id = "b" * 32, "c" * 32, "d" * 32
    for op_id in (old_id, new_id):
        (root / f"guided-{op_id}").mkdir()
        (root / f"guided-{op_id}" / WORK_MARKER).write_text(op_id)
    (root / f"guided-{foreign_id}").mkdir()  # no marker: a user's own restore
    (root / "guided-recovery").mkdir()
    stale_preview = root / ".guided-preview-x1"
    stale_preview.mkdir()
    (stale_preview / WORK_MARKER).write_text("preview")
    past = time.time() - 30 * 86400
    for path in (root / f"guided-{old_id}", root / f"guided-{foreign_id}", stale_preview):
        os.utime(path, (past, past))

    assert engine._prune_work_dirs() == 1

    assert not (root / f"guided-{old_id}").exists()
    assert (root / f"guided-{new_id}").exists()
    assert (root / f"guided-{foreign_id}").exists()
    assert (root / "guided-recovery").exists()
    assert not stale_preview.exists()


async def test_undo_refused_when_safety_dump_is_gone(config):
    pg, app, files = postgres_setup(config)
    engine = make_engine(config, files, pg, app)
    op = await run_to_end(engine, engine.start(body()))
    Path(op["safety_dump"]["path"]).unlink()

    assert (await engine.get(op["id"]))["can_undo"] is False
    with pytest.raises(GuidedRestoreError) as exc:
        await engine.start_undo(op["id"], GuidedRestoreUndo(confirm="appdb"))
    assert exc.value.status_code == 410


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_quoting_helpers():
    assert pg_ident('we"ird db') == '"we""ird db"'
    assert pg_literal("it's") == "'it''s'"
    prelude = postgres_prelude('my "db"', exists=True).decode()
    assert 'ALTER DATABASE "my ""db""" WITH ALLOW_CONNECTIONS false;' in prelude
    assert "WHERE datname = 'my \"db\"'" in prelude
    assert postgres_prelude("x", exists=False) == b"\\set ON_ERROR_STOP on\n"
    assert mysql_prelude("appdb", exists=False) == b""
    assert b"TABLE_SCHEMA = 'appdb'" in mysql_prelude("appdb", exists=True)


def test_owner_sql_matches_server_version():
    assert "p.prokind" in postgres_owner_sql("db", "app", 160000)
    old = postgres_owner_sql("db", "app", 100000)
    assert "p.proisagg" in old and "prokind" not in old
    assert "$arkive$" in old and 'OWNER TO "app"' in old


@pytest.mark.parametrize(
    "ref",
    [
        DatabaseRef("pg", "postgres", "-oops"),
        DatabaseRef("pg", "postgres", "host=evil"),
        DatabaseRef("pg", "postgres", "a/b"),
        DatabaseRef("pg", "postgres", "x" * 64),
        DatabaseRef("-pg", "postgres", "appdb"),
        DatabaseRef("db", "mariadb", "-e"),
        DatabaseRef("db", "mariadb", "has space"),
        DatabaseRef("db", "mariadb", "information_schema"),
        DatabaseRef("app", "sqlite", "x.db", "relative/x.db"),
        DatabaseRef("app", "sqlite", "x.db", "/mnt/user/../etc/x.db"),
        DatabaseRef("app", "sqlite", "x.db", None),
    ],
)
def test_validate_ref_rejects_unsafe_names(ref):
    with pytest.raises(GuidedRestoreError):
        validate_ref(ref)


def test_validate_ref_accepts_real_names():
    validate_ref(DatabaseRef("immich_postgres", "postgres", "Immich Library"))
    validate_ref(DatabaseRef("db", "mariadb", "nextcloud.prod-1"))
    validate_ref(DatabaseRef("app", "sqlite", "x.db", "/mnt/user/appdata/app/x.db"))


def test_restore_method():
    assert restore_method("postgres") == ("load", None)
    assert restore_method("mysql") == ("load", None)
    assert restore_method("sqlite") == ("manual", None)
    method, reason = restore_method("mongodb")
    assert method == "unsupported" and "mongorestore" in reason


def test_mysql_dump_databases(tmp_path):
    path = tmp_path / "d.sql"
    path.write_bytes(mysql_dump_text("app`db", ["x"]).replace(b"`app`db`", b"`app``db`"))
    assert mysql_dump_databases(path) == {"app`db"}
    path.write_bytes(b"INSERT INTO t VALUES ('USE `evil`;');\n-- Dump completed\n")
    assert mysql_dump_databases(path) == set()


def test_sqlite_instructions_quote_paths():
    ref = DatabaseRef("app; rm -rf /", "sqlite", "x.db", "/mnt/user/appdata/it's here/x.db")
    steps = sqlite_instructions(ref, "/mnt/user/appdata/arkive/restores/r.sqlite3", None, "20261109")
    commands = [s["command"] for s in steps]
    assert commands[0] == "docker stop 'app; rm -rf /'"
    assert "cp /mnt/user/appdata/arkive/restores/r.sqlite3 '/mnt/user/appdata/it'\"'\"'s here/x.db'" in commands
    assert len(steps) == 5


# ---------------------------------------------------------------------------
# stdin exec transport
# ---------------------------------------------------------------------------


def test_exec_with_stdin_streams_input_and_reports_exit_code():
    received = {}

    class Container(FakePostgres):
        def run(self, cmd, env, stdin):
            received["stdin"] = stdin
            return 7, b"out\n", b"psql:<stdin>:3: ERROR:  boom\n"

    container = Container()
    progress = []
    result = exec_with_stdin(
        container, ["psql"], {"A": "1"}, [b"one\n", b"", b"two\n"], progress.append, poll_seconds=0.01
    )

    assert received["stdin"] == b"one\ntwo\n"
    assert result.exit_code == 7
    assert result.stdout == "out\n" and "ERROR:  boom" in result.stderr
    assert progress == [4, 4]
    exec_record = next(iter(container.client.api.execs.values()))
    assert exec_record["stdin"] is True and exec_record["env"] == {"A": "1"}


def test_exec_with_stdin_survives_early_exit():
    class Container(FakePostgres):
        def run(self, cmd, env, stdin):
            return 3, b"", b"psql:<stdin>:1: ERROR:  stopped early\n"

    container = Container()
    container.read_limit = 10
    chunks = (b"x" * 65536 for _ in range(200))  # far more than the socket buffers
    result = exec_with_stdin(container, ["psql"], None, chunks, None, poll_seconds=0.01)

    assert result.exit_code == 3
    assert "stopped early" in result.stderr
    assert result.write_error
