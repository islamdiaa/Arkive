"""Log viewing and streaming API routes."""

import asyncio
import json

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import get_config, get_event_bus, require_auth

router = APIRouter(prefix="/logs", tags=["logs"], dependencies=[Depends(require_auth)])


@router.get("")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    lines: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    level: str | None = None,
    component: str | None = None,
    since: str | None = None,
    config=Depends(get_config),
):
    """Get recent log entries from log file, with filtering and pagination."""
    # Accept 'lines' as a legacy alias for 'limit'
    actual_limit = lines if lines is not None else limit
    log_file = config.log_dir / "arkive.log"
    if not log_file.exists():
        return {"items": [], "logs": [], "total": 0, "limit": actual_limit, "offset": offset, "has_more": False}

    all_lines = log_file.read_text().strip().split("\n")
    filtered = []
    for line in reversed(all_lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if level and entry.get("level", "").upper() != level.upper():
                continue
            if component and component not in entry.get("component", entry.get("logger", "")):
                continue
            if since and entry.get("timestamp", "") < since:
                continue
            filtered.append(entry)
        except json.JSONDecodeError:
            filtered.append({"message": line, "level": "INFO", "timestamp": ""})

    total = len(filtered)
    paginated = filtered[offset : offset + actual_limit]
    return {
        "items": paginated,
        "logs": paginated,
        "total": total,
        "limit": actual_limit,
        "offset": offset,
        "has_more": offset + actual_limit < total,
    }


@router.get("/stream")
async def stream_logs(
    token: str | None = None,
    config=Depends(get_config),
    event_bus=Depends(get_event_bus),
):
    """Stream logs via SSE."""
    queue = event_bus.subscribe()
    log_file = config.log_dir / "arkive.log"
    file_offset = log_file.stat().st_size if log_file.exists() else 0

    async def event_generator():
        nonlocal file_offset
        loop = asyncio.get_running_loop()
        last_ping = loop.time()
        try:
            while True:
                emitted = False

                # Consume direct log events if any publisher uses the bus.
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1)
                    if event.get("event") == "log":
                        yield {"event": "log", "data": json.dumps(event.get("data", {}))}
                        emitted = True
                except TimeoutError:
                    pass

                # Tail the structured log file so stream works even without
                # explicit event_bus.publish("log", ...) calls.
                if log_file.exists():
                    size = log_file.stat().st_size
                    if size < file_offset:
                        file_offset = 0
                    if size > file_offset:
                        with log_file.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(file_offset)
                            chunk = f.read()
                            file_offset = f.tell()
                        for line in chunk.splitlines():
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                entry = {
                                    "message": line,
                                    "level": "INFO",
                                    "component": "arkive",
                                    "timestamp": "",
                                }
                            yield {"event": "log", "data": json.dumps(entry)}
                            emitted = True

                now = loop.time()
                if not emitted and now - last_ping >= 30:
                    yield {"event": "ping", "data": "{}"}
                    last_ping = now
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@router.delete("")
async def clear_logs(config=Depends(get_config)):
    """Clear log file."""
    log_file = config.log_dir / "arkive.log"
    if log_file.exists():
        log_file.write_text("")
    return {"status": "cleared"}
