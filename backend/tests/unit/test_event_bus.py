"""Unit tests for app.core.event_bus — EventBus."""

import asyncio

from app.core.event_bus import EventBus


class TestEventBusSubscribe:
    """Test subscribe and unsubscribe."""

    def test_subscribe_creates_queue(self):
        bus = EventBus()
        q = bus.subscribe()
        assert isinstance(q, asyncio.Queue)
        assert len(bus._subscribers) == 1

    def test_unsubscribe_removes_queue(self):
        bus = EventBus()
        q = bus.subscribe()
        assert len(bus._subscribers) == 1
        bus.unsubscribe(q)
        assert len(bus._subscribers) == 0

    def test_unsubscribe_nonexistent_is_safe(self):
        """Unsubscribing a queue that was never subscribed does not raise."""
        bus = EventBus()
        fake_q = asyncio.Queue()
        bus.unsubscribe(fake_q)  # Should not raise
        assert len(bus._subscribers) == 0


class TestEventBusPublish:
    """Test event publishing."""

    async def test_publish_delivers_to_subscriber(self):
        bus = EventBus()
        q = bus.subscribe()
        await bus.publish("backup:started", {"job_id": "j1"})
        assert not q.empty()
        event = q.get_nowait()
        assert event["event"] == "backup:started"
        assert event["data"]["job_id"] == "j1"

    async def test_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        await bus.publish("backup:done", {"status": "ok"})
        # Both queues should have the event
        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1["event"] == "backup:done"
        assert e2["event"] == "backup:done"
        assert e1["data"]["status"] == "ok"
        assert e2["data"]["status"] == "ok"

    async def test_slow_consumer_doesnt_block(self):
        """Publishing more events than the queue maxsize does not raise."""
        bus = EventBus()
        q = bus.subscribe()
        # Queue maxsize is 100 (set in EventBus.subscribe)
        # Publishing 101 events should not raise — extra events are dropped
        for i in range(101):
            await bus.publish("tick", {"i": i})
        # Queue should be full (100 items) but no exception occurred
        assert q.qsize() == 100
