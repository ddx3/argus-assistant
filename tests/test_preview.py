"""Preview checks: exactly what goes to recognition is shown.

It used to be like this: a window is selected, and the preview showed the whole monitor.
The picture did not match what went into the timeline, and you could not tell from it
whether the right thing was being read, which is exactly what people look at the preview
for.
"""

from __future__ import annotations

import numpy as np

from argus.app import Argus
from argus.capture.window import WindowInfo
from argus.core.config import Config


def make(target: str = "") -> Argus:
    cfg = Config()
    cfg.screen.target_window = target
    return Argus(cfg, with_screen=True, with_brain=False)


def screen_frame() -> np.ndarray:
    """A screen where each quarter has its own color, so it is visible what exactly was cut out."""
    img = np.zeros((1440, 2560, 3), dtype=np.uint8)
    img[:720, :1280] = 10
    img[:720, 1280:] = 60
    img[720:, :1280] = 120
    img[720:, 1280:] = 200
    return img


def test_without_a_frame_there_is_nothing_to_show():
    assert make().preview_image() is None


def test_without_a_pinned_window_the_whole_screen_is_shown():
    app = make()
    frame = screen_frame()
    app._last_image = frame
    assert app.preview_image() is frame


def test_pinned_window_is_captured_separately(monkeypatch):
    """A pinned window is captured separately, not cropped out of the screen.

    It may be covered by another window or sit on another desktop, and a crop would show
    what lies on top of it, so the preview would be lying again.
    """
    import argus.capture.window as win_module

    app = make("Twitch")
    app._last_image = screen_frame()

    own_shot = np.full((300, 400, 3), 77, dtype=np.uint8)
    monkeypatch.setattr(
        win_module, "find_window",
        lambda needle, skip=(): WindowInfo(title="Twitch", left=0, top=0,
                                           right=400, bottom=300, hwnd=7),
    )
    monkeypatch.setattr(app.vision._pinned, "frame_for", lambda hwnd, title="": own_shot)
    assert app.preview_image() is own_shot


def test_pinned_window_falls_back_to_cropping(monkeypatch):
    """If capturing the window separately failed, a crop is better than nothing."""
    import argus.capture.window as win_module

    app = make("Twitch")
    app._last_image = screen_frame()
    monkeypatch.setattr(
        win_module, "find_window",
        lambda needle, skip=(): WindowInfo(title="Twitch", left=1280, top=720,
                                           right=2560, bottom=1440, hwnd=7),
    )
    monkeypatch.setattr(app.vision._pinned, "frame_for", lambda hwnd, title="": None)
    monkeypatch.setattr(win_module, "capture_window_image", lambda win: None)

    preview = app.preview_image()
    assert preview.shape[:2] == (720, 1280)
    assert np.all(preview == 200)


def test_missing_pinned_window_shows_nothing(monkeypatch):
    """The pinned window was closed: it is more honest to show nothing than someone else's window."""
    import argus.capture.window as win_module

    app = make("Closed window")
    app._last_image = screen_frame()
    monkeypatch.setattr(win_module, "find_window", lambda needle, skip=(): None)
    assert app.preview_image() is None
