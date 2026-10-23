"""Data-correctness regression tests for the prod-hardening review fixes.

Covers: rclone.conf section lifecycle and atomic/serialized writes, config
import secret merging + scheduler reload, stuck 'running' rows (verification
and restore), restore-test size cap and restore location, foreign-key
cascades, snapshot refresh error handling, run-log lookup, log tailing, SSE
subscription lifecycle, and the CLI restic environment.
"""

import asyncio
import json
import os
import sqlite3
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import yaml

from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.services.cloud_manager import CloudManager
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


def _app(client):
    return client._transport.app


def _attach_cloud_manager(client) -> CloudManager:
    app = _app(client)
    cm = CloudManager(app.state.config)
    app.state.cloud_manager = cm
    return cm


async def _create_local_target(client, api_key, tmp_path, name="Local"):
    path = tmp_path / f"backups-{name}"
    path.mkdir(parents=True, exist_ok=True)
    resp = await client.post(
        "/api/targets",
        json={"name": name, "type": "local", "config": {"path": str(path)}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# rclone.conf lifecycle
# ---------------------------------------------------------------------------


async def test_create_cloud_target_writes_rclone_section_and_delete_removes_it(client):
    data = await do_setup(client)
    cm = _attach_cloud_manager(client)
    resp = await client.post(
        "/api/targets",
        json={"name": "B2", "type": "b2", "config": {"key_id": "KID", "app_key": "AKEY", "bucket": "bkt"}},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 201, resp.text
    target_id = resp.json()["id"]
    content = cm.rclone_config.read_text()
    assert f"[{target_id}]" in content and "account = KID" in content
    assert stat.S_IMODE(os.stat(cm.rclone_config).st_mode) == 0o600

    resp = await client.delete(f"/api/targets/{target_id}", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    content = cm.rclone_config.read_text()
    assert f"[{target_id}]" not in content and "AKEY" not in content


async def test_setup_with_cloud_storage_writes_rclone_section(client):
    cm = _attach_cloud_manager(client)
    resp = await client.post("/api/auth/setup", json={
        "encryption_password": "test-password",
        "storage": {"type": "b2", "key_id": "KID", "app_key": "AKEY", "bucket": "bkt"},
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["target_created"] is True
    assert "account = KID" in cm.rclone_config.read_text()


async def test_oauth_complete_writes_rclone_section(client):
    from app.api.targets import _oauth_pending

    _oauth_pending.clear()
    data = await do_setup(client)
    cm = _attach_cloud_manager(client)
    start = await client.post("/api/targets/oauth/start", json={"provider": "dropbox", "client_id": "cid"},
                              headers=auth_headers(data["api_key"]))
    state = start.json()["state"]

    token_resp = MagicMock(status_code=200)
    token_resp.json.return_value = {"access_token": "tok", "refresh_token": "ref", "token_type": "bearer"}
    http_client = AsyncMock()
    http_client.post.return_value = token_resp
    http_client.__aenter__ = AsyncMock(return_value=http_client)
    http_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=http_client):
        resp = await client.post("/api/targets/oauth/complete",
                                 json={"provider": "dropbox", "code": "c", "state": state},
                                 headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200, resp.text
    target_id = resp.json()["id"]
    content = cm.rclone_config.read_text()
    assert f"[{target_id}]" in content and "type = dropbox" in content


async def test_concurrent_rclone_writes_do_not_lose_sections(tmp_path):
    cm = CloudManager(ArkiveConfig(config_dir=tmp_path))

    async def slow_obscure(*args, **kwargs):
        await asyncio.sleep(0.05)
        return MagicMock(returncode=0, stdout="OBS", stderr="")

    with patch("app.services.cloud_manager.run_command", side_effect=slow_obscure):
        await asyncio.gather(*[
            cm.write_target_config({
                "id": f"t{i}",
                "type": "sftp" if i % 2 else "b2",
                "config": {"host": "nas.local", "username": "u", "password": "p", "key_id": "k", "app_key": "a"},
            })
            for i in range(12)
        ])
    content = cm.rclone_config.read_text()
    for i in range(12):
        assert f"[t{i}]" in content
    # Idempotent: rewriting a section doesn't duplicate it.
    await cm.write_target_config({"id": "t0", "type": "b2", "config": {"key_id": "k2", "app_key": "a"}})
    content = cm.rclone_config.read_text()
    assert content.count("[t0]") == 1 and "account = k2" in content
    assert not list(tmp_path.glob(".rclone.conf.*"))  # no temp files left behind


# ---------------------------------------------------------------------------
# Config import
# ---------------------------------------------------------------------------


async def _post_yaml(client, api_key, doc):
    return await client.post(
        "/api/settings/import",
        content=yaml.dump(doc).encode(),
        headers={**auth_headers(api_key), "Content-Type": "application/x-yaml"},
    )


async def test_export_import_round_trip_keeps_target_secrets(client):
    data = await do_setup(client)
    api_key = data["api_key"]
    created = await client.post(
        "/api/targets",
        json={"name": "S3", "type": "s3",
              "config": {"endpoint": "https://s3.example.com", "access_key": "AKIA", "secret_key": "TOPSECRET",
                         "bucket": "bkt"}},
        headers=auth_headers(api_key),
    )
    target_id = created.json()["id"]
    exported = await client.get("/api/settings/export", headers=auth_headers(api_key))
    assert "TOPSECRET" not in exported.text

    resp = await client.post(
        "/api/settings/import",
        content=exported.text.encode(),
        headers={**auth_headers(api_key), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 200, resp.text

    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT config, status FROM storage_targets WHERE id = ?",
                                      (target_id,))).fetchone()
    assert row[0].startswith("enc:v1:")  # encrypted at rest
    cfg = decrypt_config(row[0], str(_app(client).state.config.config_dir))
    assert cfg["secret_key"] == "TOPSECRET"
    assert cfg["access_key"] == "AKIA"
    assert row[1] != "needs_credentials"


async def test_import_new_target_with_redacted_secret_flags_it(client):
    data = await do_setup(client)
    resp = await _post_yaml(client, data["api_key"], {"arkive_config": {"storage_targets": [
        {"id": "newb2", "name": "B2", "type": "b2",
         "config": {"key_id": "********", "app_key": "********", "bucket": "bkt"}},
        {"id": "bad-type", "name": "X", "type": "ftp", "config": {}},
        {"id": "inj", "name": "Inj", "type": "sftp", "config": {"host": "h\nssh = id", "username": "u"}},
    ]}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restored"]["storage_targets"] == 1
    warnings = " ".join(body["warnings"])
    assert "missing secrets" in warnings and "unsupported type 'ftp'" in warnings
    assert "control characters" in warnings

    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("SELECT id, status, config FROM storage_targets")).fetchall()
    assert [r[0] for r in rows] == ["newb2"]
    assert rows[0][1] == "needs_credentials"
    cfg = decrypt_config(rows[0][2], str(_app(client).state.config.config_dir))
    assert "app_key" not in cfg and cfg["bucket"] == "bkt"


async def test_import_reloads_scheduler_and_validates_jobs(client):
    data = await do_setup(client)
    scheduler = MagicMock()
    scheduler.remove_job = AsyncMock()
    scheduler.add_job = AsyncMock()
    _app(client).state.scheduler = scheduler
    jobs_before = (await client.get("/api/jobs", headers=auth_headers(data["api_key"]))).json()["items"]

    resp = await _post_yaml(client, data["api_key"], {"arkive_config": {"backup_jobs": [
        {"id": "imp1", "name": "Nightly", "type": "full", "schedule": "0 3 * * *", "enabled": True},
        {"id": "imp2", "name": "Bad cron", "type": "full", "schedule": "every night"},
        {"id": "imp3", "name": "Bad type", "type": "rm -rf", "schedule": "0 3 * * *"},
    ]}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["restored"]["backup_jobs"] == 1

    removed = {c.args[0] for c in scheduler.remove_job.await_args_list}
    assert removed == {j["id"] for j in jobs_before}
    added = [c.args[0]["id"] for c in scheduler.add_job.await_args_list]
    assert added == ["imp1"]


async def test_import_rejects_invalid_settings_values(client):
    data = await do_setup(client)
    resp = await _post_yaml(client, data["api_key"], {"arkive_config": {"settings": [
        {"key": "keep_daily", "value": "7d"},
        {"key": "theme", "value": "light"},
    ]}})
    assert resp.status_code == 200
    assert resp.json()["restored"]["settings"] == 1
    settings = await client.get("/api/settings", headers=auth_headers(data["api_key"]))
    assert settings.status_code == 200  # previously a bad keep_daily 500'd this endpoint


async def test_bulk_settings_rejects_non_scalar_values(client):
    data = await do_setup(client)
    for bad in (None, ["a"], {"x": 1}):
        resp = await client.put("/api/settings", json={"server_name": bad}, headers=auth_headers(data["api_key"]))
        assert resp.status_code in (200, 422), resp.text
        assert resp.status_code != 500
    resp = await client.put("/api/settings", json={"server_name": ["a"]}, headers=auth_headers(data["api_key"]))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Stuck 'running' rows
# ---------------------------------------------------------------------------


async def test_verification_skip_closes_running_rows(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    await _create_local_target(client, api_key, tmp_path)

    engine = MagicMock()
    engine.is_backup_running = MagicMock(return_value=False)
    engine.verify_all_targets = AsyncMock(return_value={"status": "skipped", "reason": "backup_in_progress"})
    _app(client).state.verify_engine = engine

    resp = await client.post("/api/verification/run", headers=auth_headers(api_key))
    assert resp.status_code == 200
    run_ids = resp.json()["run_ids"]
    for _ in range(50):
        await asyncio.sleep(0.02)
        async with aiosqlite.connect(_app(client).state.config.db_path) as db:
            rows = await (await db.execute("SELECT status, error_message FROM verification_runs")).fetchall()
        if all(r[0] != "running" for r in rows):
            break
    assert [r[0] for r in rows] == ["skipped"] * len(run_ids)
    assert "backup_in_progress" in rows[0][1]

    # A skipped run doesn't trip the 10-minute rate limit.
    again = await client.post("/api/verification/run", headers=auth_headers(api_key))
    assert again.status_code == 200


async def test_verification_refused_while_backup_running(client, tmp_path):
    data = await do_setup(client)
    await _create_local_target(client, data["api_key"], tmp_path)
    engine = MagicMock()
    engine.is_backup_running = MagicMock(return_value=True)
    _app(client).state.verify_engine = engine
    resp = await client.post("/api/verification/run", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 409
    async with aiosqlite.connect(_app(client).state.config.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM verification_runs")).fetchone())[0]
    assert count == 0


async def test_verify_engine_marks_preassigned_runs_when_skipping(tmp_path):
    from app.core.database import init_db, run_migrations
    from app.services.verify_engine import VerifyEngine

    config = ArkiveConfig(config_dir=tmp_path)
    await init_db(config.db_path)
    await run_migrations(config.db_path)
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO storage_targets (id, name, type) VALUES ('t1', 'T', 'local')")
        await db.execute("INSERT INTO verification_runs (id, target_id, status) VALUES ('r1', 't1', 'running')")
        await db.commit()

    engine = VerifyEngine(config, backup_engine=MagicMock())
    with patch.object(engine, "is_backup_running", return_value=True):
        result = await engine.verify_all_targets(run_ids={"t1": "r1"})
    assert result["status"] == "skipped"
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status, completed_at FROM verification_runs")).fetchone()
    assert row[0] == "skipped" and row[1]


async def test_restore_conflict_leaves_no_running_row(client, tmp_path):
    from app.services.orchestrator import _get_proc_start_time

    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_local_target(client, api_key, tmp_path)
    config_dir = Path(os.environ["ARKIVE_CONFIG_DIR"])
    lock = config_dir / "backup.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "proc_start_time": _get_proc_start_time(os.getpid())}))
    try:
        resp = await client.post(
            "/api/restore",
            json={"target": target_id, "snapshot_id": "abc123", "paths": ["/data"],
                  "restore_to": str(config_dir / "restores" / "x")},
            headers=auth_headers(api_key),
        )
    finally:
        lock.unlink(missing_ok=True)
    assert resp.status_code == 409
    async with aiosqlite.connect(_app(client).state.config.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM restore_runs")).fetchone())[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Restore test: size cap, persistent location, off-loop hashing
# ---------------------------------------------------------------------------


async def test_restore_test_skips_oversized_files_and_uses_restore_dir(client, tmp_path):
    from app.api.restore import MAX_RESTORE_TEST_BYTES

    data = await do_setup(client)
    api_key = data["api_key"]
    target_id = await _create_local_target(client, api_key, tmp_path)
    restore_root = str(_app(client).state.config.restore_dir)
    restored_into = {}

    async def fake_restore(target, snapshot_id, paths, restore_to):
        restored_into["dir"] = restore_to
        Path(restore_to, "small.txt").write_text("hello")
        return {"status": "success"}

    engine = MagicMock()
    engine.ls = AsyncMock(return_value=[
        {"name": "huge.img", "type": "file", "size": MAX_RESTORE_TEST_BYTES + 1},
        {"name": "small.txt", "type": "file", "size": 5},
    ])
    engine.restore = AsyncMock(side_effect=fake_restore)
    _app(client).state.backup_engine = engine

    resp = await client.post("/api/restore/test", json={"snapshot_id": "abc123", "target_id": target_id},
                             headers=auth_headers(api_key))
    body = resp.json()
    assert body["status"] == "success", body
    assert body["file"] == "/small.txt" and body["size_bytes"] == 5
    assert restored_into["dir"].startswith(restore_root + "/arkive-test-")
    assert not os.path.exists(restored_into["dir"])  # cleaned up


async def test_restore_test_refuses_explicit_oversized_file(client, tmp_path):
    from app.api.restore import MAX_RESTORE_TEST_BYTES

    data = await do_setup(client)
    target_id = await _create_local_target(client, data["api_key"], tmp_path)
    engine = MagicMock()
    engine.ls = AsyncMock(return_value=[{"name": "vm.img", "type": "file", "size": MAX_RESTORE_TEST_BYTES * 4}])
    engine.restore = AsyncMock()
    _app(client).state.backup_engine = engine
    resp = await client.post("/api/restore/test",
                             json={"snapshot_id": "abc123", "target_id": target_id, "path": "/mnt/vm.img"},
                             headers=auth_headers(data["api_key"]))
    assert resp.json()["status"] == "failed"
    assert "too large" in resp.json()["error"]
    engine.restore.assert_not_awaited()


async def test_verify_engine_sqlite_check_is_strict(tmp_path):
    from app.services.verify_engine import VerifyEngine

    engine = VerifyEngine(ArkiveConfig(config_dir=tmp_path))
    corrupt = MagicMock(returncode=0, stdout="row 12 missing from index idx_bookmarks\n", stderr="")
    healthy = MagicMock(returncode=0, stdout="ok\n", stderr="")
    with patch("app.services.verify_engine.run_command", AsyncMock(return_value=corrupt)):
        assert (await engine._check_sqlite_dump("/x.db"))["status"] == "failed"
    with patch("app.services.verify_engine.run_command", AsyncMock(return_value=healthy)):
        assert (await engine._check_sqlite_dump("/x.db"))["status"] == "success"


async def test_verify_engine_restore_test_uses_restore_dir_and_size_cap(tmp_path):
    from app.services.verify_engine import MAX_RESTORE_TEST_BYTES, VerifyEngine

    config = ArkiveConfig(config_dir=tmp_path)
    backup_engine = MagicMock()
    backup_engine.snapshots = AsyncMock(return_value=[{"short_id": "abcd1234"}])
    backup_engine.ls = AsyncMock(return_value=[
        {"name": "big.bin", "type": "file", "size": MAX_RESTORE_TEST_BYTES + 1},
        {"name": "a.txt", "type": "file", "size": 3},
    ])
    seen = {}

    async def fake_restore(target, snapshot_id, paths, restore_to):
        seen["dir"], seen["paths"] = restore_to, paths
        Path(restore_to, "a.txt").write_text("abc")
        return {"status": "success"}

    backup_engine.restore = AsyncMock(side_effect=fake_restore)
    result = await VerifyEngine(config, backup_engine)._run_restore_test({"id": "t1"})
    assert result["status"] == "success"
    assert seen["paths"] == ["/a.txt"]
    assert seen["dir"].startswith(str(config.restore_dir))


# ---------------------------------------------------------------------------
# Foreign-key cascades
# ---------------------------------------------------------------------------


async def test_delete_job_cascades_run_history(client):
    data = await do_setup(client)
    api_key = data["api_key"]
    job_id = (await client.get("/api/jobs", headers=auth_headers(api_key))).json()["items"][0]["id"]
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO job_runs (id, job_id, status) VALUES ('r1', ?, 'success')", (job_id,))
        await db.execute("INSERT INTO job_run_databases (run_id, container_name, db_type) VALUES ('r1', 'c', 'pg')")
        await db.commit()
    resp = await client.delete(f"/api/jobs/{job_id}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    async with aiosqlite.connect(db_path) as db:
        runs = (await (await db.execute("SELECT COUNT(*) FROM job_runs")).fetchone())[0]
        dbs = (await (await db.execute("SELECT COUNT(*) FROM job_run_databases")).fetchone())[0]
    assert runs == 0 and dbs == 0


async def test_deleted_target_no_longer_counts_toward_trust_score(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    keep = await _create_local_target(client, api_key, tmp_path, name="Keep")
    drop = await _create_local_target(client, api_key, tmp_path, name="Drop")
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO verification_runs (id, target_id, status, trust_score) "
                         "VALUES ('v1', ?, 'passed', 90)", (keep,))
        await db.execute("INSERT INTO verification_runs (id, target_id, status, trust_score) "
                         "VALUES ('v2', ?, 'failed', 10)", (drop,))
        await db.commit()
    assert (await client.get("/api/verification", headers=auth_headers(api_key))).json()["trust_score"] == 50

    await client.delete(f"/api/targets/{drop}", headers=auth_headers(api_key))
    body = (await client.get("/api/verification", headers=auth_headers(api_key))).json()
    assert body["trust_score"] == 90
    assert [r["target_id"] for r in body["results"]] == [keep]


async def test_running_verification_does_not_hide_last_score(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    target = await _create_local_target(client, api_key, tmp_path)
    async with aiosqlite.connect(_app(client).state.config.db_path) as db:
        await db.execute("INSERT INTO verification_runs (id, target_id, status, trust_score, started_at) "
                         "VALUES ('v1', ?, 'passed', 80, '2026-01-01T00:00:00Z')", (target,))
        await db.execute("INSERT INTO verification_runs (id, target_id, status, trust_score, started_at) "
                         "VALUES ('v2', ?, 'running', 0, '2026-01-02T00:00:00Z')", (target,))
        await db.commit()
    status = (await client.get("/api/status", headers=auth_headers(api_key))).json()
    assert status["trust_score"] == 80
    assert status["verification_status"]["verification_passing"] is True


# ---------------------------------------------------------------------------
# Snapshot refresh, run logs, misc input handling
# ---------------------------------------------------------------------------


async def test_refresh_snapshots_keeps_cache_when_listing_returns_nothing(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    target = await _create_local_target(client, api_key, tmp_path)
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO snapshots (id, target_id, full_id, time) VALUES ('s1', ?, 'f1', 't')",
                         (target,))
        await db.execute("UPDATE storage_targets SET snapshot_count = 1, total_size_bytes = 500 WHERE id = ?",
                         (target,))
        await db.commit()
    engine = MagicMock()
    engine.snapshots = AsyncMock(return_value=[])  # what a restic error looks like
    _app(client).state.backup_engine = engine
    resp = await client.post("/api/snapshots/refresh", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert resp.json()["skipped"][0]["target_id"] == target
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT snapshot_count, total_size_bytes FROM storage_targets WHERE id = ?",
                                      (target,))).fetchone()
        snaps = (await (await db.execute("SELECT COUNT(*) FROM snapshots")).fetchone())[0]
    assert tuple(row) == (1, 500) and snaps == 1


async def test_refresh_snapshots_prunes_forgotten_snapshots(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    target = await _create_local_target(client, api_key, tmp_path)
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO snapshots (id, target_id, full_id, time) VALUES ('old1', ?, 'f', 't')",
                         (target,))
        await db.commit()
    engine = MagicMock()
    engine.snapshots = AsyncMock(return_value=[{"id": "new1full", "short_id": "new1", "time": "t", "size": 7}])
    _app(client).state.backup_engine = engine
    await client.post("/api/snapshots/refresh", headers=auth_headers(api_key))
    async with aiosqlite.connect(db_path) as db:
        ids = [r[0] for r in await (await db.execute("SELECT id FROM snapshots")).fetchall()]
    assert ids == ["new1"]


async def test_run_logs_found_for_old_runs(client):
    data = await do_setup(client)
    api_key = data["api_key"]
    job_id = (await client.get("/api/jobs", headers=auth_headers(api_key))).json()["items"][0]["id"]
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO job_runs (id, job_id, status) VALUES ('oldrun', ?, 'success')", (job_id,))
        await db.execute(
            "INSERT INTO activity_log (type, action, message, details, timestamp) "
            "VALUES ('backup', 'run_started', 'old run', ?, '2020-01-01T00:00:00Z')",
            (json.dumps({"run_id": "oldrun"}),),
        )
        await db.execute("INSERT INTO activity_log (type, action, message, details) "
                         "VALUES ('system', 'x', 'broken', 'not json')")
        await db.executemany(
            "INSERT INTO activity_log (type, action, message, details) VALUES ('system', 'noise', 'n', '{}')",
            [() for _ in range(2500)],
        )
        await db.commit()
    resp = await client.get("/api/jobs/runs/oldrun/logs?limit=10", headers=auth_headers(api_key))
    assert resp.status_code == 200
    assert [e["message"] for e in resp.json()["items"]] == ["old run"]
    assert (await client.get("/api/jobs/runs/oldrun/logs?limit=100000",
                             headers=auth_headers(api_key))).status_code == 422


async def test_activity_rejects_negative_limit(client):
    data = await do_setup(client)
    resp = await client.get("/api/activity?limit=-1", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 422


async def test_update_directory_to_duplicate_path_is_409(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    await client.post("/api/directories", json={"path": str(a), "label": "A"}, headers=auth_headers(api_key))
    rb = await client.post("/api/directories", json={"path": str(b), "label": "B"}, headers=auth_headers(api_key))
    resp = await client.put(f"/api/directories/{rb.json()['id']}", json={"path": str(a), "label": "B"},
                            headers=auth_headers(api_key))
    assert resp.status_code == 409


async def test_notifications_list_survives_undecryptable_url(client):
    data = await do_setup(client)
    async with aiosqlite.connect(_app(client).state.config.db_path) as db:
        await db.execute(
            "INSERT INTO notification_channels (id, type, name, config, events) VALUES (?, ?, ?, ?, ?)",
            ("ch1", "webhook", "Foreign", json.dumps({"url": "enc:v1:not-a-valid-token"}), "[]"),
        )
        await db.commit()
    resp = await client.get("/api/notifications", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    assert resp.json()["items"][0]["config"]["url"] == ""


# ---------------------------------------------------------------------------
# Logs: tail read off the event loop
# ---------------------------------------------------------------------------


async def test_logs_reads_only_the_tail(client, monkeypatch):
    import app.api.logs as logs_mod

    data = await do_setup(client)
    log_dir = _app(client).state.config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"timestamp": f"t{i:05d}", "level": "INFO", "component": "c", "message": f"m{i}"})
             for i in range(2000)]
    (log_dir / "arkive.log").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(logs_mod, "MAX_SCAN_BYTES", 10_000)
    monkeypatch.setattr(logs_mod._read_log_tail, "__defaults__", (10_000,))
    resp = await client.get("/api/logs?limit=5", headers=auth_headers(data["api_key"]))
    body = resp.json()
    assert [e["message"] for e in body["items"]] == ["m1999", "m1998", "m1997", "m1996", "m1995"]
    assert body["truncated"] is True and body["has_more"] is True
    assert body["total"] < 2000


# ---------------------------------------------------------------------------
# SSE: no subscription leaks when the generator never starts
# ---------------------------------------------------------------------------


async def test_event_stream_subscribes_lazily():
    from app.api.events import event_stream

    bus = MagicMock()
    response = await event_stream(event_bus=bus)
    bus.subscribe.assert_not_called()  # nothing to leak if the response is cancelled now
    gen = response.body_iterator
    handle = MagicMock()
    handle.queue.get = AsyncMock(return_value={"event": "x", "data": {}})
    bus.subscribe.return_value = handle
    first = await gen.__anext__()
    assert first["event"] == "x"
    await gen.aclose()
    handle.cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# CLI restic environment
# ---------------------------------------------------------------------------


def test_cli_restic_env_uses_engine_repo_and_encryption_password(tmp_path):
    import cli
    from app.core.database import SCHEMA_SQL
    from app.core.security import _load_fernet_from_dir, _reset_fernet, encrypt_config, encrypt_value

    _reset_fernet()
    _load_fernet_from_dir(str(tmp_path))
    db_path = tmp_path / "arkive.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO settings (key, value, encrypted) VALUES ('encryption_password', ?, 1)",
                 (encrypt_value("restic-pass"),))
    conn.execute("INSERT INTO storage_targets (id, name, type, config) VALUES ('sf1', 'S', 'sftp', ?)",
                 (encrypt_config({"host": "h", "username": "u", "password": "sftp-login", "remote_path": "bk"},
                                 str(tmp_path)),))
    conn.commit()
    conn.close()
    try:
        env = cli._get_target_env("sf1", db_path)
    finally:
        _reset_fernet()
    assert env["RESTIC_PASSWORD"] == "restic-pass"
    assert env["RESTIC_REPOSITORY"] == "rclone:sf1:bk/arkive-backups"
    assert env["RCLONE_CONFIG"] == str(tmp_path / "rclone.conf")
