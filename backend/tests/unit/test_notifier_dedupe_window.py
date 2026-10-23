"""Tests for notifier deduplication window, per-job scoping and recovery alerts."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.notifier import MAX_PER_HOUR, THROTTLE_COOLDOWN, Notifier

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
# Helpers
# ---------------------------------------------------------------------------


def _sent_titles(apprise_instance):
    return [c.kwargs["title"] for c in apprise_instance.async_notify.call_args_list]


async def _send(notifier, mock_aiosqlite, channels, *args, **kwargs):
    mock_aiosqlite.connect.return_value = FakeDB(channels)
    return await notifier.send(*args, **kwargs)


# ---------------------------------------------------------------------------
# Tests: dedupe window constant
# ---------------------------------------------------------------------------


class TestDedupeWindowConstant:
    def test_throttle_cooldown_is_one_hour(self):
        """Repeated alerts are deduplicated for 1 hour (was 24h, which hid new failures)."""
        assert THROTTLE_COOLDOWN == 3600


# ---------------------------------------------------------------------------
# Tests: window behaviour
# ---------------------------------------------------------------------------


class TestDedupeWindow:
    @pytest.mark.asyncio
    async def test_same_event_within_window_suppressed(self, mock_config, mock_event_bus, mock_apprise):
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
            r1 = await _send(notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "Error", "error")
            assert r1[0]["status"] == "sent"

            mock_time.time.return_value = 1000.0 + 30 * 60  # 30 minutes later
            r2 = await _send(notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "Error", "error")
            assert r2[0]["status"] == "throttled"
            assert apprise_instance.async_notify.call_count == 1

    @pytest.mark.asyncio
    async def test_same_event_after_window_allowed(self, mock_config, mock_event_bus, mock_apprise):
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
            await _send(notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "Error", "error")

            mock_time.time.return_value = THROTTLE_COOLDOWN - 1
            r2 = await _send(notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "Error", "error")
            assert r2[0]["status"] == "throttled"

            mock_time.time.return_value = THROTTLE_COOLDOWN + 1
            r3 = await _send(notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "Error", "error")
            assert r3[0]["status"] == "sent"
            # The suppressed alert is summarised in the next one that goes out.
            assert "(1 more since last alert)" in apprise_instance.async_notify.call_args.kwargs["body"]


# ---------------------------------------------------------------------------
# Tests: per-job scoping and recovery
# ---------------------------------------------------------------------------


class TestJobScopedThrottle:
    @pytest.mark.asyncio
    async def test_failures_of_different_jobs_are_not_cross_throttled(self, mock_config, mock_event_bus, mock_apprise):
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
            r1 = await _send(
                notifier, mock_aiosqlite, channels, "backup.failed", "DB Dumps failed", "x", "error", job_id="job-db"
            )
            mock_time.time.return_value = 1060.0
            r2 = await _send(
                notifier,
                mock_aiosqlite,
                channels,
                "backup.failed",
                "Full Backup failed",
                "y",
                "error",
                job_id="job-full",
            )
            mock_time.time.return_value = 1120.0
            r3 = await _send(
                notifier, mock_aiosqlite, channels, "backup.failed", "DB Dumps failed", "x", "error", job_id="job-db"
            )

        assert [r1[0]["status"], r2[0]["status"], r3[0]["status"]] == ["sent", "sent", "throttled"]

    @pytest.mark.asyncio
    async def test_failure_after_recovery_alerts_again(self, mock_config, mock_event_bus, mock_apprise):
        """failure -> success -> failure (all within the window) must alert every transition."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed", "backup.success"])]
        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0
            r1 = await _send(
                notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "e", "error", job_id="j1"
            )
            mock_time.time.return_value = 1100.0
            r2 = await _send(
                notifier, mock_aiosqlite, channels, "backup.success", "Backup ok", "ok", "success", job_id="j1"
            )
            mock_time.time.return_value = 1200.0
            r3 = await _send(
                notifier, mock_aiosqlite, channels, "backup.failed", "Backup failed", "e", "error", job_id="j1"
            )

        assert [r1[0]["status"], r2[0]["status"], r3[0]["status"]] == ["sent", "sent", "sent"]
        assert _sent_titles(apprise_instance) == ["Backup failed", "RECOVERED: Backup ok", "Backup failed"]

    @pytest.mark.asyncio
    async def test_recovery_bypasses_success_throttle_and_rate_limit(self, mock_config, mock_event_bus, mock_apprise):
        """A recovery is delivered even if a success was sent recently and the hourly cap is hit."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["*"])]
        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0
            await _send(notifier, mock_aiosqlite, channels, "backup.success", "ok", "ok", "success", job_id="j1")
            mock_time.time.return_value = 1010.0
            await _send(notifier, mock_aiosqlite, channels, "backup.failed", "failed", "e", "error", job_id="j1")
            # Exhaust the hourly cap with unrelated events.
            for i in range(MAX_PER_HOUR):
                mock_time.time.return_value = 1020.0 + i
                await _send(notifier, mock_aiosqlite, channels, f"other.event{i}", "t", "b", "info")
            mock_time.time.return_value = 1100.0
            rec = await _send(notifier, mock_aiosqlite, channels, "backup.success", "ok", "ok", "success", job_id="j1")

        assert rec[0]["status"] == "sent"
        assert apprise_instance.async_notify.call_args.kwargs["title"] == "RECOVERED: ok"

    @pytest.mark.asyncio
    async def test_success_after_partial_is_a_recovery(self, mock_config, mock_event_bus, mock_apprise):
        """partial -> success -> partial: the success is a recovery and re-arms the partial alert."""
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.partial", "backup.success"])]
        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0
            await _send(notifier, mock_aiosqlite, channels, "backup.partial", "Incomplete", "e", "warning", job_id="j1")
            mock_time.time.return_value = 1100.0
            await _send(notifier, mock_aiosqlite, channels, "backup.success", "ok", "ok", "success", job_id="j1")
            mock_time.time.return_value = 1200.0
            await _send(notifier, mock_aiosqlite, channels, "backup.partial", "Incomplete", "e", "warning", job_id="j1")

        assert _sent_titles(apprise_instance) == ["Incomplete", "RECOVERED: ok", "Incomplete"]

    @pytest.mark.asyncio
    async def test_recovery_is_scoped_to_the_failed_job(self, mock_config, mock_event_bus, mock_apprise):
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed", "backup.success"])]
        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
            patch("app.services.notifier.time") as mock_time,
        ):
            mock_aiosqlite.Row = MagicMock()
            mock_time.time.return_value = 1000.0
            await _send(notifier, mock_aiosqlite, channels, "backup.failed", "A failed", "e", "error", job_id="a")
            mock_time.time.return_value = 1100.0
            await _send(notifier, mock_aiosqlite, channels, "backup.success", "B ok", "ok", "success", job_id="b")

        assert _sent_titles(apprise_instance) == ["A failed", "B ok"]

    @pytest.mark.asyncio
    async def test_send_without_job_id_is_backward_compatible(self, mock_config, mock_event_bus, mock_apprise):
        apprise_mod, apprise_instance = mock_apprise
        channels = [_make_channel("ch1", ["backup.failed"])]
        notifier = Notifier(mock_config, mock_event_bus)

        with (
            patch("app.services.notifier.aiosqlite") as mock_aiosqlite,
            patch.dict("sys.modules", {"apprise": apprise_mod}),
        ):
            mock_aiosqlite.Row = MagicMock()
            r = await _send(notifier, mock_aiosqlite, channels, "backup.failed", "t", "b", severity="error")
        assert r[0]["status"] == "sent"


class TestThrottleStatePruning:
    def test_expired_entries_are_dropped_and_recovery_state_is_capped(self, mock_config, mock_event_bus, monkeypatch):
        import app.services.notifier as notifier_mod

        notifier = Notifier(mock_config, mock_event_bus)
        notifier._last_sent[("ch1", "backup.failed", "deleted-job")] = 0.0
        notifier._suppressed_counts[("ch1", "backup.failed", "deleted-job")] = 3
        notifier._send_counts["old-channel"] = [0.0]
        monkeypatch.setattr(notifier_mod, "MAX_TRACKED_EVENT_STATES", 2)
        for i in range(4):
            notifier._last_event_status[("backup.failed", f"job{i}")] = "failed"

        now = 10 * 3600.0
        notifier._last_sent[("ch1", "backup.failed", "live-job")] = now - 60
        notifier._prune_state(now)

        assert ("ch1", "backup.failed", "deleted-job") not in notifier._last_sent
        assert ("ch1", "backup.failed", "deleted-job") not in notifier._suppressed_counts
        assert ("ch1", "backup.failed", "live-job") in notifier._last_sent  # still in cooldown
        assert "old-channel" not in notifier._send_counts
        assert list(notifier._last_event_status) == [("backup.failed", "job2"), ("backup.failed", "job3")]

    def test_pruning_is_rate_limited(self, mock_config, mock_event_bus):
        notifier = Notifier(mock_config, mock_event_bus)
        notifier._prune_state(1000.0)
        notifier._last_sent[("ch1", "e", "")] = 0.0
        notifier._prune_state(1000.0 + 60)  # within PRUNE_INTERVAL: untouched
        assert ("ch1", "e", "") in notifier._last_sent
