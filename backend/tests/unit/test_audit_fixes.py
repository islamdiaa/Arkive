"""Tests for DB schema indexes, migration v3, and notifier memory cleanup."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.core.database import init_db, run_migrations
from app.services.notifier import THROTTLE_COOLDOWN, Notifier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "audit_test.db")


@pytest.fixture
def mock_config(tmp_path):
    """Minimal config mock with a db_path."""
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "notifier_test.db")
    return cfg


@pytest.fixture
def mock_event_bus():
    """Mock event bus with async publish."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# 1. DB Schema Indexes
# ---------------------------------------------------------------------------


class TestSchemaIndexes:
    """Verify that SCHEMA_SQL creates expected indexes on fresh init."""

    @pytest.mark.asyncio
    async def test_idx_snapshots_target_id_exists(self, db_path):
        """Schema should create idx_snapshots_target_id on the snapshots table."""
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(snapshots)")
            indexes = [row[1] for row in await cursor.fetchall()]

        assert "idx_snapshots_target_id" in indexes

    @pytest.mark.asyncio
    async def test_idx_activity_log_type_exists(self, db_path):
        """Schema should create idx_activity_log_type on the activity_log table."""
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(activity_log)")
            indexes = [row[1] for row in await cursor.fetchall()]

        assert "idx_activity_log_type" in indexes

    @pytest.mark.asyncio
    async def test_idx_snapshots_target_id_columns(self, db_path):
        """idx_snapshots_target_id should be on the target_id column."""
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_info(idx_snapshots_target_id)")
            cols = [row[2] for row in await cursor.fetchall()]

        assert cols == ["target_id"]

    @pytest.mark.asyncio
    async def test_idx_activity_log_type_columns(self, db_path):
        """idx_activity_log_type should be on the type column."""
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_info(idx_activity_log_type)")
            cols = [row[2] for row in await cursor.fetchall()]

        assert cols == ["type"]

    @pytest.mark.asyncio
    async def test_all_expected_indexes_present(self, db_path):
        """All expected indexes from SCHEMA_SQL should be present after init."""
        await init_db(db_path)

        expected_indexes = [
            ("job_runs", "idx_job_runs_job_id"),
            ("job_runs", "idx_job_runs_started_at"),
            ("restore_runs", "idx_restore_runs_started_at"),
            ("job_run_targets", "idx_job_run_targets_run_id"),
            ("job_run_databases", "idx_job_run_databases_run_id"),
            ("snapshots", "idx_snapshots_time"),
            ("snapshots", "idx_snapshots_target_id"),
            ("activity_log", "idx_activity_log_timestamp"),
            ("activity_log", "idx_activity_log_type"),
        ]

        async with aiosqlite.connect(db_path) as db:
            for table, index_name in expected_indexes:
                cursor = await db.execute(f"PRAGMA index_list({table})")
                indexes = [row[1] for row in await cursor.fetchall()]
                assert index_name in indexes, f"Missing index {index_name} on table {table}. Found: {indexes}"


# ---------------------------------------------------------------------------
# 2. DB Migration v3
# ---------------------------------------------------------------------------


class TestMigrationV3:
    """Verify that migration version 3 adds the new indexes to an existing DB."""

    @pytest.mark.asyncio
    async def test_migration_v3_adds_snapshots_target_id_index(self, db_path):
        """Migration v3 should add idx_snapshots_target_id if missing."""

        # Create a DB at schema version 2 — baseline schema without the v3 indexes.
        # We do this by init_db (which creates everything including the indexes),
        # then drop the target indexes and set version to 2.
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            await db.execute("DROP INDEX IF EXISTS idx_snapshots_target_id")
            await db.execute("DROP INDEX IF EXISTS idx_activity_log_type")
            # Set version to 2 so migration v3 is pending
            await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")
            await db.commit()

        # Verify indexes are gone before migration
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(snapshots)")
            indexes_before = [row[1] for row in await cursor.fetchall()]
        assert "idx_snapshots_target_id" not in indexes_before

        # Run migrations — should apply v3
        count = await run_migrations(db_path)
        assert count >= 1, f"Expected at least 1 migration applied, got {count}"

        # Verify indexes are now present
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(snapshots)")
            indexes_after = [row[1] for row in await cursor.fetchall()]
        assert "idx_snapshots_target_id" in indexes_after

    @pytest.mark.asyncio
    async def test_migration_v3_adds_activity_log_type_index(self, db_path):
        """Migration v3 should add idx_activity_log_type if missing."""
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            await db.execute("DROP INDEX IF EXISTS idx_snapshots_target_id")
            await db.execute("DROP INDEX IF EXISTS idx_activity_log_type")
            await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")
            await db.commit()

        # Verify index is gone
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(activity_log)")
            indexes_before = [row[1] for row in await cursor.fetchall()]
        assert "idx_activity_log_type" not in indexes_before

        await run_migrations(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(activity_log)")
            indexes_after = [row[1] for row in await cursor.fetchall()]
        assert "idx_activity_log_type" in indexes_after

    @pytest.mark.asyncio
    async def test_migration_v3_records_schema_version(self, db_path):
        """Migration v3 should insert a schema_version row for version 3."""
        await init_db(db_path)

        async with aiosqlite.connect(db_path) as db:
            await db.execute("DROP INDEX IF EXISTS idx_snapshots_target_id")
            await db.execute("DROP INDEX IF EXISTS idx_activity_log_type")
            await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")
            await db.commit()

        await run_migrations(db_path)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT version FROM schema_version WHERE version = 3")
            row = await cursor.fetchone()
        assert row is not None, "schema_version row for version 3 not found"

    @pytest.mark.asyncio
    async def test_migration_v3_idempotent_with_existing_indexes(self, db_path):
        """Migration v3 should succeed even if indexes already exist (CREATE IF NOT EXISTS)."""
        await init_db(db_path)

        # Indexes already exist from init_db. Set version to 2 so v3 runs again.
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")
            await db.commit()

        # Should not raise despite indexes already existing
        count = await run_migrations(db_path)
        assert count >= 1

        # Indexes should still be present
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA index_list(snapshots)")
            indexes = [row[1] for row in await cursor.fetchall()]
        assert "idx_snapshots_target_id" in indexes


# ---------------------------------------------------------------------------
# 3. Notifier Memory Cleanup
# ---------------------------------------------------------------------------


class TestNotifierMemoryCleanup:
    """Test that Notifier._cleanup_stale_state() properly manages memory."""

    def _make_notifier(self, mock_config, mock_event_bus):
        """Create a Notifier with _last_cleanup set to 0 so cleanup always runs."""
        notifier = Notifier(mock_config, mock_event_bus)
        notifier._last_cleanup = 0.0
        return notifier

    def test_removes_last_sent_entries_older_than_cooldown(self, mock_config, mock_event_bus):
        """Entries in _last_sent older than THROTTLE_COOLDOWN should be removed."""
        notifier = self._make_notifier(mock_config, mock_event_bus)

        now = time.time()
        # Stale entry: older than THROTTLE_COOLDOWN
        notifier._last_sent[("ch1", "backup.failed")] = now - THROTTLE_COOLDOWN - 100
        # Fresh entry: within THROTTLE_COOLDOWN
        notifier._last_sent[("ch2", "backup.success")] = now - 100

        notifier._cleanup_stale_state()

        assert ("ch1", "backup.failed") not in notifier._last_sent
        assert ("ch2", "backup.success") in notifier._last_sent

    def test_removes_empty_send_counts_channels(self, mock_config, mock_event_bus):
        """Empty _send_counts channel entries should be removed after cleanup."""
        notifier = self._make_notifier(mock_config, mock_event_bus)

        now = time.time()
        # Channel with only stale timestamps (older than 1 hour)
        notifier._send_counts["ch_stale"] = [now - 7200, now - 5000]
        # Channel with a recent timestamp
        notifier._send_counts["ch_active"] = [now - 100]

        notifier._cleanup_stale_state()

        assert "ch_stale" not in notifier._send_counts
        assert "ch_active" in notifier._send_counts
        assert len(notifier._send_counts["ch_active"]) == 1

    def test_caps_suppressed_counts_at_500(self, mock_config, mock_event_bus):
        """_suppressed_counts should be cleared when exceeding 500 entries."""
        notifier = self._make_notifier(mock_config, mock_event_bus)

        # Fill with 501 entries
        for i in range(501):
            notifier._suppressed_counts[(f"ch{i}", f"event.{i}")] = i

        assert len(notifier._suppressed_counts) == 501

        notifier._cleanup_stale_state()

        assert len(notifier._suppressed_counts) == 0

    def test_preserves_suppressed_counts_at_500_or_below(self, mock_config, mock_event_bus):
        """_suppressed_counts should NOT be cleared when at or below 500 entries."""
        notifier = self._make_notifier(mock_config, mock_event_bus)

        for i in range(500):
            notifier._suppressed_counts[(f"ch{i}", f"event.{i}")] = i

        notifier._cleanup_stale_state()

        assert len(notifier._suppressed_counts) == 500

    def test_caps_last_event_status_at_100(self, mock_config, mock_event_bus):
        """_last_event_status should be cleared when it exceeds 100 entries."""
        notifier = self._make_notifier(mock_config, mock_event_bus)

        # Fill with 101 entries
        for i in range(101):
            notifier._last_event_status[f"event.type.{i}"] = "failed"

        assert len(notifier._last_event_status) == 101

        notifier._cleanup_stale_state()

        assert len(notifier._last_event_status) == 0, (
            "Expected _last_event_status to be cleared when exceeding 100 entries"
        )

    def test_does_not_clear_last_event_status_at_100_or_below(self, mock_config, mock_event_bus):
        """_last_event_status should NOT be cleared when at or below 100 entries."""
        notifier = self._make_notifier(mock_config, mock_event_bus)

        # Exactly 100 entries
        for i in range(100):
            notifier._last_event_status[f"event.type.{i}"] = "failed"

        notifier._cleanup_stale_state()

        assert len(notifier._last_event_status) == 100

    def test_cleanup_skipped_within_10_min_interval(self, mock_config, mock_event_bus):
        """Cleanup should be skipped if called within 10 minutes of last cleanup."""
        notifier = Notifier(mock_config, mock_event_bus)

        now = time.time()
        # Set last cleanup to recent past (5 minutes ago)
        notifier._last_cleanup = now - 300

        # Add a stale entry that would be cleaned if cleanup actually ran
        stale_key = ("ch1", "backup.failed")
        notifier._last_sent[stale_key] = now - THROTTLE_COOLDOWN - 100

        notifier._cleanup_stale_state()

        # Stale entry should still be present because cleanup was skipped
        assert stale_key in notifier._last_sent

    def test_cleanup_runs_after_10_min_interval(self, mock_config, mock_event_bus):
        """Cleanup should run if more than 10 minutes since last cleanup."""
        notifier = Notifier(mock_config, mock_event_bus)

        now = time.time()
        # Set last cleanup to 11 minutes ago
        notifier._last_cleanup = now - 660

        # Add a stale entry
        stale_key = ("ch1", "backup.failed")
        notifier._last_sent[stale_key] = now - THROTTLE_COOLDOWN - 100

        notifier._cleanup_stale_state()

        # Stale entry should be removed because cleanup ran
        assert stale_key not in notifier._last_sent


# ---------------------------------------------------------------------------
# 4. Notifier auto-cleanup on send
# ---------------------------------------------------------------------------


class FakeDB:
    """Async context manager fake for aiosqlite.connect."""

    def __init__(self, channels):
        self.row_factory = None
        self._channels = channels

    async def execute(self, sql, params=None):
        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=self._channels)
        return cursor

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestNotifierAutoCleanupOnSend:
    """Test that _cleanup_stale_state runs automatically during send()."""

    @pytest.mark.asyncio
    async def test_send_triggers_cleanup_after_interval(self, mock_config, mock_event_bus):
        """send() should invoke cleanup which removes stale _last_sent entries."""
        import json

        channels = [
            {
                "id": "ch1",
                "type": "slack",
                "name": "test-ch1",
                "enabled": 1,
                "config": json.dumps({"url": "slack://hook"}),
                "events": json.dumps(["backup.success"]),
                "last_sent": None,
                "last_status": None,
            }
        ]

        apprise_mod = MagicMock()
        apprise_mod.NotifyType.INFO = "info"
        apprise_mod.NotifyType.SUCCESS = "success"
        apprise_mod.NotifyType.WARNING = "warning"
        apprise_mod.NotifyType.FAILURE = "failure"
        instance = MagicMock()
        instance.async_notify = AsyncMock(return_value=True)
        apprise_mod.Apprise.return_value = instance

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()

            # Start time: 100000
            base_time = 100000.0
            mock_time.time.return_value = base_time

            # Force _last_cleanup far enough back so cleanup runs
            notifier._last_cleanup = 0.0

            # Seed a stale _last_sent entry (older than THROTTLE_COOLDOWN)
            stale_key = ("ch_old", "old.event")
            notifier._last_sent[stale_key] = base_time - THROTTLE_COOLDOWN - 500

            # Seed a stale _suppressed_counts entry tied to the stale key
            notifier._suppressed_counts[stale_key] = 7

            mock_aiosqlite.connect.return_value = FakeDB(channels)

            # send() should call _cleanup_stale_state() which removes the stale entry
            await notifier.send("backup.success", "Backup OK", "Done", "success")

            # Verify stale _last_sent entry was cleaned up
            assert stale_key not in notifier._last_sent, "Stale _last_sent entry should have been cleaned by send()"
            # _suppressed_counts are preserved until consumed by next send (cap-based cleanup only)
            assert stale_key in notifier._suppressed_counts, (
                "_suppressed_counts should be preserved for next send summary"
            )

    @pytest.mark.asyncio
    async def test_send_does_not_cleanup_within_interval(self, mock_config, mock_event_bus):
        """send() should skip cleanup if within the 10-minute interval."""
        import json

        channels = [
            {
                "id": "ch1",
                "type": "slack",
                "name": "test-ch1",
                "enabled": 1,
                "config": json.dumps({"url": "slack://hook"}),
                "events": json.dumps(["backup.success"]),
                "last_sent": None,
                "last_status": None,
            }
        ]

        apprise_mod = MagicMock()
        apprise_mod.NotifyType.INFO = "info"
        apprise_mod.NotifyType.SUCCESS = "success"
        apprise_mod.NotifyType.WARNING = "warning"
        apprise_mod.NotifyType.FAILURE = "failure"
        instance = MagicMock()
        instance.async_notify = AsyncMock(return_value=True)
        apprise_mod.Apprise.return_value = instance

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()

            base_time = 100000.0
            mock_time.time.return_value = base_time

            # Set _last_cleanup to 5 minutes ago (within 10-min interval)
            notifier._last_cleanup = base_time - 300

            # Seed a stale entry
            stale_key = ("ch_old", "old.event")
            notifier._last_sent[stale_key] = base_time - THROTTLE_COOLDOWN - 500

            mock_aiosqlite.connect.return_value = FakeDB(channels)

            await notifier.send("backup.success", "Backup OK", "Done", "success")

            # Stale entry should still be present because cleanup was skipped
            assert stale_key in notifier._last_sent, "Stale entry should NOT be cleaned when within the 10-min interval"

    @pytest.mark.asyncio
    async def test_successive_sends_trigger_cleanup_at_interval(self, mock_config, mock_event_bus):
        """Two send() calls: first at t=0, second at t=700 should trigger cleanup."""
        import json

        channels = [
            {
                "id": "ch1",
                "type": "slack",
                "name": "test-ch1",
                "enabled": 1,
                "config": json.dumps({"url": "slack://hook"}),
                "events": json.dumps(["*"]),
                "last_sent": None,
                "last_status": None,
            }
        ]

        apprise_mod = MagicMock()
        apprise_mod.NotifyType.INFO = "info"
        apprise_mod.NotifyType.SUCCESS = "success"
        apprise_mod.NotifyType.WARNING = "warning"
        apprise_mod.NotifyType.FAILURE = "failure"
        instance = MagicMock()
        instance.async_notify = AsyncMock(return_value=True)
        apprise_mod.Apprise.return_value = instance

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()

            # First send at t=1000 — cleanup runs (_last_cleanup starts at 0)
            mock_time.time.return_value = 1000.0
            mock_aiosqlite.connect.return_value = FakeDB(channels)
            await notifier.send("event.a", "Title A", "Body A", "info")

            # Verify _last_cleanup was updated
            assert notifier._last_cleanup == 1000.0

            # Inject a stale entry after first cleanup
            stale_key = ("ch_stale", "stale.event")
            notifier._last_sent[stale_key] = 1000.0 - THROTTLE_COOLDOWN - 500

            # Second send at t=1300 (5 min later) — cleanup should NOT run
            mock_time.time.return_value = 1300.0
            mock_aiosqlite.connect.return_value = FakeDB(channels)
            await notifier.send("event.b", "Title B", "Body B", "info")

            assert stale_key in notifier._last_sent, "Stale entry should persist when cleanup does not run"

            # Third send at t=1700 (700s after first cleanup) — cleanup SHOULD run
            mock_time.time.return_value = 1700.0
            mock_aiosqlite.connect.return_value = FakeDB(channels)
            await notifier.send("event.c", "Title C", "Body C", "info")

            assert stale_key not in notifier._last_sent, (
                "Stale entry should be cleaned after the 10-min interval passes"
            )
