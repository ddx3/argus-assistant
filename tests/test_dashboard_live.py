"""A check that the dashboard really serves data.

This file exists because of a real breakage. The interface opened but was empty, and the
buttons did not respond. The cause was not in the interface: `uvicorn` came as a
dependency of another package without the `websockets` library, and without it it
**silently returns 404** on an attempt to establish a connection instead of refusing to
start.

The previous test checked that the `/ws` route is registered in the application. It was
registered, and it still did not work, because the breakage was one level below, in the
server. Hence the rule: check not that a route exists but that data really arrives over
it. So a real server is started here and a real client connects to it.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from argus.app import Argus
from argus.core.config import Config
from argus.ui.server import build, check_websocket_support


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_websocket_support_is_installed():
    """The cheapest check for that very breakage."""
    check_websocket_support()


@pytest.fixture
async def live_server():
    """A real uvicorn on a free port, with no access to hardware."""
    import uvicorn

    app = Argus(Config(), with_screen=False, with_brain=False)
    port = free_port()
    config = uvicorn.Config(
        build(app), host="127.0.0.1", port=port,
        log_level="critical", access_log=False, ws="auto",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("the server did not come up")

    yield app, port

    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


async def test_websocket_delivers_state(live_server):
    """The main test: data really arrives over /ws.

    It is the absence of this test that let the breakage live all the way to the browser.
    """
    import websockets

    _, port = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

    assert "controls" in payload
    # Recognition waits for the button: the dashboard comes up in advance so that the person
    # has time to choose the window and the sources before anything random flows into the
    # timeline.
    assert payload["controls"]["running"] is False
    assert "levels" in payload
    assert "stages" in payload


async def test_websocket_keeps_pushing_updates(live_server):
    """The state must arrive by itself, not only when the connection opens."""
    import websockets

    _, port = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        second = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert first["controls"] == second["controls"]


async def test_control_endpoint_changes_the_pipeline(live_server):
    """A checkbox in the interface must reach the pipeline, not just the view."""
    import httpx

    app, port = live_server
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post("/control", json={"mic": False})
        assert r.status_code == 200
        assert r.json()["sources"]["mic"] is False

    from argus.core.events import Source

    assert Source.ME not in app.speech.enabled

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        await client.post("/control", json={"running": False})
    assert app.speech.paused


async def test_control_change_reaches_the_websocket(live_server):
    """After a checkbox is clicked the page must see the new state."""
    import httpx
    import websockets

    _, port = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            await client.post("/control", json={"screen": False})

        for _ in range(10):
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if payload["controls"]["sources"]["screen"] is False:
                return
    pytest.fail("the change did not reach the page")


async def test_page_is_served(live_server):
    import httpx

    _, port = live_server
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.get("/")
    assert r.status_code == 200
    # `/` is now the home page with the chat list; the working screen with the recognition
    # switch is `/chat/<id>` (checked in `tests/test_chats.py`).
    assert 'id="new-chat"' in r.text


async def test_preview_streams_several_frames(live_server):
    """The preview is a continuous stream, not one picture.

    Polling on a timer looked like a slideshow: the browser asked for a frame every N
    milliseconds. MJPEG holds one connection into which the server itself keeps sending
    frames, and the browser plays them in an ordinary `<img>` as video.
    """
    import httpx
    import numpy as np

    app, port = live_server
    # The server is up without screen capture, so we put the frame in by hand.
    app.sources["screen"] = True
    app._last_image = np.full((360, 640, 3), 90, dtype=np.uint8)
    got = 0
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    async with client, client.stream("GET", "/screen.mjpg", timeout=15) as r:
        assert r.status_code == 200
        assert "multipart/x-mixed-replace" in r.headers["content-type"]
        async for chunk in r.aiter_bytes():
            got += chunk.count(b"Content-Type: image/jpeg")
            if got >= 2:
                break
    assert got >= 2, "only one frame arrived — that is not a stream"
