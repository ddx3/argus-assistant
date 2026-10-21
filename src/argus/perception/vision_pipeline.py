"""Vision pipeline: screen frames -> screen text.

It is built like the speech one, and for the same reason: recognition blocks for a long
time (about 400 ms per frame, measured) and cannot be kept in the shared loop. It goes to
a separate thread, and to exactly one: parallel recognitions fight for the same CPU
cores and only get in each other's way.

`FrameGate` stands before recognition. Without it, on any video or just a blinking
cursor, the pipeline would burn the CPU continuously and to no purpose.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

from argus.capture.wgc import WindowStreamCache
from argus.capture.window import find_window
from argus.core.events import Frame, ScreenEvent
from argus.perception.framegate import FrameGate, GateSettings
from argus.perception.vision.base import VisionProvider

if TYPE_CHECKING:
    from argus.core.bus import Bus
    from argus.core.config import Config

log = logging.getLogger(__name__)


def build_vision(cfg: Config) -> VisionProvider:
    if cfg.ocr.provider == "rapidocr":
        from argus.perception.vision.ocr_rapid import RapidOCRProvider

        return RapidOCRProvider(cfg.ocr)
    raise ValueError(f"unknown screen-reading provider: {cfg.ocr.provider}")


class VisionPipeline:
    """Frame -> filter -> OCR -> ScreenEvent."""

    def __init__(self, bus: Bus, cfg: Config, vision: VisionProvider | None = None) -> None:
        self.bus = bus
        self.cfg = cfg
        self.vision = vision or build_vision(cfg)
        self.gate = FrameGate(GateSettings(**cfg.gate.model_dump()))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        self.events = 0
        self.empty = 0

        # It is turned off on the fly from the interface: reading the screen is the most noticeable
        # CPU load, and people often want to remove it.
        self.enabled = True
        self.paused = False
        self.target_missing = 0   # the pinned window was not found
        self.last_window: str | None = None

        # A live stream of frames of the pinned window. It is needed exactly because cropping from
        # the screen sees neither another desktop nor anything that is covered, and PrintWindow
        # does not capture video.
        self._pinned = WindowStreamCache()

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, self.vision.warmup)

    def _region(self, frame: Frame):
        """What exactly to read in this frame.

        There are two cases, and the user chooses both:

        * a window is pinned: we read it, wherever the user is looking;
        * otherwise, the whole screen.

        There is deliberately no following of the active window. It seemed convenient, but in
        practice it meant reading something random: switch to mail and we read the mail, open a
        dashboard and we read the dashboard. The user already knows what they want to follow, and
        says so once.

        Returns `(image, title)`, or `None` if there is nothing to read.
        """
        image = frame.image
        target = self.cfg.screen.target_window
        if not target:
            return image, None

        win = find_window(target, tuple(self.cfg.screen.skip_windows))
        if win is None:
            self.target_missing += 1
            return None
        # We capture the window by itself instead of cropping it from the screen: a pinned window
        # may be on another desktop or under another window.
        own = self._pinned.frame_for(win.hwnd, win.title)
        if own is not None:
            return own, win.title
        # The stream has not delivered its first frame yet; a crop is better than nothing.
        return win.crop(image), win.title

    def _recheck_in(self) -> float | None:
        """In how many seconds to reread the last frame without waiting for a new one; `None` means
        there is no need.
        """
        if not self.enabled or self.paused:
            return None
        wait = self.gate.wait_s()
        # The floor is not a whim: a zero timeout of `wait_for` would cancel the wait without
        # looking at the queue.
        return None if wait is None else max(0.05, wait + 0.05)

    async def _read(self, loop: asyncio.AbstractEventLoop, frame: Frame) -> bool:
        """Read a frame if the filter lets it through. `False` means it did not get as far as the filter."""
        if frame.image is None or not self.enabled or self.paused:
            return False

        region = self._region(frame)
        if region is None:
            return False
        image, window = region

        # We compute the fingerprint from what we read, not from the whole screen: otherwise
        # switching between windows that look the same would slip past the filter, and motion in
        # someone else's window would wake up reading.
        passed, frame_hash = self.gate.check(image)
        if not passed:
            return True

        blocks = await loop.run_in_executor(self._pool, self.vision.read, image)
        if not blocks:
            self.empty += 1
            return True
        self.events += 1
        self.last_window = window
        self.bus.publish(
            ScreenEvent(
                t=frame.t,
                blocks=blocks,
                frame_hash=f"{int(frame_hash):016x}",
                window=window,
            )
        )
        return True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        # The queue is short: a frame weighs 11 MB, and there is no reason to accumulate them. If
        # we cannot keep up, a fresh frame is worth more than a stale one anyway.
        sub = self.bus.subscribe(Frame, maxsize=4, name="vision")
        # The last received frame and when it arrived. DXGI sends a frame only when the picture
        # changes, so on a frozen screen there will be no new ones at all, yet we must read what is
        # there now: a frame held back by the rate filter, and the `heartbeat_s` deadlines, are
        # counted by the timer, not by the stream of frames.
        last: Frame | None = None
        seen_ns = 0
        try:
            while True:
                wait = self._recheck_in() if last is not None else None
                try:
                    frame = await (sub.get() if wait is None else asyncio.wait_for(sub.get(), wait))
                    last, seen_ns = frame, time.perf_counter_ns()
                    by_timer = False
                except TimeoutError:
                    # The frame is the same, but the read gets the current time: a screen event takes its place
                    # in the timeline where it was read, not where this frame once arrived.
                    frame = replace(last, t=last.t + time.perf_counter_ns() - seen_ns)
                    by_timer = True

                # A failure on one frame (screen reading crashed, a window vanished in the middle of the
                # call) must not kill the loop: it is unattended, and without it the screen would silently
                # stop being read.
                try:
                    reached_gate = await self._read(loop, frame)
                except Exception:
                    log.exception("screen reading: frame skipped")
                    reached_gate = False
                if by_timer and not reached_gate:
                    last = None         # nothing to read on the timer: we wait for a real frame instead of spinning
        finally:
            sub.close()

    @property
    def avg_ms(self) -> float:
        return getattr(self.vision, "avg_ms", 0.0)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._pinned.stop()
        self.vision.close()
