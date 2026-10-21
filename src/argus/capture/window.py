"""Detection of the active window.

It appeared because of a real fault. Argus captured the whole screen, and its own
dashboard was open on that same screen, so the program began reading itself. "ARGUS",
"RECOGNITION IN PROGRESS", "26 utterances" got into the timeline, went into the prompt,
were shown in the dashboard, and were recognized again. A loop.

The solution is to read not the whole screen but the window the person is looking at right
now. The side benefit is bigger than the fix itself: the taskbar, other people's windows,
the tray, and the clock disappear. Only what the user actually looks at goes into the
timeline.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

# A window smaller than this is a popup, a hint, or a stub: there is nothing to read.
MIN_SIDE = 200

# Windows service windows: visible, full screen, but pointless to choose. "Program Manager"
# is the desktop; the others hang on top of everything and are empty.
SYSTEM_WINDOWS = (
    "Program Manager",
    "Интерфейс ввода Windows",
    "Windows Input Experience",
    "NVIDIA GeForce Overlay",
    "Переключение задач",
    "Task Switching",
    "Setup",
    "搜索",
)


@dataclass(slots=True)
class WindowInfo:
    """The window the user is looking at."""

    title: str
    left: int
    top: int
    right: int
    bottom: int
    hwnd: int = 0
    # The owning process. On macOS the system itself hands it out along with the window list
    # (on Windows it is asked from `hwnd` separately: `app_loopback.window_pid`).
    pid: int = 0

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def usable(self) -> bool:
        return self.width >= MIN_SIDE and self.height >= MIN_SIDE

    def crop(self, image):
        """Cut the window out of the full screen frame."""
        h, w = image.shape[:2]
        left = max(0, min(self.left, w - 1))
        top = max(0, min(self.top, h - 1))
        right = max(left + 1, min(self.right, w))
        bottom = max(top + 1, min(self.bottom, h))
        return image[top:bottom, left:right]


def foreground_window() -> WindowInfo | None:
    """The foreground window. `None` if it could not be determined."""
    if sys.platform == "darwin":
        from argus.mac import windows

        return windows.foreground_window()
    try:
        import win32gui
    except ImportError:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or not win32gui.IsWindowVisible(hwnd):
            return None
        title = win32gui.GetWindowText(hwnd) or ""
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception as e:  # noqa: BLE001 — the window may have closed between calls
        log.debug("could not determine the active window: %s", e)
        return None

    win = WindowInfo(title=title, left=left, top=top, right=right,
                     bottom=bottom, hwnd=hwnd)
    return win if win.usable else None


def list_windows(skip: tuple[str, ...] = ()) -> list[WindowInfo]:
    """All visible windows fit for reading, for choosing in the interface.

    Windows does not hide minimized windows but carries them off to coordinates like (-32000,
    -32000). By size they look normal, so we cut them off separately: pinning a minimized
    window is pointless, there is nothing to read in it.
    """
    if sys.platform == "darwin":
        from argus.mac import windows

        return windows.list_windows(skip)
    try:
        import win32gui
    except ImportError:
        return []

    found: list[WindowInfo] = []

    def collect(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:  # noqa: BLE001 — the window may have just closed
            return
        win = WindowInfo(title=title, left=left, top=top, right=right,
                         bottom=bottom, hwnd=hwnd)
        if not win.usable or win.left < -10_000 or win.top < -10_000:
            return
        if is_own_window(title, SYSTEM_WINDOWS):
            return
        if skip and is_own_window(title, skip):
            return
        found.append(win)

    try:
        win32gui.EnumWindows(collect, None)
    except Exception as e:  # noqa: BLE001 — window enumeration must not crash the pipeline
        log.debug("could not enumerate windows: %s", e)
    return found


PW_RENDERFULLCONTENT = 2

# The share of non-empty pixels below which a window counts as an empty shell. Service
# overlays like "XBOX" are visible, full screen, and have a title, but nothing is drawn in
# them: the snapshot comes out solid black. By title they cannot be told apart, by content
# immediately.
MIN_CONTENT = 0.02


def capture_window_image(win: WindowInfo):
    """Capture a window separately from the screen, together with what covers it from above.

    Needed for the thumbnails in the window picker. Cutting the window out of a screenshot is
    impossible here: an overlapped window would show the content of whatever lies on top of
    it, and all the thumbnails would turn out identical.

    `PrintWindow` with `PW_RENDERFULLCONTENT` asks the application to redraw into a separate
    context. Measured on this machine: 44-52 ms per window, and the content arrives even for
    fully covered ones.
    """
    if sys.platform == "darwin":
        from argus.mac import windows

        return windows.capture_window_image(win)
    try:
        import numpy as np
        import win32gui
        import win32ui
    except ImportError:
        return None

    from ctypes import windll

    width, height = win.width, win.height
    if width <= 0 or height <= 0:
        return None

    dc = mfc = save = bmp = None
    try:
        dc = win32gui.GetWindowDC(win.hwnd)
        mfc = win32ui.CreateDCFromHandle(dc)
        save = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc, width, height)
        save.SelectObject(bmp)
        if not windll.user32.PrintWindow(win.hwnd, save.GetSafeHdc(), PW_RENDERFULLCONTENT):
            return None
        info = bmp.GetInfo()
        raw = bmp.GetBitmapBits(True)
        image = np.frombuffer(raw, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        return image[:, :, :3].copy()  # BGRA -> BGR, a copy: we are about to free the buffer
    except Exception as e:  # noqa: BLE001 — the window may have just closed
        log.debug("could not capture window '%s': %s", win.title[:40], e)
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
            if save is not None:
                save.DeleteDC()
            if mfc is not None:
                mfc.DeleteDC()
            if dc is not None:
                win32gui.ReleaseDC(win.hwnd, dc)
        except Exception as e:  # noqa: BLE001 — releasing must not crash the call
            log.debug("could not release the window context: %s", e)


def has_content(image) -> bool:
    """Whether anything at all is drawn in the window."""
    if image is None or image.size == 0:
        return False
    return float((image > 12).mean()) >= MIN_CONTENT


def find_window(needle: str, skip: tuple[str, ...] = ()) -> WindowInfo | None:
    """Find a pinned window by a piece of the title.

    We search anew each time rather than remember: windows are moved, maximized, and resized,
    and a tab title changes on switching. A substring match survives both.
    """
    if not needle:
        return None
    low = needle.lower()
    matches = [w for w in list_windows(skip) if low in w.title.lower()]
    if not matches:
        return None
    # The largest: an application sometimes has service windows with the same title.
    return max(matches, key=lambda w: w.width * w.height)


def is_own_window(title: str, markers: tuple[str, ...]) -> bool:
    """Is this our dashboard.

    We look at the window title. When the dashboard is open in a browser tab, the window title
    is the title of the active tab, so the check works for the tab too: switch to it and
    screen reading pauses by itself.
    """
    low = title.lower()
    return any(m.lower() in low for m in markers if m)
