"""Notification service using Apprise for 90+ notification services.

Includes per-channel rate limiting, repeated error suppression with cooldown,
and recovery alerts.
"""

import json
import logging
import time
from datetime import UTC, datetime

import aiosqlite

from app.core.config import ArkiveConfig
from app.core.event_bus import EventBus
from app.core.security import decrypt_value, is_encrypted

logger = logging.getLogger("arkive.notifier")

# Throttle constants
THROTTLE_COOLDOWN = 86400  # 24 hours per spec
MAX_PER_HOUR = 10


class Notifier:
    """Sends notifications via Apprise to configured channels."""

    def __init__(self, config: ArkiveConfig, event_bus: EventBus):
        self.config = config
        self.event_bus = event_bus

        # Throttle state
        # (channel_id, event_type) -> timestamp of last sent notification
        self._last_sent: dict[tuple[str, str], float] = {}
        # channel_id -> list of send timestamps (for per-hour rate limiting)
        self._send_counts: dict[str, list[float]] = {}
        # (channel_id, event_type) -> count of suppressed notifications
        self._suppressed_counts: dict[tuple[str, str], int] = {}
        # event_type -> last status ("success" / "failed")
        self._last_event_status: dict[str, str] = {}
        # Timestamp of last cleanup run
        self._last_cleanup: float = 0.0

    def _cleanup_stale_state(self) -> None:
        """Remove stale entries from throttle dicts to prevent unbounded growth."""
        now = time.time()
        # Only run cleanup every 10 minutes
        if now - self._last_cleanup < 600:
            return
        self._last_cleanup = now

        cutoff = now - THROTTLE_COOLDOWN
        # Prune _last_sent entries older than cooldown
        stale_keys = [k for k, ts in self._last_sent.items() if ts < cutoff]
        for k in stale_keys:
            del self._last_sent[k]

        # Prune _send_counts: remove timestamps > 1h and empty channels
        one_hour_ago = now - 3600
        stale_channels = []
        for ch, timestamps in self._send_counts.items():
            self._send_counts[ch] = [ts for ts in timestamps if ts > one_hour_ago]
            if not self._send_counts[ch]:
                stale_channels.append(ch)
        for ch in stale_channels:
            del self._send_counts[ch]

        # Cap _suppressed_counts to prevent unbounded growth.
        # Don't prune based on _last_sent — suppressed counts are consumed on next
        # successful send (to include "N more since last alert" in the body).
        max_suppressed = 500
        if len(self._suppressed_counts) > max_suppressed:
            self._suppressed_counts.clear()

        # Cap _last_event_status to prevent unbounded growth
        max_event_types = 100
        if len(self._last_event_status) > max_event_types:
            self._last_event_status.clear()

    def _is_rate_limited(self, channel_id: str) -> bool:
        """Check if a channel has exceeded its per-hour rate limit."""
        now = time.time()
        one_hour_ago = now - 3600

        if channel_id not in self._send_counts:
            self._send_counts[channel_id] = []

        # Prune timestamps older than 1 hour
        self._send_counts[channel_id] = [ts for ts in self._send_counts[channel_id] if ts > one_hour_ago]

        return len(self._send_counts[channel_id]) >= MAX_PER_HOUR

    def _is_throttled(self, channel_id: str, event_type: str) -> bool:
        """Check if a (channel, event_type) pair is within cooldown."""
        key = (channel_id, event_type)
        last = self._last_sent.get(key)
        if last is None:
            return False
        return (time.time() - last) < THROTTLE_COOLDOWN

    def _check_recovery(self, event_type: str) -> bool:
        """Check if this event represents a recovery from a previous failure.

        Returns True if the event_type indicates success and the corresponding
        failure event was the last status seen.
        """
        # Map success events to their failure counterparts
        recovery_pairs = {
            "backup.success": "backup.failed",
            "restore.success": "restore.failed",
        }

        failure_event = recovery_pairs.get(event_type)
        if failure_event is None:
            return False

        return self._last_event_status.get(failure_event) == "failed"

    def _record_send(self, channel_id: str, event_type: str) -> None:
        """Record that a notification was sent for throttle tracking."""
        now = time.time()
        key = (channel_id, event_type)

        self._last_sent[key] = now

        if channel_id not in self._send_counts:
            self._send_counts[channel_id] = []
        self._send_counts[channel_id].append(now)

        # Clear suppressed count since we just sent
        self._suppressed_counts.pop(key, None)

    async def send(self, event_type: str, title: str, body: str, severity: str = "info") -> list[dict]:
        """Send notification to all channels subscribed to this event type.

        Applies per-channel rate limiting, cooldown-based deduplication,
        and recovery alert detection before sending.
        """
        self._cleanup_stale_state()

        results = []
        try:
            import apprise
        except ImportError:
            logger.warning("Apprise not installed, skipping notifications")
            return results

        # Check for recovery before processing channels
        is_recovery = self._check_recovery(event_type)

        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM notification_channels WHERE enabled = 1")
            channels = await cursor.fetchall()

        for channel in channels:
            events = json.loads(channel["events"]) if channel["events"] else []
            if event_type not in events and "*" not in events:
                continue

            channel_id = channel["id"]
            config = json.loads(channel["config"]) if channel["config"] else {}
            url = config.get("url", "")
            if not url:
                continue
            # Decrypt Fernet-encrypted URLs before use
            if is_encrypted(url):
                try:
                    url = decrypt_value(url)
                except Exception:
                    logger.error("Decryption failed for channel %s — skipping", channel_id)
                    results.append(
                        {"channel_id": channel_id, "status": "failed", "error": "Credential decryption failed"}
                    )
                    continue
            key = (channel_id, event_type)

            # --- Per-channel rate limit check ---
            if self._is_rate_limited(channel_id):
                logger.info(
                    "Rate limited: channel %s exceeded %d/hour",
                    channel_id,
                    MAX_PER_HOUR,
                )
                results.append(
                    {
                        "channel_id": channel_id,
                        "status": "rate_limited",
                    }
                )
                continue

            # --- Cooldown / deduplication check ---
            if self._is_throttled(channel_id, event_type):
                self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1
                logger.info(
                    "Throttled: channel %s, event %s (suppressed %d)",
                    channel_id,
                    event_type,
                    self._suppressed_counts[key],
                )
                results.append(
                    {
                        "channel_id": channel_id,
                        "status": "throttled",
                    }
                )
                continue

            # --- Build final body with suppressed summary if applicable ---
            final_body = body
            suppressed = self._suppressed_counts.get(key, 0)
            if suppressed > 0:
                final_body += f"\n\n({suppressed} more since last alert)"

            # --- Recovery prefix ---
            final_title = title
            if is_recovery:
                final_title = f"RECOVERED: {title}"

            try:
                ap = apprise.Apprise()
                ap.add(url)

                notify_type = {
                    "info": apprise.NotifyType.INFO,
                    "success": apprise.NotifyType.SUCCESS,
                    "warning": apprise.NotifyType.WARNING,
                    "error": apprise.NotifyType.FAILURE,
                }.get(severity, apprise.NotifyType.INFO)

                sent = await ap.async_notify(title=final_title, body=final_body, notify_type=notify_type)

                status = "sent" if sent else "failed"
                results.append({"channel_id": channel_id, "status": status})

                if sent:
                    self._record_send(channel_id, event_type)

                # Update last_sent and last_status in DB
                async with aiosqlite.connect(self.config.db_path) as db:
                    await db.execute(
                        "UPDATE notification_channels SET last_sent = ?, last_status = ? WHERE id = ?",
                        (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), status, channel_id),
                    )
                    await db.commit()

            except Exception as e:
                logger.error("Notification failed for channel %s: %s", channel_id, e)
                results.append({"channel_id": channel_id, "status": "failed", "error": str(e)})

        # Track event status for recovery detection
        if "failed" in event_type:
            self._last_event_status[event_type] = "failed"
        elif "success" in event_type:
            # Clear the failure status for the corresponding failure event
            failure_event = event_type.replace(".success", ".failed")
            self._last_event_status[failure_event] = "success"

        # Also publish to event bus for SSE
        await self.event_bus.publish(
            "notification",
            {
                "event_type": event_type,
                "title": title,
                "severity": severity,
                "results": results,
            },
        )

        return results

    async def test_channel(self, url: str) -> dict:
        """Send a test notification to a URL (bypasses throttling)."""
        try:
            import apprise

            ap = apprise.Apprise()
            ap.add(url)
            sent = await ap.async_notify(
                title="Arkive Test Notification",
                body="If you see this, notifications are working correctly.",
                notify_type=apprise.NotifyType.INFO,
            )
            return {"status": "ok" if sent else "failed"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}
