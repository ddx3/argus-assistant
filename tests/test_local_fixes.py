"""Local server: what broke silently after the audit of 2026-10-14.

The event loop must not stand on network and disk calls; the state broadcast must not
die from a single error; a foreign payment address must not open; file names must not
send data to an invisible place; the browser cache must not confuse frames of different
sessions.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from argus.app import Argus, Shot
from argus.assist import materials
from argus.core.config import Config
from argus.ui import admin, server
from argus.ui.server import build

ROOT = Path(__file__).resolve().parents[1]
JPEG = b"\xff\xd8\xff" + b"x" * 40


def make_app(tmp_path, *, account: bool = False) -> Argus:
    cfg = Config()
    cfg.root = tmp_path                  # chats and materials are in a temporary folder, not in the working copy
    cfg.ui.admin = True
    if account:
        cfg.account.server = "http://server"
    return Argus(cfg, with_screen=False, with_brain=False)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def ticks_while(coro) -> tuple[object, int]:
    """The result of `coro` and how many times a neighboring loop (every 10 ms) managed to fire
    in that time.

    A frozen loop gives zero or one tick; a live one over a 0.4 s wait gives about forty.
    """
    stop = asyncio.Event()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await task
    return result, ticks


def client_for(app: Argus) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build(app)), base_url="http://test")


# --------------------------------------------------------------------------
# The event loop does not stand on slow calls
# --------------------------------------------------------------------------


async def test_the_account_routes_do_not_freeze_the_event_loop(tmp_path, monkeypatch):
    """Every `Account` call is a synchronous request to the server that can take up to ten
    seconds. The page asks it on every load and every four seconds while it waits for payment."""
    from argus.account.client import Account

    def slow(self, *args, **kwargs):
        time.sleep(0.4)
        return {"signed_in": True, "email": "user@example.com", "right": None, "offline": False}

    for name in ("state", "pricing", "history", "usage"):
        monkeypatch.setattr(Account, name, slow)

    async with client_for(make_app(tmp_path, account=True)) as client:
        answers, ticks = await ticks_while(asyncio.gather(
            client.get("/admin/account"), client.get("/admin/account/pricing"),
            client.get("/admin/account/history"), client.get("/admin/account/usage"),
        ))

    assert all(a.status_code == 200 and a.json().get("signed_in") for a in answers)
    assert ticks >= 15, f"the event loop stood still during the requests to the account server (ticks: {ticks})"


async def test_the_state_changing_account_routes_do_not_freeze_the_loop_either(tmp_path, monkeypatch):
    from argus.account.client import Account

    def slow(self, *args, **kwargs):
        time.sleep(0.3)
        return {"signed_in": True, "email": "user@example.com", "right": None, "offline": False}

    for name in ("sign_in", "register", "accept_terms", "request_reset", "sign_out",
                 "change_password", "delete_account"):
        monkeypatch.setattr(Account, name, slow)
    monkeypatch.setattr(Account, "state", slow)

    async with client_for(make_app(tmp_path, account=True)) as client:
        calls = [
            client.post("/admin/account/sign-in", json={"email": "a@b.c", "password": "x"}),
            client.post("/admin/account/register", json={"email": "a@b.c", "password": "x"}),
            client.post("/admin/account/terms"),
            client.post("/admin/account/reset", json={"email": "a@b.c"}),
            client.post("/admin/account/sign-out"),
        ]
        answers, ticks = await ticks_while(asyncio.gather(*calls))

    assert all(a.status_code == 200 and a.json()["ok"] is True for a in answers)
    assert ticks >= 10, f"the event loop stood still (ticks: {ticks})"


async def test_the_audio_reopening_of_the_control_panel_runs_in_a_thread(tmp_path, monkeypatch):
    """`AudioCapture.reopen()` waits for the capture threads to stop and brings the device up
    again.
    """
    app = make_app(tmp_path)
    monkeypatch.setattr(app, "reopen_audio", lambda: time.sleep(0.4) or {})
    monkeypatch.setattr(app, "set_mic_device", lambda spec: time.sleep(0.4) or {})
    monkeypatch.setattr(app, "set_loopback_windows", lambda titles: time.sleep(0.4) or {})

    async with client_for(app) as client:
        for body in ({"reopen_audio": True}, {"mic_device": "Microphone"},
                     {"loopback_windows": ["Zoom"]}):
            answer, ticks = await ticks_while(client.post("/control", json=body))
            assert answer.status_code == 200, body
            assert ticks >= 15, f"{body}: the event loop stood still (ticks: {ticks})"


async def test_the_audio_reopenings_never_overlap(tmp_path, monkeypatch):
    """Before, two in a row (a double click on "reconnect sound") were lined
    up by the event loop itself, which stood blocked on each. In threads they would run at once:
    `close()` of one and `open()` of the other would bring up the capture threads twice."""
    app = make_app(tmp_path)
    running = peak = 0

    def reopen():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        time.sleep(0.1)
        running -= 1
        return {}

    monkeypatch.setattr(app, "reopen_audio", reopen)
    monkeypatch.setattr(app, "set_mic_device", lambda spec: reopen())
    async with client_for(app) as client:
        answers = await asyncio.gather(
            client.post("/control", json={"reopen_audio": True}),
            client.post("/control", json={"reopen_audio": True}),
            client.post("/control", json={"mic_device": "Microphone"}),
            client.post("/control", json={"reopen_audio": True}),
        )

    assert all(a.status_code == 200 for a in answers)
    assert peak == 1, f"the audio was reopened simultaneously in {peak} threads"


async def test_the_control_panel_rejects_a_window_list_that_is_not_a_list(tmp_path):
    async with client_for(make_app(tmp_path)) as client:
        answer = await client.post("/control", json={"loopback_windows": 5})
    assert answer.status_code == 400


async def test_the_materials_are_parsed_in_a_thread(tmp_path, monkeypatch):
    """A scanned PDF of hundreds of pages takes tens of seconds to render, and for all that time
    the windows and the sound stood still.
    """
    real = materials.load

    def slow(*args, **kwargs):
        time.sleep(0.4)
        return real(*args, **kwargs)

    monkeypatch.setattr(materials, "load", slow)
    async with client_for(make_app(tmp_path)) as client:
        answer, ticks = await ticks_while(
            client.post("/admin/assist/materials?name=a.md", content="lecture".encode()))
    assert answer.json()["ok"] is True
    assert ticks >= 15, f"the event loop stood still while parsing materials (ticks: {ticks})"


async def test_the_materials_are_parsed_one_request_at_a_time(tmp_path, monkeypatch):
    """Parsing moved from the loop into threads; two requests at once must not parse one folder
    together: cleaning the page cache of one would erase the fresh pages of the other."""
    running = peak = 0
    real = materials.load

    def slow(*args, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        time.sleep(0.15)
        running -= 1
        return real(*args, **kwargs)

    monkeypatch.setattr(materials, "load", slow)
    async with client_for(make_app(tmp_path)) as client:
        answers = await asyncio.gather(*(client.get("/admin/assist/materials") for _ in range(4)))

    assert all(a.json()["ok"] for a in answers)
    assert peak == 1, f"the folder was parsed by {peak} requests at once"


async def test_the_hotkey_capture_waits_in_a_thread(tmp_path, monkeypatch):
    keys = SimpleNamespace(suspend=lambda: time.sleep(0.4), resume=lambda: time.sleep(0.4))
    monkeypatch.setattr("argus.desktop.hotkeys", lambda: keys)
    async with client_for(make_app(tmp_path)) as client:
        for on in (True, False):
            answer, ticks = await ticks_while(client.post("/admin/hotkeys/capture", json={"on": on}))
            assert answer.json() == {"held": True}
            assert ticks >= 15, f"on={on}: the event loop stood still (ticks: {ticks})"


async def test_a_chat_copy_is_made_off_the_event_loop(tmp_path, monkeypatch):
    """A session copy copies the materials folder: hundreds of megabytes on the event loop."""
    from argus.chats import manager_of

    app = make_app(tmp_path)
    manager = manager_of(app)
    real = manager.duplicate

    def slow(chat_id):
        time.sleep(0.4)
        return real(chat_id)

    monkeypatch.setattr(manager, "duplicate", slow)
    async with client_for(app) as client:
        made = (await client.post("/chats", json={"name": "Interview"})).json()
        answer, ticks = await ticks_while(client.post(f"/chats/{made['chat']['id']}/duplicate"))

    assert answer.json()["ok"] is True
    assert ticks >= 15, f"the event loop stood still while copying a session (ticks: {ticks})"


# --------------------------------------------------------------------------
# State broadcast
# --------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, *, fail: bool = False, delay: float = 0.0, on_send=None) -> None:
        self.fail, self.delay, self.on_send = fail, delay, on_send
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        if self.on_send:
            self.on_send()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("window closed")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


async def test_the_push_drops_dead_and_stalled_windows_without_holding_up_the_rest():
    good, dead, stalled = FakeSocket(), FakeSocket(fail=True), FakeSocket(delay=30)
    clients = {good, dead, stalled}

    started = time.perf_counter()
    await server.push(clients, "snapshot", timeout=0.2)

    assert time.perf_counter() - started < 2, "the stalled window held up the others"
    assert good.sent == ["snapshot"] and clients == {good}
    assert stalled.closed, "the stalled window must be disconnected, not waited for"


async def test_a_window_connecting_during_the_push_does_not_break_it():
    """Before, a live set was iterated: a window connecting in the middle of a push broke the
    broadcast.
    """
    clients: set = set()
    newcomer = FakeSocket()
    first = FakeSocket(on_send=lambda: clients.add(newcomer))
    second = FakeSocket(on_send=lambda: clients.discard(first))
    clients |= {first, second}

    await server.push(clients, "x", timeout=1)

    assert first.sent == ["x"] and second.sent == ["x"]


async def test_a_background_task_that_crashes_is_logged_and_started_again(caplog):
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("went down")

    await asyncio.wait_for(server.supervise("checker", flaky, pause=0), 2)

    assert len(runs) == 3
    assert "went down" in caplog.text and "checker" in caplog.text


async def test_a_supervised_task_still_obeys_cancellation():
    async def forever():
        await asyncio.sleep(60)

    task = asyncio.create_task(server.supervise("eternal", forever, pause=0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_failing_snapshot_does_not_stop_the_broadcast(tmp_path, monkeypatch):
    """One error while building a snapshot killed the broadcast task: all the windows froze until a
    restart.
    """
    calls = []
    real = server.snapshot

    def flaky(app):
        calls.append(1)
        if len(calls) == 2:               # the first call is at connection, the second is the broadcast tick
            raise RuntimeError("the snapshot did not build")
        return real(app)

    monkeypatch.setattr(server, "snapshot", flaky)
    with (TestClient(build(make_app(tmp_path))) as client, ThreadPoolExecutor(1) as pool,
          client.websocket_connect("/ws") as ws):
        ws.receive_json()
        # Without a guard it would wait forever here, so there is a ceiling.
        assert "levels" in pool.submit(ws.receive_json).result(timeout=10)
    assert len(calls) >= 3


def test_the_number_of_windows_listening_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_CLIENTS", 1)
    with TestClient(build(make_app(tmp_path))) as client, client.websocket_connect("/ws") as first:
        assert "levels" in first.receive_json()
        with pytest.raises(WebSocketDisconnect) as info, client.websocket_connect("/ws"):
            pass
        assert info.value.code == 1013


# --------------------------------------------------------------------------
# Frames, window, questions
# --------------------------------------------------------------------------


def test_restored_screenshots_are_not_cached_because_their_numbers_repeat_in_every_chat(tmp_path):
    app = make_app(tmp_path)
    app.state.shots.append(Shot(t=-1_000_000_000, jpeg=JPEG))     # restored from a chat
    app.state.shots.append(Shot(t=1_760_000_000_000, jpeg=JPEG))  # just taken
    client = TestClient(build(app))

    restored = client.get("/shot/-1000000000.jpg")
    assert restored.status_code == 200 and restored.headers["cache-control"] == "no-store"
    fresh = client.get("/shot/1760000000000.jpg")
    assert "immutable" in fresh.headers["cache-control"]
    assert client.get("/shot/-2000000000.jpg").status_code == 404


def test_the_capture_toggle_needs_an_explicit_value(tmp_path, monkeypatch):
    """An empty POST "toggled blindly", which is how a foreign page made the windows visible in a
    screen share.
    """
    calls: list[bool] = []
    monkeypatch.setattr("argus.desktop.main_window", lambda: object())
    monkeypatch.setattr(
        "argus.desktop.set_capture_exclusion", lambda on: calls.append(on) or (True, ""))
    monkeypatch.setattr("argus.desktop.capture_excluded", lambda: bool(calls and calls[-1]))
    client = TestClient(build(make_app(tmp_path)))

    assert client.post("/window/capture").status_code == 422
    for body in ({}, {"excluded": None}, {"excluded": "yes"}, {"excluded": 1}):
        assert client.post("/window/capture", json=body).status_code == 400, body
    assert calls == [], "without an explicit value the windows must not switch"

    # An explicit value is set as is, both "hide" and "show".
    assert client.post("/window/capture", json={"excluded": True}).json()["excluded"] is True
    assert client.post("/window/capture", json={"excluded": False}).json()["excluded"] is False
    assert calls == [True, False]


def test_the_capture_toggle_says_so_when_there_is_no_app_window(tmp_path, monkeypatch):
    monkeypatch.setattr("argus.desktop.main_window", lambda: None)
    client = TestClient(build(make_app(tmp_path)))
    answer = client.post("/window/capture", json={"excluded": True}).json()
    assert answer == {"ok": False, "error": "only works in the app"}


def test_a_question_that_is_not_text_is_a_plain_400(tmp_path):
    client = TestClient(build(make_app(tmp_path)))
    for path in ("/ask", "/ask/screen"):
        for question in ([1, 2], 5, {"a": 1}):
            answer = client.post(path, json={"question": question})
            assert answer.status_code == 400, (path, question)
            assert "Traceback" not in answer.text and "split" not in answer.text


def test_the_debug_route_that_recorded_the_microphone_is_gone(tmp_path):
    """`POST /sample` recorded 15 seconds of the microphone and system sound, and no page called it
    (removed 2026-10-14). Debugging does not need to turn on the microphone on a request from
    the network, even a local one."""
    app = build(make_app(tmp_path))
    assert not [r for r in app.routes if str(getattr(r, "path", "")).startswith("/sample")]
    client = TestClient(app)
    assert client.post("/sample", json={"seconds": 1}).status_code in (404, 405)
    assert client.get("/sample/me.wav").status_code in (404, 405)
    from argus.app import Argus

    assert not hasattr(Argus, "record_sample")


def test_the_paid_and_recording_routes_need_a_body_even_if_the_guard_were_bypassed(tmp_path):
    """The second line of defense: an empty POST without `Content-Type` is the only thing a foreign
    page sends without the browser's permission. The page and the hotkeys always send a body."""
    client = TestClient(build(make_app(tmp_path)))
    for path in ("/ask", "/ask/screen", "/window/capture"):
        assert client.post(path).status_code == 422, path


def test_a_window_size_that_is_not_a_number_is_a_plain_400(tmp_path):
    client = TestClient(build(make_app(tmp_path)))
    for body in ({"panel": "screen", "width": "abc", "height": 300},
                 {"panel": "screen", "width": 400, "height": [1]}):
        assert client.post("/window/resize", json=body).status_code == 400, body
    # Python reads "Infinity" in JSON as a number, and `int()` of it fails with OverflowError.
    huge = client.post("/window/resize", headers={"Content-Type": "application/json"},
                       content='{"panel": "screen", "width": Infinity, "height": 300}')
    assert huge.status_code == 400


def test_apply_and_reset_reject_values_of_the_wrong_shape(tmp_path):
    client = TestClient(build(make_app(tmp_path)))
    for values in ("abc", 5, [1, 2]):
        assert client.post("/admin/apply", json={"values": values}).status_code == 400, values
    assert client.post("/admin/apply", json={"values": {}}).json()["ok"] is True
    assert client.post("/admin/reset", json={"paths": 5}).status_code == 400


# --------------------------------------------------------------------------
# Payment address from the server
# --------------------------------------------------------------------------

SERVER = "http://server"


@pytest.mark.parametrize("url", [
    r"C:\Windows\System32\cmd.exe",         # a path to a program: os.startfile would launch it
    r"\\attacker\share\x.exe",              # network path: leak of the password hash over SMB
    "search-ms:query=x&crumb=location:\\\\attacker\\share",
    "ms-msdt:/id PCWDiagnostic",
    "file:///C:/Windows/System32/cmd.exe",
    "javascript:alert(1)",
    "http://user@server/pay",               # a login in the address is a host-substitution trick
    "http://server\\@evil.example/",        # the browser reads "\" as "/"
    "https:///nohost",
    "http://server/ pay",
    "http://server/pay\nhttp://evil.example",
    "", None, 5, ["http://server/"],
])
def test_a_payment_address_that_is_not_a_web_page_is_neither_shown_nor_opened(url):
    assert admin.pay_link(url, SERVER) == ("", False)


@pytest.mark.parametrize("url, opens", [
    ("http://server/billing/stub/7", True),                  # the account server itself, as configured
    ("http://SERVER:8421/billing/stub/7", True),
    ("https://server/billing/stub/7", True),
    ("https://www.liqpay.ua/api/3/checkout?data=1", True),    # a payment provider from the list
    ("http://www.liqpay.ua/api/3/checkout", False),           # to the payment provider only over https
    ("https://evil.example/pay", False),                      # it can be shown as text, but not opened
    ("http://evil.example/pay", False),
    ("https://server.evil.example/pay", False),
])
def test_only_our_server_and_the_cashier_are_opened(url, opens):
    shown, safe = admin.pay_link(url, SERVER)
    assert shown == url and safe is opens


def test_without_a_configured_server_nothing_is_opened():
    assert admin.pay_link("http://server/pay", "")[1] is False


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A payment page with a stub server answer; opening the browser is recorded."""
    import webbrowser

    from argus.account.client import Account

    opened: list[str] = []
    asked: list[int] = []
    reply: dict = {}

    def fake_checkout(self, dollars, lang=""):
        asked.append(dollars)
        return dict(reply)

    monkeypatch.setattr(Account, "checkout", fake_checkout)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    client = TestClient(build(make_app(tmp_path, account=True)))
    return SimpleNamespace(client=client, opened=opened, asked=asked, reply=reply)


def test_a_dangerous_payment_address_is_not_opened_and_the_server_cannot_override_our_fields(checkout):
    checkout.reply.update(order=3, ok=False, opened=True, pay_url=r"C:\Windows\System32\cmd.exe")
    answer = checkout.client.post("/admin/account/checkout", json={"dollars": 5}).json()

    assert checkout.opened == []
    assert answer["ok"] is True and answer["opened"] is False
    assert answer["pay_url"] == "" and answer["order"] == 3


def test_our_own_payment_page_is_opened_in_the_browser(checkout):
    checkout.reply.update(order=4, pay_url="http://server/billing/stub/4")
    answer = checkout.client.post("/admin/account/checkout", json={"dollars": 5}).json()

    assert checkout.opened == ["http://server/billing/stub/4"]
    assert answer["opened"] is True and answer["pay_url"] == "http://server/billing/stub/4"
    assert checkout.asked == [5]


def test_a_foreign_web_address_is_shown_as_text_but_not_opened(checkout):
    checkout.reply.update(order=5, pay_url="https://evil.example/pay")
    answer = checkout.client.post("/admin/account/checkout", json={"dollars": 5}).json()

    assert checkout.opened == []
    assert answer["opened"] is False and answer["pay_url"] == "https://evil.example/pay"


def test_an_unreadable_order_answer_gets_a_plain_error_not_a_crash(checkout, monkeypatch):
    """A placeholder page instead of the payment provider's answer: JSON parsing fails with
    ValueError. Before, this was caught by a general `except`, and the person got a phrase, not
    an empty 500 response."""
    import json

    from argus.account.client import Account

    def broken(self, dollars, lang=""):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(Account, "checkout", broken)
    answer = checkout.client.post("/admin/account/checkout", json={"dollars": 5})
    assert answer.status_code == 200
    body = answer.json()
    assert body["ok"] is False and body["error"] and "Expecting" not in body["error"]
    assert checkout.opened == []

    monkeypatch.setattr(Account, "checkout", lambda self, dollars, lang="": ["not", "an order"])
    body = checkout.client.post("/admin/account/checkout", json={"dollars": 5}).json()
    assert body["ok"] is False and "error" in body
    assert checkout.opened == []


def test_a_sum_that_is_not_a_whole_number_gets_a_plain_answer(checkout):
    for dollars in ("abc", [5], True, 5.5, None, {"a": 1}):
        answer = checkout.client.post("/admin/account/checkout", json={"dollars": dollars}).json()
        assert answer == {"ok": False, "error": "the amount must be a whole number of dollars"}, dollars
    assert checkout.asked == []
    checkout.reply.update(order=6, pay_url="")
    assert checkout.client.post("/admin/account/checkout", json={"dollars": "7"}).json()["ok"]
    assert checkout.asked == [7]


# --------------------------------------------------------------------------
# Uploading materials
# --------------------------------------------------------------------------


@pytest.fixture
def uploads(tmp_path):
    app = make_app(tmp_path)
    return SimpleNamespace(
        app=app, client=TestClient(build(app)), folder=materials.directory(app.cfg))


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.mark.parametrize("name", [
    "a.pdf:evil.pdf",            # an NTFS alternate stream: the data would go to an invisible place
    "a\x00b.txt",                # NUL crashed the write
    "a\nb.txt", "a<b.txt", 'a"b.txt', "a|b.txt", "a?b.txt", "a*b.txt",
    "con.txt", "NUL.md", "Lpt1.txt", "com3.pdf",     # Windows device names
    "report.txt.", "report.txt ",                    # Windows silently drops a trailing dot and space
])
def test_a_file_name_that_hides_data_or_means_a_device_is_refused(uploads, name):
    answer = uploads.client.post("/admin/assist/materials", params={"name": name}, content=b"data")
    assert answer.status_code == 200 and answer.json()["ok"] is False, name
    assert leftovers(uploads.folder) == []


@pytest.mark.parametrize("name", [
    "café-lecture.md", "Notes, part 1 (2).md", "notes-v2_final.txt", "O'Reilly & Co #3.md",
])
def test_an_ordinary_file_name_goes_through(uploads, name):
    answer = uploads.client.post("/admin/assist/materials", params={"name": name}, content=b"data")
    assert answer.json()["ok"] is True, name
    assert (uploads.folder / name).read_bytes() == b"data"
    assert [n for n in leftovers(uploads.folder) if n.endswith(".part")] == []


def test_an_upload_over_the_limit_is_refused_by_its_declared_size_and_by_its_actual_size(
        uploads, monkeypatch):
    monkeypatch.setattr(admin, "MAX_UPLOAD", 1000)
    declared = uploads.client.post("/admin/assist/materials?name=big.md", content=b"x" * 2000)
    assert declared.json()["ok"] is False and "MB" in declared.json()["error"]

    # Without a size header (in chunks): what really arrived is counted.
    def chunks():
        for _ in range(5):
            yield b"x" * 400

    streamed = uploads.client.post("/admin/assist/materials?name=big.md", content=chunks())
    assert streamed.json()["ok"] is False and "MB" in streamed.json()["error"]
    assert leftovers(uploads.folder) == [], "an unfinished file was left in the materials folder"

    exact = uploads.client.post("/admin/assist/materials?name=fits.md", content=b"x" * 1000)
    assert exact.json()["ok"] is True


def test_an_empty_upload_leaves_nothing_behind(uploads):
    answer = uploads.client.post("/admin/assist/materials?name=empty.md", content=b"")
    assert answer.json() == {"ok": False, "error": "the file is empty"}
    assert leftovers(uploads.folder) == []


def test_a_new_upload_replaces_the_old_file_of_the_same_name_in_one_step(uploads):
    uploads.client.post("/admin/assist/materials?name=a.md", content=b"first")
    uploads.client.post("/admin/assist/materials?name=a.md", content=b"second")
    assert (uploads.folder / "a.md").read_bytes() == b"second"
    assert leftovers(uploads.folder) == ["a.md"]


# --------------------------------------------------------------------------
# Port and launch
# --------------------------------------------------------------------------


@pytest.mark.windows_only
async def test_a_foreign_listener_that_allows_sharing_the_port_is_not_shared_with(tmp_path):
    """`http.server` sets SO_REUSEADDR: with the same flag on our side, Argus would bind alongside
    it and start without an error, while all connections would go to the foreign program, and
    the window would show its page, including the sign-in form."""
    port = free_port()
    with socket.socket() as other:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", port))
        other.listen(1)
        # With a ceiling: if the protection broke, the server would sit alongside and work
        # forever, and the test would hang.
        with pytest.raises(OSError, match="port is taken"):
            await asyncio.wait_for(
                server.serve(make_app(tmp_path), port=port, stop=asyncio.Event()), 5)


@pytest.mark.windows_only
async def test_the_port_can_be_taken_again_right_after_the_previous_run_closed_it(tmp_path):
    """This is what SO_REUSEADDR was once turned on for: a quick restart must not fail."""
    port = free_port()
    for _ in range(2):
        stop = asyncio.Event()
        task = asyncio.create_task(server.serve(make_app(tmp_path), port=port, stop=stop))
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            task.cancel()
            pytest.fail("the server did not come up")
        writer.write(f"GET /build HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        await reader.read(1024)
        stop.set()
        await asyncio.wait_for(task, 5)
        writer.close()


async def test_the_real_server_refuses_a_foreign_host_and_a_foreign_origin(tmp_path):
    """Protection on a live server: real header parsing and a real WebSocket handshake."""
    import websockets

    port = free_port()          # not 8420 from the settings: the port is taken from the request itself
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve(make_app(tmp_path), port=port, stop=stop))
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            for _ in range(100):
                try:
                    own = await http.get("/build")
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("the server did not come up")
            assert own.status_code == 200
            assert (await http.get("/build", headers={"Host": f"evil.example:{port}"})).status_code == 403
            assert (await http.post("/legal/notice", headers={"Origin": "http://evil.example"})
                    ).status_code == 403
            assert (await http.post("/legal/notice", headers={"Origin": f"http://localhost:{port}"})
                    ).status_code == 200
            assert (await http.post("/legal/notice")).status_code == 200      # like the hotkeys

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            assert "levels" in await ws.recv()
        with pytest.raises(websockets.exceptions.InvalidStatus) as refused:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin="http://evil.example"):
                pass
        assert refused.value.response.status_code == 403
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


# --------------------------------------------------------------------------
# Settings mode and recording without a screen
# --------------------------------------------------------------------------


def test_the_settings_mode_asks_whether_the_admin_page_exists_with_the_config():
    """Without the config, `has_admin()` does not know about `[ui] admin = false`, and the window
    opened on a 404.
    """
    for rel in ("src/argus/settings_only.py", "src/argus/cli.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "has_admin()" not in text, rel


async def test_the_settings_mode_opens_the_page_that_exists(tmp_path, monkeypatch):
    import webbrowser

    from argus import settings_only

    opened: list[str] = []

    async def no_serve(app, port=0, stop=None):
        return None

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr("argus.ui.server.serve", no_serve)
    monkeypatch.setattr(settings_only, "Argus", lambda *a, **k: SimpleNamespace())
    for admin_on in (False, True):
        cfg = Config()
        cfg.root = tmp_path
        cfg.ui.admin = admin_on
        await settings_only.run_settings(cfg, 8420, open_browser=True)
    assert opened == ["http://127.0.0.1:8420", "http://127.0.0.1:8420/admin"]


class FakeAudio:
    healthy = True

    def __init__(self, *args, **kwargs) -> None:
        pass

    def open(self):
        device = SimpleNamespace(name="Microphone", rate=16000, channels=1)
        return device, SimpleNamespace(name="Speakers", rate=48000, channels=2)

    def first_error(self):
        return None

    def close(self) -> None:
        pass


def fake_screen(opened: list[str]):
    class FakeScreen:
        """A thread that was never opened is "not alive": this is what `--no-screen` looks like."""

        alive = False
        polls = 0
        error = None

        def __init__(self, *args, **kwargs) -> None:
            pass

        def open(self) -> None:
            opened.append("open")

        def close(self) -> None:
            pass

    return FakeScreen


async def test_audio_only_capture_runs_to_the_end_instead_of_stopping_on_the_first_tick(monkeypatch):
    """`argus record --no-screen`: the unopened screen counted as dead, and the recording broke off
    after 80 ms with an error.
    """
    from argus import preview

    opened: list[str] = []
    monkeypatch.setattr(preview, "AudioCapture", FakeAudio)
    monkeypatch.setattr(preview, "ScreenCapture", fake_screen(opened))

    started = time.perf_counter()
    code = await preview.run_capture(Config(), seconds=0.4, snapshot=None, with_screen=False)

    assert code == 0
    assert time.perf_counter() - started >= 0.35, "the recording broke off ahead of time"
    assert opened == [], "the screen must not be opened with --no-screen"


async def test_a_dead_screen_thread_still_stops_a_capture_that_uses_the_screen(monkeypatch):
    from argus import preview

    opened: list[str] = []
    monkeypatch.setattr(preview, "AudioCapture", FakeAudio)
    monkeypatch.setattr(preview, "ScreenCapture", fake_screen(opened))

    code = await preview.run_capture(Config(), seconds=5, snapshot=None, with_screen=True)

    assert code == 1 and opened == ["open"]


# --------------------------------------------------------------------------
# Installing an update during a live recording (update-12)
# --------------------------------------------------------------------------


def test_the_update_is_not_installed_over_a_live_recording_without_confirmation(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from argus import update, update_service

    cfg = Config()
    cfg.root = tmp_path
    cfg.update.server = "http://example.invalid"
    service = update_service.UpdateService(cfg, running=lambda: True)
    service.enabled = True
    service.release = update.Release(version="2.0", zip="x.zip", sha256="0" * 64, size=1, build=9)
    monkeypatch.setattr(update, "install_release", lambda *a, **k: False)
    web = FastAPI()
    update_service.attach(web, service)
    client = TestClient(web)

    refused = client.post("/update/install", json={}).json()
    assert refused["ok"] is False and refused["busy"] is True
    assert service.phase == "idle"

    assert client.post("/update/install", json={"confirm": True}).json()["ok"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="the windows_only checks above are skipped separately")
def test_the_windows_port_policy_is_written_down_in_the_source():
    """A safeguard against "simplification": on Windows the server socket is SO_EXCLUSIVEADDRUSE,
    not SO_REUSEADDR.
    """
    text = (ROOT / "src" / "argus" / "ui" / "server.py").read_text(encoding="utf-8")
    assert "SO_EXCLUSIVEADDRUSE" in text
