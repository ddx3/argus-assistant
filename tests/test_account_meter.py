"""Recognition metering on the app side: it accumulates seconds, reports them, does not lose
them when the connection drops, and stops the audio when the money runs out.

The server is real (`TestClient`), as in `test_account_client.py`; we do not wait for time,
`flush()` is called by hand.
"""

from __future__ import annotations

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from argus.account.client import Account
from argus.account.meter import CHUNK_S, MAX_PENDING_S, Meter
from argus.core.events import SAMPLE_RATE, Source, Utterance
from argus.server import entitlements, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "reasonably-long-password"


@pytest.fixture
def setup(tmp_path, monkeypatch):
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
    # Our own counter without a background thread: the test decides when to report.
    meter = Meter(acc, interval=3600)
    monkeypatch.setattr(meter, "_ensure_thread", lambda: None)
    return acc, meter, db


def test_seconds_pile_up_until_the_flush(setup):
    acc, meter, db = setup
    before = entitlements.balance(db, 1)
    meter.add(3.5)
    meter.add(2.0)
    assert meter.pending == pytest.approx(5.5)
    assert entitlements.balance(db, 1) == before     # nothing has been charged yet

    meter.flush()
    assert meter.pending == 0
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(5.5)
    assert entitlements.balance(db, 1) < before


def test_nothing_is_reported_when_signed_out(setup):
    acc, meter, db = setup
    acc.forget()
    meter.add(10)
    assert meter.pending == 0


def test_a_failed_report_is_kept_and_sent_later(setup, monkeypatch):
    """The network blinked — the seconds are not lost and not charged twice."""
    acc, meter, db = setup
    meter.add(30)

    real = acc.report_stt
    monkeypatch.setattr(acc, "report_stt",
                        lambda s, tier="normal", report_id=None: (_ for _ in ()).throw(httpx.ConnectError("no network")))
    meter.flush()
    assert meter.pending == pytest.approx(30)
    assert entitlements.spent(db, 1, "stt_seconds") == 0

    monkeypatch.setattr(acc, "report_stt", real)
    meter.flush()
    assert meter.pending == 0
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(30)


def test_seconds_are_billed_at_the_tier_that_listened(setup):
    """The "More accurate" tier costs more, and the seconds must not be merged into one
    number: the bill for it would come out at the price of the regular tier."""
    acc, meter, db = setup
    entitlements.credit(db, 1, 20 * pricing.MICRO, "topup")
    before = entitlements.balance(db, 1)

    meter.add(60, "high")
    meter.add(60)
    assert meter.pending == pytest.approx(120)

    meter.flush()
    assert meter.pending == 0
    take = (pricing.charged(pricing.stt_cost(60, "high"))
            + pricing.charged(pricing.stt_cost(60)))
    assert entitlements.balance(db, 1) == before - take
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(120)


def test_a_failed_report_keeps_the_seconds_of_every_tier(setup, monkeypatch):
    acc, meter, db = setup
    meter.add(30)
    meter.add(20, "high")

    real = acc.report_stt
    monkeypatch.setattr(acc, "report_stt",
                        lambda s, tier="normal", report_id=None: (_ for _ in ()).throw(httpx.ConnectError("no network")))
    meter.flush()
    assert meter.pending == pytest.approx(50)
    assert entitlements.spent(db, 1, "stt_seconds") == 0

    monkeypatch.setattr(acc, "report_stt", real)
    before = entitlements.balance(db, 1)
    meter.flush()
    assert meter.pending == 0
    take = (pricing.charged(pricing.stt_cost(30))
            + pricing.charged(pricing.stt_cost(20, "high")))
    assert entitlements.balance(db, 1) == before - take


def test_a_long_backlog_is_sent_in_chunks_the_server_accepts(setup):
    acc, meter, db = setup
    entitlements.credit(db, 1, 50 * pricing.MICRO, "topup")
    meter.add(CHUNK_S * 2 + 30)
    meter.flush()
    assert meter.pending == 0
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(CHUNK_S * 2 + 30)
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 3


def test_the_backlog_has_a_ceiling(setup):
    """Hours without a connection are no reason to charge for everything at once later."""
    acc, meter, _ = setup
    for _ in range(10):
        meter.add(MAX_PENDING_S)
    assert meter.pending == MAX_PENDING_S


# --------------------------------------------------------------------------
# A retried report does not charge a second time
# --------------------------------------------------------------------------


def _lose_the_answer(acc, monkeypatch, times: int = 1) -> list[dict]:
    """The server receives the report and charges, but the reply never reaches the client — as
    when the connection breaks right after the charge. Returns what the client sent (in
    order)."""
    real = acc.report_stt
    sent: list[dict] = []
    left = [times]

    def report(seconds, tier="normal", report_id=None):
        sent.append({"seconds": seconds, "tier": tier, "report_id": report_id})
        right = real(seconds, tier, report_id=report_id)         # it arrived and was charged
        if left[0] > 0:
            left[0] -= 1
            raise httpx.ReadTimeout("the reply got lost")
        return right

    monkeypatch.setattr(acc, "report_stt", report)
    return sent


def test_a_report_whose_answer_was_lost_is_not_charged_twice(setup, monkeypatch):
    """The nastiest spot: the server has charged, the client thinks it did not arrive, and
    sends again.
    """
    acc, meter, db = setup
    before = entitlements.balance(db, 1)
    sent = _lose_the_answer(acc, monkeypatch)

    meter.add(30)
    meter.flush()                                  # charged, but to the client it "did not arrive"
    assert meter.pending == pytest.approx(30)
    meter.flush()                                  # retry

    assert meter.pending == 0
    assert [s["seconds"] for s in sent] == [30, 30]
    assert sent[0]["report_id"] and sent[0]["report_id"] == sent[1]["report_id"]
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(30)       # not 60
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.stt_cost(30))
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1


def test_a_retry_goes_out_with_the_same_seconds_even_if_more_piled_up(setup, monkeypatch):
    """A retry is exactly the same report; the seconds that piled up during the trouble go in a
    separate one with a new number. Otherwise the server would see a familiar number with a
    different figure and would either undercount or recount."""
    acc, meter, db = setup
    before = entitlements.balance(db, 1)
    sent = _lose_the_answer(acc, monkeypatch)

    meter.add(30)
    meter.flush()
    meter.add(12)                                  # while there was no answer, the conversation went on
    meter.flush()

    assert [s["seconds"] for s in sent] == [30, 30, 12]
    assert sent[0]["report_id"] == sent[1]["report_id"] != sent[2]["report_id"]
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(42)
    assert entitlements.balance(db, 1) == before - (
        pricing.charged(pricing.stt_cost(30)) + pricing.charged(pricing.stt_cost(12)))
    assert meter.pending == 0


def test_every_chunk_gets_its_own_number(setup, monkeypatch):
    acc, meter, db = setup
    entitlements.credit(db, 1, 50 * pricing.MICRO, "topup")
    sent = _lose_the_answer(acc, monkeypatch, times=0)
    meter.add(CHUNK_S * 2 + 30)
    meter.add(20, "high")
    meter.flush()
    ids = [s["report_id"] for s in sent]
    assert len(ids) == 4 and len(set(ids)) == 4
    import re
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", i) for i in ids)       # what the server accepts
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(CHUNK_S * 2 + 50)


def test_a_failed_send_keeps_the_numbers_of_every_waiting_report(setup, monkeypatch):
    acc, meter, db = setup
    entitlements.credit(db, 1, 50 * pricing.MICRO, "topup")
    meter.add(CHUNK_S + 10)                       # two reports
    real = acc.report_stt
    seen: list[str] = []

    def flaky(seconds, tier="normal", report_id=None):
        seen.append(report_id)
        if len(seen) == 2:
            raise httpx.ConnectError("no network")
        return real(seconds, tier, report_id=report_id)

    monkeypatch.setattr(acc, "report_stt", flaky)
    meter.flush()                                  # the first is accepted, the second broke off
    assert meter.pending == pytest.approx(10)
    meter.flush()                                  # the second goes out again, with the same number
    assert seen[1] == seen[2] and seen[0] != seen[1]
    assert meter.pending == 0
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(CHUNK_S + 10)


def test_the_ceiling_counts_reports_already_waiting_to_be_sent(setup, monkeypatch):
    """Reports already composed are also undelivered: there is one ceiling for everything."""
    acc, meter, _ = setup
    monkeypatch.setattr(acc, "report_stt",
                        lambda s, tier="normal", report_id=None: (_ for _ in ()).throw(
                            httpx.ConnectError("no network")))
    meter.add(MAX_PENDING_S)
    meter.flush()                                  # everything is put into reports, none is accepted
    meter.add(500)                                 # no room
    assert meter.pending == pytest.approx(MAX_PENDING_S)


def test_an_old_server_that_knows_no_numbers_still_gets_a_working_report(setup, monkeypatch):
    """The number is an extra field for an old server: it does not read it, and accepts the
    report as before.
    """
    acc, meter, db = setup
    posted: list[dict] = []
    real = acc._call

    def spy(method, path, **kw):
        if path == "/meter/stt":
            posted.append(dict(kw["json"]))
            kw["json"] = {k: v for k, v in kw["json"].items() if k != "report_id"}   # as if it had not arrived
        return real(method, path, **kw)

    monkeypatch.setattr(acc, "_call", spy)
    meter.add(10)
    meter.flush()
    assert "report_id" in posted[0]
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(10)


def test_the_meter_knows_when_the_money_is_gone(setup):
    acc, meter, db = setup
    assert meter.exhausted is False            # does not know the entitlement yet, so it does not forbid

    acc.state()
    assert meter.exhausted is False

    db.execute("UPDATE accounts SET balance_micro = 1")
    db.commit()
    meter.add(60)
    meter.flush()                              # the server answered "no money"
    assert meter.exhausted is True


def test_a_top_up_lifts_the_stop_without_any_audio(setup):
    """The flag does not belong to the audio: after a top-up and a press of "turn on"
    (`state()`) it clears on its own. Otherwise a stopped audio would have nothing to
    reset it with."""
    acc, meter, db = setup
    db.execute("UPDATE accounts SET balance_micro = 1")
    db.commit()
    meter.add(60)
    meter.flush()
    assert meter.exhausted is True

    entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")
    acc.state()
    assert meter.exhausted is False


def test_close_hands_in_the_rest(setup):
    """The last seconds of a conversation must not be left unpaid."""
    acc, meter, db = setup
    meter.add(12)
    meter.close()
    assert entitlements.spent(db, 1, "stt_seconds") == pytest.approx(12)


# --------------------------------------------------------------------------
# Connection to the speech pipeline
# --------------------------------------------------------------------------


class _Engine:
    def __init__(self, name, tier=None):
        self.name = name
        self.calls = 0
        if tier is not None:               # engines without a precision choice have no such attribute
            self.tier = tier

    def transcribe(self, utterance):
        self.calls += 1
        return None


def _pipeline(meter, engine, on_broke=None):
    from argus.perception.speech import SpeechPipeline
    from argus.core.bus import Bus
    from argus.core.config import Config

    sp = SpeechPipeline.__new__(SpeechPipeline)
    sp.bus = Bus()
    sp.meter = meter
    sp.on_broke = on_broke
    sp.engines = {Source.ME: engine, Source.THEM: engine}
    sp.shadow = None
    sp._shadow_pool = None
    sp._shadow_tasks = set()
    sp.utterances = sp.transcribed = sp.dropped = sp.rescued = 0

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    sp._pools = {Source.ME: pool, Source.THEM: pool}
    return sp


def _utterance(seconds=2.0):
    return Utterance(t=0, t_end=int(seconds * 1e9), source=Source.THEM,
                     samples=np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16))


async def test_an_utterance_is_metered_by_its_duration(setup):
    import asyncio

    acc, meter, db = setup
    engine = _Engine("realtime-openai")
    sp = _pipeline(meter, engine)
    await sp._handle(asyncio.get_running_loop(), _utterance(2.0))
    assert meter.pending == pytest.approx(2.0)
    assert engine.calls == 1


async def test_an_utterance_is_metered_at_the_tier_of_the_engine(setup):
    import asyncio

    acc, meter, db = setup
    entitlements.credit(db, 1, 20 * pricing.MICRO, "topup")
    sharp = _pipeline(meter, _Engine("realtime-openai", tier="high"))
    await sharp._handle(asyncio.get_running_loop(), _utterance(60.0))
    before = entitlements.balance(db, 1)
    meter.flush()
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.stt_cost(60, "high"))


async def test_the_free_stub_engine_is_not_metered(setup):
    import asyncio

    acc, meter, _ = setup
    sp = _pipeline(meter, _Engine("stub"))
    await sp._handle(asyncio.get_running_loop(), _utterance(2.0))
    assert meter.pending == 0


async def test_with_no_money_the_audio_does_not_leave_the_machine(setup):
    """The money has run out — the audio does not leave for the cloud, and the conversation
    stops.
    """
    import asyncio

    acc, meter, db = setup
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()
    acc.state()
    assert meter.exhausted

    engine = _Engine("realtime-openai")
    stopped = []
    sp = _pipeline(meter, engine, on_broke=lambda: stopped.append(True))
    await sp._handle(asyncio.get_running_loop(), _utterance(2.0))

    assert engine.calls == 0, "paid audio went out with an empty balance"
    assert stopped == [True]
    assert meter.pending == 0


async def test_without_a_meter_nothing_changes(setup):
    """A self-built version on its own keys: there is no meter, the behavior is as before."""
    import asyncio

    engine = _Engine("realtime-openai")
    sp = _pipeline(None, engine)
    await sp._handle(asyncio.get_running_loop(), _utterance(1.0))
    assert engine.calls == 1


# --------------------------------------------------------------------------
# We charge for audio that was actually sent, not for every line
# --------------------------------------------------------------------------


class _Reporting(_Engine):
    """An engine that itself says whether the audio of an utterance went onto the network
    (`RealtimeOpenAI.audio_sent`).
    """

    def __init__(self, name, sent):
        super().__init__(name)
        self.audio_sent = sent


async def test_audio_that_never_left_the_machine_is_not_billed(setup):
    """The session did not come up (no key, the server is down) — the audio stayed at home,
    there is nothing to pay for.
    """
    import asyncio

    _, meter, _db = setup
    engine = _Reporting("realtime-openai", sent=False)
    sp = _pipeline(meter, engine)
    await sp._handle(asyncio.get_running_loop(), _utterance(2.0))
    assert engine.calls == 1
    assert meter.pending == 0


async def test_audio_that_left_is_billed_even_when_nothing_came_back(setup):
    """It went out and came back empty — the cloud accepted the audio, and it was paid for."""
    import asyncio

    _, meter, _db = setup
    sp = _pipeline(meter, _Reporting("realtime-openai", sent=True))
    await sp._handle(asyncio.get_running_loop(), _utterance(2.0))
    assert meter.pending == pytest.approx(2.0)
