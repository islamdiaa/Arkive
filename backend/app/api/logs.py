"""Log viewing and streaming API routes."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from app.core.dependencies import get_config, get_event_bus, require_auth

router = APIRouter(prefix="/logs", tags=["logs"], dependencies=[Depends(require_auth)])

# Only the tail of the (rotating, up to 50 MB) log file is scanned per request.
MAX_SCAN_BYTES = 8 * 1024 * 1024


def _parse_line(line: str) -> dict:
    try:
        entry = json.loads(line)
        if isinstance(entry, dict):
            return entry
    except json.JSONDecodeError:
        pass
    return {"message": line, "level": "INFO", "timestamp": ""}


def _read_log_tail(
    log_file: Path,
    level: str | None,
    component: str | None,
    since: str | None,
    max_scan_bytes: int = MAX_SCAN_BYTES,
) -> tuple[list[dict], bool]:
    """Return matching entries (newest first) from the last ``max_scan_bytes``.

    Blocking — call via ``asyncio.to_thread``. The second element is True when
    older content beyond the scanned window was not examined.
    """
    try:
        size = log_file.stat().st_size
    except FileNotFoundError:
        return [], False
    start = max(0, size - max_scan_bytes)
    with log_file.open("rb") as f:
        f.seek(start)
        data = f.read(size - start)
    truncated = start > 0
    lines = data.decode("utf-8", errors="replace").split("\n")
    if truncated and lines:
        lines = lines[1:]  # first line is very likely partial

    wanted_level = level.upper() if level else None
    filtered: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        entry = _parse_line(line)
        if wanted_level and str(entry.get("level", "")).upper() != wanted_level:
            continue
        if component and component not in str(entry.get("component", entry.get("logger", ""))):
            continue
        if since and str(entry.get("timestamp", "")) < since:
            continue
        filtered.append(entry)
    return filtered, truncated


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
    """Get recent log entries from the log file tail, with filtering and pagination."""
    # Accept 'lines' as a legacy alias for 'limit'
    actual_limit = lines if lines is not None else limit
    log_file = config.log_dir / "arkive.log"

    filtered, truncated = await asyncio.to_thread(_read_log_tail, log_file, level, component, since)

    total = len(filtered)
    paginated = filtered[offset:offset + actual_limit]
    return {
        "items": paginated,
        "logs": paginated,
        "total": total,
        "limit": actual_limit,
        "offset": offset,
        "has_more": offset + actual_limit < total or truncated,
        "truncated": truncated,
    }


def _read_appended(log_file: Path, file_offset: int) -> tuple[str, int]:
    """Read text appended since ``file_offset`` (handles truncation/rotation)."""
    try:
        size = log_file.stat().st_size
    except FileNotFoundError:
        return "", 0
    if size < file_offset:
        file_offset = 0
    if size == file_offset:
        return "", file_offset
    with log_file.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(file_offset)
        chunk = f.read(MAX_SCAN_BYTES)
        return chunk, f.tell()


@router.get("/stream")
async def stream_logs(
    token: str | None = None,
    config=Depends(get_config),
    event_bus=Depends(get_event_bus),
):
    """Stream logs via SSE."""
    log_file = config.log_dir / "arkive.log"

    async def event_generator():
        # Subscribe inside the generator so cleanup always pairs with it: if the
        # response is cancelled before the first iteration, nothing leaks.
        handle = event_bus.subscribe() if event_bus is not None else None
        try:
            try:
                file_offset = (await asyncio.to_thread(log_file.stat)).st_size
            except FileNotFoundError:
                file_offset = 0
            loop = asyncio.get_running_loop()
            last_ping = loop.time()
            while True:
                emitted = False

                # Consume direct log events if any publisher uses the bus.
                if handle is not None:
                    try:
                        event = await asyncio.wait_for(handle.queue.get(), timeout=1)
                        if event.get("event") == "log":
                            yield {"event": "log", "data": json.dumps(event.get("data", {}))}
                            emitted = True
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(1)

                # Tail the structured log file so stream works even without
                # explicit event_bus.publish("log", ...) calls.
                chunk, file_offset = await asyncio.to_thread(_read_appended, log_file, file_offset)
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    entry = _parse_line(line)
                    entry.setdefault("component", "arkive")
                    yield {"event": "log", "data": json.dumps(entry)}
                    emitted = True

                now = loop.time()
                if not emitted and now - last_ping >= 30:
                    yield {"event": "ping", "data": "{}"}
                    last_ping = now
        finally:
            if handle is not None:
                handle.cleanup()

    return EventSourceResponse(event_generator())


@router.delete("")
async def clear_logs(config=Depends(get_config)):
    """Clear log file."""
    log_file = config.log_dir / "arkive.log"
    if log_file.exists():
        await asyncio.to_thread(log_file.write_text, "")
    return {"status": "cleared"}
