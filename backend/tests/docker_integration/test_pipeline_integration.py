"""Phase 7: Full pipeline integration test with FakeDockerClient + real services."""

import json
import os
import uuid
from pathlib import Path

import aiosqlite
import pytest

import app.services.orchestrator as orch_mod
from app.core.config import ArkiveConfig
from app.core.database import init_db
from app.core.event_bus import EventBus
from app.core.platform import Platform
from app.services.backup_engine import BackupEngine
from app.services.cloud_manager import CloudManager
from app.services.db_dumper import DBDumper
from app.services.discovery import DiscoveryEngine
from app.services.flash_backup import FlashBackup
from app.services.notifier import Notifier
from app.services.orchestrator import BackupOrchestrator
from tests.fakes.fake_docker import create_fake_docker_client


def _mock_backup_engine(engine: BackupEngine) -> BackupEngine:
    """Patch backup engine to succeed without real restic."""

    async def _fake_init_repo(target):
        return True

    async def _fake_backup(target, paths, excludes=None, tags=None, cancel_check=None, **kwargs):
        return {"status": "success", "snapshot_id": "abc12345", "total_bytes_processed": 1024, "files_new": 2}

    async def _fake_forget(target, **kwargs):
        return {"status": "success"}

    async def _fake_snapshots(target):
        return []

    engine.init_repo = _fake_init_repo
    engine.backup = _fake_backup
    engine.forget = _fake_forget
    engine.snapshots = _fake_snapshots
    return engine


async def _setup_db(config: ArkiveConfig, target_path: str) -> tuple[str, str]:
    """Initialize DB schema, create job + storage target. Return (job_id, target_id)."""
    from app.core.security import _load_fernet_from_dir, _reset_fernet, encrypt_value

    _reset_fernet()
    _load_fernet_from_dir(str(config.config_dir))

    await init_db(config.db_path)

    job_id = str(uuid.uuid4())[:8]
    target_id = str(uuid.uuid4())[:8]

    async with aiosqlite.connect(config.db_path) as db:
        # Set encryption password
        enc_pw = encrypt_value("test-restic-password")
        await db.execute(
            "INSERT INTO settings (key, value, encrypted) VALUES ('encryption_password', ?, 1)",
            (enc_pw,),
        )

        # Create job
        await db.execute(
            "INSERT INTO backup_jobs (id, name, schedule, include_databases, include_flash) "
            "VALUES (?, 'test-job', '0 3 * * *', 1, 1)",
            (job_id,),
        )

        # Create local storage target
        target_config = json.dumps({"path": target_path})
        await db.execute(
            "INSERT INTO storage_targets (id, name, type, config, status, enabled) "
            "VALUES (?, 'local-test', 'local', ?, 'healthy', 1)",
            (target_id, target_config),
        )

        await db.commit()

    return job_id, target_id


def _build_orchestrator(
    config: ArkiveConfig,
    fake_docker=None,
    platform=Platform.LINUX,
    fake_boot_config=None,
):
    """Build a BackupOrchestrator with the given components."""
    event_bus = EventBus()

    discovery = None
    db_dumper = None
    if fake_docker:
        discovery = DiscoveryEngine(fake_docker, config)
        db_dumper = DBDumper(fake_docker, config)

    flash = FlashBackup(config, platform)
    if fake_boot_config:
        config.boot_config_path = fake_boot_config

    backup_engine = _mock_backup_engine(BackupEngine(config))
    cloud_manager = CloudManager(config)
    notifier = Notifier(config, event_bus)

    return BackupOrchestrator(
        discovery=discovery,
        db_dumper=db_dumper,
        flash_backup=flash,
        backup_engine=backup_engine,
        cloud_manager=cloud_manager,
        notifier=notifier,
        event_bus=event_bus,
        config=config,
    )


@pytest.fixture
def pipeline_config(tmp_path):
    """Config for pipeline tests with all directories."""
    os.environ["ARKIVE_CONFIG_DIR"] = str(tmp_path)
    config = ArkiveConfig(
        config_dir=tmp_path,
        profiles_dir=Path(__file__).resolve().parents[3] / "profiles",
        boot_config_path=tmp_path / "boot-config",
    )
    config.ensure_dirs()

    # Create some test data files to back up
    data_dir = tmp_path / "test-data"
    data_dir.mkdir()
    (data_dir / "file1.txt").write_text("hello world")
    (data_dir / "file2.txt").write_text("backup me")

    # Patch the module-level LOCK_FILE to use our temp dir
    original_lock = orch_mod.LOCK_FILE
    orch_mod.LOCK_FILE = tmp_path / "backup.lock"

    yield config

    orch_mod.LOCK_FILE = original_lock
    os.environ.pop("ARKIVE_CONFIG_DIR", None)
    from app.core.security import _reset_fernet

    _reset_fernet()


@pytest.mark.asyncio
class TestPipelineIntegration:
    """Full orchestrator pipeline with mock Docker."""

    async def test_full_pipeline_with_mocks(self, pipeline_config, tmp_path, fake_boot_config):
        """run_backup → success, databases dumped, flash backed up."""
        redis_dir = tmp_path / "redis-data"
        redis_dir.mkdir()
        fake_docker = create_fake_docker_client(redis_rdb_dir=str(redis_dir))

        # Create redis dump.rdb so the dump succeeds
        (redis_dir / "dump.rdb").write_bytes(b"REDIS0011" + os.urandom(32))

        target_path = str(tmp_path / "backup-target")
        os.makedirs(target_path, exist_ok=True)

        job_id, target_id = await _setup_db(pipeline_config, target_path)

        orch = _build_orchestrator(
            pipeline_config,
            fake_docker=fake_docker,
            platform=Platform.UNRAID,
            fake_boot_config=fake_boot_config,
        )

        result = await orch.run_backup(job_id=job_id, trigger="manual")

        assert result["status"] == "success"
        assert "run_id" in result

        # Verify DB records
        async with aiosqlite.connect(pipeline_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            run = await (await db.execute("SELECT * FROM job_runs WHERE id = ?", (result["run_id"],))).fetchone()
            assert run is not None
            assert run["status"] == "success"
            assert run["databases_discovered"] > 0
            assert run["databases_dumped"] > 0
            assert run["flash_backed_up"] == 1

    async def test_pipeline_skip_databases(self, pipeline_config, tmp_path, fake_boot_config):
        """skip_databases=True → no dump results."""
        fake_docker = create_fake_docker_client()
        target_path = str(tmp_path / "backup-target")
        os.makedirs(target_path, exist_ok=True)

        job_id, target_id = await _setup_db(pipeline_config, target_path)

        orch = _build_orchestrator(
            pipeline_config,
            fake_docker=fake_docker,
            platform=Platform.UNRAID,
            fake_boot_config=fake_boot_config,
        )

        result = await orch.run_backup(
            job_id=job_id,
            trigger="manual",
            skip_databases=True,
        )

        assert result["status"] == "success"

        async with aiosqlite.connect(pipeline_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            run = await (await db.execute("SELECT * FROM job_runs WHERE id = ?", (result["run_id"],))).fetchone()
            # Databases should not have been dumped
            assert (run["databases_dumped"] or 0) == 0

    async def test_pipeline_skip_flash(self, pipeline_config, tmp_path, fake_boot_config):
        """skip_flash=True → no flash backup."""
        redis_dir = tmp_path / "redis-data"
        redis_dir.mkdir()
        fake_docker = create_fake_docker_client(redis_rdb_dir=str(redis_dir))
        (redis_dir / "dump.rdb").write_bytes(b"REDIS0011" + os.urandom(32))
        target_path = str(tmp_path / "backup-target")
        os.makedirs(target_path, exist_ok=True)

        job_id, target_id = await _setup_db(pipeline_config, target_path)

        orch = _build_orchestrator(
            pipeline_config,
            fake_docker=fake_docker,
            platform=Platform.UNRAID,
            fake_boot_config=fake_boot_config,
        )

        result = await orch.run_backup(
            job_id=job_id,
            trigger="manual",
            skip_flash=True,
        )

        assert result["status"] == "success"

        async with aiosqlite.connect(pipeline_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            run = await (await db.execute("SELECT * FROM job_runs WHERE id = ?", (result["run_id"],))).fetchone()
            assert (run["flash_backed_up"] or 0) == 0

    async def test_pipeline_no_docker(self, pipeline_config, tmp_path, fake_boot_config):
        """discovery=None, db_dumper=None → still succeeds (files-only backup)."""
        target_path = str(tmp_path / "backup-target")
        os.makedirs(target_path, exist_ok=True)

        job_id, target_id = await _setup_db(pipeline_config, target_path)

        orch = _build_orchestrator(
            pipeline_config,
            fake_docker=None,  # No Docker
            platform=Platform.UNRAID,
            fake_boot_config=fake_boot_config,
        )

        result = await orch.run_backup(job_id=job_id, trigger="manual")

        assert result["status"] == "success"

        async with aiosqlite.connect(pipeline_config.db_path) as db:
            db.row_factory = aiosqlite.Row
            run = await (await db.execute("SELECT * FROM job_runs WHERE id = ?", (result["run_id"],))).fetchone()
            assert (run["databases_discovered"] or 0) == 0
