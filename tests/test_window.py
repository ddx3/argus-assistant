"""Checks for reading the active window.

These tests appeared because of a real breakage: Argus captured the whole screen, and its
own dashboard was open on that same screen, so the program started reading itself.
"ARGUS", "RECOGNITION IN PROGRESS" and reply counters got into the
feed; that went into the prompt, was shown in the dashboard and was recognized again.

The loop produced neither an error nor a hang. It looked like "poor recognition quality".
"""

from __future__ import annotations

import numpy as np
import pytest

from argus.capture.window import MIN_SIDE, WindowInfo, foreground_window, is_own_window


def win(left=100, top=50, right=900, bottom=650, title="Window") -> WindowInfo:
    return WindowInfo(title=title, left=left, top=top, right=right, bottom=bottom)


# --------------------------------------------------------------------------
# Own windows
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Argus",
        "Argus — Google Chrome",
        "argus - Mozilla Firefox",
        "(2) Argus — Chrome",
    ],
)
def test_own_dashboard_is_recognised(title: str):
    """The dashboard in a browser tab must be recognized too.

    The title of a browser window is the title of the active tab, so a check by title works
    for a tab as well: switched to the dashboard, screen reading paused by itself and the
    loop did not close.
    """
    assert is_own_window(title, ("Argus",))


@pytest.mark.parametrize(
    "title",
    [
        "Twitch - Google Chrome",
        "Multimodal AI project — Visual Studio Code",
        "Telegram",
        "",
    ],
)
def test_other_windows_are_read(title: str):
    assert not is_own_window(title, ("Argus",))


def test_empty_marker_does_not_match_everything():
    """An empty string in the list must not turn reading off altogether."""
    assert not is_own_window("Anything", ("",))


# --------------------------------------------------------------------------
# Cutting a window out of a frame
# --------------------------------------------------------------------------


def test_window_crop_takes_only_its_own_pixels():
    """This is what it is all for: the taskbar, the tray, and other windows are not read."""
    screen = np.zeros((1440, 2560, 3), dtype=np.uint8)
    screen[50:650, 100:900] = 200          # the window content
    screen[1400:, :] = 90                  # the taskbar at the bottom

    cropped = win().crop(screen)
    assert cropped.shape[:2] == (600, 800)
    assert np.all(cropped == 200)          # nothing foreign got in


def test_crop_is_clamped_to_the_screen():
    """A window at the edge goes into negative coordinates; that is normal for Windows.

    Measured on this machine: a maximized Chrome window gives (-8, -8, 2568, 1400) on a
    2560x1440 screen. Without clamping the slice would come out empty.
    """
    screen = np.full((1440, 2560, 3), 120, dtype=np.uint8)
    cropped = win(left=-8, top=-8, right=2568, bottom=1400).crop(screen)
    assert cropped.shape[0] > 1000
    assert cropped.shape[1] > 2000
    assert cropped.size > 0


def test_tiny_window_is_not_worth_reading():
    """There is no point reading pop-ups and tooltips."""
    assert not win(right=100 + MIN_SIDE - 1).usable
    assert not win(bottom=50 + MIN_SIDE - 1).usable
    assert win().usable


def test_window_size_is_computed_from_the_rectangle():
    w = win(left=10, top=20, right=810, bottom=620)
    assert (w.width, w.height) == (800, 600)


# --------------------------------------------------------------------------
# A real window
# --------------------------------------------------------------------------


def test_foreground_window_is_detectable():
    """On this machine the active window must be detectable."""
    w = foreground_window()
    if w is None:
        pytest.skip("the active window was not detected — happens in a background run")
    assert w.usable
    assert w.width > MIN_SIDE


# --------------------------------------------------------------------------
# Pipeline settings
# --------------------------------------------------------------------------


def test_nothing_is_pinned_by_default():
    """Until a window is chosen, the whole screen is read.

    Following the active window is absent on purpose: it meant that something random was
    read. Switched to mail, we read mail; opened the dashboard, we read the dashboard. The
    person already knows what to watch and says so once.
    """
    from argus.core.config import Config

    cfg = Config()
    assert cfg.screen.target_window == ""
    assert "Argus" in cfg.screen.skip_windows


class FakeVision:
    name = "fake"
    avg_ms = 0.0

    def read(self, image): ...
    def warmup(self) -> None: ...
    def close(self) -> None: ...


def make_frame(size=(600, 800)):
    from argus.core.events import Frame

    return Frame(t=0, image=np.zeros((*size, 3), dtype=np.uint8))


def make_pipe(target: str = ""):
    from argus.core.bus import Bus
    from argus.core.config import Config
    from argus.perception.vision_pipeline import VisionPipeline

    cfg = Config()
    cfg.screen.target_window = target
    return VisionPipeline(Bus(), cfg, FakeVision())


def test_without_a_pinned_window_the_whole_screen_is_read():
    pipe = make_pipe()
    frame = make_frame()
    image, title = pipe._region(frame)
    assert image is frame.image
    assert title is None


def test_pinned_window_is_read_instead_of_the_screen(monkeypatch):
    from argus.perception import vision_pipeline

    pipe = make_pipe("Twitch")
    monkeypatch.setattr(
        vision_pipeline, "find_window",
        lambda needle, skip=(): WindowInfo(
            title="Twitch — Chrome", left=0, top=0, right=400, bottom=300, hwnd=7),
    )
    own = np.full((300, 400, 3), 42, dtype=np.uint8)
    monkeypatch.setattr(pipe._pinned, "frame_for", lambda hwnd, title="": own)

    image, title = pipe._region(make_frame())
    assert image is own
    assert title == "Twitch — Chrome"


def test_missing_pinned_window_reads_nothing(monkeypatch):
    """The pinned window was closed: it is more honest to read nothing than to read someone
    else's.
    """
    from argus.perception import vision_pipeline

    pipe = make_pipe("Closed")
    monkeypatch.setattr(vision_pipeline, "find_window", lambda needle, skip=(): None)
    assert pipe._region(make_frame()) is None
    assert pipe.target_missing == 1
