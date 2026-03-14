"""Tests for notifier 24-hour throttle cooldown per spec."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notifier import THROTTLE_COOLDOWN, Notifier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(channel_id="ch1", events=None, url="slack://hook"):
    """Build a fake notification_channels DB row."""
    if events is None:
        events = ["*"]
    return {
        "id": channel_id,
        "type": "slack",
        "name": f"test-{channel_id}",
        "enabled": 1,
        "config": json.dumps({"url": url}),
        "events": json.dumps(events),
        "last_sent": None,
        "last_status": None,
    }


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


@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "test.db")
    return cfg


@pytest.fixture
def mock_event_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_apprise():
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


# ---------------------------------------------------------------------------
# Tests: THROTTLE_COOLDOWN constant
# ---------------------------------------------------------------------------


class TestThrottleCooldownConstant:
    """Verify the throttle cooldown constant matches spec (24 hours)."""

    def test_throttle_cooldown_is_86400(self):
        """THROTTLE_COOLDOWN must equal 86400 seconds (24 hours)."""
        assert THROTTLE_COOLDOWN == 86400

    def test_throttle_cooldown_is_24_hours_in_seconds(self):
        """Verify 86400 == 24 * 60 * 60."""
        assert THROTTLE_COOLDOWN == 24 * 60 * 60


# ---------------------------------------------------------------------------
# Tests: Throttle suppression within 24hr window
# ---------------------------------------------------------------------------


class TestThrottle24HrWindow:
    """Verify same event within 24hr window is suppressed."""

    @pytest.mark.asyncio
    async def test_same_event_within_24hr_suppressed(self, mock_config, mock_event_bus, mock_apprise):
        """Same event on same channel within 24hr window must be throttled."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0
            mock_aiosqlite.connect.return_value = FakeDB(channels)

            # First send goes through
            results1 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results1[0]["status"] == "sent"
            assert apprise_instance.async_notify.call_count == 1

            # Advance 12 hours (still within 24hr window)
            mock_time.time.return_value = 1000.0 + (12 * 3600)
            mock_aiosqlite.connect.return_value = FakeDB(channels)

            results2 = await notifier.send("backup.failed", "Backup failed again", "Error", "error")
            assert results2[0]["status"] == "throttled"
            assert apprise_instance.async_notify.call_count == 1  # not sent again

    @pytest.mark.asyncio
    async def test_same_event_at_23hr_still_suppressed(self, mock_config, mock_event_bus, mock_apprise):
        """Same event at 23 hours 59 minutes (just before window expires) must be throttled."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 0.0
            mock_aiosqlite.connect.return_value = FakeDB(channels)

            await notifier.send("backup.failed", "Backup failed", "Error", "error")

            # 23:59:59 — just under 24 hours
            mock_time.time.return_value = THROTTLE_COOLDOWN - 1
            mock_aiosqlite.connect.return_value = FakeDB(channels)

            results = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results[0]["status"] == "throttled"


# ---------------------------------------------------------------------------
# Tests: Throttle allows send after 24hr window
# ---------------------------------------------------------------------------


class TestThrottle24HrExpiry:
    """Verify same event after 24hr window is allowed through."""

    @pytest.mark.asyncio
    async def test_same_event_after_24hr_allowed(self, mock_config, mock_event_bus, mock_apprise):
        """Same event after 24hr window must be sent (not throttled)."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 0.0
            mock_aiosqlite.connect.return_value = FakeDB(channels)

            # First send
            results1 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results1[0]["status"] == "sent"

            # Advance exactly past 24 hours
            mock_time.time.return_value = THROTTLE_COOLDOWN + 1
            mock_aiosqlite.connect.return_value = FakeDB(channels)
            apprise_instance.async_notify.reset_mock()

            # Second send — after cooldown expires, should go through
            results2 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results2[0]["status"] == "sent"
            assert apprise_instance.async_notify.call_count == 1

    @pytest.mark.asyncio
    async def test_send_after_24hr_resets_cooldown(self, mock_config, mock_event_bus, mock_apprise):
        """After a successful send post-cooldown, the window resets for another 24 hours."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]

        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            t0 = 0.0
            mock_time.time.return_value = t0
            mock_aiosqlite.connect.return_value = FakeDB(channels)

            # First send at t0
            await notifier.send("backup.failed", "Backup failed", "Error", "error")

            # Second send just after cooldown — allowed, resets window to t1
            t1 = THROTTLE_COOLDOWN + 1
            mock_time.time.return_value = t1
            mock_aiosqlite.connect.return_value = FakeDB(channels)
            results2 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results2[0]["status"] == "sent"

            # Third send 1 second after t1 — now within new 24hr window, should be throttled
            mock_time.time.return_value = t1 + 1
            mock_aiosqlite.connect.return_value = FakeDB(channels)
            results3 = await notifier.send("backup.failed", "Backup failed", "Error", "error")
            assert results3[0]["status"] == "throttled"
