"""The session is brought back up after it dies.

The engine used to be the second one and could afford to die: the local Whisper picked up
the utterance. Whisper has been removed from the product (the user has no reason to
download one and a half gigabytes of weights), and this engine became the only one. Now
any death of it means silence instead of conversation.

It dies predictably. A realtime session has a hard ceiling of sixty minutes, after which
the server answers everything with `session_expired`. For a channel where the
conversation goes on for an hour or an hour and a half, this is not an exception but a
routine event in the middle of a session, and we caught it exactly that way, on a live
run.
"""

from __future__ import annotations

from concurrent.futures import Future

import numpy as np
import pytest

from argus.core.config import RealtimeSTTConfig
from argus.core.events import SAMPLE_RATE, Source
from argus.perception.stt.base import Utterance
from argus.perception.stt.realtime_openai import SESSION_IS_OVER, RealtimeOpenAI

NS = 1_000_000_000


def utterance() -> Utterance:
    samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
    return Utterance(t=NS, t_end=2 * NS, source=Source.THEM, samples=samples)


@pytest.fixture
def stt() -> RealtimeOpenAI:
    engine = RealtimeOpenAI(RealtimeSTTConfig(timeout_s=0.2))
    engine._thread = object()          # "session is up", with no network and no threads
    return engine


# --------------------------------------------------------------------------
# What counts as death
# --------------------------------------------------------------------------


def test_expiry_kills_the_session(stt):
    """That very refusal at the sixty-minute ceiling."""
    stt._on_message({
        "type": "error",
        "error": {"code": "session_expired", "message": "maximum duration of 60 minutes"},
    })
    assert stt._dead == "session_expired"


def test_an_ordinary_refusal_does_not(stt):
    """A refusal about one utterance must not bring down the whole session.

    Otherwise on every failed phrase the pipeline would reconnect from scratch, losing the
    context, which is the very thing this session is kept open for.
    """
    stt._on_message({
        "type": "error",
        "error": {"code": "invalid_value", "message": "bad parameter"},
    })
    assert stt._dead == ""


def test_the_fatal_list_is_short_and_explicit():
    assert "session_expired" in SESSION_IS_OVER


# --------------------------------------------------------------------------
# Reviving
# --------------------------------------------------------------------------


def test_a_dead_session_is_revived_before_the_next_utterance(stt, monkeypatch):
    revived: list[bool] = []

    def fake_warmup() -> None:
        revived.append(True)
        stt._thread = object()

    monkeypatch.setattr(stt, "close", lambda: None)
    monkeypatch.setattr(stt, "warmup", fake_warmup)

    stt._dead = "session_expired"
    stt.transcribe(utterance())

    assert revived == [True]
    assert stt.revivals == 1
    assert stt._dead == ""


def test_a_live_session_is_left_alone(stt, monkeypatch):
    """Reviving a live session would mean losing context for no reason."""
    monkeypatch.setattr(stt, "warmup", lambda: pytest.fail("the live session must not be touched"))
    stt.transcribe(utterance())
    assert stt.revivals == 0


def test_a_failed_revival_is_remembered_not_repeated(stt, monkeypatch):
    """The network is down: we say so and wait for the next utterance, instead of hammering in a
    loop.
    """
    from argus.perception.stt.realtime_openai import RealtimeSTTError

    def boom() -> None:
        raise RealtimeSTTError("network unavailable")

    monkeypatch.setattr(stt, "close", lambda: None)
    monkeypatch.setattr(stt, "warmup", boom)

    stt._dead = "session_expired"
    assert stt.transcribe(utterance()) is None
    assert stt._dead == "network unavailable"
    assert stt.failures == 1


def test_the_utterance_is_retried_once_after_a_revival(stt, monkeypatch):
    """The utterance on which the session died is handed over again, but exactly once.

    Retrying once more, at fifteen seconds an attempt, would turn the loss of one phrase into
    the loss of a minute of conversation.
    """
    from argus.perception.stt import realtime_openai as mod

    attempts: list[int] = []

    def never_answers(coro, loop):
        # We close the coroutine right away: it will never reach the event loop, and without this
        # Python complains about an unexpected abandoned coroutine.
        coro.close()
        attempts.append(1)
        if len(attempts) == 1:
            stt._dead = "session_expired"
        return Future()        # like a real one: the send is "still going", no error and no end

    monkeypatch.setattr(stt, "close", lambda: None)
    monkeypatch.setattr(stt, "warmup", lambda: None)
    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", never_answers)
    stt._loop = object()

    # There will be no answer at all: the first attempt dies on a timeout with a dead session,
    # the second simply on a timeout. We check that the second one happened and that there is no
    # third.
    assert stt.transcribe(utterance()) is None
    assert len(attempts) == 2
    assert stt.revivals == 1


# --------------------------------------------------------------------------
# A refusal on history trimming and a send failure must not ruin a live utterance
# --------------------------------------------------------------------------


def test_a_refused_history_delete_does_not_fail_the_waiting_utterance(stt):
    """Trimming sends the deletes right after the utterance, and the refusal for them arrives
    while the utterance is waiting for its text.

    Before, any `error` event closed the waiting utterance with a failure.
    """
    stt._pending = waiting = Future()

    # straight to the cloud: the refusal carries our label
    stt._on_message({"type": "error", "error": {"code": "invalid_value", "event_id": "trim-3"}})
    # through the account server: it does not forward the label, the code remains
    stt._on_message({"type": "error", "error": {"code": "item_not_found", "message": "none"}})

    assert not waiting.done()
    assert stt._pending is waiting

    # And a real refusal for the utterance still closes it.
    stt._on_message({"type": "error", "error": {"code": "invalid_value", "message": "bad"}})
    assert waiting.done() and isinstance(waiting.exception(), Exception)


def test_a_failed_send_ends_the_wait_at_once_and_marks_the_session_dead(stt):
    from argus.perception.stt.realtime_openai import RealtimeSTTError

    stt._pending = waiting = Future()
    sending: Future = Future()
    sending.set_exception(ConnectionError("socket closed"))

    stt._on_sent(sending, waiting)

    assert waiting.done() and isinstance(waiting.exception(), RealtimeSTTError)
    assert stt._dead.startswith("sending failed")      # the next attempt will bring the session up


def test_a_late_send_report_does_not_touch_the_next_utterance(stt):
    """The report about sending the previous utterance arrives when the next one is already
    waiting.
    """
    old, current = Future(), Future()
    stt._pending = current
    sending: Future = Future()
    sending.set_exception(ConnectionError("socket closed"))

    stt._on_sent(sending, old)

    assert not current.done() and stt._pending is current


def test_the_end_of_a_session_fails_the_waiting_utterance(stt):
    """The session closed: an utterance waiting in it will not get its text, and it must learn of
    that at once.
    """
    from argus.perception.stt.realtime_openai import RealtimeSTTError

    stt._pending = waiting = Future()
    stt._finish(fail="session closed")
    assert waiting.done() and isinstance(waiting.exception(), RealtimeSTTError)
    stt._finish(fail="once more")                           # a repeat: quietly, with no exception
