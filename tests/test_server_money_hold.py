"""Relay money: a hold before the call, not a balance check ahead of it.

The relay used to look at the balance, wait for the paid call and only then charge. While
the call was in progress, parallel requests from the same account saw the same untouched
balance, passed the check and each paid the provider, while only the remainder was
collected from the person: a fresh account with a 30-cent gift could start twenty
parallel questions, and we paid the difference. Here this is reproduced with real
concurrency against a slow upstream.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from argus.server import accounts, entitlements, pricing, relay
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"
MODEL = "gpt-5.6-luna"
VOICE = "21m00Tcm4TlvDq8ikWAM"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def account(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    return login.account_id, {"Authorization": f"Bearer {login.access}"}


def client(db) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build(db, SECRET)),
                             base_url="http://server", timeout=30.0)


def ask_body(prompt: str = "hello", model: str = MODEL, **over) -> dict:
    return {"provider": "openai-api", "model": model, "prompt": prompt, **over}


def ceiling_of(prompt: str, model: str = MODEL, max_tokens: int = 1024) -> int:
    """The ceiling as the relay computes it, but written out here separately from the relay."""
    return pricing.charged(pricing.brain_ceiling(
        model, len(prompt), 0, max_tokens, text_bytes=len(prompt.encode())))


def set_balance(db, micro: int) -> None:
    db.execute("UPDATE accounts SET balance_micro = ?", (micro,))
    db.commit()


def held_rows(db) -> int:
    return db.execute("SELECT COUNT(*) FROM holds").fetchone()[0]


class SlowUpstream:
    """A paid provider that does not answer right away: exactly the time window in which the race
    happens.
    """

    def __init__(self, delay: float = 0.15, tokens: tuple[int, int] = (1000, 500)) -> None:
        self.delay = delay
        self.tokens = tokens
        self.calls = 0
        self.active = 0
        self.peak = 0

    async def __call__(self, req):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return relay.BrainAskResponse(text="answer", input_tokens=self.tokens[0],
                                      output_tokens=self.tokens[1])


# --------------------------------------------------------------------------
# Model answers
# --------------------------------------------------------------------------


async def test_parallel_asks_cannot_spend_the_same_money_twice(db, account, monkeypatch):
    """There is money for two questions: exactly two reach the provider, the rest get 402."""
    account_id, headers = account
    upstream = SlowUpstream()
    monkeypatch.setattr(relay, "_ask_openai", upstream)
    # Its own protection layer (the number of simultaneous calls) is switched off: the hold
    # itself is checked.
    monkeypatch.setattr(relay, "MAX_INFLIGHT_BRAIN", 100)
    monkeypatch.setattr(relay, "MAX_INFLIGHT_TOTAL", 100)
    held = ceiling_of("hello")
    set_balance(db, 2 * held + 5)

    async with client(db) as http:
        replies = await asyncio.gather(*[
            http.post("/relay/brain/ask", headers=headers, json=ask_body())
            for _ in range(20)
        ])

    codes = sorted(r.status_code for r in replies)
    assert codes.count(200) == 2, codes
    assert set(codes) <= {200, 402}, codes
    assert upstream.calls == 2, "the provider got more calls than were paid for"
    paid = pricing.charged(pricing.brain_cost(MODEL, 1000, 500))
    assert entitlements.balance(db, account_id) == 2 * held + 5 - 2 * paid
    assert held_rows(db) == 0, "the hold was left hanging after the answer"


async def test_the_fresh_gift_account_cannot_overdraw_with_twenty_parallel_asks(
        db, account, monkeypatch):
    """Scenario from the audit: a $0.30 gift, twenty parallel questions to the expensive model.

    What is collected from the person must equal what we paid, and the balance is not run down
    to zero "on credit": whatever the provider counted is what is charged."""
    account_id, headers = account
    prompt = "x" * 20_000
    upstream = SlowUpstream(tokens=(10_000, 3_000))
    monkeypatch.setattr(relay, "_ask_openai", upstream)

    async with client(db) as http:
        replies = await asyncio.gather(*[
            http.post("/relay/brain/ask", headers=headers,
                      json=ask_body(prompt, model="gpt-5.6-terra", max_tokens=3000))
            for _ in range(20)
        ])

    assert all(r.status_code in (200, 402, 429) for r in replies)
    served = sum(r.status_code == 200 for r in replies)
    assert served == upstream.calls
    billed = upstream.calls * pricing.charged(pricing.brain_cost("gpt-5.6-terra", 10_000, 3_000))
    assert billed <= pricing.WELCOME_MICRO, (
        f"the provider counted {billed}, but the person had {pricing.WELCOME_MICRO}")
    collected = db.execute("SELECT COALESCE(SUM(charged_micro), 0) FROM usage").fetchone()[0]
    assert collected == billed
    assert entitlements.balance(db, account_id) == pricing.WELCOME_MICRO - billed
    assert held_rows(db) == 0


async def test_no_more_than_the_cap_of_calls_run_at_once(db, account, monkeypatch):
    """Second layer: on an account with a large sum, the hold alone would let a thousand parallel
    calls through.
    """
    _, headers = account
    upstream = SlowUpstream()
    monkeypatch.setattr(relay, "_ask_openai", upstream)
    entitlements.credit(db, 1, 50 * pricing.MICRO, "topup")

    async with client(db) as http:
        replies = await asyncio.gather(*[
            http.post("/relay/brain/ask", headers=headers, json=ask_body())
            for _ in range(10)
        ])

    codes = [r.status_code for r in replies]
    assert upstream.peak <= relay.MAX_INFLIGHT_BRAIN
    assert codes.count(200) == relay.MAX_INFLIGHT_BRAIN
    assert codes.count(429) == 10 - relay.MAX_INFLIGHT_BRAIN
    refused = next(r for r in replies if r.status_code == 429)
    assert "at once" in refused.json()["detail"]
    assert refused.headers["retry-after"]


async def test_a_slot_is_given_back_when_the_call_ends(db, account, monkeypatch):
    _, headers = account
    monkeypatch.setattr(relay, "_ask_openai", SlowUpstream(delay=0.01))
    entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")
    async with client(db) as http:
        for _ in range(relay.MAX_INFLIGHT_BRAIN * 3):
            r = await http.post("/relay/brain/ask", headers=headers, json=ask_body())
            assert r.status_code == 200


async def test_a_failed_call_gives_back_everything_that_was_held(db, account, monkeypatch):
    account_id, headers = account
    before = entitlements.balance(db, account_id)

    async def broken(req):
        await asyncio.sleep(0.01)
        raise RuntimeError("limit exhausted")

    monkeypatch.setattr(relay, "_ask_openai", broken)
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", headers=headers, json=ask_body())
    assert r.status_code == 502
    assert entitlements.balance(db, account_id) == before
    assert held_rows(db) == 0
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


async def test_a_hung_provider_is_cut_off_and_costs_nothing(db, account, monkeypatch):
    """A hung provider holds neither the handler nor the held money."""
    account_id, headers = account
    before = entitlements.balance(db, account_id)
    monkeypatch.setattr(relay, "UPSTREAM_DEADLINE_S", 0.05)
    monkeypatch.setattr(relay, "_ask_openai", SlowUpstream(delay=5))
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", headers=headers, json=ask_body())
    assert r.status_code == 502
    assert entitlements.balance(db, account_id) == before
    assert held_rows(db) == 0


async def test_a_cancelled_request_gives_the_money_back(db, account, monkeypatch):
    """The server is stopped in the middle of a call: the held money must not be lost."""
    account_id, headers = account
    before = entitlements.balance(db, account_id)
    monkeypatch.setattr(relay, "_ask_openai", SlowUpstream(delay=5))
    async with client(db) as http:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                http.post("/relay/brain/ask", headers=headers, json=ask_body()), 0.1)
    await asyncio.sleep(0.05)
    assert entitlements.balance(db, account_id) == before
    assert held_rows(db) == 0


async def test_a_price_above_the_ceiling_is_taken_from_what_is_left(db, account, monkeypatch):
    """The ceiling underestimated the input: we collect the rest from what is left, but never go
    below zero, same as `charge`.
    """
    account_id, headers = account
    monkeypatch.setattr(relay, "_ask_openai",
                        SlowUpstream(delay=0.01, tokens=(5_000_000, 0)))
    held = ceiling_of("hello")
    set_balance(db, held + 100)
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", headers=headers, json=ask_body())
    assert r.status_code == 200
    assert entitlements.balance(db, account_id) == 0
    row = db.execute("SELECT charged_micro FROM usage").fetchone()
    assert row["charged_micro"] == pricing.charged(pricing.brain_cost(MODEL, 5_000_000, 0))
    assert held_rows(db) == 0


async def test_the_balance_matches_the_journal_after_parallel_work(db, account, monkeypatch):
    """Balance = all top-ups minus everything charged, also when nothing is held."""
    account_id, headers = account
    monkeypatch.setattr(relay, "_ask_openai", SlowUpstream(delay=0.02))
    entitlements.credit(db, account_id, 10 * pricing.MICRO, "topup")
    async with client(db) as http:
        await asyncio.gather(*[
            http.post("/relay/brain/ask", headers=headers, json=ask_body())
            for _ in range(8)
        ])
    credited = db.execute("SELECT SUM(micro) FROM credits").fetchone()[0]
    charged = db.execute("SELECT SUM(charged_micro) FROM usage").fetchone()[0]
    assert held_rows(db) == 0
    assert entitlements.balance(db, account_id) == credited - charged


# --------------------------------------------------------------------------
# Voice-over
# --------------------------------------------------------------------------


class SlowVoice:
    """A fake httpx.AsyncClient that does not answer right away."""

    calls = 0
    delay = 0.15

    class Response:
        status_code = 200
        content = b"mp3"
        text = ""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        type(self).calls += 1
        await asyncio.sleep(type(self).delay)
        return self.Response()


async def test_parallel_speech_cannot_spend_the_same_money_twice(db, account, monkeypatch):
    account_id, headers = account
    SlowVoice.calls = 0
    # The real client is needed by the test itself, so it is taken BEFORE the patch: the patch
    # concerns only the relay's call to the voice-over.
    test_client = client(db)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    monkeypatch.setattr(httpx, "AsyncClient", SlowVoice)
    monkeypatch.setattr(relay, "MAX_INFLIGHT_TTS", 100)
    monkeypatch.setattr(relay, "MAX_INFLIGHT_TOTAL", 100)

    text = "hello" * 100
    cost = pricing.charged(pricing.tts_cost(len(text)))
    set_balance(db, 2 * cost + 3)
    async with test_client as http:
        replies = await asyncio.gather(*[
            http.post("/relay/tts/synthesize", headers=headers,
                      json={"voice_id": VOICE, "text": text})
            for _ in range(10)
        ])

    codes = sorted(r.status_code for r in replies)
    assert codes.count(200) == 2, codes
    assert set(codes) <= {200, 402}
    assert SlowVoice.calls == 2
    assert entitlements.balance(db, account_id) == 3
    assert held_rows(db) == 0


# --------------------------------------------------------------------------
# The hold by itself
# --------------------------------------------------------------------------


def test_a_hold_takes_money_out_of_the_balance_until_it_is_closed(db, account):
    account_id, _ = account
    start = entitlements.balance(db, account_id)
    held = entitlements.hold(db, account_id, 100_000)
    assert held.allowed and held.micro == 100_000
    assert entitlements.balance(db, account_id) == start - 100_000
    assert held_rows(db) == 1

    take = entitlements.settle(db, held, "brain_tokens", 10, 20_000, model=MODEL)
    assert take == pricing.charged(20_000) == 40_000
    assert entitlements.balance(db, account_id) == start - 40_000
    assert held_rows(db) == 0


def test_a_hold_that_does_not_fit_changes_nothing_and_says_why(db, account):
    account_id, _ = account
    start = entitlements.balance(db, account_id)
    denied = entitlements.hold(db, account_id, start + 1)
    assert not denied.allowed and denied.id == 0
    assert "top up" in denied.reason
    assert entitlements.balance(db, account_id) == start
    assert held_rows(db) == 0


def test_a_hold_may_take_the_whole_balance(db, account):
    account_id, _ = account
    start = entitlements.balance(db, account_id)
    held = entitlements.hold(db, account_id, start)
    assert held.allowed
    assert entitlements.balance(db, account_id) == 0
    entitlements.release(db, held)
    assert entitlements.balance(db, account_id) == start


def test_a_hold_is_closed_only_once(db, account):
    """Closing a second time (a bug in the caller's code) does not return the money twice."""
    account_id, _ = account
    start = entitlements.balance(db, account_id)
    held = entitlements.hold(db, account_id, 50_000)
    entitlements.release(db, held)
    entitlements.release(db, held)
    assert entitlements.balance(db, account_id) == start
    entitlements.settle(db, held, "brain_tokens", 1, 10_000)
    assert entitlements.balance(db, account_id) == start - pricing.charged(10_000)


def test_a_hold_on_an_empty_balance_says_it_is_empty(db, account):
    account_id, _ = account
    set_balance(db, 0)
    denied = entitlements.hold(db, account_id, 1)
    assert not denied.allowed
    assert "empty" in denied.reason


def test_the_refusal_quotes_the_real_balance_not_the_one_lowered_by_own_holds(db, account):
    """On $0.10 with a running call that holds $0.06, "your balance is $0.04"
    would be untrue.
    """
    account_id, _ = account
    set_balance(db, 100_000)
    mine = entitlements.hold(db, account_id, 60_000)
    assert entitlements.balance(db, account_id) == 40_000

    denied = entitlements.hold(db, account_id, 150_000)
    assert not denied.allowed
    assert pricing.money(100_000) in denied.reason
    assert pricing.money(40_000) not in denied.reason
    assert "top up" in denied.reason

    short = entitlements.afford(db, account_id, 150_000)
    assert not short.allowed
    assert pricing.money(100_000) in short.reason and pricing.money(40_000) not in short.reason
    entitlements.release(db, mine)


def test_money_tied_up_by_own_calls_is_not_called_missing(db, account):
    """There would be enough, but it is all tied up in the same person's running calls: "top up"
    would be a lie here.
    """
    account_id, _ = account
    set_balance(db, 100_000)
    mine = entitlements.hold(db, account_id, 80_000)
    denied = entitlements.hold(db, account_id, 50_000)
    assert not denied.allowed
    assert "top up" not in denied.reason
    assert pricing.money(20_000) not in denied.reason
    assert "held by your other requests" in denied.reason
    entitlements.release(db, mine)
    assert entitlements.hold(db, account_id, 50_000).allowed


def test_an_empty_account_with_money_only_in_flight_is_not_empty(db, account):
    account_id, _ = account
    set_balance(db, 30_000)
    entitlements.hold(db, account_id, 30_000)
    assert entitlements.balance(db, account_id) == 0
    assert entitlements.total(db, account_id) == 30_000
    assert "empty" not in entitlements.hold(db, account_id, 10_000).reason


async def test_the_402_over_http_quotes_the_real_balance(db, account, monkeypatch):
    """The same thing the person sees: the relay's response with money held for a neighbouring
    call.
    """
    account_id, headers = account
    monkeypatch.setattr(relay, "_ask_openai", SlowUpstream(delay=0.01))
    mine = entitlements.hold(db, account_id, entitlements.balance(db, account_id) - 100)
    async with client(db) as http:
        # This question's ceiling ($0.38) is above the whole balance ($0.30): an honest "not
        # enough" refusal.
        r = await http.post("/relay/brain/ask", headers=headers,
                            json=ask_body(model="gpt-5.6-terra", max_tokens=16_000))
    assert r.status_code == 402
    assert pricing.money(pricing.WELCOME_MICRO) in r.json()["detail"]
    assert pricing.money(100) not in r.json()["detail"]
    entitlements.release(db, mine)


def test_a_new_start_returns_what_a_dead_process_left_held(db, account, caplog):
    """The process died in the middle of a call: at startup the held money is returned to the
    person.
    """
    account_id, _ = account
    start = entitlements.balance(db, account_id)
    entitlements.hold(db, account_id, 120_000)
    assert entitlements.balance(db, account_id) == start - 120_000

    build(db, SECRET)                       # start of a new process
    assert entitlements.balance(db, account_id) == start
    assert held_rows(db) == 0
    assert "unclosed holds left over" in caplog.text


# --------------------------------------------------------------------------
# What the provider returned
# --------------------------------------------------------------------------


def _fake_sdk(monkeypatch, module: str, name: str, response):
    created: list[dict] = []

    class Client:
        def __init__(self, **kw):
            created.append(kw)
            call = types.SimpleNamespace(create=self._create)
            self.responses = call
            self.messages = call

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def _create(self, **kw):
            return response

    monkeypatch.setattr(f"{module}.{name}", Client)
    return created


async def test_an_openai_answer_cut_by_the_token_limit_is_marked(monkeypatch):
    response = types.SimpleNamespace(
        status="incomplete", output_text=" part of the answer ",
        incomplete_details=types.SimpleNamespace(reason="max_output_tokens"),
        usage=types.SimpleNamespace(input_tokens=11, output_tokens=22))
    created = _fake_sdk(monkeypatch, "openai", "AsyncOpenAI", response)
    out = await relay._ask_openai(relay.BrainAskRequest(**ask_body()))
    assert out.truncated is True
    assert (out.text, out.input_tokens, out.output_tokens) == ("part of the answer", 11, 22)
    # An explicit deadline and one retry: a hung provider does not hold a handler for minutes.
    assert created == [{"timeout": relay.UPSTREAM_TIMEOUT_S, "max_retries": 1}]


@pytest.mark.parametrize("details", [
    types.SimpleNamespace(reason="content_filter"),        # stopped by the filter, not by the ceiling
    {"reason": "content_filter"},
    types.SimpleNamespace(reason=None),
    None,                                                  # reason unknown: we do not claim anything
])
async def test_an_openai_answer_stopped_for_another_reason_is_not_called_cut_by_the_limit(
        monkeypatch, details):
    response = types.SimpleNamespace(
        status="incomplete", output_text="part", incomplete_details=details,
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=2))
    _fake_sdk(monkeypatch, "openai", "AsyncOpenAI", response)
    out = await relay._ask_openai(relay.BrainAskRequest(**ask_body()))
    assert out.truncated is False
    assert out.text == "part"                           # the text itself is not lost at that


async def test_the_limit_reason_is_read_from_a_plain_dict_too(monkeypatch):
    response = types.SimpleNamespace(
        status="incomplete", output_text="part", incomplete_details={"reason": "max_output_tokens"},
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=2))
    _fake_sdk(monkeypatch, "openai", "AsyncOpenAI", response)
    assert (await relay._ask_openai(relay.BrainAskRequest(**ask_body()))).truncated is True


async def test_a_complete_openai_answer_is_not_marked(monkeypatch):
    response = types.SimpleNamespace(
        status="completed", output_text="full",
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=2))
    _fake_sdk(monkeypatch, "openai", "AsyncOpenAI", response)
    assert (await relay._ask_openai(relay.BrainAskRequest(**ask_body()))).truncated is False


@pytest.mark.parametrize("stop, expected", [
    ("max_tokens", True), ("end_turn", False), ("refusal", False), ("stop_sequence", False),
])
async def test_an_anthropic_answer_is_marked_by_its_stop_reason(monkeypatch, stop, expected):
    response = types.SimpleNamespace(
        stop_reason=stop,
        content=[types.SimpleNamespace(type="text", text="text")],
        usage=types.SimpleNamespace(input_tokens=3, output_tokens=4))
    created = _fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", response)
    req = relay.BrainAskRequest(**ask_body(model="claude-haiku-4-5") | {"provider": "anthropic-api"})
    out = await relay._ask_anthropic(req)
    assert out.truncated is expected
    assert created == [{"timeout": relay.UPSTREAM_TIMEOUT_S, "max_retries": 1}]


async def test_the_truncated_flag_reaches_the_client(db, account, monkeypatch):
    _, headers = account

    async def cut(req):
        return relay.BrainAskResponse(text="cut off", input_tokens=5, output_tokens=6,
                                      truncated=True)

    monkeypatch.setattr(relay, "_ask_openai", cut)
    async with client(db) as http:
        body = (await http.post("/relay/brain/ask", headers=headers, json=ask_body())).json()
    assert body["truncated"] is True


def test_the_cap_of_parallel_calls_leaves_room_for_what_the_app_does_at_once():
    """The conversation recap sends several requests at once, and the person's question must not
    be refused with "too many requests at once" in the meantime: the
    server's cap is above what the app does."""
    from argus.recap.engine import PARALLEL

    assert relay.MAX_INFLIGHT_BRAIN >= PARALLEL + 1
