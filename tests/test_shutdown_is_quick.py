"""Closing the app takes seconds and does not hang.

While the process is alive, it holds the single-instance lock: the next launch (the person
closed the window and immediately opened it again) waits for it. A measurement on
2026-10-07 found two delays:

* uvicorn without a deadline waited until all connections went away, and if one of them
  is endless (the frame stream `/screen.mjpg` of a second browser tab), the stop never
  ended: twenty seconds until the forced exit;
* two recognition channels closed their sessions one after another, and when the other
  side did not answer, that was two or three seconds for each.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from argus.app import Argus
from argus.core.bus import Bus
from argus.core.config import Config
from argus.perception.speech import SpeechPipeline
from argus.ui.server import serve


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_server_stops_even_if_a_stream_never_ends():
    """A browser tab with an open frame stream must not hold the process."""
    port = free_port()
    app = Argus(Config(), with_screen=True, with_brain=False)
    stop = asyncio.Event()
    task = asyncio.create_task(serve(app, port=port, stop=stop))

    for _ in range(200):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("the server did not come up")

    try:
        writer.write(
            f"GET /screen.mjpg HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.read(256), timeout=10)
        assert b"multipart/x-mixed-replace" in head, head

        # The client stays in place and does not read any further; it is the server that gets
        # closed.
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=8)
        assert time.monotonic() - started < 6, "the server waited for an endless connection"
    finally:
        writer.close()
        task.cancel()


class SlowToClose:
    """An engine whose session close is a wait for an answer that never comes."""

    name = "slow-to-close"
    per_channel = True

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.closed = threading.Event()

    def warmup(self) -> None: ...

    def transcribe(self, utterance):
        return None

    def close(self) -> None:
        time.sleep(self.seconds)
        self.closed.set()


def _pipeline(*engines) -> SpeechPipeline:
    from argus.core.events import Source

    speech = SpeechPipeline(Bus(), Config(), stt=engines[0])
    speech.engines = {Source.ME: engines[0], Source.THEM: engines[-1]}
    return speech


def test_recognition_sessions_close_together_not_one_after_another():
    first, second = SlowToClose(1.0), SlowToClose(1.0)
    speech = _pipeline(first, second)

    started = time.monotonic()
    speech.close()
    elapsed = time.monotonic() - started

    assert first.closed.is_set() and second.closed.is_set()
    assert elapsed < 1.7, f"sessions were closed one after another: {elapsed:.1f} s"


def test_a_session_that_never_closes_is_abandoned_after_the_budget(monkeypatch):
    monkeypatch.setattr(SpeechPipeline, "CLOSE_BUDGET_S", 0.4)
    stuck = SlowToClose(30.0)
    speech = _pipeline(stuck)

    started = time.monotonic()
    speech.close()

    assert time.monotonic() - started < 2.0, "a hung session holds the stop"
    assert not stuck.closed.is_set()


# -- the pipeline stop is limited in time -------------------------------------------


async def test_a_silent_recognition_does_not_hold_the_stop(monkeypatch):
    """Unfinished utterances are read to the end over the network, and when the cloud is silent,
    each channel waited its own fifteen seconds: the stop never finished, and the process was
    killed without closing the recording (measured)."""
    from argus.capture.audio import DeviceInfo

    app = Argus(Config(), with_screen=False, with_brain=False)
    mic = DeviceInfo(index=1, name="microphone", rate=16000, channels=1)
    loop = DeviceInfo(index=2, name="system", rate=48000, channels=2)
    app.audio.open = lambda: (mic, loop)             # type: ignore[method-assign]
    await app.start()

    async def never() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(app.speech, "drain", never)
    monkeypatch.setattr(Argus, "DRAIN_BUDGET_S", 0.3)

    started = time.monotonic()
    await asyncio.wait_for(app.stop(), timeout=10)

    assert time.monotonic() - started < 3.0


async def test_the_recording_is_closed_before_anything_that_can_hang(monkeypatch):
    """Without `meta.json` a recording is unreadable. It must be closed before audio, screen, and
    network: if one of them hangs, the process gets killed, and the recording is already
    closed."""
    order: list[str] = []
    app = Argus(Config(), with_screen=False, with_brain=False)
    monkeypatch.setattr(app, "_close_session", lambda: order.append("session"))
    monkeypatch.setattr(app.audio, "close", lambda: order.append("audio"))
    monkeypatch.setattr(app.speech, "close", lambda: order.append("speech"))

    await app.stop()

    assert order.index("session") < order.index("audio")
    assert order.index("session") < order.index("speech")


class _SlowMeter:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.closed = threading.Event()

    def close(self) -> None:
        time.sleep(self.seconds)
        self.closed.set()


def test_the_meter_flush_over_the_network_is_inside_the_same_budget(monkeypatch):
    """The usage meter reports to the account server on close, and an unreachable server held the
    stop for up to ten seconds (measured)."""
    monkeypatch.setattr(SpeechPipeline, "CLOSE_BUDGET_S", 0.4)
    engine = SlowToClose(0.0)
    speech = _pipeline(engine)
    speech.meter = _SlowMeter(30.0)

    started = time.monotonic()
    speech.close()

    assert time.monotonic() - started < 2.0, "the meter holds the stop"
    assert engine.closed.is_set()
