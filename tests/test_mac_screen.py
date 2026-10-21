"""Screen and windows on macOS: the logic against a fake Quartz.

The real Quartz exists only on macOS, so here it is replaced by an object with the same
names and the same response shapes as PyObjC (the shapes are checked against the
`pyobjc-framework-Quartz` metadata: `CGGetActiveDisplayList` returns
`(code, displays, count)`, `CGWindowListCopyWindowInfo` a list of dicts with string
keys). The tests check what does not depend on the system: parsing a frame with row
alignment, dropping repeated frames, choosing a screen and a region, window titles, and
filtering out service windows.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from argus.capture import window as shared_window
from argus.capture.window import WindowInfo
from argus.mac import camera, windows


class Image:
    """What `CGWindowListCreateImage` returns: the sizes and raw BGRA bytes with a row wider than
    the frame.
    """

    def __init__(self, pixels: np.ndarray, pad: int = 0, bits: int = 32) -> None:
        h, w, _ = pixels.shape
        self.width, self.height, self.bits = w, h, bits
        self.stride = w * 4 + pad
        rows = np.zeros((h, self.stride), dtype=np.uint8)
        rows[:, : w * 4] = pixels.reshape(h, w * 4)
        self.data = rows.tobytes()


def bgra(h: int, w: int, value: int = 0) -> np.ndarray:
    out = np.full((h, w, 4), value, dtype=np.uint8)
    out[:, :, 3] = 255
    return out


class FakeQuartz:
    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGWindowListOptionIncludingWindow = 8
    kCGNullWindowID = 0
    kCGWindowImageNominalResolution = 16
    kCGWindowImageBoundsIgnoreFraming = 1
    CGRectNull = "NULL"
    kCGWindowNumber = "kCGWindowNumber"
    kCGWindowOwnerPID = "kCGWindowOwnerPID"
    kCGWindowOwnerName = "kCGWindowOwnerName"
    kCGWindowName = "kCGWindowName"
    kCGWindowBounds = "kCGWindowBounds"
    kCGWindowLayer = "kCGWindowLayer"
    kCGWindowAlpha = "kCGWindowAlpha"

    def __init__(self) -> None:
        self.screen = bgra(60, 100, 10)
        self.windows: list[dict] = []
        # On macOS the main screen is the one whose corner is at the origin; the second one is to
        # the right of it.
        self.displays = [(3, (1512, 0, 1920, 1080)), (7, (0, 0, 1512, 982))]
        self.main = 7
        self.calls: list[tuple] = []
        self.window_image = bgra(20, 30, 99)

    # windows
    def CGWindowListCopyWindowInfo(self, options, window_id):  # noqa: N802
        self.calls.append(("list", options))
        return self.windows

    def CGWindowListCreateImage(self, rect, options, window_id, image_options):  # noqa: N802
        self.calls.append(("image", rect, options, window_id, image_options))
        if options == self.kCGWindowListOptionIncludingWindow:
            return Image(self.window_image)
        return Image(self.screen)

    # image
    def CGImageGetWidth(self, image):  # noqa: N802
        return image.width

    def CGImageGetHeight(self, image):  # noqa: N802
        return image.height

    def CGImageGetBytesPerRow(self, image):  # noqa: N802
        return image.stride

    def CGImageGetBitsPerPixel(self, image):  # noqa: N802
        return image.bits

    def CGImageGetDataProvider(self, image):  # noqa: N802
        return image

    def CGDataProviderCopyData(self, provider):  # noqa: N802
        return provider.data

    # displays
    def CGGetActiveDisplayList(self, maximum, a, b):  # noqa: N802
        return 0, tuple(d for d, _ in self.displays), len(self.displays)

    def CGMainDisplayID(self):  # noqa: N802
        return self.main

    def CGDisplayBounds(self, display):  # noqa: N802
        left, top, width, height = dict(self.displays)[display]
        return SimpleNamespace(origin=SimpleNamespace(x=left, y=top),
                               size=SimpleNamespace(width=width, height=height))


def entry(owner="Safari", name="Apple", layer=0, x=0, y=0, w=800, h=600, pid=100, num=1, alpha=1.0):
    return {
        "kCGWindowOwnerName": owner, "kCGWindowName": name, "kCGWindowLayer": layer,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
        "kCGWindowOwnerPID": pid, "kCGWindowNumber": num, "kCGWindowAlpha": alpha,
    }


@pytest.fixture
def q():
    return FakeQuartz()


# --------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------


def test_a_frame_with_padded_rows_becomes_a_bgr_array(q):
    pixels = bgra(3, 5, 0)
    pixels[1, 2] = (11, 22, 33, 255)           # B, G, R, A
    frame = camera.cgimage_to_bgr(Image(pixels, pad=12), q)
    assert frame.shape == (3, 5, 3) and frame.dtype == np.uint8
    assert tuple(frame[1, 2]) == (11, 22, 33)
    assert frame.flags["C_CONTIGUOUS"]


@pytest.mark.parametrize("bad", [None, Image(bgra(2, 2), bits=24), Image(np.zeros((0, 4, 4), dtype=np.uint8))])
def test_an_unusable_image_is_nothing_not_a_crash(q, bad):
    assert camera.cgimage_to_bgr(bad, q) is None


def test_a_truncated_image_is_nothing(q):
    image = Image(bgra(4, 4))
    image.data = image.data[:10]
    assert camera.cgimage_to_bgr(image, q) is None


def test_the_main_display_comes_first(q):
    assert camera.display_ids(q) == [7, 3]


def test_when_the_system_lists_nothing_the_main_display_is_used(q):
    q.CGGetActiveDisplayList = lambda *a: (1, (), 0)
    assert camera.display_ids(q) == [7]


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_the_first_grab_returns_a_frame_and_asks_for_one_pixel_per_point(q):
    clock = Clock()
    cam = camera.MacCamera(0, None, q, clock)
    frame = cam.grab()
    assert frame is not None and frame.shape == (60, 100, 3)
    kind, rect, options, wid, image_options = q.calls[-1]
    assert options == q.kCGWindowListOptionOnScreenOnly and wid == q.kCGNullWindowID
    assert image_options == q.kCGWindowImageNominalResolution
    assert rect == ((0.0, 0.0), (1512.0, 982.0))


def test_grabbing_faster_than_the_limit_costs_nothing(q):
    clock = Clock()
    cam = camera.MacCamera(0, None, q, clock)
    assert cam.grab() is not None
    before = len(q.calls)
    clock.t += 0.05
    assert cam.grab() is None
    assert len(q.calls) == before, "the system was not asked for a frame that was not handed over"


def test_a_static_screen_gives_none_like_dxgi_but_not_forever(q):
    clock = Clock()
    cam = camera.MacCamera(0, None, q, clock)
    assert cam.grab() is not None
    clock.t += 0.5
    assert cam.grab() is None                       # the same
    clock.t += camera.HEARTBEAT_S
    assert cam.grab() is not None                   # it has not been delivered for a long time, and the preview must not freeze


def test_a_changed_screen_is_sent_at_once(q):
    clock = Clock()
    cam = camera.MacCamera(0, None, q, clock)
    cam.grab()
    clock.t += 0.5
    q.screen = bgra(60, 100, 200)
    frame = cam.grab()
    assert frame is not None and int(frame[0, 0, 0]) == 200


def test_the_second_display_and_a_region_are_honoured(q):
    cam = camera.MacCamera(1, (10, 20, 110, 220), q, Clock())
    # Screen #1 is second in order (after the main one); its corner is to the right of the
    # main one's.
    assert cam.bounds() == (1512.0 + 10.0, 0.0 + 20.0, 100.0, 200.0)


def test_a_missing_display_falls_back_to_the_main_one(q):
    cam = camera.MacCamera(5, None, q, Clock())
    assert cam.bounds() == (0.0, 0.0, 1512.0, 982.0)


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


def test_a_window_title_always_names_the_program():
    assert windows.display_title("Zoom", "") == "Zoom"
    assert windows.display_title("Zoom", "Zoom Meeting") == "Zoom Meeting"
    assert windows.display_title("Safari", "Apple") == "Safari — Apple"
    assert windows.display_title("", "Untitled") == "Untitled"


def test_only_ordinary_windows_of_ordinary_programs_are_listed(q):
    q.windows = [
        entry("Dock", "", layer=20, w=1512, h=60),
        entry("Safari", "Apple", num=1),
        entry("Zoom", "", num=2, w=900, h=700, pid=321),
        entry("Argus", "Argus · answers", num=3, w=460, h=340),
        entry("Notes", "tiny", num=4, w=100, h=100),                  # below the threshold
        entry("Mail", "Inbox", num=5, alpha=0.0),                     # invisible
        entry("Window Server", "Desktop", num=6),
    ]
    found = windows.list_windows(("Argus",), q)
    assert [w.title for w in found] == ["Safari — Apple", "Zoom", ]
    zoom = found[1]
    assert zoom.pid == 321 and zoom.hwnd == 2
    assert (zoom.width, zoom.height) == (900, 700)


def test_without_the_screen_right_windows_are_still_found_by_program_name(q):
    """Without permission `kCGWindowName` is empty for other people's windows: the list still
    consists of programs.
    """
    q.windows = [entry("Telegram", "", num=9, pid=55)]
    found = windows.list_windows((), q)
    assert [w.title for w in found] == ["Telegram"] and found[0].pid == 55


def test_a_program_is_not_dropped_because_its_title_contains_a_system_name(q):
    """"Docker Desktop" contains "Dock", "Notes — Spotlight ideas" contains "Spotlight": these are
    ordinary windows.
    """
    q.windows = [
        entry("Dock", "", num=1, w=1512, h=90),                               # the real Dock is a service window
        entry("Docker Desktop", "Containers", num=2),
        entry("Code", "Dockerfile — argus", num=3),
        entry("Notes", "Spotlight ideas", num=4),
        entry("Spotlight", "Spotlight Search", num=5),                        # the real Spotlight is a service window
    ]
    titles = [w.title for w in windows.list_windows((), q)]
    assert titles == ["Docker Desktop — Containers", "Code — Dockerfile — argus", "Notes — Spotlight ideas"]


def test_a_window_without_an_owner_name_takes_the_name_from_the_program(q, monkeypatch):
    """Without permission, in newer system versions even the owner name can be empty: we take it
    from the program itself.
    """
    q.windows = [entry("", "", num=9, pid=55), entry("", "", num=10, pid=56)]
    monkeypatch.setattr(windows, "_app_name", lambda pid: {55: "Telegram"}.get(pid, ""))
    found = windows.list_windows((), q)
    assert [(w.title, w.pid) for w in found] == [("Telegram", 55)], "unnamed and without a program: not a window to choose"


def test_the_name_of_a_program_comes_from_appkit_and_never_raises(monkeypatch):
    import types

    app = types.SimpleNamespace(localizedName=lambda: "Telegram")
    appkit = types.SimpleNamespace(
        NSRunningApplication=types.SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: app if pid == 55 else None
        )
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    assert windows._app_name(55) == "Telegram"
    assert windows._app_name(56) == "" and windows._app_name(0) == ""
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace())      # a binding without the needed class
    assert windows._app_name(55) == ""


def test_the_foreground_window_is_the_topmost_ordinary_one(q):
    q.windows = [
        entry("Control Center", "", layer=25),
        entry("Zoom", "Meeting", num=2, pid=7),
        entry("Safari", "Apple", num=1),
    ]
    win = windows.foreground_window(q)
    assert win.title == "Zoom — Meeting" and win.pid == 7


def test_no_window_is_none_not_an_error(q):
    q.windows = []
    assert windows.foreground_window(q) is None
    q.CGWindowListCopyWindowInfo = lambda *a: (_ for _ in ()).throw(RuntimeError("no permission"))
    assert windows.foreground_window(q) is None
    assert windows.list_windows((), q) == []


def test_a_window_is_captured_by_its_number_without_its_frame(q):
    image = windows.capture_window_image(
        WindowInfo(title="x", left=0, top=0, right=30, bottom=20, hwnd=42), q)
    assert image.shape == (20, 30, 3) and int(image[0, 0, 0]) == 99
    kind, rect, options, wid, image_options = q.calls[-1]
    assert options == q.kCGWindowListOptionIncludingWindow and wid == 42
    assert image_options & q.kCGWindowImageBoundsIgnoreFraming
    assert windows.capture_window_image(WindowInfo("x", 0, 0, 1, 1, hwnd=0), q) is None


# --------------------------------------------------------------------------
# Shared window functions on macOS go here
# --------------------------------------------------------------------------


@pytest.fixture
def on_mac(monkeypatch, q):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(windows, "_quartz", lambda: q)
    return q


def test_shared_window_functions_use_quartz_on_macos(on_mac):
    on_mac.windows = [entry("Zoom", "Meeting", num=2, pid=7), entry("Safari", "Apple", num=1)]
    assert shared_window.foreground_window().pid == 7
    assert [w.title for w in shared_window.list_windows()] == ["Zoom — Meeting", "Safari — Apple"]
    found = shared_window.find_window("safari")
    assert found is not None and found.hwnd == 1
    assert shared_window.capture_window_image(found).shape == (20, 30, 3)


def test_a_pinned_window_is_found_by_the_program_name_even_when_the_title_changes(on_mac):
    on_mac.windows = [entry("Zoom", "Webinar", num=2)]
    assert shared_window.find_window("Zoom") is not None


def test_screen_capture_builds_the_mac_camera_on_macos(monkeypatch, on_mac):
    from argus.capture.screen import ScreenCapture
    from argus.core.config import ScreenConfig

    built = {}

    class Spy:
        def __init__(self, output_idx, region):
            built["args"] = (output_idx, region)

        def grab(self):
            return None

        def stop(self):
            pass

        def release(self):
            pass

    monkeypatch.setattr(camera, "MacCamera", Spy)
    capture = ScreenCapture(object(), object(), ScreenConfig(output_idx=1, region=(0, 0, 10, 10)))
    capture.open()
    try:
        assert built["args"] == (1, (0, 0, 10, 10))
    finally:
        capture.close()


# --------------------------------------------------------------------------
# The live stream of a pinned window
# --------------------------------------------------------------------------


def wait_until(condition, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_the_poller_delivers_frames_until_the_window_is_gone():
    import threading

    frames, closed = [], threading.Event()
    answers = iter([np.ones((4, 4, 3), np.uint8)] * 3 + [None] * 50)
    poller = windows.WindowPoller(7, "Zoom", frames.append, closed.set, interval=0.005,
                                  grab=lambda info: next(answers))
    poller.start()
    try:
        assert closed.wait(5.0), "after several empty replies in a row the window counts as closed"
        assert len(frames) == 3
    finally:
        poller.stop()


def test_a_missing_frame_now_and_then_does_not_close_the_stream():
    import threading

    frames, closed = [], threading.Event()
    image = np.ones((4, 4, 3), np.uint8)
    answers = iter([image, None, None, image, None, image] + [image] * 100)
    poller = windows.WindowPoller(7, "Zoom", frames.append, closed.set, interval=0.005,
                                  grab=lambda info: next(answers))
    poller.start()
    try:
        assert wait_until(lambda: len(frames) >= 6)
        assert not closed.is_set(), "the window disappears for a moment when the desktop changes"
    finally:
        poller.stop()


def test_a_failing_grab_is_a_missing_frame_not_a_dead_thread():
    import threading

    closed = threading.Event()

    def broken(info):
        raise RuntimeError("the window was closed")

    poller = windows.WindowPoller(7, "Zoom", lambda f: None, closed.set, interval=0.005, grab=broken)
    poller.start()
    try:
        assert closed.wait(5.0)
    finally:
        poller.stop()


def test_the_shared_window_stream_uses_the_poller_on_macos(monkeypatch):
    from argus.capture import wgc

    monkeypatch.setattr(sys, "platform", "darwin")
    image = np.full((6, 8, 3), 42, np.uint8)
    monkeypatch.setattr(windows, "capture_window_image", lambda info, q=None: image)

    stream = wgc.WindowStream(7, "Zoom")
    assert stream.start() is True
    try:
        assert wait_until(lambda: stream.latest() is not None)
        assert stream.latest().shape == (6, 8, 3) and stream.frames >= 1
        assert stream.alive
    finally:
        stream.stop()
    assert stream.latest() is None


def test_the_cache_keeps_one_stream_per_pinned_window_on_macos(monkeypatch):
    from argus.capture import wgc

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(windows, "capture_window_image",
                        lambda info, q=None: np.full((2, 2, 3), info.hwnd, np.uint8))
    cache = wgc.WindowStreamCache()
    try:
        cache.frame_for(7, "A")
        assert wait_until(lambda: cache.frames >= 1)
        first = cache._stream
        cache.frame_for(7, "A")
        assert cache._stream is first
        cache.frame_for(8, "B")
        assert cache._stream is not first
        assert wait_until(lambda: cache.frames >= 1)
        assert int(cache.frame_for(8, "B")[0, 0, 0]) == 8
    finally:
        cache.stop()
