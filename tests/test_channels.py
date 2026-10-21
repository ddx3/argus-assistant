"""Two channels, two recognizers, and a safety net for the case of failure.

The "me / interlocutor" separation relies on the channels being separate right from the
device. The checks here make sure that this separation reaches the engine too: a
session-based recognizer has its own session for each channel, while a local one has a
single instance for both, because there is no point in splitting video memory in two.

The second half of the file is about substitution. A network engine fails for reasons a
local one does not have: the connection dropped, the limit ran out, a conversational
model slipped into a dialogue. The interlocutor's utterance must not be lost because of
that.
"""

from __future__ import annotations

import numpy as np

from argus.core.bus import Bus
from argus.core.config import Config
from argus.core.events import SAMPLE_RATE, Source, SpeechEvent, Utterance
from argus.perception.speech import SpeechPipeline, build_engines

NS = 1_000_000_000


def utterance(source: Source = Source.THEM, seconds: float = 1.0) -> Utterance:
    return Utterance(
        t=0,
        t_end=int(seconds * NS),
        source=source,
        samples=np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16),
    )


class FakeSTT:
    """A stub: returns the given text or stays silent."""

    per_channel = False

    def __init__(self, name: str, text: str | None = "text", engine: str = "") -> None:
        self.name = name
        self.text = text
        self.engine = engine
        self.seen: list[Utterance] = []

    def transcribe(self, u: Utterance) -> SpeechEvent | None:
        self.seen.append(u)
        if self.text is None:
            return None
        return SpeechEvent(
            t=u.t, t_end=u.t_end, source=u.source,
            text=self.text, engine=self.engine, latency_ms=1.0,
        )

    def warmup(self) -> None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------
# How many engine instances to bring up
# --------------------------------------------------------------------------


def test_an_engine_without_a_session_is_shared(monkeypatch):
    """An engine without a session is one for both channels.

    The rule outlived Whisper, for which it was written: it lived in video memory and
    computed in turn, so a second instance would have taken one and a half gigabytes and
    sped up nothing. The product no longer has a local engine, but the rule remains in the
    code, and it is easy to break silently, so it is checked on a fake rather than on a
    live engine.
    """
    from argus.perception import speech as mod

    shared = FakeSTT("shared", text="")
    shared.per_channel = False
    monkeypatch.setattr(mod, "build_stt", lambda cfg, provider=None, account=None: shared)

    engines = build_engines(Config())
    assert engines[Source.ME] is engines[Source.THEM]


def test_session_engine_gets_a_session_per_channel():
    """A session accumulates the conversation context, and that is its whole value.

    A session shared by two channels would mix both sides in that context and bring back
    the "who is speaking" problem, which separate streams solve for free.
    """
    cfg = Config()
    cfg.stt.provider = "realtime-openai"

    engines = build_engines(cfg)
    assert engines[Source.ME] is not engines[Source.THEM]


def test_shared_engine_keeps_a_single_thread():
    """A shared instance must not be called from two threads: there is only one model."""
    pipe = SpeechPipeline(Bus(), Config(), stt=FakeSTT("single"))
    assert pipe._pools[Source.ME] is pipe._pools[Source.THEM]


def test_separate_engines_get_separate_threads():
    """Otherwise the interlocutor's question would wait until the user's long reply is finished."""
    cfg = Config()
    cfg.stt.provider = "realtime-openai"

    pipe = SpeechPipeline(Bus(), cfg)
    assert pipe._pools[Source.ME] is not pipe._pools[Source.THEM]
    pipe.close()


async def test_each_channel_goes_to_its_own_engine():
    cfg = Config()
    pipe = SpeechPipeline(Bus(), cfg, stt=FakeSTT("shared"))
    me, them = FakeSTT("mine"), FakeSTT("theirs")
    pipe.engines = {Source.ME: me, Source.THEM: them}

    import asyncio
    loop = asyncio.get_running_loop()
    await pipe._handle(loop, utterance(Source.ME))
    await pipe._handle(loop, utterance(Source.THEM))

    assert len(me.seen) == 1 and me.seen[0].source is Source.ME
    assert len(them.seen) == 1 and them.seen[0].source is Source.THEM


# --------------------------------------------------------------------------
# Backup with a second engine
# --------------------------------------------------------------------------


async def test_backup_picks_up_the_utterance_the_main_engine_lost():
    """The main engine is silent, so we take the utterance from the second one: it listened to
    the same audio.

    Waiting for it here is fine: the main path has already failed, and an extra second is
    cheaper than a lost question from the interlocutor.
    """
    bus = Bus()
    sub = bus.subscribe(SpeechEvent, maxsize=8, name="test")
    main = FakeSTT("realtime", text=None)
    backup = FakeSTT("whisper", text="and why is it needed", engine="whisper-local")
    pipe = SpeechPipeline(bus, Config(), stt=main, shadow=backup)

    import asyncio
    await pipe._handle(asyncio.get_running_loop(), utterance())

    events = [await sub.get() for _ in range(sub.qsize())]

    # The shadow engine's event comes first: it stays what it was, a mark for comparison;
    # right after it comes the same utterance, put into the timeline.
    rescued = [e for e in events if not e.engine]
    assert len(rescued) == 1
    assert rescued[0].text == "and why is it needed"
    assert rescued[0].rescued_by == "whisper-local"
    assert pipe.rescued == 1


async def test_healthy_path_does_not_touch_the_backup_text():
    """When the main one coped, there is no substitution and the counter does not grow."""
    bus = Bus()
    main = FakeSTT("realtime", text="said by the main one")
    backup = FakeSTT("whisper", text="said by the second one", engine="whisper-local")
    pipe = SpeechPipeline(bus, Config(), stt=main, shadow=backup)

    import asyncio
    await pipe._handle(asyncio.get_running_loop(), utterance())

    assert pipe.rescued == 0


async def test_main_engine_label_does_not_make_event_shadow():
    """The main engine's label must not make an utterance drop out of the feed and the model."""
    bus = Bus()
    sub = bus.subscribe(SpeechEvent, maxsize=8, name="test")
    pipe = SpeechPipeline(
        bus, Config(), stt=FakeSTT("realtime", text="question", engine="realtime-openai")
    )

    import asyncio
    await pipe._handle(asyncio.get_running_loop(), utterance())

    event = await sub.get()
    assert event.text == "question"
    assert event.engine == ""
    pipe.close()


async def test_silence_stays_silence():
    """Both engines are silent — so there was no speech. An utterance must not be invented."""
    bus = Bus()
    pipe = SpeechPipeline(
        bus, Config(),
        stt=FakeSTT("realtime", text=None),
        shadow=FakeSTT("whisper", text=None, engine="whisper-local"),
    )

    import asyncio
    await pipe._handle(asyncio.get_running_loop(), utterance())

    assert pipe.rescued == 0
    assert pipe.transcribed == 0


async def test_backup_absent_is_not_an_error():
    """The comparison is off — the pipeline just loses the utterance, as before."""
    bus = Bus()
    pipe = SpeechPipeline(bus, Config(), stt=FakeSTT("realtime", text=None))

    import asyncio
    await pipe._handle(asyncio.get_running_loop(), utterance())

    assert pipe.dropped == 1
    assert pipe.rescued == 0
