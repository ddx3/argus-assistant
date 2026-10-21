"""The panel and screen-capture protection: order, refusal, the "hidden" state.

The product promise is "invisible in the recording": the panel must be protected from
capture before it is shown for the first time, and if the protection did not take effect,
no window should be left (audit 2026-10-14, desk-a-1, desk-b-1, desk-b-10). `Panels.open`
used to create an ordinary window, wait for the title, resize it, round the corners and
only as the last step set the protection; all that time screen sharing could capture the
panel with the answers.

Here there are fake windows that record the order of calls, and real Win32 windows
off screen (as in `test_caption_removed.py`), so that the protection flag is checked on
the system and not on a fake.
"""

from __future__ import annotations

import ctypes
import sys
import types
from ctypes import wintypes

import pytest

from argus import desktop
from argus.core.config import Config
from argus.core.friendly import plain
from argus.desktop import PANEL_TITLES, Panels


def _events():
    noop = type("S", (), {"__iadd__": lambda s, f: s})()
    return type("E", (), {"closed": noop, "resized": noop})()


@pytest.fixture
def flow(monkeypatch):
    """A stub Windows: everything `Panels.open` asks of the system is written to a log in order."""
    log: list[str] = []
    made: dict = {}

    class FakeWindow:
        events = _events()

        def show(self):
            log.append("show")

        def destroy(self):
            log.append("destroy")

    def create_window(*args, **kwargs):
        log.append("create")
        made.update(kwargs)
        made["url"] = args[1]
        return FakeWindow()

    fake = types.ModuleType("webview")
    fake.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", fake)

    def rec(name, result=True):
        return lambda *a, **kw: log.append(name) or result

    state = types.SimpleNamespace(hide_one=True, form=77)
    monkeypatch.setattr(desktop, "_form_hwnd", lambda window, timeout=1.0: state.form)
    monkeypatch.setattr(desktop, "_hide_one", lambda hwnd: log.append("hide_one") or state.hide_one)
    monkeypatch.setattr(desktop, "_no_taskbar_button", rec("taskbar"))
    monkeypatch.setattr(desktop, "_glass", rec("glass"))
    monkeypatch.setattr(desktop, "_show_panel", lambda window, hwnd=None: log.append("show"))
    monkeypatch.setattr(desktop, "_resize_one", rec("resize"))
    monkeypatch.setattr(desktop, "_round_corners", rec("round"))
    monkeypatch.setattr(desktop, "_set_max_size", rec("max"))
    monkeypatch.setattr(Panels, "_await_titled", staticmethod(rec("find", 77)))
    monkeypatch.setattr(desktop, "_panel_overlay", lambda cfg, on: None)

    def make(*, exclude=True, glass=False):
        cfg = Config()
        cfg.ui.exclude_from_capture = exclude
        cfg.ui.panel_glass = glass
        monkeypatch.setattr(desktop, "glass_supported", lambda: glass)
        panels = Panels("http://127.0.0.1:0", cfg)
        panels.ready.set()
        return panels

    return types.SimpleNamespace(log=log, made=made, make=make, state=state)


# --------------------------------------------------------------------------
# Order
# --------------------------------------------------------------------------


@pytest.mark.windows_only
def test_an_opaque_panel_is_protected_before_it_is_ever_shown(flow):
    """Protection and "no taskbar button" come right after the form is born, before showing, sizes
    and corners.
    """
    panels = flow.make(exclude=True, glass=False)
    panels.open("chat")

    assert flow.made["hidden"] is True, "the window is born hidden"
    assert flow.log == [
        "create", "hide_one", "taskbar", "show", "find", "resize", "round", "max",
    ]
    assert panels.hwnds["chat"] == 77


@pytest.mark.windows_only
def test_a_glass_panel_is_protected_before_the_page_starts_to_load(flow):
    """The glass window shows itself at the start of the page load, so the protection must be
    applied before that.
    """
    panels = flow.make(exclude=True, glass=True)
    panels.open("live")

    assert flow.made["transparent"] is True
    assert flow.made["hidden"] is False, (
        "glass does not need `hidden`: pywebview hides a transparent form after birth anyway, and an extra step with "
        "form transparency must not change how the all-at-once window looks"
    )
    assert flow.log == [
        "create", "hide_one", "taskbar", "glass", "find", "resize", "round", "max",
    ], "the glass is painted after the protection, we do not show the window ourselves"
    assert "show" not in flow.log


@pytest.mark.windows_only
def test_without_the_switch_the_panel_is_shown_but_nothing_is_hidden(flow):
    panels = flow.make(exclude=False, glass=False)
    panels.open("chat")

    assert "hide_one" not in flow.log and "taskbar" not in flow.log
    assert "show" in flow.log, "a window born hidden still has to be shown"
    assert flow.log.index("create") < flow.log.index("show") < flow.log.index("find")


def test_on_macos_the_window_is_protected_right_after_it_is_found(flow, monkeypatch):
    """macOS: the window is born visible, the protection goes right after the window is found,
    before size and corners.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    panels = flow.make(exclude=True, glass=False)
    panels.open("chat")

    assert flow.made["hidden"] is False, "the window is born hidden only on Windows"
    assert flow.log == ["create", "find", "hide_one", "resize", "round", "max"]


@pytest.mark.windows_only
def test_without_a_form_the_window_is_still_shown_and_protected_right_after_it_is_found(flow):
    """The form was not found (pywebview internals changed): we still show a window that was born
    hidden, otherwise it would not appear at all, and the protection goes right after the window
    is found, before size and corners."""
    flow.state.form = None
    panels = flow.make(exclude=True, glass=False)
    panels.open("chat")

    assert flow.log == ["create", "show", "find", "hide_one", "resize", "round", "max"]


# --------------------------------------------------------------------------
# Refusal
# --------------------------------------------------------------------------


@pytest.mark.windows_only
def test_a_panel_that_could_not_be_hidden_is_closed_not_left_on_screen(flow):
    """A window that is visible in a screen share while the person is sure it is hidden is worse
    than no window at all.
    """
    flow.state.hide_one = False
    panels = flow.make(exclude=True, glass=False)

    with pytest.raises(RuntimeError, match="hide the panel from recordings") as raised:
        panels.open("chat")

    assert flow.log == ["create", "hide_one", "destroy"], "no showing, no sizes - only closing"
    assert not panels.windows and not panels.hwnds
    assert plain(str(raised.value)), "the page will show this phrase: it has to be human-readable"


def test_a_fallback_window_that_could_not_be_hidden_is_closed_too(flow, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    flow.state.hide_one = False
    panels = flow.make(exclude=True, glass=False)

    with pytest.raises(RuntimeError, match="hide the panel from recordings"):
        panels.open("chat")

    assert flow.log == ["create", "find", "hide_one", "destroy"]
    assert not panels.windows and not panels.hwnds


@pytest.mark.windows_only
def test_a_window_that_never_shows_up_is_closed(flow, monkeypatch):
    monkeypatch.setattr(Panels, "_await_titled", staticmethod(lambda *a, **kw: flow.log.append("find")))
    panels = flow.make(exclude=True, glass=True)

    with pytest.raises(RuntimeError, match="didn't appear"):
        panels.open("live")

    assert flow.log[-1] == "destroy"
    assert not panels.windows


# --------------------------------------------------------------------------
# A single "hidden" state
# --------------------------------------------------------------------------


class _Pane:
    events = _events()

    def __init__(self, log, name):
        self.log, self.name = log, name

    def show(self):
        self.log.append(f"show:{self.name}")

    def hide(self):
        self.log.append(f"hide:{self.name}")


def _panels_with(monkeypatch, log, names=("chat",), exclude=False):
    cfg = Config()
    cfg.ui.exclude_from_capture = exclude
    panels = Panels("http://127.0.0.1:0", cfg)
    panels.ready.set()
    for number, name in enumerate(names, start=1):
        panels.windows[name] = _Pane(log, name)
        panels.hwnds[name] = 100 + number
    monkeypatch.setattr(desktop, "_show_panel", lambda window, hwnd=None: window.show())
    monkeypatch.setattr(desktop, "_resize_one", lambda hwnd, w, h: True)
    return panels


def test_opening_a_panel_while_they_are_hidden_clears_the_flag(monkeypatch):
    """The "detach" button in the dashboard while the panels are hidden: the window is visible, but
    the flag said "hidden".
    """
    log: list[str] = []
    panels = _panels_with(monkeypatch, log, names=("chat", "screen"))
    panels.hide_all()
    assert panels.hidden is True

    panels.open("chat")                      # one already exists: the same window is raised

    assert panels.hidden is False, "the window is shown - the flag must not lie"
    assert "show:chat" in log and "show:screen" in log, "one flag for all windows: all of them come back"
    # now the size request is applied, not queued
    assert panels.resize("chat", 500, 300) is True


def test_the_hide_key_after_such_an_open_hides_instead_of_revealing(monkeypatch):
    log: list[str] = []
    panels = _panels_with(monkeypatch, log, names=("chat",))
    panels.hide_all()
    panels.open("chat")
    del log[:]

    assert panels.toggle() is True, "one press - hide"
    assert log == ["hide:chat"]


def _actions(monkeypatch, panels):
    """The hotkey actions as `_start_hotkeys` builds them, without real keys."""
    captured: dict = {}

    class FakeKeys:
        def __init__(self, actions, api=None):
            captured["actions"] = actions

        def start(self, combos):
            captured["combos"] = combos

        def stop(self):
            pass

    monkeypatch.setattr("argus.hotkeys.Hotkeys", FakeKeys)
    monkeypatch.setattr(
        "argus.assist.settings.read",
        lambda cfg: types.SimpleNamespace(hotkeys=types.SimpleNamespace(model_dump=dict)),
    )
    monkeypatch.setattr(desktop, "_PANELS", panels)
    monkeypatch.setattr(desktop, "_HOTKEYS", None)
    desktop._start_hotkeys(Config(), "http://127.0.0.1:1", None)
    return captured["actions"]


def test_the_panel_key_uses_the_state_of_the_panels_themselves(monkeypatch):
    log: list[str] = []
    panels = _panels_with(monkeypatch, log, names=("chat", "screen"))
    actions = _actions(monkeypatch, panels)

    actions["hide_panel"]()
    assert panels.hidden is True and log == ["hide:chat", "hide:screen"]

    # the panel was opened with the mouse while the others are hidden: the next key press must
    # hide, not show
    panels.open("chat")
    assert panels.hidden is False
    del log[:]
    actions["hide_panel"]()
    assert panels.hidden is True and log == ["hide:chat", "hide:screen"]

    actions["hide_panel"]()
    assert panels.hidden is False and log[-2:] == ["show:chat", "show:screen"]


def test_the_panel_key_opens_the_live_window_when_nothing_is_detached(monkeypatch):
    opened: list[str] = []
    panels = Panels("http://127.0.0.1:0", Config())
    monkeypatch.setattr(panels, "open", opened.append)
    actions = _actions(monkeypatch, panels)

    actions["hide_panel"]()

    assert opened == ["live"]


def test_the_recognition_key_never_starts_recognition_blind(monkeypatch):
    """The state was not read: turning on blindly means paid recognition and the microphone."""
    posted: list[tuple] = []
    monkeypatch.setattr(desktop, "_post_json", lambda url, payload: posted.append((url, payload)))
    actions = _actions(monkeypatch, Panels("http://127.0.0.1:0", Config()))

    monkeypatch.setattr(desktop, "_get_json", lambda url: None)
    actions["recognition"]()
    monkeypatch.setattr(desktop, "_get_json", lambda url: {})
    actions["recognition"]()
    monkeypatch.setattr(desktop, "_get_json", lambda url: {"healthy": True})            # an answer without the remote control
    actions["recognition"]()
    monkeypatch.setattr(desktop, "_get_json", lambda url: {"controls": {"running": None}})
    actions["recognition"]()
    assert posted == [("http://127.0.0.1:1/control", {"running": False})] * 4

    del posted[:]
    monkeypatch.setattr(desktop, "_get_json", lambda url: {"controls": {"running": False}})
    actions["recognition"]()
    monkeypatch.setattr(desktop, "_get_json", lambda url: {"controls": {"running": True}})
    actions["recognition"]()
    assert [p[1] for p in posted] == [{"running": True}, {"running": False}], "a normal toggle"


# --------------------------------------------------------------------------
# Bringing the hidden panels back
# --------------------------------------------------------------------------


def test_showing_the_panels_protects_them_again_first(monkeypatch):
    """The switch could have been turned on while the panel was hidden; if the protection did not
    apply, we do not show the panel.
    """
    log: list[str] = []
    panels = _panels_with(monkeypatch, log, names=("chat", "screen"), exclude=True)
    answers = {101: True, 102: False}
    monkeypatch.setattr(desktop, "_hide_one", lambda hwnd: log.append(f"hide_one:{hwnd}") or answers[hwnd])

    panels.hide_all()
    del log[:]
    panels.show_all()

    assert log == ["hide_one:101", "show:chat", "hide_one:102"]
    assert panels.hidden is True, "one panel stayed hidden - the next press will try again"


def test_showing_the_panels_without_the_switch_does_not_touch_the_flag(monkeypatch):
    log: list[str] = []
    panels = _panels_with(monkeypatch, log, names=("chat",), exclude=False)
    monkeypatch.setattr(desktop, "_hide_one", lambda hwnd: log.append("hide_one") or False)

    panels.hide_all()
    panels.show_all()

    assert "hide_one" not in log and panels.hidden is False


# --------------------------------------------------------------------------
# Real Win32 windows off screen: the protection is checked on the system
# --------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _make_window(title: str, *, visible: bool = True, ex: int = 0x08000080) -> int:
    """A real window off-screen, without focus (`WS_EX_NOACTIVATE`) and without a taskbar button
    (`WS_EX_TOOLWINDOW`, as in `test_caption_removed.py`): the check must not flash windows and
    buttons."""
    u = ctypes.windll.user32
    u.CreateWindowExW.restype = wintypes.HWND
    u.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    style = 0x00CF0000 | (0x10000000 if visible else 0)
    return u.CreateWindowExW(ex, "STATIC", title, style, -32000, -32000, 400, 300, None, None, None, None)


def _affinity(hwnd: int) -> int:
    got = wintypes.DWORD()
    assert ctypes.windll.user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
    return got.value


@pytest.fixture
def probes():
    """Probe windows: they close themselves whatever happens."""
    made: list[int] = []

    def make(title, **kw):
        hwnd = _make_window(title, **kw)
        assert hwnd, "the probe window was not created"
        made.append(hwnd)
        return hwnd

    yield make
    for hwnd in made:
        ctypes.windll.user32.DestroyWindow(hwnd)


@windows_only
def test_hide_and_show_in_capture_change_the_real_flag(probes, monkeypatch):
    """A wrong flag value, a lost check or an uncalled `SetWindowDisplayAffinity` are caught here:
    before, the only test ran without a window and passed with any behavior of the code."""
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMain")
    hwnd = probes("ArgusProbeMain")

    ok, detail = desktop.hide_from_capture(timeout=2)
    assert ok, detail
    assert _affinity(hwnd) == desktop.WDA_EXCLUDEFROMCAPTURE == 0x11

    ok, detail = desktop.show_in_capture(timeout=2)
    assert ok, detail
    assert _affinity(hwnd) == desktop.WDA_NONE == 0


@windows_only
def test_the_switch_also_reaches_windows_that_are_hidden_right_now(probes, monkeypatch):
    """A panel hidden with a key was skipped when the switch was turned on, and came back without
    protection.
    """
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMain")
    main = probes("ArgusProbeMain")
    panel = probes(PANEL_TITLES["chat"], visible=False)
    stranger = probes("Foreign service window", visible=False)

    assert panel not in desktop.own_windows() and panel in desktop.own_windows(hidden=True)
    assert stranger not in desktop.own_windows(hidden=True), "we do not touch foreign service windows"

    ok, detail = desktop.hide_from_capture(timeout=2)
    assert ok, detail
    assert _affinity(main) == 0x11 and _affinity(panel) == 0x11 and _affinity(stranger) == 0

    ok, detail = desktop.show_in_capture(timeout=2)
    assert ok, detail
    assert _affinity(panel) == 0


@windows_only
def test_hidden_windows_are_looked_up_without_messaging_them(probes, monkeypatch):
    """`GetWindowText` sends `WM_GETTEXT` to a window of its own process and waits for the answer:
    with a window whose thread is busy or does not process messages, the iteration on the
    capture-protection path would stall along with it."""
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMain")
    panel = probes(PANEL_TITLES["chat"], visible=False)

    def sent(hwnd):
        raise AssertionError("the title of a hidden window was taken by sending it a message")

    monkeypatch.setattr(desktop, "_window_title", sent)

    assert desktop._quiet_title(panel) == PANEL_TITLES["chat"]
    assert panel in desktop.own_windows(hidden=True)


@windows_only
def test_hide_one_sets_and_reads_back_the_flag(probes):
    hwnd = probes("ArgusProbeOne")
    assert desktop._hide_one(hwnd) is True
    assert _affinity(hwnd) == 0x11
    assert desktop._hide_one(0) is False, "a nonexistent window is a refusal, not 'succeeded'"


@windows_only
def test_the_panic_key_hides_a_panel_even_if_its_form_did_not_notice_it_was_shown(probes):
    """`_show_panel` brings the panel back with a bare `ShowWindow`; a form that did not notice this
    would do nothing on its own `Hide`, and a miss is unacceptable for the "panic" key."""
    hwnd = probes("ArgusProbePanel")
    log: list[str] = []
    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["chat"] = _Pane(log, "chat")             # its `hide()` does nothing to the window
    panels.hwnds["chat"] = hwnd
    assert ctypes.windll.user32.IsWindowVisible(hwnd)

    panels.hide_all()

    assert log == ["hide:chat"] and panels.hidden is True
    assert not ctypes.windll.user32.IsWindowVisible(hwnd), "the window is hidden directly"
    desktop._hide_native(hwnd)                              # already hidden: a second time breaks nothing
    desktop._hide_native(None)


@windows_only
def test_the_taskbar_button_and_alt_tab_entry_go_away(probes):
    """The taskbar is included in a capture of the whole screen: an "invisible" panel with its
    button beside it would give it away.
    """
    hwnd = probes("ArgusProbeTool", ex=0x08000000 | desktop.WS_EX_APPWINDOW)
    u = ctypes.windll.user32
    before = ctypes.c_uint32(u.GetWindowLongW(hwnd, desktop.GWL_EXSTYLE)).value
    assert before & desktop.WS_EX_APPWINDOW and not before & desktop.WS_EX_TOOLWINDOW

    assert desktop._no_taskbar_button(hwnd) is True

    after = ctypes.c_uint32(u.GetWindowLongW(hwnd, desktop.GWL_EXSTYLE)).value
    assert after & desktop.WS_EX_TOOLWINDOW and not after & desktop.WS_EX_APPWINDOW
    assert after & 0x08000000, "the other window flags are untouched"
    assert desktop._window_title(hwnd) == "ArgusProbeTool", "the window is found by its title as before"


def _exstyle(hwnd: int) -> int:
    return ctypes.c_uint32(ctypes.windll.user32.GetWindowLongW(hwnd, desktop.GWL_EXSTYLE)).value


@windows_only
def test_the_main_window_loses_and_regains_its_taskbar_button_without_leaving_the_screen(probes, monkeypatch):
    """`ui.main_taskbar_button = false`: the same style as for panels, but the window is already
    shown, so it hides for a moment and comes back, stays visible and loses nothing else in its
    style. Back via the switch in the settings."""
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMainTaskbar")
    monkeypatch.setitem(desktop._main_no_taskbar, "on", False)
    hwnd = probes("ArgusProbeMainTaskbar", ex=0x08000000 | desktop.WS_EX_APPWINDOW)
    u = ctypes.windll.user32
    assert u.IsWindowVisible(hwnd) and _exstyle(hwnd) & desktop.WS_EX_APPWINDOW

    ok, detail = desktop._main_taskbar_button(False)

    assert ok, detail
    assert _exstyle(hwnd) & desktop.WS_EX_TOOLWINDOW and not _exstyle(hwnd) & desktop.WS_EX_APPWINDOW
    assert _exstyle(hwnd) & 0x08000000, "the other window flags are untouched"
    assert u.IsWindowVisible(hwnd), "the window stayed on the screen"
    assert desktop._main_no_taskbar["on"] is True

    ok, detail = desktop._main_taskbar_button(True)

    assert ok, detail
    assert _exstyle(hwnd) & desktop.WS_EX_APPWINDOW and not _exstyle(hwnd) & desktop.WS_EX_TOOLWINDOW
    assert u.IsWindowVisible(hwnd)
    assert desktop._main_no_taskbar["on"] is False


@windows_only
def test_a_main_window_hidden_by_the_key_stays_hidden_when_its_button_goes(probes, monkeypatch):
    """A window hidden by the key combination must not "pop out" because of the style change."""
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMainTaskbar")
    monkeypatch.setitem(desktop._main_no_taskbar, "on", False)
    hwnd = probes("ArgusProbeMainTaskbar", visible=False, ex=0x08000000 | desktop.WS_EX_APPWINDOW)

    ok, detail = desktop._main_taskbar_button(False)

    assert ok, detail
    assert _exstyle(hwnd) & desktop.WS_EX_TOOLWINDOW
    assert not ctypes.windll.user32.IsWindowVisible(hwnd)


@windows_only
def test_without_a_taskbar_button_minimize_hides_the_window_instead(probes, monkeypatch):
    """A minimized "tool" window would remain a strip at the edge of the screen with no button to
    take it by: "Minimize" hides the window, like the key combination, and it can be
    brought back with the same combination or by a second launch."""
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMainMin")
    monkeypatch.setitem(desktop._main_hidden, "on", False)
    hwnd = probes("ArgusProbeMainMin")
    u = ctypes.windll.user32
    hidden: list[str] = []
    monkeypatch.setattr(desktop, "_MAIN_WINDOW", types.SimpleNamespace(hide=lambda: hidden.append("hide")))

    monkeypatch.setitem(desktop._main_no_taskbar, "on", True)
    desktop.main_window_action("minimize")
    assert hidden == ["hide"] and desktop.main_window_hidden() is True
    assert not u.IsIconic(hwnd), "the window is not minimized"

    monkeypatch.setitem(desktop._main_hidden, "on", False)
    monkeypatch.setitem(desktop._main_no_taskbar, "on", False)
    desktop.main_window_action("minimize")
    assert hidden == ["hide"], "with a taskbar button 'Minimize' minimizes as always"
    assert u.IsIconic(hwnd)


@windows_only
def test_a_window_is_taken_for_ours_by_its_program_not_by_its_title(probes):
    """A window titled "Argus" can also belong to a folder in Explorer: the second launch used to
    raise it instead of the program.
    """
    ours = probes("Argus")
    assert desktop._is_same_program(ours) is True

    tray = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        pytest.skip("this machine has no Explorer taskbar")
    assert desktop._is_same_program(tray) is False


@windows_only
def test_hiding_a_window_that_cannot_take_the_flag_is_reported(monkeypatch):
    """A silent refusal is worse than not having the feature: with no windows hidden, say so in
    words instead of "succeeded".
    """
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "")
    monkeypatch.setattr(desktop, "own_windows", lambda hidden=False: [])
    ok, detail = desktop.hide_from_capture(timeout=0.1)
    assert ok is False and detail == "window not found"


# --------------------------------------------------------------------------
# The glass survives a change of system settings (desk-a-9)
# --------------------------------------------------------------------------


def _glass_system(monkeypatch, dwm_result):
    """Stub .NET and DWM: a form with a handle, which `_glass` paints and for which pywebview keeps
    its own theme-change handling."""
    system = types.ModuleType("System")
    system.Func = type("F", (), {"__getitem__": lambda s, i: (lambda fn: fn)})()
    system.Type = object
    drawing = types.ModuleType("System.Drawing")
    drawing.Color = types.SimpleNamespace(Black="black")
    drawing.ColorTranslator = types.SimpleNamespace(FromHtml=lambda color: color)
    system.Drawing = drawing
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "System.Drawing", drawing)

    theme_calls: list[str] = []

    class Form:
        IsHandleCreated = True
        InvokeRequired = False
        BackColor = None
        Handle = types.SimpleNamespace(ToInt64=lambda: 42)

        def update_title_bar_theme(self):
            theme_calls.append("pywebview set its own backdrop type")

    form = Form()
    winforms = types.ModuleType("webview.platforms.winforms")
    winforms.BrowserView = types.SimpleNamespace(instances={"w1": form})
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", winforms)

    dwm = types.SimpleNamespace(
        DwmExtendFrameIntoClientArea=lambda hwnd, margins: 0,
        DwmSetWindowAttribute=lambda hwnd, attr, value, size: dwm_result,
    )
    monkeypatch.setattr(ctypes.windll, "dwmapi", dwm)
    return form, theme_calls


@windows_only
def test_the_glass_survives_a_change_of_system_settings(monkeypatch):
    """On every change of theme, accent or wallpaper, pywebview sets "mica" again on a dark system,
    and the panel's glass would be lost until it is reopened. After a successful `_glass` its
    theme update is empty."""
    form, theme_calls = _glass_system(monkeypatch, dwm_result=0)

    assert desktop._glass(types.SimpleNamespace(uid="w1"), timeout=0.2) is True
    form.update_title_bar_theme()

    assert theme_calls == []


@windows_only
def test_a_window_that_did_not_get_glass_keeps_pywebviews_theme_handling(monkeypatch):
    form, theme_calls = _glass_system(monkeypatch, dwm_result=1)

    assert desktop._glass(types.SimpleNamespace(uid="w1"), timeout=0.2) is False
    form.update_title_bar_theme()

    assert theme_calls == ["pywebview set its own backdrop type"]


# --------------------------------------------------------------------------
# The "invisible on recording" switch and hidden windows (desk-a-6, desk-b-11, desk-b-21)
# --------------------------------------------------------------------------


@windows_only
def test_the_settings_switch_protects_a_panel_hidden_by_the_key(probes, monkeypatch):
    """A panel hidden by the key did not get into the iteration: the switch showed "on", and the
    panel, coming back with the key, ended up in the screen share without protection."""
    monkeypatch.setitem(desktop._MAIN_TITLE, "t", "ArgusProbeMain")
    monkeypatch.setitem(desktop._capture_excluded, "on", False)
    main = probes("ArgusProbeMain")
    panel = probes(PANEL_TITLES["chat"], visible=False)         # hidden by "panic"
    cfg = Config()
    cfg.ui.exclude_from_capture = False
    monkeypatch.setattr(desktop, "_PANELS", Panels("http://127.0.0.1:0", cfg))

    ok, detail = desktop.set_capture_exclusion(True)

    assert ok, detail
    assert desktop.capture_excluded() is True and cfg.ui.exclude_from_capture is True
    assert _affinity(main) == 0x11 and _affinity(panel) == 0x11

    ok, detail = desktop.set_capture_exclusion(False)
    assert ok, detail
    assert desktop.capture_excluded() is False and cfg.ui.exclude_from_capture is False
    assert _affinity(main) == 0 and _affinity(panel) == 0


# --------------------------------------------------------------------------
# A hidden panel does not pop up when the page reloads (desk-b-12)
# --------------------------------------------------------------------------


class _Signal:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def _fake_form(monkeypatch):
    shown = []

    class Form:
        Visible = False
        VisibleChanged = _Signal()

        def Hide(self):                      
            shown.append("hide")
            self.Visible = False

    form = Form()
    winforms = types.ModuleType("webview.platforms.winforms")
    winforms.BrowserView = types.SimpleNamespace(instances={"p1": form})
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", winforms)
    return form, shown


def test_a_hidden_panel_that_pywebview_shows_again_is_hidden_again(monkeypatch):
    """pywebview shows the glass window on every navigation, and a language change reloads all the
    windows.
    """
    form, calls = _fake_form(monkeypatch)
    panels = Panels("http://127.0.0.1:0", Config())

    panels._keep_hidden_when_asked(types.SimpleNamespace(uid="p1"))
    (handler,) = form.VisibleChanged.handlers

    panels.hidden = False                      # the panels are shown: the guard stays quiet
    form.Visible = True
    handler(form, None)
    assert calls == [] and form.Visible is True

    panels.hidden = True                       # hidden with a key, and then the page reloaded and "showed" the window
    handler(form, None)
    assert calls == ["hide"] and form.Visible is False

    handler(form, None)                        # already hidden: we do not touch it a second time
    assert calls == ["hide"]


def test_a_failing_guard_never_breaks_the_panel(monkeypatch):
    panels = Panels("http://127.0.0.1:0", Config())
    panels._keep_hidden_when_asked(object())                   # the window has no `uid`: simply no guard

    form, _calls = _fake_form(monkeypatch)
    form.Hide = lambda: (_ for _ in ()).throw(OSError("the form is already closed"))
    panels._keep_hidden_when_asked(types.SimpleNamespace(uid="p1"))
    panels.hidden = True
    form.Visible = True
    form.VisibleChanged.handlers[0](form, None)                 # does not throw: an error has no place in a .NET handler


# --------------------------------------------------------------------------
# No WebView2: `doctor` sees it (desk-b-14)
# --------------------------------------------------------------------------


@windows_only
@pytest.mark.parametrize("chromium, expected", [(False, False), (True, True)])
def test_the_window_check_notices_a_missing_webview_runtime(monkeypatch, chromium, expected):
    fake = types.ModuleType("webview.platforms.winforms")
    fake.is_chromium = chromium
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", fake)
    import webview.platforms

    monkeypatch.setattr(webview.platforms, "winforms", fake, raising=False)

    ok, detail = desktop.available()

    assert ok is expected
    if not expected:
        assert "WebView2" in detail
