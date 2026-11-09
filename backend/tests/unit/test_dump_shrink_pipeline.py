"""A dump that shrinks sharply makes the backup run partial; every dump record
carries the database identity and dump time (database health history)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from app.models.discovery import DiscoveredContainer, DiscoveredDatabase
from app.services.db_dumper import DBDumper, DumpResult, dump_prefix

MIB = 1024 * 1024
KEY = dump_prefix("pg", "postgres", "app")


@pytest_asyncio.fixture
async def config(tmp_path):
    from app.core.config import ArkiveConfig
    from app.core.database import init_db, run_migrations

    cfg = ArkiveConfig(config_dir=tmp_path)
    cfg.ensure_dirs()
    await init_db(cfg.db_path)
    await run_migrations(cfg.db_path)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES ('t1', 'Local', 'local', 1, '{}')"
        )
        await db.execute(
            """INSERT INTO backup_jobs (id, name, type, schedule, targets, directories)
               VALUES ('job', 'Databases', 'db_dump', '0 2 * * *', '[]', '[]')"""
        )
        # Not about disk space: don't depend on the free space where tmp_path is.
        await db.execute("INSERT INTO settings (key, value) VALUES ('min_disk_space_bytes', '0')")
        await db.commit()
    return cfg


def _db():
    return DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app")


def _dump(size: int, ts: str) -> DumpResult:
    return DumpResult(
        container_name="pg",
        db_type="postgres",
        db_name="app",
        dump_path=f"/config/dumps/{KEY}_{ts}.sql",
        dump_size_bytes=size,
        integrity_check="ok",
        status="success",
        db_key=KEY,
        started_at=f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z",
    )


def make_orchestrator(config, dumps: list[DumpResult]):
    from app.services.orchestrator import BackupOrchestrator

    discovery = MagicMock()
    discovery.scan = AsyncMock(
        return_value=[DiscoveredContainer(name="pg", image="postgres:16", status="running", databases=[_db()])]
    )
    dumper = MagicMock()
    dumper.dump_all = AsyncMock(side_effect=[[d] for d in dumps])
    backup_engine = MagicMock()
    backup_engine.init_repo = AsyncMock(return_value=True)
    backup_engine.backup = AsyncMock(
        return_value={"status": "success", "snapshot_id": "abc", "total_bytes_processed": 1}
    )
    backup_engine.forget = AsyncMock(return_value={"status": "success", "output": ""})
    backup_engine.snapshots = AsyncMock(return_value=[{"id": "abc" * 10, "short_id": "abc", "size": 1}])
    orch = BackupOrchestrator(
        discovery=discovery,
        db_dumper=dumper,
        flash_backup=MagicMock(backup=AsyncMock(return_value=SimpleNamespace(status="success", size_bytes=1))),
        backup_engine=backup_engine,
        cloud_manager=MagicMock(write_target_config=AsyncMock()),
        notifier=MagicMock(send=AsyncMock()),
        event_bus=MagicMock(publish=AsyncMock()),
        config=config,
    )
    orch._self_backup = AsyncMock()
    orch.discovery_retry_seconds = 0
    return orch


async def _run(config, run_id):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        return dict(await cursor.fetchone())


@pytest.mark.asyncio
async def test_sharp_dump_shrink_makes_the_run_partial(config):
    sizes = [200 * MIB, 201 * MIB, 202 * MIB, 30 * MIB]
    orch = make_orchestrator(config, [_dump(size, f"2026110{i + 1}_020000") for i, size in enumerate(sizes)])

    for i in range(3):
        assert (await orch.run_backup("job", run_id=f"r{i}"))["status"] == "success"
    result = await orch.run_backup("job", run_id="r3")

    assert result["status"] == "partial"
    run = await _run(config, "r3")
    assert "pg/app: dump is 85% smaller than its recent median" in run["error_message"]
    assert orch.notifier.send.await_args.args[0] == "backup.partial"
    # The dump itself succeeded and stays the newest restore point.
    assert (run["databases_dumped"], run["databases_failed"]) == (1, 0)


@pytest.mark.asyncio
async def test_disabled_shrink_check_keeps_the_run_successful(config):
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('dump_shrink_alert_percent', '0')")
        await db.commit()
    sizes = [200 * MIB, 200 * MIB, 200 * MIB, 1 * MIB]
    orch = make_orchestrator(config, [_dump(size, f"2026110{i + 1}_020000") for i, size in enumerate(sizes)])

    statuses = [(await orch.run_backup("job", run_id=f"r{i}"))["status"] for i in range(4)]

    assert statuses == ["success"] * 4


@pytest.mark.asyncio
async def test_dump_records_carry_identity_and_dump_time(config):
    excluded = DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="scratch")
    orch = make_orchestrator(config, [_dump(5 * MIB, "20261101_020000")])
    orch.discovery.scan = AsyncMock(
        return_value=[
            DiscoveredContainer(name="pg", image="postgres:16", status="running", databases=[_db(), excluded])
        ]
    )
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('excluded_databases', ?)",
            ('[{"container_name": "pg", "db_type": "postgres", "db_name": "scratch"}]',),
        )
        await db.commit()

    await orch.run_backup("job", run_id="r1")

    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute(
            "SELECT db_name, status, db_key, dumped_at FROM job_run_databases WHERE run_id = 'r1' ORDER BY db_name"
        )
        rows = await cursor.fetchall()
    assert rows == [
        ("app", "success", KEY, "2026-11-01T02:00:00Z"),
        ("scratch", "skipped", dump_prefix("pg", "postgres", "scratch"), None),
    ]


@pytest.mark.asyncio
async def test_size_check_failure_does_not_fail_the_backup(config, monkeypatch):
    import app.services.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "shrink_warnings", AsyncMock(side_effect=RuntimeError("boom")))
    orch = make_orchestrator(config, [_dump(5 * MIB, "20261101_020000")])

    assert (await orch.run_backup("job", run_id="r1"))["status"] == "success"


@pytest.mark.asyncio
async def test_dump_all_sets_identity_and_start_time(tmp_path, monkeypatch):
    from app.core.config import ArkiveConfig

    cfg = ArkiveConfig(config_dir=tmp_path)
    cfg.ensure_dirs()
    dumper = DBDumper(MagicMock(), cfg)
    sqlite_db = DiscoveredDatabase(container_name="app", db_type="sqlite", db_name="app.db", host_path="/mnt/a/app.db")

    async def fake_dump_one(db):
        return DumpResult(
            container_name=db.container_name,
            db_type=db.db_type,
            db_name=db.db_name,
            dump_path="",
            dump_size_bytes=0,
            integrity_check="skipped",
            status="failed",
            error="boom",
        )

    monkeypatch.setattr(dumper, "_dump_one", fake_dump_one)
    monkeypatch.setattr(dumper, "_update_manifest", AsyncMock())

    [result] = await dumper.dump_all([sqlite_db])

    assert result.db_key == dump_prefix("app", "sqlite", "app.db", "/mnt/a/app.db")
    assert result.started_at and result.started_at.endswith("Z")
