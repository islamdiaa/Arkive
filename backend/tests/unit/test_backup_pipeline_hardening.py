"""Backup pipeline behaviour that previously produced silent "success" runs.

Covers: Docker/discovery failures, job-type semantics, scheduled lock queueing,
missing backup paths, database-count regressions, stopped DB containers,
restic partial snapshots and runs without storage targets.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from app.models.discovery import DiscoveredContainer, DiscoveredDatabase
from app.services.db_dumper import DumpResult
from app.services.docker_client import DockerUnavailableError


@pytest_asyncio.fixture
async def config(tmp_path):
    from app.core.config import ArkiveConfig
    from app.core.database import init_db

    cfg = ArkiveConfig(config_dir=tmp_path)
    cfg.ensure_dirs()
    await init_db(cfg.db_path)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES ('t1', 'Local', 'local', 1, '{}')"
        )
        for job_id, job_type in (("job-full", "full"), ("job-db", "db_dump"), ("job-flash", "flash")):
            await db.execute(
                """INSERT INTO backup_jobs (id, name, type, schedule, targets, directories)
                   VALUES (?, ?, ?, '0 2 * * *', '[]', '[]')""",
                (job_id, job_id, job_type),
            )
        await db.commit()
    return cfg


def _container(name, databases, status="running"):
    return DiscoveredContainer(name=name, image="img", status=status, databases=databases)


def _db(container, db_type="postgres", name="app"):
    return DiscoveredDatabase(container_name=container, db_type=db_type, db_name=name)


def _ok_dump(db):
    return DumpResult(
        container_name=db.container_name, db_type=db.db_type, db_name=db.db_name,
        dump_path="/x", dump_size_bytes=10, integrity_check="ok", status="success",
    )


def make_orchestrator(config, *, discovery=None, db_dumper=None, backup_result=None, flash_status="success"):
    from app.services.orchestrator import BackupOrchestrator

    backup_engine = MagicMock()
    backup_engine.init_repo = AsyncMock(return_value=True)
    backup_engine.backup = AsyncMock(return_value=backup_result or {
        "status": "success", "snapshot_id": "abc", "total_bytes_processed": 1024,
    })
    backup_engine.forget = AsyncMock(return_value={"status": "success", "output": ""})
    backup_engine.snapshots = AsyncMock(return_value=[{"id": "abc" * 10, "short_id": "abc", "size": 1}])

    flash = MagicMock()
    flash.backup = AsyncMock(return_value=SimpleNamespace(status=flash_status, size_bytes=5, error=None))

    orch = BackupOrchestrator(
        discovery=discovery,
        db_dumper=db_dumper,
        flash_backup=flash,
        backup_engine=backup_engine,
        cloud_manager=MagicMock(write_target_config=AsyncMock()),
        notifier=MagicMock(send=AsyncMock()),
        event_bus=MagicMock(publish=AsyncMock()),
        config=config,
    )
    orch._self_backup = AsyncMock()
    orch.discovery_retry_seconds = 0
    orch.lock_poll_seconds = 0.05
    return orch


async def _run_row(config, run_id):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        return dict(await cursor.fetchone())


@pytest.mark.asyncio
async def test_docker_unavailable_marks_run_partial_and_still_uploads(config):
    discovery = MagicMock()
    discovery.scan = AsyncMock(side_effect=DockerUnavailableError("Docker daemon unavailable: timed out"))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock())

    result = await orch.run_backup("job-full", run_id="r1")

    assert result["status"] == "partial"
    assert discovery.scan.await_count == orch.discovery_attempts
    orch.backup_engine.backup.assert_awaited_once()  # flash + files still protected
    row = await _run_row(config, "r1")
    assert row["status"] == "partial"
    assert "container discovery failed" in row["error_message"]
    orch.notifier.send.assert_awaited()
    assert orch.notifier.send.await_args.args[0] == "backup.failed"


@pytest.mark.asyncio
async def test_discovery_retries_transient_failure(config):
    db = _db("pg")
    discovery = MagicMock()
    discovery.scan = AsyncMock(side_effect=[DockerUnavailableError("boom"), [_container("pg", [db])]])
    dumper = MagicMock()
    dumper.dump_all = AsyncMock(return_value=[_ok_dump(db)])
    orch = make_orchestrator(config, discovery=discovery, db_dumper=dumper)

    result = await orch.run_backup("job-full", run_id="r2")

    assert result["status"] == "success"
    row = await _run_row(config, "r2")
    assert (row["databases_discovered"], row["databases_dumped"]) == (1, 1)


@pytest.mark.asyncio
async def test_db_dump_job_skips_flash_and_flash_job_skips_databases(config):
    db = _db("pg")
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("pg", [db])]))
    dumper = MagicMock(dump_all=AsyncMock(return_value=[_ok_dump(db)]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=dumper)

    await orch.run_backup("job-db", run_id="r-db")
    dumper.dump_all.assert_awaited_once()
    orch.flash_backup.backup.assert_not_awaited()

    dumper.dump_all.reset_mock()
    discovery.scan.reset_mock()
    await orch.run_backup("job-flash", run_id="r-flash")
    discovery.scan.assert_not_awaited()
    dumper.dump_all.assert_not_awaited()
    orch.flash_backup.backup.assert_awaited_once()

    # Every job type uploads a complete snapshot (same path set for retention).
    paths = [c.args[1] for c in orch.backup_engine.backup.await_args_list]
    assert paths[0] == paths[1] == [str(config.dump_dir)]


@pytest.mark.asyncio
async def test_scheduled_run_waits_for_running_backup(config):
    orch = make_orchestrator(config)
    assert orch.lock_manager.acquire_backup_lock("other-run")

    async def release_soon():
        await asyncio.sleep(0.2)
        orch.lock_manager.release_backup_lock()

    releaser = asyncio.create_task(release_soon())
    result = await orch.run_backup("job-full", trigger="scheduled", run_id="r-wait", lock_wait_seconds=5)
    await releaser

    assert result["status"] == "success"
    assert (await _run_row(config, "r-wait"))["status"] == "success"


@pytest.mark.asyncio
async def test_manual_run_does_not_wait_for_lock(config):
    orch = make_orchestrator(config)
    assert orch.lock_manager.acquire_backup_lock("other-run")
    try:
        result = await orch.run_backup("job-full", trigger="manual", run_id="r-manual")
    finally:
        orch.lock_manager.release_backup_lock()
    assert result["status"] == "conflict"
    assert (await _run_row(config, "r-manual"))["status"] == "failed"


@pytest.mark.asyncio
async def test_missing_watched_directory_is_reported(config, tmp_path):
    existing = tmp_path / "keep"
    existing.mkdir()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO watched_directories (id, path, label) VALUES ('d1', ?, 'gone')",
            (str(tmp_path / "gone"),),
        )
        await db.execute("INSERT INTO watched_directories (id, path, label) VALUES ('d2', ?, 'keep')", (str(existing),))
        await db.commit()
    orch = make_orchestrator(config)

    result = await orch.run_backup("job-flash", run_id="r-missing")

    assert result["status"] == "partial"
    assert orch.backup_engine.backup.await_args.args[1] == [str(config.dump_dir), str(existing)]
    assert "not found" in (await _run_row(config, "r-missing"))["error_message"]


@pytest.mark.asyncio
async def test_drop_to_zero_databases_is_flagged_once(config):
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO job_runs (id, job_id, status, started_at, databases_discovered)
               VALUES ('prev', 'job-db', 'success', '2026-01-01T00:00:00Z', 12)"""
        )
        await db.commit()
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("web", [])]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock())

    first = await orch.run_backup("job-db", run_id="r-zero-1")
    second = await orch.run_backup("job-db", run_id="r-zero-2")

    assert first["status"] == "partial"
    assert "previous run found 12" in (await _run_row(config, "r-zero-1"))["error_message"]
    assert second["status"] == "success"


@pytest.mark.asyncio
async def test_databases_in_stopped_containers_are_skipped_not_failed(config):
    running_db, stopped_db = _db("pg-live"), _db("pg-old")
    discovery = MagicMock(scan=AsyncMock(return_value=[
        _container("pg-live", [running_db]),
        _container("pg-old", [stopped_db], status="exited"),
    ]))
    dumper = MagicMock(dump_all=AsyncMock(return_value=[_ok_dump(running_db)]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=dumper)

    result = await orch.run_backup("job-db", run_id="r-stopped")

    assert result["status"] == "success"
    assert dumper.dump_all.await_args.args[0] == [running_db]
    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute(
            "SELECT container_name, status FROM job_run_databases WHERE run_id = 'r-stopped' ORDER BY container_name"
        )
        assert await cursor.fetchall() == [("pg-live", "success"), ("pg-old", "skipped")]


@pytest.mark.asyncio
async def test_duplicate_companion_databases_are_dumped_once(config):
    db = _db("immich_postgres", name="immich")
    discovery = MagicMock(scan=AsyncMock(return_value=[
        _container("immich", [db]),
        _container("immich_postgres", [_db("immich_postgres", name="immich")]),
    ]))
    dumper = MagicMock(dump_all=AsyncMock(return_value=[_ok_dump(db)]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=dumper)

    await orch.run_backup("job-db", run_id="r-dupe")

    assert len(dumper.dump_all.await_args.args[0]) == 1
    assert (await _run_row(config, "r-dupe"))["databases_discovered"] == 1


@pytest.mark.asyncio
async def test_restic_partial_snapshot_marks_run_partial(config):
    orch = make_orchestrator(config, backup_result={
        "status": "partial", "snapshot_id": "abc", "total_bytes_processed": 10,
        "error": "Some files could not be read: /x: permission denied",
    })

    result = await orch.run_backup("job-flash", run_id="r-p")

    assert result["status"] == "partial"
    assert "could not be read" in (await _run_row(config, "r-p"))["error_message"]
    orch.backup_engine.forget.assert_awaited_once()  # snapshot exists, retention still applies


@pytest.mark.asyncio
async def test_no_enabled_targets_is_not_success(config):
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE storage_targets SET enabled = 0")
        await db.commit()
    orch = make_orchestrator(config)

    result = await orch.run_backup("job-full", run_id="r-none")

    assert result["status"] == "partial"
    assert "no enabled storage targets" in (await _run_row(config, "r-none"))["error_message"]


@pytest.mark.asyncio
async def test_post_backup_retention_does_not_prune(config):
    orch = make_orchestrator(config)
    await orch.run_backup("job-full", run_id="r-forget")
    assert orch.backup_engine.forget.await_args.kwargs["prune"] is False


@pytest.mark.asyncio
async def test_cloud_target_rclone_config_written_before_upload(config):
    from app.core.security import encrypt_config

    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("DELETE FROM storage_targets")
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES ('b2', 'B2', 'b2', 1, ?)",
            (encrypt_config({"bucket": "b", "key_id": "k", "app_key": "s"}, str(config.config_dir)),),
        )
        await db.commit()
    orch = make_orchestrator(config)

    await orch.run_backup("job-full", run_id="r-cloud")

    orch.cloud_manager.write_target_config.assert_awaited_once()
    assert orch.cloud_manager.write_target_config.await_args.args[0]["config"]["bucket"] == "b"


@pytest.mark.asyncio
async def test_local_target_dir_gets_exclusion_marker(config, tmp_path):
    from app.core.security import encrypt_config
    from app.services.backup_engine import EXCLUDE_MARKER

    repo_parent = tmp_path / "local-backup"
    repo_parent.mkdir()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "UPDATE storage_targets SET config = ? WHERE id = 't1'",
            (encrypt_config({"path": str(repo_parent)}, str(config.config_dir)),),
        )
        await db.commit()
    orch = make_orchestrator(config)

    await orch.run_backup("job-full", run_id="r-mark")

    assert (repo_parent / EXCLUDE_MARKER).exists()
    assert (config.restore_dir / EXCLUDE_MARKER).exists()


@pytest.mark.asyncio
async def test_stale_dumps_pruned_even_without_docker(config):
    import os
    import time

    old = config.dump_dir / "gone_db.sqlite3_20200101_000000.sqlite3"
    old.write_bytes(b"x")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))
    orch = make_orchestrator(config)  # discovery/db_dumper None (no Docker)

    await orch.run_backup("job-flash", run_id="r-prune")

    assert not old.exists()

