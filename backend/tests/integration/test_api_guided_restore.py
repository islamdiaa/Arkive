"""Guided database restore API (/api/restore/database).

The engine on app.state is wired to fake database containers and a fake
backup engine (tests/fakes/fake_db_server.py); everything else is real: auth,
validation, the backup lock, the guided_restores table and the activity log.
"""

import json

import aiosqlite
import pytest

from app.services.db_dumper import DUMP_MANIFEST_NAME, dump_prefix
from app.services.guided_restore import GuidedRestoreEngine
from app.services.lock_manager import BackupLockManager
from tests.conftest import auth_headers, do_setup
from tests.fakes.fake_db_server import (
    FakeAppContainer,
    FakeDockerForRestore,
    FakePostgres,
    FakeSnapshotBackupEngine,
    pg_dump_text,
)

SNAPSHOT_ID = "abcd1234"
FULL_ID = SNAPSHOT_ID + "0" * 56
DUMP = f"{dump_prefix('pg', 'postgres', 'appdb')}_20261101_100000.sql"
SELECTION = {
    "container_name": "pg",
    "db_type": "postgres",
    "db_name": "appdb",
    "snapshot_id": SNAPSHOT_ID,
    "target_id": "t1",
}


def _app(client):
    return client._transport.app


@pytest.fixture
async def setup(client):
    data = await do_setup(client)
    app = _app(client)
    config = app.state.config
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, config) VALUES ('t1', 'Local NAS', 'local', '{}')"
        )
        await db.execute(
            "INSERT INTO snapshots (id, target_id, full_id, time, paths) VALUES (?, 't1', ?, ?, ?)",
            (SNAPSHOT_ID, FULL_ID, "2026-11-01T10:05:00Z", json.dumps([str(config.dump_dir)])),
        )
        uses_pg = json.dumps([{"container_name": "pg", "db_type": "postgres", "db_name": "appdb"}])
        await db.executemany(
            "INSERT INTO discovered_containers (name, image, status, databases) VALUES (?, 'img', 'running', ?)",
            [("pg", uses_pg), ("app-server", uses_pg)],
        )
        await db.commit()
    pg = FakePostgres(databases={"appdb": {"rows": ["current"]}})
    app_server = FakeAppContainer("app-server")
    files = {
        DUMP: pg_dump_text("appdb", ["from-snapshot"]),
        DUMP_MANIFEST_NAME: json.dumps(
            {
                "version": 1,
                "dumps": [
                    {
                        "file": DUMP,
                        "container_name": "pg",
                        "db_type": "postgres",
                        "db_name": "appdb",
                        "kind": "database",
                    }
                ],
            }
        ).encode(),
    }
    engine = GuidedRestoreEngine(
        config,
        backup_engine=FakeSnapshotBackupEngine(config.dump_dir, files, FULL_ID),
        docker_client=FakeDockerForRestore(pg, app_server),
    )
    engine.min_free_bytes = 0
    engine.progress_interval = 0.05
    app.state.guided_restore = engine
    return auth_headers(data["api_key"]), engine, pg, app_server


async def _finish(client, headers, engine, op_id):
    await engine._task
    resp = await client.get(f"/api/restore/database/{op_id}", headers=headers)
    assert resp.status_code == 200
    return resp.json()


async def test_requires_auth(client):
    await do_setup(client)
    for method, path in (
        ("get", "/api/restore/database"),
        ("get", "/api/restore/database/options"),
        ("post", "/api/restore/database/preview"),
        ("post", "/api/restore/database"),
        ("get", f"/api/restore/database/{'a' * 32}"),
        ("post", f"/api/restore/database/{'a' * 32}/undo"),
    ):
        resp = await getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 401, path


async def test_options_and_preview(client, setup):
    headers, _engine, pg, app_server = setup

    options = (await client.get("/api/restore/database/options", headers=headers)).json()["databases"]
    assert options[0]["container_name"] == "pg" and options[0]["method"] == "load"
    assert [d["name"] for d in options[0]["dependents"]] == ["app-server"]

    resp = await client.post("/api/restore/database/preview", json=SELECTION, headers=headers)
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["confirm_phrase"] == "appdb"
    assert preview["dump"]["file"] == DUMP
    assert preview["blockers"] == []
    assert pg.databases["appdb"]["rows"] == ["current"]  # preview changes nothing
    assert app_server.calls == []


async def test_preview_errors(client, setup):
    headers, engine, _pg, _app_server = setup
    resp = await client.post("/api/restore/database/preview", json={**SELECTION, "db_name": "nope"}, headers=headers)
    assert resp.status_code == 404
    # A snapshot ID that could be read as a restic option never gets that far.
    resp = await client.post(
        "/api/restore/database/preview", json={**SELECTION, "snapshot_id": "--password-command=x"}, headers=headers
    )
    assert resp.status_code == 422
    engine.backup_engine.files = {}
    resp = await client.post("/api/restore/database/preview", json=SELECTION, headers=headers)
    assert resp.status_code == 422
    assert "has no dump of pg/appdb" in resp.json()["message"]


async def test_restore_undo_and_history(client, setup):
    headers, engine, pg, app_server = setup

    resp = await client.post(
        "/api/restore/database",
        json={**SELECTION, "confirm": "wrong", "stop_containers": ["app-server"]},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "Type the database name (appdb)" in resp.json()["message"]

    resp = await client.post(
        "/api/restore/database",
        json={**SELECTION, "confirm": "appdb", "stop_containers": ["app-server"]},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    op_id = resp.json()["id"]
    op = await _finish(client, headers, engine, op_id)
    assert op["status"] == "completed", op
    assert pg.databases["appdb"]["rows"] == ["from-snapshot"]
    assert op["can_undo"] is True and op["safety_dump"]["available"] is True
    assert op["stopped_containers"] == ["app-server"] and app_server.status == "running"

    resp = await client.post(f"/api/restore/database/{op_id}/undo", json={"confirm": "x"}, headers=headers)
    assert resp.status_code == 400
    resp = await client.post(f"/api/restore/database/{op_id}/undo", json={"confirm": "appdb"}, headers=headers)
    assert resp.status_code == 202, resp.text
    undo = await _finish(client, headers, engine, resp.json()["id"])
    assert undo["status"] == "completed" and undo["parent_id"] == op_id
    assert pg.databases["appdb"]["rows"] == ["current"]

    history = (await client.get("/api/restore/database?limit=10", headers=headers)).json()
    assert history["total"] == 2
    assert [item["kind"] for item in history["items"]] == ["undo", "restore"]

    activity = (await client.get("/api/activity", headers=headers)).json()
    entries = activity.get("activities") or activity.get("items") or []
    assert any(a.get("action") == "database_restore_completed" for a in entries)


async def test_conflicts_and_not_found(client, setup):
    headers, _engine, _pg, _app_server = setup
    lock = BackupLockManager(_app(client).state.config.config_dir)
    assert lock.acquire_backup_lock("job-run")
    try:
        resp = await client.post("/api/restore/database", json={**SELECTION, "confirm": "appdb"}, headers=headers)
        assert resp.status_code == 409
    finally:
        lock.release_backup_lock()

    assert (await client.get(f"/api/restore/database/{'f' * 32}", headers=headers)).status_code == 404
    assert (await client.get("/api/restore/database/not-an-id", headers=headers)).status_code == 404
    resp = await client.post(f"/api/restore/database/{'f' * 32}/undo", json={"confirm": "appdb"}, headers=headers)
    assert resp.status_code == 404

    resp = await client.post(
        "/api/restore/database", json={**SELECTION, "confirm": "appdb", "stop_containers": ["pg"]}, headers=headers
    )
    assert resp.status_code == 400


async def test_engine_is_created_on_demand(client):
    data = await do_setup(client)
    app = _app(client)
    assert getattr(app.state, "guided_restore", None) is None
    resp = await client.get("/api/restore/database", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}
    assert isinstance(app.state.guided_restore, GuidedRestoreEngine)
    # Without Docker a load cannot run: the API says so instead of failing later.
    resp = await client.post(
        "/api/restore/database", json={**SELECTION, "confirm": "appdb"}, headers=auth_headers(data["api_key"])
    )
    assert resp.status_code == 404  # nothing discovered in this fresh instance
