"""Server-Sent Events streaming API routes."""

import asyncio
import json

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import get_event_bus, require_sse_auth

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/stream", dependencies=[Depends(require_sse_auth)])
async def event_stream(
    event_bus=Depends(get_event_bus),
):
    """Stream real-time events via SSE."""
    queue = event_bus.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {
                        "event": event.get("event", "message"),
                        "data": json.dumps(event.get("data", {})),
                    }
                except TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"type": "keepalive"})}
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())
