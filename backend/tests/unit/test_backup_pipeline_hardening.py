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
    from app.core.database import init_db, run_migrations

    cfg = ArkiveConfig(config_dir=tmp_path)
    cfg.ensure_dirs()
    await init_db(cfg.db_path)
    await run_migrations(cfg.db_path)
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
    assert orch.notifier.send.await_args.args[0] == "backup.partial"


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
async def test_drop_to_zero_databases_stops_warning_after_the_window(config):
    """Databases last seen more than database_loss_window_days ago count as
    intentionally removed."""
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            """INSERT INTO job_runs (id, job_id, status, started_at, databases_discovered)
               VALUES ('prev', 'job-db', 'success',
                       strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-45 days'), 12)"""
        )
        await db.commit()
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("web", [])]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock())

    result = await orch.run_backup("job-db", run_id="r-zero-1")

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_databases_in_stopped_containers_are_skipped_not_failed(config):
    from app.services.db_dumper import dump_prefix

    running_db, stopped_db = _db("pg-live"), _db("pg-old")
    # Dumped while it was running; that dump keeps covering it.
    (config.dump_dir / f"{dump_prefix('pg-old', 'postgres', 'app')}_20261001_000000.sql").write_bytes(b"x")
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
async def test_roles_dumps_are_not_counted_as_databases(config):
    db = _db("pg")
    roles_ok = DumpResult(container_name="pg", db_type="postgres", db_name="globals", dump_path="/g",
                          dump_size_bytes=5, integrity_check="ok", status="success", kind="globals")
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("pg", [db])]))
    dumper = MagicMock(dump_all=AsyncMock(return_value=[_ok_dump(db), roles_ok]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=dumper)

    result = await orch.run_backup("job-db", run_id="r-roles")

    assert result["status"] == "success"
    row = await _run_row(config, "r-roles")
    assert (row["databases_discovered"], row["databases_dumped"], row["databases_failed"]) == (1, 1, 0)


@pytest.mark.asyncio
async def test_failed_roles_dump_makes_run_partial(config):
    db = _db("pg")
    roles_failed = DumpResult(container_name="pg", db_type="postgres", db_name="globals", dump_path="",
                              dump_size_bytes=0, integrity_check="skipped", status="failed",
                              error="permission denied", kind="globals")
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("pg", [db])]))
    dumper = MagicMock(dump_all=AsyncMock(return_value=[_ok_dump(db), roles_failed]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=dumper)

    result = await orch.run_backup("job-db", run_id="r-roles-bad")

    assert result["status"] == "partial"
    row = await _run_row(config, "r-roles-bad")
    assert row["databases_failed"] == 0
    assert "roles dump failed for pg" in row["error_message"]


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
async def test_arkive_storage_is_excluded_by_exact_path(config, tmp_path):
    """Local repositories and the restore dir are excluded with --exclude on
    their exact paths. No marker is written into the target's configured
    directory, which may be a parent of real data (e.g. /mnt/user/appdata)."""
    from app.core.security import encrypt_config
    from app.services.backup_engine import EXCLUDE_MARKER

    repo_parent = tmp_path / "appdata"
    (repo_parent / "some-app").mkdir(parents=True)
    (repo_parent / "arkive-repo").mkdir()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "UPDATE storage_targets SET config = ? WHERE id = 't1'",
            (encrypt_config({"path": str(repo_parent)}, str(config.config_dir)),),
        )
        await db.commit()
    orch = make_orchestrator(config)

    await orch.run_backup("job-full", run_id="r-mark")

    excludes = orch.backup_engine.backup.await_args.args[2]
    assert str(repo_parent / "arkive-repo") in excludes
    assert str(config.restore_dir) in excludes
    assert str(repo_parent) not in excludes
    # Markers only inside Arkive-owned directories, never the configured parent.
    assert not (repo_parent / EXCLUDE_MARKER).exists()
    assert (repo_parent / "arkive-repo" / EXCLUDE_MARKER).exists()
    assert (config.restore_dir / EXCLUDE_MARKER).exists()


@pytest.mark.asyncio
async def test_storage_exclusions_cover_host_path_aliases(config, tmp_path, monkeypatch):
    """/config/restores is also visible as <host appdata>/arkive/restores through
    the /mnt/user mount; both spellings must be excluded."""
    import app.services.orchestrator as orch_mod

    alias_root = tmp_path / "host-view"
    (alias_root / "restores").mkdir(parents=True)
    (alias_root / "dumps").mkdir()

    def fake_host_path(path, docker_client):
        if path == str(config.restore_dir):
            return str(alias_root / "restores")
        if path == str(config.dump_dir):
            return str(alias_root / "dumps")
        return None

    monkeypatch.setattr(orch_mod, "host_path_for", fake_host_path)
    orch = make_orchestrator(config)
    await orch.run_backup("job-flash", run_id="r-alias")
    excludes = orch.backup_engine.backup.await_args.args[2]
    assert str(alias_root / "restores") in excludes
    assert f"{alias_root / 'dumps'}/*.partial" in excludes


@pytest.mark.asyncio
async def test_notification_event_matches_outcome(config):
    orch = make_orchestrator(config, backup_result={
        "status": "partial", "snapshot_id": "abc", "total_bytes_processed": 10,
        "error": "Some files could not be read: /x",
    })
    await orch.run_backup("job-flash", run_id="r-evt-partial")
    assert orch.notifier.send.await_args.args[0] == "backup.partial"

    orch = make_orchestrator(config, backup_result={"status": "failed", "error": "repo locked"})
    await orch.run_backup("job-flash", run_id="r-evt-failed")
    args = orch.notifier.send.await_args
    assert args.args[0] == "backup.failed" and args.kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_database_loss_keeps_warning_on_consecutive_empty_runs(config):
    """5 -> 0 -> 0: the second empty run must still be partial."""
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO job_runs (id, job_id, status, started_at, databases_discovered, databases_dumped) "
            "VALUES ('had-dbs', 'job-db', 'success', strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 days'), 5, 5)")
        await db.commit()
    discovery = MagicMock(scan=AsyncMock(return_value=[]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock())

    first = await orch.run_backup("job-db", run_id="empty-1")
    second = await orch.run_backup("job-db", run_id="empty-2")

    assert first["status"] == "partial" and second["status"] == "partial"
    assert "a recent run found 5" in (await _run_row(config, "empty-2"))["error_message"]


@pytest.mark.asyncio
async def test_dumps_are_not_aged_out_when_discovery_is_incomplete(config):
    """Docker down (or a job that doesn't dump databases): this run can't tell
    a vanished database from an unreachable one, so no dump is aged out."""
    import os
    import time

    old = config.dump_dir / "gone_db.sqlite3_20200101_000000.sqlite3"
    old.write_bytes(b"x")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))
    orch = make_orchestrator(config)  # discovery/db_dumper None (no Docker)

    await orch.run_backup("job-flash", run_id="r-prune")

    assert old.exists()


@pytest.mark.asyncio
async def test_dumps_of_vanished_databases_age_out(config):
    import os
    import time

    from app.services.db_dumper import dump_prefix

    stale = time.time() - 30 * 86400
    db = _db("pg-live")
    gone = config.dump_dir / f"{dump_prefix('gone', 'postgres', 'app')}_20200101_000000.sql"
    live = config.dump_dir / f"{dump_prefix('pg-live', 'postgres', 'app')}_20200101_000000.sql"
    for f in (gone, live):
        f.write_bytes(b"-- PostgreSQL database dump complete\n")
        os.utime(f, (stale, stale))
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("pg-live", [db])]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock(dump_all=AsyncMock(return_value=[])))

    await orch.run_backup("job-db", run_id="r-age")

    assert not gone.exists()
    assert live.exists()  # still discovered: its newest dump is never aged out


@pytest.mark.asyncio
async def test_stopped_database_container_blocks_age_out(config):
    """A stopped Postgres can't list its databases, so dumps of its other
    databases look 'vanished'; nothing is aged out while it is stopped."""
    import os
    import time

    from app.services.db_dumper import dump_prefix

    stale = time.time() - 30 * 86400
    stopped_db = _db("pg-old")
    other = config.dump_dir / f"{dump_prefix('pg-old', 'postgres', 'second_db')}_20200101_000000.sql"
    kept = config.dump_dir / f"{dump_prefix('pg-old', 'postgres', 'app')}_20200101_000000.sql"
    for f in (other, kept):
        f.write_bytes(b"-- PostgreSQL database dump complete\n")
        os.utime(f, (stale, stale))
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("pg-old", [stopped_db], status="exited")]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock(dump_all=AsyncMock(return_value=[])))

    result = await orch.run_backup("job-db", run_id="r-stopped-age")

    assert result["status"] == "success"
    assert other.exists() and kept.exists()


@pytest.mark.asyncio
async def test_stopped_database_without_any_dump_is_a_warning(config):
    stopped_db = _db("pg-new")
    discovery = MagicMock(scan=AsyncMock(return_value=[_container("pg-new", [stopped_db], status="exited")]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock(dump_all=AsyncMock(return_value=[])))

    result = await orch.run_backup("job-db", run_id="r-never")

    assert result["status"] == "partial"
    assert "never been dumped: pg-new/app" in (await _run_row(config, "r-never"))["error_message"]


@pytest.mark.asyncio
async def test_discovery_outcome_is_recorded(config):
    from app.services.docker_client import DockerUnavailableError as _Down

    discovery = MagicMock(scan=AsyncMock(side_effect=_Down("down")))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock())
    await orch.run_backup("job-db", run_id="r-disc-fail")
    assert (await _run_row(config, "r-disc-fail"))["discovery_ok"] == 0

    discovery = MagicMock(scan=AsyncMock(return_value=[]))
    orch = make_orchestrator(config, discovery=discovery, db_dumper=MagicMock())
    await orch.run_backup("job-db", run_id="r-disc-ok")
    assert (await _run_row(config, "r-disc-ok"))["discovery_ok"] == 1


@pytest.mark.asyncio
async def test_prerelease_marker_in_target_parent_is_removed(config, tmp_path):
    from app.core.security import encrypt_config
    from app.services.backup_engine import EXCLUDE_MARKER
    from app.services.orchestrator import _EXCLUDE_MARKER_TEXT

    parent = tmp_path / "appdata"
    (parent / "arkive-repo").mkdir(parents=True)
    (parent / EXCLUDE_MARKER).write_text(_EXCLUDE_MARKER_TEXT)
    user_marked = tmp_path / "user-dir"
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE storage_targets SET config = ? WHERE id = 't1'",
                         (encrypt_config({"path": str(parent)}, str(config.config_dir)),))
        await db.commit()
    orch = make_orchestrator(config)

    await orch.run_backup("job-flash", run_id="r-unmark")

    assert not (parent / EXCLUDE_MARKER).exists()
    assert (parent / "arkive-repo" / EXCLUDE_MARKER).exists()
    assert not user_marked.exists()


@pytest.mark.asyncio
async def test_user_created_marker_in_target_parent_is_left_alone(config, tmp_path):
    from app.core.security import encrypt_config
    from app.services.backup_engine import EXCLUDE_MARKER

    parent = tmp_path / "appdata"
    (parent / "arkive-repo").mkdir(parents=True)
    (parent / EXCLUDE_MARKER).write_text("my own exclusion\n")
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE storage_targets SET config = ? WHERE id = 't1'",
                         (encrypt_config({"path": str(parent)}, str(config.config_dir)),))
        await db.commit()
    orch = make_orchestrator(config)

    await orch.run_backup("job-flash", run_id="r-keep-marker")

    assert (parent / EXCLUDE_MARKER).read_text() == "my own exclusion\n"


@pytest.mark.asyncio
async def test_enumeration_warning_makes_run_partial_and_blocks_age_out(config):
    import os
    import time

    db = _db("pg")
    container = _container("pg", [db])
    container.warnings = ["could not list databases in pg (timeout); only app is being backed up"]
    other = config.dump_dir / "pg_otherdb-0123abcd_20200101_000000.sql"
    other.write_bytes(b"x")
    stale = time.time() - 30 * 86400
    os.utime(other, (stale, stale))
    discovery = MagicMock(scan=AsyncMock(return_value=[container]))
    orch = make_orchestrator(config, discovery=discovery,
                             db_dumper=MagicMock(dump_all=AsyncMock(return_value=[_ok_dump(db)])))

    result = await orch.run_backup("job-db", run_id="r-enum")

    assert result["status"] == "partial"
    assert "could not list databases in pg" in (await _run_row(config, "r-enum"))["error_message"]
    assert other.exists()


@pytest.mark.asyncio
async def test_partial_dump_files_are_excluded_from_snapshots(config):
    orch = make_orchestrator(config)
    await orch.run_backup("job-flash", run_id="r-partial-ex")
    excludes = orch.backup_engine.backup.await_args.args[2]
    assert f"{config.dump_dir}/*.partial" in excludes
    assert "*.partial" not in excludes  # user files named *.partial elsewhere are kept
