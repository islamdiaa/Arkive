"""Docker client reconnects, hostname resolution, JSON logging, mount detection,
restic exit codes, scheduler coordination, migration 4 and gzip dump checks."""

import gzip
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.utils.subprocess_runner import CommandResult


# --- ResilientDockerClient ---------------------------------------------------

class TestResilientDockerClient:
    def test_connects_lazily_and_proxies_attributes(self):
        from app.services.docker_client import ResilientDockerClient

        real = MagicMock()
        factory = MagicMock(return_value=real)
        client = ResilientDockerClient(factory=factory)
        factory.assert_not_called()

        assert client.containers is real.containers
        assert client.containers is real.containers
        factory.assert_called_once()

    def test_reconnects_after_startup_failure(self):
        from app.services.docker_client import DockerUnavailableError, ResilientDockerClient

        real = MagicMock()
        factory = MagicMock(side_effect=[RuntimeError("Read timed out"), real])
        client = ResilientDockerClient(factory=factory, backoff_seconds=0)

        with pytest.raises(DockerUnavailableError):
            client.get_client()
        assert client.last_error and "timed out" in client.last_error
        assert client.info is real.info
        assert client.last_error is None

    def test_backoff_avoids_hammering_dead_daemon(self):
        from app.services.docker_client import DockerUnavailableError, ResilientDockerClient

        factory = MagicMock(side_effect=RuntimeError("down"))
        client = ResilientDockerClient(factory=factory, backoff_seconds=60)
        for _ in range(3):
            with pytest.raises(DockerUnavailableError):
                client.get_client()
        factory.assert_called_once()

    def test_ping_false_when_unreachable(self):
        from app.services.docker_client import ResilientDockerClient

        client = ResilientDockerClient(factory=MagicMock(side_effect=RuntimeError("down")), backoff_seconds=0)
        assert client.ping() is False


# --- hostname resolution -------------------------------------------------------

class TestResolveHostname:
    def setup_method(self):
        from app.services.host_identity import reset_hostname_cache
        reset_hostname_cache()

    teardown_method = setup_method

    def test_unraid_ident_cfg_used_when_docker_unavailable(self, tmp_path):
        from app.services.host_identity import resolve_hostname

        ident = tmp_path / "ident.cfg"
        ident.write_text('NAME="tower"\r\nCOMMENT="media"\r\n')
        docker_client = MagicMock()
        docker_client.info.side_effect = RuntimeError("down")
        assert resolve_hostname(docker_client=docker_client, ident_cfg=ident) == "tower"

    def test_last_daemon_name_survives_outage(self, tmp_path):
        from app.services.host_identity import resolve_hostname

        docker_client = MagicMock()
        docker_client.info.return_value = {"Name": "unraid-test"}
        assert resolve_hostname(docker_client=docker_client, ident_cfg=tmp_path / "none") == "unraid-test"
        docker_client.info.side_effect = RuntimeError("down")
        assert resolve_hostname(docker_client=docker_client, ident_cfg=tmp_path / "none") == "unraid-test"

    def test_daemon_name_cached(self, tmp_path):
        from app.services.host_identity import resolve_hostname

        docker_client = MagicMock()
        docker_client.info.return_value = {"Name": "unraid-test"}
        for _ in range(5):
            resolve_hostname(docker_client=docker_client, ident_cfg=tmp_path / "none")
        docker_client.info.assert_called_once()

    def test_configured_name_wins(self):
        from app.services.host_identity import resolve_hostname

        assert resolve_hostname(settings={"server_name": " test-server "}) == "test-server"


# --- JSON logging -----------------------------------------------------------------

def test_json_formatter_escapes_and_uses_utc():
    from app.utils.log_config import JsonFormatter

    record = logging.LogRecord("arkive.test", logging.WARNING, __file__, 1,
                               'Execution of job "Health Check" skipped: %s', ("x\ny",), None)
    record.created = 0  # 1970-01-01T00:00:00Z
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["timestamp"] == "1970-01-01T00:00:00Z"
    assert payload["message"] == 'Execution of job "Health Check" skipped: x\ny'
    assert payload["run_id"] == "-"
    assert "\n" not in line


# --- mounts ---------------------------------------------------------------------------

MOUNTINFO = """\
600 500 0:50 / / rw,relatime - overlay overlay rw,lowerdir=/x
601 600 0:51 /appdata/arkive /config rw,relatime - fuse.shfs shfs rw
602 600 0:51 /appdata/arkive/local-backup /data rw,relatime - fuse.shfs shfs rw
603 600 8:1 /docker/volumes/""" + "a" * 64 + """/_data /cache rw,relatime - btrfs /dev/loop2 rw
604 600 0:51 / /mnt/user rw,relatime - fuse.shfs shfs rw
"""


class TestEphemeralPaths:
    @pytest.fixture
    def mountinfo(self, tmp_path):
        p = tmp_path / "mountinfo"
        p.write_text(MOUNTINFO)
        return p

    def test_container_root_is_ephemeral(self, mountinfo):
        from app.utils.mounts import ephemeral_path_reason

        assert "container" in ephemeral_path_reason("/backups/repo", mountinfo, in_container=True)

    def test_anonymous_volume_is_ephemeral(self, mountinfo):
        from app.utils.mounts import ephemeral_path_reason

        assert "anonymous" in ephemeral_path_reason("/cache/x", mountinfo, in_container=True)

    @pytest.mark.parametrize("path", ["/data", "/data/local", "/mnt/user/backups", "/config/x"])
    def test_bind_mounts_are_persistent(self, mountinfo, path):
        from app.utils.mounts import ephemeral_path_reason

        assert ephemeral_path_reason(path, mountinfo, in_container=True) is None

    def test_not_in_container_never_ephemeral(self, mountinfo):
        from app.utils.mounts import ephemeral_path_reason

        assert ephemeral_path_reason("/backups", mountinfo, in_container=False) is None


# --- restic backup exit codes --------------------------------------------------------

class TestResticExitCodes:
    @pytest.fixture
    def engine(self, tmp_path):
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = tmp_path / "x.db"
        eng = BackupEngine(config)
        eng._get_password = AsyncMock(return_value="pw")
        eng._get_bandwidth_limit = AsyncMock(return_value="")
        eng._get_server_name = AsyncMock(return_value="test-server")
        return eng

    @pytest.mark.asyncio
    async def test_exit_code_3_is_partial_with_snapshot(self, engine):
        stdout = "\n".join([
            json.dumps({"message_type": "error", "error": {"message": "permission denied"}, "item": "/data/x"}),
            json.dumps({"message_type": "summary", "snapshot_id": "deadbeef", "total_bytes_processed": 42}),
        ])
        with patch("app.services.backup_engine.run_command",
                   AsyncMock(return_value=CommandResult(3, stdout, "warning", 1, "restic"))) as run:
            result = await engine.backup({"type": "local", "config": {"path": "/data"}}, ["/config/dumps"])

        assert result["status"] == "partial"
        assert result["snapshot_id"] == "deadbeef"
        assert "/data/x: permission denied" in result["error"]
        cmd = run.await_args.args[0]
        assert "--retry-lock" in cmd and "--exclude-caches" in cmd and "--exclude-if-present" in cmd
        assert run.await_count == 1  # not retried

    @pytest.mark.asyncio
    async def test_forget_without_prune(self, engine):
        with patch("app.services.backup_engine.run_command",
                   AsyncMock(return_value=CommandResult(0, "[]", "", 1, "restic"))) as run:
            await engine.forget({"type": "local", "config": {"path": "/data"}}, prune=False)
            assert "--prune" not in run.await_args.args[0]
            await engine.forget({"type": "local", "config": {"path": "/data"}})
            assert "--prune" in run.await_args.args[0]


# --- scheduler coordination ------------------------------------------------------------

class TestSchedulerCoordination:
    def _scheduler(self, tmp_path, **kwargs):
        from app.services.lock_manager import BackupLockManager
        from app.services.scheduler import ArkiveScheduler

        orchestrator = MagicMock()
        orchestrator.lock_manager = BackupLockManager(tmp_path)
        orchestrator.run_backup = AsyncMock(return_value={"status": "success"})
        config = MagicMock()
        config.db_path = tmp_path / "x.db"
        sched = ArkiveScheduler(orchestrator, config, **kwargs)
        sched.lock_poll_seconds = 0.01
        return sched, orchestrator

    @pytest.mark.asyncio
    async def test_trigger_job_runs_in_background_as_manual(self, tmp_path):
        import asyncio

        sched, orchestrator = self._scheduler(tmp_path)
        started = asyncio.Event()

        async def slow_backup(**kwargs):
            started.set()
            await asyncio.sleep(0.2)
            return {"status": "success"}

        orchestrator.run_backup = AsyncMock(side_effect=slow_backup)
        sched._job_map["j1"] = "backup_j1"
        await asyncio.wait_for(sched.trigger_job("j1"), timeout=0.1)  # returns immediately
        await asyncio.wait_for(started.wait(), timeout=1)
        assert orchestrator.run_backup.await_args.kwargs == {"job_id": "j1", "trigger": "manual"}
        await asyncio.gather(*sched._background_tasks)

    @pytest.mark.asyncio
    async def test_integrity_verify_waits_for_backup_then_runs(self, tmp_path):
        import asyncio

        verify = MagicMock()
        verify.verify_all_targets = AsyncMock(return_value={"status": "completed", "overall_trust_score": 100,
                                                            "targets": []})
        sched, orchestrator = self._scheduler(tmp_path, verify_engine=verify)
        assert orchestrator.lock_manager.acquire_backup_lock("backup-run")

        async def release():
            await asyncio.sleep(0.1)
            orchestrator.lock_manager.release_backup_lock()

        with patch("app.services.scheduler.log_activity", AsyncMock()), \
             patch("app.services.scheduler.aiosqlite.connect") as connect:
            connect.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            connect.return_value.__aexit__ = AsyncMock(return_value=False)
            await asyncio.gather(release(), sched._run_integrity_verify())

        verify.verify_all_targets.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retention_holds_backup_lock(self, tmp_path):
        backup_engine = MagicMock()
        sched, orchestrator = self._scheduler(tmp_path, backup_engine=backup_engine)
        seen = {}

        def fake_targets(*args, **kwargs):
            seen["locked"] = orchestrator.lock_manager.is_backup_running()
            raise RuntimeError("stop here")

        with patch("app.services.scheduler.aiosqlite.connect", side_effect=fake_targets):
            await sched._run_retention_cleanup()

        assert seen["locked"] is True
        assert orchestrator.lock_manager.is_backup_running() is False


# --- migration 4 ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_4_drops_health_check_noise(tmp_path):
    from app.core.database import init_db, run_migrations

    db_path = tmp_path / "m.db"
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        # Simulate a v3 database with the old noisy entries.
        await db.execute("DELETE FROM schema_version WHERE version > 3")
        await db.executemany(
            "INSERT INTO activity_log (type, action, message) VALUES (?, ?, ?)",
            [("system", "health_check", "Health check completed")] * 5 + [("backup", "completed", "ok")],
        )
        await db.commit()

    await run_migrations(db_path)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT type, action FROM activity_log")
        assert await cursor.fetchall() == [("backup", "completed")]
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE name = 'idx_snapshots_target_id'")
        assert await cursor.fetchone() is not None


# --- verify engine gzip SQL dump check ----------------------------------------------------

class TestGzipSqlDumpCheck:
    def _write(self, path: Path, data: bytes):
        with gzip.open(path, "wb") as f:
            f.write(data)

    def test_complete_postgres_dump_passes(self, tmp_path):
        from app.services.verify_engine import VerifyEngine

        p = tmp_path / "pg_app_20260101_000000.sql.gz"
        self._write(p, b"x" * 10000 + b"\n-- PostgreSQL database dump complete\n")
        assert VerifyEngine._check_gzip_sql_dump(str(p))["status"] == "success"

    def test_truncated_dump_fails(self, tmp_path):
        from app.services.verify_engine import VerifyEngine

        p = tmp_path / "pg_app_20260101_000000.sql.gz"
        self._write(p, b"CREATE TABLE t (id int);\n")
        assert VerifyEngine._check_gzip_sql_dump(str(p))["status"] == "failed"

    def test_corrupt_gzip_fails(self, tmp_path):
        from app.services.verify_engine import VerifyEngine

        p = tmp_path / "pg_app_20260101_000000.sql.gz"
        self._write(p, b"y" * 100000)
        p.write_bytes(p.read_bytes()[:-20])
        result = VerifyEngine._check_gzip_sql_dump(str(p))
        assert result["status"] == "failed"
