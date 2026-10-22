"""Backup freshness watchdog, heartbeat, recovery kit, keyfile guard,
read-only restore destinations, legacy snapshot host names, and the
notification event catalog."""

import json
import logging
import logging.config
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from cryptography.fernet import Fernet

import app.services.scheduler as scheduler_mod
from app.core.database import init_db, run_migrations
from app.core.security import ALLOW_NEW_KEYFILE_ENV, KeyfileError, verify_keyfile
from app.services import freshness
from app.services.backup_engine import BackupEngine, ResticError
from app.utils import mounts
from app.utils.subprocess_runner import CommandResult
from tests.conftest import auth_headers, do_setup

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Keyfile guard
# ---------------------------------------------------------------------------


def _db_with_encrypted_value(path: Path, key: bytes | None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, encrypted INTEGER, updated_at TEXT)")
    if key is not None:
        token = Fernet(key).encrypt(b"restic-pass").decode()
        conn.execute("INSERT INTO settings VALUES ('encryption_password', ?, 1, '')", (f"enc:v1:{token}",))
    conn.commit()
    conn.close()


class TestKeyfileGuard:
    def test_fresh_install_without_keyfile_is_fine(self, tmp_path):
        _db_with_encrypted_value(tmp_path / "arkive.db", None)
        verify_keyfile(tmp_path, tmp_path / "arkive.db")
        assert not (tmp_path / ".keyfile").exists()

    def test_no_database_is_fine(self, tmp_path):
        verify_keyfile(tmp_path, tmp_path / "missing.db")

    def test_missing_keyfile_with_encrypted_data_refuses(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_NEW_KEYFILE_ENV, raising=False)
        _db_with_encrypted_value(tmp_path / "arkive.db", Fernet.generate_key())
        with pytest.raises(KeyfileError, match="missing.*1 values encrypted"):
            verify_keyfile(tmp_path, tmp_path / "arkive.db")

    def test_override_allows_missing_keyfile(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOW_NEW_KEYFILE_ENV, "1")
        _db_with_encrypted_value(tmp_path / "arkive.db", Fernet.generate_key())
        verify_keyfile(tmp_path, tmp_path / "arkive.db")

    def test_wrong_keyfile_refuses(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_NEW_KEYFILE_ENV, raising=False)
        _db_with_encrypted_value(tmp_path / "arkive.db", Fernet.generate_key())
        (tmp_path / ".keyfile").write_bytes(Fernet.generate_key())
        with pytest.raises(KeyfileError, match="does not match"):
            verify_keyfile(tmp_path, tmp_path / "arkive.db")

    def test_matching_keyfile_passes(self, tmp_path):
        key = Fernet.generate_key()
        _db_with_encrypted_value(tmp_path / "arkive.db", key)
        (tmp_path / ".keyfile").write_bytes(key)
        verify_keyfile(tmp_path, tmp_path / "arkive.db")

    def test_one_foreign_value_does_not_block_startup(self, tmp_path):
        key = Fernet.generate_key()
        _db_with_encrypted_value(tmp_path / "arkive.db", key)
        conn = sqlite3.connect(tmp_path / "arkive.db")
        foreign = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
        conn.execute("INSERT INTO settings VALUES ('aaa_imported', ?, 1, '')", (f"enc:v1:{foreign}",))
        conn.commit()
        conn.close()
        (tmp_path / ".keyfile").write_bytes(key)
        verify_keyfile(tmp_path, tmp_path / "arkive.db")

    def test_corrupt_keyfile_refuses(self, tmp_path):
        _db_with_encrypted_value(tmp_path / "arkive.db", None)
        (tmp_path / ".keyfile").write_text("not-a-key")
        with pytest.raises(KeyfileError, match="corrupt"):
            verify_keyfile(tmp_path, tmp_path / "arkive.db")

    def test_notification_channel_url_is_checked(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ALLOW_NEW_KEYFILE_ENV, raising=False)
        key = Fernet.generate_key()
        conn = sqlite3.connect(tmp_path / "arkive.db")
        conn.execute("CREATE TABLE notification_channels (id TEXT, config TEXT)")
        token = Fernet(key).encrypt(b"https://hooks.example.com/x").decode()
        conn.execute("INSERT INTO notification_channels VALUES ('c1', ?)",
                     (json.dumps({"url": f"enc:v1:{token}"}),))
        conn.commit()
        conn.close()
        (tmp_path / ".keyfile").write_bytes(Fernet.generate_key())
        with pytest.raises(KeyfileError, match="does not match"):
            verify_keyfile(tmp_path, tmp_path / "arkive.db")


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestScheduleGap:
    @pytest.mark.parametrize("schedule,expected", [
        ("0 7 * * *", 24),
        ("0 6,18 * * *", 12),
        ("0 3 * * 1-5", 72),       # Friday -> Monday
        ("0 7 * * 0", 168),
        ("*/30 * * * *", 0.5),
    ])
    def test_longest_gap(self, schedule, expected):
        assert freshness.schedule_gap_hours(schedule, NOW) == pytest.approx(expected, abs=1.01)

    def test_sparse_schedules_still_have_a_gap(self):
        assert freshness.schedule_gap_hours("0 0 1 1 *", NOW) == pytest.approx(365 * 24, abs=25)
        assert freshness.schedule_gap_hours("0 3 1 * *", NOW) == pytest.approx(31 * 24, abs=25)

    def test_impossible_schedule(self):
        assert freshness.schedule_gap_hours("0 0 30 2 *", NOW) is None

    def test_invalid_schedule(self):
        assert freshness.schedule_gap_hours("not a cron", NOW) is None

    def test_threshold_adds_grace(self):
        assert freshness.threshold_hours_for("0 7 * * *", 0, NOW) == pytest.approx(30, abs=1.01)
        assert freshness.threshold_hours_for("*/30 * * * *", 0, NOW) == pytest.approx(2.5, abs=0.1)
        assert freshness.threshold_hours_for("0 7 * * *", 48, NOW) == 48


@pytest.fixture
async def fresh_db(tmp_path):
    path = tmp_path / "arkive.db"
    await init_db(path)
    await run_migrations(path)
    async with aiosqlite.connect(path) as db:
        yield db


async def _add_job(db, job_id="j1", name="DB Dumps", job_type="db_dump", schedule="0 7 * * *",
                   created=NOW - timedelta(days=30), include_databases=1):
    await db.execute(
        "INSERT INTO backup_jobs (id, name, type, schedule, include_databases, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, name, job_type, schedule, include_databases, _ts(created)),
    )
    await db.commit()


async def _add_run(db, run_id, started, status="success", job_id="j1", dumped=3, target_status="success"):
    await db.execute(
        "INSERT INTO job_runs (id, job_id, status, started_at, databases_dumped) VALUES (?, ?, ?, ?, ?)",
        (run_id, job_id, status, _ts(started), dumped),
    )
    if target_status:
        await db.execute(
            "INSERT INTO job_run_targets (run_id, target_id, status) VALUES (?, 't1', ?)",
            (run_id, target_status),
        )
    await db.commit()


class TestEvaluateFreshness:
    async def test_recent_success_is_fresh_and_healthy(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=13))
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is False and result["healthy"] is True
        assert result["jobs"][0]["last_good_at"] == _ts(NOW - timedelta(hours=13))

    async def test_old_success_is_stale(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=40))
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is True and result["healthy"] is False
        stale = result["stale_jobs"][0]
        assert stale["job_name"] == "DB Dumps"
        assert stale["overdue_hours"] == pytest.approx(10, abs=1.1)

    async def test_never_ran_is_stale_after_threshold_from_creation(self, fresh_db):
        await _add_job(fresh_db, created=NOW - timedelta(days=3))
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale_jobs"][0]["last_good_at"] is None

    async def test_new_job_is_not_stale_yet(self, fresh_db):
        await _add_job(fresh_db, created=NOW - timedelta(hours=2))
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is False

    async def test_partial_without_databases_is_not_good_when_dbs_existed(self, fresh_db):
        """The original silent failure: runs kept 'completing' with zero databases."""
        await _add_job(fresh_db)
        await _add_run(fresh_db, "old", NOW - timedelta(days=5), dumped=40)
        await _add_run(fresh_db, "r2", NOW - timedelta(hours=10), status="partial", dumped=0)
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is True
        assert result["jobs"][0]["last_run_bad"] is True

    async def test_partial_with_databases_and_snapshot_counts(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=10), status="partial", dumped=40)
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is False and result["healthy"] is True

    async def test_partial_without_snapshot_is_not_good(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=40))
        await _add_run(fresh_db, "r2", NOW - timedelta(hours=10), status="partial", target_status="failed")
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is True

    async def test_flash_job_partial_counts_without_databases(self, fresh_db):
        await _add_job(fresh_db, job_type="flash", include_databases=1)
        await _add_job(fresh_db, job_id="j0", name="Old", job_type="db_dump")
        await _add_run(fresh_db, "old", NOW - timedelta(days=9), job_id="j0", dumped=4)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=10), status="partial", dumped=0)
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        flash = next(j for j in result["jobs"] if j["job_id"] == "j1")
        assert flash["stale"] is False

    async def test_failed_latest_run_is_unhealthy_but_not_stale(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=20))
        await _add_run(fresh_db, "r2", NOW - timedelta(hours=1), status="failed", target_status=None)
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is False
        assert result["healthy"] is False

    async def test_disabled_jobs_are_ignored_but_no_jobs_is_not_healthy(self, fresh_db):
        await _add_job(fresh_db)
        await fresh_db.execute("UPDATE backup_jobs SET enabled = 0")
        await fresh_db.commit()
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["jobs"] == [] and result["stale"] is False
        assert result["healthy"] is False  # nothing is backed up: no heartbeat

    async def test_snapshot_with_unreadable_files_counts(self, fresh_db):
        """restic exit 3 stores the target as partial; the snapshot still counts."""
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=10), status="partial", target_status="partial")
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is False and result["healthy"] is True

    async def test_never_dumped_with_databases_discovered_is_not_good(self, fresh_db):
        """New install: databases discovered, every dump fails, files upload fine."""
        await _add_job(fresh_db, created=NOW - timedelta(days=3))
        await fresh_db.execute(
            "INSERT INTO job_runs (id, job_id, status, started_at, databases_discovered, databases_dumped) "
            "VALUES ('r1', 'j1', 'partial', ?, 4, 0)", (_ts(NOW - timedelta(hours=5)),))
        await fresh_db.execute("INSERT INTO job_run_targets (run_id, target_id, status) VALUES ('r1', 't1', 'success')")
        await fresh_db.commit()
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is True and result["healthy"] is False

    async def test_system_without_databases_is_fine(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=10), status="partial", dumped=0)
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is False

    async def test_override_setting(self, fresh_db):
        await _add_job(fresh_db)
        await _add_run(fresh_db, "r1", NOW - timedelta(hours=13))
        await fresh_db.execute("INSERT INTO settings (key, value) VALUES ('stale_backup_hours', '12')")
        await fresh_db.commit()
        result = await freshness.evaluate_freshness(fresh_db, NOW)
        assert result["stale"] is True
        assert result["stale_jobs"][0]["threshold_hours"] == 12

    def test_describe_stale_job(self):
        text = freshness.describe_stale_job(
            {"job_name": "DB Dumps", "last_good_at": None, "threshold_hours": 15.0})
        assert text == "DB Dumps has never produced a good backup (expected within 15 h)."


class TestFreshnessJob:
    def _scheduler(self, tmp_path, notifier):
        config = SimpleNamespace(db_path=tmp_path / "arkive.db", config_dir=tmp_path, dump_dir=tmp_path / "dumps")
        sched = scheduler_mod.ArkiveScheduler(orchestrator=MagicMock(), config=config, notifier=notifier)
        return sched

    async def _seed(self, path, stale: bool, heartbeat_url: str = "https://kuma.example.com/api/push/x"):
        await init_db(path)
        await run_migrations(path)
        async with aiosqlite.connect(path) as db:
            now = datetime.now(timezone.utc)
            await _add_job(db, created=now - timedelta(days=30))
            await _add_run(db, "r1", now - timedelta(hours=40 if stale else 2))
            await db.execute("INSERT INTO settings (key, value) VALUES ('heartbeat_url', ?)", (heartbeat_url,))
            await db.commit()

    async def test_stale_job_notifies_once_and_withholds_heartbeat(self, tmp_path, monkeypatch):
        await self._seed(tmp_path / "arkive.db", stale=True)
        ping = AsyncMock(return_value={"ok": True, "status_code": 200, "error": None})
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", ping)
        notifier = MagicMock()
        notifier.send = AsyncMock()
        sched = self._scheduler(tmp_path, notifier)

        await sched._run_freshness_check()
        await sched._run_freshness_check()

        assert notifier.send.await_count == 1
        args, kwargs = notifier.send.await_args
        assert args[0] == "backup.stale" and kwargs["severity"] == "error" and kwargs["job_id"] == "j1"
        ping.assert_not_awaited()
        async with aiosqlite.connect(tmp_path / "arkive.db") as db:
            cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE type='backup' AND action='stale'")
            assert (await cursor.fetchone())[0] == 1

    async def test_reminder_after_a_day(self, tmp_path, monkeypatch):
        await self._seed(tmp_path / "arkive.db", stale=True)
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        notifier = MagicMock()
        notifier.send = AsyncMock()
        sched = self._scheduler(tmp_path, notifier)
        await sched._run_freshness_check()
        sched._stale_alerted["j1"] -= scheduler_mod.STALE_REMINDER_SECONDS + 1
        await sched._run_freshness_check()
        assert notifier.send.await_count == 2

    async def test_healthy_sends_heartbeat_and_records_recovery(self, tmp_path, monkeypatch):
        await self._seed(tmp_path / "arkive.db", stale=False)
        ping = AsyncMock(return_value={"ok": True, "status_code": 200, "error": None})
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", ping)
        sched = self._scheduler(tmp_path, None)
        sched._stale_alerted["j1"] = 0.0
        await sched._run_freshness_check()
        ping.assert_awaited_once_with("https://kuma.example.com/api/push/x")
        assert sched._stale_alerted == {}
        async with aiosqlite.connect(tmp_path / "arkive.db") as db:
            cursor = await db.execute("SELECT message FROM activity_log WHERE action='fresh'")
            assert "again" in (await cursor.fetchone())[0]

    async def test_no_url_no_ping(self, tmp_path, monkeypatch):
        await self._seed(tmp_path / "arkive.db", stale=False, heartbeat_url="")
        ping = AsyncMock()
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", ping)
        await self._scheduler(tmp_path, None)._run_freshness_check()
        ping.assert_not_awaited()

    def test_registered_as_system_job(self, tmp_path):
        sched = self._scheduler(tmp_path, None)
        sched._register_system_jobs()
        job = sched.scheduler.get_job(scheduler_mod.SYSTEM_JOB_FRESHNESS)
        assert job is not None and job.name == "Backup Freshness"


async def test_ping_heartbeat_reports_http_errors(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    result = await freshness.ping_heartbeat("https://kuma.example.com/api/push/x")
    assert result == {"ok": False, "status_code": 503, "error": "HTTP 503"}


async def test_ping_heartbeat_never_raises():
    result = await freshness.ping_heartbeat("http://127.0.0.1:9/unreachable", timeout=2)
    assert result["ok"] is False and result["error"]


# ---------------------------------------------------------------------------
# Status, recovery kit, heartbeat test endpoint (API)
# ---------------------------------------------------------------------------


def _app(client):
    return client._transport.app


async def test_status_reports_stale_jobs_as_error(client):
    data = await do_setup(client)
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM backup_jobs")
        await _add_job(db, created=datetime.now(timezone.utc) - timedelta(days=5))
    resp = await client.get("/api/status", headers=auth_headers(data["api_key"]))
    body = resp.json()
    assert body["status"] == "error"
    assert body["backup_freshness"]["stale"] is True
    assert body["backup_freshness"]["stale_jobs"][0]["job_name"] == "DB Dumps"


async def test_status_unauthenticated_payload_has_no_freshness(client):
    await do_setup(client)
    body = (await client.get("/api/status")).json()
    assert "backup_freshness" not in body and "recovery_kit_confirmed" not in body


async def test_recovery_kit_flow(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    db_path = _app(client).state.config.db_path
    from app.core.security import encrypt_config
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, config) VALUES ('b2t', 'Offsite', 'b2', ?)",
            (encrypt_config({"bucket": "my-bucket", "key_id": "KID", "app_key": "SECRET-APP-KEY"}),),
        )
        await db.commit()

    status = (await client.get("/api/status", headers=headers)).json()
    assert status["recovery_kit_confirmed"] is False

    resp = await client.post("/api/settings/recovery-kit", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    kit = resp.json()
    assert kit["encryption_password"] == "test-password"
    assert kit["config_keyfile"]
    offsite = next(t for t in kit["targets"] if t["id"] == "b2t")
    assert offsite["repository"] == "rclone:b2t:my-bucket/arkive-backups"
    assert offsite["details"] == {"bucket": "my-bucket"}
    assert "SECRET-APP-KEY" not in kit["text"] and "KID" not in kit["text"]
    assert "test-password" in kit["text"] and "rclone config" in kit["text"]

    resp = await client.post("/api/settings/recovery-kit/confirm", headers=headers)
    assert resp.status_code == 200
    status = (await client.get("/api/status", headers=headers)).json()
    assert status["recovery_kit_confirmed"] is True

    # A password changed after the confirmation needs a new kit.
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE settings SET updated_at = '2999-01-01T00:00:00Z' WHERE key = 'encryption_password'")
        await db.commit()
    status = (await client.get("/api/status", headers=headers)).json()
    assert status["recovery_kit_confirmed"] is False


async def test_recovery_kit_requires_auth(client):
    await do_setup(client)
    resp = await client.post("/api/settings/recovery-kit")
    assert resp.status_code == 401


async def test_heartbeat_test_endpoint(client, monkeypatch):
    import app.api.settings as settings_api
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    resp = await client.post("/api/settings/heartbeat/test", headers=headers)
    assert resp.status_code == 422

    ping = AsyncMock(return_value={"ok": True, "status_code": 200, "error": None})
    monkeypatch.setattr(settings_api, "ping_heartbeat", ping)
    resp = await client.post("/api/settings/heartbeat/test", headers=headers,
                             json={"url": "https://hc-ping.example.com/abc"})
    assert resp.json()["ok"] is True
    ping.assert_awaited_once_with("https://hc-ping.example.com/abc")

    resp = await client.post("/api/settings/heartbeat/test", headers=headers, json={"url": "ftp://x"})
    assert resp.status_code == 422


@pytest.mark.parametrize("key,value,ok", [
    ("heartbeat_url", "https://kuma.example.com/api/push/abc?status=up", True),
    ("heartbeat_url", "javascript:alert(1)", False),
    ("stale_backup_hours", "26", True),
    ("stale_backup_hours", "-1", False),
    ("dump_compression", "gzip", True),
    ("dump_compression", "zip", False),
    ("restore_drill_enabled", "false", True),
    ("restore_drill_enabled", "maybe", False),
    ("restore_drill_schedule", "0 7 * * 0", True),
    ("restore_drill_schedule", "every sunday", False),
    ("restore_drill_max_databases", "0", False),
])
async def test_new_settings_validation(client, key, value, ok):
    data = await do_setup(client)
    resp = await client.put("/api/settings", json={"settings": {key: value}},
                            headers=auth_headers(data["api_key"]))
    assert (resp.status_code == 200) is ok, resp.text


async def test_drill_settings_change_reloads_job(client):
    data = await do_setup(client)
    scheduler = MagicMock()
    scheduler.reload_restore_drill_job = AsyncMock()
    _app(client).state.scheduler = scheduler
    await client.put("/api/settings", json={"settings": {"restore_drill_schedule": "0 8 * * 6"}},
                     headers=auth_headers(data["api_key"]))
    scheduler.reload_restore_drill_job.assert_awaited_once()
    settings = (await client.get("/api/settings", headers=auth_headers(data["api_key"]))).json()
    assert settings["restore_drill_schedule"] == "0 8 * * 6"
    assert settings["restore_drill_enabled"] is True
    assert settings["dump_compression"] == "none"


async def test_restore_into_read_only_mount_is_rejected(client, monkeypatch):
    import app.api.restore as restore_api
    data = await do_setup(client)
    db_path = _app(client).state.config.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO storage_targets (id, name, type, config) VALUES ('t1', 'L', 'local', '{}')")
        await db.commit()
    _app(client).state.backup_engine = MagicMock()
    config_dir = str(_app(client).state.config.config_dir)
    monkeypatch.setattr(restore_api, "read_only_mount_for",
                        lambda p: config_dir if p.startswith(config_dir) else None)
    resp = await client.post(
        "/api/restore", headers=auth_headers(data["api_key"]),
        json={"target": "t1", "snapshot_id": "abc123", "paths": ["/mnt/user/appdata/app"]},
    )
    assert resp.status_code == 400
    assert "mounted read-only" in resp.text
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM restore_runs")
        assert (await cursor.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# Notification events
# ---------------------------------------------------------------------------


async def test_notification_event_catalog(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    body = (await client.get("/api/notifications/events", headers=headers)).json()
    values = {e["value"] for e in body["events"]}
    assert {"backup.stale", "drill.failed", "verification.failed", "target.error"} <= values
    assert set(body["defaults"]) <= values

    resp = await client.post("/api/notifications", headers=headers, json={
        "name": "n", "type": "ntfy", "url": "ntfy://topic", "events": ["discovery.completed"]})
    assert resp.status_code == 422

    resp = await client.post("/api/notifications", headers=headers, json={
        "name": "n", "type": "ntfy", "url": "ntfy://topic"})
    assert resp.status_code == 201, resp.text
    channels = (await client.get("/api/notifications", headers=headers)).json()["items"]
    assert "backup.stale" in channels[0]["events"]


async def test_migration_backfills_new_events(tmp_path):
    path = tmp_path / "arkive.db"
    await init_db(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO notification_channels (id, type, name, events) VALUES "
            "('a', 'ntfy', 'A', '[\"backup.failed\"]'), ('b', 'ntfy', 'B', '[\"backup.success\"]')")
        await db.commit()
    await run_migrations(path)
    async with aiosqlite.connect(path) as db:
        rows = dict(await (await db.execute("SELECT id, events FROM notification_channels")).fetchall())
    assert json.loads(rows["a"]) == ["backup.failed", "backup.stale", "drill.failed",
                                     "verification.failed", "target.error"]
    assert json.loads(rows["b"]) == ["backup.success"]


# ---------------------------------------------------------------------------
# Mount helpers
# ---------------------------------------------------------------------------

CID = "0f" * 32
MOUNTINFO = f"""\
100 90 0:40 / / rw,relatime - overlay overlay rw
101 100 0:42 /containers/{CID}/hostname /etc/hostname rw,noatime - btrfs /dev/loop2 rw
102 100 0:50 /appdata/arkive /config rw,noatime - shfs shfs rw
103 100 0:50 / /mnt/user ro,noatime - shfs shfs rw
104 103 0:50 /backups /mnt/user/backups rw,noatime - shfs shfs rw
"""


class TestMountHelpers:
    @pytest.fixture
    def mountinfo(self, tmp_path):
        path = tmp_path / "mountinfo"
        path.write_text(MOUNTINFO)
        return path

    def test_read_only_detection(self, mountinfo):
        assert mounts.read_only_mount_for("/mnt/user/appdata/x", mountinfo) == "/mnt/user"
        assert mounts.read_only_mount_for("/mnt/user/backups/arkive", mountinfo) is None
        assert mounts.read_only_mount_for("/config/restores/r1", mountinfo) is None

    def test_own_container_id(self, mountinfo):
        assert mounts.own_container_id(mountinfo) == CID

    def test_host_path_for(self, mountinfo):
        docker = MagicMock()
        docker.containers.get.return_value.attrs = {"Mounts": [
            {"Type": "bind", "Source": "/mnt/user/appdata/arkive", "Destination": "/config"},
            {"Type": "bind", "Source": "/mnt/user/appdata/arkive/local-backup", "Destination": "/data"},
        ]}
        assert mounts.host_path_for("/data/arkive-repo", docker, mountinfo) == \
            "/mnt/user/appdata/arkive/local-backup/arkive-repo"
        assert mounts.host_path_for("/config", docker, mountinfo) == "/mnt/user/appdata/arkive"
        assert mounts.host_path_for("/elsewhere", docker, mountinfo) is None
        docker.containers.get.assert_called_with(CID)

    def test_host_path_for_without_docker(self, mountinfo):
        assert mounts.host_path_for("/data", None, mountinfo) is None
        broken = MagicMock()
        broken.containers.get.side_effect = RuntimeError("down")
        assert mounts.host_path_for("/data", broken, mountinfo) is None


# ---------------------------------------------------------------------------
# Snapshot listing errors and legacy host names
# ---------------------------------------------------------------------------


def _engine(tmp_path):
    engine = BackupEngine(SimpleNamespace(rclone_config=tmp_path / "rclone.conf", db_path=tmp_path / "x.db"))
    engine._get_password = AsyncMock(return_value="pw")
    return engine


class TestLegacyHostnames:
    async def test_list_snapshots_raises_on_failure(self, tmp_path, monkeypatch):
        import app.services.backup_engine as be
        monkeypatch.setattr(be, "run_command", AsyncMock(return_value=CommandResult(
            returncode=1, stdout="", stderr="unable to open repository", duration_seconds=0.0, command="restic")))
        engine = _engine(tmp_path)
        with pytest.raises(ResticError, match="unable to open"):
            await engine.list_snapshots({"type": "local", "config": {"path": "/data"}})
        assert await engine.snapshots({"type": "local", "config": {"path": "/data"}}) == []

    async def test_relabels_only_arkive_container_id_snapshots(self, tmp_path, monkeypatch):
        import app.services.backup_engine as be
        listing = [
            {"id": "a" * 64, "hostname": "0123456789ab", "paths": ["/config/dumps", "/mnt/user/appdata"]},
            {"id": "b" * 64, "hostname": "tower", "paths": ["/config/dumps"]},
            {"id": "c" * 64, "hostname": "abcdef012345", "paths": ["/home/other"]},
            {"id": "d" * 64, "hostname": "laptop", "paths": ["/config/dumps"]},
            # Another Arkive instance sharing the repository: same shape, not ours.
            {"id": "e" * 64, "hostname": "fedcba987654", "paths": ["/config/dumps"]},
        ]
        calls = []

        async def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1] == "snapshots":
                return CommandResult(returncode=0, stdout=json.dumps(listing), stderr="", duration_seconds=0.0, command="restic")
            return CommandResult(returncode=0, stdout="", stderr="", duration_seconds=0.0, command="restic")

        monkeypatch.setattr(be, "run_command", fake_run)
        engine = _engine(tmp_path)
        engine.current_hostname = AsyncMock(return_value="tower")
        owned = {"a" * 64, "b" * 64, "c" * 8}
        result = await engine.normalize_legacy_hostnames(
            {"type": "local", "config": {"path": "/data"}}, "/config/dumps", owned)
        assert result == {"status": "success", "rewritten": 1, "host": "tower"}
        assert "e" * 64 not in calls[-1]
        rewrite = calls[-1]
        assert rewrite[:2] == ["restic", "rewrite"]
        assert rewrite[rewrite.index("--new-host") + 1] == "tower"
        assert "--forget" in rewrite and rewrite[-1] == "a" * 64

    async def test_skips_when_current_host_is_a_container_id(self, tmp_path):
        engine = _engine(tmp_path)
        engine.current_hostname = AsyncMock(return_value="0123456789ab")
        result = await engine.normalize_legacy_hostnames({"type": "local", "config": {}}, "/config/dumps", set())
        assert result["status"] == "skipped"

    async def test_retention_relabels_before_forget(self, tmp_path):
        config = SimpleNamespace(db_path=tmp_path / "arkive.db", config_dir=tmp_path, dump_dir=tmp_path / "dumps")
        await init_db(config.db_path)
        await run_migrations(config.db_path)
        async with aiosqlite.connect(config.db_path) as db:
            await db.execute("INSERT INTO storage_targets (id, name, type, config) VALUES ('t1', 'L', 'local', '{}')")
            await db.execute("INSERT INTO backup_jobs (id, name, schedule) VALUES ('j1', 'J', '0 1 * * *')")
            await db.execute("INSERT INTO job_runs (id, job_id, status) VALUES ('r1', 'j1', 'success')")
            await db.execute("INSERT INTO job_run_targets (run_id, target_id, status, snapshot_id) "
                             "VALUES ('r1', 't1', 'success', 'deadbeef')")
            await db.commit()
        order = []
        engine = MagicMock()
        engine.normalize_legacy_hostnames = AsyncMock(side_effect=lambda *a: order.append("relabel") or {"status": "success"})
        engine.forget = AsyncMock(side_effect=lambda *a, **k: order.append("forget") or {"status": "success"})
        sched = scheduler_mod.ArkiveScheduler(orchestrator=MagicMock(), config=config, backup_engine=engine)
        await sched._run_retention_cleanup()
        assert order == ["relabel", "forget"]
        engine.normalize_legacy_hostnames.assert_awaited_once()
        assert engine.normalize_legacy_hostnames.await_args.args[1] == str(config.dump_dir)
        assert engine.normalize_legacy_hostnames.await_args.args[2] == {"deadbeef"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_uvicorn_log_config_is_json(tmp_path, capsys):
    config_path = Path(__file__).resolve().parents[2] / "logging.json"
    logging.config.dictConfig(json.loads(config_path.read_text()))
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "Started server process [1]"
    assert payload["component"] == "uvicorn.error"


def test_setup_logging_routes_uvicorn_through_root(tmp_path):
    from app.utils.log_config import setup_logging
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.addHandler(logging.StreamHandler())
    uvicorn_logger.propagate = False
    setup_logging(tmp_path)
    try:
        assert uvicorn_logger.handlers == [] and uvicorn_logger.propagate is True
        assert logging.getLogger("uvicorn.access").propagate is True
    finally:
        logging.getLogger().handlers.clear()
