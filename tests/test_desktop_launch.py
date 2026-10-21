"""Window launch and second launch: proxy, foreign windows, mutex, hidden main window,
startup failures.

Audit of 2026-10-14 (desk-a-3/4/7/8, desk-b-3/5/9/13/14/15/16/18, mac-3). There is no
real window here: the page, `webview` and `user32` are fakes, and what can be checked in
the system without showing windows is checked in the system.
"""

from __future__ import annotations

import asyncio
import ctypes
import http.server
import json
import sys
import threading
import time
import types
import urllib.request
from ctypes import wintypes

import pytest

from argus import desktop
from argus.core.config import Config

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: windows and mutexes")


# --------------------------------------------------------------------------
# Own server on the loopback, bypassing the proxy
# --------------------------------------------------------------------------


@pytest.fixture
def local_server():
    seen: dict = {"posted": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"ok": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen["posted"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seen["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield seen
    server.shutdown()
    server.server_close()


def test_loopback_calls_do_not_go_through_a_dead_proxy(local_server, monkeypatch):
    """With a closed Fiddler or a VPN proxy the port answers, while every `urlopen` failed:
    launch waited for the server for all 30 seconds and closed, and the five hotkeys
    silently did not reach the server."""
    for key in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    url = local_server["url"]

    # proof that the test checks something: an ordinary opener goes to a dead proxy
    with pytest.raises(OSError):
        urllib.request.build_opener().open(url, timeout=2)

    assert desktop.wait_until_serving(url, timeout=5.0) is True
    assert desktop._get_json(url + "/state") == {"ok": 1}
    desktop._post_json(url + "/ask", {"deep": True})
    assert local_server["posted"] == [{"deep": True}]


def test_only_our_own_opener_skips_the_proxy(monkeypatch):
    """Only our own server bypasses the proxy, and via an opener, not `NO_PROXY` in the
    environment: a non-empty proxy set there would make `getproxies()` skip the system
    settings, and outgoing requests (account, answers) would lose the person's proxy."""
    import os

    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    # an empty `ProxyHandler` does not get into the handler list (it has no `*_open`); what
    # matters is that the system one is not there: it replaced the one that `build_opener`
    # puts in by default
    proxies = [h for h in desktop._LOOPBACK.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert not proxies, "this opener has no proxies"
    assert "NO_PROXY" not in os.environ and "no_proxy" not in os.environ, "we do not touch the environment"


# --------------------------------------------------------------------------
# Second launch: foreign windows, an unfolded window, mutex
# --------------------------------------------------------------------------


class _User32:
    """`user32` made of three stub windows: what is visible, what is minimized, what was asked
    to be done.
    """

    def __init__(self, windows):
        self.windows = {w["hwnd"]: w for w in windows}
        self.calls: list[tuple] = []

    def EnumWindows(self, callback, _lparam):
        for hwnd in list(self.windows):
            if not callback(hwnd, 0):
                break
        return 1

    def IsWindowVisible(self, hwnd):
        return int(self.windows[hwnd]["visible"])

    def IsIconic(self, hwnd):
        return int(self.windows[hwnd].get("iconic", False))

    def ShowWindow(self, hwnd, command):
        self.calls.append(("show", hwnd, command))
        return 1

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("front", hwnd))
        return 1


@pytest.fixture
def second_launch(monkeypatch):
    def prepare(windows):
        user32 = _User32(windows)
        monkeypatch.setattr(ctypes.windll, "user32", user32)
        monkeypatch.setattr(desktop, "_window_title", lambda hwnd: user32.windows[hwnd]["title"])
        monkeypatch.setattr(desktop, "_is_same_program", lambda hwnd: user32.windows[hwnd].get("ours", True))
        return user32

    return prepare


@windows_only
def test_the_second_launch_skips_a_stranger_window_with_our_title(second_launch):
    """The "Argus" folder in Explorer: it was found first, and the real window under the others
    stayed buried.
    """
    user32 = second_launch([
        {"hwnd": 1, "title": "Argus", "visible": True, "ours": False},       # Explorer
        {"hwnd": 2, "title": "Argus", "visible": False, "ours": True},       # the program's hidden window
    ])

    assert desktop._focus_existing_window("Argus") is False, "a foreign folder is no reason to consider the program found"
    assert user32.calls == []

    assert desktop._focus_existing_window("Argus", include_hidden=True) is True
    assert user32.calls == [("show", 2, desktop.SW_SHOW), ("front", 2)]


@windows_only
def test_the_second_launch_does_not_unmaximize_the_main_window(second_launch):
    """The main window is maximized by default, and `SW_RESTORE` made it a normal one on every
    second launch.
    """
    user32 = second_launch([{"hwnd": 5, "title": "Argus", "visible": True, "iconic": False}])

    assert desktop._focus_existing_window("Argus") is True

    assert user32.calls == [("front", 5)], "only to the front, without restoring the size"


@windows_only
def test_the_second_launch_restores_a_minimized_window(second_launch):
    user32 = second_launch([{"hwnd": 5, "title": "Argus", "visible": True, "iconic": True}])

    assert desktop._focus_existing_window("Argus") is True

    assert user32.calls == [("show", 5, desktop.SW_RESTORE), ("front", 5)]


@windows_only
def test_a_mutex_that_exists_but_is_closed_to_us_means_somebody_runs(monkeypatch):
    """The previous instance was launched as administrator: the mutex carries its rights, our
    launch got NULL and "access denied", considered itself the first and went for a busy
    port."""
    kernel32, advapi = ctypes.windll.kernel32, ctypes.windll.advapi32

    class Attributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    descriptor = ctypes.c_void_p()
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:(D;;GA;;;WD)", 1, ctypes.byref(descriptor), None
    ), "the access denial was not built"          # "everything is forbidden to everyone": the name cannot be opened, but the creator can
    attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
    name = f"ArgusSingleInstance-denied-{time.monotonic_ns()}"
    handle = kernel32.CreateMutexW(ctypes.byref(attributes), False, name)
    assert handle
    try:
        monkeypatch.setattr(desktop, "_instance_mutex", None)
        assert desktop._claim_single_instance(name) is False
    finally:
        kernel32.CloseHandle(handle)
        kernel32.LocalFree(descriptor)


def test_waiting_without_a_lock_does_not_spin(monkeypatch):
    """We have no lock — `_acquire_when_free` returned at once, and the second launch spun
    through the windows without a pause for all 25 seconds."""
    monkeypatch.setattr(desktop, "_instance_mutex", None)
    started = time.monotonic()

    assert desktop._acquire_when_free(0.1) is False

    assert time.monotonic() - started >= 0.09


# --------------------------------------------------------------------------
# Main window: whether it is hidden is decided by the screen
# --------------------------------------------------------------------------


def test_the_hide_key_decides_by_what_is_on_screen_not_by_memory(monkeypatch):
    """The second launch shows the hidden window bypassing this process: the remembered
    "hidden" stayed, and the very first press of "hide" would only activate the already
    visible window. A panic key cannot miss."""
    calls: list[str] = []
    window = types.SimpleNamespace(hide=lambda: calls.append("hide"), show=lambda: calls.append("show"))
    monkeypatch.setattr(desktop, "_MAIN_WINDOW", window)
    monkeypatch.setitem(desktop._main_hidden, "on", True)               # we remember "hidden"
    monkeypatch.setattr(desktop, "_main_visible", lambda: True)         # but the window is on the screen

    assert desktop.toggle_main_window() is True
    assert calls == ["hide"]

    monkeypatch.setattr(desktop, "_main_visible", lambda: False)
    assert desktop.toggle_main_window() is False
    assert calls == ["hide", "show"]


def test_when_the_screen_cannot_be_asked_the_remembered_state_decides(monkeypatch):
    calls: list[str] = []
    window = types.SimpleNamespace(hide=lambda: calls.append("hide"), show=lambda: calls.append("show"))
    monkeypatch.setattr(desktop, "_MAIN_WINDOW", window)
    monkeypatch.setattr(desktop, "_main_visible", lambda: None)
    monkeypatch.setitem(desktop._main_hidden, "on", False)

    desktop.toggle_main_window()
    desktop.toggle_main_window()

    assert calls == ["hide", "show"]


# --------------------------------------------------------------------------
# run_desktop on a fake window
# --------------------------------------------------------------------------


class _Event:
    def __init__(self):
        self.items = []

    def __iadd__(self, fn):
        self.items.append(fn)
        return self


class _Window:
    def __init__(self, env):
        self.env = env
        self.events = types.SimpleNamespace(closing=_Event())
        self.hidden = False
        self.destroyed = False

    def load_url(self, url):
        self.env.loaded.append(url)

    def destroy(self):
        self.destroyed = True

    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False


@pytest.fixture
def env(monkeypatch):
    out = types.SimpleNamespace(loaded=[], alerts=[], calls=[], window=None, during=None, module=None)
    module = types.ModuleType("webview")

    def create_window(*args, **kwargs):
        out.window = _Window(out)
        return out.window

    def start(func=None, **kwargs):
        if func is not None:
            func()
        if out.during is not None:
            out.during()

    module.create_window, module.start = create_window, start
    out.module = module
    monkeypatch.setitem(sys.modules, "webview", module)
    monkeypatch.setattr(desktop, "_start_hotkeys", lambda *a, **k: out.calls.append("hotkeys"))
    monkeypatch.setattr(desktop, "_declare_dpi_aware", lambda: None)
    monkeypatch.setattr(desktop, "_PANELS", None)
    monkeypatch.setattr(desktop, "_HOTKEYS", None)
    monkeypatch.setattr(desktop, "wait_until_serving", lambda *a, **k: True)
    monkeypatch.setattr(desktop, "_alert", lambda text, warning=False: out.alerts.append((text, warning)))
    monkeypatch.setattr(desktop, "use_page_caption", lambda title, color: out.calls.append("caption") or False)
    monkeypatch.setattr(desktop, "caption_removed", lambda title: False)
    monkeypatch.setattr(desktop, "CAPTURE_RETRY_S", 0)
    # Capture protection is on by default (`UIConfig.exclude_from_capture`), and there is no
    # real window here: let it "succeed" unless the test substituted its own. Tests about a
    # protection failure substitute the failure themselves.
    monkeypatch.setattr(desktop, "hide_from_capture", lambda *a, **k: (True, "window hidden from capture (stub)"))
    monkeypatch.setattr(desktop, "_main_taskbar_button", lambda shown: out.calls.append(f"button:{shown}") or (True, "ok"))
    monkeypatch.setitem(desktop._capture_excluded, "on", False)
    monkeypatch.setitem(desktop._main_hidden, "on", False)
    monkeypatch.setitem(desktop._main_parked, "on", False)
    monkeypatch.setitem(desktop._main_no_taskbar, "on", False)
    return out


async def _done(stop):
    pass


async def _until_stopped(stop):
    while not stop.is_set():
        await asyncio.sleep(0.01)


def _run(path="", pipeline=_done, cfg=None):
    return desktop.run_desktop(cfg or Config(), pipeline, 18999, path=path, claimed=True)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_window_that_cannot_be_hidden_is_retried_then_said_out_loud(env, monkeypatch):
    """Previously — one console line, which the built application does not have: the page with
    answers opened as if nothing had happened, and the person shared the screen sure that
    the window could not be seen."""
    attempts = []
    monkeypatch.setattr(desktop, "hide_from_capture", lambda *a, **k: attempts.append(k) or (False, "hidden 1 windows out of 2"))
    cfg = Config()
    cfg.ui.exclude_from_capture = True

    assert _run(cfg=cfg) == 0

    assert len(attempts) == desktop.CAPTURE_ATTEMPTS, "the window may lag behind the 'shown' event, so we try again"
    assert attempts[0] == {} and all(k == {"timeout": 1.0} for k in attempts[1:]), "we wait for the window only the first time"
    assert _wait_for(lambda: env.alerts), "the person is told with a message box"
    assert env.alerts[0] == (desktop.CAPTURE_WARNING, True)
    assert desktop.capture_excluded() is False, "the switch in the settings will show the truth"
    assert "capture=fail" in env.loaded[0], "the page can also say 'visible in the recording'"


def test_a_window_that_hides_on_the_second_try_is_not_a_failure(env, monkeypatch):
    answers = iter([(False, "window not found"), (True, "window hidden from capture (1)")])
    monkeypatch.setattr(desktop, "hide_from_capture", lambda *a, **k: next(answers))
    cfg = Config()
    cfg.ui.exclude_from_capture = True

    _run(cfg=cfg)

    assert desktop.capture_excluded() is True
    assert env.alerts == [] and "capture=fail" not in env.loaded[0]


def test_an_exception_while_hiding_is_a_failure_not_a_dead_thread(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("ctypes refused")

    monkeypatch.setattr(desktop, "hide_from_capture", boom)
    cfg = Config()
    cfg.ui.exclude_from_capture = True

    assert _run(cfg=cfg) == 0

    assert env.loaded, "the page is loaded: a protection failure does not break the launch, but it is reported"
    assert _wait_for(lambda: env.alerts) and "capture=fail" in env.loaded[0]


def test_the_protection_is_on_without_any_settings_file(env):
    """The code default is "invisible when recorded": the window is hidden from capture also for
    those who launched from source without `argus.toml` (earlier the default was the
    opposite, and the protection depended on the file lying next to it)."""
    _run(cfg=Config())

    assert desktop.capture_excluded() is True


def test_the_taskbar_button_of_the_main_window_stays_by_default(env):
    _run(cfg=Config())

    assert not [c for c in env.calls if str(c).startswith("button:")]


@windows_only
def test_the_taskbar_button_is_dropped_after_the_capture_protection_and_before_the_page(env, monkeypatch):
    """The "Argus" taskbar button is visible in a shared screen next to the invisible window
    (`ui.main_taskbar_button`).
    """
    order = []
    monkeypatch.setattr(desktop, "hide_from_capture", lambda *a, **k: order.append("capture") or (True, "ok"))
    monkeypatch.setattr(desktop, "_main_taskbar_button", lambda shown: order.append(f"button:{shown}") or (True, "ok"))
    monkeypatch.setattr(desktop, "use_page_caption", lambda title, color: order.append("caption") or False)
    cfg = Config()
    cfg.ui.main_taskbar_button = False

    _run(cfg=cfg)

    assert order == ["capture", "button:False", "caption"]
    assert env.loaded, "the page is open"


@windows_only
def test_a_button_that_cannot_be_dropped_is_retried_and_never_stops_the_start(env, monkeypatch):
    attempts = []
    monkeypatch.setattr(
        desktop, "_main_taskbar_button", lambda shown: attempts.append(shown) or (False, "window not found"))
    cfg = Config()
    cfg.ui.main_taskbar_button = False

    assert _run(cfg=cfg) == 0

    assert attempts == [False, False, False], "the window may lag behind the 'shown' event: three attempts"
    assert env.loaded and env.alerts == [], "a window with a button is worse than nothing, but the launch does not break"


def test_an_exception_in_the_start_thread_closes_the_window_and_says_why(env, monkeypatch):
    """An unhandled error killed the launch thread: the page did not load, while the pipeline
    behind the splash screen kept working.
    """
    import argus.startup

    def boom(what):
        if what == "window shown":          # the first mark is inside the launch thread; the rest are outside it
            raise RuntimeError("failure in the start thread")

    monkeypatch.setattr(argus.startup, "mark", boom)

    assert _run(pipeline=_until_stopped) == 1

    assert env.loaded == [] and env.calls == []
    assert len(env.alerts) == 1 and "argus.log" in env.alerts[0][0]


@windows_only
def test_a_missing_webview_runtime_is_said_not_shown_as_a_broken_page(env):
    """pywebview without WebView2 quietly falls back to the old engine: a window without pages
    and without a system title bar.
    """
    env.module.renderer = "mshtml"

    assert _run(pipeline=_until_stopped) == 1

    assert env.loaded == [], "we do not load the page into the old engine"
    assert env.alerts == [(desktop.NO_RUNTIME_TEXT, False)]
    assert "WebView2" in desktop.NO_RUNTIME_TEXT, "the person must be told exactly what to install"


def test_the_settings_only_window_keeps_its_system_caption(env, monkeypatch):
    """`admin.html` does not draw a bar (`winbar.js` is not on it): without a system one the
    window can be neither moved nor closed.
    """
    monkeypatch.setattr(desktop, "use_page_caption", lambda title, color: env.calls.append("caption") or True)

    _run(path="/admin")

    assert "caption" not in env.calls
    assert env.loaded == ["http://127.0.0.1:18999/admin"], "such a page has no `desk=1` mark"


def test_the_marks_go_before_the_fragment_so_the_page_sees_them(env, monkeypatch):
    """`/admin#account?desk=1` — a mark after the fragment does not reach the page: the window
    bar depended on the probe.
    """
    monkeypatch.setattr(desktop, "use_page_caption", lambda title, color: True)

    _run(path=lambda: "/#account")

    assert env.loaded == ["http://127.0.0.1:18999/?desk=1#account"]


def test_the_plain_dashboard_address_still_gets_its_mark(env, monkeypatch):
    monkeypatch.setattr(desktop, "use_page_caption", lambda title, color: True)

    _run(path="")

    assert env.loaded == ["http://127.0.0.1:18999?desk=1"]


def test_add_query_keeps_existing_query_and_fragment():
    assert desktop._add_query("http://h/x?a=1#f", "desk=1", "capture=fail") == "http://h/x?a=1&desk=1&capture=fail#f"
    assert desktop._add_query("http://h/x#f") == "http://h/x#f"


# --------------------------------------------------------------------------
# The close button of the main window with panels open
# --------------------------------------------------------------------------


@windows_only
def test_the_close_button_hides_the_dashboard_while_panels_are_open(env):
    """A closed window cannot be brought back: the second launch waited 25 seconds and asked to
    kill a live program.
    """
    seen: dict = {}

    def during():
        handler = env.window.events.closing.items[0]
        seen["alone"] = handler()                                   # no panels: close as before
        desktop._PANELS.windows["chat"] = object()
        seen["with_panel"] = handler()
        seen["hidden"] = (env.window.hidden, desktop.main_window_hidden(), dict(desktop._main_parked))
        desktop._PANELS.windows.clear()
        desktop.quit_app()
        seen["quitting"] = handler()                                # the application is exiting: we do not hold it

    env.during = during
    _run(pipeline=_until_stopped)

    assert seen["alone"] is None
    assert seen["with_panel"] is False, "closing is cancelled"
    assert seen["hidden"] == (True, True, {"on": True})
    assert seen["quitting"] is None


@windows_only
def test_the_close_button_does_not_hold_back_the_application_quitting_itself(env):
    """A panel is still open and the application is quitting by itself (update, end of the
    pipeline): closing the main window must not be cancelled — separately from the case "no
    panels left", which does not hold it either."""
    seen: dict = {}

    def during():
        desktop._PANELS.windows["chat"] = object()
        desktop.quit_app()
        seen["result"] = env.window.events.closing.items[0]()
        seen["hidden"] = env.window.hidden
        desktop._PANELS.windows.clear()

    env.during = during
    _run(pipeline=_until_stopped)

    assert seen == {"result": None, "hidden": False}


@windows_only
def test_the_close_button_does_not_hold_back_a_windows_shutdown(env, monkeypatch):
    """Cancelling the main window's close on system logoff would stop the shutdown: Windows
    would say that Argus is in its way."""
    seen: dict = {}

    def during():
        desktop._PANELS.windows["chat"] = object()
        shutting_down = types.SimpleNamespace(
            GetSystemMetrics=lambda index: 1 if index == desktop.SM_SHUTTINGDOWN else 0
        )
        monkeypatch.setattr(ctypes.windll, "user32", shutting_down)
        seen["result"] = env.window.events.closing.items[0]()
        seen["hidden"] = (env.window.hidden, dict(desktop._main_parked))
        desktop._PANELS.windows.clear()

    env.during = during
    _run(pipeline=_until_stopped)

    assert seen == {"result": None, "hidden": (False, {"on": False})}


@windows_only
def test_the_parked_dashboard_closes_with_the_last_panel(env, monkeypatch):
    """Otherwise the process would stay alive without a single visible window — with sound and
    recording.
    """
    seen: dict = {}
    monkeypatch.setattr(desktop, "_main_visible", lambda: False)

    def during():
        panels = desktop._PANELS
        panels.windows["chat"] = object()
        panels.windows["screen"] = object()
        env.window.events.closing.items[0]()
        panels._forget("chat")
        seen["one_left"] = env.window.destroyed
        panels._forget("screen")
        seen["none_left"] = env.window.destroyed

    env.during = during
    _run(pipeline=_until_stopped)

    assert seen == {"one_left": False, "none_left": True}


@windows_only
def test_a_dashboard_brought_back_meanwhile_is_not_closed_with_the_last_panel(env, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(desktop, "_main_visible", lambda: True)          # the second launch brought the window back

    def during():
        panels = desktop._PANELS
        panels.windows["chat"] = object()
        env.window.events.closing.items[0]()
        panels._forget("chat")
        seen["destroyed"] = env.window.destroyed
        seen["parked"] = dict(desktop._main_parked)

    env.during = during
    _run(pipeline=_until_stopped)

    assert seen == {"destroyed": False, "parked": {"on": False}}


# --------------------------------------------------------------------------
# The "Invisible in recordings" switch does not hold the server's event loop
# --------------------------------------------------------------------------


def test_the_capture_switch_is_applied_off_the_event_loop(tmp_path, monkeypatch):
    """Without a window the switch waits for it up to five seconds (`hide_from_capture`): while
    it stood right in the handler, the server's event loop stood too — together with the
    answers to the other pages."""
    from fastapi.testclient import TestClient

    from argus.app import Argus
    from argus.ui.server import build

    cfg = Config()
    cfg.root = tmp_path
    on_loop: list[bool] = []

    def apply(excluded):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)                    # called directly on the event loop
        except RuntimeError:
            on_loop.append(False)                   # in a thread: there is no loop here
        return True, ""

    monkeypatch.setattr("argus.desktop.main_window", lambda: object())
    monkeypatch.setattr("argus.desktop.set_capture_exclusion", apply)
    client = TestClient(build(Argus(cfg, with_screen=False, with_brain=False)))

    assert client.post("/window/capture", json={"excluded": True}).json()["ok"] is True
    assert on_loop == [False]
