"""Updating from the running app: the service, its routes and what it gives to the page.

Download and swap are checked in `test_update.py` and `test_mac_update.py`; here we check
what the button does: when it is there, what happens on a press and what happens on
failure.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from argus import update, update_service
from argus.core.config import Config
from argus.update_service import UpdateService


def release(version="1.0+new", build=2000) -> update.Release:
    return update.Release(version=version, zip="Argus-win.zip", sha256="a" * 64, size=10, build=build)


@pytest.fixture
def svc(monkeypatch, tmp_path):
    """The service of the built app with an update server; the network and the installation are
    stubbed.
    """
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update_service, "QUIT_DELAY_S", 0.0)
    cfg = Config()
    cfg.update.server = "http://example.invalid"
    cfg.root = tmp_path
    quit_calls = []
    service = UpdateService(cfg, running=lambda: False, quit_app=lambda: quit_calls.append(1))
    service.quit_calls = quit_calls
    monkeypatch.setattr(update, "cannot_install_reason", lambda root: "")
    return service


def wait_for(service: UpdateService, phase: str, timeout: float = 3.0) -> None:
    done = threading.Event()

    def poll():
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end and service.phase != phase:
            time.sleep(0.01)
        done.set()

    threading.Thread(target=poll, daemon=True).start()
    done.wait(timeout + 1)
    assert service.phase == phase


def test_without_a_server_or_in_a_dev_run_there_is_nothing_to_offer(monkeypatch, tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    assert UpdateService(cfg).snapshot()["available"] is False       # no address
    cfg.update.server = "http://example.invalid"
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    dev = UpdateService(cfg)
    assert dev.check_now() is False and dev.snapshot()["available"] is False and dev.start_install() is False


def test_no_release_means_no_button(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: None)
    assert svc.check_now() is False
    state = svc.snapshot()
    assert state["available"] is False and state["can_install"] is False and state["phase"] == "idle"


def test_a_new_release_shows_the_button(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    assert svc.check_now() is True
    state = svc.snapshot()
    assert state["available"] is True and state["can_install"] is True and state["busy"] is False


def test_the_page_never_gets_versions_or_reasons(svc, monkeypatch):
    """Nothing technical goes out: the page draws a button, not a report (reasons go only to the
    log).
    """
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    assert set(svc.snapshot()) == {"available", "phase", "percent", "can_install", "busy"}


def test_where_installing_is_impossible_the_button_offers_the_download_page(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    monkeypatch.setattr(update, "cannot_install_reason", lambda root: "run from Downloads")
    svc.check_now()
    state = svc.snapshot()
    assert state["available"] is True and state["can_install"] is False
    assert svc.start_install() is False
    opened = []
    monkeypatch.setattr(update_service.webbrowser, "open", lambda url: opened.append(url) or True)
    assert svc.open_download_page() is True
    assert opened == ["http://example.invalid/download"]


def test_a_running_recognition_is_reported_so_the_page_can_ask_first(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.running = lambda: True
    svc.check_now()
    assert svc.snapshot()["busy"] is True


def test_the_install_downloads_hands_over_and_closes_the_app(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    seen = {}

    def fake_install(server, rel, root, progress=None):
        seen["args"] = (server, rel.build, root)
        progress(5, 10)
        progress(10, 10)
        return True

    monkeypatch.setattr(update, "install_release", fake_install)
    assert svc.start_install() is True
    deadline = threading.Event()
    deadline.wait(0.5)
    assert seen["args"] == ("http://example.invalid", 2000, svc.root)
    assert svc.phase == "installing" and svc.percent == 100
    assert svc.quit_calls == [1], "the windows are closed, the swap waits for the process to exit"


def test_progress_is_a_percent_of_the_signed_size(svc, monkeypatch):
    svc.release = release()
    svc._progress(5, 10)
    assert svc.percent == 50 and svc.phase == "idle"
    svc._progress(10, 10)
    assert svc.percent == 100 and svc.phase == "installing"


def test_a_failed_install_leaves_the_app_running_and_says_so(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()

    def broken(server, rel, root, progress=None):
        raise update.UpdateError("the downloaded archive doesn't match the signed description")

    monkeypatch.setattr(update, "install_release", broken)
    assert svc.start_install() is True
    wait_for(svc, "failed")
    assert svc.quit_calls == [], "the app does not close if there is nothing to install"
    assert svc.snapshot()["available"] is True, "the button stays: one can try again or download by hand"
    assert "doesn't match" in svc.failed_reason


def test_an_unexpected_crash_in_the_install_is_a_failure_not_a_dead_thread(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    monkeypatch.setattr(update, "install_release", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("something")))
    svc.start_install()
    wait_for(svc, "failed")


def test_a_swap_that_did_not_start_is_a_failure(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    monkeypatch.setattr(update, "install_release", lambda *a, **k: False)
    svc.start_install()
    wait_for(svc, "failed")
    assert svc.quit_calls == []


def test_a_second_press_while_installing_does_nothing(svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    gate = threading.Event()
    started = []

    def slow(server, rel, root, progress=None):
        started.append(1)
        gate.wait(2)
        return False

    monkeypatch.setattr(update, "install_release", slow)
    assert svc.start_install() is True
    assert svc.start_install() is False
    gate.set()
    wait_for(svc, "failed")
    assert started == [1]


def test_the_background_check_runs_after_a_delay_and_survives_errors(svc, monkeypatch):
    monkeypatch.setattr(update_service, "FIRST_CHECK_S", 0.01)
    monkeypatch.setattr(update_service, "CHECK_EVERY_S", 0.01)
    calls = []

    def flaky(timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("network")
        return False

    monkeypatch.setattr(svc, "check_now", flaky)

    async def go():
        task = asyncio.create_task(svc.run())
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(go())
    assert len(calls) >= 2, "the check did not die after the error"


def test_a_disabled_service_never_polls(monkeypatch, tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    service = UpdateService(cfg)
    monkeypatch.setattr(update_service, "FIRST_CHECK_S", 0.0)
    asyncio.run(asyncio.wait_for(service.run(), 1))          # returns at once, does not spin forever


def test_the_exit_falls_back_to_a_signal_when_there_is_no_window(monkeypatch):
    from argus import desktop

    monkeypatch.setattr(desktop, "_QUIT", None)
    raised = []
    monkeypatch.setattr(update_service.signal, "raise_signal", lambda sig: raised.append(sig))
    update_service._quit_app()
    assert raised, "in web mode there is nothing to close the windows with: the process stops as on Ctrl+C"


def test_the_exit_closes_the_windows_when_there_are_some(monkeypatch):
    from argus import desktop

    closed = []
    monkeypatch.setattr(desktop, "_QUIT", lambda: closed.append(1))
    update_service._quit_app()
    assert closed == [1] and desktop.quit_app() is True


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@pytest.fixture
def client(svc):
    web = FastAPI()
    update_service.attach(web, svc)
    return TestClient(web)


def test_the_state_route(client, svc, monkeypatch):
    assert client.get("/update/state").json()["available"] is False
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    assert client.get("/update/state").json()["available"] is True


def test_the_install_route_starts_the_install(client, svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    monkeypatch.setattr(update, "install_release", lambda *a, **k: False)
    answer = client.post("/update/install", json={}).json()
    assert answer["ok"] is True and "phase" in answer
    wait_for(svc, "failed")


def test_a_failed_install_can_be_retried(client, svc, monkeypatch):
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    monkeypatch.setattr(update, "install_release", lambda *a, **k: False)
    client.post("/update/install", json={})
    wait_for(svc, "failed")
    attempts = []
    monkeypatch.setattr(update, "install_release", lambda *a, **k: attempts.append(1) or False)
    assert client.post("/update/install", json={}).json()["ok"] is True
    wait_for(svc, "failed")
    assert attempts == [1]


def test_the_install_route_without_a_release_does_nothing(client):
    assert client.post("/update/install", json={}).json()["ok"] is False


def test_the_server_does_not_trust_the_pages_old_idea_of_a_running_recording(client, svc, monkeypatch):
    """The page decides by the "recording is running" flag, which can be up to a minute old:
    recording was just turned on, and without confirmation the server does not close windows
    but asks the page to ask the person."""
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    started = []
    monkeypatch.setattr(update, "install_release", lambda *a, **k: started.append(1) or False)
    svc.running = lambda: True
    answer = client.post("/update/install", json={}).json()
    assert answer["ok"] is False and answer["confirm"] is True and answer["busy"] is True
    assert not started and svc.phase == "idle"
    assert client.post("/update/install", json={"confirm": False}).json()["ok"] is False
    assert client.post("/update/install", json={"confirm": "yes"}).json()["ok"] is False, "only a real true"
    assert not started
    assert client.post("/update/install", json={"confirm": True}).json()["ok"] is True
    wait_for(svc, "failed")
    assert started == [1]


def test_a_failure_is_forgotten_when_a_different_release_comes_out(svc, monkeypatch):
    """Previously a single failure left "Failed — download" in place until
    restart, even when the next release came out.
    """
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release(build=2000))
    svc.check_now()
    monkeypatch.setattr(update, "install_release", lambda *a, **k: (_ for _ in ()).throw(update.UpdateError("network")))
    svc.start_install()
    wait_for(svc, "failed")
    svc.check_now()                                           # the same release: the failure stays
    assert svc.phase == "failed"
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release("1.0+newer", build=2001))
    svc.check_now()
    assert svc.phase == "idle" and svc.snapshot()["can_install"] is True


def test_the_post_routes_need_a_json_body_so_a_foreign_page_cannot_press_the_button(client, svc, monkeypatch):
    """A form from a foreign page in a browser goes out without JSON: the app must not restart
    because of it.
    """
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    for path in ("/update/install", "/update/open"):
        assert client.post(path, data="x=1", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 422
        assert client.post(path).status_code == 422
    assert svc.phase == "idle"


def test_a_json_body_sent_the_way_a_foreign_page_can_send_it_is_refused_too(client, svc, monkeypatch):
    """A foreign page without a preflight request can only send something simple: a form,
    `text/plain`, or a body with no type. None of the three must press the button. This is
    held by the strict Content-Type check in FastAPI itself (hence the lower bound in
    pyproject is the release on which this was verified), and a test holds it, not hope."""
    monkeypatch.setattr(update, "check_release", lambda server, root, timeout=2.0: release())
    svc.check_now()
    started = []
    monkeypatch.setattr(update, "install_release", lambda *a, **k: started.append(1) or False)
    for path in ("/update/install", "/update/open"):
        assert client.post(path, content='{"confirm": true}', headers={"content-type": "text/plain"}).status_code == 422
        assert client.post(path, content='{"confirm": true}').status_code == 422, "a JSON body without a type"
        assert client.post(path, data={"confirm": "true"}).status_code == 422, "an ordinary form"
        assert client.post(path, content='{"confirm": true}',
                           headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 422
    assert not started and svc.phase == "idle"


def test_the_open_route(client, monkeypatch):
    opened = []
    monkeypatch.setattr(update_service.webbrowser, "open", lambda url: opened.append(url) or True)
    assert client.post("/update/open", json={}).json() == {"ok": True}
    assert opened == ["http://example.invalid/download"]


def test_the_real_server_has_the_update_routes():
    src = (Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "server.py").read_text(encoding="utf-8")
    assert "attach_update(web, updater)" in src and "updater.run()" in src


# --------------------------------------------------------------------------
# Previewing the button during development
# --------------------------------------------------------------------------


def test_the_preview_shows_the_button_in_a_dev_run_and_fakes_the_install(monkeypatch, tmp_path):
    monkeypatch.setenv(update_service.PREVIEW_ENV, "1")
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    monkeypatch.setattr(update_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(update, "install_release", lambda *a, **k: (_ for _ in ()).throw(AssertionError("installs nothing")))
    cfg = Config()
    cfg.root = tmp_path
    closed = []
    service = UpdateService(cfg, quit_app=lambda: closed.append(1))
    assert service.snapshot()["available"] is True and service.snapshot()["can_install"] is True
    assert service.check_now() is True
    assert service.start_install() is True
    deadline = threading.Event()
    deadline.wait(0.3)
    assert service.phase == "idle" and service.percent is None, "the progress was shown and returned to the initial state"
    assert closed == [], "the app does not close"


def test_a_built_app_ignores_the_preview_variable(monkeypatch, tmp_path):
    monkeypatch.setenv(update_service.PREVIEW_ENV, "1")
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    cfg = Config()
    cfg.root = tmp_path
    assert UpdateService(cfg).snapshot()["available"] is False
