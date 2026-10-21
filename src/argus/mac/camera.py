"""Screen frames on macOS, with the same interface as `bettercam` on Windows.

`ScreenCapture` (`capture/screen.py`) knows three things about the camera: it is
created, it has a `grab()` that returns a BGR frame or `None`, and it has
`stop()`/`release()`. Here is the same shape on top of Quartz.

How macOS differs from DXGI, and what was done about it.

* **`None` when nothing changed is a gift of DXGI, not a property of the screen.**
  Quartz always returns a frame and charges 10-30 ms for it. So the camera limits the
  rate itself (`MAX_FPS`) and compares the frame with the previous one on a sparse
  pixel grid itself; an identical frame is returned as `None`, but no less often than
  once per `HEARTBEAT_S`, otherwise the preview on the page would freeze on a static
  screen.
* **Nominal resolution.** On Retina the frame would be four times larger and add nothing
  for reading text. We ask for one pixel per point (`kCGWindowImageNominalResolution`):
  as a bonus, window coordinates (in points) match the pixels of the frame, and cutting
  a window out of the screen (`WindowInfo.crop`) works without rescaling.
* **No permission, no error.** Without the screen recording permission the system
  returns not a refusal but a snapshot of wallpaper and our own windows alone. It cannot
  be told apart by the picture; the permission is reported by the audio complaints
  (`mac/audio.py`), which ask for it directly.

The request goes to Quartz through PyObjC, inside functions.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# More than five frames per second of the screen are not needed: text reading handles one
# frame every few seconds, and the model gets one frame per call. The page shows a preview
# with what there is.
MAX_FPS = 5.0

# Even on a still screen we return a frame at least once per this many seconds.
HEARTBEAT_S = 2.0

# The step of the grid on which frames are compared with each other.
GRID = 8

MAX_DISPLAYS = 16


def _quartz() -> Any:
    import Quartz

    return Quartz


def bytes_of(data: Any) -> bytes:
    """The contents of `NSData`/`CFData` that Quartz returns, as ordinary bytes."""
    try:
        return bytes(data)
    except TypeError:
        return bytes(data.bytes())


def cgimage_to_bgr(image: Any, q: Any | None = None) -> np.ndarray | None:
    """`CGImage` -> a BGR array (H, W, 3). `None` means empty or cannot be parsed.

    Quartz returns `BGRA` with premultiplied alpha (`kCGImageAlphaPremultipliedFirst` on
    little-endian), that is, B, G, R, A in memory already, so it is enough to drop the last
    byte. A row can be wider than the frame (alignment), so we read by `bytes_per_row`, not
    by the width.
    """
    if image is None:
        return None
    q = q or _quartz()
    width = int(q.CGImageGetWidth(image))
    height = int(q.CGImageGetHeight(image))
    if width <= 0 or height <= 0:
        return None
    stride = int(q.CGImageGetBytesPerRow(image))
    if int(q.CGImageGetBitsPerPixel(image)) != 32 or stride < width * 4:
        log.debug("unexpected frame format, bits: %s, row %s", q.CGImageGetBitsPerPixel(image), stride)
        return None
    provider = q.CGImageGetDataProvider(image)
    data = bytes_of(q.CGDataProviderCopyData(provider))
    if len(data) < stride * height:
        return None
    rows = np.frombuffer(data, dtype=np.uint8, count=stride * height).reshape(height, stride)
    pixels = rows[:, : width * 4].reshape(height, width, 4)
    return np.ascontiguousarray(pixels[:, :, :3])


def display_ids(q: Any | None = None) -> list[int]:
    """Active screens, the main one first (this is what `output_idx = 0` refers to)."""
    q = q or _quartz()
    err, ids, _count = q.CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
    if err or not ids:
        return [int(q.CGMainDisplayID())]
    main = int(q.CGMainDisplayID())
    return sorted((int(i) for i in ids), key=lambda i: (i != main, i))


class MacCamera:
    """A screenshot on request, with the `bettercam` camera interface."""

    def __init__(
        self,
        output_idx: int = 0,
        region: tuple[int, int, int, int] | None = None,
        q: Any | None = None,
        clock=time.monotonic,
    ) -> None:
        self._q = q or _quartz()
        self._region = region
        self._clock = clock
        ids = display_ids(self._q)
        if not 0 <= output_idx < len(ids):
            log.warning("display not found: #%s — using the main one", output_idx)
            output_idx = 0
        self._display = ids[output_idx]
        self._last_taken = 0.0
        self._last_sent = 0.0
        self._signature: np.ndarray | None = None

    # -- what we capture -----------------------------------------------------

    def bounds(self) -> tuple[float, float, float, float]:
        """The capture rectangle in points: the whole screen or `region` inside it."""
        box = self._q.CGDisplayBounds(self._display)
        left, top = float(box.origin.x), float(box.origin.y)
        width, height = float(box.size.width), float(box.size.height)
        if self._region is not None:
            r_left, r_top, r_right, r_bottom = self._region
            return (left + r_left, top + r_top, float(max(1, r_right - r_left)),
                    float(max(1, r_bottom - r_top)))
        return left, top, width, height

    def _snapshot(self) -> np.ndarray | None:
        q = self._q
        x, y, w, h = self.bounds()
        rect = q.CGRectMake(x, y, w, h) if hasattr(q, "CGRectMake") else ((x, y), (w, h))
        image = q.CGWindowListCreateImage(
            rect,
            q.kCGWindowListOptionOnScreenOnly,
            q.kCGNullWindowID,
            q.kCGWindowImageNominalResolution,
        )
        return cgimage_to_bgr(image, q)

    # -- the camera interface -------------------------------------------------

    def grab(self) -> np.ndarray | None:
        """A BGR frame if the screen has changed (or nothing was returned for a long time). Otherwise
        `None`.
        """
        now = self._clock()
        if now - self._last_taken < 1.0 / MAX_FPS:
            return None
        self._last_taken = now

        frame = self._snapshot()
        if frame is None:
            return None

        signature = frame[::GRID, ::GRID]
        unchanged = (
            self._signature is not None
            and self._signature.shape == signature.shape
            and np.array_equal(self._signature, signature)
        )
        self._signature = signature.copy()
        if unchanged and now - self._last_sent < HEARTBEAT_S:
            return None
        self._last_sent = now
        return frame

    def stop(self) -> None:
        pass

    def release(self) -> None:
        self._signature = None
