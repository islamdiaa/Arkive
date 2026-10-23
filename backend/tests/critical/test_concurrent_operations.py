"""Critical tests for concurrent operations: discovery + backup writes.

Validates that concurrent writes to the database during discovery
persistence and backup operations don't corrupt data or deadlock.
"""

import asyncio
import json
import logging

import aiosqlite
import pytest
import pytest_asyncio

from app.core.database import init_db
from app.services.discovery_persistence import persist_discovery_results

logger = logging.getLogger(__name__)


class FakeContainer:
    """Minimal container object for discovery persistence tests."""

    def __init__(self, name, image="test:latest", status="running",
                 databases=None, compose_project=None):
        self.name = name
        self.image = image
        self.status = status
        self.ports = []
        self.mounts = []
        self.databases = databases or []
        self.profile = None
        self.priority = "medium"
        self.compose_project = compose_project


class FakeDatabase:
    """Minimal database object for discovery persistence tests."""

    def __init__(self, container_name, db_type, db_name):
        self.container_name = container_name
        self.db_type = db_type
        self.db_name = db_name
        self.host_path = None

    def model_dump(self):
        return {
            "container_name": self.container_name,
            "db_type": self.db_type,
            "db_name": self.db_name,
            "host_path": self.host_path,
        }


@pytest_asyncio.fixture
async def test_db(tmp_path):
    """Create an isolated test database."""
    db_path = tmp_path / "concurrent_test.db"
    await init_db(db_path)
    return db_path


class TestConcurrentDiscoveryPersistence:
    """Test concurrent writes during discovery persistence."""

    async def test_persist_discovery_results_writes_containers(self, test_db):
        """Discovery results are correctly persisted to database."""
        containers = [
            FakeContainer("postgres-1", databases=[
                FakeDatabase("postgres-1", "postgres", "mydb"),
            ]),
            FakeContainer("redis-1"),
        ]

        async with aiosqlite.connect(test_db) as db:
            db.row_factory = aiosqlite.Row
            await persist_discovery_results(db, containers)

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM discovered_containers")
            row = await cursor.fetchone()
            assert row["cnt"] == 2

            cursor = await db.execute(
                "SELECT databases FROM discovered_containers WHERE name = ?",
                ("postgres-1",),
            )
            row = await cursor.fetchone()
            dbs = json.loads(row["databases"])
            assert len(dbs) == 1
            assert dbs[0]["db_name"] == "mydb"
            logger.info("Discovery persistence wrote %d containers", 2)

    async def test_persist_removes_stale_containers(self, test_db):
        """Re-persisting with fewer containers removes the stale ones."""
        # First scan: 3 containers
        containers_v1 = [
            FakeContainer("c1"), FakeContainer("c2"), FakeContainer("c3"),
        ]
        async with aiosqlite.connect(test_db) as db:
            await persist_discovery_results(db, containers_v1)

        # Second scan: only 2 containers (c2 removed)
        containers_v2 = [FakeContainer("c1"), FakeContainer("c3")]
        async with aiosqlite.connect(test_db) as db:
            db.row_factory = aiosqlite.Row
            await persist_discovery_results(db, containers_v2)

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM discovered_containers")
            row = await cursor.fetchone()
            assert row["cnt"] == 2
            logger.info("Stale container removed, %d remain", row["cnt"])

    async def test_concurrent_discovery_and_backup_writes(self, test_db):
        """Concurrent discovery persist and backup run insert don't deadlock."""
        # Seed a backup job
        async with aiosqlite.connect(test_db) as db:
            await db.execute(
                """INSERT INTO backup_jobs (id, name, schedule, targets, directories,
                   exclude_patterns, include_databases, include_flash)
                   VALUES ('job-1', 'Test', '0 0 * * *', '[]', '[]', '[]', 1, 1)"""
            )
            await db.commit()

        async def write_discovery():
            containers = [FakeContainer(f"container-{i}") for i in range(10)]
            async with aiosqlite.connect(test_db) as db:
                await persist_discovery_results(db, containers)
            logger.info("Discovery write completed")

        async def write_backup_run():
            async with aiosqlite.connect(test_db) as db:
                await db.execute(
                    """INSERT INTO job_runs (id, job_id, status, trigger)
                       VALUES ('run-1', 'job-1', 'running', 'manual')"""
                )
                await db.commit()
            logger.info("Backup run write completed")

        # Run both concurrently -- should not deadlock (SQLite busy_timeout=5000)
        await asyncio.gather(write_discovery(), write_backup_run())

        # Verify both writes succeeded
        async with aiosqlite.connect(test_db) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM discovered_containers")
            assert (await cursor.fetchone())["cnt"] == 10

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM job_runs")
            assert (await cursor.fetchone())["cnt"] == 1
            logger.info("Both concurrent writes succeeded without deadlock")

    async def test_empty_discovery_clears_all_containers(self, test_db):
        """Empty discovery scan removes all containers from DB."""
        # Seed containers
        containers = [FakeContainer("c1"), FakeContainer("c2")]
        async with aiosqlite.connect(test_db) as db:
            await persist_discovery_results(db, containers)

        # Empty scan
        async with aiosqlite.connect(test_db) as db:
            db.row_factory = aiosqlite.Row
            await persist_discovery_results(db, [])

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM discovered_containers")
            assert (await cursor.fetchone())["cnt"] == 0
            logger.info("Empty discovery cleared all containers")
