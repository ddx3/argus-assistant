"""Capture of a single window through Windows Graphics Capture.

It appeared because of a case nothing else could solve: a person keeps a stream on another
desktop or under another window, and Argus must read it.

What does not work and why:

* **Cutting from a screenshot.** A window on another desktop is not rendered on the
  current screen by Windows at all, and an overlapped one shows whatever lies on top of it.
* **`PrintWindow`.** It asks the application to redraw into a buffer, and for an ordinary
  interface that works. But video and everything the graphics card draws goes past it.
  Measured on a Chrome window with a broadcast: 39% non-empty pixels over the whole
  window, **0% in the center**, exactly where the picture is, a black rectangle.

Windows Graphics Capture takes what the window really draws, video included. On the same
window: **92% in the center**. It is the only way to read a broadcast without keeping it in
front of your eyes.

The session lives permanently while the window is pinned: bringing it up costs about
270 ms, and doing that for every frame would be pointless.
"""

from __future__ import annotations

import logging
import sys
import threading

import numpy as np

log = logging.getLogger(__name__)

# Frames more often than this are needed by no one: recognition is fine with one per second,
# the preview (`preview_fps`) with twelve. But the system delivers a frame on every window
# update (30-60 per second for a call or video), and each is copied in full: for a 1440p
# window that is hundreds of megabytes per second and time under the GIL the whole while the
# window is pinned.
MIN_UPDATE_MS = 80


class WindowStream:
    """A live stream of frames of one window.

    Frames arrive in someone else's thread, so the latest one is kept under a lock. The reader
    always takes the fresh one and never waits: a lagging frame is no use to the stream, and
    locking on the hot path would cost more.
    """

    def __init__(self, hwnd: int, title: str = "") -> None:
        self.hwnd = hwnd
        self.title = title
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._control = None
        self._closed = False
        self.frames = 0
        self.error: str | None = None

    def _start_mac(self) -> bool:
        """macOS: the window is polled once every half second (`mac/windows.py: WindowPoller`)."""
        from argus.mac.windows import WindowPoller

        def on_frame(image: np.ndarray) -> None:
            with self._lock:
                self._frame = image
                self.frames += 1

        def on_closed() -> None:
            self._closed = True

        self._control = WindowPoller(self.hwnd, self.title, on_frame, on_closed)
        self._control.start()
        return True

    def start(self) -> bool:
        if sys.platform == "darwin":
            return self._start_mac()
        try:
            from windows_capture import WindowsCapture
        except ImportError as e:
            self.error = f"windows-capture is not available: {e}"
            return False

        # First with a frame rate ceiling. Not every Windows supports it (a recent Windows 11 is
        # needed): there the library answers with a refusal, and then we go without the ceiling, as
        # before.
        error: Exception | None = None
        for interval in (MIN_UPDATE_MS, None):
            try:
                self._control = self._begin(WindowsCapture, interval)
                return True
            except Exception as e:  # noqa: BLE001 — the window may have just closed
                error = e
        self.error = f"{type(error).__name__}: {error}"
        log.warning("could not start capturing window '%s': %s", self.title[:40], error)
        return False

    def _begin(self, capture_cls, interval_ms: int | None):
        extra = {} if interval_ms is None else {"minimum_update_interval": interval_ms}
        capture = capture_cls(
            cursor_capture=False,
            # Windows draws a border around the captured window by default. It tells the user nothing
            # and only gets in the way.
            draw_border=False,
            window_hwnd=self.hwnd,
            **extra,
        )

        @capture.event
        def on_frame_arrived(frame, control):
            # BGRA from the system -> BGR, as everywhere else in the pipeline.
            image = frame.frame_buffer[:, :, :3].copy()
            with self._lock:
                self._frame = image
                self.frames += 1

        @capture.event
        def on_closed():
            self._closed = True

        return capture.start_free_threaded()

    def latest(self) -> np.ndarray | None:
        with self._lock:
            return self._frame

    @property
    def alive(self) -> bool:
        return not self._closed and self.error is None

    def stop(self) -> None:
        control, self._control = self._control, None
        if control is not None:
            try:
                control.stop()
            except Exception as e:  # noqa: BLE001 — stopping must not crash the call
                log.debug("window capture closed with an error: %s", e)
        with self._lock:
            self._frame = None


class WindowStreamCache:
    """Holds one live stream per pinned window.

    When the target changes, the previous stream is closed and a new one is brought up. The
    window itself is searched for by title anew each time, so it survives moving and a change
    of the tab title.
    """

    def __init__(self) -> None:
        self._stream: WindowStream | None = None
        self._hwnd: int | None = None
        # It is asked from the event loop (the reading pipeline) and from worker threads (the
        # preview, `/screen.jpg` and `/screen.mjpg` twelve times a second). Bringing up a session
        # takes about 270 ms, and all that time the others saw "no stream" and brought up their own:
        # the last one won, and the earlier ones stayed alive with no owner until the window is
        # closed (the wrapper over the system capture has no finalizer), and each copied frames for
        # nothing.
        self._lock = threading.Lock()

    def frame_for(self, hwnd: int, title: str = "") -> np.ndarray | None:
        stream, current = self._stream, self._hwnd
        if stream is not None and current == hwnd and stream.alive:
            return stream.latest()

        # A new stream is needed. One brings it up, whoever took the lock; the others do not wait
        # and do not spawn their own, but return "no frame yet": every caller has its own fallback
        # picture for that case (a crop from the screen, a window snapshot), and the event loop must
        # not stand still under the lock.
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._stream is None or self._hwnd != hwnd or not self._stream.alive:
                self._stop_locked()
                fresh = WindowStream(hwnd, title)
                if not fresh.start():
                    return None
                self._stream = fresh
                self._hwnd = hwnd
            return self._stream.latest()
        finally:
            self._lock.release()

    @property
    def frames(self) -> int:
        stream = self._stream           # read once: another thread may reset it between lines
        return stream.frames if stream else 0

    @property
    def error(self) -> str | None:
        stream = self._stream
        return stream.error if stream else None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        stream, self._stream = self._stream, None
        self._hwnd = None
        if stream is not None:
            stream.stop()
