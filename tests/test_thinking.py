"""A request in progress is visible while it runs.

The answer arrives in two or three seconds, and all that time the screen looks the same as
when the filter stayed silent. The two cannot be told apart, and the difference is
significant: in the first case you have to wait, in the second you have to press yourself.
Without a hint the person presses the button on top of a request that is already running,
and that second request is refused with "the assistant is still answering", burning an
attempt as well.
"""

from __future__ import annotations

import asyncio

import pytest

from argus.app import Argus
from argus.brain.assistant import Assistant
from argus.brain.base import Answer
from argus.brain.gate import Trigger
from argus.core.config import Config
from argus.core.events import Source
from argus.ui.server import snapshot


class SlowProvider:
    """A provider that holds a pause until it is released."""

    name = "slow"

    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.entered = asyncio.Event()

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False) -> Answer:
        self.entered.set()
        await self.released.wait()
        return Answer(text="done", provider=self.name)


@pytest.fixture
def app() -> Argus:
    a = Argus(Config(), with_screen=False, with_brain=False)
    a.assistant = Assistant(a.bus, a.cfg, a.clock, a.timeline, provider=SlowProvider())
    return a


async def ask_in_background(app: Argus, question: str | None = None):
    task = asyncio.create_task(app.assistant.ask_now(question))
    await asyncio.wait_for(app.assistant.provider.entered.wait(), timeout=2)
    return task


async def test_nothing_is_shown_while_idle(app):
    assert snapshot(app)["thinking"] is None


async def test_the_ask_is_visible_while_it_runs(app):
    task = await ask_in_background(app, "what about the indexes here?")

    thinking = snapshot(app)["thinking"]
    assert thinking is not None
    assert thinking["cause"] == "what about the indexes here?"
    assert thinking["trigger"] == Trigger.HOTKEY.value
    assert thinking["t"] > 0

    app.assistant.provider.released.set()
    await task


async def test_it_disappears_when_the_answer_arrives(app):
    task = await ask_in_background(app, "question")
    app.assistant.provider.released.set()
    await task
    assert snapshot(app)["thinking"] is None


async def test_it_disappears_even_when_the_model_fails(app):
    """Otherwise "thinking" would hang forever and lie until a restart.

    A provider that broke the contract and raised an exception still produces an answer with
    an error: the exception does not escape (before, it ended the voice loop or answered
    500), and the card shows the reason instead of silence.
    """

    async def boom(prompt, system=None, image=None, pinned=(), deep=False):
        raise RuntimeError("the network is down")

    app.assistant.provider.ask = boom
    answer = await app.assistant.ask_now("question")
    assert not answer.ok and answer.error
    assert app.assistant.pending is None
    assert snapshot(app)["thinking"] is None


async def test_the_cause_is_the_interlocutor_reply(app):
    """For the interlocutor's question we show their utterance, not the filter's words.

    "The interlocutor asked a question" does not say which question exactly, and by this point several have been spoken in the conversation.
    """
    from argus.core.events import SpeechEvent

    app.timeline.add_speech(SpeechEvent(
        t=1, t_end=2, source=Source.THEM, text="How did you shard the database?"
    ))
    decision = app.assistant.gate.on_hotkey(app.clock.now())
    decision.trigger = Trigger.QUESTION

    task = asyncio.create_task(app.assistant._ask(decision))
    await asyncio.wait_for(app.assistant.provider.entered.wait(), timeout=2)

    assert snapshot(app)["thinking"]["cause"] == "How did you shard the database?"

    app.assistant.provider.released.set()
    await task


async def test_the_answer_carries_its_cause_to_the_page(app):
    """The cause reaches the card, not just `meta`.

    In the header it stands in place of the filter's words: "the interlocutor asked a
    question" does not say which one exactly, and by this point several have been spoken.
    """
    task = await ask_in_background(app, "why are there two indexes here?")
    app.assistant.provider.released.set()
    await task

    app.state.answers.append(app.assistant.answers[-1])
    assert snapshot(app)["answers"][-1]["cause"] == "why are there two indexes here?"


async def test_the_answer_remembers_what_it_answered(app):
    """The reason stays in the finished answer too, otherwise there is nothing to investigate."""
    task = await ask_in_background(app, "a question about the GIL")
    app.assistant.provider.released.set()
    answer = await task
    assert answer.meta["cause"] == "a question about the GIL"
    assert answer.meta["t"] > 0
