"""Leftovers from the 2026-10-14 audit: a recording that was asked for, the remote control
in a worker thread, the IPv6 address.

* `argus run --record --start --seconds N`: if the recording did not open, recognition
  is not switched on and the time limit does not hang forever (before, paid recognition
  ran until Ctrl+C);
* `/control` switches recognition on and off in a worker thread (cleaning up old
  recordings and closing a recording are slow), so `set_running` and `recording_began`
  are designed to be called from any thread;
* the dashboard address with `ui.host = "::1"` goes in brackets.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import numpy as np
import pytest
from fastapi.testclient import TestClient

from argus import runner
from argus.app import Argus
from argus.context.store import TimelineStore
from argus.context.timeline import Entry, Kind
from argus.core.config import Config, UIConfig
from argus.core.events import AudioChunk, Source
from argus.ui.server import build


def make_app(tmp_path, *, record: bool = False, always: bool = True) -> Argus:
    cfg = Config()
    cfg.root = tmp_path
    cfg.paths.sessions = tmp_path / "sessions"
    cfg.record.always = always
    cfg.ui.admin = True
    cfg.legal.notice = False                 # the first switch-on does not ask about the notice
    return Argus(cfg, with_screen=False, with_brain=False, record=record)


def boom(*a, **k):
    raise OSError(28, "No space left on device")


# --------------------------------------------------------------------------
# A recording that was asked for explicitly
# --------------------------------------------------------------------------


def test_a_recording_asked_for_by_flag_that_cannot_open_keeps_recognition_off(tmp_path, monkeypatch):
    """`argus run --record`: a run without a recording is not the one it was started for, and
    recognition is paid.
    """
    app = make_app(tmp_path, record=True, always=False)
    monkeypatch.setattr("argus.app.Recorder", boom)

    app.set_running(True)

    assert app.running is False and app.speech.paused is True
    assert app.recorder is None
    assert app.record_error.startswith("didn't start")
    assert app.controls()["running"] is False


def test_a_recording_that_is_only_on_by_setting_does_not_stop_recognition(tmp_path, monkeypatch):
    """The "always record" setting is not a request: turning on recognition
    matters more than recording it (the earlier decision).
    """
    app = make_app(tmp_path, record=False, always=True)
    monkeypatch.setattr("argus.app.Recorder", boom)

    app.set_running(True)

    assert app.running is True and app.speech.paused is False
    assert app.recorder is None and app.record_error.startswith("didn't start")


def test_the_recording_that_opens_fine_is_unchanged_for_a_required_one(tmp_path):
    app = make_app(tmp_path, record=True, always=False)
    app.set_running(True)
    try:
        assert app.running is True and app.recorder is not None
        assert app.recording_began.is_set()
    finally:
        app.set_running(False)


class _Server:
    """A server task that lives until it is cancelled."""

    def __init__(self) -> None:
        self.task = asyncio.create_task(asyncio.sleep(3600))

    async def cancelled(self) -> bool:
        await asyncio.sleep(0.05)
        return self.task.cancelled()


def _fake_app(**kw):
    base = {
        "_record": True, "running": False, "record_required": True, "record_error": "",
        "recording_began": asyncio.Event(), "session_dir": None, "pruned": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_the_timer_gives_up_when_the_requested_recording_cannot_open():
    """Previously `_stop_after` waited for `recording_began` forever, and paid recognition ran
    until Ctrl+C.
    """
    app = _fake_app(record_error="didn't start — check the disk space")
    server = _Server()

    await asyncio.wait_for(runner._stop_after(app, server.task, 600), 3)

    assert await server.cancelled(), "the run ends instead of hanging"


async def test_the_timer_waits_while_the_recording_may_still_start():
    """Recognition is off, there is no error — the person is still choosing a window: we wait
    rather than finish.
    """
    app = _fake_app()
    server = _Server()
    waiting = asyncio.create_task(runner._stop_after(app, server.task, 0.2))
    try:
        await asyncio.sleep(0.5)
        assert not waiting.done()

        app.recording_began.set()                       # the button was pressed and the recording started
        await asyncio.wait_for(waiting, 3)               # the time limit expired after 0.2 s
        assert await server.cancelled()
    finally:
        waiting.cancel()
        server.task.cancel()


async def test_without_a_recording_the_timer_counts_from_the_recognition():
    """`--seconds` without `--record`: the "recording started" event will never come, and the
    deadline never came due at all.
    """
    app = _fake_app(_record=False, record_required=False)
    server = _Server()
    waiting = asyncio.create_task(runner._stop_after(app, server.task, 0.2))
    try:
        await asyncio.sleep(0.5)
        assert not waiting.done(), "while recognition is off, the deadline does not run"

        app.running = True
        await asyncio.wait_for(waiting, 3)
        assert await server.cancelled()
    finally:
        waiting.cancel()
        server.task.cancel()


async def test_the_timer_ends_quietly_when_the_server_died_first():
    app = _fake_app()
    dead = asyncio.create_task(asyncio.sleep(0))
    await dead

    await asyncio.wait_for(runner._stop_after(app, dead, 600), 3)


# --------------------------------------------------------------------------
# The remote control in a worker thread
# --------------------------------------------------------------------------


async def test_recording_began_is_signalled_from_a_worker_thread(tmp_path):
    """`asyncio.Event.set()` from another thread wakes nothing: `_stop_after` would wait
    forever.
    """
    app = make_app(tmp_path)
    app._loop = asyncio.get_running_loop()
    waiter = asyncio.create_task(asyncio.wait_for(app.recording_began.wait(), 3))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await asyncio.to_thread(app.set_running, True)
    try:
        await waiter                                       # it woke up, it did not time out
        assert app.recorder is not None
    finally:
        await asyncio.to_thread(app.set_running, False)


def test_two_quick_presses_open_one_recording(tmp_path):
    """The control panel now calls `set_running` from threads: a double click must not open two
    recordings.
    """
    app = make_app(tmp_path)
    opened: list[int] = []
    real = app._open_session

    def slow_open() -> None:
        opened.append(1)
        time.sleep(0.05)
        real()

    app._open_session = slow_open
    threads = [threading.Thread(target=app.set_running, args=(True,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    try:
        assert len(opened) == 1
        assert app.recorder is not None and app.running is True
    finally:
        app.set_running(False)
    assert app.recorder is None


def test_the_audio_pump_skips_a_chunk_instead_of_waiting_for_a_closing_recording(tmp_path):
    """Closing a recording in a worker thread can take seconds; the event loop must not stall
    because of it.
    """
    app = make_app(tmp_path)
    fed: list[str] = []
    app.recorder = SimpleNamespace(feed_audio=fed.append)
    held, release = threading.Event(), threading.Event()

    def closing() -> None:
        with app._rec_lock:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=closing)
    thread.start()
    assert held.wait(2)
    began = time.monotonic()
    app._feed_recorder("feed_audio", "at the moment of closing")
    waited = time.monotonic() - began
    release.set()
    thread.join(5)

    assert waited < 0.5 and fed == [], "the chunk was skipped, the pump did not wait"
    app._feed_recorder("feed_audio", "after")
    assert fed == ["after"]
    app.recorder = None


async def test_a_pump_never_feeds_a_recording_that_was_replaced_under_it(tmp_path):
    """Closed and opened again while the pump held the old recording — the old one no longer
    gets a chunk.
    """
    app = make_app(tmp_path)
    old, new = [], []
    app.recorder = SimpleNamespace(feed_audio=old.append)
    stale = app.recorder
    app.recorder = SimpleNamespace(feed_audio=new.append)

    chunk = AudioChunk(t=0, source=Source.ME, samples=np.zeros(160, dtype=np.int16))
    app._feed_recorder("feed_audio", chunk)

    assert old == [] and len(new) == 1
    assert stale is not app.recorder
    app.recorder = None


def test_the_timeline_store_opened_in_one_thread_works_in_another(tmp_path):
    """The timeline database is opened by the control panel's worker thread, and the event loop
    writes to it.
    """
    box: dict[str, TimelineStore] = {}
    opener = threading.Thread(target=lambda: box.update(store=TimelineStore(tmp_path / "t.db")))
    opener.start()
    opener.join(5)
    store = box["store"]

    store.add(Entry(t=1, kind=Kind.SPEECH, source=Source.ME, text="one"))
    store.add_many([Entry(t=2, kind=Kind.SPEECH, source=Source.THEM, text="two")])
    store.commit()
    assert store.count() == 2

    closer = threading.Thread(target=store.close)
    closer.start()
    closer.join(5)


def test_control_hands_set_running_to_a_worker_thread(tmp_path, monkeypatch):
    """On the event loop, switching off the recording would stall for up to ten seconds: all
    windows and answers would freeze.
    """
    app = make_app(tmp_path)
    real = asyncio.to_thread
    handed: list[str] = []

    async def spy(fn, /, *args, **kwargs):
        handed.append(getattr(fn, "__name__", repr(fn)))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    client = TestClient(build(app))

    answer = client.post("/control", json={"running": False})

    assert answer.status_code == 200 and answer.json()["running"] is False
    assert "set_running" in handed


def test_control_turns_recognition_on_and_off_for_real(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(build(app))

    on = client.post("/control", json={"running": True})
    try:
        assert on.status_code == 200
        assert app.running is True
    finally:
        off = client.post("/control", json={"running": False})
    assert off.json()["running"] is False
    assert app.recorder is None


# --------------------------------------------------------------------------
# IPv6 address
# --------------------------------------------------------------------------


def test_the_dashboard_address_brackets_an_ipv6_host():
    ui = UIConfig(host="::1", port=8420)

    assert ui.base_url() == "http://[::1]:8420"
    assert ui.base_url(9000) == "http://[::1]:9000"
    assert urlsplit(ui.base_url()).hostname == "::1" and urlsplit(ui.base_url()).port == 8420


def test_the_dashboard_address_of_the_usual_hosts_is_unchanged():
    assert UIConfig().base_url() == "http://127.0.0.1:8420"
    assert UIConfig(host="localhost").base_url(8421) == "http://localhost:8421"
    assert Config().ui.base_url() == "http://127.0.0.1:8420"


@pytest.mark.parametrize("module", ["runner", "settings_only", "desktop"])
def test_no_url_is_built_from_the_bare_host(module):
    """The address is built by one method; these modules do not build it by hand (without
    brackets IPv6 breaks).
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "src" / "argus" / f"{module}.py").read_text(encoding="utf-8")
    assert "http://{cfg.ui.host}" not in text


# --------------------------------------------------------------------------
# Main window button on the taskbar: a switch in the settings
# --------------------------------------------------------------------------

def test_the_taskbar_switch_is_a_plain_setting_that_applies_at_once(tmp_path, monkeypatch):
    """An ordinary switch of the settings window: in the schema, with the default "the button is
    there", and an edit goes to the window at once (not after a restart). On macOS the window
    has no such button — the setting exists, but without a row."""
    app = make_app(tmp_path)
    client = TestClient(build(app))
    schema = client.get("/admin/schema").json()
    rows = {f["path"]: f for s in schema["sections"] for f in s["settings"]}
    row = rows["ui.main_taskbar_button"]
    assert row["kind"] == "bool" and row["hot"] is True
    assert row["hidden"] is (sys.platform == "darwin")
    assert schema["values"]["ui.main_taskbar_button"] is True

    asked: list[bool] = []
    monkeypatch.setattr("argus.desktop.set_main_taskbar_button", lambda shown: asked.append(shown) or True)

    answer = client.post("/admin/apply", json={"values": {"ui.main_taskbar_button": False}}).json()

    assert answer["ok"] is True and "ui.main_taskbar_button" in answer["changed"]
    assert app.cfg.ui.main_taskbar_button is False
    assert asked == [False]
    assert answer["restart"] == [], "no restart needed"

    # Another edit does not touch the button.
    client.post("/admin/apply", json={"values": {"channel.assist.interface.language": "en"}})
    assert asked == [False]

    # Reset brings the button back (the default is: present).
    client.post("/admin/reset", json={"paths": ["ui.main_taskbar_button"]})
    assert app.cfg.ui.main_taskbar_button is True
    assert asked == [False, True]


def test_setting_the_button_without_an_app_window_is_a_quiet_no_op(monkeypatch):
    """In a browser and in `argus run --settings` without a window there is nothing to switch:
    no exception, no thread.
    """
    from argus import desktop

    monkeypatch.setattr(desktop, "_MAIN_WINDOW", None)
    started: list[object] = []
    monkeypatch.setattr(desktop.threading, "Thread", lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))

    assert desktop.set_main_taskbar_button(False) is False
    assert started == []
