"""Critical tests for EventBus subscriber lifecycle.

Validates the full subscriber lifecycle:
- Subscribe returns a SubscriberHandle with a bounded queue
- Publish delivers to all subscribers
- Cleanup removes the queue
- Slow consumers don't block publishers (events dropped)
- Double cleanup is safe
- Context manager usage works
"""

import asyncio
import logging

import pytest

from app.core.event_bus import EventBus, SubscriberHandle

logger = logging.getLogger(__name__)


class TestSubscriberLifecycle:
    """Test the full subscribe -> publish -> cleanup lifecycle."""

    def test_subscribe_returns_handle_with_queue(self):
        """subscribe() should return a SubscriberHandle wrapping a bounded queue."""
        bus = EventBus()
        handle = bus.subscribe()
        assert isinstance(handle, SubscriberHandle)
        assert isinstance(handle.queue, asyncio.Queue)
        assert handle.queue.maxsize > 0
        assert len(bus._subscribers) == 1
        logger.info("SubscriberHandle created with queue maxsize=%d", handle.queue.maxsize)

    def test_multiple_subscribers(self):
        """Multiple subscribers should each get their own handle."""
        bus = EventBus()
        h1 = bus.subscribe()
        h2 = bus.subscribe()
        h3 = bus.subscribe()
        assert len(bus._subscribers) == 3
        assert h1.queue is not h2.queue
        assert h2.queue is not h3.queue
        logger.info("Created %d independent subscribers", len(bus._subscribers))

    def test_cleanup_removes_queue(self):
        """handle.cleanup() should remove the queue from subscribers."""
        bus = EventBus()
        handle = bus.subscribe()
        assert len(bus._subscribers) == 1

        handle.cleanup()
        assert len(bus._subscribers) == 0
        logger.info("Subscriber removed via cleanup, count=%d", len(bus._subscribers))

    def test_double_cleanup_is_safe(self):
        """Calling cleanup() twice should not raise."""
        bus = EventBus()
        handle = bus.subscribe()
        handle.cleanup()
        handle.cleanup()  # Should not raise
        assert len(bus._subscribers) == 0
        logger.info("Double cleanup handled safely")

    def test_legacy_unsubscribe_still_works(self):
        """Legacy unsubscribe(queue) should still work."""
        bus = EventBus()
        handle = bus.subscribe()
        bus.unsubscribe(handle.queue)
        assert len(bus._subscribers) == 0
        logger.info("Legacy unsubscribe still works")

    def test_unsubscribe_nonexistent_queue(self):
        """Unsubscribing a never-subscribed queue should not raise."""
        bus = EventBus()
        fake_q = asyncio.Queue()
        bus.unsubscribe(fake_q)  # Should not raise
        assert len(bus._subscribers) == 0
        logger.info("Nonexistent queue unsubscribe handled safely")

    def test_context_manager_cleanup(self):
        """SubscriberHandle as context manager should cleanup on exit."""
        bus = EventBus()
        with bus.subscribe() as handle:
            assert len(bus._subscribers) == 1
            assert isinstance(handle.queue, asyncio.Queue)
        assert len(bus._subscribers) == 0
        logger.info("Context manager cleanup worked")


class TestEventDelivery:
    """Test event delivery to subscribers."""

    async def test_publish_delivers_to_single_subscriber(self):
        """Published event should be delivered to the subscriber's queue."""
        bus = EventBus()
        handle = bus.subscribe()

        await bus.publish("backup:started", {"job_id": "j1"})

        assert not handle.queue.empty()
        event = handle.queue.get_nowait()
        assert event["event"] == "backup:started"
        assert event["data"]["job_id"] == "j1"
        logger.info("Event delivered: %s", event["event"])
        handle.cleanup()

    async def test_publish_delivers_to_all_subscribers(self):
        """Published event should be delivered to ALL subscribers."""
        bus = EventBus()
        h1 = bus.subscribe()
        h2 = bus.subscribe()

        await bus.publish("backup:completed", {"status": "success"})

        for h in [h1, h2]:
            assert not h.queue.empty()
            event = h.queue.get_nowait()
            assert event["event"] == "backup:completed"
            assert event["data"]["status"] == "success"
        logger.info("Event delivered to all %d subscribers", 2)
        h1.cleanup()
        h2.cleanup()

    async def test_cleaned_up_queue_stops_receiving(self):
        """After cleanup, the queue should not receive new events."""
        bus = EventBus()
        h1 = bus.subscribe()
        h2 = bus.subscribe()

        h1.cleanup()
        await bus.publish("backup:progress", {"percent": 50})

        assert h1.queue.empty(), "Cleaned-up queue should not receive events"
        assert not h2.queue.empty(), "Still-subscribed queue should receive events"
        logger.info("Cleaned-up queue correctly excluded from delivery")
        h2.cleanup()

    async def test_publish_with_no_subscribers(self):
        """Publishing with no subscribers should not raise."""
        bus = EventBus()
        await bus.publish("test:event", {"data": "value"})
        # No assertion needed -- just verify no exception
        logger.info("Publish with no subscribers completed safely")


class TestSlowConsumer:
    """Test slow consumer handling (queue full)."""

    async def test_slow_consumer_events_dropped_silently(self):
        """When queue is full, new events should be dropped without blocking."""
        bus = EventBus()
        handle = bus.subscribe()
        maxsize = handle.queue.maxsize

        # Fill the queue beyond capacity
        for i in range(maxsize + 10):
            await bus.publish("tick", {"i": i})

        assert handle.queue.qsize() == maxsize
        logger.info("Slow consumer: queue capped at maxsize=%d, extra events dropped", maxsize)
        handle.cleanup()

    async def test_slow_consumer_doesnt_affect_fast_consumer(self):
        """A full queue on one subscriber should not block delivery to others."""
        bus = EventBus()
        slow_h = bus.subscribe()
        fast_h = bus.subscribe()

        # Fill both queues beyond capacity
        for i in range(slow_h.queue.maxsize + 5):
            await bus.publish("tick", {"i": i})

        # Both capped at maxsize
        assert fast_h.queue.qsize() == fast_h.queue.maxsize
        assert slow_h.queue.qsize() == slow_h.queue.maxsize
        logger.info("Slow consumer did not block fast consumer")
        slow_h.cleanup()
        fast_h.cleanup()


class TestSubscriberCleanup:
    """Test that subscriber cleanup prevents resource leaks."""

    def test_subscriber_count_after_mixed_subscribe_cleanup(self):
        """Subscriber count should track subscribe/cleanup correctly."""
        bus = EventBus()
        handles = [bus.subscribe() for _ in range(5)]
        assert len(bus._subscribers) == 5

        # Cleanup half
        for h in handles[:3]:
            h.cleanup()

        assert len(bus._subscribers) == 2
        logger.info("Subscriber count correct after partial cleanup: %d",
                    len(bus._subscribers))

    def test_all_subscribers_cleaned_up(self):
        """After cleaning up all, subscriber list should be empty."""
        bus = EventBus()
        handles = [bus.subscribe() for _ in range(10)]
        for h in handles:
            h.cleanup()

        assert len(bus._subscribers) == 0
        logger.info("All subscribers cleaned up, count=%d", len(bus._subscribers))

    def test_no_leak_with_context_manager(self):
        """Context manager ensures no leaked subscribers."""
        bus = EventBus()
        for _ in range(5):
            with bus.subscribe():
                assert len(bus._subscribers) == 1
            assert len(bus._subscribers) == 0
        logger.info("No leaked subscribers after context manager usage")
