"""The application window: what can be tested without the window itself.

Showing the window is checked by hand; it cannot be opened automatically. But everything
around it is checked, and it breaks more quietly: the startup race, stopping on close,
a missing runtime.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from argus.desktop import available, wait_until_serving
from argus.runner import _stop_on_signal


def test_window_runtime_is_present():
    """Without this, `argus run` would silently fall back to the browser or crash at launch."""
    ok, detail = available()
    assert ok, detail
    assert "pywebview" in detail


async def test_waits_for_the_server_before_showing_the_window():
    """A window opened before the server shows a connection error, and that is all.

    The user has nothing to reload the page with: the application has no address bar. In a
    browser this race was invisible, because the person pressed F5.
    """
    from websockets.asyncio.server import serve as ws_serve

    async def handler(ws):
        pass

    # Any listening socket will do: the fact of an answer is checked, not its body.
    async with ws_serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        ok = await asyncio.to_thread(
            wait_until_serving, f"http://127.0.0.1:{port}", 5.0
        )
    assert ok


async def test_gives_up_when_nothing_ever_answers():
    """Otherwise launch would hang forever without saying a word."""
    ok = await asyncio.to_thread(wait_until_serving, "http://127.0.0.1:1", 0.5)
    assert not ok


async def test_second_copy_does_not_start_a_second_pipeline(monkeypatch):
    """Two instances do not get along, and not only because of the port.

    WebView2 holds the profile as a directory and does not give it to a second process: the
    window will open, but the page will never appear in it, and this will be visible only in
    the log. So we ask in advance and say it in words.

    "Already running" here is not a listening port (that used to be checked
    with an HTTP poll, with a race on a fast restart; see `_claim_single_instance`) but a
    taken mutex: the same one that a real first instance would take. We take it ourselves,
    playing the first instance, and `run_desktop` must see the refusal and not bring up the
    pipeline at all.

    We do not look for the "first instance" window here (`_focus_existing_window` walks
    over all windows of the system, and the real Argus on this machine has the same title,
    so the test would bring its window to the front), and there is no point in waiting for
    its exit as long as in real life.
    """
    import argus.desktop as desktop
    from argus.core.config import Config
    from argus.desktop import _claim_single_instance, _instance_mutex_name, run_desktop

    monkeypatch.setattr(desktop, "_focus_existing_window", lambda title, include_hidden=False: False)
    monkeypatch.setattr(desktop, "TAKEOVER_WAIT_S", 0.3)

    port = 18420
    assert _claim_single_instance(_instance_mutex_name(port)), (
        "the test itself had to take the mutex first — otherwise it is no longer the \"first instance\""
    )

    started = []

    async def never(stop):
        started.append(True)

    code = await asyncio.to_thread(run_desktop, Config(), never, port)

    assert code == 0
    assert not started, "the second instance brought up the pipeline after all"


def test_claiming_the_same_name_twice_only_succeeds_once():
    """The mutex hands out to exactly one — the very guarantee, without a race."""
    from argus.desktop import _claim_single_instance

    name = "ArgusSingleInstance-test-only"
    assert _claim_single_instance(name) is True
    assert _claim_single_instance(name) is False


def test_hiding_is_on_unless_switched_off():
    """The point of the product is an assistant that the interlocutor does not see: a forgotten
    checkbox would give it away in the very first screen share. So capture protection is on,
    and it is turned off explicitly (2026-10-14; earlier the code default was the opposite,
    and only `argus.toml` turned it on)."""
    from argus.core.config import Config

    assert Config().ui.exclude_from_capture is True


def test_the_shipped_settings_agree_with_the_code_default():
    """`argus.toml` is put into the build as is: its value must not diverge from the code
    default, otherwise "built" and "from source" behave differently."""
    import tomllib
    from pathlib import Path

    from argus.core.config import Config

    shipped = tomllib.loads((Path(__file__).resolve().parents[1] / "argus.toml").read_text(encoding="utf-8"))
    ui = Config().ui
    assert shipped["ui"]["exclude_from_capture"] is ui.exclude_from_capture
    assert shipped["ui"]["main_taskbar_button"] is ui.main_taskbar_button


def test_the_main_window_keeps_its_taskbar_button_by_default():
    """The main window's taskbar button is the only way back to a minimized window, so by
    default it is there; removing it is a conscious choice by the person
    (`ui.main_taskbar_button`)."""
    from argus.core.config import Config

    assert Config().ui.main_taskbar_button is True


def test_failure_to_hide_is_never_silent():
    """A silent failure is worse here than the absence of the feature.

    The person is sure the hints are not visible and shares their screen. So what is returned
    is not "done" but what exactly happened, and the reason is never empty.
    """
    from argus.desktop import hide_from_capture

    ok, detail = hide_from_capture(timeout=0.2)
    assert isinstance(ok, bool)
    assert detail.strip(), "a refusal must explain itself"


def test_own_windows_asks_the_system_and_survives_having_none():
    """A process may have no windows at all — that is not an error."""
    from argus.desktop import own_windows

    handles = own_windows()
    assert isinstance(handles, list)
    assert all(isinstance(h, int) for h in handles)


def test_swapping_the_caption_never_renames_the_window():
    """Its own title bar instead of the system one (requirement, 2026-10-07), and the window
    title stays the same.

    The title must not be changed: the second launch finds the first window by it, it is in
    Alt+Tab, and the application hides its windows from capture by it. So the style and the
    frame color are changed, and nothing is done if there is no window with that title.
    """
    import inspect

    from argus import desktop

    assert desktop.use_page_caption("no such window in this process", "#0a0c10") is False
    for fn in (desktop.use_page_caption, desktop.restore_native_caption, desktop.main_window_action):
        assert "SetWindowText" not in inspect.getsource(fn), "the window title must not be touched"
    assert desktop.WS_CAPTION == 0x00C00000
    assert desktop.WS_THICKFRAME & ~desktop.WS_CAPTION, "the resize frame stays"


def test_the_main_window_actions_without_a_window_answer_not_ok():
    from argus import desktop

    saved = dict(desktop._MAIN_TITLE)
    desktop._MAIN_TITLE["t"] = "no such window in this process"
    try:
        assert desktop.main_window_action("state")["ok"] is False
        assert desktop.main_window_action("close")["ok"] is False
        assert desktop.main_window_action("take_off")["ok"] is False
    finally:
        desktop._MAIN_TITLE.update(saved)


def test_frame_color_is_the_page_color():
    """The frame band at the top is painted with the page color, to blend with the title strip."""
    from argus import desktop

    assert desktop._colorref("#0a0c10") == 0x100C0A
    assert desktop._colorref("#ff8000") == 0x0080FF


def test_profile_lives_where_it_may_be_written():
    """Not next to the program: Program Files cannot be written without rights, and then the
    window does not open at all."""
    from argus.desktop import storage_dir

    path = storage_dir()
    assert path.is_dir()
    assert path.name == "webview"


async def test_closing_the_window_asks_the_server_to_finish():
    """The window was closed: the server is asked to finish, not cancelled.

    Cancelling the task breaks out as a traceback from uvicorn, and a normal window close
    would look like a crash. In a terminal this is noise, and in a built application it is
    the only thing that will remain in the log.
    """
    closed = threading.Event()
    bye = asyncio.Event()
    server = asyncio.create_task(asyncio.sleep(30))
    watcher = asyncio.create_task(_stop_on_signal(closed, server, bye))

    closed.set()
    await asyncio.wait_for(bye.wait(), timeout=2.0)
    assert not server.cancelled()
    watcher.cancel()
    server.cancel()


async def test_without_a_polite_way_out_the_task_is_cancelled():
    """The old path remains: not every server can finish on request."""
    closed = threading.Event()
    server = asyncio.create_task(asyncio.sleep(30))
    watcher = asyncio.create_task(_stop_on_signal(closed, server))

    closed.set()
    with pytest.raises(asyncio.CancelledError):
        await server
    watcher.cancel()


async def test_watcher_lets_go_when_the_server_ends_first():
    """The pipeline can end on its own, by time limit or because a device disappeared.

    The watcher must notice this and leave: otherwise it keeps spinning until the end of the
    process and holds up the exit.
    """
    closed = threading.Event()
    server = asyncio.create_task(asyncio.sleep(0))
    await server
    await asyncio.wait_for(_stop_on_signal(closed, server), timeout=2.0)
