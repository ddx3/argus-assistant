"""Screen frames do not pile up in memory while the event loop is stalled.

The capture thread puts frames into the loop's queue thirty times a second, each up to
14 MB. A one-second stall (cleaning up sessions, closing a recording on a slow disk)
means hundreds of megabytes that are handed over only once the loop comes back to life,
and are immediately thrown away.
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np

from argus.core.bus import Bus
from argus.core.events import AudioChunk, Frame, Source


def frame(i: int) -> Frame:
    return Frame(t=i, image=np.zeros((2, 2, 3), dtype=np.uint8), seq=i)


def publish_while_the_loop_is_stuck(bus: Bus, events: list) -> None:
    """Publishing from another thread while the loop (this thread) is blocked waiting."""
    done = threading.Event()

    def producer() -> None:
        for event in events:
            bus.publish_threadsafe(event)
        done.set()

    thread = threading.Thread(target=producer)
    thread.start()
    assert done.wait(5)          # the loop is stalled: nothing is delivered, everything is in flight
    thread.join()


async def test_a_stuck_loop_keeps_only_the_newest_few_frames_in_flight():
    bus = Bus(asyncio.get_running_loop())
    bus.limit_pending(Frame, 3)
    sub = bus.subscribe(Frame, maxsize=100, name="t")

    publish_while_the_loop_is_stuck(bus, [frame(i) for i in range(50)])
    await asyncio.sleep(0.05)

    assert sub.qsize() == 3
    assert bus.shed == 47                       # the loss is counted, not silent


async def test_frames_flow_normally_once_the_loop_is_free_again():
    bus = Bus(asyncio.get_running_loop())
    bus.limit_pending(Frame, 3)
    sub = bus.subscribe(Frame, maxsize=100, name="t")

    publish_while_the_loop_is_stuck(bus, [frame(i) for i in range(10)])
    await asyncio.sleep(0.05)
    before = sub.qsize()

    for i in range(10):                          # the loop is free: the cap does not get in the way
        bus.publish_threadsafe(frame(100 + i))
        await asyncio.sleep(0.005)

    assert sub.qsize() == before + 10
    assert bus.shed == 7


async def test_other_events_are_never_shed():
    """Audio must not be lost at the input: there would be a hole in the recording."""
    bus = Bus(asyncio.get_running_loop())
    bus.limit_pending(Frame, 3)
    sub = bus.subscribe(AudioChunk, maxsize=100, name="t")
    chunks = [AudioChunk(t=i, source=Source.ME, samples=np.zeros(8, dtype=np.int16))
              for i in range(50)]

    publish_while_the_loop_is_stuck(bus, chunks)
    await asyncio.sleep(0.05)

    assert sub.qsize() == 50
    assert bus.shed == 0


async def test_without_a_limit_nothing_changes():
    bus = Bus(asyncio.get_running_loop())
    sub = bus.subscribe(Frame, maxsize=100, name="t")

    publish_while_the_loop_is_stuck(bus, [frame(i) for i in range(20)])
    await asyncio.sleep(0.05)

    assert sub.qsize() == 20 and bus.shed == 0


def test_the_app_bus_limits_frames():
    from argus.app import Argus
    from argus.core.config import Config

    app = Argus(Config(), with_screen=False, with_brain=False)
    assert app.bus._pending_limit[Frame] == 3
