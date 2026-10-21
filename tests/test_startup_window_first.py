"""Startup: window first, server later.

Complaint (2026-10-07): "the app starts insanely slowly, competitors do it right away".
The main window was created only after the whole pipeline (audio, network, models) came
up and the server responded; until then there was nothing on the screen. The person
clicked the shortcut again and again, and the second launch, finding no window, quietly
exited (in the log that is dozens of "the app is already running"
lines).

What is checked here is what can be checked without a real window: the order (window
before server), the behavior of the second launch, and that the leading edge of startup
does not pull in the pipeline.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import types
import uuid

import pytest

import argus.desktop as desktop
from argus.core.config import Config


# -- splash screen ---------------------------------------------------------------


def test_splash_has_the_window_color_and_needs_nothing_from_outside():
    """The splash screen is shown when the server does not exist yet, so it has nowhere to fetch
    from.
    """
    page = desktop.splash_html("Argus")

    assert desktop.BACKGROUND in page
    assert "http://" not in page and "https://" not in page
    assert "src=" not in page and "href=" not in page and "@import" not in page


def test_the_window_colour_is_the_colour_of_the_pages():
    """The splash hangs for about a second, and if its color differs from the color of the pages,
    the transition to the page is a visible jump. The pages were recolored (2026-10-05) while
    the window color stayed the same; this test catches exactly that mismatch."""
    import re
    from pathlib import Path

    static = Path(desktop.__file__).parent / "ui" / "static"
    for name in ("home.html", "index.html", "admin.css"):
        found = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", (static / name).read_text("utf-8"))
        assert found, f"--bg not found in {name}"
        assert found.group(1).lower() == desktop.BACKGROUND.lower(), (
            f"{name}: the page background is {found.group(1)}, but the window and splash are {desktop.BACKGROUND}"
        )


def test_splash_escapes_the_title():
    """The window title is a setting, and it must not get into the markup raw."""
    page = desktop.splash_html('<b>"x"</b>')

    assert "<b>" not in page
    assert "&lt;b&gt;" in page


# -- waiting for the server ------------------------------------------------------


def test_waiting_for_the_server_stops_as_soon_as_it_is_called_off():
    """The pipeline crashed or the window was closed: there is no reason to wait the full time
    for a server that will not come."""
    abort = threading.Event()
    abort.set()

    started = time.monotonic()
    assert desktop.wait_until_serving("http://127.0.0.1:1", timeout=10.0, abort=abort) is False
    assert time.monotonic() - started < 1.0


# -- second launch ---------------------------------------------------------------


def _hold(name: str, release: threading.Event, held: threading.Event) -> threading.Thread:
    """A foreign instance: a thread takes the named lock and holds it until told to let go. A
    thread, not a process: the lock belongs to the thread, and when it ends, Windows marks the
    lock as abandoned, exactly as on the exit of a real process."""
    import ctypes

    def run() -> None:
        ctypes.windll.kernel32.CreateMutexW(None, True, name)
        held.set()
        release.wait(10.0)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert held.wait(5.0)
    return thread


@pytest.fixture
def second_launch(monkeypatch):
    """Its own port (and so its own lock name) and no touching of real windows:
    `_focus_existing_window` looks for a window by title among all windows in the system, and
    the Argus running on this machine has the same title."""
    port = 20000 + uuid.uuid4().int % 20000
    name = desktop._instance_mutex_name(port)
    cfg = Config()
    monkeypatch.setattr(desktop, "_focus_existing_window", lambda title, include_hidden=False: False)
    monkeypatch.setattr(desktop, "_instance_mutex", None)
    monkeypatch.setattr(desktop, "TAKEOVER_POLL_S", 0.05)
    return types.SimpleNamespace(port=port, name=name, cfg=cfg)


@pytest.mark.windows_only
def test_a_second_launch_waits_for_the_first_one_to_finish_and_takes_over(second_launch):
    """The window was closed and the app was started again at once: the previous process is still
    stopping the pipeline. A second launch used to quietly exit here, which looked like "the
    shortcut does not work"."""
    release, held = threading.Event(), threading.Event()
    thread = _hold(second_launch.name, release, held)

    threading.Timer(0.4, release.set).start()       # "the previous one exited" after 0.4 s
    started = time.monotonic()
    claimed = desktop.claim_instance_or_focus(second_launch.cfg, second_launch.port)
    elapsed = time.monotonic() - started
    thread.join(5.0)

    assert claimed is True, "the second launch should have taken the lock when the first one exited"
    assert 0.3 < elapsed < 5.0


@pytest.mark.windows_only
def test_a_second_launch_raises_the_window_of_the_first_when_there_is_one(
    second_launch, monkeypatch
):
    monkeypatch.setattr(desktop, "_focus_existing_window", lambda title, include_hidden=False: True)
    release, held = threading.Event(), threading.Event()
    thread = _hold(second_launch.name, release, held)

    started = time.monotonic()
    claimed = desktop.claim_instance_or_focus(second_launch.cfg, second_launch.port)
    release.set()
    thread.join(5.0)

    assert claimed is False
    assert time.monotonic() - started < 1.0, "there is a window — nothing to wait for"


@pytest.mark.windows_only
def test_a_second_launch_gives_up_when_the_first_never_shows_a_window_or_dies(
    second_launch, monkeypatch
):
    monkeypatch.setattr(desktop, "TAKEOVER_WAIT_S", 0.4)
    release, held = threading.Event(), threading.Event()
    thread = _hold(second_launch.name, release, held)

    said: list[str] = []

    started = time.monotonic()
    claimed = desktop.claim_instance_or_focus(
        second_launch.cfg, second_launch.port, on_stuck=said.append
    )
    elapsed = time.monotonic() - started
    release.set()
    thread.join(5.0)

    assert claimed is False
    assert 0.3 < elapsed < 3.0
    assert len(said) == 1 and "Task Manager" in said[0], "the person must be told what to do"


@pytest.mark.windows_only
def test_a_second_launch_reveals_a_window_the_first_one_has_hidden(second_launch, monkeypatch):
    """The main window is hidden by a key combination (for the duration of a screen share), and a
    person who cannot find it clicks the shortcut: what they hid themselves must be shown. But
    not right away, because a just-started instance has no window yet."""
    calls: list[tuple[str, bool]] = []

    def focus(title, include_hidden=False):
        calls.append((title, include_hidden))
        return include_hidden                    # no visible one, a hidden one exists

    monkeypatch.setattr(desktop, "_focus_existing_window", focus)
    monkeypatch.setattr(desktop, "HIDDEN_AFTER_S", 0.3)
    release, held = threading.Event(), threading.Event()
    thread = _hold(second_launch.name, release, held)

    started = time.monotonic()
    claimed = desktop.claim_instance_or_focus(second_launch.cfg, second_launch.port)
    elapsed = time.monotonic() - started
    release.set()
    thread.join(5.0)

    assert claimed is False
    assert 0.25 < elapsed < 3.0, "the hidden window is not searched for before the deadline"
    assert calls[0][1] is False, "first the visible one"
    assert calls[-1][1] is True, "then the hidden one too"


def test_the_first_launch_takes_the_lock_at_once(second_launch):
    started = time.monotonic()
    assert desktop.claim_instance_or_focus(second_launch.cfg, second_launch.port) is True
    assert time.monotonic() - started < 1.0


# -- order: window before server -------------------------------------------------


class _FakeWebview:
    """A stand-in for `webview`: records what was asked of it and in what order."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.window_kwargs: dict = {}
        self.loaded: list[str] = []
        outer = self

        class Window:
            def load_url(self, url: str) -> None:
                outer.events.append("load_url")
                outer.loaded.append(url)

            def destroy(self) -> None:
                outer.events.append("destroy")

        self._window = Window()

    def create_window(self, *args, **kwargs):
        self.events.append("create_window")
        self.window_args = args
        self.window_kwargs = kwargs
        return self._window

    def start(self, func=None, **kwargs) -> None:
        self.events.append("start")
        if func is not None:
            func()


@pytest.fixture
def desktop_env(monkeypatch):
    """`run_desktop` without a real window, hotkeys, and a profile on disk."""
    events: list = []
    fake = _FakeWebview(events)
    module = types.ModuleType("webview")
    module.create_window = fake.create_window
    module.start = fake.start
    monkeypatch.setitem(sys.modules, "webview", module)
    monkeypatch.setattr(desktop, "_start_hotkeys", lambda *a, **kw: events.append("hotkeys"))
    monkeypatch.setattr(desktop, "_declare_dpi_aware", lambda: None)
    monkeypatch.setattr(desktop, "_PANELS", None)
    monkeypatch.setattr(desktop, "_HOTKEYS", None)
    # The message window ("did not finish starting") is not shown in the
    # test: it is modal and would wait for a click.
    alerts: list[str] = []
    monkeypatch.setattr(desktop, "_alert", lambda text, warning=False: alerts.append(text))
    # Protection from capture is on by default (`UIConfig.exclude_from_capture`), and there is
    # no real window here: let it "succeed" unless the test substitutes its own (order and
    # refusal are tested below and in `test_desktop_launch.py`).
    monkeypatch.setattr(desktop, "hide_from_capture", lambda *a, **kw: (True, "ok"))
    return types.SimpleNamespace(events=events, webview=fake, alerts=alerts)


def test_the_window_is_created_before_the_server_answers(desktop_env, monkeypatch):
    """The main point of it all: the window with the splash screen appears before the server is
    up.
    """
    events = desktop_env.events

    def serving(url, timeout=0, abort=None):
        events.append("wait_until_serving")
        return True

    monkeypatch.setattr(desktop, "wait_until_serving", serving)

    async def pipeline(stop):
        events.append("pipeline")

    code = desktop.run_desktop(Config(), pipeline, 18999, path="", claimed=True)

    assert code == 0
    assert events.index("create_window") < events.index("wait_until_serving")
    assert events.index("wait_until_serving") < events.index("load_url")
    assert events.index("load_url") < events.index("hotkeys"), "the page matters more than the key combinations"

    kwargs = desktop_env.webview.window_kwargs
    assert "html" in kwargs, "the window must be born with the splash screen, not with the server address"
    assert len(desktop_env.webview.window_args) == 1, "the first window must have no address"
    assert kwargs["background_color"] == desktop.BACKGROUND
    assert desktop_env.webview.loaded == ["http://127.0.0.1:18999"]


def test_the_landing_page_is_worked_out_after_the_server_answers(desktop_env, monkeypatch):
    """The answer depends on whether the account is signed in, so there is no reason to compute
    it before the window.
    """
    events = desktop_env.events
    monkeypatch.setattr(desktop, "wait_until_serving", lambda *a, **kw: True)

    def landing() -> str:
        events.append("landing")
        return "/#account"

    async def pipeline(stop):
        pass

    desktop.run_desktop(Config(), pipeline, 18999, path=landing, claimed=True)

    assert events.index("create_window") < events.index("landing") < events.index("load_url")
    assert desktop_env.webview.loaded == ["http://127.0.0.1:18999/#account"]


def test_a_server_that_never_answers_ends_the_launch_with_an_error(desktop_env, monkeypatch):
    monkeypatch.setattr(desktop, "wait_until_serving", lambda *a, **kw: False)

    async def pipeline(stop):
        # Lives until the window is told to close: a pipeline that did not crash but did not bring
        # the server up either.
        while not stop.is_set():
            await _sleep(0.02)

    code = desktop.run_desktop(Config(), pipeline, 18999, path="", claimed=True)

    assert code == 1
    assert "load_url" not in desktop_env.events
    # The window closed by itself, and there is no console in the built app: the person is told
    # this with a message window.
    assert len(desktop_env.alerts) == 1
    assert "did not finish starting" in desktop_env.alerts[0] and "argus.log" in desktop_env.alerts[0]


def test_a_pipeline_that_dies_while_starting_is_reported_not_swallowed(desktop_env):
    """The window is already on screen, but the pipeline crashed (port busy, no device): there is
    no point waiting for the server, and the cause must reach `main`, which will show it in a
    message box."""

    async def pipeline(stop):
        raise RuntimeError("port busy")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="port busy"):
        desktop.run_desktop(Config(), pipeline, 18999, path="", claimed=True)

    assert time.monotonic() - started < 5.0
    assert "load_url" not in desktop_env.events


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# -- the leading edge of startup does not pull in the pipeline ------------------


def test_what_the_window_needs_does_not_load_the_pipeline():
    """Before the window, the settings, the lock, and `desktop` itself are loaded, and nothing
    heavier. If `numpy` or `onnxruntime` ever gets in here, the window will again wait seconds
    for them, and the test will say so before a person does."""
    script = (
        "import sys\n"
        "import argus.core.config, argus.desktop, argus.startup, argus.landing\n"
        "heavy = ('argus.cli', 'argus.runner', 'argus.app', 'typer', 'numpy', 'cv2',\n"
        "         'onnxruntime', 'fastapi', 'uvicorn', 'httpx', 'anthropic', 'openai')\n"
        "loaded = [h for h in heavy if any(m == h or m.startswith(h + '.') for m in sys.modules)]\n"
        "print(','.join(loaded))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    ).stdout.strip()

    assert out == "", f"extra modules were loaded before the window: {out}"


def test_the_entry_point_of_the_build_does_not_import_the_pipeline_before_the_window():
    """`packaging/argus_app.py` used to import `argus.cli` and `argus.runner` at the very
    beginning of `main`, so the window waited for them. Now they are inside the pipeline
    function."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "packaging" / "argus_app.py").read_text("utf-8")
    head = source.split("def start_pipeline", 1)[0]

    assert "argus.cli" not in head.replace("`argus.cli`", "")
    assert "from argus.runner import" not in head
    assert "from argus.runner import run_app" in source.split("def start_pipeline", 1)[1]


def test_leaving_does_not_wait_for_dotnet_to_shut_down():
    """While the .NET runtime (pythonnet, WebView2) is loaded in the process, `os._exit` waits
    exactly two seconds for it to shut down: measured, 2.03 s versus 3 ms without it. For that
    time the process holds the single-instance lock, and a repeat launch waits. `leave` ends
    the process by itself, in milliseconds."""
    import re
    from pathlib import Path

    packaging = Path(__file__).parent.parent / "packaging"
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(packaging)!r})\n"
        "import clr\n"                                  # the .NET runtime is up, as in the window
        "from System import String\n"
        "import argus_app\n"
        "print('T_BEFORE_EXIT %.3f' % time.time(), flush=True)\n"
        "argus_app.leave(7)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    ended = time.time()

    assert process.returncode == 7, process.stderr
    before = float(re.search(r"T_BEFORE_EXIT ([\d.]+)", process.stdout).group(1))
    assert ended - before < 1.0, f"exit took {ended - before:.1f} s — it waits for .NET"


def test_process_age_is_measured_from_the_creation_of_the_process():
    from argus.startup import process_age_ms

    first = process_age_ms()
    time.sleep(0.2)
    second = process_age_ms()

    assert first is not None and second is not None
    assert first > 0
    assert second - first >= 150


# -- what else was removed from the path to the window --------------------------


def test_the_server_is_noticed_within_a_fraction_of_a_second():
    """It used to poll with `urlopen(timeout=1.0)`, and Windows answers a connection to a port
    that is not yet listening not with a refusal but with silence, so a server that came up in
    the middle of an attempt was noticed on average after a third of a second, up to 0.65. Now
    it is a cheap 50 ms TCP probe."""
    import http.server
    import socket
    import socketserver

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class Quiet(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    started: list[float] = []

    def serve_later() -> None:
        time.sleep(0.35)
        server = socketserver.TCPServer(("127.0.0.1", port), Quiet)
        started.append(time.monotonic())
        server.timeout = 0.05
        end = time.monotonic() + 5
        while time.monotonic() < end and not done.is_set():
            server.handle_request()
        server.server_close()

    done = threading.Event()
    thread = threading.Thread(target=serve_later, daemon=True)
    thread.start()
    try:
        assert desktop.wait_until_serving(f"http://127.0.0.1:{port}/", timeout=5.0)
        noticed = time.monotonic()
    finally:
        done.set()
        thread.join(5)

    assert noticed - started[0] < 0.30, f"the server was noticed after {noticed - started[0]:.2f} s"


def test_the_window_takes_the_prefetch_only_after_it_is_shown(desktop_env, monkeypatch):
    """A prefetch started before the window shares the interpreter with it and pushes it back."""
    events = desktop_env.events
    monkeypatch.setattr(desktop, "wait_until_serving", lambda *a, **kw: True)

    async def pipeline(stop):
        pass

    desktop.run_desktop(
        Config(), pipeline, 18999, path="", claimed=True,
        on_shown=lambda: events.append("on_shown"),
    )

    assert events.index("create_window") < events.index("on_shown")


def test_console_does_not_load_rich_until_something_is_printed():
    """`import rich.console` costs about fifty milliseconds on the path to the window."""
    script = (
        "import sys\n"
        "from argus.console import console\n"
        "before = 'rich.console' in sys.modules\n"
        "console.print('x')\n"
        "print(before, 'rich.console' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()

    assert out[-2:] == ["False", "True"]


def test_seeding_the_platform_cache_skips_the_wmi_queries():
    """On Python 3.12, `platform.uname()` makes two WMI queries, about 80 ms, and both onnxruntime
    and pywebview call it. The answer is known without the queries."""
    import re
    from pathlib import Path

    packaging = Path(__file__).parent.parent / "packaging"
    script = (
        "import sys, time, platform\n"
        f"sys.path.insert(0, {str(packaging)!r})\n"
        "import argus_app\n"
        "argus_app.seed_platform()\n"
        "t = time.perf_counter()\n"
        "u = platform.uname()\n"
        "print('MS %.1f' % ((time.perf_counter() - t) * 1000))\n"
        "print('VAL', u.system, u.release, u.version, u.machine)\n"
    )
    seeded = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    ).stdout
    real = subprocess.run(
        [sys.executable, "-c",
         "import platform; u = platform.uname(); print('VAL', u.system, u.release, u.version, u.machine)"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout

    assert float(re.search(r"MS ([\d.]+)", seeded).group(1)) < 20, seeded
    assert re.search(r"VAL .*", seeded).group(0) == re.search(r"VAL .*", real).group(0)


def test_the_window_is_hidden_from_capture_before_anything_else_runs(desktop_env, monkeypatch):
    """The window is already on screen (even if with a splash), and it must not be in a screen
    share. Everything else, the prefetch and waiting for the server, comes after this: a
    prefetch started earlier shared the interpreter with this thread, and the window stayed
    visible in the capture a quarter of a second longer (measured: 263 ms versus 12)."""
    events = desktop_env.events
    monkeypatch.setattr(desktop, "wait_until_serving", lambda *a, **kw: events.append("serving") or True)
    monkeypatch.setattr(
        desktop, "hide_from_capture", lambda *a, **kw: events.append("hidden") or (True, "ok")
    )

    async def pipeline(stop):
        pass

    cfg = Config()
    cfg.ui.exclude_from_capture = True
    desktop.run_desktop(
        cfg, pipeline, 18999, path="", claimed=True, on_shown=lambda: events.append("on_shown"),
    )

    assert events.index("hidden") < events.index("on_shown")
    assert events.index("hidden") < events.index("serving")
