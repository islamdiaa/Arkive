"""Unit tests for app.core.event_bus — EventBus."""

import asyncio

import pytest

from app.core.event_bus import EventBus, SubscriberHandle


class TestEventBusSubscribe:
    """Test subscribe and unsubscribe."""

    def test_subscribe_returns_handle(self):
        bus = EventBus()
        handle = bus.subscribe()
        assert isinstance(handle, SubscriberHandle)
        assert isinstance(handle.queue, asyncio.Queue)
        assert len(bus._subscribers) == 1

    def test_handle_cleanup_removes_queue(self):
        bus = EventBus()
        handle = bus.subscribe()
        assert len(bus._subscribers) == 1
        handle.cleanup()
        assert len(bus._subscribers) == 0

    def test_handle_double_cleanup_is_safe(self):
        """Calling cleanup twice does not raise."""
        bus = EventBus()
        handle = bus.subscribe()
        handle.cleanup()
        handle.cleanup()  # Should not raise
        assert len(bus._subscribers) == 0

    def test_unsubscribe_legacy_still_works(self):
        """Legacy unsubscribe(queue) API still works."""
        bus = EventBus()
        handle = bus.subscribe()
        bus.unsubscribe(handle.queue)
        assert len(bus._subscribers) == 0

    def test_unsubscribe_nonexistent_is_safe(self):
        """Unsubscribing a queue that was never subscribed does not raise."""
        bus = EventBus()
        fake_q = asyncio.Queue()
        bus.unsubscribe(fake_q)  # Should not raise
        assert len(bus._subscribers) == 0

    def test_subscribe_unsubscribe_100_times_no_leak(self):
        """Subscribe/cleanup 100 times, verify 0 leaked queues."""
        bus = EventBus()
        handles = [bus.subscribe() for _ in range(100)]
        assert len(bus._subscribers) == 100
        for h in handles:
            h.cleanup()
        assert len(bus._subscribers) == 0

    def test_handle_context_manager(self):
        """SubscriberHandle works as a context manager."""
        bus = EventBus()
        with bus.subscribe() as handle:
            assert len(bus._subscribers) == 1
            assert isinstance(handle.queue, asyncio.Queue)
        assert len(bus._subscribers) == 0


class TestEventBusPublish:
    """Test event publishing."""

    async def test_publish_delivers_to_subscriber(self):
        bus = EventBus()
        handle = bus.subscribe()
        await bus.publish("backup:started", {"job_id": "j1"})
        assert not handle.queue.empty()
        event = handle.queue.get_nowait()
        assert event["event"] == "backup:started"
        assert event["data"]["job_id"] == "j1"

    async def test_multiple_subscribers(self):
        bus = EventBus()
        h1 = bus.subscribe()
        h2 = bus.subscribe()
        await bus.publish("backup:done", {"status": "ok"})
        # Both queues should have the event
        e1 = h1.queue.get_nowait()
        e2 = h2.queue.get_nowait()
        assert e1["event"] == "backup:done"
        assert e2["event"] == "backup:done"
        assert e1["data"]["status"] == "ok"
        assert e2["data"]["status"] == "ok"

    async def test_slow_consumer_doesnt_block(self):
        """Publishing more events than the queue maxsize does not raise."""
        bus = EventBus()
        handle = bus.subscribe()
        # Queue maxsize is 1000 (set in EventBus.subscribe)
        # Publishing 1001 events should not raise -- extra events are dropped
        for i in range(1001):
            await bus.publish("tick", {"i": i})
        # Queue should be full (1000 items) but no exception occurred
        assert handle.queue.qsize() == 1000
