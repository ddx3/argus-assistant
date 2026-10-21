"""RelayProvider: failures come back in `Answer.error`, and a request does not exceed the
server's caps.

The provider contract (`brain/base.py`): an error is returned, not raised. Break it and
an exception kills the loop that waits for spoken questions (`Assistant.run`).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.account.client import Account, AccountError
from argus.brain import relay_api
from argus.brain.relay_api import RelayProvider
from argus.core import friendly
from argus.core.config import BrainConfig
from argus.server import relay
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"


def _cfg(**over) -> BrainConfig:
    base = {"provider": "relay", "relay_upstream": "openai-api", "api_model": "gpt-5.6-luna",
            "deep_model": "gpt-5.6-terra", "effort": "low", "deep_effort": "high"}
    base.update(over)
    return BrainConfig(**base)


class FakeAccount:
    """An account that answers with a canned response and remembers what was sent to it."""

    def __init__(self, answer=None, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.sent: list[dict] = []

    def _call(self, method, path, **kw):
        self.sent.append({"path": path, **kw})
        if self.raises is not None:
            raise self.raises
        return self.answer


def _body(**fields):
    body = {"text": "answer", "input_tokens": 1, "output_tokens": 1, **fields}
    return SimpleNamespace(json=lambda: body)


# --------------------------------------------------------------------------
# Failures come as an answer, not as an exception
# --------------------------------------------------------------------------


async def test_a_raw_network_error_comes_back_as_an_error_answer():
    boom = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    provider = RelayProvider(_cfg(), FakeAccount(raises=boom))
    answer = await provider.ask("question")
    assert not answer.ok
    assert answer.error == friendly.NETWORK
    assert provider.failures == 1


async def test_a_page_instead_of_json_comes_back_as_an_error_answer():
    def not_json():
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    provider = RelayProvider(_cfg(), FakeAccount(answer=SimpleNamespace(json=not_json)))
    answer = await provider.ask("question")
    assert not answer.ok and answer.error == friendly.NETWORK


async def test_a_json_list_instead_of_an_object_comes_back_as_an_error_answer():
    provider = RelayProvider(_cfg(), FakeAccount(answer=SimpleNamespace(json=lambda: [1, 2])))
    answer = await provider.ask("question")
    assert not answer.ok and answer.error


async def test_an_account_error_keeps_its_own_words():
    provider = RelayProvider(_cfg(), FakeAccount(raises=AccountError("top up the balance")))
    answer = await provider.ask("question")
    assert answer.error == "top up the balance"


async def test_a_dead_network_with_a_real_account_is_an_error_answer(tmp_path, monkeypatch):
    """The first question after launch: there is no pass in memory, the network is dead —
    previously a `ConnectError` flew out of here, and the loop of answers to voice died."""
    (tmp_path / "argus.account.json").write_text(
        json.dumps({"refresh": "r", "email": "user@example.com"}), encoding="utf-8")
    account = Account("http://server", tmp_path)

    def dead(*a, **kw):
        raise httpx.ConnectError("the network is gone")

    monkeypatch.setattr("argus.account.client.httpx.post", dead)
    monkeypatch.setattr("argus.account.client.httpx.request", dead)

    answer = await RelayProvider(_cfg(), account).ask("question")

    assert not answer.ok and answer.error == friendly.NETWORK


# --------------------------------------------------------------------------
# A long wait does not hold up the exit from the program
# --------------------------------------------------------------------------


async def test_the_long_wait_for_an_answer_does_not_hold_the_exit_of_the_program():
    """At exit the process waits for `asyncio.to_thread` threads: a thread waiting up to 160
    seconds for an answer would keep the process alive holding the single-instance lock. A
    daemon thread does not hold it."""
    import threading

    seen: list[tuple[bool, str]] = []

    class Probe:
        def _call(self, method, path, **kw):
            seen.append((threading.current_thread().daemon, threading.current_thread().name))
            return _body()

    answer = await RelayProvider(_cfg(), Probe()).ask("question")

    assert answer.ok
    assert seen == [(True, "relay-ask")]


async def test_an_answer_that_comes_after_the_wait_was_given_up_is_dropped_quietly():
    """The deadline passed before the answer: the thread finishes waiting on its own, and there
    is nobody to deliver to — no noise.
    """
    import asyncio
    import threading

    release = threading.Event()
    finished = threading.Event()

    class Slow:
        def _call(self, method, path, **kw):
            release.wait(5)
            finished.set()
            return _body()

    task = asyncio.ensure_future(RelayProvider(_cfg(), Slow()).ask("question"))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0.05)                       # delivery on the loop: silence, no exception
    assert task.cancelled()


async def test_a_failure_in_the_thread_reaches_the_one_who_waits():
    """The call's exception reaches the place where it is turned into an answer with an error."""
    answer = await RelayProvider(_cfg(), FakeAccount(raises=KeyError("no such field"))).ask("question")
    assert not answer.ok and answer.error


# --------------------------------------------------------------------------
# The answer is cut off by the size cap
# --------------------------------------------------------------------------


async def test_a_cut_answer_says_so():
    answer_body = _body(text="start of the answer", truncated=True)
    provider = RelayProvider(_cfg(), FakeAccount(answer=answer_body))
    answer = await provider.ask("question")
    assert answer.ok
    assert answer.text.startswith("start of the answer")
    assert "cut off" in answer.text
    assert answer.meta["truncated"] is True


async def test_a_cut_and_empty_answer_is_an_error_not_a_blank_card():
    """The model spent its whole output budget on reasoning: an empty card for a paid call is
    the worst thing that can be shown."""
    provider = RelayProvider(_cfg(), FakeAccount(answer=_body(text="", truncated=True)))
    answer = await provider.ask("question")
    assert not answer.ok
    assert answer.error == relay_api.TRUNCATED_EMPTY


async def test_an_empty_answer_is_an_error_too():
    provider = RelayProvider(_cfg(), FakeAccount(answer=_body(text="  ")))
    answer = await provider.ask("question")
    assert not answer.ok and answer.error == relay_api.EMPTY


async def test_a_whole_answer_has_no_marks():
    provider = RelayProvider(_cfg(), FakeAccount(answer=_body(text="in full", truncated=False)))
    answer = await provider.ask("question")
    assert answer.text == "in full"
    assert "truncated" not in answer.meta


def test_the_words_for_the_person_name_no_internals():
    for words in (relay_api.TRUNCATED_NOTE, relay_api.TRUNCATED_EMPTY, relay_api.EMPTY):
        for bad in ("token", "model", "api", "relay", "openai", "json"):
            assert bad not in words.lower()


# --------------------------------------------------------------------------
# What goes to the server: no more than its caps
# --------------------------------------------------------------------------


async def test_the_answer_size_is_capped_at_what_the_server_accepts():
    account = FakeAccount(answer=_body())
    await RelayProvider(_cfg(max_tokens=64_000), account).ask("question")
    assert account.sent[0]["json"]["max_tokens"] == relay_api.MAX_ANSWER_TOKENS == 16_000


async def test_a_usual_answer_size_is_left_alone():
    account = FakeAccount(answer=_body())
    await RelayProvider(_cfg(max_tokens=2000), account).ask("question")
    assert account.sent[0]["json"]["max_tokens"] == 2000


async def test_too_many_pages_are_cut_to_the_servers_count_in_order():
    account = FakeAccount(answer=_body())
    pages = [bytes([i]) * 100 for i in range(60)]
    await RelayProvider(_cfg(), account).ask("question", pinned=pages, image=b"\xff\xd8screen")
    sent = account.sent[0]["json"]
    assert len(sent["pinned_b64"]) == relay_api.MAX_IMAGES - 1       # one place, for the screen frame
    import base64
    assert base64.b64decode(sent["pinned_b64"][0]) == pages[0]
    assert base64.b64decode(sent["pinned_b64"][-1]) == pages[relay_api.MAX_IMAGES - 2]


async def test_pages_beyond_the_size_budget_are_not_sent():
    account = FakeAccount(answer=_body())
    page = b"x" * 1_000_000                                          # about 1.33 MB in base64
    await RelayProvider(_cfg(), account).ask("question", pinned=[page] * 30)
    sent = account.sent[0]["json"]["pinned_b64"]
    assert 0 < len(sent) < 30
    assert sum(len(p) for p in sent) <= relay_api.MAX_IMAGES_B64


async def test_a_long_system_part_and_a_long_request_are_cut():
    account = FakeAccount(answer=_body())
    system = "s" * (relay_api.MAX_SYSTEM_CHARS + 5000)
    prompt = "feed " * 20_000 + "QUESTION"                    # longer than the cap, the question is at the end
    await RelayProvider(_cfg(), account).ask(prompt, system=system)
    sent = account.sent[0]["json"]
    assert len(sent["system"]) == relay_api.MAX_SYSTEM_CHARS
    assert len(sent["prompt"]) == relay_api.MAX_PROMPT_CHARS
    assert sent["prompt"].endswith("QUESTION")                       # the main part of a request is at the end


async def test_a_picture_the_server_cannot_take_is_refused_in_words():
    account = FakeAccount(answer=_body())
    huge = b"\xff\xd8" + b"0" * 3_100_000
    answer = await RelayProvider(_cfg(), account).ask("question", image=huge)
    assert not answer.ok and "too large" in answer.error
    assert account.sent == []                                       # did not reach the server


# --------------------------------------------------------------------------
# The largest request the client may send is accepted by the real server
# --------------------------------------------------------------------------


@pytest.fixture
def real_account(tmp_path, monkeypatch):
    db = connect(tmp_path / "accounts.sqlite")
    server = TestClient(build(db, SECRET))

    def request(method, url, **kw):
        kw.pop("timeout", None)
        return server.request(method, url.replace("http://server", ""), **kw)

    monkeypatch.setattr("argus.account.client.httpx.request", request)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda url, **kw: request("POST", url, **kw))
    acc = Account("http://server", tmp_path)
    acc.register("user@example.com", PASSWORD)
    return acc


async def test_the_biggest_request_the_client_may_send_passes_the_real_server(
        real_account, monkeypatch):
    seen = {}

    async def fake_openai(req):
        seen["system"] = len(req.system)
        seen["prompt"] = len(req.prompt)
        seen["pages"] = len(req.pinned_b64)
        return relay.BrainAskResponse(text="accepted", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)
    page = b"\xff\xd8" + b"\x00" * 1_000_000
    answer = await RelayProvider(_cfg(), real_account).ask(
        "q" * 70_000, system="s" * 250_000, pinned=[page] * 40, image=b"\xff\xd8frame")

    assert answer.ok, answer.error
    assert seen["system"] == relay_api.MAX_SYSTEM_CHARS
    assert seen["prompt"] == relay_api.MAX_PROMPT_CHARS
    assert 0 < seen["pages"] <= relay_api.MAX_IMAGES


async def test_a_cut_answer_of_the_real_server_is_marked(real_account, monkeypatch):
    async def fake_openai(req):
        return relay.BrainAskResponse(text="broken off", input_tokens=1, output_tokens=2000,
                                      truncated=True)

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)
    answer = await RelayProvider(_cfg(), real_account).ask("question")
    assert answer.ok and "cut off" in answer.text
