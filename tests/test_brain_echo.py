"""The assistant does not listen to itself from the speakers.

Speech output plays the answer on the output device, and system audio capture hears the
whole device: an answer read aloud comes back as an "interlocutor utterance", and if it
contains a question word or a command ("solve", "calculate"), the model wakes
up again, on its own voice.
"""

from __future__ import annotations

import asyncio

from argus.brain.assistant import Assistant
from argus.brain.base import Answer
from argus.brain.gate import Trigger
from argus.context.timeline import Kind, Timeline
from argus.core.bus import Bus
from argus.core.clock import ReplayClock
from argus.core.config import Config
from argus.core.events import Source, SpeechEvent

NS = 1_000_000_000

REPLY = ("An index speeds up a table lookup because it stores the keys in sorted order. "
         "Solve for the needed key in logarithmic time.")


class Provider:
    name = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def start(self) -> None:
        pass

    async def warm(self, system=None, pinned=()) -> None:
        pass

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False) -> Answer:
        self.prompts.append(prompt)
        return Answer(text="answer", provider=self.name, latency_ms=1.0)

    async def close(self) -> None:
        pass


def make(tts: bool = True) -> tuple[Assistant, Provider]:
    cfg = Config()
    cfg.gate_brain.cooldown_s = 0
    cfg.channel["tts"] = {"enabled": tts}
    clock = ReplayClock.__new__(ReplayClock)
    clock._t = 0
    clock._t0_wall = 0
    provider = Provider()
    a = Assistant(Bus(asyncio.get_event_loop()), cfg, clock, Timeline(), provider)
    a.timeline.add_reply(10 * NS, REPLY)
    return a, provider


def heard(text: str, t_s: float = 14.0, source: Source = Source.THEM) -> SpeechEvent:
    return SpeechEvent(t=int((t_s - 2) * NS), t_end=int(t_s * NS), source=source, text=text)


async def test_the_spoken_answer_coming_back_does_not_wake_the_model():
    a, provider = make()
    await a._on_speech(heard("solve for the needed key in logarithmic time"))
    assert provider.prompts == []
    # It does not get into the timeline as the "interlocutor" either: these are not his words.
    assert not [e for e in a.timeline.since(0) if e.kind is Kind.SPEECH]


async def test_a_real_question_on_the_same_topic_is_answered():
    """A question on the same subject shares words with the answer, but not in a row."""
    a, provider = make()
    await a._on_speech(heard("What is an index in a database and why is it needed"))
    assert len(provider.prompts) == 1


async def test_a_short_phrase_is_never_taken_for_an_echo():
    a, provider = make()
    await a._on_speech(heard("Solve the key in time?"))
    assert len(provider.prompts) == 1


async def test_without_the_voice_the_guard_is_off():
    a, provider = make(tts=False)
    await a._on_speech(heard("solve for the needed key in logarithmic time"))
    assert len(provider.prompts) == 1


async def test_an_old_answer_is_not_an_echo_any_more():
    """An answer can come back from the speakers only while it is playing: a few minutes later
    the same phrase from the interlocutor is already their own words."""
    a, provider = make()
    await a._on_speech(heard("solve for the needed key in logarithmic time", t_s=10 + 600))
    assert len(provider.prompts) == 1


async def test_my_own_microphone_is_not_filtered():
    a, provider = make()
    a.gate.settings.mic_mode = a.gate.settings.system_mode        # the user's own utterances wake it too
    await a._on_speech(heard("solve for the needed key in logarithmic time",
                             source=Source.ME))
    assert len(provider.prompts) == 1
    assert a.last_decision.trigger is Trigger.QUESTION
