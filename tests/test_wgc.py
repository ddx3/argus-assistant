"""Checks for capturing a single window.

They appeared because of a case that nothing else could solve: the stream is kept on
another desktop or under another window, and it has to be read. Everything tried before
does not work on video:

    screen crop         a window on another desktop is simply not there
    PrintWindow         measured on Chrome with a broadcast: 39% non-empty pixels over
                        the window, 0% in the center, so video is not captured at all
    WindowsGraphicsCapture   99% on the same window, including the center

There is no live window in the tests, so the logic around it is checked: changing the
target, surviving a close, the library being absent.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pytest

from argus.capture.wgc import WindowStream, WindowStreamCache


class FakeStream(WindowStream):
    """A stream that "captures" without Windows."""

    started: ClassVar[list[int]] = []

    def start(self) -> bool:
        FakeStream.started.append(self.hwnd)
        self._frame = np.full((10, 20, 3), self.hwnd % 200, dtype=np.uint8)
        self.frames = 1
        return True

    def stop(self) -> None:
        self._frame = None


def make_cache(monkeypatch) -> WindowStreamCache:
    from argus.capture import wgc

    FakeStream.started = []
    monkeypatch.setattr(wgc, "WindowStream", FakeStream)
    return WindowStreamCache()


def test_stream_is_started_once_per_window(monkeypatch):
    """Bringing a session up costs about 270 ms, which is unacceptable for every frame."""
    cache = make_cache(monkeypatch)
    for _ in range(5):
        cache.frame_for(111, "Twitch")
    assert FakeStream.started == [111]


def test_changing_the_target_restarts_the_stream(monkeypatch):
    cache = make_cache(monkeypatch)
    a = cache.frame_for(111, "Twitch")
    b = cache.frame_for(222, "Discord")
    assert FakeStream.started == [111, 222]
    assert not np.array_equal(a, b)


def test_closed_window_makes_the_stream_restart(monkeypatch):
    """The window was closed and opened again: reading must continue."""
    cache = make_cache(monkeypatch)
    cache.frame_for(111, "Twitch")
    cache._stream._closed = True
    cache.frame_for(111, "Twitch")
    assert FakeStream.started == [111, 111]


def test_stop_forgets_everything(monkeypatch):
    cache = make_cache(monkeypatch)
    cache.frame_for(111, "Twitch")
    cache.stop()
    assert cache.frames == 0
    assert cache.error is None
    cache.frame_for(111, "Twitch")
    assert FakeStream.started == [111, 111]


@pytest.mark.windows_only
def test_missing_library_is_reported_not_crashed(monkeypatch):
    """Without the library, the pipeline must keep working on a crop."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *a, **kw):
        if name == "windows_capture":
            raise ImportError("no module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    stream = WindowStream(111, "Twitch")
    assert stream.start() is False
    assert "windows-capture" in stream.error
    assert stream.latest() is None


def test_empty_cache_has_no_frames():
    cache = WindowStreamCache()
    assert cache.frames == 0
    assert cache.error is None


class SlowStream(FakeStream):
    """A stream that takes as long to come up as a real one (about 270 ms) and keeps count."""

    instances: ClassVar[list] = []
    starting: ClassVar = __import__("threading").Event()    # someone started bringing it up and has not finished

    def __init__(self, hwnd: int, title: str = "") -> None:
        super().__init__(hwnd, title)
        self.stopped = False
        SlowStream.instances.append(self)

    def start(self) -> bool:
        import time

        SlowStream.starting.set()
        time.sleep(0.15)
        return super().start()

    def stop(self) -> None:
        self.stopped = True
        super().stop()


def test_concurrent_callers_start_one_stream_and_orphan_none(monkeypatch):
    """The event loop and the preview worker threads ask for a frame at the same time.

    While the first was bringing up the session, the others saw "no stream" and brought up
    their own; the last one won, and the earlier ones stayed alive without an owner, together
    with the system capture and copying.
    """
    import threading

    from argus.capture import wgc

    SlowStream.instances = []
    monkeypatch.setattr(wgc, "WindowStream", SlowStream)
    cache = WindowStreamCache()

    gate = threading.Barrier(8)
    results: list = []

    def ask():
        gate.wait()
        results.append(cache.frame_for(111, "Twitch"))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(SlowStream.instances) == 1, "more than one stream was brought up for one window"
    # Whoever came later got "no frame yet" and did not wait, but got no error either.
    assert any(r is not None for r in results) or cache.frame_for(111, "Twitch") is not None
    assert not any(s.stopped for s in SlowStream.instances)
    cache.stop()
    assert all(s.stopped for s in SlowStream.instances), "the stream stayed open after stop()"


def test_a_caller_that_needs_no_new_stream_never_waits_for_one_being_started(monkeypatch):
    """A reader of a ready stream does not queue behind someone who is bringing up another one."""
    import threading
    import time

    from argus.capture import wgc

    monkeypatch.setattr(wgc, "WindowStream", SlowStream)
    SlowStream.instances = []
    cache = WindowStreamCache()
    cache.frame_for(111, "Twitch")                       # ready

    SlowStream.starting.clear()
    switching = threading.Thread(target=lambda: cache.frame_for(222, "Discord"))
    switching.start()
    assert SlowStream.starting.wait(2), "the window change never started"   # the lock is busy
    began = time.perf_counter()
    other = cache.frame_for(333, "Third")                # one more, for a different window
    assert time.perf_counter() - began < 0.1, "the call queued behind the stream start-up"
    assert other is None
    switching.join()
    cache.stop()


@pytest.mark.windows_only
class TestCaptureSettings:
    """How the frame ceiling is set for the system window capture."""

    class FakeWindowsCapture:
        calls: ClassVar[list] = []
        made: ClassVar[list] = []
        refuse_interval = False

        def __init__(self, **kw) -> None:
            type(self).calls.append(kw)
            type(self).made.append(self)
            if type(self).refuse_interval and "minimum_update_interval" in kw:
                raise RuntimeError(
                    "minimum update interval is not supported by the Graphics Capture API "
                    "on this platform.")
            self.handlers: dict = {}

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        def start_free_threaded(self):
            from types import SimpleNamespace

            return SimpleNamespace(stop=lambda: None)

    @pytest.fixture
    def fake_library(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        self.FakeWindowsCapture.calls = []
        self.FakeWindowsCapture.made = []
        self.FakeWindowsCapture.refuse_interval = False
        monkeypatch.setitem(
            sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=self.FakeWindowsCapture))
        return self.FakeWindowsCapture

    def test_frames_are_limited_to_what_readers_use(self, fake_library):
        """A window with a call refreshes 30-60 times per second; twelve is enough for the readers."""
        from argus.capture.wgc import MIN_UPDATE_MS

        stream = WindowStream(111, "Meet")
        assert stream.start() is True
        assert fake_library.calls[0]["minimum_update_interval"] == MIN_UPDATE_MS
        assert 0 < MIN_UPDATE_MS <= 100, "a twelve-frames-per-second preview would turn into a slideshow"

    def test_a_system_without_the_limit_still_captures(self, fake_library):
        """Not every Windows supports the ceiling: without it the stream comes up as before."""
        fake_library.refuse_interval = True
        stream = WindowStream(111, "Meet")
        assert stream.start() is True
        assert len(fake_library.calls) == 2
        assert "minimum_update_interval" not in fake_library.calls[1]
        assert stream.error is None

    def test_a_real_failure_is_reported_not_retried_forever(self, fake_library, monkeypatch):
        def broken(self, **kw):
            raise RuntimeError("the window closed")

        monkeypatch.setattr(fake_library, "__init__", broken)
        stream = WindowStream(111, "Meet")
        assert stream.start() is False
        assert "the window closed" in stream.error

    def test_frames_are_stored_as_bgr(self, fake_library):
        stream = WindowStream(111, "Meet")
        stream.start()
        # The frame handler sits in the library stand-in: we fetch it the way the system would call
        # it.
        handler = fake_library.made[-1].handlers["on_frame_arrived"]

        class Frame:
            frame_buffer = np.dstack([
                np.full((4, 6), 10, np.uint8), np.full((4, 6), 20, np.uint8),
                np.full((4, 6), 30, np.uint8), np.full((4, 6), 255, np.uint8),
            ])

        handler(Frame(), None)
        image = stream.latest()
        assert image.shape == (4, 6, 3)
        assert image[0, 0].tolist() == [10, 20, 30]
        assert stream.frames == 1


def test_pinned_window_uses_the_stream_not_the_screen(monkeypatch):
    """Main point: a pinned window is read by its own stream, not by a screen crop.

    A screen crop sees neither another desktop nor an overlapped window; that is what all of
    this was done for.
    """
    from argus.capture.window import WindowInfo
    from argus.core.bus import Bus
    from argus.core.config import Config
    from argus.core.events import Frame
    from argus.perception import vision_pipeline
    from argus.perception.vision_pipeline import VisionPipeline

    class FakeVision:
        name = "fake"
        avg_ms = 0.0

        def read(self, image): ...
        def warmup(self) -> None: ...
        def close(self) -> None: ...

    cfg = Config()
    cfg.screen.target_window = "Twitch"
    pipe = VisionPipeline(Bus(), cfg, FakeVision())

    # We patch the name in the pipeline itself: it imported the function directly, and a
    # substitution in the source module will not reach it.
    monkeypatch.setattr(
        vision_pipeline, "find_window",
        lambda needle, skip=(): WindowInfo(
            title="Twitch", left=0, top=0, right=400, bottom=300, hwnd=777),
    )
    own = np.full((300, 400, 3), 55, dtype=np.uint8)
    monkeypatch.setattr(pipe._pinned, "frame_for", lambda hwnd, title="": own)

    screen = Frame(t=0, image=np.zeros((600, 800, 3), dtype=np.uint8))
    image, title = pipe._region(screen)
    assert image is own, "a crop of the screen was read instead of the window itself"
    assert title == "Twitch"


def test_falls_back_to_cropping_before_the_first_frame(monkeypatch):
    """The stream does not spin up instantly: while it is silent, a crop is better than nothing."""
    from argus.capture.window import WindowInfo
    from argus.core.bus import Bus
    from argus.core.config import Config
    from argus.core.events import Frame
    from argus.perception import vision_pipeline
    from argus.perception.vision_pipeline import VisionPipeline

    class FakeVision:
        name = "fake"
        avg_ms = 0.0

        def read(self, image): ...
        def warmup(self) -> None: ...
        def close(self) -> None: ...

    cfg = Config()
    cfg.screen.target_window = "Twitch"
    pipe = VisionPipeline(Bus(), cfg, FakeVision())

    # We patch the name in the pipeline itself: it imported the function directly, and a
    # substitution in the source module will not reach it.
    monkeypatch.setattr(
        vision_pipeline, "find_window",
        lambda needle, skip=(): WindowInfo(
            title="Twitch", left=0, top=0, right=400, bottom=300, hwnd=777),
    )
    monkeypatch.setattr(pipe._pinned, "frame_for", lambda hwnd, title="": None)

    screen = Frame(t=0, image=np.zeros((600, 800, 3), dtype=np.uint8))
    image, title = pipe._region(screen)
    assert image.shape[:2] == (300, 400)
    assert title == "Twitch"
