"""Standard cron weekday numbering, and databases excluded from backups."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from app.core.database import init_db, run_migrations
from app.models.discovery import DiscoveredContainer, DiscoveredDatabase
from app.services import db_exclusions
from app.utils.cron import crontab_trigger
from tests.conftest import auth_headers, do_setup

TZ = ZoneInfo("America/New_York")
SATURDAY = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)


def _next(expr: str, now: datetime = SATURDAY) -> datetime:
    return crontab_trigger(expr, timezone=TZ).get_next_fire_time(None, now)


class TestCrontabTrigger:
    @pytest.mark.parametrize(
        "expr,weekday",
        [
            ("0 7 * * 0", "Sunday"),  # APScheduler's from_crontab fired this on Mondays
            ("0 7 * * 7", "Sunday"),
            ("0 7 * * sun", "Sunday"),
            ("0 2 * * 1-5", "Monday"),  # weekdays: next after Saturday is Monday, not Tuesday
            ("0 2 * * mon-fri", "Monday"),
        ],
    )
    def test_weekdays_follow_cron(self, expr, weekday):
        assert _next(expr).strftime("%A") == weekday

    def test_saturday_is_six(self):
        # Midnight Saturday has passed at noon Saturday: next is the following Saturday.
        assert _next("0 0 * * 6").strftime("%A %d") == "Saturday 31"

    def test_steps_and_lists(self):
        # */2 in cron means Sun, Tue, Thu, Sat.
        days = set()
        now = SATURDAY
        for _ in range(4):
            now = _next("0 0 * * */2", now + timedelta(seconds=1))
            days.add(now.strftime("%a"))
        assert days == {"Sun", "Tue", "Thu", "Sat"}

    def test_unrestricted_fields_unchanged(self):
        assert _next("*/15 * * * *").minute in (0, 15, 30, 45)
        assert _next("0 6,18 * * *").hour == 18

    @pytest.mark.parametrize("expr", ["0 7 * *", "0 7 * * 5#3", "0 7 * * funday"])
    def test_invalid_or_unsupported(self, expr):
        with pytest.raises(ValueError):
            crontab_trigger(expr)


async def test_scheduler_registers_drill_on_sunday(tmp_path):
    from types import SimpleNamespace

    import app.services.scheduler as scheduler_mod

    config = SimpleNamespace(db_path=tmp_path / "arkive.db", config_dir=tmp_path, dump_dir=tmp_path / "dumps")
    await init_db(config.db_path)
    await run_migrations(config.db_path)
    sched = scheduler_mod.ArkiveScheduler(orchestrator=MagicMock(), config=config, restore_drill=MagicMock())
    await sched.reload_restore_drill_job()
    job = sched.scheduler.get_job(scheduler_mod.SYSTEM_JOB_RESTORE_DRILL)
    fire = job.trigger.get_next_fire_time(None, SATURDAY)
    assert fire.strftime("%A %H:%M") == "Sunday 07:00"


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

DROPBOX = {
    "container_name": "Dropbox",
    "db_type": "sqlite",
    "db_name": "nucleus.sqlite3",
    "host_path": "/mnt/user/appdata/dropbox/sync/nucleus.sqlite3",
}


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "arkive.db"
    await init_db(path)
    await run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        yield conn


async def test_exclusion_round_trip(db):
    assert await db_exclusions.read_exclusions(db) == []
    await db_exclusions.set_excluded(db, DROPBOX, True)
    await db_exclusions.set_excluded(db, DROPBOX, True)  # idempotent
    assert await db_exclusions.read_exclusions(db) == [DROPBOX]
    assert db_exclusions.identity(DiscoveredDatabase(**DROPBOX)) in await db_exclusions.excluded_identities(db)
    await db_exclusions.set_excluded(db, DROPBOX, False)
    assert await db_exclusions.read_exclusions(db) == []


async def test_identity_distinguishes_same_name_different_path(db):
    other = {**DROPBOX, "host_path": "/mnt/user/appdata/dropbox2/sync/nucleus.sqlite3"}
    await db_exclusions.set_excluded(db, DROPBOX, True)
    keys = await db_exclusions.excluded_identities(db)
    assert db_exclusions.identity(other) not in keys


async def test_corrupt_setting_reads_as_no_exclusions(db):
    await db.execute("INSERT INTO settings (key, value) VALUES ('excluded_databases', 'not json')")
    await db.commit()
    assert await db_exclusions.read_exclusions(db) == []


async def test_api_lists_and_toggles_exclusions(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    from app.core.dependencies import get_config

    async with aiosqlite.connect(get_config().db_path) as conn:
        await conn.execute(
            "INSERT INTO discovered_containers (name, image, status, databases) VALUES (?, ?, ?, ?)",
            ("Dropbox", "dropbox:latest", "running", json.dumps([DROPBOX])),
        )
        await conn.commit()

    items = (await client.get("/api/databases", headers=headers)).json()["items"]
    assert items[0]["excluded"] is False

    resp = await client.put("/api/databases/exclusions", headers=headers, json={**DROPBOX, "excluded": True})
    assert resp.status_code == 200, resp.text
    items = (await client.get("/api/databases", headers=headers)).json()["items"]
    assert items[0]["excluded"] is True

    resp = await client.put(
        "/api/databases/exclusions", headers=headers, json={**DROPBOX, "db_name": "other.db", "excluded": True}
    )
    assert resp.status_code == 404

    resp = await client.put("/api/databases/exclusions", headers=headers, json={**DROPBOX, "excluded": False})
    assert (await client.get("/api/databases", headers=headers)).json()["items"][0]["excluded"] is False
    async with aiosqlite.connect(get_config().db_path) as conn:
        cursor = await conn.execute("SELECT action FROM activity_log WHERE type = 'database' ORDER BY id")
        assert [r[0] for r in await cursor.fetchall()] == ["excluded", "included"]


async def test_status_ignores_excluded_databases(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    from app.core.dependencies import get_config

    pg = {"container_name": "pg", "db_type": "postgres", "db_name": "app", "host_path": None}
    async with aiosqlite.connect(get_config().db_path) as conn:
        await conn.execute(
            "INSERT INTO discovered_containers (name, image, status, databases) VALUES (?, ?, ?, ?)",
            ("mixed", "img", "running", json.dumps([pg, DROPBOX])),
        )
        await conn.execute("INSERT INTO backup_jobs (id, name, schedule, enabled) VALUES ('j', 'J', '0 1 * * *', 0)")
        await conn.execute("INSERT INTO job_runs (id, job_id, status) VALUES ('r', 'j', 'success')")
        await conn.execute(
            "INSERT INTO job_run_databases (run_id, container_name, db_type, db_name, status) "
            "VALUES ('r', 'pg', 'postgres', 'app', 'success')"
        )
        await conn.commit()
        await db_exclusions.set_excluded(conn, DROPBOX, True)

    body = (await client.get("/api/status", headers=headers)).json()
    assert body["databases"] == {"total": 1, "healthy": 1}


async def test_orchestrator_skips_excluded_databases(tmp_path):
    from app.core.config import ArkiveConfig
    from app.services.db_dumper import DumpResult
    from app.services.orchestrator import BackupOrchestrator

    config = ArkiveConfig(config_dir=tmp_path)
    config.ensure_dirs()
    await init_db(config.db_path)
    await run_migrations(config.db_path)
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES ('t1', 'L', 'local', 1, '{}')"
        )
        await conn.execute(
            "INSERT INTO backup_jobs (id, name, type, schedule, targets, directories) "
            "VALUES ('job', 'DB', 'db_dump', '0 2 * * *', '[]', '[]')"
        )
        await conn.commit()
        await db_exclusions.set_excluded(conn, DROPBOX, True)

    pg = DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app")
    containers = [
        DiscoveredContainer(name="pg", image="postgres:16", status="running", databases=[pg]),
        DiscoveredContainer(
            name="Dropbox", image="dropbox", status="running", databases=[DiscoveredDatabase(**DROPBOX)]
        ),
    ]
    dumper = MagicMock()
    dumper.dump_all = AsyncMock(
        return_value=[
            DumpResult(
                container_name="pg",
                db_type="postgres",
                db_name="app",
                dump_path="/x",
                dump_size_bytes=1,
                integrity_check="ok",
                status="success",
            )
        ]
    )
    engine = MagicMock()
    engine.init_repo = AsyncMock(return_value=True)
    engine.backup = AsyncMock(return_value={"status": "success", "snapshot_id": "abc", "total_bytes_processed": 1})
    engine.forget = AsyncMock(return_value={"status": "success"})
    engine.snapshots = AsyncMock(return_value=[])
    orch = BackupOrchestrator(
        discovery=MagicMock(scan=AsyncMock(return_value=containers)),
        db_dumper=dumper,
        flash_backup=MagicMock(),
        backup_engine=engine,
        cloud_manager=MagicMock(write_target_config=AsyncMock()),
        notifier=MagicMock(send=AsyncMock()),
        event_bus=MagicMock(publish=AsyncMock()),
        config=config,
    )
    orch._self_backup = AsyncMock()

    result = await orch.run_backup("job", run_id="r1")

    assert result["status"] == "success"
    assert [d.container_name for d in dumper.dump_all.await_args.args[0]] == ["pg"]
    async with aiosqlite.connect(config.db_path) as conn:
        rows = await (
            await conn.execute("SELECT container_name, status, error FROM job_run_databases ORDER BY container_name")
        ).fetchall()
        run = await (await conn.execute("SELECT databases_discovered, databases_dumped FROM job_runs")).fetchone()
    assert rows[0] == ("Dropbox", "skipped", "Excluded from backups on the Databases page")
    assert rows[1][:2] == ("pg", "success")
    assert run == (1, 1)


async def test_deleting_a_directory_removes_it_from_jobs(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    from app.core.dependencies import get_config

    async with aiosqlite.connect(get_config().db_path) as conn:
        await conn.execute(
            "INSERT INTO watched_directories (id, path, label) VALUES ('d1', '/mnt/user/appdata/gone', 'gone')"
        )
        await conn.execute(
            "INSERT INTO backup_jobs (id, name, schedule, directories) VALUES "
            "('j1', 'A', '0 1 * * *', '[\"d1\", \"d2\"]'), ('j2', 'B', '0 1 * * *', '[\"/mnt/user/appdata/gone\"]')"
        )
        await conn.commit()

    resp = await client.delete("/api/directories/d1", headers=headers)
    assert resp.status_code == 200

    async with aiosqlite.connect(get_config().db_path) as conn:
        rows = dict(
            await (await conn.execute("SELECT id, directories FROM backup_jobs WHERE id IN ('j1','j2')")).fetchall()
        )
    assert json.loads(rows["j1"]) == ["d2"]
    assert json.loads(rows["j2"]) == []
