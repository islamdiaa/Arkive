"""API: GET /api/databases/health and POST /api/databases/health/accept-size."""

import json

import aiosqlite
import pytest

from app.core.dependencies import get_config
from app.services.db_dumper import dump_prefix
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024
KEY = dump_prefix("immich-db", "postgres", "immich")


async def _seed(client, sizes: list[int]) -> str:
    """Setup, one discovered Postgres database and one uploaded run per size."""
    api_key = (await do_setup(client))["api_key"]
    async with aiosqlite.connect(get_config().db_path) as db:
        await db.execute(
            "INSERT INTO discovered_containers (name, image, status, databases) VALUES (?, ?, ?, ?)",
            (
                "immich-db",
                "postgres:16",
                "running",
                json.dumps(
                    [{"container_name": "immich-db", "db_type": "postgres", "db_name": "immich", "host_path": None}]
                ),
            ),
        )
        await db.execute("INSERT INTO backup_jobs (id, name, schedule) VALUES ('job', 'Job', '0 2 * * *')")
        await db.execute("INSERT INTO storage_targets (id, name, type) VALUES ('t1', 'Local', 'local')")
        for day, size in enumerate(sizes, start=1):
            run_id = f"r{day}"
            await db.execute(
                "INSERT INTO job_runs (id, job_id, status, started_at, completed_at) VALUES (?, 'job', 'success', ?, ?)",
                (run_id, f"2026-11-{day:02d}T02:00:00Z", f"2026-11-{day:02d}T02:10:00Z"),
            )
            await db.execute(
                """INSERT INTO job_run_databases
                   (run_id, container_name, db_type, db_name, dump_size_bytes, status, host_path, db_key, dumped_at)
                   VALUES (?, 'immich-db', 'postgres', 'immich', ?, 'success', ?, ?, ?)""",
                (
                    run_id,
                    size,
                    f"/config/dumps/{KEY}_202611{day:02d}_020100.sql",
                    KEY,
                    f"2026-11-{day:02d}T02:01:00Z",
                ),
            )
            await db.execute(
                "INSERT INTO job_run_targets (run_id, target_id, status, snapshot_id) VALUES (?, 't1', 'success', ?)",
                (run_id, f"snap{day}"),
            )
        await db.commit()
    return api_key


async def test_health_requires_auth(client):
    await do_setup(client)
    resp = await client.get("/api/databases/health")
    assert resp.status_code == 401


async def test_health_lists_each_discovered_database(client):
    api_key = await _seed(client, [100 * MIB, 101 * MIB, 102 * MIB])

    resp = await client.get("/api/databases/health", headers=auth_headers(api_key))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["key"] == KEY
    assert item["status"] == "ok"
    assert item["last_success_at"] == "2026-11-03T02:01:00Z"
    assert item["size_bytes"] == 102 * MIB
    assert len(item["trend"]) == 3
    assert item["restorable_as_of"] == "2026-11-03T02:01:00Z"
    assert item["last_drill"] is None
    assert body["policy"] == {
        "shrink_alert_percent": 50,
        "shrink_min_mb": 1,
        "baseline_dumps": 5,
        "min_baseline_dumps": 3,
    }


async def test_database_list_carries_the_health_key(client):
    api_key = await _seed(client, [])
    resp = await client.get("/api/databases", headers=auth_headers(api_key))
    assert resp.json()["items"][0]["key"] == KEY


async def test_accept_size_clears_the_shrink_warning(client):
    api_key = await _seed(client, [200 * MIB] * 4 + [20 * MIB])
    headers = auth_headers(api_key)
    item = (await client.get("/api/databases/health", headers=headers)).json()["items"][0]
    assert item["status"] == "warning" and item["shrunk"] is True

    resp = await client.post("/api/databases/health/accept-size", json={"key": KEY}, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"key": KEY, "accepted_from": "2026-11-05T02:01:00Z"}
    item = (await client.get("/api/databases/health", headers=headers)).json()["items"][0]
    assert item["status"] == "ok" and item["shrunk"] is False
    activity = (await client.get("/api/activity", headers=headers)).json()
    entries = activity.get("items") or activity.get("activities") or []
    assert any(e["action"] == "size_accepted" for e in entries)


async def test_accept_size_unknown_database(client):
    api_key = await _seed(client, [])
    resp = await client.post("/api/databases/health/accept-size", json={"key": KEY}, headers=auth_headers(api_key))
    assert resp.status_code == 404


async def test_shrink_settings_are_validated_and_returned(client):
    api_key = (await do_setup(client))["api_key"]
    headers = auth_headers(api_key)

    resp = await client.put("/api/settings", json={"dump_shrink_alert_percent": "96"}, headers=headers)
    assert resp.status_code == 422
    resp = await client.put(
        "/api/settings", json={"dump_shrink_alert_percent": "30", "dump_shrink_min_mb": "0"}, headers=headers
    )
    assert resp.status_code == 200

    body = (await client.get("/api/settings", headers=headers)).json()
    assert body["dump_shrink_alert_percent"] == 30
    assert body["dump_shrink_min_mb"] == 0
