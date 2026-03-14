"""Phase 4: SQLite dump tests — real sqlite3 binary, no Docker needed."""

import os
import sqlite3

import pytest

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper


@pytest.mark.asyncio
class TestSQLiteDump:
    """Real SQLite dump via host sqlite3 binary."""

    async def test_sqlite_dump_success(self, tmp_config, fake_sqlite_db):
        """Real SQLite DB → dump succeeds with integrity ok."""
        dumper = DBDumper(docker_client=None, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-vaultwarden",
            db_type="sqlite",
            db_name="test.sqlite3",
            host_path=str(fake_sqlite_db),
        )

        result = await dumper._dump_sqlite(db)

        assert result.status == "success"
        assert result.integrity_check == "ok"
        assert result.dump_size_bytes > 0
        assert os.path.exists(result.dump_path)

        # Verify the dump is a valid SQLite DB with our data
        conn = sqlite3.connect(result.dump_path)
        rows = conn.execute("SELECT name FROM items ORDER BY id").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["alpha", "beta", "gamma"]

    async def test_sqlite_dump_no_host_path(self, tmp_config):
        """Missing host_path → status=failed."""
        dumper = DBDumper(docker_client=None, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-vaultwarden",
            db_type="sqlite",
            db_name="db.sqlite3",
            host_path=None,
        )

        result = await dumper._dump_sqlite(db)

        assert result.status == "failed"
        assert "host path" in (result.error or "").lower()

    async def test_sqlite_dump_invalid_path(self, tmp_config):
        """Non-existent path → status=failed."""
        dumper = DBDumper(docker_client=None, config=tmp_config)
        db = DiscoveredDatabase(
            container_name="fake-vaultwarden",
            db_type="sqlite",
            db_name="db.sqlite3",
            host_path="/tmp/nonexistent/path/db.sqlite3",
        )

        result = await dumper._dump_sqlite(db)

        assert result.status == "failed"
