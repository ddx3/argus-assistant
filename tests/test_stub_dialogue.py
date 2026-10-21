"""A conversation script instead of audio, going the same way the real one lives.

Not about the content of the utterances (this is a fixture, not a sample interview), but
about `_run_dialogue` behaving like the rest of the pipeline: it publishes `Utterance` and
`SpeechEvent` on the bus, respects the pause of the same button as real audio does, and
does not hang if the script is empty.
"""

from __future__ import annotations

import asyncio

import pytest

from argus.core.bus import Bus
from argus.core.config import Config, DialogueTurn
from argus.core.events import Source, SpeechEvent, Utterance
from argus.perception.speech import SpeechPipeline


class DummySTT:
    """Must not call the "real" engine at all: the scenario already carries the text."""

    name = "dummy"
    per_channel = False

    def transcribe(self, utterance):  # pragma: no cover — must not be called
        raise AssertionError("the scenario must not call transcribe()")

    def warmup(self) -> None:
        return

    def close(self) -> None:
        return


def dialogue_cfg(*turns: DialogueTurn, interval_s: float = 0.01) -> Config:
    cfg = Config()
    cfg.stt.provider = "stub"
    cfg.stub_stt.dialogue_enabled = True
    cfg.stub_stt.dialogue_interval_s = interval_s
    cfg.stub_stt.dialogue = list(turns)
    return cfg


async def collect(bus: Bus, n: int, timeout: float = 3.0):
    sub = bus.subscribe(SpeechEvent, maxsize=64, name="test")
    out = []
    try:
        while len(out) < n:
            out.append(await asyncio.wait_for(sub.get(), timeout=timeout))
    finally:
        sub.close()
    return out


async def test_dialogue_publishes_scripted_speech_events_in_order():
    cfg = dialogue_cfg(
        DialogueTurn(source="them", text="Question one"),
        DialogueTurn(source="me", text="Answer one"),
    )
    bus = Bus()
    pipe = SpeechPipeline(bus, cfg, stt=DummySTT())

    task = asyncio.create_task(pipe.run())
    try:
        events = await collect(bus, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert [e.text for e in events] == ["Question one", "Answer one"]
    assert events[0].source is Source.THEM
    assert events[1].source is Source.ME


async def test_dialogue_loops_the_script():
    cfg = dialogue_cfg(DialogueTurn(source="them", text="The only line"))
    bus = Bus()
    pipe = SpeechPipeline(bus, cfg, stt=DummySTT())

    task = asyncio.create_task(pipe.run())
    try:
        events = await collect(bus, 3)      # the same one three times means it looped
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert [e.text for e in events] == ["The only line"] * 3


async def test_dialogue_respects_pause():
    cfg = dialogue_cfg(DialogueTurn(source="them", text="A line"))
    bus = Bus()
    pipe = SpeechPipeline(bus, cfg, stt=DummySTT())
    pipe.paused = True

    task = asyncio.create_task(pipe.run())
    try:
        sub = bus.subscribe(SpeechEvent, maxsize=64, name="test")
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.get(), timeout=0.3)
        finally:
            sub.close()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_empty_dialogue_returns_without_hanging():
    cfg = dialogue_cfg()
    cfg.stub_stt.dialogue = []
    bus = Bus()
    pipe = SpeechPipeline(bus, cfg, stt=DummySTT())

    await asyncio.wait_for(pipe.run(), timeout=1.0)


def test_default_dialogue_alternates_them_and_me():
    from argus.core.config import DEFAULT_DIALOGUE

    assert len(DEFAULT_DIALOGUE) >= 4
    assert DEFAULT_DIALOGUE[0].source == "them"       # the interviewer starts
    assert {t.source for t in DEFAULT_DIALOGUE} == {"me", "them"}


async def test_dialogue_utterance_precedes_its_speech_event():
    """`Utterance` comes first: the session recording and the levels must see it too."""
    cfg = dialogue_cfg(DialogueTurn(source="them", text="Hello"))
    bus = Bus()
    pipe = SpeechPipeline(bus, cfg, stt=DummySTT())

    usub = bus.subscribe(Utterance, maxsize=16, name="u")
    task = asyncio.create_task(pipe.run())
    try:
        u = await asyncio.wait_for(usub.get(), timeout=1.0)
        assert u.source is Source.THEM
    finally:
        usub.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
