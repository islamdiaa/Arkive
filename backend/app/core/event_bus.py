"""In-memory async pub/sub for SSE event streaming."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger("arkive.events")


class SubscriberHandle:
    """RAII-style handle returned by EventBus.subscribe().

    Call ``cleanup()`` (or use as a context manager) to deterministically
    remove the subscriber queue from the bus.  This prevents leaked queues
    when SSE connections are dropped.
    """

    def __init__(self, bus: "EventBus", queue: asyncio.Queue):
        self._bus = bus
        self.queue = queue

    def cleanup(self) -> None:
        """Remove the subscriber queue from the bus."""
        try:
            self._bus._subscribers.remove(self.queue)
        except ValueError:
            pass

    # Allow usage as a context-manager for convenience
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


class EventBus:
    """In-memory async pub/sub for SSE event streaming."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> SubscriberHandle:
        """Create a new subscriber queue wrapped in a SubscriberHandle."""
        # Increased from 100 to 1000 to handle event bursts during concurrent
        # backup + discovery + status polling.  A typical backup emits ~50-100
        # progress events; with multi-target uploads this can spike to ~500.
        # Slow SSE clients still drop events after 1000 queued.
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return SubscriberHandle(self, q)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue (legacy helper, prefer handle.cleanup())."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event to all subscribers. Drop for slow consumers.

        Iterates over a snapshot of the subscriber list so that concurrent
        cleanup (e.g. an SSE disconnect during publish) cannot mutate the
        list mid-iteration.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait({"event": event_type, "data": data})
            except asyncio.QueueFull:
                logger.warning("Dropping event %s for slow consumer", event_type)
