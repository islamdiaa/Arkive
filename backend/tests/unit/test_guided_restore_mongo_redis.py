"""Guided restore of MongoDB (loaded in place) and Redis (prepared, swapped in by hand).

The fakes (tests/fakes/fake_mongo_redis.py) write and read real mongodump
archives and RDB files, so the integrity checks run on the same bytes the real
tools produce.
"""

import gzip
import json
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.models.guided_restore import GuidedRestoreUndo
from app.services.db_dumper import DUMP_MANIFEST_NAME, DumpResult, dump_prefix
from app.services.dump_checks import read_mongo_archive
from app.services.guided_restore import (
    DatabaseRef,
    GuidedRestoreError,
    RedisSetup,
    redis_instructions,
    restore_scope,
    validate_ref,
)
from tests.fakes.fake_db_server import FakeAppContainer
from tests.fakes.fake_mongo_redis import FakeMongo, FakeRedis, mongo_archive, rdb_file
from tests.unit.test_guided_restore import (
    TS,
    body,
    entry,
    make_config,
    make_engine,
    manifest,
    run_to_end,
    step_status,
)


@pytest_asyncio.fixture
async def config(tmp_path):
    return await make_config(tmp_path)


SNAPSHOT_DATA = {
    "app": {"notes": [{"_id": 1, "text": "alice"}, {"_id": 2, "text": "bob"}], "tags": [{"_id": 1, "t": "x"}]},
    "other": {"items": [{"_id": 1, "n": 1}]},
}
CURRENT_DATA = {
    "app": {"notes": [{"_id": 9, "text": "changed"}], "added_later": [{"_id": 1}]},
    "other": {"items": [{"_id": 1, "n": 2}]},
    "created_later": {"c": [{"_id": 1}]},
}


async def discover(config, name, db_type, db_name, dependents=()):
    async with aiosqlite.connect(config.db_path) as db:
        databases = json.dumps([{"container_name": name, "db_type": db_type, "db_name": db_name}])
        await db.execute(
            "INSERT INTO discovered_containers (name, image, status, databases, compose_project) "
            "VALUES (?, 'img', 'running', ?, 'stack')",
            (name, databases),
        )
        for dep in dependents:
            await db.execute(
                "INSERT INTO discovered_containers (name, image, status, databases, compose_project) "
                "VALUES (?, 'img', 'running', '[]', 'stack')",
                (dep,),
            )
        await db.commit()


def mongo_file(db_name="app", ext="archive"):
    return f"{dump_prefix('mongo', 'mongodb', db_name)}_{TS}.{ext}"


def mongo_files(db_name="app", data=None, ext="archive", archive=None):
    archive = archive if archive is not None else mongo_archive(data or SNAPSHOT_DATA)
    if ext.endswith(".gz"):
        archive = gzip.compress(archive)
    name = mongo_file(db_name, ext)
    return {name: archive, DUMP_MANIFEST_NAME: manifest(entry(name, "mongo", "mongodb", db_name))}


def clone(data):
    return json.loads(json.dumps(data))


async def mongo_setup(config, db_name="app", current=None, **kwargs):
    await discover(config, "mongo", "mongodb", db_name, dependents=("mongo-app",))
    mongo = FakeMongo(databases=clone(current or CURRENT_DATA), init_db=None if db_name == "admin" else db_name)
    for key, value in kwargs.items():
        setattr(mongo, key, value)
    return mongo, FakeAppContainer("mongo-app")


def all_argv(container) -> str:
    return json.dumps([c["cmd"] for c in container.client.api.execs.values()] + [c["cmd"] for c in container.calls])


def mongo_body(db_name="app", **kwargs):
    return body(container="mongo", db_type="mongodb", db_name=db_name, confirm=kwargs.pop("confirm", db_name), **kwargs)


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------


async def test_mongo_single_database_restore_and_undo(config):
    mongo, app = await mongo_setup(config)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body(stop=["mongo-app"])))

    assert op["status"] == "completed", op
    assert set(step_status(op).values()) == {"done"}
    # The database is the snapshot's: changed documents back, the collection
    # created after the backup gone. Other databases and admin are untouched.
    assert mongo.databases["app"] == SNAPSHOT_DATA["app"]
    assert mongo.documents("other", "items") == [{"_id": 1, "n": 2}]
    assert "created_later" in mongo.databases and "system.users" in mongo.databases["admin"]
    steps = {s["key"]: s["detail"] for s in op["steps"]}
    assert "every collection's checksum matches; 2 collections, 3 documents in app" in steps["verify_dump"]
    assert "MongoDB 8.0.4, logged in as root" in steps["check_container"]
    assert "dropped first: app; password read from a private file" in steps["load"]
    assert steps["verify"] == "2 collections, 3 documents in app"
    restore_cmd = next(e["cmd"] for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongorestore")
    assert "--nsInclude=app.*" in restore_cmd and "--drop" in restore_cmd
    # The password never reaches an argv, and the file that held it is gone.
    assert "mongo-secret" not in all_argv(mongo)
    assert mongo.tmp_dirs == [] and mongo.files == {}
    # The safety dump is a complete archive of the database as it was.
    safety = Path(op["safety_dump"]["path"])
    archive = read_mongo_archive(safety)
    assert archive.databases == {"app"} and len(archive.counted(["app"])) == 2
    assert [c["cmd"] for c in app.calls] == [["<stop>"], ["<start>"]]
    assert op["can_undo"] is True

    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="app")))

    assert undo["status"] == "completed", undo
    assert mongo.databases["app"] == CURRENT_DATA["app"]
    assert safety.is_file()  # undo never consumes the safety dump


async def test_mongo_whole_server_restore_replaces_every_user_database(config):
    mongo, app = await mongo_setup(config, db_name="admin")
    engine = make_engine(config, mongo_files("admin"), mongo, app)

    preview = await engine.preview(mongo_body("admin"))
    assert preview["database"]["scope"] == "every database on the server except admin, config and local"
    assert "now on the server: app, created_later, other" in preview["database"]["container_detail"]
    assert preview["confirm_phrase"] == "admin"

    op = await run_to_end(engine, engine.start(mongo_body("admin")))

    assert op["status"] == "completed", op
    assert mongo.user_databases() == ["app", "other"]  # created_later is gone: it was not in the backup
    assert mongo.databases["app"] == SNAPSHOT_DATA["app"]
    assert mongo.databases["admin"] == {"system.users": [{"_id": "admin.root", "user": "root"}]}
    restore_cmd = next(e["cmd"] for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongorestore")
    assert [a for a in restore_cmd if a.startswith("--ns")] == [
        "--nsExclude=admin.*",
        "--nsExclude=config.*",
        "--nsExclude=local.*",
    ]
    assert op["scope"] == "every database on the server except admin, config and local"

    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="admin")))
    assert undo["status"] == "completed", undo
    assert mongo.user_databases() == ["app", "created_later", "other"]
    assert mongo.databases["other"] == CURRENT_DATA["other"]


async def test_mongo_without_auth_and_gzip_dump(config):
    await discover(config, "mongo", "mongodb", "app")
    mongo = FakeMongo(databases=clone(CURRENT_DATA), user=None, password=None, init_db="app")
    engine = make_engine(config, mongo_files(ext="archive.gz"), mongo)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "completed", op
    assert mongo.databases["app"] == SNAPSHOT_DATA["app"]
    restore_cmd = next(e["cmd"] for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongorestore")
    assert not [a for a in restore_cmd if a.startswith(("--username", "--config", "--password"))]
    assert mongo.tmp_dirs == []
    assert not (Path(config.restore_dir) / f"guided-{op['id']}" / "load.archive").exists()


async def test_mongo_wrong_password_fails_the_check(config):
    mongo, app = await mongo_setup(config)
    mongo.password = "rotated"  # the container env still has the old one
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "failed"
    assert step_status(op)["check_container"] == "failed"
    assert "Authentication failed" in op["error_message"] and "app was not changed" in op["error_message"]
    assert mongo.databases["app"] == CURRENT_DATA["app"]


@pytest.mark.parametrize("program", ["mongosh", "mongorestore", "mongodump"])
async def test_mongo_missing_tools_are_reported(config, program):
    mongo, app = await mongo_setup(config)
    mongo.missing = {program}
    engine = make_engine(config, mongo_files(), mongo, app)

    preview = await engine.preview(mongo_body())

    assert len(preview["blockers"]) == 1 and f"{program} is not available in mongo" in preview["blockers"][0]


@pytest.mark.parametrize(
    "hello, message",
    [
        ({"ok": 1, "msg": "isdbgrid"}, "mongos router"),
        ({"ok": 1, "setName": "rs0", "isWritablePrimary": False}, "not the primary of replica set rs0"),
    ],
)
async def test_mongo_sharded_or_secondary_is_refused(config, hello, message):
    mongo, app = await mongo_setup(config)
    mongo.hello = hello
    engine = make_engine(config, mongo_files(), mongo, app)

    preview = await engine.preview(mongo_body())

    assert message in preview["blockers"][0]


async def test_mongo_truncated_archive_is_refused_before_anything_changes(config):
    mongo, app = await mongo_setup(config)
    archive = mongo_archive(SNAPSHOT_DATA)
    engine = make_engine(config, mongo_files(archive=archive[: len(archive) - 40]), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body(stop=["mongo-app"])))

    assert op["status"] == "failed"
    assert step_status(op)["verify_dump"] == "failed"
    assert "cut short" in op["error_message"] or "truncated" in op["error_message"]
    assert all(b"dropDatabase" not in c["stdin"] for c in mongo.calls)
    assert not [e for e in mongo.client.api.execs.values() if e["cmd"][0] in ("mongorestore", "mongodump")]
    assert mongo.databases["app"] == CURRENT_DATA["app"]
    assert app.calls == []
    assert op["can_undo"] is False


async def test_mongo_dump_without_the_database_is_refused(config):
    mongo, app = await mongo_setup(config)
    engine = make_engine(config, mongo_files(data={"other": SNAPSHOT_DATA["other"]}), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "failed"
    assert "the dump has no database app" in op["error_message"]
    assert mongo.databases["app"] == CURRENT_DATA["app"]


async def test_mongo_failed_safety_dump_stops_before_anything_changes(config):
    mongo, app = await mongo_setup(config, dump_truncated=True)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body(stop=["mongo-app"])))

    assert op["status"] == "failed"
    assert step_status(op)["safety_dump"] == "failed"
    assert "safety dump is not complete" in op["error_message"]
    assert mongo.databases["app"] == CURRENT_DATA["app"]
    assert app.calls == []
    assert not list((Path(config.restore_dir) / f"guided-{op['id']}" / "safety").glob("*"))


async def test_mongo_restore_failing_midway_can_be_undone(config):
    mongo, app = await mongo_setup(config, fail_restore_midway=True)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body(stop=["mongo-app"])))

    assert op["status"] == "failed"
    assert step_status(op)["load"] == "failed"
    assert "mongorestore exited with code 1" in op["error_message"]
    assert "Use Undo" in op["error_message"] and "Apps left stopped: mongo-app" in op["error_message"]
    assert op["database_changed"] is True and op["can_undo"] is True
    assert mongo.databases["app"] != CURRENT_DATA["app"]
    assert mongo.tmp_dirs == []  # the password file is removed on failure too

    mongo.fail_restore_midway = False
    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="app")))
    assert undo["status"] == "completed", undo
    assert mongo.databases["app"] == CURRENT_DATA["app"]
    assert app.status == "running"


async def test_mongo_documents_that_failed_to_restore_fail_the_load(config):
    mongo, app = await mongo_setup(config, failed_documents=3)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "failed"
    assert "could not restore 3 document(s)" in op["error_message"]
    assert op["can_undo"] is True


async def test_mongo_drop_failure_offers_undo(config):
    mongo, app = await mongo_setup(config, fail_drop=True)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "failed"
    assert "not primary" in op["error_message"]
    # Some databases may have been dropped before the error: undo stays possible.
    assert op["database_changed"] is True and op["can_undo"] is True


async def test_mongo_password_on_the_command_line_only_without_a_private_tmp(config):
    mongo, app = await mongo_setup(config, fail_mktemp=True)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "completed", op
    load = next(s["detail"] for s in op["steps"] if s["key"] == "load")
    assert "password passed on the mongorestore command line" in load
    restore_cmd = next(e["cmd"] for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongorestore")
    assert "--password=mongo-secret" in restore_cmd
    # mongodump (safety dump) and mongosh still read it from stdin.
    assert all(
        "mongo-secret" not in json.dumps(e["cmd"])
        for e in mongo.client.api.execs.values()
        if e["cmd"][0] != "mongorestore"
    )


async def test_mongo_tmp_on_a_mount_is_never_written(config):
    mongo, app = await mongo_setup(config)
    mongo.attrs["Mounts"] = [{"Source": "/mnt/user/appdata/mongo-tmp", "Destination": "/tmp"}]
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))

    assert op["status"] == "completed", op
    assert mongo.commands("mktemp") == []
    assert "command line" in next(s["detail"] for s in op["steps"] if s["key"] == "load")


@pytest.mark.parametrize(
    "ref",
    [
        DatabaseRef("mongo", "mongodb", "config"),
        DatabaseRef("mongo", "mongodb", "local"),
        DatabaseRef("mongo", "mongodb", "-oops"),
        DatabaseRef("mongo", "mongodb", "a.b"),
        DatabaseRef("mongo", "mongodb", "a*"),
        DatabaseRef("mongo", "mongodb", "has space"),
        DatabaseRef("mongo", "mongodb", "x" * 64),
    ],
)
def test_mongo_unsafe_or_system_names_are_refused(ref):
    with pytest.raises(GuidedRestoreError):
        validate_ref(ref)


def test_mongo_names_that_restore():
    validate_ref(DatabaseRef("mongo", "mongodb", "admin"))  # the whole server
    validate_ref(DatabaseRef("mongo", "mongodb", "unifi_stat-2"))
    assert restore_scope("mongodb", "unifi") is None


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


class FakeRedisDumper:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def dump_to_directory(self, db, directory, **kwargs):
        self.calls.append((db, directory))
        directory.mkdir(parents=True, exist_ok=True)
        if not self.ok:
            return DumpResult(db.container_name, "redis", "redis", "", 0, "skipped", "failed", "BGSAVE failed")
        path = directory / f"{dump_prefix('cache', 'redis', 'redis')}_20261109_120000.rdb"
        path.write_bytes(rdb_file(keys={"current": "1"}))
        return DumpResult(db.container_name, "redis", "redis", str(path), path.stat().st_size, "skipped", "success")


def redis_files(data: bytes):
    name = f"{dump_prefix('cache', 'redis', 'redis')}_{TS}.rdb"
    return name, {name: data, DUMP_MANIFEST_NAME: manifest(entry(name, "cache", "redis", "redis"))}


def redis_body():
    return body(container="cache", db_type="redis", db_name="redis", confirm="")


async def test_redis_7_with_aof_prepares_file_and_rebuilds_the_aof(config):
    name, files = redis_files(rdb_file(version=11, redis_ver="7.2.4"))
    redis = FakeRedis(appendonly=True, password="r-secret")
    dumper = FakeRedisDumper()
    engine = make_engine(config, files, redis, dumper_factory=lambda: dumper)

    op = await run_to_end(engine, engine.start(redis_body()))

    assert op["status"] == "action_required", op
    assert op["method"] == "manual" and op["can_undo"] is False
    assert [s["key"] for s in op["steps"]][0] == "check_container"
    assert set(step_status(op).values()) == {"done"}
    steps = {s["key"]: s["detail"] for s in op["steps"]}
    assert "Redis 7.2.4; data file /mnt/user/appdata/redis/dump.rdb; append-only file on" in steps["check_container"]
    assert steps["verify_dump"].startswith("RDB format 11, written by Redis 7.2.4 at 2026-")
    assert steps["verify_dump"].endswith("checksum matches")
    restored = Path(config.restore_dir) / f"guided-{op['id']}" / "restored" / name
    assert restored.is_file()
    commands = [i["command"] for i in op["instructions"]]
    assert commands[0] == "docker stop cache"
    assert commands[2] == (
        "if [ -e /mnt/user/appdata/redis/appendonlydir ]; then mv /mnt/user/appdata/redis/appendonlydir "
        '/mnt/user/appdata/redis/appendonlydir".before-restore-$(date +%Y%m%d-%H%M%S)"; fi'
    )
    assert f"cp {restored} /mnt/user/appdata/redis/dump.rdb" in commands
    rebuild = next(c for c in commands if c.startswith("mkdir "))
    assert rebuild == (
        f"mkdir /mnt/user/appdata/redis/appendonlydir && cp {restored} "
        "/mnt/user/appdata/redis/appendonlydir/appendonly.aof.1.base.rdb && "
        "printf 'file %s seq 1 type b\\n' appendonly.aof.1.base.rdb > "
        "/mnt/user/appdata/redis/appendonlydir/appendonly.aof.manifest"
    )
    assert "chown -R 999:999 /mnt/user/appdata/redis/dump.rdb /mnt/user/appdata/redis/appendonlydir" in commands
    assert "docker start cache" in commands
    assert "docker exec -it cache redis-cli --askpass INFO keyspace" in commands
    assert commands[-1].endswith("_20261109_120000.rdb")  # the safety copy
    # The order matters: stopped first, the old AOF moved away before the new one is made.
    assert commands.index("docker stop cache") < commands.index(rebuild) < commands.index("docker start cache")
    # The password went in the environment, never in argv.
    assert all("r-secret" not in json.dumps(c["cmd"]) for c in redis.calls)
    assert redis.calls[0]["env"] == {"REDISCLI_AUTH": "r-secret"}


async def test_redis_6_with_aof_uses_the_rdb_as_the_append_only_file(config):
    _name, files = redis_files(rdb_file(version=9, redis_ver="6.2.14"))
    redis = FakeRedis(version="6.2.14", appendonly=True)
    engine = make_engine(config, files, redis, dumper_factory=FakeRedisDumper)

    op = await run_to_end(engine, engine.start(redis_body()))

    assert op["status"] == "action_required", op
    commands = [i["command"] for i in op["instructions"]]
    assert any(c.startswith("if [ -e /mnt/user/appdata/redis/appendonly.aof ]; then mv ") for c in commands)
    assert any(c.startswith("cp ") and c.endswith(" /mnt/user/appdata/redis/appendonly.aof") for c in commands)
    assert not any(c.startswith("mkdir") for c in commands)
    assert "docker exec cache redis-cli INFO keyspace" in commands


async def test_redis_without_aof_only_swaps_the_rdb(config):
    _name, files = redis_files(rdb_file())
    redis = FakeRedis(mounts=[{"Source": "/mnt/user/appdata/my redis", "Destination": "/data"}])
    redis.config["dbfilename"] = "cache.rdb"
    engine = make_engine(config, files, redis, dumper_factory=FakeRedisDumper)

    op = await run_to_end(engine, engine.start(redis_body()))

    commands = [i["command"] for i in op["instructions"]]
    assert not any("appendonly" in c for c in commands)
    assert commands[1] == (
        "if [ -e '/mnt/user/appdata/my redis/cache.rdb' ]; then cp -a '/mnt/user/appdata/my redis/cache.rdb' "
        "'/mnt/user/appdata/my redis/cache.rdb'\".before-restore-$(date +%Y%m%d-%H%M%S)\"; fi"
    )
    assert "chown -R 999:999 '/mnt/user/appdata/my redis/cache.rdb'" in commands


@pytest.mark.parametrize(
    "data, message",
    [
        (rdb_file()[:-20], "truncated or damaged"),
        (rdb_file()[:30] + b"X" + rdb_file()[31:], "checksum mismatch"),
        (b"NOTREDIS" * 4, "not a Redis RDB file"),
    ],
)
async def test_redis_damaged_dump_is_refused(config, data, message):
    _name, files = redis_files(data)
    engine = make_engine(config, files, FakeRedis(), dumper_factory=FakeRedisDumper)

    op = await run_to_end(engine, engine.start(redis_body()))

    assert op["status"] == "failed"
    assert step_status(op)["verify_dump"] == "failed"
    assert message in op["error_message"]
    assert op["instructions"] == []


async def test_redis_dump_too_new_for_the_running_redis_is_refused(config):
    _name, files = redis_files(rdb_file(version=12, redis_ver="7.4.1"))
    engine = make_engine(config, files, FakeRedis(version="7.0.15"), dumper_factory=FakeRedisDumper)

    op = await run_to_end(engine, engine.start(redis_body()))

    assert op["status"] == "failed"
    assert "RDB format 12 (written by Redis 7.4.1), which Redis 7.0.15 in cache cannot read" in op["error_message"]


async def test_redis_dump_without_checksum_is_a_warning(config):
    _name, files = redis_files(rdb_file(checksum=False))
    engine = make_engine(config, files, FakeRedis(), dumper_factory=FakeRedisDumper)

    op = await run_to_end(engine, engine.start(redis_body()))

    assert op["status"] == "action_required"
    assert any("without a checksum" in w for w in op["warnings"])


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda r: r.attrs.__setitem__("Mounts", []), "not on a volume or bind mount"),
        (lambda r: setattr(r, "password", "unknown-to-arkive"), "NOAUTH Authentication required"),
        (lambda r: setattr(r, "config_disabled", True), "CONFIG may be disabled"),
        (lambda r: r.config.__setitem__("dbfilename", "../escape.rdb"), "not a plain file name"),
    ],
)
async def test_redis_setups_that_cannot_be_swapped_are_refused_first(config, change, message):
    _name, files = redis_files(rdb_file())
    redis = FakeRedis()
    change(redis)
    dumper = FakeRedisDumper()
    engine = make_engine(config, files, redis, dumper_factory=lambda: dumper)

    preview = await engine.preview(redis_body())
    assert message in preview["blockers"][0]
    restores = len(engine.backup_engine.restores)  # the preview reads the dump manifest

    op = await run_to_end(engine, engine.start(redis_body()))
    assert op["status"] == "failed"
    assert step_status(op)["check_container"] == "failed"
    # Refused before anything was restored or dumped.
    assert dumper.calls == [] and len(engine.backup_engine.restores) == restores


async def test_redis_safety_dump_failure_is_a_warning(config):
    _name, files = redis_files(rdb_file())
    engine = make_engine(config, files, FakeRedis(), dumper_factory=lambda: FakeRedisDumper(ok=False))

    op = await run_to_end(engine, engine.start(redis_body()))

    assert op["status"] == "action_required"
    assert "No safety copy" in op["warnings"][0]
    assert not op["instructions"][-1]["command"].endswith(".rdb")


async def test_redis_preview_checks_the_container(config):
    _name, files = redis_files(rdb_file())
    engine = make_engine(config, files, FakeRedis(appendonly=True))

    preview = await engine.preview(redis_body())

    assert preview["method"] == "manual" and preview["confirm_phrase"] is None
    assert preview["blockers"] == []
    assert "append-only file on (appendonlydir)" in preview["database"]["container_detail"]
    assert [s["key"] for s in preview["steps"]] == [
        "check_container",
        "locate_dump",
        "restore_dump",
        "verify_dump",
        "safety_dump",
        "instructions",
    ]


def test_redis_instructions_without_owner_skip_chown():
    setup = RedisSetup(
        version="7.4.1",
        data_dir="/data",
        host_dir="/mnt/user/appdata/redis",
        dbfilename="dump.rdb",
        appendonly=False,
        aof_dir="appendonlydir",
        aof_file="appendonly.aof",
        owner=None,
        needs_auth=False,
    )
    steps = redis_instructions("cache", setup, "/mnt/user/appdata/arkive/restores/x.rdb", None)
    assert [s["command"] for s in steps] == [
        "docker stop cache",
        "if [ -e /mnt/user/appdata/redis/dump.rdb ]; then cp -a /mnt/user/appdata/redis/dump.rdb "
        '/mnt/user/appdata/redis/dump.rdb".before-restore-$(date +%Y%m%d-%H%M%S)"; fi',
        "cp /mnt/user/appdata/arkive/restores/x.rdb /mnt/user/appdata/redis/dump.rdb",
        "docker start cache",
        "docker exec cache redis-cli INFO keyspace",
    ]


async def test_mongo_undo_removes_a_database_the_restore_created(config):
    current = {"other": CURRENT_DATA["other"]}
    mongo, app = await mongo_setup(config, current=current)
    engine = make_engine(config, mongo_files(), mongo, app)

    op = await run_to_end(engine, engine.start(mongo_body()))
    assert op["status"] == "completed", op
    assert "app does not exist yet and will be created" in op["steps"][0]["detail"]
    assert mongo.databases["app"] == SNAPSHOT_DATA["app"]

    undo = await run_to_end(engine, engine.start_undo(op["id"], GuidedRestoreUndo(confirm="app")))
    assert undo["status"] == "completed", undo
    assert "the database did not exist before the restore" in undo["steps"][1]["detail"]
    assert "app" not in mongo.databases
    assert mongo.databases["other"] == CURRENT_DATA["other"]
