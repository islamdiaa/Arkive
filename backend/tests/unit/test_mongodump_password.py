"""The backup dumper gives mongodump its password on stdin, not in argv.

The fake MongoDB (tests/fakes/fake_mongo_redis.py) streams a real archive,
logs in only with the right password and, like the legacy 4.2 tools, rejects
``--config`` when ``legacy_tools`` is set.
"""

import json
import logging
import os
from unittest.mock import MagicMock

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper
from app.services.dump_checks import read_mongo_archive
from tests.fakes.fake_db_server import FakeDockerForRestore
from tests.fakes.fake_mongo_redis import FakeMongo, with_docker_api

PASSWORD = 'p@ss w"rd $1'
DATA = {"app": {"notes": [{"_id": i, "t": "x" * 20} for i in range(50)]}}


def make_dumper(tmp_path, container):
    config = MagicMock()
    config.dump_dir = tmp_path / "dumps"
    return DBDumper(FakeDockerForRestore(container), config)


def mongo_db():
    return DiscoveredDatabase(container_name="mongo", db_type="mongodb", db_name="admin", host_path=None)


def argv_of(mongo) -> list[list[str]]:
    return [e["cmd"] for e in mongo.client.api.execs.values()] + [c["cmd"] for c in mongo.calls]


def published(tmp_path) -> list[str]:
    return sorted(os.listdir(tmp_path / "dumps"))


def test_password_goes_to_stdin_and_the_dump_is_complete(tmp_path):
    mongo = with_docker_api(FakeMongo(databases=DATA, password=PASSWORD))
    dumper = make_dumper(tmp_path, mongo)

    result = dumper._dump_mongodb_blocking(mongo_db())

    assert result.status == "success", result.error
    dump_cmd = next(e for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongodump")
    assert dump_cmd["cmd"] == [
        "mongodump",
        "--archive",
        "--username",
        "root",
        "--config=/dev/stdin",
        "--authenticationDatabase",
        "admin",
    ]
    assert dump_cmd["stdin"] is True
    assert PASSWORD not in json.dumps(argv_of(mongo))
    # Published atomically under its final name, no .partial left, and a complete archive.
    assert published(tmp_path) == [os.path.basename(result.dump_path)]
    archive = read_mongo_archive(result.dump_path)
    assert {c.name: c.documents for c in archive.counted(["app"])} == {"notes": 50}
    assert result.dump_size_bytes == os.path.getsize(result.dump_path)


def test_wrong_password_fails_and_leaves_no_file(tmp_path):
    mongo = with_docker_api(FakeMongo(databases=DATA, password=PASSWORD))
    mongo.attrs["Config"]["Env"] = ["MONGO_INITDB_ROOT_USERNAME=root", "MONGO_INITDB_ROOT_PASSWORD=rotated"]
    dumper = make_dumper(tmp_path, mongo)

    result = dumper._dump_mongodb_blocking(mongo_db())

    assert result.status == "failed"
    assert "exited with code 1" in result.error and "Authentication failed" in result.error
    assert published(tmp_path) == []


def test_legacy_tools_fall_back_to_argv_with_a_warning(tmp_path, caplog):
    mongo = with_docker_api(FakeMongo(databases=DATA, password=PASSWORD))
    mongo.legacy_tools = True
    dumper = make_dumper(tmp_path, mongo)

    with caplog.at_level(logging.WARNING, logger="arkive.db_dumper"):
        result = dumper._dump_mongodb_blocking(mongo_db())

    assert result.status == "success", result.error
    dump_cmd = next(e["cmd"] for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongodump")
    assert "--config=/dev/stdin" not in dump_cmd
    assert dump_cmd[dump_cmd.index("--password") + 1] == PASSWORD
    assert any("no --config option" in r.getMessage() for r in caplog.records)
    assert read_mongo_archive(result.dump_path).counted(["app"])


def test_no_login_sends_no_config(tmp_path):
    mongo = with_docker_api(FakeMongo(databases=DATA, user=None, password=None))
    dumper = make_dumper(tmp_path, mongo)

    result = dumper._dump_mongodb_blocking(mongo_db())

    assert result.status == "success", result.error
    dump_cmd = next(e for e in mongo.client.api.execs.values() if e["cmd"][0] == "mongodump")
    assert dump_cmd["cmd"] == ["mongodump", "--archive"] and dump_cmd["stdin"] is False
    assert not [c for c in mongo.calls if c["cmd"][:2] == ["mongodump", "--help"]]


def test_failed_dump_after_reading_the_password_keeps_the_previous_one_intact(tmp_path):
    mongo = with_docker_api(FakeMongo(databases=DATA, password=PASSWORD))
    dumper = make_dumper(tmp_path, mongo)
    first = dumper._dump_mongodb_blocking(mongo_db())
    assert first.status == "success"

    mongo.missing = {"mongodump"}
    second = dumper._dump_mongodb_blocking(mongo_db())

    assert second.status == "failed"
    assert published(tmp_path) == [os.path.basename(first.dump_path)]
