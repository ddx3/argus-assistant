"""Recognition through the account server: this is how the distributed build works.

Recognition used to not work for a person using the distributed build: it has no cloud
key (and must not have one), and there was nowhere else to go for audio. Now the engine
opens a socket to the server (`server/relay_stt.py`), and the whole chain is checked end
to end: the app's engine -> the real account server (uvicorn) -> a fake cloud. The key is
not on the "person's computer", neither in the environment nor in the config.

Separately, what the person sees when it fails: not "something went wrong" but
"you need to sign in" and "your balance is empty — top up".
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from argus.account.client import Account
from argus.core import friendly
from argus.core.bus import Bus
from argus.core.config import Config, RealtimeSTTConfig
from argus.core.events import SAMPLE_RATE, Source, SpeechEvent, Utterance
from argus.perception.speech import SpeechPipeline, build_stt
from argus.perception.stt.realtime_openai import RealtimeOpenAI, RealtimeSTTError, RelayRefused
from argus.server import entitlements, pricing
from test_realtime_stt import ask, shut, utterance
from test_server_relay_stt import cloud, db, free_port, relay, token  # noqa: F401 — fixtures

NS = 1_000_000_000


def signed_in(port: int, root, access: str) -> Account:
    """An app that has already signed in to the account: the pass is in memory, and we do not go to
    the network for it.
    """
    account = Account(f"http://127.0.0.1:{port}", root)
    account._data = {"refresh": "r", "email": "user@example.com"}
    account._access = access
    account._access_until = time.time() + 900
    return account


@pytest.fixture
def cfg() -> RealtimeSTTConfig:
    # The variable the direct mode would take the key from does not exist, and must not.
    return RealtimeSTTConfig(relay=True, api_key_env="NO_SUCH_KEY_VARIABLE", timeout_s=5.0)


@pytest.fixture
def account(relay, token, tmp_path) -> Account:  # noqa: F811
    return signed_in(relay, tmp_path, token)


# --------------------------------------------------------------------------
# The main point: a person with no key gets speech recognized
# --------------------------------------------------------------------------


async def test_a_person_with_no_key_gets_speech_recognised(cloud, account, cfg):  # noqa: F811
    cloud.replies = ["they push mid"]
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        ev = await ask(stt, utterance())
        assert ev is not None
        assert ev.text == "they push mid"
        assert ev.source is Source.THEM
    finally:
        await shut(stt)
    # The cloud was knocked on by the server with its own key, not by the app.
    assert cloud.headers == ["Bearer sk-test-0123456789"]


async def test_one_session_serves_the_whole_conversation(cloud, account, cfg):  # noqa: F811
    cloud.replies = ["first", "second", "third"]
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        texts = [(await ask(stt, utterance(start_s=i))).text for i in range(3)]  # type: ignore[union-attr]
    finally:
        await shut(stt)
    assert texts == ["first", "second", "third"]
    assert len(cloud.paths) == 1                  # the context accumulates in one session


async def test_the_session_config_reaches_the_cloud_as_the_server_rebuilt_it(cloud, account, cfg):  # noqa: F811
    cfg.language = "uk"
    cfg.terms = "Kubernetes"
    cfg.smart = True
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        await ask(stt, utterance())
    finally:
        await shut(stt)
    assert cloud.session_config["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-transcribe", "language": "uk", "prompt": "Kubernetes"}
    assert cloud.session_config["audio"]["input"]["turn_detection"] is None


async def test_the_server_charges_and_the_app_does_not_double_it(cloud, account, cfg, db):  # noqa: F811
    before = entitlements.balance(db, 1)
    stt = RealtimeOpenAI(cfg, account=account)
    assert stt.metered_by_server is True
    try:
        await ask(stt, utterance(seconds=2.0))
    finally:
        await shut(stt)
    await asyncio.sleep(0.3)
    assert before - entitlements.balance(db, 1) == pricing.charged(pricing.stt_cost(2.0, "normal"))
    # And the one that goes by its own key still reports by itself.
    assert RealtimeOpenAI(RealtimeSTTConfig()).metered_by_server is False


async def test_the_smart_switch_is_charged_at_the_dearer_rate(cloud, account, cfg, db):  # noqa: F811
    cfg.smart = True
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        await ask(stt, utterance(seconds=2.0))
    finally:
        await shut(stt)
    await asyncio.sleep(0.3)
    row = db.execute("SELECT cost_micro FROM usage").fetchone()
    assert row["cost_micro"] == pricing.stt_cost(2.0, "high")


# --------------------------------------------------------------------------
# What we say when it fails
# --------------------------------------------------------------------------


async def test_signed_out_says_to_sign_in(cloud, relay, tmp_path, cfg):  # noqa: F811
    account = Account(f"http://127.0.0.1:{relay}", tmp_path)          # not signed in
    stt = RealtimeOpenAI(cfg, account=account)
    with pytest.raises(RelayRefused, match="you need to sign in"):
        stt.warmup()
    assert cloud.paths == []                      # no request was even made


async def test_not_signed_in_is_remembered_for_lost_utterances_but_not_a_startup_complaint(
        cloud, relay, tmp_path, cfg):  # noqa: F811
    """A fresh install with no sign-in: at launch we stay silent (the hint about the account is
    already on the page, with a link), but if an utterance is lost after all, the reason is
    named."""
    account = Account(f"http://127.0.0.1:{relay}", tmp_path)
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        assert await ask(stt, utterance()) is None
        assert stt.last_error == "you need to sign in — it's in settings"
    finally:
        await shut(stt)

    from argus.perception.stt.realtime_openai import NotSignedIn

    assert issubclass(NotSignedIn, RelayRefused) and NotSignedIn.quiet is True

    speech = SpeechPipeline(Bus(), Config(), stt=RealtimeOpenAI(cfg, account=account))
    speech.warm_in_background()
    await speech._warm_task
    assert speech.warm_error == ""                # no complaint on top of the account hint
    assert speech.complaints() == []
    speech.close()


async def test_an_empty_balance_is_explained_in_words(cloud, account, cfg, db):  # noqa: F811
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()
    stt = RealtimeOpenAI(cfg, account=account)
    with pytest.raises(RelayRefused) as e:
        await asyncio.to_thread(stt.warmup)
    assert "your balance is empty" in str(e.value) and "top up" in str(e.value)
    assert friendly.plain(str(e.value))           # the text is fit to show as is
    await shut(stt)


async def test_an_utterance_lost_for_lack_of_money_leaves_the_reason(cloud, account, cfg, db):  # noqa: F811
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        assert await ask(stt, utterance()) is None
        assert "your balance is empty" in stt.last_error
        assert stt.failures == 1
    finally:
        await shut(stt)


async def test_the_reason_goes_away_once_it_works_again(cloud, account, cfg, db):  # noqa: F811
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        assert await ask(stt, utterance()) is None and stt.last_error
        entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")          # the person topped up
        assert (await ask(stt, utterance())) is not None
        assert stt.last_error == ""
    finally:
        await shut(stt)


async def test_a_pass_the_server_rejects_says_to_sign_in_again(cloud, relay, tmp_path, cfg):  # noqa: F811
    stt = RealtimeOpenAI(cfg, account=signed_in(relay, tmp_path, "not.a-real-token"))
    with pytest.raises(RelayRefused, match="sign in to your account again"):
        await asyncio.to_thread(stt.warmup)
    await shut(stt)


async def test_a_server_that_is_down_is_a_connection_problem_not_a_refusal(tmp_path, cfg):
    stt = RealtimeOpenAI(cfg, account=signed_in(free_port(), tmp_path, "x.y"))
    with pytest.raises(RealtimeSTTError) as e:
        await asyncio.to_thread(stt.warmup)
    assert not isinstance(e.value, RelayRefused)
    assert friendly.explain(e.value, trust_text=False) == friendly.NETWORK
    assert stt.last_error == ""                   # there is nothing to tell people here except "no connection"
    await shut(stt)


async def test_money_running_out_mid_conversation_is_said_in_words(cloud, account, cfg, db, monkeypatch):  # noqa: F811
    from argus.server import relay_stt

    monkeypatch.setattr(relay_stt, "MIN_RESERVE_S", 1.0)
    db.execute("UPDATE accounts SET balance_micro = ?", (pricing.charged(pricing.stt_cost(3.0)),))
    db.commit()
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        assert await ask(stt, utterance(seconds=1.0)) is not None      # enough for a second
        assert await ask(stt, utterance(seconds=20.0)) is None         # and not enough for twenty
        assert "top up" in stt.last_error or "balance" in stt.last_error
    finally:
        await shut(stt)


# --------------------------------------------------------------------------
# The server closed the connection: we bring the session up again
# --------------------------------------------------------------------------


async def test_a_session_the_server_closed_is_brought_back(cloud, account, cfg):  # noqa: F811
    """The cloud's session ceiling, a drop, a long silence: the server closes the socket.

    Without the "session is dead" mark, the next utterance would go into a closed socket and
    be lost, and so on until the app restarts: silence instead of conversation.
    """
    cloud.replies = ["before the drop", "after the drop"]
    stt = RealtimeOpenAI(cfg, account=account)
    try:
        assert (await ask(stt, utterance())).text == "before the drop"           # type: ignore[union-attr]
        for theirs in list(cloud.connections):
            await theirs.close()
        for _ in range(50):                        # the server does not close the client instantly
            if stt._dead:
                break
            await asyncio.sleep(0.1)
        assert stt._dead
        assert (await ask(stt, utterance())).text == "after the drop"        # type: ignore[union-attr]
        assert stt.revivals == 1
    finally:
        await shut(stt)


# --------------------------------------------------------------------------
# Mode choice and the address
# --------------------------------------------------------------------------


def test_the_server_address_for_sockets(tmp_path):
    assert Account("http://203.0.113.10:8421", tmp_path).ws_url("/relay/stt") == \
        "ws://203.0.113.10:8421/relay/stt"
    assert Account("https://api.example.com/", tmp_path).ws_url("/relay/stt") == \
        "wss://api.example.com/relay/stt"
    assert Account("127.0.0.1:8421", tmp_path).ws_url("/x") == "ws://127.0.0.1:8421/x"


def test_with_an_account_and_the_flag_the_engine_goes_through_the_server(tmp_path):
    cfg = Config()
    cfg.realtime_stt.relay = True
    account = Account("http://127.0.0.1:1", tmp_path)
    assert build_stt(cfg, account=account).account is account


def test_without_an_account_the_key_is_used_as_before(tmp_path):
    """A build of its own without `[account] server`: no accounts, the key from the environment
    works.
    """
    cfg = Config()
    cfg.realtime_stt.relay = True
    assert build_stt(cfg, account=None).account is None


def test_the_flag_off_keeps_the_direct_way_even_with_an_account(tmp_path):
    cfg = Config()
    cfg.realtime_stt.relay = False
    assert build_stt(cfg, account=Account("http://127.0.0.1:1", tmp_path)).account is None


def test_the_app_hands_its_account_to_both_channels_engines():
    """Wiring: `Argus` builds the link to the account server and hands it to the engines of both
    channels. Without this the relay would remain code on the side, and the app would keep
    looking for a key."""
    from argus.app import Argus

    cfg = Config()
    cfg.account.server = "http://127.0.0.1:1"
    cfg.realtime_stt.relay = True
    app = Argus(cfg, with_screen=False, with_brain=False)
    for source in (Source.ME, Source.THEM):
        engine = app.speech.engines[source]
        assert engine.account is app._account and engine.account is not None
        assert engine.metered_by_server is True
    assert app.speech.engines[Source.ME] is not app.speech.engines[Source.THEM]   # a session of its own per channel


def test_an_app_without_accounts_keeps_the_engines_on_their_own_key():
    from argus.app import Argus

    cfg = Config()
    cfg.account.server = ""
    cfg.realtime_stt.relay = True
    app = Argus(cfg, with_screen=False, with_brain=False)
    assert app.speech.engines[Source.THEM].account is None


def test_the_shipped_config_goes_through_the_server():
    """The distributed build takes its settings from `argus.toml`: the relay must be switched on
    there.
    """
    from pathlib import Path

    toml = Path(__file__).resolve().parents[1] / "argus.toml"
    assert Config.load(toml, overrides=False).realtime_stt.relay is True


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


class Engine:
    name = "fake"
    per_channel = False

    def __init__(self, *, server: bool, event: bool = True, last_error: str = "") -> None:
        self.metered_by_server = server
        self.event = event
        self.last_error = last_error

    def warmup(self) -> None: ...

    def close(self) -> None: ...

    def transcribe(self, u):
        if not self.event:
            return None
        return SpeechEvent(t=u.t, t_end=u.t_end, source=u.source, text="hello")


class Meter:
    exhausted = False

    def __init__(self) -> None:
        self.added: list[float] = []

    def add(self, seconds: float, tier: str = "normal") -> None:
        self.added.append(seconds)

    def close(self) -> None: ...


def make_utterance() -> Utterance:
    return Utterance(t=NS, t_end=2 * NS, source=Source.THEM,
                     samples=np.zeros(SAMPLE_RATE, dtype=np.int16))


async def test_the_pipeline_leaves_the_counting_to_the_server():
    meter = Meter()
    speech = SpeechPipeline(Bus(), Config(), stt=Engine(server=True), meter=meter)
    await speech._handle(asyncio.get_running_loop(), make_utterance())
    assert meter.added == []
    speech.close()


async def test_the_pipeline_still_counts_for_an_engine_that_goes_by_its_own_key():
    meter = Meter()
    speech = SpeechPipeline(Bus(), Config(), stt=Engine(server=False), meter=meter)
    await speech._handle(asyncio.get_running_loop(), make_utterance())
    assert meter.added == [pytest.approx(1.0)]
    speech.close()


async def test_a_dropped_utterance_puts_the_reason_among_the_complaints():
    reason = "your balance is empty — top up from $5, it's in settings"
    speech = SpeechPipeline(Bus(), Config(), stt=Engine(server=True, event=False, last_error=reason))
    await speech._handle(asyncio.get_running_loop(), make_utterance())
    assert speech.complaints() == [f"recognition: {reason}"]
    speech.close()


async def test_a_dropped_utterance_without_a_reason_adds_no_complaint():
    speech = SpeechPipeline(Bus(), Config(), stt=Engine(server=True, event=False))
    await speech._handle(asyncio.get_running_loop(), make_utterance())
    assert speech.complaints() == []
    speech.close()


async def test_our_servers_refusal_at_startup_is_shown_as_written():
    reason = "you need to sign in — it's in settings"

    class Refused(Engine):
        def warmup(self) -> None:
            raise RelayRefused(reason)

    speech = SpeechPipeline(Bus(), Config(), stt=Refused(server=True))
    speech.warm_in_background()
    await speech._warm_task
    assert speech.warm_error == reason            # rather than the generic "no access"
    speech.close()


async def test_any_other_startup_failure_still_gets_the_general_phrase():
    class Broken(Engine):
        def warmup(self) -> None:
            raise RealtimeSTTError("no key: the environment variable OPENAI_API_KEY is empty")

    speech = SpeechPipeline(Bus(), Config(), stt=Broken(server=False))
    speech.warm_in_background()
    await speech._warm_task
    assert speech.warm_error == friendly.ACCESS
    assert "OPENAI" not in speech.warm_error
    speech.close()
