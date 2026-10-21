"""The app's windows on macOS: what `desktop.py` does on Windows through `user32` and DWM.

`desktop.py` stays shared: the startup order, the `Panels` class with its locks and
timestamps, hotkeys, shutdown. Here are only the **primitives** it calls on the
system: change a window's size, hide it from recording, find it by title, take the
"single instance". On Windows they take an `hwnd`; here it is a number that stands for
an `NSWindow` (`handle`). This way the whole policy (what to do when the panels are
hidden, whose size request is older) works here too without a second copy.

What is decided in this file.

* **Everything about windows happens on the main thread.** AppKit is not thread-safe.
  The server calls us from its own threads, so each call goes to the main one
  (`mainthread`) and waits for the answer.
* **Invisibility to recording is `NSWindow.sharingType = .none`.** This is the
  documented way, but not a promise: reports are contradictory. On macOS 15 and newer,
  by developers' reports, some screen-sharing programs still do not hide windows with
  this flag. So the function returns not "done" but what exactly was requested, and
  for newer systems says so in words. It can be verified only on a real computer, with
  the program the person shares the screen with.
* **Panels on top of everything.** A window at the `NSStatusWindowLevel` level (set by
  pywebview itself for `on_top`) is above ordinary ones on the desktop, but on top of
  another program's full-screen window (Zoom, a presentation) three things are needed
  at once (`finish_panel`, `set_overlay_mode`; analysis by an Apple engineer, Apple
  developer forums, thread 826308): the "all spaces" and "full-screen auxiliary" flags;
  the screen saver level (1000), because the "status" and "floating" levels lie below
  the content of a full-screen window; and an "agent" app (the `accessory` policy, with
  no Dock icon and no menu bar), since the system does not layer an ordinary app's
  windows over another program's full-screen space at all. The last one is a cost, so
  the mode is turned on by the `ui.mac_overlay` setting. When a panel is brought back,
  the window is put in front without activating the app (`show_panel`): activation
  would pull the person out of a call onto the Argus desktop.
* **Our own exit.** `⌘Q` does not close the window but calls `terminate:`, and AppKit
  exits by itself, bypassing our shutdown: the session recording would be left
  unfinished. So `on_will_terminate` is called before exit.
* **A second launch.** On macOS a double click on an already running `.app` is handled
  by the system itself (it raises the running one), but `argus run` from a terminal is
  not, so it has a `flock` lock and a request to raise the window over HTTP.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from argus import host
from argus.mac.mainthread import run_in_main_thread

try:
    import fcntl
except ImportError:  # pragma: no cover - on Windows the module is needed only for tests
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Replaced in tests: on a real system it is AppKit's main thread.
_run_on_main: Callable[[Callable[[], Any]], Any] = run_in_main_thread

# `NSWindowSharingType`.
SHARING_NONE = 0
SHARING_READ_ONLY = 1

# `NSWindowCollectionBehavior`.
CAN_JOIN_ALL_SPACES = 1 << 0
IGNORES_CYCLE = 1 << 6
FULL_SCREEN_AUXILIARY = 1 << 8

# From this macOS version the reports diverge: the "do not show" flag may not take effect.
SHARING_DOUBT_FROM = (15, 0)

# `NSScreenSaverWindowLevel`: above the menu bar (24), status (25) and popup menus (101).
PANEL_LEVEL = 1000

# `NSApplicationActivationPolicy`.
POLICY_REGULAR = 0
POLICY_ACCESSORY = 1

# `NSWindowStyleMask`: the window content starts from the very top, under the strip, and
# the "full screen" mode.
STYLE_FULL_SIZE_CONTENT_VIEW = 1 << 15
STYLE_FULL_SCREEN = 1 << 14

# `NSWindowCollectionBehavior`: the window supports "full screen" (a separate space, the
# green button).
FULL_SCREEN_PRIMARY = 1 << 7

# `NSWindowTitleVisibility`.
TITLE_VISIBLE = 0
TITLE_HIDDEN = 1

# `NSWindowButton`: the traffic lights in the top left corner.
SYSTEM_BUTTONS = (0, 1, 2)            # close, minimize, maximize


def _appkit() -> Any:
    import AppKit

    return AppKit


def _foundation() -> Any:
    import Foundation

    return Foundation


def _app() -> Any:
    return _appkit().NSApplication.sharedApplication()


# --------------------------------------------------------------------------
# Windows as numbers
# --------------------------------------------------------------------------

_registry: dict[int, Any] = {}


def handle_of(window: Any) -> int:
    """The number by which `Panels` remembers the window (on Windows it is `hwnd`)."""
    try:
        import objc

        handle = int(objc.pyobjc_id(window))
    except Exception:  # noqa: BLE001 - tests and builds without objc
        handle = id(window)
    _registry[handle] = window
    return handle


def native_of(handle: int) -> Any | None:
    """The `NSWindow` window by number."""
    return _registry.get(handle)


def _all_windows() -> list[Any]:
    """All windows of the process, including those hidden by the "panic" action."""
    return list(_app().windows())


def _prune() -> None:
    """Forget closed windows: otherwise the registry would hold them until the process ends."""
    def sweep() -> None:
        alive = {handle_of(w) for w in _all_windows()}
        for handle in list(_registry):
            if handle not in alive:
                _registry.pop(handle, None)

    try:
        _run_on_main(sweep)
    except Exception as e:  # noqa: BLE001 - cleanup is not worth refusing to open a window
        log.debug("window registry was not cleaned: %s", e)


def own_windows() -> list[int]:
    """Visible windows of this process, like `desktop.own_windows` on Windows."""
    try:
        def collect() -> list[int]:
            return [handle_of(w) for w in _all_windows() if w.isVisible()]

        return _run_on_main(collect)
    except Exception as e:  # noqa: BLE001 - no AppKit or the loop has not started yet: there are "no" windows
        log.debug("cannot get the process windows: %s", e)
        return []


def window_title(handle: int) -> str:
    window = native_of(handle)
    if window is None:
        return ""
    try:
        return str(_run_on_main(lambda: window.title()))
    except Exception:  # noqa: BLE001
        return ""


def await_window(title: str, timeout: float = 5.0) -> int | None:
    """The number of the window with this title; we wait until the window loop draws it.

    We look it up by name and do not take "the last one that appeared": the main window and a
    detached panel are born almost at the same time, and everything ordered for the panel
    would go to the main window. Panels have their own titles, and these cannot coincide.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found = _run_on_main(
                lambda: next((handle_of(w) for w in _all_windows() if str(w.title()) == title), None)
            )
        except Exception as e:  # noqa: BLE001
            log.debug("cannot find window '%s': %s", title, e)
            found = None
        if found is not None:
            _prune()
            return found
        time.sleep(0.05)
    return None


# --------------------------------------------------------------------------
# Sizes
# --------------------------------------------------------------------------


def resize_one(handle: int, width: int, height: int) -> bool:
    """Change the size of the window's content without showing or activating it.

    The top left corner stays in place, like `SetWindowPos` with `SWP_NOMOVE` on Windows:
    `setContentSize` on macOS keeps the bottom one, and the window would grow upward. A
    panel hidden by the "panic" must not pop up because of a size change, so there is no
    `orderFront` and no `makeKey` here.
    """
    window = native_of(handle)
    if window is None:
        return False

    def apply() -> bool:
        frame = window.frame()
        top_left = (frame.origin.x, frame.origin.y + frame.size.height)
        window.setContentSize_((float(width), float(height)))
        window.setFrameTopLeftPoint_(top_left)
        return True

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001 - the window may have been closed
        log.debug("window size did not change: %s", e)
        return False


def _native_of_pywebview(window: Any) -> Any | None:
    return getattr(window, "native", None)


def set_min_size(window: Any, min_width: int, min_height: int) -> bool:
    native = _native_of_pywebview(window)
    if native is None:
        return False
    try:
        return bool(_run_on_main(
            lambda: native.setContentMinSize_((float(min_width), float(min_height))) or True
        ))
    except Exception as e:  # noqa: BLE001
        log.debug("lower window limit was not raised: %s", e)
        return False


def set_max_size(window: Any, max_width: int, max_height: int) -> bool:
    native = _native_of_pywebview(window)
    if native is None:
        return False
    try:
        return bool(_run_on_main(
            lambda: native.setContentMaxSize_((float(max_width), float(max_height))) or True
        ))
    except Exception as e:  # noqa: BLE001
        log.debug("upper window limit was not raised: %s", e)
        return False


# --------------------------------------------------------------------------
# Panels on top of everything
# --------------------------------------------------------------------------


def finish_panel(handle: int) -> bool:
    """Finish a detached panel: visible in all spaces and on top of full-screen windows.

    It is called where Windows rounds the corners (`_round_corners`): a macOS window has its
    corners rounded by the system itself, but it cannot follow the person across desktops
    and stand on top of another program's full-screen window by itself. The "all at once"
    panel is needed precisely on top of a call.
    """
    window = native_of(handle)
    if window is None:
        return False

    def apply() -> bool:
        window.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | FULL_SCREEN_AUXILIARY | IGNORES_CYCLE)
        # Higher than the level pywebview sets (`NSStatusWindowLevel`, 25): that level sits below
        # the contents of another app's full-screen window, and only the screen-saver level
        # stays above it.
        window.setLevel_(PANEL_LEVEL)
        window.setHidesOnDeactivate_(False)         # the panel must not disappear when a call has focus
        return True

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001
        log.debug("panel flags were not set: %s", e)
        return False


def show_panel(window: Any) -> bool:
    """Bring a panel to the front without making the app active. `False` means there is no
    window, let pywebview show it.

    `orderFrontRegardless` shows the window even when the app is not in focus, and does not
    move the person between desktops; whereas pywebview's `window.show()` also calls
    `activateIgnoringOtherApps`.
    """
    native = _native_of_pywebview(window)
    if native is None:
        return False

    def apply() -> bool:
        native.orderFrontRegardless()
        return True

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001
        log.debug("panel did not come to the front: %s", e)
        return False


def set_overlay_mode(on: bool) -> bool:
    """An app with no Dock icon and no menu bar (an "agent"), or an ordinary one again.

    Only this way do the panels sit on top of another program's full-screen window (see the
    file header). pywebview sets the ordinary policy by itself on load, so the switch must
    be made after the window loop has started. It returns whether the system accepted the
    switch.
    """
    def apply() -> bool:
        return bool(_app().setActivationPolicy_(POLICY_ACCESSORY if on else POLICY_REGULAR))

    try:
        done = bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001 — this mode must not break startup
        log.warning("'above full-screen windows' mode was not enabled: %s", e)
        return False
    if not done:
        log.warning("the system did not accept the 'no Dock icon' mode")
    return done


def hide_one(handle: int) -> bool:
    """Hide one window from screen recording: for a panel born later than the general pass."""
    window = native_of(handle)
    if window is None:
        return False

    def apply() -> bool:
        window.setSharingType_(SHARING_NONE)
        return int(window.sharingType()) == SHARING_NONE

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001
        log.debug("could not hide the window from recording: %s", e)
        return False


# --------------------------------------------------------------------------
# Invisibility to screen recording
# --------------------------------------------------------------------------


def _wait_for_windows(timeout: float) -> list[Any]:
    """The process windows. A window may not exist yet at the time of the call, so we wait."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = _run_on_main(_all_windows)
        except Exception as e:  # noqa: BLE001
            log.debug("cannot get the process windows: %s", e)
            found = []
        if found:
            return found
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.1)


def _set_sharing(kind: int, timeout: float) -> tuple[int, int]:
    windows = _wait_for_windows(timeout)
    if not windows:
        return 0, 0

    def apply() -> int:
        done = 0
        for window in windows:
            window.setSharingType_(kind)
            # We check instead of trusting: a silent failure here is worse than a missing feature.
            if int(window.sharingType()) == kind:
                done += 1
        return done

    return int(_run_on_main(apply)), len(windows)


def _doubt() -> str:
    from argus.mac.syscap import macos_version

    version = macos_version()
    if version >= SHARING_DOUBT_FROM:
        return (
            f"; on macOS {version[0]} some screen-sharing apps still see such "
            "windows — check on yours"
        )
    return ""


def hide_from_capture(timeout: float = 5.0) -> tuple[bool, str]:
    """Ask the system not to hand the app's windows to screen recording.

    It returns not "done" but what exactly came out: whether the flag is set or not. That
    not all screen-sharing programs honor it on newer systems is stated in the file
    docstring and in the answer.
    """
    try:
        done, total = _set_sharing(SHARING_NONE, timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("could not hide the windows from recording: %s", e)
        return False, "the system refused to hide the window"
    if not total:
        return False, "window not found"
    if done == total:
        return True, f"windows marked as hidden from screen recording ({done}){_doubt()}"
    if done:
        return False, f"{done} of {total} windows hidden — some are visible"
    return False, "the system refused to hide the window"


def show_in_capture(timeout: float = 5.0) -> tuple[bool, str]:
    """Return the windows to the recording: the opposite of `hide_from_capture`."""
    try:
        done, total = _set_sharing(SHARING_READ_ONLY, timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("could not return the windows to recording: %s", e)
        return False, "couldn't make the window visible in recordings again"
    if not total:
        return False, "window not found"
    if done == total:
        return True, f"window visible in recordings again ({done})"
    if done:
        return False, f"{done} of {total} windows are visible again — some are still hidden"
    return False, "couldn't make the window visible in recordings again"


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


def _main_native() -> Any | None:
    from argus import desktop

    return _native_of_pywebview(desktop.main_window())


# Where the window drag started: where the mouse was and where the window was (both
# measured from the bottom-left corner of the screen, as in AppKit). Every step is
# computed from this pair, not from the previous step, so a lost or late request
# accumulates nothing.
_drag: dict = {}


def main_window_action(action: str) -> dict:
    """What the page asks of the main window; the same contract as the Windows version.

    The system strip is removed by `use_page_caption`, and the traffic lights and the header
    are drawn by the page (`ui/static/winbar.js`), as on Windows. That is why there are both
    "minimize / maximize / close" and the pair `drag_start` / `drag_move` here: Python drags
    the window by the strip according to the mouse position from the system
    (`NSEvent.mouseLocation`), since the page sees the mouse only within its own pixels.
    """
    native = _main_native()
    if native is None:
        return {"ok": False, "error": "there is no main window"}

    def state() -> dict:
        frame = native.frame()
        screen = _appkit().NSScreen.screens()[0].frame()
        left = int(frame.origin.x)
        top = int(screen.size.height - (frame.origin.y + frame.size.height))
        try:
            fullscreen = bool(int(native.styleMask()) & STYLE_FULL_SCREEN)
        except Exception:  # noqa: BLE001 — the page can live without this full-screen flag
            fullscreen = False
        return {
            "ok": True,
            "maximized": bool(native.isZoomed()),
            "minimized": bool(native.isMiniaturized()),
            "fullscreen": fullscreen,
            "rect": [left, top, left + int(frame.size.width), top + int(frame.size.height)],
        }

    def cursor() -> tuple[float, float]:
        point = _appkit().NSEvent.mouseLocation()
        return float(point.x), float(point.y)

    def drag_to_cursor() -> None:
        x, y = cursor()
        origin = _drag["origin"]
        new_x = origin[0] + (x - _drag["cursor"][0])
        new_y = origin[1] + (y - _drag["cursor"][1])
        # The strip never moves above the visible area: behind the menu bar the window buttons
        # would be out of reach.
        screen = native.screen()
        if screen is not None:
            visible = screen.visibleFrame()
            top_limit = visible.origin.y + visible.size.height
            height = native.frame().size.height
            if new_y + height > top_limit:
                new_y = top_limit - height
        native.setFrameOrigin_((new_x, new_y))

    def apply() -> dict:
        if action == "state":
            return state()
        if action == "minimize":
            native.miniaturize_(None)
        elif action == "maximize":
            if not native.isZoomed():
                native.zoom_(None)
        elif action == "restore":
            if native.isMiniaturized():
                native.deminiaturize_(None)
            elif native.isZoomed():
                native.zoom_(None)
        elif action == "toggle":
            native.zoom_(None)
        elif action == "fullscreen":
            # The page's green circle: "full screen" and back; with Option it "zooms" (`toggle`),
            # like the system one.
            native.toggleFullScreen_(None)
        elif action == "close":
            native.performClose_(None)
            return {"ok": True}
        elif action == "raise":
            if native.isMiniaturized():
                native.deminiaturize_(None)
            native.makeKeyAndOrderFront_(None)
            _app().activateIgnoringOtherApps_(True)
            from argus import desktop

            desktop._main_hidden["on"] = False
        elif action == "drag_start":
            x, y = cursor()
            frame = native.frame()
            _drag.update(cursor=(x, y), origin=(float(frame.origin.x), float(frame.origin.y)))
            return state()
        elif action == "drag_move":
            if "cursor" not in _drag:
                return {"ok": False}
            drag_to_cursor()
            return {"ok": True}
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
        return state()

    try:
        return _run_on_main(apply)
    except Exception as e:  # noqa: BLE001
        log.debug("window did not perform '%s': %s", action, e)
        return {"ok": False, "error": "the window didn't respond"}


def style_main_window(native: Any, color: str) -> bool:
    """Dark styling: the strip, the traffic lights, the scroll bars and the menu take the
    page's color.

    The interface is always dark; in the light theme the system strip would stand out from it
    as a white patch at the top.
    """
    AppKit = _appkit()

    def apply() -> bool:
        dark = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
        if dark is not None:
            native.setAppearance_(dark)
        native.setTitlebarAppearsTransparent_(True)
        c = color.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        native.setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))
        # Without this flag, "Enter Full Screen" works neither from the green circle nor from the
        # "View" menu item. Requirement: the window must be able to go full screen. The pywebview
        # window does not set the flag until it goes full screen by itself.
        native.setCollectionBehavior_(int(native.collectionBehavior()) | FULL_SCREEN_PRIMARY)
        return True

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001 — window styling must not break startup
        log.debug("window styling was not applied: %s", e)
        return False


def _set_system_buttons_hidden(native: Any, hidden: bool) -> None:
    """Hide or restore the window's system traffic lights. Buttons the window does not have are
    skipped.
    """
    for kind in SYSTEM_BUTTONS:
        button = native.standardWindowButton_(kind)
        if button is not None:
            button.setHidden_(hidden)


def _titlebar_container(native: Any) -> Any | None:
    """The window's system strip as a view: a "container" that holds the title and the traffic
    lights.

    It is drawn on top of the window content, and this is visible as soon as the content
    starts from the very top: pywebview paints it in the "window color" by itself
    (`cocoa.py`: "so that the color does not change together with the window"), and a gray
    28 px plate would cover the top of our header (verified on real macOS 26, 2026-10-13).
    It cannot be made transparent, since it paints itself, so the view is hidden entirely,
    and it also stops intercepting clicks in the top 28 px. This is a known trick: the
    container is the parent of the parent of any of the system buttons. If it is not found,
    `None`, and the window stays as it is.
    """
    for kind in SYSTEM_BUTTONS:
        try:
            button = native.standardWindowButton_(kind)
            container = button.superview().superview() if button is not None else None
        except Exception:  # noqa: BLE001 — the AppKit internals may have changed
            container = None
        if container is not None:
            return container
    return None


def _set_titlebar_hidden(native: Any, hidden: bool) -> None:
    container = _titlebar_container(native)
    if container is not None:
        container.setHidden_(hidden)


def use_page_caption() -> bool:
    """Remove the system strip from the main window: the header and the traffic lights live in
    the page itself instead.

    As on Windows (`desktop.use_page_caption`): the window content starts from the very top,
    and the top block with buttons, sources and levels is drawn by `ui/static/winbar.js`.
    The window remains an ordinary one, with a shadow, corners, resizing, ⌘W, ⌘M and the
    items of the "Window" menu; only the title and the three system buttons are hidden. The
    page draws them itself on the left, with the same traffic lights, and calls
    `main_window_action`. The system buttons can be moved to the height of our header only
    with tricks in the AppKit internals that break from version to version, so they are
    hidden entirely.

    It returns whether it worked. It is called before the page loads: otherwise at startup
    the content would jump up by the height of the strip.
    """
    native = _main_native()
    if native is None:
        return False

    def apply() -> bool:
        native.setStyleMask_(int(native.styleMask()) | STYLE_FULL_SIZE_CONTENT_VIEW)
        native.setTitleVisibility_(TITLE_HIDDEN)
        native.setTitlebarAppearsTransparent_(True)
        _set_system_buttons_hidden(native, True)
        _set_titlebar_hidden(native, True)
        return True

    try:
        done = bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001 — window styling must not break startup
        log.debug("system title bar was not removed: %s", e)
        return False
    if done:
        _watch_full_screen(native)
    return done


def _watch_full_screen(native: Any) -> None:
    """In "full screen" mode bring back the system buttons and the strip, and hide them again on
    exit.

    In full screen the menu bar slides in at the top and covers our header, so our circles
    cannot be clicked there; the mode is left in the standard way, with a dropdown strip
    with real buttons (it lives in its own system window and shows under the menu bar when
    the mouse is at the top edge). And they are hidden for good in our case, so for the
    duration of the mode they must be brought back. Our circles in it are hidden by the page
    (`winbar.js`, class `fs`).
    """
    try:
        AppKit = _appkit()
        center = _foundation().NSNotificationCenter.defaultCenter()

        def entering(_note: Any) -> None:
            _set_titlebar_hidden(native, False)
            _set_system_buttons_hidden(native, False)

        def left(_note: Any) -> None:
            _set_system_buttons_hidden(native, True)
            _set_titlebar_hidden(native, True)

        for name, handler in (
            (AppKit.NSWindowWillEnterFullScreenNotification, entering),
            (AppKit.NSWindowDidExitFullScreenNotification, left),
        ):
            _observers.append(center.addObserverForName_object_queue_usingBlock_(name, native, None, handler))
    except Exception as e:  # noqa: BLE001 — without this, the only way out of full screen is ⌃⌘F, but we must not crash
        log.debug("full screen is not being tracked: %s", e)


def page_caption_in_use() -> bool:
    """The main window has no system title bar, so the page draws it."""
    native = _main_native()
    if native is None:
        return False
    try:
        return bool(_run_on_main(lambda: int(native.styleMask()) & STYLE_FULL_SIZE_CONTENT_VIEW))
    except Exception as e:  # noqa: BLE001
        log.debug("could not find out whether there is a system title bar: %s", e)
        return False


def restore_native_caption() -> bool:
    """Bring back the system strip if the page with its replacement failed to load.

    A window with no strip and no page with buttons can be closed only with ⌘W and ⌘Q.
    """
    native = _main_native()
    if native is None:
        return False

    def apply() -> bool:
        native.setStyleMask_(int(native.styleMask()) & ~STYLE_FULL_SIZE_CONTENT_VIEW)
        native.setTitleVisibility_(TITLE_VISIBLE)
        _set_titlebar_hidden(native, False)
        _set_system_buttons_hidden(native, False)
        return True

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001
        log.debug("system title bar did not come back: %s", e)
        return False


# The "all at once" window and other detached windows: a transparent background that shows
# the desktop through it, like the extended DWM glass on Windows (`desktop._glass`): no
# blur and no tint, only the background is transparent. The WKWebView content is drawn on
# opaque white until "draw background" is turned off, which is what the keys below do.
GLASS_KEYS = (("drawsBackground", False), ("drawsTransparentBackground", True))


def make_glass(window: Any, timeout: float = 5.0) -> bool:
    """Make the window transparent. `False` means the window stayed opaque, and that is not a
    breakage.

    It takes a ready window and not `transparent=True` in `create_window`: in this branch
    pywebview touches a WKWebView key whose existence depends on the macOS version, and if
    it is not found, the window is not born at all. Here every key is separate and fails
    "silently": it cannot be worse than an opaque window.
    """
    deadline = time.monotonic() + timeout
    native = _native_of_pywebview(window)
    while native is None and time.monotonic() < deadline:
        time.sleep(0.02)
        native = _native_of_pywebview(window)
    if native is None:
        return False

    def view_of_window() -> Any | None:
        try:
            from webview.platforms.cocoa import BrowserView

            instance = BrowserView.instances.get(window.uid)
            return getattr(instance, "webview", None)
        except Exception:  # noqa: BLE001 — the pywebview internals may have changed
            return None

    def apply() -> bool:
        AppKit = _appkit()
        native.setOpaque_(False)
        native.setHasShadow_(False)              # the panel on Windows has no shadow either — same picture
        native.setBackgroundColor_(AppKit.NSColor.clearColor())
        view = view_of_window()
        if view is None:
            return False
        seen = False
        for key, value in GLASS_KEYS:
            try:
                view.setValue_forKey_(value, key)
                seen = True
            except Exception as e:  # noqa: BLE001 — this key does not exist in this version of the system
                log.debug("key '%s' was not accepted by WKWebView: %s", key, e)
        return seen

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001
        log.debug("transparent window background was not enabled: %s", e)
        return False


def maximize_main(native: Any) -> bool:
    """Maximize to the visible area of the screen, not under the menu bar and not under the Dock.

    The way pywebview itself does it takes the whole screen, and the top of the window
    goes under the menu bar.
    """
    def apply() -> bool:
        screen = native.screen()
        if screen is None:
            return False
        native.setFrame_display_animate_(screen.visibleFrame(), True, False)
        return True

    try:
        return bool(_run_on_main(apply))
    except Exception as e:  # noqa: BLE001
        log.debug("window did not expand: %s", e)
        return False


_observers: list[Any] = []

# Work the user is waiting for right now: the system does not put the process to sleep
# (App Nap) and does not terminate it abruptly, but it still lets the computer sleep. The
# numbers come from `NSProcessInfo.h`: `NSActivityUserInitiated` is 0x00FFFFFF, which
# includes the "prevent idle sleep" bit, and the variant we need,
# `...AllowingIdleSystemSleep`, is the same value without that bit (so 0x00EFFFFF, not
# 0x00FFFFFF: bit 20 is already set in 0x00FFFFFF).
IDLE_SYSTEM_SLEEP_DISABLED = 1 << 20
ACTIVITY_USER_INITIATED_ALLOWING_SLEEP = (0x00FFFFFF | IDLE_SYSTEM_SLEEP_DISABLED) & ~IDLE_SYSTEM_SLEEP_DISABLED
_activities: list[Any] = []


def prevent_app_nap(reason: str = "Argus is listening to the conversation") -> bool:
    """Do not let the system "put the app to sleep" when its windows are closed or hidden.

    Argus holds audio, network and recognition, and its windows are sometimes hidden by a
    hotkey for the time of screen sharing. The system will consider an invisible app with no
    sound output unneeded and throttle its timers and network (App Nap): lines would come
    with a delay, or even be lost. It works as long as the process lives.
    """
    try:
        foundation = _foundation()
        # The name from the system itself is more reliable than my own calculation; the
        # calculation is a fallback in case the binding does not expose the name.
        options = getattr(foundation, "NSActivityUserInitiatedAllowingIdleSystemSleep", None)
        if not isinstance(options, int):
            options = ACTIVITY_USER_INITIATED_ALLOWING_SLEEP
        token = foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(options, reason)
    except Exception as e:  # noqa: BLE001 — if it fails, the app may simply run slower
        log.debug("App Nap was not disabled: %s", e)
        return False
    _activities.append(token)           # without a reference, the garbage collector would release the "busy" activity
    return True


def on_will_terminate(callback: Callable[[], None]) -> None:
    """Call `callback` before exit by `⌘Q` or from the Dock menu.

    After this notification AppKit exits by itself without returning to our code, so
    stopping the pipeline, releasing the key combinations and finishing the recording are
    done here and not after `webview.start()`.

    Subscribing to the notification is thread-safe and does not need a running window loop
    (this call happens before `webview.start`), so it does not go to the main thread. The
    block is called on the thread that sends the notification, the main one.
    """
    try:
        AppKit = _appkit()
        center = _foundation().NSNotificationCenter.defaultCenter()
        token = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationWillTerminateNotification, None, None, lambda _note: callback()
        )
        _observers.append(token)       # without a reference, the garbage collector would remove the observer
    except Exception as e:  # noqa: BLE001
        log.debug("exit on ⌘Q was not intercepted: %s", e)


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------

_lock_fd: int | None = None
_lock_path: Path | None = None


def lock_dir() -> Path:
    path = host.user_data_dir() / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _open_lock(name: str) -> int:
    global _lock_path
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    _lock_path = lock_dir() / f"{safe}.lock"
    return os.open(_lock_path, os.O_RDWR | os.O_CREAT, 0o600)


def claim_single_instance(name: str) -> bool:
    """Take the lock under this name; `True` if we took it first.

    `flock` hands the lock to exactly one party, atomically, and releases it by itself when
    the process dies (including by crashing): an abandoned lock that makes "the app not
    open" cannot happen. We write the process number into the file, and the second launch
    uses it to find the first.
    """
    global _lock_fd
    if fcntl is None:
        return True
    try:
        fd = _open_lock(name)
    except OSError as e:
        # If the lock cannot be created (no permission on the folder), that is no reason not to
        # open.
        log.warning("cannot create the single-instance lock: %s", e)
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        log.warning("cannot acquire the single-instance lock: %s", e)
        return True
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd
    return True


def acquire_when_free(timeout_s: float) -> bool:
    """Wait until the previous owner of the lock finishes, and take the lock."""
    global _lock_fd
    if _lock_path is None or fcntl is None:
        return False
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        _lock_fd = fd
        return True


def _owner() -> tuple[int, int]:
    """(process, port) of the previous instance. Zeros mean it could not be found out."""
    if _lock_path is None:
        return 0, 0
    try:
        pid = int(_lock_path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        pid = 0
    found = re.search(r"-(\d+)$", _lock_path.stem)
    return pid, int(found.group(1)) if found else 0


def focus_existing(title: str, include_hidden: bool = False) -> bool:
    """Raise the window of an already running instance. It returns whether it worked.

    We ask the instance itself: it knows whether its window is hidden (by a hotkey) and can
    bring it back. We cannot reach another process's window from here.
    """
    from argus.desktop import loopback_open

    pid, port = _owner()
    if not pid or not port:
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/window/main",
        data=json.dumps({"action": "raise"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        # No system proxy: the address is on the loopback interface, and the macOS proxy (or a dead
        # one) would answer for it with a refusal, so a second launch would decide that the
        # previous instance was hung.
        with loopback_open(request, timeout=2) as response:
            answer = json.loads(response.read() or b"{}")
    except (OSError, ValueError) as e:
        log.debug("previous instance did not respond: %s", e)
        return False
    return bool(answer.get("ok"))
