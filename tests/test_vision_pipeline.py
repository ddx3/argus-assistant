"""Screen-reading loop: the re-check timer and resilience to failures.

DXGI delivers a frame only when the picture changes. So on a frozen screen there will be
no new frame, and everything the filter deferred (a frame held back by rate) and
everything due by deadline (heartbeat) must be read by the loop's timer, not by the frame
stream. Separately: a failure on one frame does not kill the loop; nobody watches it, and
the screen would silently stop being read.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from argus.core.bus import Bus
from argus.core.config import Config
from argus.core.events import Frame, ScreenBlock, ScreenEvent
from argus.perception import vision_pipeline
from argus.perception.framegate import FrameGate, GateSettings
from argus.perception.vision_pipeline import VisionPipeline


class FakeVision:
    """Screen reading without OCR: remembers which images it was given."""

    name = "fake"
    avg_ms = 0.0

    def __init__(self, fail_first: int = 0) -> None:
        self.images: list[np.ndarray] = []
        self.fail_first = fail_first

    def read(self, image):
        self.images.append(image)
        if len(self.images) <= self.fail_first:
            raise RuntimeError("read failed")
        return [ScreenBlock(text=f"read {len(self.images)}", box=(0, 0, 10, 10))]

    def warmup(self) -> None: ...
    def close(self) -> None: ...


def screen(x: int, y: int) -> np.ndarray:
    img = np.full((200, 320, 3), 40, dtype=np.uint8)
    img[y : y + 20, x : x + 80] = 230
    return img


async def started(pipe: VisionPipeline) -> asyncio.Task:
    task = asyncio.create_task(pipe.run())
    await asyncio.sleep(0)          # the subscription is set up at the first step
    return task


async def stopped(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_frame_held_back_by_the_rate_limit_is_read_when_the_interval_ends():
    """The screen changed right after a read and froze: without the timer, this change would
    never be read.
    """
    bus = Bus()
    vision = FakeVision()
    pipe = VisionPipeline(bus, Config(), vision)
    pipe.gate = FrameGate(GateSettings(min_interval_s=0.6, heartbeat_s=60.0))
    events = bus.subscribe(ScreenEvent, name="test")
    task = await started(pipe)

    first, second = screen(10, 10), screen(200, 150)
    bus.publish(Frame(t=1_000, image=first))
    await asyncio.wait_for(events.get(), 2)

    bus.publish(Frame(t=2_000, image=second))            # too soon: held back
    await asyncio.sleep(0.1)
    assert len(vision.images) == 1, "the held-back frame was read ahead of time"

    event = await asyncio.wait_for(events.get(), 2)       # no new frames, so the timer reads
    assert vision.images[1] is second
    assert event.t > 2_000, "the event time did not move by how long the frame was held"
    await stopped(task)


async def test_a_static_screen_is_reread_at_the_heartbeat_without_any_new_frame():
    bus = Bus()
    vision = FakeVision()
    pipe = VisionPipeline(bus, Config(), vision)
    pipe.gate = FrameGate(GateSettings(min_interval_s=0.1, heartbeat_s=0.3))
    events = bus.subscribe(ScreenEvent, name="test")
    task = await started(pipe)

    still = screen(10, 10)
    bus.publish(Frame(t=1_000, image=still))
    await asyncio.wait_for(events.get(), 2)
    await asyncio.wait_for(events.get(), 2)               # heartbeat is due, and there are no more frames
    assert len(vision.images) >= 2
    assert vision.images[1] is still
    await stopped(task)


async def test_a_failing_read_does_not_kill_the_loop(caplog):
    """A failure on one frame is a line in the log, not a silent death of the loop."""
    bus = Bus()
    vision = FakeVision(fail_first=1)
    pipe = VisionPipeline(bus, Config(), vision)
    pipe.gate = FrameGate(GateSettings(min_interval_s=0.1, heartbeat_s=60.0))
    events = bus.subscribe(ScreenEvent, name="test")
    task = await started(pipe)

    with caplog.at_level("ERROR", logger=vision_pipeline.log.name):
        bus.publish(Frame(t=1_000, image=screen(10, 10)))
        await asyncio.sleep(0.2)
        assert not task.done(), "the screen-reading loop died"
        assert any("frame skipped" in r.getMessage() for r in caplog.records)

        bus.publish(Frame(t=2_000, image=screen(200, 150)))
        event = await asyncio.wait_for(events.get(), 2)
    assert event.blocks[0].text == "read 2"
    await stopped(task)


async def test_nothing_to_read_does_not_make_the_timer_spin(monkeypatch):
    """The pinned window disappeared and the filter is waiting for a recheck: the loop must not
    spin idle.
    """
    bus = Bus()
    cfg = Config()
    cfg.screen.target_window = "Vanished window"
    pipe = VisionPipeline(bus, cfg, FakeVision())

    asked = []
    monkeypatch.setattr(vision_pipeline, "find_window", lambda needle, skip=(): asked.append(1))

    # The filter had already read a frame and held the next one back by rate: the re-check
    # deadline came long ago.
    gate = FrameGate(GateSettings(min_interval_s=0.1))
    import time

    now = time.perf_counter()
    gate.check(screen(10, 10), now=now - 10.0)
    gate.check(screen(200, 150), now=now - 9.95)
    assert gate.wait_s() == 0.0
    pipe.gate = gate

    task = await started(pipe)
    bus.publish(Frame(t=1_000, image=screen(10, 10)))
    await asyncio.sleep(0.5)
    assert len(asked) <= 4, f"the window was searched for {len(asked)} times in half a second: the loop is spinning"
    await stopped(task)


async def test_a_paused_pipeline_does_not_recheck():
    bus = Bus()
    vision = FakeVision()
    pipe = VisionPipeline(bus, Config(), vision)
    pipe.gate = FrameGate(GateSettings(min_interval_s=0.1, heartbeat_s=0.2))
    task = await started(pipe)

    bus.publish(Frame(t=1_000, image=screen(10, 10)))
    await asyncio.sleep(0.1)
    pipe.paused = True
    seen = len(vision.images)
    await asyncio.sleep(0.6)
    assert len(vision.images) == seen, "reading did not stop on pause"
    await stopped(task)
