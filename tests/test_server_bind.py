"""A quick restart of the dashboard on the same port must not fail.

The real breakage: the built `.exe` sometimes did not open in a window. The server crashed
with `[Errno 10048] only one usage of each socket address is normally permitted`, and
this happened mostly on a repeated launch soon after closing. The cause is TIME_WAIT: a
just-closed connection (the same address and port) hangs in the system for a while, and
Windows without `SO_REUSEADDR` refuses a new `bind()` to the same port. The mutex does not
help here: it is not about the port but about the fact that a second process must not come
up at all; if exactly one process comes up, but twice in a row (the first closed, the
second launched right after it), the race was precisely in TIME_WAIT.

TIME_WAIT itself cannot be reliably caught in a test that lasts a second: in real life
seconds pass between closing and relaunching, here milliseconds. The first test below is
a smoke test of the same scenario (closed, immediately brought up again on the same
port); the second checks that a port that is really occupied by someone else still fails
honestly and is not silently hijacked.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from argus.app import Argus
from argus.core.config import Config
from argus.ui.server import serve


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run_once(app: Argus, port: int) -> None:
    """Bring up the server, wait for an answer, leave one connection hanging, then stop: like the
    ordinary start/close cycle of the app."""
    stop = asyncio.Event()
    task = asyncio.create_task(serve(app, port=port, stop=stop))

    url = f"http://127.0.0.1:{port}/"
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        break
    else:
        task.cancel()
        pytest.fail("the server did not come up")

    # A real HTTP connection, not just an open socket: the very thing that goes into TIME_WAIT
    # on the server's side when the server is closed.
    writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    await writer.drain()
    await reader.read(1024)

    stop.set()
    await asyncio.wait_for(task, timeout=5)

    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


async def test_restarting_on_the_same_port_right_after_closing_does_not_fail():
    """A smoke test of the same scenario as in production: closed, and brought up again at once."""
    port = free_port()

    app1 = Argus(Config(), with_screen=False, with_brain=False)
    await _run_once(app1, port)

    app2 = Argus(Config(), with_screen=False, with_brain=False)
    # Before, an OSError [Errno 10048] sometimes came in here.
    await _run_once(app2, port)


async def test_a_port_genuinely_held_by_someone_else_still_fails_clearly():
    """`SO_REUSEADDR` fixes a race with oneself, it does not hide a real conflict: a live foreign
    listener still does not let itself be taken over."""
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)

        app = Argus(Config(), with_screen=False, with_brain=False)
        with pytest.raises(OSError):
            await serve(app, port=port, stop=asyncio.Event())
