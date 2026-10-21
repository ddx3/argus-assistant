"""Windows on macOS: the active one, a list for pinning, and a snapshot of a single window.

The same set as `capture/window.py` on Windows, on top of `CGWindowListCopyWindowInfo`.
The result is the same `WindowInfo`, so the rest of the code (window pinning, thumbnails,
cutting a window out of the screen, the sound of pinned programs) knows nothing about the
platform.

What you need to know.

* **Permissions.** The system gives other apps' window titles only to a program that has
  the screen-recording permission. Without it `kCGWindowName` is empty for other apps'
  windows, and the list consists of program names. It is usable, just without titles.
  This is not a failure. In newer system versions the window owner's name
  (`kCGWindowOwnerName`) can also be empty without the permission; in that case the name
  is taken from the program itself by process number (`_app_name`).
* **Title.** On Windows every window has a title ("Document1 - Word"); on macOS many
  windows have none at all (Zoom, Telegram). So that a window can be chosen and later
  found by part of its title, the title always includes the program name: "Zoom - Zoom
  Meeting", and for an unnamed window just "Zoom".
* **Window level.** We need the ordinary program windows (`kCGWindowLayer == 0`). The
  menu bar, Dock, Control Center and overlays are on other levels, and there is no reason
  to select them. Argus's own panels are higher too (`NSStatusWindowLevel`), so they do
  not end up as the active window.
* **Coordinates.** In points, with the origin at the top-left corner of the main screen.
  The camera (`mac/camera.py`) captures one pixel per point, so window coordinates are
  directly coordinates in the frame.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from argus.capture.window import WindowInfo, is_own_window

log = logging.getLogger(__name__)

# macOS service windows that can sit at level zero and that there is no reason to select.
SYSTEM_WINDOWS = (
    "Window Server",
    "Dock",
    "Control Center",
    "Notification Center",
    "Spotlight",
    "loginwindow",
    "SystemUIServer",
    "WindowManager",
)


def _quartz() -> Any:
    import Quartz

    return Quartz


def _app_name(pid: int) -> str:
    """The program name by process number. Empty means unknown (a process without an interface, or
    one that has already closed).
    """
    if pid <= 0:
        return ""
    try:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return str(app.localizedName() or "") if app is not None else ""
    except Exception as e:  # noqa: BLE001 — fallback path: no name means no name
        log.debug("cannot get the app name for %s: %s", pid, e)
        return ""


def display_title(owner: str, name: str) -> str:
    """How to name a window in the list: always with the program name, never empty."""
    owner, name = (owner or "").strip(), (name or "").strip()
    if not name:
        return owner
    if not owner or owner.lower() in name.lower():
        return name
    return f"{owner} — {name}"


def _raw_windows(q: Any | None = None) -> list[dict]:
    q = q or _quartz()
    options = q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements
    return list(q.CGWindowListCopyWindowInfo(options, q.kCGNullWindowID) or [])


def _info(entry: Any, q: Any) -> WindowInfo | None:
    """A window from a Quartz record. `None` means this is not an ordinary program window."""
    try:
        if int(entry.get(q.kCGWindowLayer, 0)) != 0:
            return None
        bounds = entry.get(q.kCGWindowBounds)
        if bounds is None:
            return None
        left, top = float(bounds["X"]), float(bounds["Y"])
        width, height = float(bounds["Width"]), float(bounds["Height"])
        pid = int(entry.get(q.kCGWindowOwnerPID, 0))
        owner = str(entry.get(q.kCGWindowOwnerName) or "") or _app_name(pid)
        title = display_title(owner, str(entry.get(q.kCGWindowName) or ""))
        if not title or owner in SYSTEM_WINDOWS:
            return None
        if float(entry.get(q.kCGWindowAlpha, 1.0)) <= 0.01:
            return None
        return WindowInfo(
            title=title,
            left=round(left), top=round(top),
            right=round(left + width), bottom=round(top + height),
            hwnd=int(entry.get(q.kCGWindowNumber, 0)),
            pid=pid,
        )
    except (KeyError, TypeError, ValueError) as e:
        log.debug("cannot parse window: %s", e)
        return None


def foreground_window(q: Any | None = None) -> WindowInfo | None:
    """The window the user is looking at: the topmost ordinary window. `None` means it could not
    be determined.

    The list comes from front to back, so the first suitable window is the one visible on top.
    There is no need to ask the system which program is active: the active program and the
    topmost ordinary window coincide, and the extra call drags in AppKit.
    """
    q = q or _quartz()
    try:
        entries = _raw_windows(q)
    except Exception as e:  # noqa: BLE001 — the window could have closed between calls
        log.debug("could not determine the active window: %s", e)
        return None
    for entry in entries:
        win = _info(entry, q)
        if win is not None and win.usable:
            return win
    return None


def list_windows(skip: tuple[str, ...] = (), q: Any | None = None) -> list[WindowInfo]:
    """All visible windows suitable for reading, for the choice in the interface."""
    q = q or _quartz()
    try:
        entries = _raw_windows(q)
    except Exception as e:  # noqa: BLE001 — enumeration must not bring down the pipeline
        log.debug("could not list windows: %s", e)
        return []

    found: list[WindowInfo] = []
    for entry in entries:
        win = _info(entry, q)
        if win is None or not win.usable:
            continue
        # System service windows were already filtered out by `_info`, by the owner's exact name.
        # Their names cannot be searched for in the title: "Docker Desktop" contains "Dock",
        # "Notes - Spotlight ideas" contains "Spotlight".
        if skip and is_own_window(win.title, skip):
            continue
        found.append(win)
    return found


def find_window(needle: str, skip: tuple[str, ...] = ()) -> WindowInfo | None:
    """The pinned window by a piece of the title (shared logic with Windows)."""
    from argus.capture import window

    return window.find_window(needle, skip)


def capture_window_image(win: WindowInfo, q: Any | None = None):
    """Capture a window separately from the screen, including what is covered on top of it.

    `kCGWindowListOptionIncludingWindow` with the window number gives the content of exactly
    this window, not what lies over it. We do not take the frame and shadow
    (`kCGWindowImageBoundsIgnoreFraming`).
    """
    from argus.mac.camera import cgimage_to_bgr

    q = q or _quartz()
    if win.hwnd <= 0:
        return None
    try:
        image = q.CGWindowListCreateImage(
            q.CGRectNull,
            q.kCGWindowListOptionIncludingWindow,
            win.hwnd,
            q.kCGWindowImageBoundsIgnoreFraming | q.kCGWindowImageNominalResolution,
        )
        return cgimage_to_bgr(image, q)
    except Exception as e:  # noqa: BLE001 — the window could have closed just now
        log.debug("could not capture window '%s': %s", win.title[:40], e)
        return None


class WindowPoller:
    """A live stream of frames of one window, the analog of `windows-capture` on Windows.

    Without ScreenCaptureKit the system gives no "subscription" to window content, but
    `capture_window_image` takes the content separately from the screen, even when the window
    is covered by others. Every half second is enough: the reader is text recognition, for
    which even one frame every few seconds is frequent.

    When the window closes, `on_closed` is called after several empty answers in a row, not
    after the first one: the window briefly disappears when the desktop changes or when it is
    minimized.
    """

    MISSES = 6

    def __init__(
        self,
        hwnd: int,
        title: str,
        on_frame,
        on_closed,
        interval: float = 0.5,
        grab=None,
    ) -> None:
        self._info = WindowInfo(title=title, left=0, top=0, right=1, bottom=1, hwnd=hwnd)
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._interval = interval
        self._grab = grab or capture_window_image
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="window-poller", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        misses = 0
        while not self._stop.is_set():
            try:
                image = self._grab(self._info)
            except Exception as e:  # noqa: BLE001 — the window could have closed just now
                log.debug("window frame not captured: %s", e)
                image = None
            if image is None:
                misses += 1
                if misses >= self.MISSES:
                    self._on_closed()
                    return
            else:
                misses = 0
                self._on_frame(image)
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
