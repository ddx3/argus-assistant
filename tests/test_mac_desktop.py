"""App windows on macOS: primitives against a fake AppKit.

There are no real windows here, so `NSWindow` is replaced by an object with the same
methods (the names are checked against the AppKit documentation and against what
pywebview itself calls in `platforms/cocoa.py`). The tests check what does not depend on
the system: what exactly we ask the window for, in what order, what we return, and how
we report it in words. Windows on screen and how they look are checked only by hand on a
real computer.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from argus import desktop
from argus.mac import desktop as mac
from argus.mac import syscap


def rect(x, y, w, h):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=w, height=h))


class Window:
    """The minimum of `NSWindow` that our code needs."""

    def __init__(self, title: str, x=100.0, y=100.0, w=460.0, h=340.0, visible=True) -> None:
        self._title = title
        self._frame = rect(x, y, w, h)
        self.visible = visible
        self.sharing = 1
        self.refuse_sharing = False
        self.collection = None
        self.min_size = self.max_size = None
        self.zoomed = False
        self.miniaturized = False
        self.calls: list[str] = []
        self.appearance = None
        self.background = None
        self.transparent_title = None
        self.visible_frame = rect(0, 25, 1512, 900)
        self.level = None
        self.hides_on_deactivate = None
        self.front_regardless = 0
        self.style = 1 | 2 | 4 | 8        # title, close, minimize, resize
        self.title_visibility = 0
        # The container of the system title strip: the parent of the parent of any of the buttons
        # ("traffic lights" -> strip -> container).
        self.titlebar = SimpleNamespace(hidden=False)
        self.titlebar.setHidden_ = lambda on: setattr(self.titlebar, "hidden", on)
        row = SimpleNamespace(superview=lambda: self.titlebar)
        self.buttons = {kind: SimpleNamespace(hidden=False, setHidden_=None, superview=lambda: row) for kind in (0, 1, 2)}
        for button in self.buttons.values():
            button.setHidden_ = lambda on, b=button: setattr(b, "hidden", on)
        self.opaque = True
        self.shadow = True

    def title(self):
        return self._title

    def styleMask(self):  # noqa: N802
        return self.style

    def setStyleMask_(self, mask):  # noqa: N802
        self.style = mask

    def setTitleVisibility_(self, value):  # noqa: N802
        self.title_visibility = value

    def standardWindowButton_(self, kind):  # noqa: N802
        return self.buttons.get(kind)

    def setOpaque_(self, on):  # noqa: N802
        self.opaque = on

    def collectionBehavior(self):  # noqa: N802
        return self.collection or 0

    def toggleFullScreen_(self, _):  # noqa: N802
        self.style ^= mac.STYLE_FULL_SCREEN
        self.calls.append("fullscreen")

    def setHasShadow_(self, on):  # noqa: N802
        self.shadow = on

    def setFrameOrigin_(self, point):  # noqa: N802
        x, y = point
        self._frame = rect(x, y, self._frame.size.width, self._frame.size.height)
        self.calls.append("origin")

    def isVisible(self):  # noqa: N802
        return self.visible

    def frame(self):
        return self._frame

    def setContentSize_(self, size):  # noqa: N802
        self.calls.append("size")
        w, h = size
        # Like AppKit: the bottom-left corner stays in place.
        self._frame = rect(self._frame.origin.x, self._frame.origin.y, w, h)

    def setFrameTopLeftPoint_(self, point):  # noqa: N802
        self.calls.append("topleft")
        x, top = point
        self._frame = rect(x, top - self._frame.size.height, self._frame.size.width, self._frame.size.height)

    def setSharingType_(self, kind):  # noqa: N802
        if not self.refuse_sharing:
            self.sharing = kind

    def sharingType(self):  # noqa: N802
        return self.sharing

    def setCollectionBehavior_(self, value):  # noqa: N802
        self.collection = value

    def setLevel_(self, level):  # noqa: N802
        self.level = level

    def setHidesOnDeactivate_(self, on):  # noqa: N802
        self.hides_on_deactivate = on

    def orderFrontRegardless(self):  # noqa: N802
        self.visible = True
        self.front_regardless += 1
        self.calls.append("front-regardless")

    def setContentMinSize_(self, size):  # noqa: N802
        self.min_size = size

    def setContentMaxSize_(self, size):  # noqa: N802
        self.max_size = size

    def isZoomed(self):  # noqa: N802
        return self.zoomed

    def isMiniaturized(self):  # noqa: N802
        return self.miniaturized

    def zoom_(self, _):
        self.zoomed = not self.zoomed
        self.calls.append("zoom")

    def miniaturize_(self, _):
        self.miniaturized = True
        self.calls.append("miniaturize")

    def deminiaturize_(self, _):
        self.miniaturized = False
        self.calls.append("deminiaturize")

    def performClose_(self, _):  # noqa: N802
        self.calls.append("close")

    def makeKeyAndOrderFront_(self, _):  # noqa: N802
        self.visible = True
        self.calls.append("front")

    def screen(self):
        return SimpleNamespace(visibleFrame=lambda: self.visible_frame)

    def setFrame_display_animate_(self, frame, display, animate):  # noqa: N802
        self._frame = frame
        self.calls.append("setframe")

    def setAppearance_(self, appearance):  # noqa: N802
        self.appearance = appearance

    def setTitlebarAppearsTransparent_(self, on):  # noqa: N802
        self.transparent_title = on

    def setBackgroundColor_(self, color):  # noqa: N802
        self.background = color


class FakeAppKit:
    NSAppearanceNameDarkAqua = "dark"
    NSApplicationWillTerminateNotification = "will-terminate"

    def __init__(self, *windows: Window) -> None:
        self.windows_list = list(windows)
        self.activated = False
        outer = self

        class App:
            def windows(self_inner):  # noqa: N805
                return list(outer.windows_list)

            def activateIgnoringOtherApps_(self_inner, flag):  # noqa: N802, N805
                outer.activated = flag

        self.NSApplication = SimpleNamespace(sharedApplication=lambda: App())
        self.NSScreen = SimpleNamespace(screens=lambda: [SimpleNamespace(frame=lambda: rect(0, 0, 1512, 982))])
        self.NSAppearance = SimpleNamespace(appearanceNamed_=lambda name: f"appearance:{name}")
        self.NSColor = SimpleNamespace(
            colorWithSRGBRed_green_blue_alpha_=lambda r, g, b, a: ("color", round(r, 3), round(g, 3), round(b, 3), a),
            clearColor=lambda: "clear",
        )
        self.mouse = SimpleNamespace(x=0.0, y=0.0)
        self.NSEvent = SimpleNamespace(mouseLocation=lambda: self.mouse)


class FakeFoundation:
    def __init__(self) -> None:
        self.observers: list = []
        outer = self
        center = SimpleNamespace(
            addObserverForName_object_queue_usingBlock_=lambda name, obj, queue, block: (
                outer.observers.append((name, block)) or f"token-{len(outer.observers)}"
            )
        )
        self.NSNotificationCenter = SimpleNamespace(defaultCenter=lambda: center)


@pytest.fixture
def system(monkeypatch):
    """Windows of the "process" and a stub AppKit; the window registry is clean."""
    main = Window("Argus", 0, 0, 1280, 860)
    panel = Window("Argus · answers")
    appkit = FakeAppKit(main, panel)
    monkeypatch.setattr(mac, "_appkit", lambda: appkit)
    monkeypatch.setattr(mac, "_run_on_main", lambda func: func())
    mac._registry.clear()
    return SimpleNamespace(appkit=appkit, main=main, panel=panel)


# --------------------------------------------------------------------------
# Windows as numbers
# --------------------------------------------------------------------------


def test_a_window_found_by_its_title_gets_a_stable_handle(system):
    handle = mac.await_window("Argus · answers", timeout=1)
    assert handle is not None and mac.native_of(handle) is system.panel
    assert mac.await_window("Argus · answers", timeout=1) == handle
    assert mac.window_title(handle) == "Argus · answers"


def test_a_window_that_never_appears_is_none_not_a_hang(system):
    assert mac.await_window("no such window", timeout=0.2) is None


def test_only_visible_windows_count_as_own_windows(system):
    system.panel.visible = False
    handles = mac.own_windows()
    assert len(handles) == 1 and mac.native_of(handles[0]) is system.main


def test_own_windows_without_appkit_is_empty_not_an_error(monkeypatch):
    def broken():
        raise ImportError("AppKit")

    monkeypatch.setattr(mac, "_appkit", broken)
    monkeypatch.setattr(mac, "_run_on_main", lambda func: func())
    assert mac.own_windows() == []


def test_closed_windows_are_forgotten(system):
    handle = mac.await_window("Argus · answers", timeout=1)
    system.appkit.windows_list.remove(system.panel)
    mac._prune()
    assert mac.native_of(handle) is None


# --------------------------------------------------------------------------
# Sizes and the panel
# --------------------------------------------------------------------------


def test_resizing_keeps_the_top_left_corner_and_never_shows_the_window(system):
    handle = mac.await_window("Argus · answers", timeout=1)
    system.panel.visible = False                       # hidden by "panic"
    before = system.panel.frame()
    top = before.origin.y + before.size.height
    assert mac.resize_one(handle, 600, 200) is True
    after = system.panel.frame()
    assert (after.size.width, after.size.height) == (600, 200)
    assert after.origin.y + after.size.height == top, "the top of the window did not move"
    assert after.origin.x == before.origin.x
    assert system.panel.visible is False, "a hidden panel did not resurface from the resize"
    assert "front" not in system.panel.calls


def test_resizing_an_unknown_window_is_a_refusal(system):
    assert mac.resize_one(123456, 10, 10) is False


def test_a_panel_is_visible_on_every_desktop_and_over_full_screen_windows(system):
    handle = mac.await_window("Argus · answers", timeout=1)
    assert mac.finish_panel(handle) is True
    behaviour = system.panel.collection
    assert behaviour & mac.CAN_JOIN_ALL_SPACES and behaviour & mac.FULL_SCREEN_AUXILIARY
    assert behaviour & mac.IGNORES_CYCLE


def test_a_panel_sits_at_the_screen_saver_level_and_does_not_hide_with_the_app(system):
    """Screen saver level: "status" and "floating" lie below the contents of someone else's
    full-screen window.
    """
    handle = mac.await_window("Argus · answers", timeout=1)
    assert mac.finish_panel(handle) is True
    assert system.panel.level == mac.PANEL_LEVEL == 1000
    assert system.panel.hides_on_deactivate is False


def test_a_returned_panel_comes_forward_without_activating_the_app(system):
    holder = SimpleNamespace(native=system.panel)
    assert mac.show_panel(holder) is True
    assert system.panel.front_regardless == 1 and system.appkit.activated is False
    assert mac.show_panel(SimpleNamespace()) is False, "there is no window - let pywebview show it"


def test_the_shared_show_uses_the_native_window_on_macos_and_pywebview_otherwise(system, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    shown = []
    desktop._show_panel(SimpleNamespace(show=lambda: shown.append("pywebview")))        # no AppKit window
    assert shown == ["pywebview"]
    holder = SimpleNamespace(native=system.panel, show=lambda: shown.append("wrong"))
    desktop._show_panel(holder)
    assert shown == ["pywebview"] and system.panel.front_regardless == 1, "the activating show() is not called"
    monkeypatch.setattr(sys, "platform", "win32")
    desktop._show_panel(SimpleNamespace(show=lambda: shown.append("windows")))
    assert shown == ["pywebview", "windows"]


def test_the_overlay_mode_turns_the_app_into_an_agent_and_back(system, monkeypatch):
    seen = []
    app = SimpleNamespace(setActivationPolicy_=lambda policy: seen.append(policy) or True)
    monkeypatch.setattr(mac, "_app", lambda: app)
    assert mac.set_overlay_mode(True) is True and mac.set_overlay_mode(False) is True
    assert seen == [mac.POLICY_ACCESSORY, mac.POLICY_REGULAR] == [1, 0]


def test_a_refused_overlay_mode_is_reported_not_raised(system, monkeypatch):
    monkeypatch.setattr(mac, "_app", lambda: SimpleNamespace(setActivationPolicy_=lambda policy: False))
    assert mac.set_overlay_mode(True) is False

    def broken():
        raise ImportError("AppKit")

    monkeypatch.setattr(mac, "_app", broken)
    assert mac.set_overlay_mode(True) is False


def test_the_overlay_follows_the_panels_by_default():
    """Verified on a real macOS 26 (2026-10-13): without the "agent" mode the panel is not visible
    behind someone else's full-screen window, with it the panel stands on top. By default the
    app becomes an agent while a panel is open."""
    from argus.core.config import UIConfig

    assert UIConfig().mac_overlay == "auto"


def test_old_boolean_overlay_settings_still_mean_something():
    from argus.core.config import UIConfig

    assert UIConfig(mac_overlay=True).mac_overlay == "always"
    assert UIConfig(mac_overlay=False).mac_overlay == "off"
    assert UIConfig(mac_overlay="auto").mac_overlay == "auto"


def test_size_limits_go_to_the_native_window_of_the_pywebview_window(system):
    pywebview_window = SimpleNamespace(native=system.panel)
    assert mac.set_min_size(pywebview_window, 360, 240) is True
    assert mac.set_max_size(pywebview_window, 1600, 1200) is True
    assert system.panel.min_size == (360.0, 240.0) and system.panel.max_size == (1600.0, 1200.0)
    assert mac.set_min_size(SimpleNamespace(native=None), 1, 1) is False


def test_a_single_panel_can_be_hidden_from_capture(system):
    handle = mac.await_window("Argus · answers", timeout=1)
    assert mac.hide_one(handle) is True and system.panel.sharing == mac.SHARING_NONE
    system.panel.refuse_sharing = True
    system.panel.sharing = 1
    assert mac.hide_one(handle) is False, "not taken on faith: we checked that the flag is set"


# --------------------------------------------------------------------------
# Invisibility to recording
# --------------------------------------------------------------------------


def test_all_windows_are_marked_hidden_and_the_answer_says_what_was_asked(system, monkeypatch):
    monkeypatch.setattr(syscap, "macos_version", lambda: (14, 5))
    ok, detail = mac.hide_from_capture(timeout=1)
    assert ok and "(2)" in detail
    assert system.main.sharing == system.panel.sharing == mac.SHARING_NONE
    assert "check on yours" not in detail, "before macOS 15 the flag holds, so there is nothing to warn about"


def test_on_new_macos_the_answer_does_not_promise_too_much(system, monkeypatch):
    monkeypatch.setattr(syscap, "macos_version", lambda: (15, 4))
    ok, detail = mac.hide_from_capture(timeout=1)
    assert ok and "macOS 15" in detail and "check on yours" in detail


def test_a_refusal_is_not_reported_as_success(system):
    system.main.refuse_sharing = True
    system.panel.refuse_sharing = True
    ok, detail = mac.hide_from_capture(timeout=1)
    assert not ok and "refused" in detail
    system.main.refuse_sharing = False
    ok, detail = mac.hide_from_capture(timeout=1)
    assert not ok and "1 of 2 windows" in detail, "partial success is not success"


def test_no_windows_is_said_in_words(monkeypatch):
    appkit = FakeAppKit()
    monkeypatch.setattr(mac, "_appkit", lambda: appkit)
    monkeypatch.setattr(mac, "_run_on_main", lambda func: func())
    ok, detail = mac.hide_from_capture(timeout=0.2)
    assert not ok and detail.strip()
    ok, detail = mac.show_in_capture(timeout=0.2)
    assert not ok and detail.strip()


def test_windows_come_back_into_the_recording(system):
    mac.hide_from_capture(timeout=1)
    ok, detail = mac.show_in_capture(timeout=1)
    assert ok and system.main.sharing == system.panel.sharing == mac.SHARING_READ_ONLY


def test_the_shared_functions_use_the_mac_ones_on_macos(system, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(syscap, "macos_version", lambda: (14, 0))
    ok, _detail = desktop.hide_from_capture(timeout=1)
    assert ok and system.main.sharing == mac.SHARING_NONE
    assert desktop.show_in_capture(timeout=1)[0]
    handle = desktop.Panels._await_titled("Argus · answers", 1)
    assert handle is not None
    assert desktop._resize_one(handle, 500, 300) is True
    assert desktop._hide_one(handle) is True
    assert desktop._round_corners(handle) is True
    assert desktop._window_title(handle) == "Argus · answers"
    assert desktop._set_min_size(SimpleNamespace(native=system.panel), 10, 10) is True
    assert desktop._set_max_size(SimpleNamespace(native=system.panel), 99, 99) is True
    assert desktop.own_windows()


# --------------------------------------------------------------------------
# The main window
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(system, monkeypatch):
    monkeypatch.setattr(desktop, "_MAIN_WINDOW", SimpleNamespace(native=system.main))
    monkeypatch.setitem(desktop._main_hidden, "on", True)
    return system.main


def test_without_a_main_window_every_action_says_not_ok(monkeypatch):
    monkeypatch.setattr(desktop, "_MAIN_WINDOW", None)
    assert mac.main_window_action("state")["ok"] is False
    assert mac.main_window_action("close")["ok"] is False


def test_unknown_actions_are_refused(main_window):
    assert mac.main_window_action("take off")["ok"] is False


def test_state_reports_the_frame_from_the_top_left_of_the_screen(main_window):
    state = mac.main_window_action("state")
    assert state["ok"] and state["maximized"] is False and state["minimized"] is False
    # a window in the system is measured from the bottom-left corner: (0, 0, 1280, 860) on a
    # screen 982 high
    assert state["rect"] == [0, 982 - 860, 1280, 982]


def test_minimize_maximize_restore_and_toggle(main_window):
    assert mac.main_window_action("minimize")["minimized"] is True
    assert mac.main_window_action("restore")["minimized"] is False
    assert mac.main_window_action("maximize")["maximized"] is True
    assert mac.main_window_action("maximize")["maximized"] is True, "the second time it does not collapse back"
    assert mac.main_window_action("restore")["maximized"] is False
    assert mac.main_window_action("toggle")["maximized"] is True


def test_close_goes_through_the_normal_close_path(main_window):
    assert mac.main_window_action("close") == {"ok": True}
    assert "close" in main_window.calls


def test_raise_shows_the_window_and_forgets_that_it_was_hidden(main_window, system):
    main_window.miniaturized = True
    assert mac.main_window_action("raise")["ok"]
    assert main_window.calls == ["deminiaturize", "front"]
    assert system.appkit.activated is True
    assert desktop._main_hidden["on"] is False


def test_the_shared_main_window_action_goes_to_the_mac_one(main_window, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop.main_window_action("state")["ok"]


def test_raise_is_an_action_the_server_accepts():
    from argus.ui.server import MAIN_WINDOW_ACTIONS

    assert "raise" in MAIN_WINDOW_ACTIONS


def test_the_title_bar_is_dark_and_blends_with_the_page(system):
    assert mac.style_main_window(system.main, "#0a0c10") is True
    assert system.main.appearance == "appearance:dark"
    assert system.main.transparent_title is True
    assert system.main.background == ("color", 0.039, 0.047, 0.063, 1.0)


def test_the_system_title_bar_is_replaced_by_the_page_one(main_window):
    """As on Windows: the content starts at the very top, the system title and traffic lights are
    hidden, and the page draws its own.
    """
    assert mac.page_caption_in_use() is False
    assert mac.use_page_caption() is True
    assert main_window.style & mac.STYLE_FULL_SIZE_CONTENT_VIEW
    assert main_window.style & 0b1111 == 0b1111, "the rest of the window is untouched: size, closing, minimizing"
    assert main_window.title_visibility == mac.TITLE_HIDDEN
    assert main_window.transparent_title is True
    assert all(button.hidden for button in main_window.buttons.values())
    assert main_window.titlebar.hidden is True, "the grey strip plate must not cover the top of the header or catch clicks"
    assert mac.page_caption_in_use() is True


def test_the_system_title_bar_comes_back_if_the_page_did_not(main_window):
    mac.use_page_caption()
    assert mac.restore_native_caption() is True
    assert not main_window.style & mac.STYLE_FULL_SIZE_CONTENT_VIEW
    assert main_window.title_visibility == mac.TITLE_VISIBLE
    assert not any(button.hidden for button in main_window.buttons.values())
    assert main_window.titlebar.hidden is False
    assert mac.page_caption_in_use() is False


def test_a_window_without_one_of_the_system_buttons_still_loses_its_title_bar(main_window):
    del main_window.buttons[1]
    assert mac.use_page_caption() is True
    assert main_window.titlebar.hidden is True


def test_a_window_whose_title_bar_cannot_be_found_is_left_as_it_is(main_window):
    """The AppKit structure may have changed: with no container, everything else is still done and
    the window does not crash.
    """
    for button in main_window.buttons.values():
        button.superview = lambda: (_ for _ in ()).throw(AttributeError("no view"))
    assert mac.use_page_caption() is True
    assert main_window.titlebar.hidden is False
    assert main_window.title_visibility == mac.TITLE_HIDDEN


def test_without_a_main_window_there_is_no_title_bar_to_swap(monkeypatch):
    monkeypatch.setattr(desktop, "_MAIN_WINDOW", None)
    assert mac.use_page_caption() is False
    assert mac.page_caption_in_use() is False
    assert mac.restore_native_caption() is False


def test_the_shared_caption_functions_go_to_the_mac_ones(main_window, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop.use_page_caption("Argus", "#101319") is True
    assert desktop.caption_removed("Argus") is True
    desktop.restore_native_caption("Argus")
    assert desktop.caption_removed("Argus") is False


def test_the_page_drags_the_window_by_the_mouse_position_of_the_system(main_window, system):
    """The page says "started" and "step", and we take where the mouse is from the system: the
    window does not run away from the cursor.
    """
    system.appkit.mouse.x, system.appkit.mouse.y = 500.0, 600.0
    main_window._frame = rect(100.0, 50.0, 1280.0, 800.0)
    assert mac.main_window_action("drag_start")["ok"]

    system.appkit.mouse.x, system.appkit.mouse.y = 540.0, 580.0
    assert mac.main_window_action("drag_move") == {"ok": True}
    frame = main_window.frame()
    assert (frame.origin.x, frame.origin.y) == (140.0, 30.0)
    assert (frame.size.width, frame.size.height) == (1280.0, 800.0)

    system.appkit.mouse.x, system.appkit.mouse.y = 500.0, 600.0       # the mouse was put back, so the window came back too
    mac.main_window_action("drag_move")
    assert (main_window.frame().origin.x, main_window.frame().origin.y) == (100.0, 50.0)


def test_the_window_cannot_be_dragged_under_the_menu_bar(main_window, system):
    system.appkit.mouse.x, system.appkit.mouse.y = 500.0, 500.0
    main_window._frame = rect(100.0, 50.0, 1280.0, 800.0)
    mac.main_window_action("drag_start")

    system.appkit.mouse.y = 5000.0
    mac.main_window_action("drag_move")
    frame = main_window.frame()
    assert frame.origin.y + frame.size.height == 925.0, "the top of the window rests on the visible area (25 + 900)"


def test_a_drag_step_without_a_start_does_nothing(main_window):
    mac._drag.clear()
    assert mac.main_window_action("drag_move") == {"ok": False}
    assert "origin" not in main_window.calls


def test_the_state_tells_whether_the_window_is_full_screen(main_window):
    assert mac.main_window_action("state")["fullscreen"] is False
    main_window.style |= mac.STYLE_FULL_SCREEN
    assert mac.main_window_action("state")["fullscreen"] is True


def test_a_panel_becomes_transparent_without_a_shadow(system):
    window = SimpleNamespace(native=system.panel, uid="x")
    assert mac.make_glass(window, timeout=0.05) is False, "WKWebView was not found: the window is transparent but its background is not"
    assert system.panel.opaque is False and system.panel.shadow is False
    assert system.panel.background == "clear"


def test_a_panel_turns_off_the_web_view_background_and_tolerates_missing_keys(system, monkeypatch):
    keys = []

    class WebView:
        def setValue_forKey_(self, value, key):  # noqa: N802
            if key == "drawsTransparentBackground":
                raise KeyError(key)          # this system version has no such key
            keys.append((key, value))

    cocoa = ModuleType("webview.platforms.cocoa")
    cocoa.BrowserView = SimpleNamespace(instances={"u1": SimpleNamespace(webview=WebView())})
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", cocoa)

    assert mac.make_glass(SimpleNamespace(native=system.panel, uid="u1"), timeout=0.05) is True
    assert keys == [("drawsBackground", False)]


def test_a_window_that_never_appeared_stays_as_it_is():
    assert mac.make_glass(SimpleNamespace(native=None, uid="x"), timeout=0.05) is False


def test_the_main_window_may_go_full_screen(system):
    """Without the "full screen" flag on the window, neither the green circle nor the "View" menu
    item works.
    """
    system.main.collection = 1 << 2          # what the window already has stays as it is
    assert mac.style_main_window(system.main, "#0a0c10") is True
    assert system.main.collection & mac.FULL_SCREEN_PRIMARY
    assert system.main.collection & (1 << 2)


def test_the_page_can_ask_for_full_screen_and_back(main_window):
    assert mac.main_window_action("state")["fullscreen"] is False
    assert mac.main_window_action("fullscreen")["ok"]
    assert "fullscreen" in main_window.calls
    assert mac.main_window_action("state")["fullscreen"] is True
    mac.main_window_action("fullscreen")
    assert mac.main_window_action("state")["fullscreen"] is False


def test_maximizing_fills_the_visible_area_not_the_whole_screen(system):
    assert mac.maximize_main(system.main) is True
    frame = system.main.frame()
    assert (frame.origin.y, frame.size.height) == (25, 900), "the top does not go under the menu bar"


def test_quitting_with_command_q_runs_the_shutdown_first(monkeypatch):
    foundation = FakeFoundation()
    appkit = FakeAppKit()
    monkeypatch.setattr(mac, "_appkit", lambda: appkit)
    monkeypatch.setattr(mac, "_foundation", lambda: foundation)
    called = []
    mac.on_will_terminate(lambda: called.append("stopped"))
    name, block = foundation.observers[0]
    assert name == "will-terminate"
    block(object())
    assert called == ["stopped"]


def test_not_hooking_the_quit_is_not_fatal(monkeypatch):
    def broken():
        raise ImportError("Foundation")

    monkeypatch.setattr(mac, "_foundation", broken)
    monkeypatch.setattr(mac, "_appkit", lambda: FakeAppKit())
    mac.on_will_terminate(lambda: None)


# --------------------------------------------------------------------------
# The single instance
# --------------------------------------------------------------------------


class FakeFcntl:
    """`flock` without macOS: a lock on a file is held by exactly one open descriptor."""

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self) -> None:
        self.held: dict[tuple[int, int], int] = {}

    def flock(self, fd: int, op: int) -> None:
        stat = os.fstat(fd)
        key = (stat.st_dev, stat.st_ino)
        owner = self.held.get(key)
        if owner is not None and owner != fd:
            raise OSError(errno.EWOULDBLOCK, "busy")
        self.held[key] = fd

    def release(self, fd: int) -> None:
        for key, owner in list(self.held.items()):
            if owner == fd:
                del self.held[key]


@pytest.fixture
def locks(monkeypatch, tmp_path):
    fake = FakeFcntl()
    monkeypatch.setattr(mac, "fcntl", fake)
    monkeypatch.setattr(mac, "lock_dir", lambda: tmp_path)
    monkeypatch.setattr(mac, "_lock_fd", None)
    monkeypatch.setattr(mac, "_lock_path", None)
    yield fake
    if mac._lock_fd is not None:
        fake.release(mac._lock_fd)
        os.close(mac._lock_fd)


def test_the_name_is_claimed_once_and_the_owner_is_written_down(locks):
    assert mac.claim_single_instance("ArgusSingleInstance-8420") is True
    assert mac.claim_single_instance("ArgusSingleInstance-8420") is False
    pid, port = mac._owner()
    assert pid == os.getpid() and port == 8420


def test_different_names_do_not_get_in_each_others_way(locks):
    assert mac.claim_single_instance("ArgusSingleInstance-8420") is True
    assert mac.claim_single_instance("ArgusSingleInstance-8421") is True


def test_waiting_for_a_free_lock_takes_it_when_the_old_owner_leaves(locks):
    assert mac.claim_single_instance("ArgusSingleInstance-8420") is True
    first = mac._lock_fd
    assert mac.claim_single_instance("ArgusSingleInstance-8420") is False
    assert mac.acquire_when_free(0.2) is False, "the owner is alive, so it was not given up"

    threading.Timer(0.15, lambda: (locks.release(first), os.close(first))).start()
    assert mac.acquire_when_free(3.0) is True


def test_a_lock_that_cannot_be_created_does_not_stop_the_app(monkeypatch):
    monkeypatch.setattr(mac, "_open_lock", lambda name: (_ for _ in ()).throw(PermissionError("no permission")))
    monkeypatch.setattr(mac, "fcntl", FakeFcntl())
    assert mac.claim_single_instance("x") is True


def test_the_shared_functions_use_the_lock_on_macos(locks, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    name = desktop._instance_mutex_name(18420)
    assert desktop._claim_single_instance(name) is True
    assert desktop._claim_single_instance(name) is False


class Answer:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_second_launch_asks_the_first_to_show_its_window(locks, monkeypatch):
    assert mac.claim_single_instance("ArgusSingleInstance-18421") is True
    asked = {}

    def fake_urlopen(request, timeout):
        asked["url"] = request.full_url
        asked["body"] = json.loads(request.data)
        return Answer({"ok": True})

    # The call to the loopback goes around the system proxy (`desktop.loopback_open`), not
    # through `urlopen`.
    monkeypatch.setattr(desktop, "loopback_open", fake_urlopen)
    assert mac.focus_existing("Argus") is True
    assert asked["url"] == "http://127.0.0.1:18421/window/main"
    assert asked["body"] == {"action": "raise"}


def test_a_first_instance_that_does_not_answer_is_not_a_crash(locks, monkeypatch):
    assert mac.claim_single_instance("ArgusSingleInstance-18422") is True

    def refuse(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(desktop, "loopback_open", refuse)
    assert mac.focus_existing("Argus") is False


def test_without_a_known_owner_there_is_nobody_to_ask(locks):
    assert mac.focus_existing("Argus") is False


def test_the_profile_folder_is_in_application_support(monkeypatch, tmp_path):
    from argus import host

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host, "user_data_dir", lambda: tmp_path / "Argus")
    path = desktop.storage_dir()
    assert path == tmp_path / "Argus" / "webview" and path.is_dir()


# --------------------------------------------------------------------------
# The real `Panels.open()` on a fake macOS: all primitives together
# --------------------------------------------------------------------------


class FakePywebviewWindow:
    """A pywebview window: on creation the "main thread" creates an `NSWindow` and puts it into
    `native`.
    """

    def __init__(self, appkit: FakeAppKit, title: str, url: str, kwargs: dict) -> None:
        self.title, self.url, self.kwargs = title, url, kwargs
        self.native = Window(title, w=float(kwargs["width"]), h=float(kwargs["height"]))
        appkit.windows_list.append(self.native)
        self.appkit = appkit
        noop = type("S", (), {"__iadd__": lambda s, f: s})()
        self.events = type("E", (), {"closed": noop, "resized": noop})()
        self.hidden = False
        self.destroyed = False

    def hide(self):
        self.hidden = True
        self.native.visible = False

    def show(self):
        self.hidden = False
        self.native.visible = True

    def destroy(self):
        self.destroyed = True
        self.appkit.windows_list.remove(self.native)


@pytest.fixture
def mac_panels(monkeypatch):
    from argus.core.config import Config

    # Its own system: only the main window. The panel windows have their own titles and cannot
    # coincide with it; a shared fake system with a ready "Argus · answers"
    # panel would find that one instead of a new one.
    appkit = FakeAppKit(Window("Argus", 0, 0, 1280, 860))
    monkeypatch.setattr(mac, "_appkit", lambda: appkit)
    monkeypatch.setattr(mac, "_run_on_main", lambda func: func())
    mac._registry.clear()
    system = SimpleNamespace(appkit=appkit)
    created = []

    def create_window(title, url, **kwargs):
        window = FakePywebviewWindow(system.appkit, title, url, kwargs)
        created.append(window)
        return window

    fake_webview = ModuleType("webview")
    fake_webview.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = Config()
    cfg.ui.exclude_from_capture = True
    panels = desktop.Panels("http://127.0.0.1:8420", cfg)
    panels.ready.set()
    return SimpleNamespace(panels=panels, created=created, system=system)


def test_opening_a_panel_goes_through_every_mac_primitive(mac_panels):
    panels, system = mac_panels.panels, mac_panels.system
    panels.open("live")

    window = mac_panels.created[0]
    kwargs = window.kwargs
    assert kwargs["frameless"] is True and kwargs["on_top"] is True
    assert kwargs["transparent"] is False, "the window is born ordinary: pywebview's `transparent=True` rests on a WKWebView key"
    assert window.url.endswith("/panel/live?glass=1"), "the page is painted with a transparent veil"

    native = window.native
    assert native.opaque is False and native.shadow is False and native.background == "clear", "and it becomes transparent right after"
    handle = panels.hwnds["live"]
    assert mac.native_of(handle) is native
    frame = native.frame()
    assert (frame.size.width, frame.size.height) == desktop.PANEL_SIZE["live"], "the size is set after the window is born"
    assert native.collection & mac.CAN_JOIN_ALL_SPACES, "the panel follows across desktops"
    assert native.sharing == mac.SHARING_NONE, "a new panel hides from recording like the others"
    assert kwargs["min_size"] == (360, 240), "the lower limit is set when the window is born"
    assert native.max_size == (1600.0, 1200.0), "the upper one right after"


def _policies(monkeypatch, mac_panels):
    """Record changes of the app policy ("agent" or regular) on a stub AppKit."""
    seen = []
    app = mac_panels.system.appkit.NSApplication.sharedApplication()

    class Recording:
        def windows(self_inner):  # noqa: N805
            return app.windows()

        def setActivationPolicy_(self_inner, policy):  # noqa: N802, N805
            seen.append(policy)
            return True

    mac_panels.system.appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: Recording())
    return seen


def test_opening_a_panel_makes_the_app_an_agent_and_closing_the_last_one_brings_the_icon_back(mac_panels, monkeypatch):
    seen = _policies(monkeypatch, mac_panels)
    panels = mac_panels.panels
    panels.open("live")
    assert seen == [mac.POLICY_ACCESSORY], "otherwise the panel will not sit above another app's full-screen window"
    panels.open("chat")
    assert seen == [mac.POLICY_ACCESSORY, mac.POLICY_ACCESSORY]
    panels._forget("live")
    assert seen[-1] == mac.POLICY_ACCESSORY, "the second panel is still open, so the icon is not brought back"
    panels._forget("chat")
    assert seen[-1] == mac.POLICY_REGULAR, "the last one was closed, so the icon is back"


def test_the_app_becomes_an_agent_before_the_panel_window_is_born(mac_panels, monkeypatch):
    """Verified on a real macOS 26: a window born by a regular app that becomes an agent later does
    not stand on top of someone else's full-screen window; one born by an agent does. The order
    matters."""
    seen = _policies(monkeypatch, mac_panels)
    webview = sys.modules["webview"]
    original = webview.create_window
    at_birth = []
    webview.create_window = lambda *a, **k: at_birth.append(list(seen)) or original(*a, **k)
    mac_panels.panels.open("live")
    assert at_birth == [[mac.POLICY_ACCESSORY]]


def test_closing_all_panels_restores_the_icon(mac_panels, monkeypatch):
    seen = _policies(monkeypatch, mac_panels)
    mac_panels.panels.open("live")
    mac_panels.panels.close_all()
    assert seen[-1] == mac.POLICY_REGULAR


@pytest.mark.parametrize("mode", ["always", "off"])
def test_the_other_overlay_settings_are_not_touched_by_panels(mac_panels, monkeypatch, mode):
    seen = _policies(monkeypatch, mac_panels)
    mac_panels.panels.cfg.ui.mac_overlay = mode
    mac_panels.panels.open("live")
    mac_panels.panels.close_all()
    assert seen == []


def test_a_panel_stays_opaque_when_glass_is_switched_off(mac_panels):
    mac_panels.panels.cfg.ui.panel_glass = False
    mac_panels.panels.open("live")
    window = mac_panels.created[0]
    assert "glass" not in window.url
    assert window.native.opaque is True and window.native.shadow is True


def test_a_panel_hidden_by_panic_does_not_come_back_on_resize(mac_panels):
    panels = mac_panels.panels
    panels.open("chat")
    native = mac_panels.created[0].native

    panels.hide_all()
    assert native.visible is False
    assert panels.resize("chat", 700, 400) is False, "while hidden the size is only remembered"
    assert native.visible is False and native.frame().size.width == desktop.DEFAULT_PANEL_SIZE[0]

    panels.show_all()
    assert native.visible is True
    assert (native.frame().size.width, native.frame().size.height) == (700, 400), "brought back, so applied"


def test_a_visible_panel_resizes_and_keeps_its_top_edge(mac_panels):
    panels = mac_panels.panels
    panels.open("screen")
    native = mac_panels.created[0].native
    before = native.frame()
    top = before.origin.y + before.size.height

    assert panels.resize("screen", 520, 300, seq=2.0) is True
    assert panels.resize("screen", 999, 999, seq=1.0) is False, "a late request does not roll back a new one"
    after = native.frame()
    assert (after.size.width, after.size.height) == (520, 300)
    assert after.origin.y + after.size.height == top


def test_opening_the_same_panel_twice_raises_the_first_one(mac_panels):
    panels = mac_panels.panels
    panels.open("chat")
    panels.open("chat")
    assert len(mac_panels.created) == 1
    assert mac_panels.created[0].native.visible


def test_a_window_that_never_shows_is_refused_and_removed(mac_panels, monkeypatch):
    panels = mac_panels.panels
    monkeypatch.setattr(desktop.Panels, "_await_titled", staticmethod(lambda title, timeout=5.0: None))
    with pytest.raises(RuntimeError, match="didn't appear"):
        panels.open("chat")
    assert mac_panels.created[0].destroyed and "chat" not in panels.windows


def test_closing_a_panel_destroys_its_window(mac_panels):
    panels = mac_panels.panels
    panels.open("chat")
    panels.close("chat")
    assert mac_panels.created[0].destroyed
    assert "chat" not in panels.windows and "chat" not in panels.hwnds


def test_the_app_is_kept_awake_while_it_listens(monkeypatch):
    seen = {}
    info = SimpleNamespace(
        beginActivityWithOptions_reason_=lambda options, reason: seen.update(options=options, reason=reason) or "token"
    )
    foundation = SimpleNamespace(NSProcessInfo=SimpleNamespace(processInfo=lambda: info))
    monkeypatch.setattr(mac, "_foundation", lambda: foundation)
    monkeypatch.setattr(mac, "_activities", [])
    assert mac.prevent_app_nap() is True
    assert seen["options"] == 0x00EFFFFF and seen["reason"]
    assert not seen["options"] & (1 << 20), "the computer may sleep: the 'do not sleep' bit (20) is cleared"
    assert seen["options"] & 0xFF, "the work is 'important': App Nap does not put timers to sleep"
    assert mac._activities == ["token"], "the reference to the 'busy' activity is held: otherwise the garbage collector would release it"


def test_the_system_name_for_the_activity_wins_over_my_arithmetic(monkeypatch):
    seen = {}
    info = SimpleNamespace(beginActivityWithOptions_reason_=lambda options, reason: seen.update(options=options) or "t")
    foundation = SimpleNamespace(
        NSProcessInfo=SimpleNamespace(processInfo=lambda: info), NSActivityUserInitiatedAllowingIdleSystemSleep=12345
    )
    monkeypatch.setattr(mac, "_foundation", lambda: foundation)
    monkeypatch.setattr(mac, "_activities", [])
    assert mac.prevent_app_nap() is True
    assert seen["options"] == 12345


def test_failing_to_stay_awake_is_not_fatal(monkeypatch):
    def broken():
        raise ImportError("Foundation")

    monkeypatch.setattr(mac, "_foundation", broken)
    assert mac.prevent_app_nap() is False


def test_macos_waits_longer_for_the_server_than_windows(monkeypatch):
    """The first launch waits for the person to answer the microphone request; within 30 seconds
    the app would close by itself.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop.ready_timeout() == desktop.MAC_READY_TIMEOUT_S and desktop.MAC_READY_TIMEOUT_S >= 120
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop.ready_timeout() == desktop.READY_TIMEOUT_S == 30.0
