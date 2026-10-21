"""Filter out frames in which nothing significant has changed.

This is the second level of filtering. The first is provided by Windows itself: DXGI
returns a frame only when the picture has changed, so on a static screen we spend
nothing at all. But for the driver "changed" means any pixel: a blinking cursor, a
ticking tray clock, a button animation twitching. Such frames must not reach OCR: one
screen recognition costs about 400 ms, and with a blinking cursor we would burn the CPU
continuously.

So we compute a perceptual hash, a coarse fingerprint of the picture that is robust to
small details. A frame goes further only if the fingerprint differs noticeably.

We use dhash: the picture is shrunk to 9x8, neighboring pixels are compared pairwise,
and the result is 64 bits. It compares the *relations* between brightness values, not
the brightness values themselves, so it does not react to a change of backlight or to
smooth shifts of the background.

It pays for this robustness with the same coarseness: one new line of text on a
2560x1440 screen changes 0-1 bits out of 64 (measured), and a threshold of 3 bits does not
see it. So tiles stand next to it: the mean brightness in a 32x18 grid (80x80 pixels per
tile on such a screen). A line of code or a message repaints three to six tiles by
several levels, while a cursor, a clock digit or a ripple in one place touches one or two.
The frame also goes further when three or more tiles have shifted. A brightness shift
that is the same in neighboring tiles (backlight, a background shift) does not count,
just as with dhash.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

HASH_W, HASH_H = 9, 8
BITS = (HASH_W - 1) * HASH_H  # 64

TILES_W, TILES_H = 32, 18


def dhash(image: np.ndarray) -> np.uint64:
    """A perceptual fingerprint of a frame: 64 bits."""
    import cv2

    small = cv2.resize(image, (HASH_W, HASH_H), interpolation=cv2.INTER_AREA)
    if small.ndim == 3:
        small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return np.uint64(int("".join("1" if b else "0" for b in bits), 2))


def distance(a: np.uint64, b: np.uint64) -> int:
    """How many bits differ: the Hamming distance."""
    return (int(a) ^ int(b)).bit_count()


def tiles(image: np.ndarray) -> np.ndarray:
    """Average brightness of the frame over a 32x18 grid: for local changes that dhash does not see."""
    import cv2

    small = cv2.resize(image, (TILES_W, TILES_H), interpolation=cv2.INTER_AREA)
    if small.ndim == 3:
        small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return small.astype(np.int16)


@dataclass(slots=True)
class GateSettings:
    # How many bits out of 64 must change for a frame to count as new.
    # 3 bits catch a window change and large changes, but do not react to the cursor.
    # They do not catch one new line of text; the tiles below are for that.
    min_distance: int = 3

    # Do not let frames through more often than once per this many seconds, even if the
    # picture jumps around: a full-screen video changes every frame, and recognizing it 10
    # times a second is pointless and impossible.
    min_interval_s: float = 1.0

    # Let a frame through even if it has not changed, when nothing has happened for a long
    # time. This keeps the timeline from "freezing" on a static screen.
    heartbeat_s: float = 30.0

    # Local changes: a tile counts as shifted if its mean brightness moved by more than
    # `tile_delta` levels (out of 255), and the frame goes further when there are at least
    # `tile_min_changed` such tiles. A cursor and a clock digit touch one or two tiles by one
    # to three levels, while a line of text touches three or more.
    tile_delta: int = 3
    tile_min_changed: int = 3


class FrameGate:
    """Decides whether this frame is worth recognizing."""

    def __init__(self, settings: GateSettings | None = None) -> None:
        self.s = settings or GateSettings()
        self._last_hash: np.uint64 | None = None
        self._last_tiles: np.ndarray | None = None
        self._last_passed = 0.0
        # The last frame was dropped only because of the rate limit: the picture behind it is new,
        # and if the screen freezes after that, no new frame will come. It has to be re-read on a
        # timer (`wait_s`).
        self._deferred = False

        self.seen = 0
        self.passed = 0
        self.skipped_same = 0
        self.skipped_rate = 0

    def check(self, image: np.ndarray, now: float | None = None) -> tuple[bool, np.uint64]:
        """Return (whether to skip, fingerprint)."""
        now = time.perf_counter() if now is None else now
        self.seen += 1
        h = dhash(image)

        if self._last_hash is None:
            self._accept(now, h, image)
            return True, h

        moved = distance(h, self._last_hash) >= self.s.min_distance or self._tiles_moved(image)
        since = now - self._last_passed

        if not moved:
            self._deferred = False
            if since >= self.s.heartbeat_s:
                self._accept(now, h, image)
                return True, h
            self.skipped_same += 1
            return False, h

        if since < self.s.min_interval_s:
            # The picture is live but too fast, so we wait.
            # We do not remember the fingerprint: otherwise the change would be "smeared" over a series
            # of small steps, each below the threshold, and the frame would never pass.
            self._deferred = True
            self.skipped_rate += 1
            return False, h

        self._accept(now, h, image)
        return True, h

    def wait_s(self, now: float | None = None) -> float | None:
        """In how many seconds to re-check the last frame if no new ones arrive; `None` means there
        is no need.

        DXGI sends a frame only when the picture changes. A frame held back by the rate limit
        after which the screen froze, and a motionless screen whose `heartbeat_s` is due, will
        not remind us of themselves: whoever has a timer must decide for them, by calling `check`
        with the last frame.
        """
        if self._last_hash is None:
            return None             # nothing has been read yet: the nearest frame will pass by itself
        now = time.perf_counter() if now is None else now
        due = self.s.min_interval_s if self._deferred else self.s.heartbeat_s
        return max(0.0, due - (now - self._last_passed))

    def _tiles_moved(self, image: np.ndarray) -> bool:
        if self._last_tiles is None:
            return False
        import cv2

        diff = (tiles(image) - self._last_tiles).astype(np.float32)
        # A smooth brightness shift (backlight, a background shift, a dimmed window) touches
        # neighboring tiles equally, and dhash deliberately does not see it. We subtract the mean
        # over the neighborhood so that the tiles do not see it either: what remains is what stands
        # out from its neighbors, a line of text and not the "breathing" of the background.
        local = diff - cv2.blur(diff, (7, 5))
        return int((np.abs(local) > self.s.tile_delta).sum()) >= self.s.tile_min_changed

    def _accept(self, now: float, h: np.uint64, image: np.ndarray) -> None:
        self._last_hash = h
        self._last_tiles = tiles(image)
        self._last_passed = now
        self._deferred = False
        self.passed += 1

    @property
    def pass_rate(self) -> float:
        return self.passed / self.seen if self.seen else 0.0
