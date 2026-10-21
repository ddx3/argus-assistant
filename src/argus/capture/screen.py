"""Screen capture through DXGI Desktop Duplication.

One peculiarity of DXGI determines the whole design: `grab()` returns a frame **only if
the picture changed**, otherwise `None`. That is not an error but a gift, a free filter at
the driver level. Measured on this machine: 60 calls in a row on a static screen took 3 ms
and returned zero frames.

Two consequences follow:

* It can be polled often; it costs almost nothing. We spend effort only when something has
  really happened on the screen.
* A frame weighs ~11 MB (2560x1440, BGR). Copying and queueing that is out of the
  question, so the frame queue is kept short, and `Frame` carries a reference to the
  array, not a copy.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Self

from argus.capture.window import foreground_window
from argus.core.events import Frame

if TYPE_CHECKING:
    from argus.core.bus import Bus
    from argus.core.clock import Clock
    from argus.core.config import ScreenConfig

log = logging.getLogger(__name__)


class ScreenCapture:
    """Screen frames -> bus, in its own thread."""

    def __init__(self, bus: Bus, clock: Clock, cfg: ScreenConfig) -> None:
        self._bus = bus
        self._clock = clock
        self._cfg = cfg
        self._cam = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.frames = 0        # how many actually arrived
        self.polls = 0         # how many times we asked
        self.error: BaseException | None = None
        self.size: tuple[int, int] | None = None

    def open(self) -> None:
        if sys.platform == "darwin":
            # The same camera interface (`grab`/`stop`/`release`), a different system underneath.
            from argus.mac.camera import MacCamera

            self._cam = MacCamera(output_idx=self._cfg.output_idx, region=self._cfg.region)
        else:
            import bettercam

            self._cam = bettercam.create(
                output_idx=self._cfg.output_idx,
                output_color="BGR",
                region=self._cfg.region,
            )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="screen-capture", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = 1.0 / self._cfg.fps
        next_tick = time.perf_counter()
        try:
            while not self._stop.is_set():
                self.polls += 1
                image = self._cam.grab()
                if image is not None:
                    if self.size is None:
                        self.size = (image.shape[1], image.shape[0])
                    # We remember the active window right here, together with the frame: asking for it later
                    # would return wherever the person switched to during processing.
                    win = foreground_window()
                    self._bus.publish_threadsafe(
                        Frame(
                            t=self._clock.now(),
                            image=image,
                            seq=self.frames,
                            window=win.title if win else None,
                            window_rect=(
                                (win.left, win.top, win.right, win.bottom) if win else None
                            ),
                        )
                    )
                    self.frames += 1

                next_tick += interval
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    # If we fall behind (a heavy frame, a loaded system), we do not accumulate debt, otherwise
                    # after a pause a queue of instant polls would pour out.
                    next_tick = time.perf_counter()
        except BaseException as e:
            self.error = e
            log.exception("screen capture thread crashed")

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.error is None

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        cam, self._cam = self._cam, None
        if cam is not None:
            for method in ("stop", "release"):
                try:
                    getattr(cam, method)()
                except Exception as e:  # noqa: BLE001 — closing must not crash the exit
                    log.debug("%s() when closing the camera: %s", method, e)
            del cam

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
