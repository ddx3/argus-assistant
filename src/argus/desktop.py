"""A window of its own instead of a browser tab.

The interface here is web — a local server and HTML — and that turned out to be lucky: it
did not have to be written a second time for the app. The window turned out to be a thin
wrapper showing the same loopback address through WebView2, which Windows has out of the box.
A second implementation of the interface did not appear.

The difference is not cosmetic. A browser tab is an address bar that is visible in screen
sharing, neighboring tabs, accidental closing, and the question "where did my assistant go"
after a reboot. An app is a window with a name that can be minimized, and a process that
lives on its own.

The order of work is imposed by the system, not by taste: **the window must live on the main
thread**, otherwise Windows silently will not show it. So the pipeline goes off to a
background thread, and the main one stays for the window. The way of stopping follows from
that too: the window is closed — we set an event, the pipeline sees it and shuts itself
down.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Coroutine
from urllib.parse import urlsplit, urlunsplit

from argus.console import console
from argus.core.config import Config

log = logging.getLogger(__name__)

# How long to wait for the server to start responding. The models and audio take the
# longest to come up, and on a cold start that is a few seconds; we take a large margin. The
# window is already on screen by then (see `run_desktop`), so the wait is not empty, but it
# must not last forever either.
READY_TIMEOUT_S = 30.0
# macOS: audio is opened in the pipeline before the server comes up, and on the first
# launch the system asks for microphone permission and waits for the person's answer, so
# opening the microphone is blocked until they click. Thirty seconds to read the question
# and answer is too little: the app would close by itself without waiting. Verified on real
# macOS: launched through Finder (`open`) with the prompt unanswered, it never reached the
# server at all.
MAC_READY_TIMEOUT_S = 180.0
READY_POLL_S = 0.02
# How long to wait for a single connection attempt to a port that is not listening yet; see
# `wait_until_serving`.
PROBE_TIMEOUT_S = 0.05

# How long to wait for the pipeline to stop after the window closes; see `run_desktop`.
STOP_JOIN_S = 8.0

# The window color before a page appears in it: it is the same as the splash screen's and
# the page's own (`--bg` in `home.html`, `index.html`, `admin.css`). The pywebview default
# is white, and the window would start with a white flash. This used to be `#14161a`, the
# color of the pages before the recolor of 2026-10-05; while the splash screen hung around
# for a fraction of a second nobody saw the difference, but now that it is on screen for a
# second, a switch to `#0a0c10` would be a visible jump.
BACKGROUND = "#0a0c10"

# How long a second launch waits for the first one to either show its window or finish. A
# closed window does not yet mean "the process is gone": stopping the pipeline takes time,
# and a repeated launch in that gap used to do nothing visible ("Argus doesn't open" until
# you click again). Now it waits instead of exiting silently.
TAKEOVER_WAIT_S = 25.0
TAKEOVER_POLL_S = 0.15
# After how long a second launch starts looking for the previous instance's hidden window as
# well.
HIDDEN_AFTER_S = 1.5


def splash_html(title: str) -> str:
    """The splash screen that the window shows while everything else comes up.

    The window appears right after launch, and the server later: while it does not answer, there
    is nothing to show in the window, and before, there was no window at all — the person clicked
    the shortcut again and again and saw emptiness. The splash screen is only the window color
    and a thin ring, with no captions or counters: there is nothing to read on it, it only needs
    to say "starting up". The ring does not appear at once (after 0.15 s), so that a fast start
    does not flash it for nothing.

    Everything is inside one string — no file, no request to a server that does not exist yet.
    """
    import html

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        f"html,body{{margin:0;height:100%;background:{BACKGROUND};overflow:hidden}}"
        "body{display:flex;align-items:center;justify-content:center}"
        "i{width:26px;height:26px;border-radius:50%;box-sizing:border-box;"
        "border:2px solid rgba(255,255,255,.10);border-top-color:rgba(255,255,255,.5);"
        "animation:r .9s linear infinite,f .4s .15s both}"
        "@keyframes r{to{transform:rotate(1turn)}}"
        "@keyframes f{from{opacity:0}to{opacity:1}}"
        "@media (prefers-reduced-motion:reduce){i{animation:f .4s .15s both}}"
        "</style></head><body><i></i></body></html>"
    )


def _declare_dpi_aware() -> None:
    """Declare the process per-monitor DPI aware — before the first window is created.

    Without this declaration Windows treats the process as DPI-unaware and, after the fact, hands
    it a picture scaled for the system scale — it stretches the finished window as a whole,
    including the system title bar with "minimize"/"close". At the usual window size this gets
    smeared and is barely visible; when the window is maximized, its physical size changes
    sharply, the stretched frame is recalculated and for a moment (or for good, until it is
    recreated) looks squeezed — exactly what is seen live. `GetDpiForWindow`, which
    `_resize_one` and its neighbors already use, does not help here: it reads the scale of an
    already existing window and declares nothing to the system about the process as a whole.

    Check this not through `python.exe` (in modern python.org builds the per-monitor awareness
    may be in its own manifest): the `argus.exe` packaged with PyInstaller carries no manifest of
    its own for this (`packaging/argus_app.py`), and in the built form there was nobody to
    declare it.

    The modern path is `SetProcessDpiAwarenessContext` (user32, Windows 10 version 1703+), the
    fallback is `SetProcessDpiAwareness` (shcore, Windows 8.1+). A refusal (a very old Windows,
    or the awareness was already declared by someone earlier) must not bring down the launch —
    the window will open as before, just without the declaration.
    """
    PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2):
            return
    except (AttributeError, OSError) as e:
        log.debug("SetProcessDpiAwarenessContext is unavailable: %s", e)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError) as e:
        log.debug("DPI awareness was not declared: %s", e)


def available() -> tuple[bool, str]:
    """Whether there is anything to show a window with. Needed by `doctor` and by the launch."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        import webview  # noqa: F401 — the import itself is what matters
    except ImportError as e:                     # pragma: no cover — the dependency is present
        return False, f"pywebview is not installed: {e}"
    if sys.platform == "win32":
        # Without the WebView2 component, pywebview does not fail but quietly falls back to the old
        # Internet Explorer engine: the app's pages do not work in it, while a "window exists"
        # check would say all is well.
        try:
            from webview.platforms import winforms

            if not winforms.is_chromium:
                return False, "the Microsoft Edge WebView2 component is missing — the window will open on the old engine and without pages"
        except Exception as e:                   # noqa: BLE001 — no .NET or pythonnet: there will be no window either
            return False, f"the window environment failed to load: {e}"
    try:
        return True, f"pywebview {version('pywebview')}"
    except PackageNotFoundError:                 # pragma: no cover — rare case
        return True, "pywebview"


# The window is visible on the monitor but does not appear in capture. Win10 2004+; on
# older builds the call succeeds but behaves like WDA_MONITOR: the window goes black both
# in capture and on screen. So we check the result instead of trusting it.
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def own_windows(hidden: bool = False) -> list[int]:
    """Top-level windows of this process.

    We find them by enumeration, not through pywebview's internals: it keeps the handle in
    different places depending on which engine was found in the system, and it silently becomes
    `None` when the version changes. Enumeration by process id does not depend on that.

    `hidden=True` — together with hidden ones (hidden by a key or by "closing" the main window),
    but only our own, by title: the process also has service invisible windows of .NET, IME and
    GDI+, which have no use for the capture mark, and a refusal on any of them would turn
    success into "some of the windows are visible". It is needed by the "Invisible in recordings"
    switch: a hidden window, returning by a key, would otherwise end
    up in the screen share unprotected.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.own_windows()

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    mine = ctypes.windll.kernel32.GetCurrentProcessId()
    found: list[int] = []
    unseen: list[int] = []

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == mine:
            if user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            elif hidden:
                unseen.append(hwnd)
        return True

    user32.EnumWindows(proc(visit), 0)
    if unseen:
        titles = _known_titles()
        found.extend(h for h in unseen if _quiet_title(h) in titles)
    return found


def _known_titles() -> set[str]:
    """Titles of the app windows: the main one and all the panels."""
    return {t for t in (_MAIN_TITLE["t"], *PANEL_TITLES.values()) if t}


def _quiet_title(hwnd: int) -> str:
    """A window title without sending the window a message — for enumerating the process's hidden
    windows.

    For a window of its own process, `GetWindowText` sends it `WM_GETTEXT` and waits for the
    answer. For a service window whose message queue is not being processed (a foreign library
    in our process, a thread busy starting up), the wait would be endless, and this enumeration
    sits on the path of the capture protection. `InternalGetWindowText` reads the title directly
    from the window and sends nothing.
    """
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.InternalGetWindowText(hwnd, buf, 256)
        return buf.value
    except (AttributeError, OSError):        # no such function — the usual path
        return _window_title(hwnd)


def hide_from_capture(timeout: float = 5.0) -> tuple[bool, str]:
    """Remove the process's windows from screen capture.

    It returns not "done" but what exactly came out, and that is the main thing here. A silent
    failure here is worse than the absence of the function: the person is sure the hints are not
    visible, and shows the screen.

    The window may not exist yet at the moment of the call — the engine creates it at its own
    pace — so we wait instead of trying once.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.hide_from_capture(timeout)
    if sys.platform != "win32":
        return False, "hiding the window works only on Windows"

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    deadline = time.monotonic() + timeout
    windows: list[int] = []
    while time.monotonic() < deadline:
        # Hidden windows too: when they come back via the key, they would otherwise appear in a
        # screen share without protection.
        windows = own_windows(hidden=True)
        if windows:
            break
        time.sleep(0.1)
    if not windows:
        return False, "window not found"

    hidden = 0
    for hwnd in windows:
        if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            continue
        # We verify instead of trusting: on older builds the call "succeeds" but a different flag
        # gets set, and the window goes black right on screen.
        got = wintypes.DWORD()
        asked = user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        if asked and got.value == WDA_EXCLUDEFROMCAPTURE:
            hidden += 1

    if hidden == len(windows):
        return True, f"window hidden from recordings ({hidden})"
    if hidden:
        return False, f"{hidden} of {len(windows)} windows hidden — some are visible"
    return False, "the system refused to hide the window (Windows 10 2004+ is required)"


# Normal visibility is what the window is born with. We restore it explicitly instead of
# relying on the default: the inverse of `hide_from_capture` must clear the flag just as
# reliably as that function sets it.
WDA_NONE = 0x00000000


def show_in_capture(timeout: float = 5.0) -> tuple[bool, str]:
    """Return the process's windows to screen capture — the reverse of `hide_from_capture`.

    The same check through `GetWindowDisplayAffinity` as there: the documentation guarantees the
    system clearing the flag no more strongly than its setting.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.show_in_capture(timeout)
    if sys.platform != "win32":
        return False, "hiding the window works only on Windows"

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    deadline = time.monotonic() + timeout
    windows: list[int] = []
    while time.monotonic() < deadline:
        windows = own_windows(hidden=True)      # hidden ones too: otherwise they would stay hidden
        if windows:
            break
        time.sleep(0.1)
    if not windows:
        return False, "window not found"

    shown = 0
    for hwnd in windows:
        if not user32.SetWindowDisplayAffinity(hwnd, WDA_NONE):
            continue
        got = wintypes.DWORD()
        asked = user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        if asked and got.value == WDA_NONE:
            shown += 1

    if shown == len(windows):
        return True, f"window visible in recordings again ({shown})"
    if shown:
        return False, f"{shown} of {len(windows)} windows are visible again — some are still hidden"
    return False, "couldn't make the window visible in recordings again"


def storage_dir() -> pathlib.Path:
    """Where WebView2 keeps its profile.

    It cannot go next to the program: the program may lie in Program Files, where writing
    without rights is not allowed, and then the window does not open at all. On macOS it is
    `~/Library/Application Support/Argus`, and writing inside the `.app` is not allowed at all.
    """
    if sys.platform == "darwin":
        from argus import host

        path = host.user_data_dir() / "webview"
        path.mkdir(parents=True, exist_ok=True)
        return path
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    path = pathlib.Path(base) / "Argus" / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
SW_SHOW = 5
SW_RESTORE = 9

# Handle of the named mutex, held by the process while it lives. There is no need to close
# it ourselves: Windows releases it on exit, and until then it serves as the "already
# running" marker for any following process.
_instance_mutex: int | None = None


def _instance_mutex_name(port: int) -> str:
    """Mutex name for a specific port: this is how both `run_desktop` and the test that simulates
    "already running" find it, without duplicating the format in two places."""
    return f"ArgusSingleInstance-{port}"


def _claim_single_instance(name: str) -> bool:
    """Take the named mutex — `True` if we took it first.

    Before, "already running" was checked with an HTTP request to our own port
    (`wait_until_serving` below, with a short timeout). Such a check has its own race: two
    processes started almost simultaneously — a double click, or the previous one has not yet
    released the port on closing — both see "the port is silent" and both try to bind. One takes
    the port, the other fails with `[Errno 10048]` — and fails not with a quiet "already running"
    but with an unhandled exception that nobody will see in an `.exe` built without a console:
    the shortcut simply "doesn't open", and this lives for years as an uncomprehended trifle.

    A Windows mutex is given to exactly one — atomically, with no gap between "checked" and
    "taken": there is no race here in principle, rather than one that has been won. (On macOS
    `flock` gives the same, see `mac/desktop.py`.)
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.claim_single_instance(name)
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        # "Access denied" is not a stray error but exactly "already running": the name is held by
        # an owner who has locked it against us (the previous instance was started as
        # administrator, and the mutex carries its rights). We must not consider ourselves first
        # here: a second pipeline would go for the port held by the previous one, and nobody would
        # bring up a window.
        #
        # Any other cause is not "already running" but a stray system error. We consider ourselves
        # first: risking a race is better than failing to open where the mutex could not be created
        # for an external reason (for example, a forbidden object name in a sandbox).
        return ctypes.GetLastError() != ERROR_ACCESS_DENIED
    already = ctypes.GetLastError() == ERROR_ALREADY_EXISTS
    global _instance_mutex
    _instance_mutex = handle
    return not already


WAIT_OBJECT_0 = 0x00
WAIT_ABANDONED = 0x80


def _acquire_when_free(timeout_s: float) -> bool:
    """Wait for the previous owner of the lock to finish and take it.

    The lock belongs to the thread that created it and is released when the process dies
    (Windows marks it abandoned, and the next waiter gets it — `WAIT_ABANDONED`). This is
    exactly what is needed: "the previous instance has fully exited" without polling the port
    and without guessing by windows.

    Called only after `_claim_single_instance`, which already has the lock handle — the very one
    that is now "somebody else's".
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.acquire_when_free(timeout_s)
    if _instance_mutex is None:
        # We have no lock (someone else's is closed to us, see `_claim_single_instance`), so there
        # is nothing to wait for, but we cannot return at once either: the caller would spin
        # through the window enumeration with no pause for all 25 seconds.
        time.sleep(timeout_s)
        return False
    # Our own instance of the library: argument types are set by hand, and the shared
    # `ctypes.windll.kernel32` must not share them, because the whole app uses it.
    wait = ctypes.WinDLL("kernel32").WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    return wait(_instance_mutex, int(timeout_s * 1000)) in (WAIT_OBJECT_0, WAIT_ABANDONED)


def _focus_existing_window(title: str, include_hidden: bool = False) -> bool:
    """Bring the window of the already running process to the foreground.

    Not `own_windows()` — it searches by the PID of **this** process, while the window we want
    belongs to another, already living one. The enumeration here is system-wide: any visible
    window with such a title, whichever process owns it.

    Before, a repeated click on the shortcut, having caught "already running", did nothing
    visible at all — no window, no message (in the built form without a console there is nowhere
    to show either). From the outside this is indistinguishable from "the shortcut is broken". A
    raised window at least answers with something on the screen.

    `include_hidden` — also look for the hidden window: the main window is hidden by a key
    combination (for the duration of screen sharing), and a person who cannot find it clicks the
    shortcut. Showing what they hid themselves is exactly what they are asking for. We just do
    not look for them on the first pass: a window that does not exist yet or no longer exists has
    no title, and the hidden window of a live instance may turn out to be someone else's window
    with the same name.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.focus_existing(title, include_hidden)
    user32 = ctypes.windll.user32
    from ctypes import wintypes

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def visit(hwnd: int, _: int) -> bool:
        # The title "Argus" can belong to someone else's window too, such as an "Argus" folder in
        # Explorer: without checking whose window it is, we would raise that one and the real one
        # would stay under the others.
        if (
            (include_hidden or user32.IsWindowVisible(hwnd))
            and _window_title(hwnd) == title
            and _is_same_program(hwnd)
        ):
            found.append(hwnd)
            return False  # found, so we stop searching
        return True

    user32.EnumWindows(proc(visit), 0)
    if not found:
        return False
    hwnd = found[0]
    if include_hidden and not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, SW_SHOW)
    # Restore only a minimized window: a maximized main window (which is the default) would
    # become a normal one on every second launch because of `SW_RESTORE`.
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def _is_same_program(hwnd: int) -> bool:
    """The window belongs to the same program as this process — by the executable's name.

    It is needed only for foreign windows with our title: the real Argus has the same file
    (`Argus.exe`, in development — the same interpreter). If the query fails (no rights to the
    other process), we do not discard the window: failing to raise the real window is worse than
    raising a doubtful one.
    """
    from ctypes import wintypes

    try:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        process = kernel32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return True
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
                return True
            theirs = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(process)
    except Exception:                          # noqa: BLE001 — could not ask, so we do not discard the window
        return True
    ours = os.path.basename(sys.executable).lower()
    accepted = {ours} | ({"python.exe", "pythonw.exe"} if ours.startswith("python") else set())
    return theirs in accepted


def ready_timeout() -> float:
    """How long to wait for the server: longer on macOS, since the first launch waits for an answer
    to the microphone prompt.
    """
    return MAC_READY_TIMEOUT_S if sys.platform == "darwin" else READY_TIMEOUT_S


# We reach our own server on the loopback directly, bypassing the system and environment
# proxy. By default `urllib` takes `HTTP_PROXY` and the Windows/macOS proxy and does not
# bypass 127.0.0.1 (even with `<local>` and `NO_PROXY=localhost`): with a dead proxy the
# port answers, but every request fails, so the launch waited for the server until the
# deadline and closed the window, and the hotkeys silently never arrived. We do not put
# `NO_PROXY` in the environment: any non-empty proxy set there makes `getproxies()` skip
# the system settings, and outbound requests (account, answers) would lose the person's
# proxy.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def loopback_open(target, timeout: float):
    """Open the address of our own server without a proxy. The response is the same as from
    `urllib.request.urlopen`.
    """
    return _LOOPBACK.open(target, timeout=timeout)


def wait_until_serving(
    url: str, timeout: float = READY_TIMEOUT_S, abort: threading.Event | None = None
) -> bool:
    """Wait until the server starts answering.

    A window opened before the server shows a connection error — and that is all: the user has
    no way to reload the page, the app has no address bar. In a browser this race was
    unnoticeable because the person pressed F5. So the real page is given to the window only
    after this answer, and before it the window shows the splash screen (`splash_html`).

    `abort` is an event after which there is no point in waiting any more: the pipeline crashed
    or the window was closed. Without it we would wait the whole term for a server that will no
    longer exist.

    First a cheap TCP probe with a short timeout, and only then the HTTP request. Before,
    `urlopen(timeout=1.0)` was polled, and Windows answered a connection to a not-yet-listening
    port not with a refusal but with silence: the attempt hung until its timeout (measured: a
    refusal comes after ~2 s, the timeout after a second), and a server that came up in the
    middle of an attempt was noticed late — on average a third of a second, up to 0.65 s. A
    50 ms probe notices it almost at once.
    """
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    address = (parts.hostname or "127.0.0.1", parts.port or 80)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if abort is not None and abort.is_set():
            return False
        try:
            with socket.create_connection(address, timeout=PROBE_TIMEOUT_S):
                pass
        except OSError:
            time.sleep(READY_POLL_S)
            continue
        # The port accepts connections; now make sure the server itself answers too.
        try:
            with loopback_open(url, timeout=1.0) as response:
                return response.status < 500
        except urllib.error.HTTPError:
            # It answered with a refusal, so it is alive. We need the fact of an answer, not a 200.
            return True
        except OSError:
            time.sleep(READY_POLL_S)
    return False


def _window_title(hwnd: int) -> str:
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.window_title(hwnd)
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
SWP_NOSIZE = 0x0001
SWP_FRAMECHANGED = 0x0020
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_CLOSE = 0x0010
SM_SHUTTINGDOWN = 0x2000      # `GetSystemMetrics`: the system is shutting down

# The page header color over the background (`--bg` plus the `--slab` underlay, sampled
# from a finished page): we paint the top frame band with it so that it blends with the
# strip and the header instead of being a third tone.
CAPTION_COLOR = "#101319"

# The main window's name: the page strip and the second launch both use it to find the
# window.
_MAIN_TITLE = {"t": ""}


def _colorref(color: str) -> int:
    """`#rrggbb` to COLORREF (0x00BBGGRR), as DWM expects it."""
    c = color.lstrip("#")
    return int(c[0:2], 16) | int(c[2:4], 16) << 8 | int(c[4:6], 16) << 16


def _main_hwnd(include_hidden: bool = False) -> int | None:
    if sys.platform != "win32" or not _MAIN_TITLE["t"]:
        return None
    for hwnd in own_windows(hidden=True) if include_hidden else own_windows():
        if _window_title(hwnd) == _MAIN_TITLE["t"]:
            return hwnd
    return None


def _set_frame_color(hwnd: int, colorref: int) -> None:
    dwm = ctypes.windll.dwmapi
    for attr in (DWMWA_CAPTION_COLOR, DWMWA_BORDER_COLOR):
        value = ctypes.c_uint(colorref)
        # Below Windows 11 the call is refused: the frame keeps the system color, and that is no
        # problem.
        dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))


def _fit_maximized(hwnd: int) -> None:
    """A maximized window without a system title bar must not cover the taskbar.

    For a window without `WS_CAPTION` Windows maximizes it to the whole monitor, over the
    taskbar. We need it as for an ordinary window: the work area plus an invisible border on each
    side (so the client area is exactly the work area). The bounds are set by the form's
    `MaximizedBounds` property; we compute them from the monitor where the window is now and
    update them on every request to maximize and on a state request.
    """
    window = _MAIN_WINDOW
    if window is None or sys.platform != "win32":
        return
    try:
        from webview.platforms.winforms import BrowserView

        form = BrowserView.instances.get(window.uid)
        if form is None:
            return
        user32 = ctypes.windll.user32
        info = _MonitorInfo(ctypes.sizeof(_MonitorInfo))
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
        dpi = user32.GetDpiForWindow(hwnd) or 96
        frame = user32.GetSystemMetricsForDpi(32, dpi) + user32.GetSystemMetricsForDpi(92, dpi)
        from System import Func, Type
        from System.Drawing import Rectangle

        bounds = Rectangle(
            info.work.left - info.monitor.left - frame,
            info.work.top - info.monitor.top - frame,
            info.work.right - info.work.left + 2 * frame,
            info.work.bottom - info.work.top + 2 * frame,
        )

        def _apply() -> None:
            form.MaximizedBounds = bounds

        if form.InvokeRequired:
            form.Invoke(Func[Type](_apply))
        else:
            _apply()
    except Exception as e:                       # noqa: BLE001 — pywebview internals may have changed
        log.debug("maximized window bounds were not set: %s", e)


# ---------- top frame: the client area starts at the very top ----------
#
# For a window without `WS_CAPTION` but with `WS_THICKFRAME`, Windows leaves a 7-pixel
# frame band at the top. It used to be painted in the page color; it is not there when the
# window is maximized, but it is visible in windowed mode: the highlight of the icon under
# the cursor and the red "close" button start below the window edge, with a gap on top, and
# the window's rounded corner falls on this band. The buttons must have no gaps around
# them. The band cannot be removed with a setting, because it is the non-client area: its
# size is decided by the window in response to `WM_NCCALCSIZE`.
#
# So we subclass the window on the fly (`comctl32.SetWindowSubclass`, the handler is
# `_flat_top_proc`): we compute the frame as usual and then give the client area back
# everything above one pixel from the top. A thin top edge remains (DWM draws it), and the
# page's strip starts right under it and fills the rounded corner itself. The left, right
# and bottom frames are untouched: resizing by them works as before; by the top edge,
# only by that thin edge. A maximized window is not touched at all: there Windows computes
# the frame itself from `MaximizedBounds` (`_fit_maximized`). The subclass is removed
# together with the strip itself (`restore_native_caption`).
WM_NCCALCSIZE = 0x0083
_FLAT_TOP_ID = 0x41524755       # "ARGU": any ID of our own for the subclass

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    _SUBCLASSPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, _wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM, ctypes.c_size_t, ctypes.c_size_t,
    )


class _NcCalcSizeParams(ctypes.Structure):
    _fields_ = [("rgrc", ctypes.c_long * 12), ("lppos", ctypes.c_void_p)]   # three RECTs in a row


# The subclass must live as long as its handler does, so we keep a reference to the
# handler here.
_flat_top_state: dict = {"proc": None, "hwnds": set()}


def _flat_top_proc(hwnd, msg, wparam, lparam, _uid, _ref):
    comctl32 = ctypes.windll.comctl32
    comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
    comctl32.DefSubclassProc.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    if msg == WM_NCCALCSIZE and wparam:
        try:
            params = ctypes.cast(lparam, ctypes.POINTER(_NcCalcSizeParams)).contents
            top = params.rgrc[1]            # rgrc[0].top: the top of the proposed window rectangle
            result = comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
            if not ctypes.windll.user32.IsZoomed(hwnd):
                params.rgrc[1] = top + 1    # the client area starts at the top of the window, without the band
            return result
        except Exception:                   # the window matters more than the frame: let the system compute it
            log.debug("window frame was not computed", exc_info=True)
    return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)


def _on_window_thread(hwnd: int, action: Callable[[], None]) -> None:
    """Run on the window thread: the system's window substitution may only be set from there."""
    window = _MAIN_WINDOW
    form = None
    if window is not None:
        try:
            from webview.platforms.winforms import BrowserView

            form = BrowserView.instances.get(window.uid)
        except Exception:  # noqa: BLE001 — pywebview may have changed: then simply go without the window thread
            form = None
    if form is not None and getattr(form, "InvokeRequired", False):
        from System import Func, Type

        form.Invoke(Func[Type](lambda: (action(), None)[1]))
    else:
        action()


def _flat_top(hwnd: int, on: bool) -> bool:
    """Turn the "client area from the very top" mode of a window on or off. Returns whether it
    worked.
    """
    if sys.platform != "win32" or os.environ.get("ARGUS_FLAT_TOP") == "0":
        return False     # `ARGUS_FLAT_TOP=0` is an emergency switch: the window keeps the system frame, as before
    comctl32 = ctypes.windll.comctl32
    user32 = ctypes.windll.user32
    result = {"ok": False}

    def apply() -> None:
        if on:
            if hwnd in _flat_top_state["hwnds"]:
                result["ok"] = True
                return
            if _flat_top_state["proc"] is None:
                _flat_top_state["proc"] = _SUBCLASSPROC(_flat_top_proc)
            ok = comctl32.SetWindowSubclass(hwnd, _flat_top_state["proc"], _FLAT_TOP_ID, 0)
            if ok:
                _flat_top_state["hwnds"].add(hwnd)
            result["ok"] = bool(ok)
        else:
            if hwnd not in _flat_top_state["hwnds"]:
                result["ok"] = True
                return
            comctl32.RemoveWindowSubclass(hwnd, _flat_top_state["proc"], _FLAT_TOP_ID)
            _flat_top_state["hwnds"].discard(hwnd)
            result["ok"] = True
        # The frame must be recalculated, otherwise the window lives with the old client area
        # until the first resize.
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            0x0002 | SWP_NOSIZE | 0x0004 | 0x0010 | SWP_FRAMECHANGED,   # NOMOVE|NOSIZE|NOZORDER|NOACTIVATE|FRAMECHANGED
        )

    try:
        _on_window_thread(hwnd, apply)
    except Exception:                       # the frame must not break startup
        log.debug("window top frame did not change", exc_info=True)
        return False
    return result["ok"]


def use_page_caption(title: str, color: str) -> bool:
    """Remove the system title bar from the main window: our own in the page instead.

    Requirement (2026-10-07): the bar with "minimize / restore / close" is too low. Windows does
    not allow changing the height and buttons of the system title bar, it can only be replaced.
    The window keeps its thick frame (`WS_THICKFRAME`), so resizing by the edge, Alt+Space and
    Win+arrows work as before; only `WS_CAPTION` is removed. The page draws its own bar at the
    top (`ui/static/winbar.js`) and controls the window through `main_window_action`.

    At the top the window keeps a 7-pixel band of frame, which DWM paints with the title color:
    we paint it with the page color (`color`), and it merges with the bar. Checked on a live
    Windows 11 window; in the maximized state there is no band.

    It returns whether it worked. It is called before the page loads: otherwise at startup the
    window with the system bar would jump up by its height.

    On macOS the same is `mac/desktop.py: use_page_caption`: the content starts from the top of
    the window, and the traffic lights on the left and the header buttons are drawn by the same
    `winbar.js`.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.use_page_caption()
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    done = False
    for hwnd in own_windows():
        if _window_title(hwnd) != title:
            continue
        style = ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_STYLE)).value
        wanted = (style & ~WS_CAPTION) | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        user32.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_int32(wanted).value)
        _set_frame_color(hwnd, _colorref(color))
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        _fit_maximized(hwnd)
        _flat_top(hwnd, True)
        done = True
    return done


def caption_removed(title: str) -> bool:
    """The main window has no system title bar (`WS_CAPTION` removed), so the page must draw it."""
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.page_caption_in_use()
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    for hwnd in own_windows():
        if _window_title(hwnd) != title:
            continue
        style = ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_STYLE)).value
        return (style & WS_CAPTION) != WS_CAPTION
    return False


def restore_native_caption(title: str) -> None:
    """Return the system title bar if the page that replaces it did not load.

    A window without a bar and without a page with buttons can be closed only with Alt+F4.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        mac.restore_native_caption()
        return
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    for hwnd in own_windows():
        if _window_title(hwnd) != title:
            continue
        style = ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_STYLE)).value
        _flat_top(hwnd, False)
        user32.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_int32(style | WS_CAPTION).value)
        _set_frame_color(hwnd, DWMWA_COLOR_DEFAULT)
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("monitor", _Rect), ("work", _Rect), ("flags", ctypes.c_ulong)]


# Where the window drag started: where the cursor was and where the window was. Each step
# computes the position from this pair instead of shifting by the difference from the
# previous step, so a late or lost request accumulates nothing.
_drag: dict = {}


def main_window_action(action: str) -> dict:
    """What the page's bar asks of the main window.

    `minimize`, `maximize`, `restore`, `toggle`, `close`, `state` and the pair
    `drag_start` / `drag_move` for dragging by the bar. We drag here, not by an offset from the
    page: the page sees the mouse in logical pixels, and the window lives in physical ones, and
    at a screen scale other than 100% the window would "run away" from the cursor. The cursor is
    taken from the system (`GetCursorPos`).

    On macOS the same is `mac/desktop.py: main_window_action`: the page draws the traffic lights
    on the left, and AppKit does the dragging from the mouse position.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.main_window_action(action)
    hwnd = _main_hwnd()
    if hwnd is None:
        return {"ok": False, "error": "there is no main window"}
    user32 = ctypes.windll.user32

    def state() -> dict:
        rect = _Rect()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {
            "ok": True,
            "maximized": bool(user32.IsZoomed(hwnd)),
            "minimized": bool(user32.IsIconic(hwnd)),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
        }

    if action in ("state", "maximize", "toggle"):
        _fit_maximized(hwnd)
    if action == "state":
        return state()
    if action == "minimize":
        if _main_no_taskbar["on"]:
            # Without a taskbar button a minimized window has nothing to grab it by, so we hide it,
            # like the hotkey does.
            hide_main_window()
        else:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
    elif action == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    elif action == "restore":
        user32.ShowWindow(hwnd, SW_RESTORE)
    elif action == "toggle":
        user32.ShowWindow(hwnd, SW_RESTORE if user32.IsZoomed(hwnd) else SW_MAXIMIZE)
    elif action == "close":
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"ok": True}
    elif action == "drag_start":
        cursor = _Point()
        user32.GetCursorPos(ctypes.byref(cursor))
        rect = _Rect()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        _drag["cursor"] = (cursor.x, cursor.y)
        _drag["origin"] = (rect.left, rect.top)
        # We do not touch a maximized window until the mouse has moved: a press on the strip (the
        # first of the two in a double click) must not restore it to a normal window.
        _drag["zoomed"] = bool(user32.IsZoomed(hwnd))
        return state()
    elif action == "drag_move":
        if "cursor" not in _drag:
            return {"ok": False}
        cursor = _Point()
        user32.GetCursorPos(ctypes.byref(cursor))
        dx = cursor.x - _drag["cursor"][0]
        dy = cursor.y - _drag["cursor"][1]
        if _drag["zoomed"]:
            if abs(dx) + abs(dy) < 5:
                return {"ok": True}
            # Like the system title bar: drag a maximized window and it becomes a normal one and hangs
            # from the cursor at the same fraction of its width.
            info = _MonitorInfo(ctypes.sizeof(_MonitorInfo))
            user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
            share = (_drag["cursor"][0] - info.work.left) / max(1, info.work.right - info.work.left)
            user32.ShowWindow(hwnd, SW_RESTORE)
            rect = _Rect()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            left = int(cursor.x - (rect.right - rect.left) * share)
            top = int(cursor.y - 20)
            user32.SetWindowPos(hwnd, 0, left, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            _drag["cursor"] = (cursor.x, cursor.y)
            _drag["origin"] = (left, top)
            _drag["zoomed"] = False
            return {"ok": True}
        user32.SetWindowPos(
            hwnd, 0, _drag["origin"][0] + dx, _drag["origin"][1] + dy, 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        return {"ok": True}
    else:
        return {"ok": False, "error": f"unknown action: {action}"}
    return state()


def _hide_one(hwnd: int) -> bool:
    """Hide one window from screen capture.

    Separate from the general `hide_from_capture`, which goes over all the process's windows: a
    detached window appears later than the others, and by the time of its birth the general pass
    happened long ago. It cannot be skipped — the window hangs over the screen exactly during
    sharing, which is why it is detached.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.hide_one(hwnd)
    user32 = ctypes.windll.user32
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return False
    got = ctypes.c_uint()
    return bool(
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        and got.value == WDA_EXCLUDEFROMCAPTURE
    )


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000


def _no_taskbar_button(hwnd: int) -> bool:
    """A window without a taskbar button and without a row in Alt+Tab.

    The taskbar is included in full-screen capture: an "invisible" panel with an "Argus" button
    next to it gives away both the app and the fact that it is open. A "tool" window
    (`WS_EX_TOOLWINDOW` without `WS_EX_APPWINDOW`) is never in the taskbar or the switcher; it
    can still be found by title, so the second launch works as before. We set it before the
    window is first shown — the taskbar does not pick up a style set on an already shown window
    at once. Precisely the window style, not `Form.ShowInTaskbar`: that recreates the window and
    resets the capture protection.

    Returns whether it took. Windows only: on macOS panels have no icon of their own.
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    style = ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value
    wanted = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    if wanted != style:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_int32(wanted).value)
    return bool(ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value & WS_EX_TOOLWINDOW)


def _taskbar_button_back(hwnd: int) -> bool:
    """The reverse of `_no_taskbar_button`: the window again with a taskbar button and a row in
    Alt+Tab.

    Returns whether it took (that is, `WS_EX_TOOLWINDOW` is cleared and `WS_EX_APPWINDOW` is
    set).
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    style = ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value
    wanted = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
    if wanted != style:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_int32(wanted).value)
    got = ctypes.c_uint32(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value
    return not got & WS_EX_TOOLWINDOW and bool(got & WS_EX_APPWINDOW)


SW_SHOWNA = 8

# The main window without a taskbar button (`ui.main_taskbar_button = false`). While it is
# so, "Minimize" hides the window instead of minimizing it: a minimized tool window would
# remain a small strip at the edge of the screen with no button to grab it by
# (`main_window_action`).
_main_no_taskbar = {"on": False}


def _main_taskbar_button(shown: bool) -> tuple[bool, str]:
    """Remove the main window's taskbar button and Alt+Tab row (`shown=False`) or bring them back.

    The same style as for the panels (`_no_taskbar_button`), but the window is already on screen:
    the taskbar does not pick up a style set on a shown window at once, so we hide the window for
    a moment and show it again, without activation (whatever was in the foreground stays there).
    The capture protection lives on the window itself and does not fall off because of this.
    Returns whether it took, and a note. Windows only.
    """
    if sys.platform != "win32":
        return False, "Windows only"
    hwnd = _main_hwnd(include_hidden=True)
    if hwnd is None:
        return False, "window not found"
    user32 = ctypes.windll.user32
    was_visible = bool(user32.IsWindowVisible(hwnd))
    was_front = user32.GetForegroundWindow() == hwnd
    if was_visible:
        user32.ShowWindow(hwnd, SW_HIDE)
    try:
        ok = _taskbar_button_back(hwnd) if shown else _no_taskbar_button(hwnd)
    finally:
        if was_visible:
            user32.ShowWindow(hwnd, SW_SHOWNA)
            if was_front:
                user32.SetForegroundWindow(hwnd)
    if ok:
        _main_no_taskbar["on"] = not shown
    if shown:
        return ok, "taskbar button restored" if ok else "the window style did not apply"
    return ok, "the window has no taskbar button" if ok else "the window style did not apply"


def set_main_taskbar_button(shown: bool) -> bool:
    """The switch from the settings: bring back or remove the main window's taskbar button.

    It works in its own thread and returns at once: the window is hidden and shown again, and
    calling this from the server loop would mean waiting for the windows thread. `False` — there
    is no app window (browser, macOS), nothing to do.
    """
    if _MAIN_WINDOW is None or sys.platform != "win32":
        return False

    def work() -> None:
        try:
            ok, detail = _main_taskbar_button(shown)
        except Exception:                        # noqa: BLE001 — the window matters more than the switch
            log.warning("main window taskbar button was not toggled", exc_info=True)
            return
        if not ok:
            log.warning("main window taskbar button was not toggled: %s", detail)

    threading.Thread(target=work, name="argus-taskbar-button", daemon=True).start()
    return True


def _form_hwnd(window, timeout: float = 1.0) -> int | None:
    """The window handle from the pywebview form itself — it exists while the window is not yet
    shown.

    Enumerating the process's windows (`own_windows`) sees only shown ones, and the glass panel
    is shown only when the page starts loading — the capture protection by enumeration would have
    to be set on an already visible window. The form has the handle from birth. `None` — there is
    no form (not Windows, a stand-in window in a test, pywebview details changed): then we search
    by title, as before.
    """
    if sys.platform != "win32":
        return None
    try:
        from webview.platforms.winforms import BrowserView

        uid = window.uid
        deadline = time.monotonic() + timeout
        form = BrowserView.instances.get(uid)
        while (form is None or not form.IsHandleCreated) and time.monotonic() < deadline:
            time.sleep(0.01)
            form = BrowserView.instances.get(uid)
        if form is None or not form.IsHandleCreated:
            return None
        return int(form.Handle.ToInt64())
    except Exception as e:                       # noqa: BLE001 — pywebview internals may have changed
        log.debug("could not get the window handle from the form: %s", e)
        return None


def _panel_overlay(cfg, on: bool) -> None:
    """macOS, `ui.mac_overlay = "auto"`: the app is an "agent" while the panel is open.

    Only this way does the panel stand above another app's full-screen window (a call, a
    presentation): checked on a real macOS 26 — without it the panel is not visible behind a
    full-screen Safari. The price for this is the Dock icon: while the panel is open it is
    absent, and when the last one is closed it returns. The other mode settings — `always` and
    `off` — are not managed by the panels.
    """
    if sys.platform != "darwin" or getattr(cfg.ui, "mac_overlay", "off") != "auto":
        return
    from argus.mac import desktop as mac

    mac.set_overlay_mode(on)


SW_SHOWNOACTIVATE = 4


def _show_panel(window, hwnd: int | None = None) -> None:
    """Bring a hidden panel back to the screen.

    On macOS not `window.show()`: it also makes the app active, and that pulls the person out of
    someone else's full-screen window (a call, a presentation) to the Argus desktop — exactly at
    the moment when the panel is returned to look at it over the call. There the window is simply
    brought to the front.

    On Windows also not `window.show()` when the handle is known: it calls `Activate`, and the
    keyboard (together with the focus of the other window where the person is typing right now)
    would go to the panel. We show it without activation, the way `_resize_one` does not take
    focus.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        if mac.show_panel(window):
            return
    elif sys.platform == "win32" and hwnd:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        if user32.IsWindowVisible(hwnd):
            return
    window.show()


SW_HIDE = 0


def _hide_native(hwnd: int | None) -> None:
    """The panel is still on screen after `window.hide()` — we hide it directly.

    `_show_panel` returns the panel with a bare `ShowWindow`, not by the form's means. If the
    form did not notice this and considers itself hidden, its `Hide` will do nothing — and the
    panel is hidden by "panic", where a miss is unacceptable. When all is well, the window is
    already not visible after `hide()`, and this call does nothing.
    """
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        if user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, SW_HIDE)
    except Exception as e:                       # noqa: BLE001 — the window may already be gone
        log.debug("panel did not hide directly: %s", e)


DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def _resize_one(hwnd: int, width: int, height: int) -> bool:
    """Resize a window without showing or activating it.

    By our own hands, not pywebview's `window.resize()`: that calls `SetWindowPos` with the
    `SWP_SHOWWINDOW` flag and without `SWP_NOACTIVATE` — that is, it also **shows** the window
    and takes focus. For the panel this is no trifle: a panel hidden by "panic" would pop back
    over the screen from a single change of the frame shape — exactly during the screen share for
    which it was hidden. And the focus theft would hit whoever is typing at that moment.

    The size comes in logical pixels, as the page sees them, while the window lives in physical
    ones. We convert by the scale of the window itself — the same way pywebview does it,
    otherwise on a 125% screen the window will not be the size that was asked for.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.resize_one(hwnd, width, height)    # points, not pixels: the system does the scaling
    user32 = ctypes.windll.user32
    dpi = user32.GetDpiForWindow(hwnd) or 96
    return bool(
        user32.SetWindowPos(
            hwnd, 0, 0, 0,
            int(width * dpi / 96), int(height * dpi / 96),
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    )


def _set_min_size(window, min_width: int, min_height: int) -> bool:
    """Raise the lower size limit of an already open window through `Form.MinimumSize`.

    We also tried this with our own WndProc that intercepts `WM_GETMINMAXINFO`: it did
    hold the limit, but on a live window it also broke dragging the whole window, the same
    symptom as with `resizable=False` before. It looks like both paths in a pywebview
    frameless window depend on one and the same thing, so we must not replace the WndProc
    ourselves, neither for the limit nor for anything else.

    Here there is no such replacement at all: `Form.MinimumSize` is a real WinForms
    property, and on a live window WinForms itself answers `WM_GETMINMAXINFO` by looking at
    exactly that property, the same path pywebview uses when it sets its own `min_size=`
    parameter at window creation (`BrowserView.BrowserForm`). It is enough to raise the same
    property on an already open window: the limit then holds through the same proven path,
    not through our own interception.

    We reach the Form through `window.uid`: the pywebview `Window` has no form object of its
    own, only an identifier, by which the WinForms backend stores the real forms in
    `BrowserView.instances`.

    The path relies on pywebview internals, not on its public API, so it may change in a
    future version. A failure here must not break opening the window or the resize that
    called it: the same logic as the neighbouring `_round_corners` (that is also an optional
    decoration, not something the window needs in order to work).

    This is not only called from the main thread: the "show screen" arrow in the "all at
    once" window sends its request over the network, and the server runs it in
    `asyncio.to_thread`, that is, in a background pool thread. `Form.MinimumSize` is a
    managed WinForms property, not a bare Win32 call like `SetWindowPos` (that is how it
    differs from the neighbouring `_resize_one`, which a foreign thread does not bother):
    pywebview itself, in that same file, wraps any other form change in `Invoke` when it is
    called from a thread other than the form's own (`self.InvokeRequired`), and without that
    a change from outside silently corrupts the form's internal state. It does not raise an
    exception; it is exactly the "everything breaks" that showed up in the reports.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.set_min_size(window, min_width, min_height)
    try:
        from webview.platforms.winforms import BrowserView

        form = BrowserView.instances.get(window.uid)
        if form is None:
            return False
        from System import Func, Type
        from System.Drawing import Size

        def _apply() -> None:
            scale = form._scale
            form.MinimumSize = Size(int(min_width * scale), int(min_height * scale))

        if form.InvokeRequired:
            form.Invoke(Func[Type](_apply))
        else:
            _apply()
        return True
    except Exception as e:            # noqa: BLE001 — pywebview internals may have changed
        log.debug("could not raise the lower window limit: %s", e)
        return False


def _set_max_size(window, max_width: int, max_height: int) -> bool:
    """The upper limit goes through the same path as `_set_min_size`, using `Form.MaximumSize`.

    `webview.create_window` has no parameter for this (only `min_size=`), so the upper limit
    is set here, not at window creation, right after the window appears (`Panels.open`).

    A failure must not break opening the window: the same caveat as for `_set_min_size` next
    to it. The same marshalling through `Invoke` as there, and for the same reason:
    `Panels.open()`, which calls this, also runs in `asyncio.to_thread`, not on the window's
    thread.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.set_max_size(window, max_width, max_height)
    try:
        from webview.platforms.winforms import BrowserView

        form = BrowserView.instances.get(window.uid)
        if form is None:
            return False
        from System import Func, Type
        from System.Drawing import Size

        def _apply() -> None:
            scale = form._scale
            form.MaximumSize = Size(int(max_width * scale), int(max_height * scale))

        if form.InvokeRequired:
            form.Invoke(Func[Type](_apply))
        else:
            _apply()
        return True
    except Exception as e:            # noqa: BLE001 — pywebview internals may have changed
        log.debug("could not raise the upper window limit: %s", e)
        return False


def _round_corners(hwnd: int) -> bool:
    """Round the corners of a frameless window.

    Done through DWM, not CSS: `border-radius` rounds the drawing inside the window, but the
    window itself stays rectangular, so background paint would remain at the corners, and
    that would look worse than square corners. DWM clips the window itself, with
    antialiasing.

    We use a large radius: it matches the `border-radius:9px` of the dashboard panels, and a
    panel window is the same panel, just detached.

    Windows 11 can do this (build 22000 and up); on Windows 10 the call returns a failure
    and the corners stay square. That is not a malfunction; it is enough to note it in the
    log, since there is nothing to show the user.

    On macOS the system rounds the corners itself, but the panel there gets what the system
    does not provide on its own: visibility in all spaces and above other apps' fullscreen
    windows (`mac/desktop.py: finish_panel`).
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.finish_panel(hwnd)
    pref = ctypes.c_int(DWMWCP_ROUND)
    hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
    )
    if hr:
        log.debug("corners were not rounded (Windows 11 required): hr=0x%08x", hr & 0xFFFFFFFF)
    return hr == 0


DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
# "No material": on a window with extended glass this is neither black nor gray but real
# transparency; the desktop shows through the background as it is, sharp. Acrylic (3) was
# the first thing tried here: it blurs and lightens the background, and over a dark window
# this looks like a dull gray plate, not glass.
DWMSBT_NONE = 1
# The window backdrop type appeared in Windows 11 22H2. Below that the call fails, and a
# window that has already been made transparent would stay gray, so on older builds we do
# not take the glass branch at all (`glass_supported`).
GLASS_MIN_BUILD = 22621
PANEL_BACKGROUND = "#1c1f25"


class _Margins(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int), ("right", ctypes.c_int),
        ("top", ctypes.c_int), ("bottom", ctypes.c_int),
    ]


def glass_supported() -> bool:
    """Whether this Windows supports a transparent window background: Windows 11 22H2+."""
    if sys.platform == "darwin":
        return True          # macOS 14 or newer: every supported version can do a transparent window
    if sys.platform != "win32":
        return False
    return getattr(sys, "getwindowsversion")().build >= GLASS_MIN_BUILD


def _keep_glass(*_args) -> None:
    """An empty update of the form theme: see `_glass`; it lets the glass survive changes to the
    system settings.
    """


def _glass(window, timeout: float = 5.0) -> bool:
    """Make the window background transparent: whatever lies beneath the window shows through.

    This is the only way that worked here. The window extends the DWM glass over the whole
    client area and asks for the "no material" backdrop type (`DWMSBT_NONE`); the form is
    painted black (for DWM, black on a layer without alpha is exactly "glass"); and the
    WebView2 background is transparent from birth (`transparent=True` in `create_window`:
    setting `DefaultBackgroundColor` later is too late, the page has already been drawn on an
    opaque one). The desktop under the window shows as it is, with no blur and no tint; text
    and images stay sharp, only the background is transparent.

    We tried other approaches too, and they do not work: an empty region in
    `DwmEnableBlurBehindWindow` (as in Tauri) with a black form gives a solid, dull #121212,
    and a color key gives solid black (see the comment at `create_window`). Acrylic worked,
    but it blurs and lightens the background: over a dark window you get a gray plate, not
    glass.

    The window's dark theme is always turned on, in case the system theme is light: the frame
    and the small details that DWM draws must not stand out from the dark interface.

    Called before the window is shown: in the transparent branch pywebview hides the form and
    shows it when the page starts loading, and if we painted later, a gray #F0F0F0 would
    flash in the first frame. The form already exists at that point, but the window handle is
    not yet visible from outside (`own_windows` takes visible ones), so we take it from the
    form itself.

    If the call fails, we give the form its previous panel color back, so the window will be
    opaque, as before, not gray and not black.

    On macOS `mac/desktop.py: make_glass` does the same thing, but after the window is
    created: the transparent background of the window and of the WKWebView can also be
    switched later, without flicker.
    """
    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        return mac.make_glass(window, timeout)
    try:
        from webview.platforms.winforms import BrowserView

        deadline = time.monotonic() + timeout
        form = BrowserView.instances.get(window.uid)
        while (form is None or not form.IsHandleCreated) and time.monotonic() < deadline:
            time.sleep(0.02)
            form = BrowserView.instances.get(window.uid)
        if form is None or not form.IsHandleCreated:
            return False
        from System import Func, Type
        from System.Drawing import Color, ColorTranslator

        dwm = ctypes.windll.dwmapi
        ok = [False]

        def _apply() -> None:
            hwnd = int(form.Handle.ToInt64())
            form.BackColor = Color.Black
            margins = _Margins(-1, -1, -1, -1)
            good = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) == 0
            for attr, value in (
                (DWMWA_USE_IMMERSIVE_DARK_MODE, 1),
                (DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE),
            ):
                v = ctypes.c_int(value)
                set_ok = dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0
                good = good and set_ok
            if not good:
                form.BackColor = ColorTranslator.FromHtml(PANEL_BACKGROUND)
            else:
                # On every change of system settings (theme, accent, wallpaper) pywebview sets the
                # backdrop type again (Mica for the dark theme), and the panel's glass would be lost until
                # the window is reopened. We replace its theme update with an empty one: the window's dark
                # theme and background are already set the way we need.
                form.update_title_bar_theme = _keep_glass
            ok[0] = good

        if form.InvokeRequired:
            form.Invoke(Func[Type](_apply))
        else:
            _apply()
        return ok[0]
    except Exception as e:            # noqa: BLE001 — pywebview internals may have changed
        log.debug("glass background was not enabled: %s", e)
        return False


class Panels:
    """Detached panels: one window per panel.

    They outlive the main window, and that is their key property. `webview.start()` returns
    when the **last** window is closed, so after closing the main window the person is left
    with the panels on screen, and the pipeline keeps running. That is what all of this is
    for: in an interview you need not a dashboard but two lines on top of someone else's
    window.
    """

    def __init__(self, url: str, cfg) -> None:
        self.url = url
        self.cfg = cfg
        self.windows: dict[str, object] = {}
        self.hwnds: dict[str, int] = {}
        # Whether the panels are hidden as a whole. We keep the state ourselves instead of asking
        # the windows: for a hidden window `pywebview` does not answer this question the same way
        # on all engines, and "hide" and "bring back" are one key.
        self.hidden = False
        # The size that was requested while the panels were hidden. We apply it when we bring them
        # back: the request itself comes from the page, which keeps living inside the hidden window
        # and keeps seeing new frames.
        self.pending_size: dict[str, tuple[int, int]] = {}
        # The own, raised width floor of individual panels. Right now only the "all at once" window
        # with the screen panel open has one (`set_min_width`). `None` is not stored here: the
        # absence of a key means "no own floor, the common one from `WINDOW_BOX` applies".
        self._min_widths: dict[str, int] = {}
        # The timestamp of the last applied size request, per panel. `/window/resize` is called
        # independently by the corner handle, the "show screen" arrow and the fitting of the screen
        # window to the frame shape (`fitScreenWindow` in index.html), and each call goes through
        # `asyncio.to_thread` on the server's shared thread pool, that is, into its own thread,
        # which is not obliged to finish in the order in which the request was sent. Without this
        # timestamp an older request, overtaken at completion by a newer one, would quietly
        # overwrite its result: `resize` below checks the timestamp under the lock and does not
        # let a request older than one already applied touch the window. It is not an error, just a
        # lost race, silently.
        self._resize_seq: dict[str, float] = {}
        # Its own lock for this, not the shared `self._lock` of `open()`: that one is held while a
        # window is being drawn in full (seconds, because we wait for the handle by title), and
        # sharing it with resize would block dragging by the handle for all that time. Here only the
        # timestamp check and `SetWindowPos` itself are under the lock, microseconds.
        self._resize_lock = threading.Lock()
        # Whether the event loop that can draw windows is up.
        self.ready = threading.Event()
        self._lock = threading.Lock()

    def open(self, name: str) -> None:
        """Open a panel window and bring it to readiness.

        The lock is held to the end, including the wait for the handle: until the window is
        found and recorded, a neighbouring request for the same panel must not decide that it
        does not exist yet and open a second one.
        """
        import webview

        # Windows are drawn by the WebView event loop, and it comes up later than the server. A
        # request that landed in this gap used to create a window object that never appeared on
        # screen, while the page received "ready".
        if not self.ready.wait(PANEL_READY_S):
            raise RuntimeError("the app is still starting — try again")

        with self._lock:
            # A request to open a panel is a request to see it, and "hidden" is one flag for all
            # windows. A panel shown while it is set would leave the flag lying: the "hide" key would
            # show the others, and the size would go into the queue and never be applied. So we first
            # bring back the hidden ones.
            if self.hidden:
                self.show_all()
            existing = self.windows.get(name)
            if existing is not None:
                # Already open: we do not spawn a second window but raise this one. Two windows of the
                # same panel would show the same thing and be confusing precisely because they stand in
                # different places.
                try:
                    _show_panel(existing, self.hwnds.get(name))
                    return
                except Exception:  # noqa: BLE001 — the window may have been closed with its close button
                    self.windows.pop(name, None)
                    self.hwnds.pop(name, None)

            title = PANEL_TITLES[name]
            width, height = PANEL_SIZE.get(name, DEFAULT_PANEL_SIZE)
            # Before the window, not after: verified on real macOS 26 (2026-10-13). A window born by a
            # regular app and only then turned into an "agent" does not get above another app's
            # fullscreen window; one born by an agent does.
            _panel_overlay(self.cfg, True)
            from argus.ui.server import WINDOW_BOX

            min_w, min_h, max_w, max_h = WINDOW_BOX
            # Glass only where the system can do it. The page learns about it from the address
            # (`?glass=1`), not from the window: the window is born before the page has time to ask.
            glass = self.cfg.ui.panel_glass and glass_supported()
            exclude = bool(self.cfg.ui.exclude_from_capture)
            # Windows: the window is born hidden, and capture protection is applied before it is first
            # shown. Otherwise it is an ordinary window that a screen share could capture for the whole
            # time (tens or hundreds of milliseconds) that `open` spent waiting for the title, changing
            # sizes and rounding corners, with the protection coming as the last step.
            #
            # `hidden` applies only to an opaque window. pywebview hides a glass window right after
            # birth anyway (it shows the transparent form only when page loading starts), and `hidden`
            # would add a step with the form's opacity (`Opacity` 0 → 1: WinForms may recreate the
            # window). The look of the "all at once" window rests on the glass, so its birth must not
            # be changed without testing on a live window. For a glass window the protection is applied
            # below as the same first step, using the form's handle.
            early = sys.platform == "win32"
            window = webview.create_window(
                title,
                f"{self.url}/panel/{name}" + ("?glass=1" if glass else ""),
                width=width, height=height, frameless=True, on_top=True,
                # `resizable=False` was here and was put back: it was tried as a way to close off dragging
                # by the invisible edge, bypassing `.resize-grip` (and `clamp_window_size` along with it),
                # and it broke dragging the whole window. Our own WndProc answering `WM_GETMINMAXINFO` was
                # tried next: it held the limit but broke dragging in exactly the same way. The lower limit
                # is now held by the real `min_size=` below (WinForms itself answers the same message with
                # it, see `_set_min_size`), and that path does not touch dragging at all.
                resizable=True,
                min_size=(min_w, min_h),
                # No shadow. It is drawn around the window and gives away its borders exactly where the
                # window should be inconspicuous: a panel on top of work should be seen as a rim, not as
                # content.
                shadow=False,
                # `transparent=True` only together with `_glass` below: by itself pywebview in this branch
                # does not paint the WinForms form, and through the transparent WebView2 you see its system
                # gray #F0F0F0. A layer with a common alpha, an empty region in `DwmEnableBlurBehindWindow`
                # and a color key (on the form's gray and on the page's paint) do not fix this: with a key
                # the window becomes solid black, because the WebView2 content is drawn by DirectComposition
                # and the key does not reach it. What works is the extended DWM glass, `_glass`.
                #
                # Without glass the window is opaque and is painted at once, before the page loads,
                # otherwise white flashes in the first frame. There is no longer a common window alpha (a
                # transparency slider on the strip): the window is what the page paints it.
                #
                # On macOS the window is born ordinary and becomes transparent right afterwards (`_glass`
                # below): pywebview's `transparent=True` there relies on a WKWebView key that does not
                # exist in some versions of the system, and then the window does not appear at all.
                transparent=glass and sys.platform != "darwin",
                background_color=PANEL_BACKGROUND,
                # Without `easy_drag`: with it the window moves from a press at any point, including the
                # strip's buttons, which then cannot be pressed without moving the window. Dragging is
                # allowed only by the strip at the top, which is marked on the page with the class
                # `pywebview-drag-region`.
                easy_drag=False,
                # The pywebview default is `False`, which disables text selection in the whole window on
                # top of any page CSS (`user-select` in the markup does not override this switch: it is a
                # separate window parameter of its own, not part of the CSS cascade). Without it no
                # `user-select:text` in `index.html` would make sense.
                text_select=True,
                hidden=early and not glass,
            )
            self.windows[name] = window
            # We take the handle from the form itself, not by enumerating visible windows: the glass
            # panel is shown only when page loading starts, and enumeration would find it already on
            # screen.
            hwnd = _form_hwnd(window) if early else None
            if hwnd is not None:
                # First thing, while the window is hidden and has never been shown. If the protection did
                # not apply, there is no window: a panel that is visible in a screen share while the person
                # is sure it is hidden is worse than none (as is the case with no handle, below).
                if exclude and not _hide_one(hwnd):
                    self._abandon(
                        name, window, "couldn't hide the panel from recordings — the window was closed"
                    )
                # The taskbar is part of a full-screen capture: an "Argus" button would give the panel
                # away.
                if exclude and not _no_taskbar_button(hwnd):
                    log.warning("panel '%s': taskbar button remained", name)
                self._keep_hidden_when_asked(window)
            window.events.closed += lambda: self._forget(name)
            if glass and not _glass(window):
                log.warning("panel '%s': glass background was not enabled, window is opaque", name)
            if early and not glass:
                # A glass window shows itself when the page starts loading; an ordinary one, born hidden,
                # is shown by us, now that the protection is already in place. If the form's handle was not
                # found (pywebview internals changed), we show it anyway, and the protection will be
                # applied right after the window is found below: worse than before showing, but better than
                # an invisible window.
                if hwnd is None:
                    log.warning("panel '%s': form not found - protection will apply to the already shown window", name)
                try:
                    _show_panel(window, hwnd)
                except Exception as e:           # noqa: BLE001 — the window may already be gone
                    log.debug("panel window '%s' did not show: %s", name, e)
                    self._abandon(name, window, "the panel window didn't appear — try again")

            # The window does not appear instantly: WebView draws it in its own thread.
            found = self._await_titled(title)
            if found is None:
                # A window without a handle does not obey us: we can neither hide it from capture nor make
                # it transparent. That is worse than not opening it at all: the panel would be visible in a
                # screen share while the person is sure it is not. So we remove it and say so plainly.
                self._abandon(name, window, "the panel window didn't appear — try again")
            if hwnd is None:
                # Fallback path (macOS, a stand-in form): the window is already on screen, so we apply the
                # protection at once, before sizes, rounding and limits, instead of as the last step.
                hwnd = found
                if exclude and not _hide_one(hwnd):
                    self._abandon(
                        name, window, "couldn't hide the panel from recordings — the window was closed"
                    )

            self.hwnds[name] = hwnd
            # `webview.create_window(width=…, height=…)` in this setup does not give what was asked
            # for: the form gets its size BEFORE its border is removed (`FormBorderStyle = None` in
            # pywebview comes as the second step), and the window comes out smaller than requested by
            # a fixed border, measured live; the difference does not depend on scale and is the same
            # at 460×340 as at 460×259. `_resize_one` computes the physical size itself, the same way
            # as `/window/resize`, and we use it to correct the size right after creation, instead of
            # waiting for `fitScreenWindow` (for the screen panel) to notice it and ask over the
            # network.
            _resize_one(hwnd, width, height)
            _round_corners(hwnd)
            # The lower limit is already held by `min_size=` in `create_window` above. pywebview has no
            # parameter of its own for the upper one, so we set it by the same path
            # (`Form.MaximumSize`), but separately and only now, when the window is surely fully drawn
            # and found in `BrowserView.instances`.
            _set_max_size(window, max_w, max_h)

    def _keep_hidden_when_asked(self, window) -> None:
        """A panel hidden with the hotkey must not show itself again.

        pywebview shows the glass window on every navigation (`on_navigation_start`: `Show` and
        `Activate`), and the page reloads (changing the language in the dashboard reloads all
        open windows), so a panel hidden by the "panic" key would pop up in the middle of a
        screen share while the flag says "hidden". So the form gets its own visibility guard:
        while `hidden` is set, a form that gets shown is hidden again at once (capture protection
        stays on the window, so no frame lands in the recording). If the guard cannot be set up
        (pywebview internals), the panel behaves as before.
        """
        try:
            from webview.platforms.winforms import BrowserView

            form = BrowserView.instances.get(window.uid)
            if form is None:
                return

            def on_visible(sender, args) -> None:
                try:
                    if self.hidden and form.Visible:
                        form.Hide()
                except Exception as e:           # noqa: BLE001 — an error has no place in a .NET event handler
                    log.debug("panel visibility watchdog: %s", e)

            form.VisibleChanged += on_visible
        except Exception as e:                   # noqa: BLE001 — pywebview internals may have changed
            log.debug("panel visibility watchdog did not start: %s", e)

    def _abandon(self, name: str, window, why: str) -> None:
        """Remove a window that cannot be trusted, and report it with an error that the page will
        see.

        Without a handle, or with capture protection not accepted, the panel would be visible in
        a screen share while the person is sure it is not: closing it is better than leaving it
        open with just an entry in the log.
        """
        self.windows.pop(name, None)
        self.hwnds.pop(name, None)
        try:
            window.destroy()
        except Exception as e:                   # noqa: BLE001 — the window may not exist
            log.debug("panel window '%s' did not close: %s", name, e)
        if not self.windows:
            _panel_overlay(self.cfg, False)      # no panel left, so we bring back the Dock icon
        raise RuntimeError(why)

    def close(self, name: str) -> None:
        """Close a detached panel.

        Through Python, not through `window.close()` on the page: the window is frameless and has
        no system close button, and if the browser-side close did not work (and on the embedded
        engine it does not always work), the panel would stay on screen forever, together with
        the process that holds on to the last window.
        """
        window = self.windows.get(name)
        if window is None:
            raise RuntimeError(f"panel window '{name}' is not open")
        window.destroy()
        self._forget(name)

    def close_all(self) -> None:
        for name, window in list(self.windows.items()):
            try:
                window.destroy()
            except Exception as e:               # noqa: BLE001 — the window may have been closed by hand
                log.debug("panel '%s' is already closed: %s", name, e)
        self.windows.clear()
        self.hwnds.clear()
        _panel_overlay(self.cfg, False)

    def hide_all(self) -> None:
        """Take the panels off the screen without closing them.

        Hide, not close: a closed panel takes seconds to come back up, because WebView2 draws the
        page again and reconnects to the websocket. For the "panic" key that is an eternity, and
        the hidden panels must come back exactly as fast as they were removed.
        """
        self.hidden = True
        for name, window in list(self.windows.items()):
            try:
                window.hide()
            except Exception as e:               # noqa: BLE001 — the window may have been closed by hand
                log.debug("panel '%s' did not hide: %s", name, e)
            _hide_native(self.hwnds.get(name))

    def show_all(self) -> None:
        self.hidden = False
        exclude = bool(self.cfg.ui.exclude_from_capture)
        for name, window in list(self.windows.items()):
            hwnd = self.hwnds.get(name)
            if exclude and hwnd is not None and not _hide_one(hwnd):
                # We apply the protection again before every show: the switch in the settings could have
                # been turned on while the panel was hidden, and a hidden window did not respond. If it did
                # not apply, the window stays hidden: on a shared screen it would be visible while the
                # person is sure it is not.
                log.error("could not hide panel '%s' from recording - not showing it", name)
                self.hidden = True
                continue
            try:
                _show_panel(window, hwnd)
            except Exception as e:               # noqa: BLE001
                log.debug("panel '%s' did not come back: %s", name, e)
        # The sizes that were requested while the panels were hidden. Now it is allowed.
        for name, (width, height) in self.pending_size.items():
            hwnd = self.hwnds.get(name)
            if hwnd is not None:
                _resize_one(hwnd, width, height)
        self.pending_size.clear()

    def toggle(self) -> bool:
        """Hide or bring back. True means it is now hidden."""
        if self.hidden:
            self.show_all()
        else:
            self.hide_all()
        return self.hidden

    def scroll(self, name: str, delta: int, target: str | None = None) -> None:
        """Scroll the panel's content without touching it with the mouse.

        The mouse is the one thing this channel cannot use: its movement is visible in a screen
        share. So a long answer is scrolled from the keyboard, and the panel page knows nothing
        about it: we scroll its body from outside, the same way a person would with the wheel.

        `target` is the section to scroll if it does not match the window's own name. Only one
        window needs this, the "all at once" window: it is not the "chat" panel but both at once
        (chat+screen), and without an explicit target it would scroll whatever
        `document.scrollingElement` happens to hit first, that is, nothing (the page as a whole
        does not scroll; the body of each section inside it does).
        """
        window = self.windows.get(name)
        if window is None:
            return
        try:
            # We aim at the body of the right panel by name, not at the first `.body` on the page. The
            # window's markup is the same as the dashboard's, with both panels in it, one of them just
            # hidden.
            what = target or name
            window.evaluate_js(
                f"(document.querySelector('#p-{what} .body')||document.scrollingElement)"
                f".scrollBy({{top:{int(delta)},behavior:'smooth'}})"
            )
        except Exception as e:                   # noqa: BLE001 — the window may have been closed
            log.debug("panel '%s' did not scroll: %s", name, e)

    def resize(self, name: str, width: int, height: int, seq: float | None = None) -> bool:
        """Resize a detached window.

        Only one window needs this: the screen window. The frame fills it entirely, and whatever
        does not fit is cut off; there is nothing to cut off exactly when the window's shape
        matches the frame's shape. Fitting the window to the frame is easier than fitting the
        frame to the window: the window can be moved, the frame cannot.

        The size comes from the page, which is also where it is known which frame has arrived.

        While the panels are hidden by the "panic" key, the size is only remembered. Changing it
        now would mean bringing the window back onto the screen (see `_resize_one`), and they are
        hidden precisely so that they cannot be seen.

        `seq` is a timestamp of when the request was sent from the page (`Date.now()` there), not
        a sequence number: without it (a direct call from a test, a page with an old version of
        the script) the behavior is as before and the resize is always applied. With it, a
        request older than one already applied does not touch the window at all. This is needed
        because the method is called not by one path but by two independent paths for the same
        window (the corner handle and the "show screen" arrow, or the fitting of the screen
        window to the frame next to it), and each call goes through `asyncio.to_thread` on the
        server's shared thread pool: the one sent earlier may well finish on its thread later
        than the one sent after it, and without the check it would be the one applied, silently
        rolling the size back after the newer one has already landed on screen. The check and the
        `_resize_one` call itself are under one lock, not separate: otherwise the race would just
        move one step earlier, between the check outside and the application here.
        """
        hwnd = self.hwnds.get(name)
        if hwnd is None:
            raise RuntimeError(f"panel window '{name}' is not open")
        if self.hidden:
            # The same `seq` check as in the visible path below: without it an older request, whose
            # `to_thread` finished later, overwrites a newer size already recorded in `pending_size`,
            # and the race simply moves to the moment when the panels are shown again.
            with self._resize_lock:
                if seq is not None:
                    last = self._resize_seq.get(name)
                    if last is not None and seq < last:
                        return False
                    self._resize_seq[name] = seq
                self.pending_size[name] = (width, height)
            return False
        with self._resize_lock:
            if seq is not None:
                last = self._resize_seq.get(name)
                if last is not None and seq < last:
                    return False
                self._resize_seq[name] = seq
            return _resize_one(hwnd, width, height)

    def get_min_width(self, name: str) -> int | None:
        """The window's own width floor, if one is currently raised; otherwise `None`.

        The server reads it before every `clamp_window_size`: the corner handle knows nothing
        about the screen panel, and without this it could drag the window below the raised floor
        (the handle is not the system drag on the edge but our own, over the network, and
        `Form.MinimumSize` alone would not stop it).
        """
        return self._min_widths.get(name)

    def set_min_width(self, name: str, min_width: int | None) -> None:
        """Raise or remove the window's own width floor on an already open window.

        `None` removes the own floor and returns the window to the common one from `WINDOW_BOX`.
        That is what the screen panel asks for when it closes.
        """
        window = self.windows.get(name)
        if window is None:
            return
        from argus.ui.server import WINDOW_BOX

        base_min_w, min_h, _, _ = WINDOW_BOX
        if min_width is None:
            self._min_widths.pop(name, None)
            _set_min_size(window, base_min_w, min_h)
        else:
            self._min_widths[name] = min_width
            _set_min_size(window, min_width, min_h)

    def _forget(self, name: str) -> None:
        """A panel was closed with its close button.

        No lock, on purpose: this is called from the window thread, and the lock may at that
        moment be held by the opening of a neighbouring panel, and the whole interface would
        freeze for seconds. `pop` calls on different keys do not interfere with each other.
        """
        self.windows.pop(name, None)
        self.hwnds.pop(name, None)
        self._min_widths.pop(name, None)
        self._resize_seq.pop(name, None)
        if not self.windows:
            _panel_overlay(self.cfg, False)
            _close_parked_main()

    @staticmethod
    def _await_titled(title: str, timeout: float = 5.0) -> int | None:
        """The window handle for a given title.

        We search by name, not by which window appeared last. The difference is not theoretical:
        the main window and a detached panel are born almost at the same time, and the "new
        window" turned out to be the main one, so everything ordered for the panel (size,
        transparency) went to the whole dashboard. The panels' titles are their own and cannot
        coincide.
        """
        if sys.platform == "darwin":
            from argus.mac import desktop as mac

            return mac.await_window(title, timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for hwnd in own_windows():
                if _window_title(hwnd) == title:
                    return hwnd
            time.sleep(0.05)
        return None

# How long to wait for the WebView event loop before saying that there will be no window.
# With a margin: seconds pass between "the server answered" and "windows are being drawn",
# and the whole time the "detach" button on the page can already be pressed.
PANEL_READY_S = 10.0

PANEL_TITLES = {
    "chat": "Argus · answers",
    "screen": "Argus · screen",
    "live": "Argus · all at once",
}

# The initial window size, before the page has had time to measure anything and ask for a
# size of its own. The answers do not fit themselves to anything, and any reasonable size
# box is equally good for them. The screen is the only panel with `fitScreenWindow`
# (index.html): as soon as the first frame arrives, it asks for a size to match the frame's
# shape, usually 16:9. Open it in the same box as the others, and the person would see the
# window, just after opening, immediately shrink in height. A window that starts in 16:9
# either needs no correction at all or only a barely noticeable one, for the frame not being
# strictly 16:9.
#
# The "all at once" window stacks the screen and the answers in a column (index.html,
# data-solo=live), so its box needs the height for both at once, not for one.
PANEL_SIZE = {
    "screen": (460, 259),
    "live": (460, 560),
}
DEFAULT_PANEL_SIZE = (460, 340)

# The detached panels of the current launch. A module variable, not a field of the app:
# WebView draws the windows in the main thread, while the web server asks to open them from
# its own. A shared place is the shortest path between them, and it is more honest than
# dragging a window object through a pipeline that knows nothing about windows.
_PANELS = None


def panels():
    """Detached panels, if the app is running with a window."""
    return _PANELS


# How to close the app from the inside: an event on which `run_desktop` closes all windows
# and exits (the pipeline sets it too, when it ends by itself). The update needs it: after
# putting the replacement in place, it must close the program so that the script can swap
# the files (`update_service.py`).
_QUIT = None


def quit_app() -> bool:
    """Close the windows and quit the app. `False` means there is no window (web mode), so there is
    nothing to close.
    """
    if _QUIT is None:
        return False
    _QUIT()
    return True


# The hotkeys of the current launch, for the same reason as the panels: the window thread
# holds them, while the settings page asks to rebind them from its own.
_HOTKEYS = None


def hotkeys():
    """Hotkeys, if the app is running with a window."""
    return _HOTKEYS


# The main window of the current launch, for the same reason as the panels: the window
# thread holds it, while both the hotkey and the settings page ask to hide or bring it
# back, each from its own thread.
_MAIN_WINDOW = None
_main_hidden = {"on": False}


def main_window():
    """The main window, if the app is running with a window (not a browser tab)."""
    return _MAIN_WINDOW


def main_window_hidden() -> bool:
    """Whether the main window is hidden right now."""
    return _main_hidden["on"]


def hide_main_window() -> bool:
    """Hide the main window without closing it. An ordinary minimize, not to be confused with
    `hide_from_capture`: this window disappears entirely, on your own screen too, and not only
    where it is being captured."""
    if _MAIN_WINDOW is None:
        return False
    _quietly(_MAIN_WINDOW.hide, "the main window did not hide")
    _main_hidden["on"] = True
    return True


def show_main_window() -> bool:
    if _MAIN_WINDOW is None:
        return False
    _quietly(_MAIN_WINDOW.show, "the main window did not come back")
    _main_hidden["on"] = False
    _main_parked["on"] = False
    return True


def _main_visible() -> bool | None:
    """Whether the main window is visible right now, according to the system itself. `None`
    means there is nothing to ask (not Windows, or no window yet).

    The remembered flag (`_main_hidden`) is not enough: a second launch shows the hidden
    window itself, bypassing this process, and the flag stays "hidden" while the window is
    visible.
    """
    hwnd = _main_hwnd(include_hidden=True)
    if hwnd is None:
        return None
    return bool(ctypes.windll.user32.IsWindowVisible(hwnd))


def toggle_main_window() -> bool:
    """Hide or bring back. Returns the new state: True if it is hidden now.

    Decides by what is on screen, not by what was remembered: the "hide" key is a panic
    button, and the very first press must hide a window that a second launch showed without
    this process knowing.
    """
    visible = _main_visible()
    hidden = _main_hidden["on"] if visible is None else not visible
    if hidden:
        show_main_window()
    else:
        hide_main_window()
    return _main_hidden["on"]


# The main window that was "closed" with its close button while panels are open: it is only
# hidden. Closing it for real would leave the process without a window that can be brought
# back: a second launch looks for the window by title and, not finding it, would wait 25
# seconds and ask to kill the live program. We close it when the last panel has closed.
_main_parked = {"on": False}


def _close_parked_main() -> None:
    """The last panel window was closed: the main window hidden with the "X" is closed for real,
    otherwise the process would stay alive without a single visible window."""
    if not _main_parked["on"]:
        return
    _main_parked["on"] = False
    if _MAIN_WINDOW is None or _main_visible():
        return        # the window was already brought back (second launch, key): it is alive, leave it alone
    _quietly(_MAIN_WINDOW.destroy, "the main window did not close")


# Whether it is currently hidden from screen capture. It is separate from
# `cfg.ui.exclude_from_capture` in the sense of "right now, in fact", although it is kept
# in a pair with it: the applier below changes both at once, otherwise a new detached panel
# (`Panels.open` reads `cfg.ui.exclude_from_capture` at the moment the window is created)
# would open in a state that is already wrong on screen.
_capture_excluded = {"on": False}


def capture_excluded() -> bool:
    return _capture_excluded["on"]


def set_capture_exclusion(enabled: bool) -> tuple[bool, str]:
    """Turn the windows' invisibility to screen recording on or off, immediately, without a
    restart. You can see them either way: it hides them not from the eyes but from capture;
    see `hide_from_capture`.
    """
    ok, detail = hide_from_capture() if enabled else show_in_capture()
    if ok:
        _capture_excluded["on"] = enabled
        if _PANELS is not None:
            _PANELS.cfg.ui.exclude_from_capture = enabled
    return ok, detail


def _quietly(what: Callable[[], object], about: str) -> None:
    try:
        what()
    except Exception as e:                       # noqa: BLE001 — the window may already be gone
        log.debug("%s: %s", about, e)


def _start_hotkeys(cfg: Config, url: str, window) -> None:
    """Set up key combinations for actions that would otherwise need the mouse.

    Not touching the mouse and not switching windows is what all five are about. Alt-Tab is
    visible in a screen share, and so is cursor movement, and either is more noticeable than
    the answer itself, which is what all of this was for.

    The main window and the detached panel are hidden by different combinations, and that is
    not separation for its own sake: one is the dashboard itself, the other is the overlay on
    top of a call, and you need to be able to hide the overlay during a screen share without
    touching the dashboard (and vice versa).
    """
    from argus.assist.settings import read
    from argus.hotkeys import Hotkeys

    def toggle_panel() -> None:
        if not _PANELS.windows:
            # Nothing to hide, because nothing is detached. We open the "all at once" window: without
            # this the hotkey on an empty place would do nothing visible, and detaching by hand is
            # exactly the mouse movement the hotkeys are meant to spare. A refusal (the app is still
            # starting) is caught and logged by the shared handler `Hotkeys._fire`.
            _PANELS.open("live")
            return
        # There is one state, and it belongs to the panels themselves (`Panels.hidden`). A separate
        # copy here drifted away from it as soon as a panel was opened with the dashboard button
        # while the others were hidden: the "hide" key would then show the others instead of
        # hiding this one.
        _PANELS.toggle()

    def ask() -> None:
        # Through our own server, not around it. This is exactly the "Ask" button that is
        # on the page: the same filter, the same timeline, the same session recording. A second
        # path to the model would diverge from the first on the first edit.
        #
        # We take "Think longer" from the server instead of sending an empty body: it is a real
        # channel setting (`channel.assist.answering.think_longer`, the control next to the input
        # field writes it through the same `/admin/apply`), not something in the browser, and
        # localStorage is out of reach for the hotkey, a separate process with no page. With an
        # empty body the hotkey always asked at the normal level, even if "Think longer" was turned
        # on at the control.
        schema = _get_json(url + "/admin/schema") or {}
        deep = bool(schema.get("values", {}).get("channel.assist.answering.think_longer"))
        _post_json(url + "/ask", {"deep": deep})

    def ask_screen() -> None:
        # The same as what the "ask about the screen" button used to do: the frame goes to the
        # model even if constant sending is off. The button was removed from the interface, but
        # the path to the server remained, and we use it.
        _post(url + "/ask/screen")

    def recognition() -> None:
        # The same shared switch as the "power" button on the control, so no reaching for the mouse
        # is needed. We compute the toggle from the server's state, not from this thread's memory:
        # the same value is changed with the mouse at the control, and a local copy would drift
        # away from it on the first click.
        current = _get_json(url + "/state")
        controls = current.get("controls") if isinstance(current, dict) else None
        if not isinstance(controls, dict) or not isinstance(controls.get("running"), bool):
            # We could not read the state (the server is busy, a refusal, a reply without the control):
            # we must not guess "off" and turn it on, because that would be paid recognition and the
            # microphone switched on blindly. Turning it off is safe in any case: a person who pressed
            # "stop" gets what they wanted, and "turn on" will repeat.
            log.warning("recognition state could not be read - turning it off just in case")
            _post_json(url + "/control", {"running": False})
            return
        _post_json(url + "/control", {"running": not controls["running"]})

    global _HOTKEYS
    _HOTKEYS = Hotkeys({
        "ask": ask, "hide_main": toggle_main_window, "hide_panel": toggle_panel,
        "ask_screen": ask_screen, "recognition": recognition,
    })
    _HOTKEYS.start(read(cfg).hotkeys.model_dump())


def _post(url: str) -> None:
    """Knock on our own server with no body. The response is not needed; the call itself matters."""
    _post_json(url, {})


def _post_json(url: str, payload: dict) -> None:
    """Knock on our own server with a body. The response is not needed; the call itself matters."""
    import json

    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        loopback_open(request, timeout=15).close()
    except Exception as e:                       # noqa: BLE001 — the server may have already stopped
        log.warning("hotkey did not reach the server: %s", e)


def _get_json(url: str) -> dict | None:
    """Read the state from the server. If it does not come through, return empty instead of
    crashing.

    Exactly one hotkey needs it, the recognition toggle: to compute the toggle, you first
    need to know the current value, not guess it from this thread's memory.
    """
    import json

    try:
        with loopback_open(url, timeout=15) as response:
            return json.loads(response.read())
    except Exception as e:                       # noqa: BLE001 — the server may have already stopped
        log.warning("hotkey could not read the state: %s", e)
        return None


def _alert(text: str, warning: bool = False) -> None:
    """Tell the person with a message box what would otherwise stay a line in the log.

    A built app has no console: a launch that closed by itself (the server did not respond, a
    required component is missing, hiding from screen capture failed) looked from outside
    like "the shortcut doesn't work". A message box is one of the few things visible without
    a page. If showing it fails, stay silent: that would be a message about a message.
    """
    try:
        if sys.platform == "darwin":
            import json
            import subprocess

            kind = "warning" if warning else "critical"
            subprocess.run(
                ["osascript", "-e",
                 f"display alert \"Argus\" message {json.dumps(text, ensure_ascii=False)} as {kind}"],
                timeout=120, check=False, capture_output=True,
            )
        elif sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, text, "Argus", 0x30 if warning else 0x10)
    except Exception as e:                       # noqa: BLE001 — the message matters more than the error of the message
        log.debug("message window did not show: %s", e)


# How many times and for how long we try to hide the window from screen capture at launch.
# The window may lag behind the "shown" event: "window not found" or
# "1 of 2 windows hidden" at the first moment is not yet a refusal by the system.
CAPTURE_ATTEMPTS = 4
CAPTURE_RETRY_S = 0.5

CAPTURE_WARNING = (
    "Argus could not hide its window from screen recording. While you are sharing your screen, "
    "the people you talk to may be able to see it. Close Argus and start it again; if that does "
    "not help, do not share your screen while it is open."
)

NO_RUNTIME_TEXT = (
    "The Argus window needs the Microsoft Edge WebView2 component, and this computer does not have it. "
    "Install it (the 'Evergreen Standalone Installer' section on the page "
    "developer.microsoft.com/microsoft-edge/webview2) and start Argus again."
)


def _hide_main_from_capture() -> tuple[bool, str]:
    """Hide the windows from capture at launch, with retries. Returns the last response."""
    ok, detail = False, "window not found"
    for attempt in range(1, CAPTURE_ATTEMPTS + 1):
        try:
            # The first attempt waits for the window to appear, the repeated ones do not: it should
            # have been there for a long time.
            ok, detail = hide_from_capture() if attempt == 1 else hide_from_capture(timeout=1.0)
        except Exception:                        # ctypes and system internals
            log.warning("attempt to hide the window from capture failed", exc_info=True)
            ok, detail = False, "error while trying to hide the window"
        if ok:
            break
        log.warning(
            "window was not hidden from capture (attempt %d/%d): %s", attempt, CAPTURE_ATTEMPTS, detail
        )
        if attempt < CAPTURE_ATTEMPTS:
            time.sleep(CAPTURE_RETRY_S)
    return ok, detail


def _add_query(target: str, *flags: str) -> str:
    """Append parameters to the URL query, before the `#`, not after it.

    A parameter placed after the fragment (`/admin#account?desk=1`) reaches neither the
    server nor the page as a query, and the server also redirected `/admin` to `/#admin`
    without it.
    """
    if not flags:
        return target
    parts = urlsplit(target)
    query = "&".join(part for part in (parts.query, *flags) if part)
    return urlunsplit(parts._replace(query=query))


def _page_draws_bar(path: str | Callable[[], str]) -> bool:
    """Whether the page that will be opened in the window draws its own strip with window
    buttons (`winbar.js`).

    The settings page (`/admin`, `--settings` mode) does not: remove the system title bar
    from its window and it can be neither moved, nor minimized, nor closed with the mouse. An
    address given as a function (evaluated after the server starts) is always a working page
    with the strip.
    """
    if callable(path):
        return True
    return urlsplit(path).path.rstrip("/") != "/admin"


def claim_instance_or_focus(
    cfg: Config, port: int = 0, on_stuck: Callable[[str], None] | None = None
) -> bool:
    """Take the mutex for this port first. Returns `False` if someone already holds it and has a
    window (we bring that one to the foreground). If it has no window, because it is still
    starting up or already shutting down, we wait up to `TAKEOVER_WAIT_S`: if a window
    appears, we raise it; if the previous instance exits, we take the lock ourselves. If
    neither happens in time, the previous instance is hung: `on_stuck(text)` tells the person
    about it (the built app shows a message box), and the result is `False`.

    Call this before everything else that decides not to open a window a second time,
    including the update check: `argus.update.maybe_self_update` used to be called before
    this check in `run_desktop`, and a repeated launch while the first copy was already
    running (a second double click, a race on a quick restart) downloaded and unpacked the
    archive over the running program every time. Several such launches in a row chased the
    same update simultaneously and corrupted each other's files.
    """
    port = port or cfg.ui.port
    if _claim_single_instance(_instance_mutex_name(port)):
        return True
    url = cfg.ui.base_url(port)
    title = cfg.ui.window_title

    # The lock is taken, but "taken" comes in two kinds, and the lock alone cannot tell them
    # apart: the first instance is either alive and about to show its window (or already has),
    # or it has closed the window and is now stopping the pipeline. In both cases a second
    # launch used to exit silently, and from outside this looked like "the shortcut doesn't
    # work". Now we wait for the outcome: if a window appears, we raise it; if the previous
    # instance exits, we become the first ourselves.
    began = time.monotonic()
    deadline = began + TAKEOVER_WAIT_S
    while True:
        # We do not look for a hidden window right away: while the first instance is starting up,
        # its window may not exist yet, and the visible ones are enough for the first seconds.
        reveal = time.monotonic() - began > HIDDEN_AFTER_S
        if _focus_existing_window(title) or (reveal and _focus_existing_window(title, True)):
            console.print(f"[yellow]the app is already running[/yellow] - it is at {url}")
            return False
        if _acquire_when_free(TAKEOVER_POLL_S):
            log.info("previous instance finished - starting in its place")
            return True
        if time.monotonic() >= deadline:
            break
    console.print(f"[yellow]the app is already running[/yellow] - it is at {url}")
    log.warning(
        "lock has been held for %.0f s and there is no window - nothing to bring to the foreground", TAKEOVER_WAIT_S
    )
    if on_stuck is not None:
        # Another instance is alive and holds the lock, but there has been no window for half a
        # minute already: it is hung. Exiting silently would again mean "the shortcut doesn't
        # work", so the person needs to be told what to do (in the built app without a console this
        # can only be done with a message box).
        how = (
            "Quit Argus in 'Activity Monitor' and start it again."
            if sys.platform == "darwin"
            else "End the Argus.exe process in Task Manager and start it again."
        )
        on_stuck(f"Argus is already running but has no window — the previous launch seems to be hung. {how}")
    return False


def run_desktop(
    cfg: Config,
    make_coro: Callable[[threading.Event], Coroutine[object, object, None]],
    port: int = 0,
    path: str | Callable[[], str] = "",
    claimed: bool = False,
    on_shown: Callable[[], None] | None = None,
) -> int:
    """Start the passed coroutine in the background and show the window. Returns the exit code.

    The coroutine comes from outside because the window is shown in two different modes: the
    full pipeline and the settings alone, without hardware. What they have in common is
    exactly what is here: the window on the main thread, waiting for the server, and stopping
    on close.

    **The window appears first, the server later.** Before, the main thread waited for the
    whole pipeline to come up (audio, network, models) and only then created the window: until
    that moment there was nothing on screen, and the person kept clicking the shortcut again
    and again. Now the window is created at once and shows a splash screen (`splash_html`),
    and the real page loads when the server has answered. In addition, the pipeline's heavy
    imports run in a background thread at the same time as the main thread brings up
    WebView2.

    `claimed=True` means the caller has already taken the mutex itself
    (`claim_instance_or_focus`, before the update check); the same mutex cannot be taken a
    second time from the same process, because `CreateMutexW` would return the same "already
    exists" a second time, as if someone else held the window.

    `path` is the page address after the server: a string or a function with no arguments. A
    function is needed when the answer costs work (checking the account sign-in), and there
    is no point computing it before the window.

    `on_shown` is what to do once the window is on screen (from the `after_start` thread, not
    the main one). It is for work that the window does not need but that must not get in its
    way: any thread started earlier shares the interpreter with the window and delays it
    (measured: preloading libraries pushed the window back by ~0.1 s).
    """
    from argus.startup import mark

    _declare_dpi_aware()

    port = port or cfg.ui.port
    url = cfg.ui.base_url(port)

    # A second instance is not started, and the reason is not only the port. WebView2 keeps the
    # profile as a directory on disk and does not give it to a second process: the window opens,
    # and the page never appears in it. A mutex, not an HTTP poll of the port (which was here
    # before): the poll has its own race on a quick repeated launch, details at
    # `_claim_single_instance`.
    #
    # The name includes its own port: `--settings` deliberately allows several copies to be
    # open at once, each on its own port ("three copies open at once", `cli.py`), and a mutex
    # shared by all ports would break exactly that. The same port means the same mutex and the
    # same meaning as the HTTP poll had before, just without its race.
    if not claimed and not claim_instance_or_focus(cfg, port):
        return 0

    # We create it before the pipeline, not together with the window: the server starts
    # answering before the main window appears, and in this gap a request to detach a panel
    # went nowhere, with the answer "we are in a browser" although we are in the app. It is
    # placed below the launch refusal, so that a process that decided not to come up does not
    # leave behind the appearance of having windows.
    global _PANELS
    _PANELS = Panels(url, cfg)

    closed = threading.Event()
    global _QUIT
    _QUIT = closed.set
    failure: list[BaseException] = []
    gave_up: list[bool] = []
    # Why we stopped the launch ourselves (a required component is missing, the window itself
    # failed): text for the person.
    stop_reason: list[str] = []

    def pipeline() -> None:
        try:
            asyncio.run(make_coro(closed))
        except BaseException as e:               # noqa: BLE001 — we will carry it upward ourselves
            failure.append(e)
            # A built app has no console, and without an entry in the log the window would just close
            # by itself without saying why.
            log.error("pipeline finished with an error", exc_info=(type(e), e, e.__traceback__))
        finally:
            # The pipeline ended by itself: the device disappeared, or a time limit ran out. The window
            # has nothing more to show, and it must not hang around.
            closed.set()

    # The pipeline goes to the background first of all, before `import webview` and the window:
    # its imports and assembly run while the main thread brings up WebView2 and .NET. These are
    # different resources (disk and kernel on one side, the CLR on the other), and together they
    # take less time than one after the other.
    worker = threading.Thread(target=pipeline, name="argus-pipeline", daemon=True)
    worker.start()

    import webview

    window = webview.create_window(
        cfg.ui.window_title,
        html=splash_html(cfg.ui.window_title),
        width=cfg.ui.window_width,
        height=cfg.ui.window_height,
        min_size=(880, 600),
        # Open straight to full screen: not over it (`fullscreen` would hide both the system
        # taskbar and the window frame) but as an ordinary "maximized", like a double click on the
        # title bar. `width`/`height` above are not redundant here: this is the size the window
        # returns to if the person restores it with the button in the frame. It is a setting, not a
        # hard-coded value: `cfg.ui.window_maximized`. On macOS pywebview "maximizes" to the whole
        # screen, and the top of the window goes under the menu bar, so there the window is
        # maximized after showing, to the visible area (`mac/desktop.py: maximize_main`).
        maximized=cfg.ui.window_maximized and sys.platform != "darwin",
        # The same color as the page. The pywebview default is white, and the window used to start
        # with a full-screen white flash: the page does not load instantly, and until it does the
        # window is painted with this color.
        background_color=BACKGROUND,
        # The pywebview default is `False`, which disables text selection in the whole window on
        # top of any page CSS; see the same parameter for the detached panels above.
        text_select=True,
    )
    global _MAIN_WINDOW
    _MAIN_WINDOW = window
    _MAIN_TITLE["t"] = cfg.ui.window_title
    _main_hidden["on"] = False
    _main_parked["on"] = False
    _main_no_taskbar["on"] = False

    def on_main_closing() -> bool | None:
        """The main window's close button hides it instead of closing it while panels are open.

        Closing the dashboard and leaving only a panel is by design (the process lives as long as
        there is a window). But a closed window cannot be brought back: a second launch looks for
        the window by title and, not finding it, would wait 25 seconds and ask to kill the live
        program, possibly in the middle of a recording. A hidden window can be found
        (`claim_instance_or_focus` shows hidden ones too); with the last panel the window really
        closes (`_close_parked_main`). `False` cancels the close.
        """
        if sys.platform != "win32" or closed.is_set() or not _PANELS.windows:
            return None
        if ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN):
            return None                          # the system is shutting down: cancelling the close would block it
        _main_parked["on"] = True
        hide_main_window()
        return False

    closing = getattr(getattr(window, "events", None), "closing", None)
    if closing is not None:
        closing += on_main_closing

    def watch_pipeline() -> None:
        """Close the windows if the pipeline ended before the user did.

        Detached ones too, and that is not a small thing: `webview.start()` releases the process
        only when the last window has closed. A panel left over the screen with a dead pipeline
        would keep the app alive and show frozen numbers as if they were real.
        """
        closed.wait()
        _PANELS.close_all()
        try:
            window.destroy()
        except Exception as e:                   # noqa: BLE001 — the window may already be gone
            log.debug("window is already closed: %s", e)

    threading.Thread(target=watch_pipeline, name="argus-window-watch", daemon=True).start()

    # Blocks until the last window closes. `private_mode=False` so that the selected window and
    # the checkboxes survive a restart: an app that forgets its settings on every start feels
    # broken.
    #
    # Since the profile is persistent, it must have a persistent location. Without an explicit
    # path pywebview puts it in a temp folder, and a cleanup of temporary files will one day
    # take with it everything the person configured.
    def _launch() -> None:
        """What happens once the window exists: hide it from capture, wait for the server, show the
        real page, set up the key combinations.

        The order is not accidental. We hide from capture **first**: the window is already on
        screen (even if still with the splash), and it must not be shown in a screen share. We
        open the page as soon as the server has answered, and the combinations come after it:
        they can wait, and the person should not wait on an empty window.
        """
        _PANELS.ready.set()
        # This function starts together with the window loop, slightly before the window itself: we
        # wait until the window is really on screen, otherwise the log entry would lie about "window
        # shown", and `hide_from_capture` would needlessly poll a window that does not exist yet.
        shown = getattr(getattr(window, "events", None), "shown", None)
        if shown is not None:
            shown.wait(15)
        mark("window shown")

        # The component pywebview uses to show pages (WebView2) is missing, so it falls back to the
        # old Internet Explorer engine, and the app's pages do not work in it, while the system
        # title bar has already been removed by this point. We say so plainly and do not load the
        # page.
        if sys.platform == "win32" and getattr(webview, "renderer", None) == "mshtml":
            log.error("no WebView2: pywebview fell back to the legacy engine, the app pages do not work in it")
            stop_reason.append(NO_RUNTIME_TEXT)
            closed.set()
            return

        capture_failed = False
        if cfg.ui.exclude_from_capture:
            ok, detail = _hide_main_from_capture()
            console.print(
                f"  [green]{detail}[/green]" if ok else f"  [red]VISIBLE IN CAPTURE: {detail}[/red]"
            )
            if ok:
                _capture_excluded["on"] = True
            else:
                # This used to be a console line, and a built app has no console: the page with the answers
                # opened as if nothing had happened. Now it goes to the log, to a message box, and to a
                # marker in the page address (`capture=fail`), by which the page can show "visible in the
                # recording".
                capture_failed = True
                log.error("VISIBLE IN CAPTURE: window was not hidden from screen recording - %s", detail)
                threading.Thread(
                    target=_alert, args=(CAPTURE_WARNING, True), name="argus-capture-warning", daemon=True
                ).start()

        if sys.platform == "win32" and not cfg.ui.main_taskbar_button:
            # After capture protection, before the page: the "Argus" taskbar button is visible in a
            # full-screen share. If that fails, the window keeps the button (so at least there is a way
            # back), and the reason is in the log.
            for attempt in range(1, 4):
                ok, detail = _main_taskbar_button(False)
                if ok:
                    break
                log.warning("main window button remained on the taskbar (attempt %d/3): %s", attempt, detail)
                time.sleep(0.3)

        if sys.platform == "darwin":
            # Dark styling, and the window fitted to the visible screen area; the system title bar
            # itself is removed by `use_page_caption` below, as on Windows.
            try:
                from argus.mac import desktop as mac

                native = getattr(window, "native", None)
                if native is not None:
                    mac.style_main_window(native, BACKGROUND)
                    if cfg.ui.window_maximized:
                        mac.maximize_main(native)
                if cfg.ui.mac_overlay == "always":
                    mac.set_overlay_mode(True)
            except Exception:                    # the styling must not break startup
                log.warning("macOS window styling was not applied", exc_info=True)

        # The system title bar is replaced by our own, in the page (`ui/static/winbar.js`): Windows
        # does not allow making it taller, and on macOS without this the header would sit under the
        # system bar as a second row. We remove it before the page loads so that the window does not
        # jump up when the page is already in place (this comes after capture protection: it is
        # looks, not protection). The settings page (`--settings` mode) draws no strip, so we leave
        # it the system one.
        caption_swapped = False
        draws_bar = _page_draws_bar(path)
        try:
            if draws_bar:
                caption_swapped = use_page_caption(cfg.ui.window_title, CAPTION_COLOR)
        except Exception:                        # noqa: BLE001 — the frame must not break startup
            log.warning("could not fully replace the window title bar", exc_info=True)
        # An exception could arrive after `WS_CAPTION` has already been removed, and the window
        # would be left with neither the system strip nor its own: the page would not know about
        # `desk=1` and would not draw the buttons (the symptom: nothing at the top of the window). So
        # the decision is made not by the function's return value but by the window itself: if there
        # is no strip, the page draws one.
        try:
            if not caption_swapped and caption_removed(cfg.ui.window_title):
                caption_swapped = True
                log.info("window has no system title bar - custom bar enabled based on window state")
        except Exception:                        # the frame check must not break startup
            log.warning("could not check the window title bar", exc_info=True)

        # Only after the window has been hidden from capture: a preload started earlier shares the
        # interpreter with this thread, and while it was ramping up, the window was visible in a
        # screen share a quarter of a second longer (measured: 263 ms against 12).
        if on_shown is not None:
            try:
                on_shown()
            except Exception:                    # noqa: BLE001 — the window matters more than the preload
                log.exception("'after window shown' work failed")

        # `closed` interrupts the wait: the pipeline may have crashed or the window may have been
        # closed, and there is no point waiting the whole timeout for a server that will not come.
        # This thread is not a daemon, so while it waits, the process will not exit.
        if not wait_until_serving(url, timeout=ready_timeout(), abort=closed):
            if not closed.is_set():
                gave_up.append(True)
                closed.set()
            return
        mark("server responding")

        try:
            target = url + (path() if callable(path) else path)
            flags = []
            if caption_swapped:
                # A marker for the page: it draws the strip with the buttons, but only in this window, not
                # in a browser tab or in the demo on the landing page.
                flags.append("desk=1")
            if capture_failed:
                flags.append("capture=fail")        # the window was not hidden from capture: the page can say so
            window.load_url(_add_query(target, *flags))
            mark("page requested")
        except Exception as e:                   # noqa: BLE001 — the window may have been closed
            log.warning("could not open the page in the window: %s", e)
            if caption_swapped:
                restore_native_caption(cfg.ui.window_title)

        try:
            _start_hotkeys(cfg, url, window)
        except Exception:                        # noqa: BLE001 — the window matters more than the keys
            log.exception("hotkeys did not start")

    def after_start() -> None:
        """`_launch` under protection: an exception in the launch thread must not leave the splash
        screen up forever.

        pywebview runs this function in an ordinary thread, and an unhandled error (ctypes,
        window styling) would kill it before the page loads and before the key combinations,
        while the pipeline behind the splash screen kept working, listening to audio and spending
        money with an empty window. If it fails, we log the reason, close the window and tell the
        person.
        """
        try:
            _launch()
        except Exception:                        # catch everything: there is a single launch thread
            log.exception("window launch was interrupted")
            if not closed.is_set():
                stop_reason.append("Argus could not finish starting. Details are in argus.log.")
                closed.set()

    def shutdown() -> None:
        """Stop everything that was started: key combinations, pipeline, recording.

        Called after the last window closes, and on macOS also before quitting via `⌘Q`
        (`mac/desktop.py: on_will_terminate`): there the system exits on its own without
        returning from `webview.start`, and without this the session recording would be left
        unfinished. A repeated call is harmless.
        """
        if _HOTKEYS is not None:
            # We unregister the combinations ourselves. The system would release them anyway, along
            # with the process, but between closing the window and the end of the process the
            # pipeline's shutdown time passes, and for all of it "ctrl+alt+a" would not reach the
            # program the person is already using instead of ours.
            _HOTKEYS.stop()

        closed.set()
        # We wait for the pipeline to close audio, screen and recording: without this the process
        # would die before the last session is fully written. But not forever: while the process is
        # alive it holds the single-instance lock, and a new launch waits. The twenty seconds that
        # used to be here were enough for any hung shutdown to make the person decide the app was
        # broken, while an honest stop (server, recognition sessions, audio) fits in a few seconds.
        worker.join(timeout=STOP_JOIN_S)
        if worker.is_alive():
            log.warning("pipeline did not stop within %.0f s - exiting without it", STOP_JOIN_S)
        mark("pipeline stopped")

    if sys.platform == "darwin":
        from argus.mac import desktop as mac

        mac.on_will_terminate(shutdown)
        mac.prevent_app_nap()

    webview.start(after_start, private_mode=False, storage_path=str(storage_dir()))
    mark("window closed")

    shutdown()
    if failure:
        raise failure[0]
    if stop_reason:
        console.print(f"\n[red]{stop_reason[0]}[/red]\n")
        _alert(stop_reason[0])
        return 1
    if gave_up:
        log.error("server did not respond within %.0f s - window closed", ready_timeout())
        console.print(f"\n[red]server did not come up within {ready_timeout():.0f} s[/red]\n")
        # By this point the window has closed, and a built app has no console: without a message the
        # person saw the splash screen and then emptiness, "the shortcut doesn't work".
        _alert(
            f"Argus did not finish starting within {ready_timeout():.0f} s and closed. "
            "Start it again. If it happens again, see the argus.log log for details."
        )
        return 1
    return 0
