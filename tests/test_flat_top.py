"""The top frame of the main window: the client area starts at the very top (2026-10-07).

For a window without `WS_CAPTION` but with `WS_THICKFRAME`, Windows leaves a frame band
of 7 pixels at the top: the highlight of the page strip icons and the red "close" began
below the window edge. `desktop._flat_top` patches the window on the fly and gives the
client area everything above one pixel from the top.

It is checked on a real Windows window, small, hidden and without a title (it is not
visible and does not take focus), not on a mock: a mistake in the frame recalculation is
visible only there.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows windows")

WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_TOOLWINDOW = 0x00000080


def _insets(user32, hwnd: int) -> tuple[int, int, int, int]:
    """Window frame: how many pixels from each edge to the client area."""
    outer = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    inner = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(inner))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    left = origin.x - outer.left
    top = origin.y - outer.top
    right = outer.right - (origin.x + inner.right)
    bottom = outer.bottom - (origin.y + inner.bottom)
    return left, top, right, bottom


@pytest.fixture
def window():
    user32 = ctypes.windll.user32
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    # Without WS_VISIBLE and far off screen: the window is needed only so that Windows
    # computes the frame.
    hwnd = user32.CreateWindowExW(
        WS_EX_TOOLWINDOW, "STATIC", "argus-flat-top-test",
        WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX,
        -32000, -32000, 600, 400, None, None, None, None,
    )
    assert hwnd, "the test window was not created"
    yield user32, hwnd
    user32.DestroyWindow(hwnd)


def test_the_top_belt_goes_away_and_the_side_frames_stay(window):
    from argus import desktop

    user32, hwnd = window
    before = _insets(user32, hwnd)
    assert before[1] > 1, f"a window with a thick frame must have a band on top, but it is {before[1]} px"

    assert desktop._flat_top(hwnd, True)
    after = _insets(user32, hwnd)
    assert after[1] == 1, f"one pixel of edge is left on top, not {after[1]}"
    # The other frames are untouched: they can still be used to resize.
    assert (after[0], after[2], after[3]) == (before[0], before[2], before[3])

    # A second call breaks nothing and does not install a second patch.
    assert desktop._flat_top(hwnd, True)
    assert _insets(user32, hwnd) == after

    assert desktop._flat_top(hwnd, False)
    assert _insets(user32, hwnd) == before, "after the override was removed the frame did not return to what it was"


def test_switching_off_a_window_that_was_never_switched_on_is_harmless(window):
    from argus import desktop

    user32, hwnd = window
    before = _insets(user32, hwnd)
    assert desktop._flat_top(hwnd, False)
    assert _insets(user32, hwnd) == before
