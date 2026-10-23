"""Notification service using Apprise for 90+ notification services.

Includes per-channel rate limiting, deduplication of repeated alerts with a
short cooldown, and recovery alerts.

Throttle semantics:
- Repeated alerts are deduplicated per ``(channel, event_type, scope)`` for
  ``THROTTLE_COOLDOWN`` seconds. ``scope`` is the caller-supplied ``dedupe_key``
  or ``job_id`` (empty when neither is given), so a failure of one job never
  suppresses the alert for a different job.
- A success for a scope clears that scope's failure cooldown, so
  failure -> success -> failure always alerts twice.
- Recovery alerts (a success after a failure for the same scope) bypass the
  cooldown and the hourly rate limit — they are always delivered.
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
THROTTLE_COOLDOWN = 3600  # 1 hour dedupe window for repeated alerts
DEDUPE_WINDOW = THROTTLE_COOLDOWN
MAX_PER_HOUR = 10
PRUNE_INTERVAL = 600  # seconds between throttle-state cleanups
MAX_TRACKED_EVENT_STATES = 5000

# Success events and the failure events they recover from.
RECOVERY_PAIRS = {
    "backup.success": "backup.failed",
    "restore.success": "restore.failed",
}


# Problem events that aren't named "*.failed" but that a success recovers
# from, mapped to the failure event they are tracked as.
PROBLEM_ALIASES = {
    "backup.partial": "backup.failed",
}


def _failure_counterpart(event_type: str) -> str | None:
    """Return the failure event a success event recovers from, if any."""
    if event_type in RECOVERY_PAIRS:
        return RECOVERY_PAIRS[event_type]
    if event_type.endswith(".success"):
        return event_type[: -len(".success")] + ".failed"
    return None


class Notifier:
    """Sends notifications via Apprise to configured channels."""

    def __init__(self, config: ArkiveConfig, event_bus: EventBus):
        self.config = config
        self.event_bus = event_bus

        # Throttle state
        # (channel_id, event_type, scope) -> timestamp of last sent notification
        self._last_sent: dict[tuple[str, str, str], float] = {}
        # channel_id -> list of send timestamps (for per-hour rate limiting)
        self._send_counts: dict[str, list[float]] = {}
        # (channel_id, event_type, scope) -> count of suppressed notifications
        self._suppressed_counts: dict[tuple[str, str, str], int] = {}
        # (failure_event_type, scope) -> last status ("success" / "failed")
        self._last_event_status: dict[tuple[str, str], str] = {}
        self._last_pruned = 0.0

    def _is_rate_limited(self, channel_id: str) -> bool:
        """Check if a channel has exceeded its per-hour rate limit."""
        now = time.time()
        one_hour_ago = now - 3600

        if channel_id not in self._send_counts:
            self._send_counts[channel_id] = []

        # Prune timestamps older than 1 hour
        self._send_counts[channel_id] = [ts for ts in self._send_counts[channel_id] if ts > one_hour_ago]

        return len(self._send_counts[channel_id]) >= MAX_PER_HOUR

    def _is_throttled(self, channel_id: str, event_type: str, scope: str = "") -> bool:
        """Check if a (channel, event_type, scope) triple is within cooldown."""
        last = self._last_sent.get((channel_id, event_type, scope))
        if last is None:
            return False
        return (time.time() - last) < THROTTLE_COOLDOWN

    def _check_recovery(self, event_type: str, scope: str = "") -> bool:
        """True if ``event_type`` is a success following a failure for ``scope``."""
        failure_event = _failure_counterpart(event_type)
        if failure_event is None:
            return False
        return self._last_event_status.get((failure_event, scope)) == "failed"

    def _record_send(self, channel_id: str, event_type: str, scope: str = "") -> None:
        """Record that a notification was sent for throttle tracking."""
        now = time.time()
        key = (channel_id, event_type, scope)

        self._last_sent[key] = now

        if channel_id not in self._send_counts:
            self._send_counts[channel_id] = []
        self._send_counts[channel_id].append(now)

        # Clear suppressed count since we just sent
        self._suppressed_counts.pop(key, None)
        self._prune_state(now)

    def _prune_state(self, now: float) -> None:
        """Drop throttle state that can no longer matter, so scopes of deleted
        jobs and one-off dedupe keys don't accumulate for the process lifetime."""
        if now - self._last_pruned < PRUNE_INTERVAL:
            return
        self._last_pruned = now
        expired = [k for k, ts in self._last_sent.items() if now - ts >= THROTTLE_COOLDOWN]
        for key in expired:
            self._last_sent.pop(key, None)
            self._suppressed_counts.pop(key, None)
        for channel_id in [c for c, stamps in self._send_counts.items() if all(now - t >= 3600 for t in stamps)]:
            del self._send_counts[channel_id]
        # Recovery tracking must outlive the cooldown; only cap it.
        overflow = len(self._last_event_status) - MAX_TRACKED_EVENT_STATES
        for key in list(self._last_event_status)[: max(overflow, 0)]:
            del self._last_event_status[key]

    def _clear_failure_cooldown(self, failure_event: str, scope: str) -> None:
        """A success resets the failure cooldown for that scope on every channel."""
        for key in [k for k in self._last_sent if k[1] == failure_event and k[2] == scope]:
            self._last_sent.pop(key, None)
            self._suppressed_counts.pop(key, None)

    async def send(
        self,
        event_type: str,
        title: str,
        body: str,
        severity: str = "info",
        job_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> list[dict]:
        """Send notification to all channels subscribed to this event type.

        ``job_id`` / ``dedupe_key`` (optional, keyword) scope deduplication and
        recovery tracking so alerts for different jobs are independent.
        """
        results = []
        try:
            import apprise
        except ImportError:
            logger.warning("Apprise not installed, skipping notifications")
            return results

        scope = str(dedupe_key or job_id or "")
        failure_event = _failure_counterpart(event_type)
        is_success = failure_event is not None
        is_failure = (
            event_type.endswith(".failed") or "failed" in event_type.split(".")[-1] or event_type in PROBLEM_ALIASES
        )

        # Check for recovery before processing channels
        is_recovery = self._check_recovery(event_type, scope)
        if is_success and failure_event:
            # A success always re-arms failure alerts for this scope.
            self._clear_failure_cooldown(failure_event, scope)
            for problem, tracked_as in PROBLEM_ALIASES.items():
                if tracked_as == failure_event:
                    self._clear_failure_cooldown(problem, scope)

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
            key = (channel_id, event_type, scope)

            if not is_recovery:
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
                if self._is_throttled(channel_id, event_type, scope):
                    self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1
                    logger.info(
                        "Throttled: channel %s, event %s, scope %s (suppressed %d)",
                        channel_id,
                        event_type,
                        scope or "-",
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
                    self._record_send(channel_id, event_type, scope)

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
        if is_success and failure_event:
            self._last_event_status[(failure_event, scope)] = "success"
        elif is_failure:
            self._last_event_status[(PROBLEM_ALIASES.get(event_type, event_type), scope)] = "failed"

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
