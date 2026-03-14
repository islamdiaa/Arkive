"""Tests for bandwidth throttling (#29) — bandwidth_limit setting validation and backup engine wiring."""

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Validation regex (mirrors settings.py)
# ---------------------------------------------------------------------------

_BANDWIDTH_RE = re.compile(r"^([1-9]\d*)?$")


class TestBandwidthLimitValidation:
    """Validate bandwidth_limit format matches restic --limit-upload expectations (KiB/s integer)."""

    @pytest.mark.parametrize("value", ["1024", "51200", "1048576", "1", "999", ""])
    def test_valid_values_pass(self, value):
        assert _BANDWIDTH_RE.match(value) is not None, f"Expected '{value}' to be valid"

    @pytest.mark.parametrize("value", ["50M", "100K", "1G", "abc", "50MB", "-1", "1.5", "50 M", "M", "0"])
    def test_invalid_values_fail(self, value):
        assert _BANDWIDTH_RE.match(value) is None, f"Expected '{value}' to be invalid"


# ---------------------------------------------------------------------------
# Settings API: bulk PUT accepts bandwidth_limit
# ---------------------------------------------------------------------------


class TestSettingsAPIBandwidthLimit:
    """Settings API accepts and rejects bandwidth_limit via PUT."""

    @pytest_asyncio.fixture
    async def settings_api_harness(self, tmp_path):
        """Reuse one app instance while swapping the backing temp DB per test."""
        from fastapi import FastAPI

        from app.api.settings import router
        from app.core.dependencies import get_db, get_event_bus, require_auth

        db_state = {"path": None}

        async def override_get_db():
            async with aiosqlite.connect(db_state["path"]) as db:
                db.row_factory = aiosqlite.Row
                yield db

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_auth] = lambda: None
        app.dependency_overrides[get_event_bus] = lambda: None

        @asynccontextmanager
        async def _client_for(db_name: str):
            db_path = tmp_path / db_name
            db_state["path"] = str(db_path)
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY,"
                    " value TEXT, encrypted INTEGER DEFAULT 0,"
                    " updated_at TEXT)"
                )
                await db.commit()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                yield client, db_path

        return _client_for

    @pytest.mark.asyncio
    async def test_put_valid_bandwidth_limit_accepted(self, settings_api_harness):
        """PUT /settings with valid bandwidth_limit (integer KiB/s) should not raise 422."""
        async with settings_api_harness("valid-bandwidth.db") as (client, _db_path):
            resp = await client.put("/settings", json={"bandwidth_limit": "1024"})
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_put_invalid_bandwidth_limit_rejected(self, settings_api_harness):
        """PUT /settings with suffix-style bandwidth_limit (e.g. '50M') should return 422."""
        async with settings_api_harness("invalid-bandwidth.db") as (client, _db_path):
            resp = await client.put("/settings", json={"bandwidth_limit": "50M"})
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_put_invalid_bandwidth_limit_k_rejected(self, settings_api_harness):
        """PUT /settings with '100K' should return 422 — suffixes not allowed."""
        async with settings_api_harness("invalid-bandwidth-k.db") as (client, _db_path):
            resp = await client.put("/settings", json={"bandwidth_limit": "100K"})
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_put_empty_bandwidth_limit_accepted(self, settings_api_harness):
        """PUT /settings with empty string bandwidth_limit disables throttling."""
        async with settings_api_harness("empty-bandwidth.db") as (client, _db_path):
            resp = await client.put("/settings", json={"bandwidth_limit": ""})
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_single_key_put_bandwidth_limit_valid(self, settings_api_harness):
        """PUT /settings/{key} accepts valid integer KiB/s bandwidth_limit."""
        async with settings_api_harness("single-key-valid.db") as (client, db_path):
            resp = await client.put("/settings/bandwidth_limit", json={"value": "1024"})
        assert resp.status_code == 200, resp.text

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'bandwidth_limit'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "1024"

    @pytest.mark.asyncio
    async def test_single_key_put_bandwidth_limit_invalid(self, settings_api_harness):
        """PUT /settings/{key} rejects suffix-style bandwidth_limit (e.g. '50M')."""
        async with settings_api_harness("single-key-invalid.db") as (client, _db_path):
            resp = await client.put("/settings/bandwidth_limit", json={"value": "50M"})
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_bandwidth_limit_zero_rejected(self, settings_api_harness):
        """bandwidth_limit '0' is rejected because restic treats --limit-upload 0 as unlimited."""
        async with settings_api_harness("zero-rejected.db") as (client, _db_path):
            resp = await client.put("/settings", json={"bandwidth_limit": "0"})
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_error_message_content(self, settings_api_harness):
        """422 error message mentions KiB/s."""
        async with settings_api_harness("error-message.db") as (client, _db_path):
            resp = await client.put("/settings", json={"bandwidth_limit": "1G"})
        assert resp.status_code == 422, resp.text
        assert "KiB/s" in resp.text


# ---------------------------------------------------------------------------
# BackupEngine: --limit-upload injected when bandwidth_limit is set
# ---------------------------------------------------------------------------


class TestBackupEngineBandwidthLimit:
    """BackupEngine.backup() inserts --limit-upload when bandwidth_limit is configured."""

    def _make_engine(self):
        from unittest.mock import MagicMock

        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = ":memory:"
        config.rclone_config = "/tmp/rclone.conf"
        return BackupEngine(config)

    @pytest.mark.asyncio
    async def test_backup_includes_limit_upload_when_set(self):
        """When bandwidth_limit is '1024', --limit-upload 1024 appears in command."""
        engine = self._make_engine()

        captured_cmd = []

        async def fake_run_command(cmd, **kwargs):
            captured_cmd.extend(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = (
                '{"message_type":"summary","snapshot_id":"abc123",'
                '"total_bytes_processed":100,"files_new":1,'
                '"files_changed":0}'
            )
            result.stderr = ""
            return result

        target = {"id": "t1", "name": "local", "type": "local", "config": {"path": "/data"}}

        with (
            patch.object(engine, "_get_password", new=AsyncMock(return_value="secret")),
            patch.object(engine, "_get_server_name", new=AsyncMock(return_value="tower")),
            patch.object(engine, "_get_bandwidth_limit", new=AsyncMock(return_value="1024")),
            patch("app.services.backup_engine.run_command", side_effect=fake_run_command),
        ):
            await engine.backup(target, ["/config"])

        assert "--host" in captured_cmd
        assert captured_cmd[captured_cmd.index("--host") + 1] == "tower"
        assert "--limit-upload" in captured_cmd
        idx = captured_cmd.index("--limit-upload")
        assert captured_cmd[idx + 1] == "1024"

    @pytest.mark.asyncio
    async def test_backup_omits_limit_upload_when_empty(self):
        """When bandwidth_limit is '', --limit-upload does not appear in command."""
        engine = self._make_engine()

        captured_cmd = []

        async def fake_run_command(cmd, **kwargs):
            captured_cmd.extend(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = (
                '{"message_type":"summary","snapshot_id":"abc123",'
                '"total_bytes_processed":100,"files_new":1,'
                '"files_changed":0}'
            )
            result.stderr = ""
            return result

        target = {"id": "t1", "name": "local", "type": "local", "config": {"path": "/data"}}

        with (
            patch.object(engine, "_get_password", new=AsyncMock(return_value="secret")),
            patch.object(engine, "_get_server_name", new=AsyncMock(return_value="tower")),
            patch.object(engine, "_get_bandwidth_limit", new=AsyncMock(return_value="")),
            patch("app.services.backup_engine.run_command", side_effect=fake_run_command),
        ):
            await engine.backup(target, ["/config"])

        assert "--host" in captured_cmd
        assert captured_cmd[captured_cmd.index("--host") + 1] == "tower"
        assert "--limit-upload" not in captured_cmd

    @pytest.mark.asyncio
    async def test_get_bandwidth_limit_returns_value_from_db(self):
        """_get_bandwidth_limit() reads an integer KiB/s value from settings."""
        import os
        import tempfile

        import aiosqlite

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY,"
                    " value TEXT, encrypted INTEGER DEFAULT 0,"
                    " updated_at TEXT)"
                )
                await db.execute("INSERT INTO settings (key, value) VALUES ('bandwidth_limit', '51200')")
                await db.commit()

            from app.services.backup_engine import BackupEngine

            config = MagicMock()
            config.db_path = db_path
            engine = BackupEngine(config)

            result = await engine._get_bandwidth_limit()
            assert result == "51200"
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_get_bandwidth_limit_returns_empty_when_not_set(self):
        """_get_bandwidth_limit() returns '' when no row exists."""
        import os
        import tempfile

        import aiosqlite

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY,"
                    " value TEXT, encrypted INTEGER DEFAULT 0,"
                    " updated_at TEXT)"
                )
                await db.commit()

            from app.services.backup_engine import BackupEngine

            config = MagicMock()
            config.db_path = db_path
            engine = BackupEngine(config)

            result = await engine._get_bandwidth_limit()
            assert result == ""
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_get_bandwidth_limit_returns_empty_on_missing_table(self):
        """_get_bandwidth_limit() returns '' when settings table does not exist (bootstrap/test)."""
        import os
        import tempfile

        import aiosqlite

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # DB exists but settings table was never created
            async with aiosqlite.connect(db_path) as db:
                await db.commit()

            from app.services.backup_engine import BackupEngine

            config = MagicMock()
            config.db_path = db_path
            engine = BackupEngine(config)

            result = await engine._get_bandwidth_limit()
            assert result == ""
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_get_bandwidth_limit_rejects_legacy_suffix_value(self):
        """_get_bandwidth_limit() returns '' for a legacy '100K' value stored in DB."""
        import os
        import tempfile

        import aiosqlite

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY,"
                    " value TEXT, encrypted INTEGER DEFAULT 0,"
                    " updated_at TEXT)"
                )
                await db.execute("INSERT INTO settings (key, value) VALUES ('bandwidth_limit', '100K')")
                await db.commit()

            from app.services.backup_engine import BackupEngine

            config = MagicMock()
            config.db_path = db_path
            engine = BackupEngine(config)

            result = await engine._get_bandwidth_limit()
            assert result == ""
        finally:
            os.unlink(db_path)
