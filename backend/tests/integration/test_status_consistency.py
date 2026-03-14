import json
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from tests.conftest import do_setup


async def test_status_uses_latest_discovery_after_backup_refresh(client):
    await do_setup(client)
    db_path = client._transport.app.state.config.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO discovered_containers
            (name, image, status, ports, mounts, databases, profile, priority, compose_project, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))""",
            (
                "paperless-paperless-1",
                "ghcr.io/paperless-ngx/paperless-ngx:latest",
                "running",
                "[]",
                "[]",
                json.dumps(
                    [
                        {
                            "container_name": "paperless-paperless-1",
                            "db_type": "postgres",
                            "db_name": "docs",
                            "host_path": None,
                        }
                    ]
                ),
                "paperless-ngx",
                "critical",
                "paperless",
            ),
        )
        await db.execute(
            "INSERT INTO job_runs (id, job_id, status, trigger) VALUES ('run1', 'job1', 'success', 'manual')"
        )
        await db.execute(
            """INSERT INTO job_run_databases
            (run_id, container_name, db_type, db_name, dump_size_bytes, integrity_check, status, host_path, error)
            VALUES ('run1', 'paperless-db-1', 'postgres', 'docs', 100, 'ok', 'success', NULL, NULL)"""
        )
        await db.execute(
            """INSERT INTO backup_jobs
            (id, name, schedule, enabled, targets, include_databases, include_flash)
            VALUES ('job1', 'DB Dumps', '0 0 * * *', 1, '[]', 0, 0)"""
        )
        await db.commit()

    status_before = await client.get("/api/status")
    assert status_before.status_code == 200
    assert status_before.json()["health"] == "degraded"

    class DbEntry:
        def __init__(self, container_name, db_type, db_name, host_path=None):
            self.container_name = container_name
            self.db_type = db_type
            self.db_name = db_name
            self.host_path = host_path

        def model_dump(self):
            return {
                "container_name": self.container_name,
                "db_type": self.db_type,
                "db_name": self.db_name,
                "host_path": self.host_path,
            }

    class ContainerEntry:
        def __init__(self):
            self.name = "paperless-db-1"
            self.image = "postgres:16"
            self.status = "running"
            self.ports = []
            self.mounts = []
            self.databases = [DbEntry("paperless-db-1", "postgres", "docs")]
            self.profile = "postgres"
            self.priority = "high"
            self.compose_project = "paperless"

    class StubDiscovery:
        async def scan(self):
            return [ContainerEntry()]

    class StubDumper:
        async def dump_all(self, all_databases):
            return []

    class StubFlashBackup:
        async def backup_if_enabled(self, *args, **kwargs):
            return None

    class StubBackupEngine:
        async def backup(self, *args, **kwargs):
            return {"snapshot_id": "snap1", "size_bytes": 1}

    class StubEventBus:
        async def publish(self, *args, **kwargs):
            return None

    from app.services.orchestrator import BackupOrchestrator

    app = client._transport.app
    orchestrator = BackupOrchestrator(
        discovery=StubDiscovery(),
        db_dumper=StubDumper(),
        flash_backup=StubFlashBackup(),
        backup_engine=StubBackupEngine(),
        cloud_manager=None,
        notifier=None,
        event_bus=StubEventBus(),
        config=app.state.config,
    )

    from app.services import orchestrator as orchestrator_module

    lock_file = Path(app.state.config.config_dir) / "backup.lock"
    with patch.object(orchestrator_module, "LOCK_FILE", lock_file):
        result = await orchestrator.run_backup("job1", run_id="run2", trigger="manual")
    assert result["status"] in {"success", "partial"}

    status_after = await client.get("/api/status")
    assert status_after.status_code == 200
    body = status_after.json()
    assert body["databases"]["total"] == 1
    assert body["databases"]["healthy"] == 1


async def test_status_prefers_configured_or_host_hostname(client):
    await do_setup(client)
    db_path = client._transport.app.state.config.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('server_name', 'test-server')")
        await db.commit()

    status = await client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["hostname"] == "test-server"
