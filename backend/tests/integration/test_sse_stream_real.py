"""SSE streaming through the real sse-starlette ``EventSourceResponse``.

The other SSE tests only check auth, because httpx's ASGI transport buffers the
whole response body. These tests read the stream itself:

- at the ASGI level, with a hand-written ``receive``/``send`` pair, so an event
  published on the bus arrives as an SSE frame and a client disconnect ends the
  response and releases the bus subscription;
- through a real uvicorn server, so a server shutdown with a client still
  connected closes the stream instead of hanging the shutdown.
"""

import asyncio
import contextlib

import httpx
import pytest
import uvicorn

from app.core.event_bus import EventBus
from tests.conftest import auth_headers, build_test_client, do_setup

pytestmark = pytest.mark.asyncio


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


async def _setup_with_real_bus(client: httpx.AsyncClient):
    """Complete setup, attach a real EventBus, return (app, bus, sse_token)."""
    data = await do_setup(client)
    app = client._transport.app
    bus = EventBus()
    app.state.event_bus = bus
    resp = await client.post("/api/auth/sse-token", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200, resp.text
    return app, bus, resp.json()["token"]


async def test_event_stream_delivers_events_and_cleans_up_on_disconnect(tmp_path):
    async with build_test_client(tmp_path) as client:
        app, bus, token = await _setup_with_real_bus(client)

        request_sent = False
        disconnect = asyncio.Event()
        messages: list[dict] = []
        body = bytearray()
        got_event = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if b"event: backup.started" in body:
                    got_event.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events/stream",
            "raw_path": b"/api/events/stream",
            "root_path": "",
            "query_string": f"token={token}".encode(),
            "headers": [(b"host", b"test"), (b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }

        task = asyncio.create_task(app(scope, receive, send))
        try:
            await _wait_until(lambda: len(bus._subscribers) == 1)
            await bus.publish("backup.started", {"job_id": "job-1"})
            await asyncio.wait_for(got_event.wait(), timeout=5)

            start = messages[0]
            assert start["type"] == "http.response.start"
            assert start["status"] == 200
            headers = dict(start["headers"])
            assert headers[b"content-type"].startswith(b"text/event-stream")
            assert b'data: {"job_id": "job-1"}' in body

            disconnect.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # The generator's finally ran: the subscription is gone.
        assert bus._subscribers == []


async def test_event_stream_closes_on_uvicorn_shutdown(tmp_path):
    async with build_test_client(tmp_path) as client:
        app, bus, token = await _setup_with_real_bus(client)

        config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_config=None, access_log=False)
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())
        try:
            await _wait_until(lambda: server.started, timeout=10)
            port = server.servers[0].sockets[0].getsockname()[1]

            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
                async with http.stream("GET", f"http://127.0.0.1:{port}/api/events/stream?token={token}") as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")

                    lines = resp.aiter_lines()
                    await _wait_until(lambda: len(bus._subscribers) == 1)
                    await bus.publish("backup.completed", {"job_id": "job-2"})

                    async def read_event():
                        seen = []
                        async for line in lines:
                            seen.append(line)
                            if line.startswith("data:") and "job-2" in line:
                                return seen
                        return seen

                    seen = await asyncio.wait_for(read_event(), timeout=5)
                    assert "event: backup.completed" in seen

                    # Shut the server down while the client is still connected.
                    server.should_exit = True

                    async def drain():
                        async for _ in lines:
                            pass

                    # The stream ends instead of staying open until the
                    # client goes away.
                    with contextlib.suppress(httpx.RemoteProtocolError, httpx.ReadError):
                        await asyncio.wait_for(drain(), timeout=10)

            await asyncio.wait_for(serve_task, timeout=10)
        finally:
            server.should_exit = True
            if not serve_task.done():
                serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task

        assert bus._subscribers == []
