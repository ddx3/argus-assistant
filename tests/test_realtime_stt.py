"""The Realtime client against a fake server speaking the same protocol.

The real OpenAI is not available in tests: it needs a key, money and a network. But what
has to be checked here is not OpenAI but **our client**: whether the text is assembled
from a stream of deltas, whether it survives a server refusal, whether the pipeline hangs
when there is no answer, whether the history is trimmed, and, separately, that
`response.create` is never sent at all: this is exactly what distinguishes input
transcription from a conversational model in the role of a transcriber.

All of this is checked on our own server, which answers with the same events. Exactly one
thing stays unchecked: the exact session schema on the other side. That is why response
parsing is written by the shape of the events, not by their names, and the server's
refusal is printed to the log in full.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from argus.core.config import RealtimeSTTConfig
from argus.core.events import SAMPLE_RATE, Source, Utterance
from argus.perception.stt.realtime_openai import (
    RealtimeOpenAI,
    RealtimeSTTError,
    _text_from_event,
    _to_pcm16,
)

NS = 1_000_000_000


def utterance(seconds: float = 1.0, start_s: float = 0.0) -> Utterance:
    n = int(seconds * SAMPLE_RATE)
    return Utterance(
        t=int(start_s * NS),
        t_end=int((start_s + seconds) * NS),
        source=Source.THEM,
        samples=(np.sin(np.arange(n) * 0.05) * 8000).astype(np.int16),
    )


# --------------------------------------------------------------------------
# Fake server
# --------------------------------------------------------------------------


class FakeRealtime:
    """A server that answers like Realtime. The behavior is set by fields."""

    def __init__(self) -> None:
        self.replies: list[str] = ["hello"]     # what to "hear", one utterance per turn
        self.stream = True                       # deliver in deltas or all at once
        self.fail_with: str | None = None        # return a session error instead of an answer
        self.item_fails = False                  # return a refusal for this item, not for the session
        self.silent = False                      # do not answer at all: a timeout check
        self.close_on_commit = 0                 # how many times to cut the link at an utterance's commit
        self.ghost = False                       # before the answer, send a late answer of an earlier utterance

        self.session_config: dict | None = None
        self.audio_chunks: list[str] = []
        self.deleted: list[str] = []
        self.delete_events: list[str | None] = []    # the `event_id`s the deletes came with
        self.responses_created = 0               # how many times `response.create` was asked for
        self.turns = 0
        self._item = 0

    async def handler(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "session.update":
                self.session_config = msg["session"]
                await ws.send(json.dumps({"type": "session.updated"}))

            elif kind == "input_audio_buffer.append":
                self.audio_chunks.append(msg["audio"])

            elif kind == "input_audio_buffer.commit":
                if self.close_on_commit > 0:
                    # The link breaks in the middle of an utterance: the audio is accepted, there will be no
                    # text.
                    self.close_on_commit -= 1
                    await ws.close()
                    return
                # A real server creates the item with the audio at the commit, not at every `append`, and
                # the transcription marks that same item rather than creating a new one.
                item_id = await self._add_item(ws, "user")
                await self._respond(ws, item_id)

            elif kind == "conversation.item.delete":
                self.deleted.append(msg["item_id"])
                self.delete_events.append(msg.get("event_id"))

            elif kind == "response.create":
                # Must never happen: input transcription does not ask for an answer. We count rather than
                # fail, so the test sees the violation instead of hanging.
                self.responses_created += 1

    async def _add_item(self, ws, role: str) -> str:
        """Like a real server: `added`, followed by `done` with the same id.

        The repeat here is not an accident but a reproduction of live behavior: the client must
        filter it out, otherwise the history counter would run double.
        """
        self._item += 1
        item_id = f"item_{self._item}"
        item = {"id": item_id, "role": role}
        await ws.send(json.dumps({"type": "conversation.item.added", "item": item}))
        await ws.send(json.dumps({"type": "conversation.item.done", "item": item}))
        return item_id

    async def _respond(self, ws, item_id: str) -> None:
        if self.silent:
            return
        if self.fail_with is not None:
            await ws.send(json.dumps({
                "type": "error", "error": {"message": self.fail_with},
            }))
            return
        if self.item_fails:
            await ws.send(json.dumps({
                "type": "conversation.item.input_audio_transcription.failed",
                "item_id": item_id,
                "error": {"message": "sound not recognised"},
            }))
            return

        text = self.replies[min(self.turns, len(self.replies) - 1)]
        self.turns += 1

        if self.ghost and self._item > 1:
            # The answer to a previous utterance, for which the client has already given up on a
            # timeout, only arrives now.
            await ws.send(json.dumps({
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": f"item_{self._item - 1}",
                "transcript": "phantom",
            }))

        if self.stream:
            for word in text.split():
                await ws.send(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": item_id,
                    "delta": word + " ",
                }))
        await ws.send(json.dumps({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": item_id,
            "transcript": text,
        }))


@pytest.fixture
async def server():
    from websockets.asyncio.server import serve

    fake = FakeRealtime()
    async with serve(fake.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        fake.url = f"ws://127.0.0.1:{port}"       # type: ignore[attr-defined]
        yield fake


@pytest.fixture
def cfg(server, monkeypatch) -> RealtimeSTTConfig:
    # ASCII only: the key goes into the Authorization header, and websockets itself refuses
    # non-ASCII there, failing at connection setup, far from where the key is set.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")
    return RealtimeSTTConfig(url=server.url, timeout_s=5.0)


async def ask(stt: RealtimeOpenAI, u: Utterance):
    """Call the synchronous `transcribe` from an async test.

    The provider lives in its own thread and blocks the caller: in the pipeline it is called
    from a pool, here from a separate thread, otherwise it would freeze the fake server that
    runs in the same loop.
    """
    return await asyncio.to_thread(stt.transcribe, u)


async def shut(stt: RealtimeOpenAI) -> None:
    """Close the provider without freezing the test loop.

    `close` waits for the closing handshake, and the fake server answers it; it runs in the
    same loop. Calling `close` directly would block the one who has to answer and run into
    the timeout for sure.
    """
    await asyncio.to_thread(stt.close)


# --------------------------------------------------------------------------
# The main path
# --------------------------------------------------------------------------


async def test_transcribes_from_stream_of_deltas(server, cfg):
    server.replies = ["they push mid"]
    stt = RealtimeOpenAI(cfg)
    try:
        ev = await ask(stt, utterance())
        assert ev is not None
        assert ev.text == "they push mid"
        assert ev.engine == stt.label
        assert ev.source is Source.THEM
        assert ev.latency_ms is not None and ev.latency_ms > 0
    finally:
        await shut(stt)


async def test_transcribes_when_answer_comes_whole(server, cfg):
    """If the server delivered the transcript all at once, without a stream of deltas, take it
    from there.

    Deltas are the main path, but relying only on them means losing the whole text at the
    slightest change on the other side.
    """
    server.stream = False
    server.replies = ["creeps under the tower"]
    stt = RealtimeOpenAI(cfg)
    try:
        ev = await ask(stt, utterance())
        assert ev is not None and ev.text == "creeps under the tower"
    finally:
        await shut(stt)


async def test_keeps_one_session_across_utterances(server, cfg):
    """One session for the whole conversation: that is the whole point of the provider.

    The context accumulates in it by itself, and jargon is resolved from the accumulated
    conversation. Reconnecting on every utterance would reset what has accumulated and drop
    the quality to the level of an ordinary transcriber.
    """
    server.replies = ["first", "second", "third"]
    stt = RealtimeOpenAI(cfg)
    try:
        texts = [
            (await ask(stt, utterance(start_s=i))).text        # type: ignore[union-attr]
            for i in range(3)
        ]
        assert texts == ["first", "second", "third"]
        assert server.turns == 3
        assert stt.calls == 3
    finally:
        await shut(stt)


async def test_session_configures_native_transcription_and_our_own_turn_detection(server, cfg):
    """A separate model listens to the input, and the utterance boundaries are cut by our VAD.

    The server itself must not cut utterances: comparing engines makes sense only on the same
    stretches of audio. If the server cuts them, the engines get different slicing, and the
    discrepancies would come from phrase boundaries rather than from recognition.
    """
    stt = RealtimeOpenAI(cfg)
    try:
        await ask(stt, utterance())
        conf = server.session_config
        assert conf is not None
        input_cfg = conf["audio"]["input"]
        assert input_cfg["turn_detection"] is None
        assert input_cfg["transcription"]["model"] == cfg.transcribe_model
    finally:
        await shut(stt)


async def test_response_create_is_never_sent(server, cfg):
    """Input transcription does not ask for an answer: this model has nothing to answer with.

    This is the architectural protection against slipping into dialogue: not a heuristic on
    top of the answer, but the absence of the request for an answer itself.
    """
    stt = RealtimeOpenAI(cfg)
    try:
        for i in range(3):
            await ask(stt, utterance(start_s=i))
        assert server.responses_created == 0
    finally:
        await shut(stt)


async def test_empty_transcript_is_dropped(server, cfg):
    """An empty transcript means "could not make out any speech", not an utterance.

    The speech detector also fires on keyboard noise; letting emptiness into the timeline is
    worse than skipping it.
    """
    server.replies = ["   "]
    stt = RealtimeOpenAI(cfg)
    try:
        assert await ask(stt, utterance()) is None
    finally:
        await shut(stt)


async def test_item_level_failure_returns_none_not_exception(server, cfg):
    """A refusal for a specific item (not for the whole session) is simply a lost utterance."""
    server.item_fails = True
    stt = RealtimeOpenAI(cfg)
    try:
        assert await ask(stt, utterance()) is None
        assert stt.failures == 1
    finally:
        await shut(stt)


# --------------------------------------------------------------------------
# Refusals: a shadow engine has no right to bring down the pipeline
# --------------------------------------------------------------------------


async def test_server_error_returns_none_not_exception(server, cfg):
    server.fail_with = "invalid session schema"
    stt = RealtimeOpenAI(cfg)
    try:
        assert await ask(stt, utterance()) is None
        assert stt.failures == 1
    finally:
        await shut(stt)


async def test_silent_server_times_out_and_does_not_hang(server, cfg):
    """Silence on the other side must not hang the pipeline forever."""
    server.silent = True
    cfg.timeout_s = 1.0
    stt = RealtimeOpenAI(cfg)
    try:
        started = asyncio.get_running_loop().time()
        assert await ask(stt, utterance()) is None
        waited = asyncio.get_running_loop().time() - started
        assert waited < 3.0, f"waited {waited:.1f} s instead of the 1 s timeout"
        assert stt.failures == 1
    finally:
        await shut(stt)


async def test_recovers_after_a_failed_turn(server, cfg):
    """One failure must not break the session for the rest of the run."""
    server.fail_with = "temporary"
    stt = RealtimeOpenAI(cfg)
    try:
        assert await ask(stt, utterance()) is None
        server.fail_with = None
        server.replies = ["I can hear again"]
        ev = await ask(stt, utterance(start_s=2))
        assert ev is not None and ev.text == "I can hear again"
    finally:
        await shut(stt)


def test_missing_key_says_which_variable(monkeypatch):
    """The message must name the variable, not say "authorization error"."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    stt = RealtimeOpenAI(RealtimeSTTConfig())
    with pytest.raises(RealtimeSTTError, match="OPENAI_API_KEY"):
        stt.warmup()


async def test_transcribe_without_key_returns_none(monkeypatch, server):
    """Without a key the shadow engine stays silent instead of crashing recognition."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    stt = RealtimeOpenAI(RealtimeSTTConfig(url=server.url, timeout_s=2.0))
    assert await ask(stt, utterance()) is None


# --------------------------------------------------------------------------
# Session history
# --------------------------------------------------------------------------


async def test_history_is_trimmed_to_the_window(server, cfg):
    """The history is re-billed on every turn: without trimming, the bill grows quadratically.

    Every turn adds one item (the user's audio: the transcription only marks it and does not
    create a new one), so four turns give four items; with a window of two, the last two
    remain and the first two are deleted.
    """
    cfg.keep_turns = 2
    server.replies = ["one", "two", "three", "four"]
    stt = RealtimeOpenAI(cfg)
    try:
        for i in range(4):
            await ask(stt, utterance(start_s=i))
        await asyncio.sleep(0.3)               # the deletes go right after
        assert server.deleted == ["item_1", "item_2"]
    finally:
        await shut(stt)


async def test_repeated_item_id_is_counted_once(server, cfg):
    """`added` and `done` carry the same id: it must be counted once.

    Otherwise the history window effectively halves and twice as much as needed is deleted.
    """
    cfg.keep_turns = 100                       # trimming must not fire
    server.replies = ["one", "two"]
    stt = RealtimeOpenAI(cfg)
    try:
        for i in range(2):
            await ask(stt, utterance(start_s=i))
        await asyncio.sleep(0.2)
        assert stt._items == ["item_1", "item_2"]
        assert server.deleted == []
    finally:
        await shut(stt)


async def test_zero_keep_turns_disables_trimming(server, cfg):
    cfg.keep_turns = 0
    server.replies = ["one", "two", "three"]
    stt = RealtimeOpenAI(cfg)
    try:
        for i in range(3):
            await ask(stt, utterance(start_s=i))
        await asyncio.sleep(0.2)
        assert server.deleted == []
    finally:
        await shut(stt)


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------


def test_audio_is_resampled_to_what_realtime_expects():
    """16 kHz inside, 24 kHz outside.

    Without resampling the model would hear speech sped up one and a half times, and it would
    look not like a breakage but like bad recognition quality.
    """
    src = np.zeros(SAMPLE_RATE, dtype=np.int16)          # exactly one second
    out = _to_pcm16(src, SAMPLE_RATE, 24_000)
    assert len(out) // 2 == pytest.approx(24_000, rel=0.01)


def test_same_rate_needs_no_resampling():
    src = (np.arange(100) * 100).astype(np.int16)
    assert _to_pcm16(src, 16_000, 16_000) == src.tobytes()


# --------------------------------------------------------------------------
# Parsing events: by shape, not by names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["response.text.delta", "response.output_text.delta",
     "response.output_audio_transcript.delta",
     "conversation.item.input_audio_transcription.delta"],
)
def test_any_text_delta_is_picked_up(kind: str):
    """Event names in Realtime have changed more than once.

    Parsing them from a list means breaking on every rename, so any event with a string field
    `delta` is taken.
    """
    assert _text_from_event({"type": kind, "delta": "Roshan"}) == "Roshan"


def test_audio_deltas_are_not_mistaken_for_text():
    assert _text_from_event({"type": "response.output_audio.delta", "delta": "AAAA"}) == ""


def test_non_delta_events_are_ignored():
    assert _text_from_event({"type": "response.created"}) == ""
    assert _text_from_event({"type": "response.output_text.delta", "delta": None}) == ""


# --------------------------------------------------------------------------
# Session setup: terms go into the model's prompt, not into its role
# --------------------------------------------------------------------------


def test_terms_become_the_transcription_prompt():
    """This model has nothing to reply with, so the terms are a hint, not a ban."""
    cfg = RealtimeSTTConfig(terms="GIL, Kubernetes")
    engine = RealtimeOpenAI(cfg)
    conf = engine._session_config()
    assert conf["session"]["audio"]["input"]["transcription"]["prompt"] == "GIL, Kubernetes"


def test_without_terms_no_prompt_is_sent():
    """An empty list must not go into the session as an empty string."""
    cfg = RealtimeSTTConfig()
    engine = RealtimeOpenAI(cfg)
    conf = engine._session_config()
    assert "prompt" not in conf["session"]["audio"]["input"]["transcription"]


def test_language_is_passed_to_the_transcription_model():
    cfg = RealtimeSTTConfig(language="en")
    engine = RealtimeOpenAI(cfg)
    conf = engine._session_config()
    assert conf["session"]["audio"]["input"]["transcription"]["language"] == "en"


def test_no_language_lets_the_model_detect_it():
    cfg = RealtimeSTTConfig(language=None)
    engine = RealtimeOpenAI(cfg)
    conf = engine._session_config()
    assert "language" not in conf["session"]["audio"]["input"]["transcription"]


# --------------------------------------------------------------------------
# Smart recognition: when enabled, the full model listens
# --------------------------------------------------------------------------


def _model_of(engine: RealtimeOpenAI) -> str:
    return engine._session_config()["session"]["audio"]["input"]["transcription"]["model"]


def test_the_switch_picks_the_model_that_listens():
    plain = RealtimeOpenAI(RealtimeSTTConfig())
    assert RealtimeSTTConfig().smart is False                 # off by default
    assert _model_of(plain) == "gpt-4o-mini-transcribe"
    assert plain.tier == "normal"

    sharp = RealtimeOpenAI(RealtimeSTTConfig(smart=True))
    assert _model_of(sharp) == "gpt-4o-transcribe"
    assert sharp.tier == "high"


def test_the_accurate_model_is_a_setting_not_a_constant():
    sharp = RealtimeOpenAI(RealtimeSTTConfig(smart=True, accurate_model="another-model"))
    assert _model_of(sharp) == "another-model"


def test_the_tier_is_that_of_the_open_session_not_of_the_config():
    """The person clicked "smart", but the session is still the old one: until it reopens,
    the regular model listens to the sound, so it is paid for at the regular price."""
    cfg = RealtimeSTTConfig()
    engine = RealtimeOpenAI(cfg)
    engine._session_config()

    cfg.smart = True
    assert engine.tier == "normal"

    assert _model_of(engine) == "gpt-4o-transcribe"      # this is what a reopen looks like
    assert engine.tier == "high"


def test_the_engine_name_follows_the_model_that_listens():
    cfg = RealtimeSTTConfig()
    engine = RealtimeOpenAI(cfg)
    assert engine.label.endswith("gpt-4o-mini-transcribe")
    cfg.smart = True
    assert engine.label.endswith("gpt-4o-transcribe")
    assert RealtimeOpenAI(cfg, label="my own").label == "my own"


def test_the_switch_is_a_yes_or_no():
    with pytest.raises(ValueError):
        RealtimeSTTConfig(smart="ultra")


# --------------------------------------------------------------------------
# Reconnect
# --------------------------------------------------------------------------


async def test_the_new_session_listens_after_a_reconnect(server, cfg):
    """Caught on a live run on 2026-10-05: after `session_expired` the session came up, but the
    very first utterance was lost on a timeout, and all the following ones with "Event loop
    is closed".

    `close()` raises the "we are closing" flag, and if `reconnect` did not lower it back, the
    new session exited on the server's very first event (`session.updated`) and closed behind
    it the loop into which the utterances were later written.
    """
    server.replies = ["before", "after"]
    stt = RealtimeOpenAI(cfg)
    try:
        assert (await ask(stt, utterance())).text == "before"

        assert await asyncio.to_thread(stt.reconnect, "check")
        await asyncio.sleep(0.3)               # enough for the session to exit on its own

        ev = await ask(stt, utterance(start_s=2))
        assert ev is not None and ev.text == "after"
        assert stt.failures == 0
    finally:
        await shut(stt)


async def test_a_session_that_expired_is_brought_back_by_the_next_utterance(server, cfg):
    """The same path it takes in real life: the server said `session_expired`, and the next
    utterance brings the session up by itself and is not lost."""
    server.replies = ["one", "two"]
    stt = RealtimeOpenAI(cfg)
    try:
        assert (await ask(stt, utterance())).text == "one"

        stt._on_message({"type": "error", "error": {"code": "session_expired"}})
        assert stt._dead == "session_expired"

        ev = await ask(stt, utterance(start_s=2))
        assert ev is not None and ev.text == "two"
        assert stt.revivals == 1 and stt.failures == 0
    finally:
        await shut(stt)


async def test_the_new_session_does_not_delete_the_old_sessions_items(server, cfg):
    """After a reconnect the item numbers of the old session are unknown to the new one.

    Before, `_items` survived the reconnect, and trimming sent the new session a deletion of
    foreign numbers; the server answered with a refusal, and the refusal brought down the
    waiting utterance.
    """
    cfg.keep_turns = 2
    server.replies = ["one", "two", "three", "four"]
    stt = RealtimeOpenAI(cfg)
    try:
        for i in range(3):
            await ask(stt, utterance(start_s=i))
        await asyncio.sleep(0.3)
        assert server.deleted == ["item_1"]

        assert await asyncio.to_thread(stt.reconnect, "check")
        assert stt._items == []                    # the new session is a clean list

        ev = await ask(stt, utterance(start_s=4))
        await asyncio.sleep(0.3)
        assert ev is not None and ev.text == "four"
        assert server.deleted == ["item_1"]        # the old numbers did not go to the new session
    finally:
        await shut(stt)


async def test_history_deletes_carry_a_mark(server, cfg):
    """A deletion goes out with a mark in `event_id`: it is used to tell a refusal of the deletion
    from a refusal for an utterance.
    """
    cfg.keep_turns = 1
    server.replies = ["one", "two", "three"]
    stt = RealtimeOpenAI(cfg)
    try:
        for i in range(3):
            await ask(stt, utterance(start_s=i))
        await asyncio.sleep(0.3)
        assert len(server.delete_events) == 2
        assert all(e and e.startswith("trim-") for e in server.delete_events)
        assert len(set(server.delete_events)) == 2          # the labels differ
    finally:
        await shut(stt)


# --------------------------------------------------------------------------
# The session died in the middle of an utterance, late answers
# --------------------------------------------------------------------------


async def test_a_session_that_dies_mid_utterance_does_not_hold_the_caller_for_the_timeout(server, cfg):
    """The link broke after the commit: there will be no answer, and waiting the whole
    `timeout_s` is pointless.

    Before, `transcribe` stood for all fifteen seconds (five in the test) and only then took
    up the single retry; while it stood, the pipeline did not read the audio of any channel.
    """
    server.close_on_commit = 1
    server.replies = ["after the drop"]
    cfg.timeout_s = 5.0
    stt = RealtimeOpenAI(cfg)
    try:
        started = asyncio.get_running_loop().time()
        ev = await ask(stt, utterance())
        waited = asyncio.get_running_loop().time() - started
        assert waited < 3.0, f"waited {waited:.1f} s although the session closed at once"
        assert ev is not None and ev.text == "after the drop"      # a retry in the new session
        assert stt.revivals == 1
    finally:
        await shut(stt)


async def test_a_late_answer_of_an_earlier_utterance_is_not_taken_for_the_current_one(server, cfg):
    """A late `completed` of an earlier utterance arrives while the next one is waiting.

    Before, nobody checked the events against their numbers, and foreign text became the
    answer to the current utterance.
    """
    server.ghost = True
    server.replies = ["first", "second"]
    stt = RealtimeOpenAI(cfg)
    try:
        assert (await ask(stt, utterance())).text == "first"                   # type: ignore[union-attr]
        assert (await ask(stt, utterance(start_s=2))).text == "second"          # type: ignore[union-attr]
    finally:
        await shut(stt)


def test_a_late_event_of_an_old_item_is_dropped_but_an_unknown_id_is_not():
    """We drop only what is certainly an earlier one; an unknown number is let through (the event
    schema is not ours: better to accept foreign text than to lose all the utterances)."""
    engine = RealtimeOpenAI(RealtimeSTTConfig())
    engine._items = ["item_1", "item_2"]
    engine._turn_item = "item_2"
    assert engine._is_stale("item_1") is True
    assert engine._is_stale("item_2") is False
    assert engine._is_stale("item_xyz") is False
    assert engine._is_stale(None) is False


# --------------------------------------------------------------------------
# Whether the audio left (the pipeline writes off seconds by this flag)
# --------------------------------------------------------------------------


async def test_audio_sent_is_true_once_the_audio_left_even_if_nothing_came_back(server, cfg):
    server.silent = True
    cfg.timeout_s = 1.0
    stt = RealtimeOpenAI(cfg)
    try:
        assert stt.audio_sent is False
        assert await ask(stt, utterance()) is None
        assert stt.audio_sent is True              # it went out and was paid for to the provider, though there is no text
        server.silent = False
        assert await ask(stt, utterance(start_s=2)) is not None
        assert stt.audio_sent is True
    finally:
        await shut(stt)


async def test_audio_sent_is_false_when_the_session_never_started(monkeypatch, server):
    """No key means the sound never left the machine, so there is nothing to charge for."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    stt = RealtimeOpenAI(RealtimeSTTConfig(url=server.url, timeout_s=2.0))
    assert await ask(stt, utterance()) is None
    assert stt.audio_sent is False
