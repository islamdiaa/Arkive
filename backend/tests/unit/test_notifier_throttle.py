"""Tests for notifier throttling, deduplication, and recovery alerts."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notifier import MAX_PER_HOUR, THROTTLE_COOLDOWN, Notifier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_channel(channel_id="ch1", events=None, url="slack://hook"):
    """Build a fake notification_channels DB row (dict-like)."""
    if events is None:
        events = ["*"]
    row = {
        "id": channel_id,
        "type": "slack",
        "name": f"test-{channel_id}",
        "enabled": 1,
        "config": json.dumps({"url": url}),
        "events": json.dumps(events),
        "last_sent": None,
        "last_status": None,
    }
    # Make it behave like aiosqlite.Row (supports [] access)
    return row


def _make_mock_db(channels):
    """Create an async context manager mock for aiosqlite.connect that returns channels."""

    async def _mock_connect(*args, **kwargs):
        db = AsyncMock()
        db.row_factory = None

        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=channels)
        db.execute = AsyncMock(return_value=cursor)
        db.commit = AsyncMock()

        # Support async context manager
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=db)
        cm.__aexit__ = AsyncMock(return_value=False)
        return db

    return _mock_connect


@pytest.fixture
def mock_config(tmp_path):
    """Minimal config mock with a db_path."""
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "test.db")
    return cfg


@pytest.fixture
def mock_event_bus():
    """Mock event bus with async publish."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_apprise():
    """Mock apprise module and its Apprise class."""
    apprise_mod = MagicMock()
    apprise_mod.NotifyType.INFO = "info"
    apprise_mod.NotifyType.SUCCESS = "success"
    apprise_mod.NotifyType.WARNING = "warning"
    apprise_mod.NotifyType.FAILURE = "failure"

    instance = MagicMock()
    instance.notify = MagicMock(return_value=True)
    instance.async_notify = AsyncMock(return_value=True)
    apprise_mod.Apprise.return_value = instance

    return apprise_mod, instance


def _patch_db_for_channels(channels):
    """Return an aiosqlite.connect patch that yields channels on SELECT and accepts UPDATE/commit."""

    class FakeDB:
        def __init__(self):
            self.row_factory = None
            self._executed = []

        async def execute(self, sql, params=None):
            self._executed.append((sql, params))
            cursor = AsyncMock()
            cursor.fetchall = AsyncMock(return_value=channels)
            return cursor

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    return FakeDB


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNotifierThrottle:
    """Notification throttling, deduplication, and recovery tests."""

    @pytest.mark.asyncio
    async def test_send_within_cooldown_suppressed(self, mock_config, mock_event_bus, mock_apprise):
        """Same event on same channel within 24h cooldown -> second send suppressed."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
        ):
            mock_aiosqlite.connect.return_value = fake_db_cls()
            mock_aiosqlite.Row = MagicMock()

            # First send should go through
            results1 = await notifier.send("backup.failed", "Backup failed", "Error details", "error")
            assert len(results1) == 1
            assert results1[0]["status"] == "sent"
            assert apprise_instance.async_notify.call_count == 1

            # Reset DB mock for second call
            mock_aiosqlite.connect.return_value = fake_db_cls()

            # Second send within cooldown should be throttled
            results2 = await notifier.send("backup.failed", "Backup failed again", "Error details 2", "error")
            assert len(results2) == 1
            assert results2[0]["status"] == "throttled"
            # Apprise.notify should NOT have been called again
            assert apprise_instance.async_notify.call_count == 1

    @pytest.mark.asyncio
    async def test_send_after_cooldown_includes_summary(self, mock_config, mock_event_bus, mock_apprise):
        """After cooldown expires with suppressed messages, send includes summary."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.connect.return_value = fake_db_cls()
            mock_aiosqlite.Row = MagicMock()

            # Time starts at 1000
            mock_time.time.return_value = 1000.0

            # First send
            results1 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results1[0]["status"] == "sent"

            # Second send at 1001 — within cooldown, should be suppressed
            mock_time.time.return_value = 1001.0
            mock_aiosqlite.connect.return_value = fake_db_cls()
            results2 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results2[0]["status"] == "throttled"

            # Third send at 1002 — also suppressed
            mock_time.time.return_value = 1002.0
            mock_aiosqlite.connect.return_value = fake_db_cls()
            results3 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results3[0]["status"] == "throttled"

            # Now advance past cooldown (1000 + 901 = 1901)
            mock_time.time.return_value = 1000.0 + THROTTLE_COOLDOWN + 1
            mock_aiosqlite.connect.return_value = fake_db_cls()
            apprise_instance.async_notify.reset_mock()

            results4 = await notifier.send("backup.failed", "Backup failed", "Error details", "error")
            assert results4[0]["status"] == "sent"

            # Check the body included summary of suppressed messages
            call_kwargs = apprise_instance.async_notify.call_args
            sent_body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body", "")
            assert "2 more since last alert" in sent_body

    @pytest.mark.asyncio
    async def test_rate_limit_per_channel(self, mock_config, mock_event_bus, mock_apprise):
        """11th notification in an hour on a channel is rate-limited."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["*"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()

            base_time = 1000.0

            # Send MAX_PER_HOUR notifications with different event types
            # (to avoid cooldown throttling)
            for i in range(MAX_PER_HOUR):
                mock_time.time.return_value = base_time + i
                mock_aiosqlite.connect.return_value = fake_db_cls()
                results = await notifier.send(f"event.type.{i}", f"Title {i}", f"Body {i}", "info")
                assert results[0]["status"] == "sent", f"Send {i} should succeed"

            # 11th notification should be rate limited
            mock_time.time.return_value = base_time + MAX_PER_HOUR
            mock_aiosqlite.connect.return_value = fake_db_cls()
            results = await notifier.send("event.type.extra", "Title extra", "Body extra", "info")
            assert results[0]["status"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_recovery_alert(self, mock_config, mock_event_bus, mock_apprise):
        """After a backup.failed, a backup.success should get RECOVERED prefix."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed", "backup.success"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.connect.return_value = fake_db_cls()
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0

            # Send a failure
            await notifier.send("backup.failed", "Backup failed", "Disk full", "error")

            # Now send success — should trigger recovery
            mock_time.time.return_value = 2000.0
            mock_aiosqlite.connect.return_value = fake_db_cls()
            apprise_instance.async_notify.reset_mock()

            await notifier.send("backup.success", "Backup completed", "All good", "success")

            call_kwargs = apprise_instance.async_notify.call_args
            sent_title = call_kwargs.kwargs.get("title") or call_kwargs[1].get("title", "")
            assert sent_title.startswith("RECOVERED:")

    @pytest.mark.asyncio
    async def test_different_event_types_not_throttled(self, mock_config, mock_event_bus, mock_apprise):
        """Different event types on same channel should both be sent (no cross-throttle)."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.success", "discovery.completed"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.connect.return_value = fake_db_cls()
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0

            # Send first event type
            results1 = await notifier.send("backup.success", "Backup OK", "Done", "success")
            assert results1[0]["status"] == "sent"

            # Send different event type on same channel — should NOT be throttled
            mock_time.time.return_value = 1001.0
            mock_aiosqlite.connect.return_value = fake_db_cls()
            results2 = await notifier.send("discovery.completed", "Discovery done", "Found 5", "info")
            assert results2[0]["status"] == "sent"

            # Both should have been sent
            assert apprise_instance.async_notify.call_count == 2

    @pytest.mark.asyncio
    async def test_no_recovery_without_prior_failure(self, mock_config, mock_event_bus, mock_apprise):
        """backup.success without prior backup.failed should NOT get RECOVERED prefix."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.success"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.connect.return_value = fake_db_cls()
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0

            await notifier.send("backup.success", "Backup completed", "All good", "success")

            call_kwargs = apprise_instance.async_notify.call_args
            sent_title = call_kwargs.kwargs.get("title") or call_kwargs[1].get("title", "")
            assert not sent_title.startswith("RECOVERED:")
            assert sent_title == "Backup completed"

    @pytest.mark.asyncio
    async def test_rate_limit_resets_after_hour(self, mock_config, mock_event_bus, mock_apprise):
        """Rate limit should reset after timestamps age past 1 hour."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["*"])]
        fake_db_cls = _patch_db_for_channels(channels)

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()

            base_time = 1000.0

            # Fill up the rate limit
            for i in range(MAX_PER_HOUR):
                mock_time.time.return_value = base_time + i
                mock_aiosqlite.connect.return_value = fake_db_cls()
                await notifier.send(f"event.{i}", f"T{i}", f"B{i}", "info")

            # Should be rate limited now
            mock_time.time.return_value = base_time + MAX_PER_HOUR
            mock_aiosqlite.connect.return_value = fake_db_cls()
            results = await notifier.send("event.extra", "T", "B", "info")
            assert results[0]["status"] == "rate_limited"

            # Advance past 1 hour — all old timestamps should be pruned
            mock_time.time.return_value = base_time + 3601
            mock_aiosqlite.connect.return_value = fake_db_cls()
            results = await notifier.send("event.after_hour", "T", "B", "info")
            assert results[0]["status"] == "sent"
