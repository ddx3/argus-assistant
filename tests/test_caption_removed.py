"""A window without a system title bar must get its own (2026-10-08: the whole top panel
was missing).

A real case: launch removed `WS_CAPTION`, but `use_page_caption` did not run to the end,
`caption_swapped` stayed `False`, the page address did not get `desk=1`, and the window
was left without a system title bar and without its own: empty at the top, nothing to
close it with. Now the window itself decides (`caption_removed`): no bar — the page
draws its own."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from argus import desktop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows windows")


def _window(title: str) -> int:
    u = ctypes.windll.user32
    u.CreateWindowExW.restype = wintypes.HWND
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HINSTANCE, wintypes.LPVOID]
    # Off screen, without focus (WS_EX_NOACTIVATE): the check must not flash windows.
    return u.CreateWindowExW(0x08000080, "STATIC", title, 0x00CF0000 | 0x10000000, -32000, -32000, 400, 300,
                             None, None, None, None)


def test_a_window_reports_whether_its_system_caption_is_gone():
    title = "ArgusCaptionProbe"
    hwnd = _window(title)
    try:
        assert desktop.caption_removed(title) is False          # an ordinary window: the title bar is the system one
        assert desktop.use_page_caption(title, desktop.CAPTION_COLOR) is True
        assert desktop.caption_removed(title) is True           # removed: the page has to draw it
        assert desktop.caption_removed("no such window") is False
    finally:
        ctypes.windll.user32.DestroyWindow(hwnd)


def test_the_page_address_follows_the_window_not_the_return_value():
    src = (Path(desktop.__file__)).read_text(encoding="utf-8")
    assert "if not caption_swapped and caption_removed(cfg.ui.window_title):" in src
    assert 'log.warning("could not fully replace the window title bar"' in src
