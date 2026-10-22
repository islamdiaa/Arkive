"""Server-Sent Events streaming API routes."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import get_event_bus, require_sse_auth

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/stream", dependencies=[Depends(require_sse_auth)])
async def event_stream(
    event_bus=Depends(get_event_bus),
):
    """Stream real-time events via SSE."""
    if event_bus is None:
        raise HTTPException(status_code=503, detail="Event bus unavailable")

    async def event_generator():
        # Subscribe inside the generator: the cleanup in `finally` then always
        # runs for a subscription that exists. Subscribing before returning the
        # response leaked the queue whenever the response was cancelled before
        # its first iteration.
        handle = event_bus.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(handle.queue.get(), timeout=30)
                    yield {
                        "event": event.get("event", "message"),
                        "data": json.dumps(event.get("data", {})),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"type": "keepalive"})}
        finally:
            handle.cleanup()

    return EventSourceResponse(event_generator())
