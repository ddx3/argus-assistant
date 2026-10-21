"""Tests for the "wake the model or not" decision and for building the request.

The filter is the only thing between the timeline and the subscription limits, which
are counted in five-hour windows. A mistake here is not visible right away: the limits
simply run out at the wrong moment. So it is tested in detail and without a model.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from argus.assist.prompt import compose
from argus.assist.settings import AssistSettings
from argus.brain.assistant import Assistant, default_system
from argus.brain.base import Answer
from argus.brain.gate import Gate, GateSettings, ReplyMode, Trigger, looks_like_question
from argus.context.timeline import Entry, Kind, Timeline
from argus.core.bus import Bus
from argus.core.clock import ReplayClock
from argus.core.config import Config
from argus.core.events import ScreenBlock, ScreenEvent, Source, SpeechEvent

NS = 1_000_000_000


def speech_entry(text: str, source: Source = Source.THEM, t_s: float = 1.0) -> Entry:
    return Entry(t=int(t_s * NS), kind=Kind.SPEECH, source=source, text=text)


# --------------------------------------------------------------------------
# Question recognition
# --------------------------------------------------------------------------


# The inputs stay Russian on purpose: the gate's question and filler word lists are mostly
# Russian (with a few English words), and these tests are about exactly those lists.
@pytest.mark.parametrize(
    "text",
    [
        "Какая сложность у быстрой сортировки?",
        "Расскажите про ваш опыт работы с базами данных",   # without a question mark
        "Что такое индекс в базе данных",
        "Почему вы ушли с прошлой работы",
        "Напишите функцию, которая разворачивает список",
        "Объясните разницу между списком и кортежем",
        "Сколько лет вы этим занимаетесь",
        "What is the time complexity of quicksort?",
        "Explain the difference between a list and a tuple",
        "А что насчёт индексов",          # live speech starts with a particle
        "Ну и как вы это решали",
        "Слушайте, а почему именно так",
        "Реализуйте бинарный поиск",
        # Live run: the detector glued two phrases into one utterance, and the question ended up
        # in the second sentence.
        "Привет, это проверка связи. Расскажите про ваш опыт работы с базами данных",
        "Понятно. А почему вы выбрали именно этот подход",
    ],
)
def test_questions_are_recognised(text: str):
    """The question mark is lost in recognition, and live speech has none anyway.

    So we also look at the first word: in Russian a question almost always starts with it.
    Otherwise half of the questions asked would slip past.
    """
    assert looks_like_question(text)


# Russian on purpose, as above: these are the Russian fillers and statements the gate must not answer.
@pytest.mark.parametrize(
    "text",
    [
        "Ага, понятно",
        "Да, я вас слышу",
        "Хорошо, давайте продолжим",
        "Ну вот примерно так это и работает",
        "Спасибо большое",
        "Понятно. Ну вот примерно так это и работает. Спасибо",
    ],
)
def test_statements_are_not_questions(text: str):
    assert not looks_like_question(text)


# --------------------------------------------------------------------------
# Filter
# --------------------------------------------------------------------------


def test_question_from_interlocutor_wakes_the_model():
    gate = Gate()
    d = gate.on_speech(speech_entry("Tell me about your experience"), now=100 * NS)
    assert d.wake
    assert d.trigger is Trigger.QUESTION


def test_own_speech_never_wakes_the_model():
    """There is no reason to answer yourself — and the limit would burn."""
    gate = Gate()
    d = gate.on_speech(speech_entry("What is the complexity?", source=Source.ME), now=100 * NS)
    assert not d.wake


def test_mic_question_wakes_the_model_when_enabled():
    """A face-to-face conversation through one microphone — there are no separate channels at
    all.
    """
    gate = Gate(GateSettings(mic_mode=ReplyMode.BY_FORM))
    d = gate.on_speech(speech_entry("What is the complexity?", source=Source.ME), now=100 * NS)
    assert d.wake
    assert d.trigger is Trigger.QUESTION
    assert d.source is Source.ME


def test_mic_mode_all_wakes_on_any_own_speech():
    """`ALL` — no shape filter at all, even a short utterance wakes the model."""
    gate = Gate(GateSettings(mic_mode=ReplyMode.ALL))
    d = gate.on_speech(speech_entry("uh-huh", source=Source.ME), now=100 * NS)
    assert d.wake
    assert d.source is Source.ME


def test_mic_mode_wake_word_requires_the_name():
    gate = Gate(GateSettings(mic_mode=ReplyMode.WAKE_WORD, wake_word="Argus"))
    ignored = gate.on_speech(speech_entry("what is the weather here", source=Source.ME), now=100 * NS)
    assert not ignored.wake
    assert gate.blocked_no_wake == 1

    named = gate.on_speech(
        speech_entry("Argus, what is the weather here", source=Source.ME), now=200 * NS
    )
    assert named.wake
    assert named.source is Source.ME


def test_mic_mode_wake_word_does_not_match_inside_another_word():
    """"Argus" must not be found inside a random "argument"."""
    gate = Gate(GateSettings(mic_mode=ReplyMode.WAKE_WORD, wake_word="Argus"))
    d = gate.on_speech(speech_entry("what argument does the function take", source=Source.ME), now=100 * NS)
    assert not d.wake


def test_system_mode_off_ignores_the_interlocutor():
    """The same ladder as for the microphone — but for the interlocutor."""
    gate = Gate(GateSettings(system_mode=ReplyMode.OFF))
    d = gate.on_speech(speech_entry("What is the complexity of quicksort?"), now=100 * NS)
    assert not d.wake
    assert d.reason == "reaction to the other party is off"


def test_system_mode_all_wakes_on_any_interlocutor_speech():
    """`ALL` for the interlocutor — no shape filter, even "uh-huh" wakes the model."""
    gate = Gate(GateSettings(system_mode=ReplyMode.ALL))
    d = gate.on_speech(speech_entry("uh-huh"), now=100 * NS)
    assert d.wake
    assert d.source is Source.THEM


def test_system_mode_wake_word_requires_the_name():
    """A group call/lecture — answer only when the bot is called by name."""
    gate = Gate(GateSettings(system_mode=ReplyMode.WAKE_WORD, wake_word="Argus"))
    ignored = gate.on_speech(speech_entry("what is the weather here"), now=100 * NS)
    assert not ignored.wake
    assert gate.blocked_no_wake == 1

    named = gate.on_speech(speech_entry("Argus, what is the weather here"), now=200 * NS)
    assert named.wake
    assert named.source is Source.THEM


def test_mic_and_system_modes_are_independent():
    """A setting of one source must not leak into the other."""
    gate = Gate(GateSettings(mic_mode=ReplyMode.OFF, system_mode=ReplyMode.ALL))
    assert not gate.on_speech(speech_entry("uh-huh", source=Source.ME), now=100 * NS).wake
    assert gate.on_speech(speech_entry("uh-huh", source=Source.THEM), now=100 * NS).wake


def test_short_reply_is_ignored():
    gate = Gate()
    assert not gate.on_speech(speech_entry("Uh-huh"), now=100 * NS).wake
    assert gate.blocked_short == 1


def test_statement_does_not_wake_the_model():
    gate = Gate()
    d = gate.on_speech(speech_entry("Well, that is roughly how it works"), now=100 * NS)
    assert not d.wake
    assert gate.blocked_not_question == 1


def test_cooldown_protects_the_quota():
    """The main protection of the limits: the model must not be pulled several times in a row."""
    gate = Gate(GateSettings(cooldown_s=10.0))
    first = speech_entry("Tell me about databases", t_s=1)
    assert gate.on_speech(first, now=10 * NS).wake
    gate.accept(10 * NS, "prompt")

    second = speech_entry("And what about indexes", t_s=12)
    blocked = gate.on_speech(second, now=12 * NS)
    assert not blocked.wake
    assert "pause" in blocked.reason

    assert gate.on_speech(second, now=25 * NS).wake


def test_session_limit_is_a_hard_stop():
    """A safeguard against an infinite loop: limits must not burn silently."""
    gate = Gate(GateSettings(max_calls=2, cooldown_s=0))
    entry = speech_entry("Tell me about databases")
    for i in range(2):
        assert gate.on_speech(entry, now=(i + 1) * 100 * NS).wake
        gate.accept((i + 1) * 100 * NS, f"prompt {i}")
    blocked = gate.on_speech(entry, now=1000 * NS)
    assert not blocked.wake
    assert "limit" in blocked.reason


def test_same_question_twice_is_not_asked_again():
    gate = Gate(GateSettings(cooldown_s=0))
    entry = speech_entry("Tell me about databases")
    assert gate.on_speech(entry, now=100 * NS).wake
    gate.accept(100 * NS, entry.text)
    repeat = gate.on_speech(entry, now=200 * NS)
    assert not repeat.wake
    assert gate.blocked_repeat == 1


def test_hotkey_ignores_the_cooldown():
    """The person pressed it themselves — so they know what they want, and there is no need to
    make them wait.
    """
    gate = Gate(GateSettings(cooldown_s=60.0))
    gate.accept(100 * NS, "prompt")
    d = gate.on_hotkey(now=101 * NS)
    assert d.wake
    assert d.trigger is Trigger.HOTKEY


def test_hotkey_still_respects_the_session_limit():
    """The person's button has its own ceiling (the same `max_calls`): automatic triggers do not
    eat it (`test_automatic_triggers_cannot_use_up_the_hotkey`), but it is not infinite
    either — a stuck key must not burn money."""
    gate = Gate(GateSettings(max_calls=1, cooldown_s=0))
    gate.accept(0, "prompt", manual=True)
    assert not gate.on_hotkey(now=100 * NS).wake


def test_screen_reaction_is_off_by_default():
    """The screen is noisier than speech, and a reaction to it would quickly eat the limits."""
    gate = Gate()
    assert not gate.on_screen(new_lines=50, now=100 * NS).wake


def test_screen_reaction_needs_enough_new_lines():
    gate = Gate(GateSettings(react_to_screen=True, screen_new_lines=5, cooldown_s=0))
    assert not gate.on_screen(new_lines=2, now=100 * NS).wake
    assert gate.on_screen(new_lines=9, now=100 * NS).wake


def test_idle_trigger_is_off_by_default():
    gate = Gate()
    assert not gate.on_idle(quiet_s=999, now=100 * NS).wake


def test_stats_explain_why_the_model_stayed_silent():
    """When there is no answer, the reason must be visible at once, not searched for."""
    gate = Gate(GateSettings(cooldown_s=100.0))
    gate.on_speech(speech_entry("Uh-huh"), now=NS)
    gate.on_speech(speech_entry("Well, that is how it works"), now=2 * NS)
    stats = gate.stats()
    assert stats["short"] == 1
    assert stats["not a question"] == 1
    assert stats["filtered out"] == 2


# --------------------------------------------------------------------------
# Building the request
# --------------------------------------------------------------------------


class FakeProvider:
    """A stand-in model: remembers what it was asked about."""

    name = "fake"

    def __init__(self, reply: str = "answer") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.images: list[bytes | None] = []
        self.deeps: list[bool] = []
        self.started = False
        self.warmed: list[tuple[str | None, list[bytes]]] = []
        self.pinned: list[list[bytes]] = []

    async def start(self) -> None:
        self.started = True

    async def warm(self, system: str | None = None, pinned: Sequence[bytes] = ()) -> None:
        self.warmed.append((system, list(pinned)))

    async def ask(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
    ) -> Answer:
        self.prompts.append(prompt)
        self.pinned.append(list(pinned))
        self.systems.append(system)
        self.images.append(image)
        self.deeps.append(deep)
        return Answer(text=self.reply, provider=self.name, latency_ms=1.0)

    async def close(self) -> None:
        pass


class FakeStreamingProvider(FakeProvider):
    """The same provider, but with `ask_stream`: in chunks, not as one answer.

    `ask_stream` is not part of `LLMProvider`; it is an optional capability (`getattr` in
    `Assistant._ask`). It is a separate fake, not a flag on `FakeProvider`, so that the
    tests of the ordinary path are not touched by logic that does not exist there.
    """

    def __init__(self, chunks: Sequence[str] = ("answer",)) -> None:
        super().__init__(reply="".join(chunks))
        self.chunks = list(chunks)

    async def ask_stream(
        self,
        prompt: str,
        system: str | None = None,
        image: bytes | None = None,
        pinned: Sequence[bytes] = (),
        deep: bool = False,
        *,
        info: dict | None = None,
    ):
        self.prompts.append(prompt)
        self.pinned.append(list(pinned))
        self.systems.append(system)
        self.images.append(image)
        self.deeps.append(deep)
        if info is not None:
            info["model"] = "fake-stream"
        for chunk in self.chunks:
            yield chunk


def make_assistant(provider: FakeProvider, **gate_overrides) -> Assistant:
    cfg = Config()
    for key, value in gate_overrides.items():
        setattr(cfg.gate_brain, key, value)
    bus = Bus(asyncio.get_event_loop())
    clock = ReplayClock.__new__(ReplayClock)
    clock._t = 0
    clock._t0_wall = 0
    return Assistant(bus, cfg, clock, Timeline(), provider)


async def test_prompt_carries_the_conversation_and_the_screen():
    """The model must see both the conversation and the screen — that is the whole point of the
    project.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM,
                    text="What is the complexity of quicksort?")
    )
    a.timeline.add_screen(
        ScreenEvent(t=2 * NS, blocks=[ScreenBlock(text="Question 3 of 10", box=(0, 0, 100, 20))])
    )
    await a.ask_now()

    prompt = provider.prompts[0]
    assert "quicksort" in prompt
    assert "Question 3 of 10" in prompt
    assert "What is on the screen now" in prompt


async def test_hotkey_answers_your_own_last_words_when_nobody_else_spoke():
    """The hotkey with no typed text really looks for the last utterance, not only the
    interlocutor's: its `decision.source` is always `None`, and with a fallback to
    `Source.THEM` `last_speech` would not find the user's own words, even when there was no
    interlocutor in the conversation at all (a solo test, streaming to oneself).
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.ME, text="Hello, can you hear me?")
    )
    answer = await a.ask_now()

    assert "Answer my own question, asked aloud" in provider.prompts[0]
    assert "Hello, can you hear me?" in provider.prompts[0]
    assert answer.meta["cause"] == "Hello, can you hear me?"


async def test_system_prompt_explains_who_is_who():
    """Without this the model would not understand that "Me" and "Them"
    are different people.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    await a.ask_now("question")
    system = provider.systems[0]
    assert "Them" in system
    assert "Screen" in system
    assert "Them" in default_system(False)


async def test_explicit_question_overrides_the_timeline():
    provider = FakeProvider()
    a = make_assistant(provider)
    await a.ask_now("A separate question of my own")
    assert "A separate question of my own" in provider.prompts[0]


def flat(text: str) -> str:
    """Without line breaks: a prompt phrase must not break the test because of a line wrap."""
    return " ".join(text.split())


def after_request(prompt: str) -> str:
    return prompt.split("# Request")[-1].strip()


def before_request(prompt: str) -> str:
    return prompt.split("# Request")[0]


async def test_next_question_sees_the_previous_answer():
    """Live chat: to "even harder" the model answered "I need the previous text".

    Its own answers did not get into the timeline, and every request was the first one for
    it. Now the answer goes into the timeline together with the typed request.
    """
    provider = FakeProvider("$$E = mc^2$$")
    a = make_assistant(provider)
    await a.ask_now("give me the formula as latex")
    await a.ask_now("even harder")

    first, second = provider.prompts
    assert "Assistant" not in before_request(first)
    seen = before_request(second)
    assert "Me → assistant: give me the formula as latex" in seen
    assert "Assistant: $$E = mc^2$$" in seen
    # The new request itself is in the request and is not repeated (it is in the timeline too).
    assert after_request(second) == "even harder"
    assert "even harder" not in seen


async def test_answer_to_a_spoken_question_is_remembered_without_a_request_line():
    provider = FakeProvider("Global lock.")
    a = make_assistant(provider)
    a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM, text="Tell me about the GIL")
    )
    await a.ask_now()
    await a.ask_now("in more detail")

    seen = before_request(provider.prompts[1])
    assert "Them: Tell me about the GIL" in seen
    assert "Assistant: Global lock." in seen
    assert "Me → assistant" not in seen


async def test_failed_answer_is_not_remembered():
    class Broken(FakeProvider):
        async def ask(self, *args, **kwargs) -> Answer:
            return Answer(text="", provider=self.name, error="the network dropped")

    a = make_assistant(Broken())
    await a.ask_now("question")
    assert len(a.timeline) == 0


async def test_answer_of_a_finished_conversation_is_not_remembered():
    """Another chat was opened while the model was thinking — its answer does not go into the
    new feed.
    """

    class Switching(FakeProvider):
        async def ask(self, *args, **kwargs) -> Answer:
            answer = await super().ask(*args, **kwargs)
            a.epoch += 1
            return answer

    a = make_assistant(Switching("someone else's answer"))
    await a.ask_now("question")
    assert len(a.timeline) == 0


async def test_streamed_answer_is_remembered_once_complete():
    provider = FakeStreamingProvider(["Hel", "lo, ", "world"])
    a = make_assistant(provider)
    await a.ask_now("say hello")
    await a.ask_now("once more")

    seen = before_request(provider.prompts[1])
    assert "Me → assistant: say hello" in seen
    assert "Assistant: Hello, world" in seen


async def test_pasted_photo_is_mentioned_in_the_remembered_request():
    """The photo itself is not in the feed — but the next question must know it existed."""
    provider = FakeProvider("The photo shows a receipt.")
    a = make_assistant(provider)
    await a.ask_now("what is this", image=b"jpeg")
    await a.ask_now("and the total?")
    assert "Me → assistant: what is this (a photo was attached to the question)" in before_request(
        provider.prompts[1]
    )


def test_system_prompt_lets_a_long_answer_through_when_it_is_asked_for():
    """Live chat: "a complex, bulky formula" got `E = mc^2` in reply.

    The "be brief" rule stood without exceptions and outweighed a direct request.
    """
    for system in (default_system(True), default_system(False)):
        assert "The exception is when a long, detailed" in flat(system)
        assert "do not shorten it" in flat(system)
    assert "do not shorten it" in flat(compose(AssistSettings()))


def test_system_prompt_does_not_hand_an_invented_task_back():
    """"Come up with it yourself" is not a lack of data: the reply was asked to be
    a result, not a question.
    """
    for system in (default_system(True), default_system(False)):
        assert "asked to come up with something yourself" in flat(system)
        assert "do not hand the task back" in flat(system)


def test_system_prompt_explains_the_assistants_own_lines_in_the_timeline():
    for system in (default_system(True), default_system(False)):
        assert '"Me → assistant"' in flat(system)
        assert '"Assistant" is you, your past answers' in flat(system)
        assert "refer to your last answer" in flat(system)


async def test_answer_is_published_to_the_bus():
    provider = FakeProvider("model answer")
    a = make_assistant(provider)
    sub = a.bus.subscribe(Answer, maxsize=4)
    await a.ask_now("question")
    got = await asyncio.wait_for(sub.get(), 1)
    assert got.text == "model answer"
    assert got.meta["trigger"] == "hotkey"


async def test_failed_answer_is_published_with_its_error_but_not_remembered():
    """A model refusal must be visible to the person: the answer card can show `error` (as on
    the streaming path). Previously it was only written to the log — the "thinking"
    indicator disappeared without explanation. Such an answer does not go into the
    conversation feed for the model: an empty one there is only garbage."""

    class Broken(FakeProvider):
        async def ask(
            self, prompt: str, system: str | None = None,
            image: bytes | None = None, pinned: Sequence[bytes] = (),
            deep: bool = False,
        ) -> Answer:
            return Answer(text="", provider=self.name, error="the network dropped")

    provider = Broken()
    a = make_assistant(provider)
    sub = a.bus.subscribe(Answer, maxsize=4)
    answer = await a.ask_now("question")
    assert not answer.ok
    got = await asyncio.wait_for(sub.get(), 1)
    assert got is answer
    assert got.error == "the network dropped"
    assert got.done is True
    assert got.meta["trigger"] == "hotkey"             # the card needs all the metadata, as for a successful one
    assert a.answers == [answer]
    assert not [e for e in a.timeline.since(0) if e.kind is Kind.REPLY]


async def test_an_empty_answer_without_a_reason_gets_one():
    """An empty card without words is worse than an error: it has nothing to show."""
    a = make_assistant(FakeProvider(reply="   "))
    sub = a.bus.subscribe(Answer, maxsize=4)
    answer = await a.ask_now("question")
    got = await asyncio.wait_for(sub.get(), 1)
    assert got is answer and answer.error and not answer.ok


async def test_a_provider_that_raises_still_gives_an_error_answer():
    """The provider contract is an error as an answer; a provider that breaks it must not crash
    the caller.
    """

    class Raises(FakeProvider):
        async def ask(self, *args, **kwargs) -> Answer:
            raise RuntimeError("something went completely wrong")

    a = make_assistant(Raises())
    answer = await a.ask_now("question")
    assert not answer.ok and answer.error
    assert a._busy is False                            # the flag is not stuck


async def test_a_hung_provider_does_not_hold_the_busy_flag_forever(monkeypatch):
    class Hangs(FakeProvider):
        async def ask(self, *args, **kwargs) -> Answer:
            await asyncio.sleep(3600)

    monkeypatch.setattr("argus.brain.assistant.ASK_GUARD_S", 0.05)
    a = make_assistant(Hangs())
    answer = await a.ask_now("question")
    assert not answer.ok and answer.error
    assert a._busy is False


async def test_a_hung_stream_is_cut_by_the_same_guard(monkeypatch):
    class Hangs(FakeStreamingProvider):
        async def ask_stream(self, *args, **kwargs):
            yield "the start"
            await asyncio.sleep(3600)

    monkeypatch.setattr("argus.brain.assistant.ASK_GUARD_S", 0.05)
    a = make_assistant(Hangs())
    answer = await a.ask_now("question")
    assert answer.text == "the start" and answer.error and answer.done is True
    assert a._busy is False


async def test_streaming_provider_publishes_early_and_grows_in_place():
    """Streaming provider: the `Answer` goes to the bus at once, empty, rather than after the
    whole answer — voice playback (and anyone else) does not wait for a single token."""
    provider = FakeStreamingProvider(["Hel", "lo, ", "world"])
    a = make_assistant(provider)
    sub = a.bus.subscribe(Answer, maxsize=4)

    answer = await a.ask_now("question")

    seen = await asyncio.wait_for(sub.get(), 1)
    # The same object, not a copy and not a second message: there is exactly one publication,
    # made before the text even started to be assembled.
    assert seen is answer
    assert answer.text == "Hello, world"
    assert answer.done is True
    assert answer.model == "fake-stream"
    assert a.answers == [answer]


async def test_streaming_failure_is_recorded_not_raised():
    """The network dropped in the middle of the stream — an answer with an error, not an
    exception thrown outward.
    """

    class Boom(FakeStreamingProvider):
        async def ask_stream(self, *args, **kwargs):
            if False:  # makes the function a generator, not a plain coroutine
                yield ""
            raise RuntimeError("the stream was cut off")

    a = make_assistant(Boom())
    answer = await a.ask_now("question")

    assert not answer.ok
    assert "the stream was cut off" in (answer.error or "")
    assert answer.done is True


async def test_second_request_while_busy_is_refused():
    """An answer to the question from the day before yesterday is not needed, and it would spend
    the limit.
    """
    provider = FakeProvider()
    a = make_assistant(provider, cooldown_s=0)
    a._busy = True
    answer = await a.ask_now("question")
    assert not answer.ok
    assert "still answering" in answer.error


async def test_gate_refusal_reaches_the_caller():
    provider = FakeProvider()
    a = make_assistant(provider, max_calls=1)
    a.gate.accept(0, "prompt", manual=True)
    answer = await a.ask_now("question")
    assert not answer.ok
    assert "limit" in answer.error
    assert provider.prompts == []


async def test_asking_twice_through_ask_does_not_repeat_the_question():
    """Regression: `accept()` received the whole prompt, not the bare question.

    The comparison in `on_speech` was made against a timeline thousands of characters long
    and never matched itself, so deduplication silently never worked, even though the gate's
    unit test was green on its own.
    """
    provider = FakeProvider()
    a = make_assistant(provider, cooldown_s=0)

    entry = a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM,
                    text="Tell me about the GIL in Python")
    )
    first = a.gate.on_speech(entry, a.clock.now())
    assert first.wake
    await a._ask(first)

    second = a.gate.on_speech(entry, a.clock.now())
    assert not second.wake
    assert "already asked" in second.reason
    assert len(provider.prompts) == 1


async def test_mic_question_is_attributed_to_the_mic_not_the_interlocutor():
    """Before, cause/task rigidly took the last THEM utterance. With the reaction to the
    microphone switched on, that would be wrong: the question was asked from the microphone,
    not by the interlocutor, and substituting the other side's last utterance would mean
    answering the wrong question.
    """
    provider = FakeProvider()
    a = make_assistant(provider, cooldown_s=0)
    # mic_mode does not live in gate_brain: it is a channel setting
    # (channel.assist.answering.mic_mode), see assist/gate.py:install().
    # Here, below the channel level, we set GateSettings directly.
    a.gate.settings.mic_mode = ReplyMode.BY_FORM

    a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM, text="All right, let's continue.")
    )
    entry = a.timeline.add_speech(
        SpeechEvent(t=2 * NS, t_end=4 * NS, source=Source.ME,
                    text="What is the complexity of quicksort?")
    )
    decision = a.gate.on_speech(entry, a.clock.now())
    assert decision.wake
    assert decision.source is Source.ME

    answer = await a._ask(decision)
    assert answer.meta["cause"] == "What is the complexity of quicksort?"
    assert "What is the complexity of quicksort?" in provider.prompts[0]
    assert "All right, let's continue." not in provider.prompts[0].split("# Request")[-1]


# --------------------------------------------------------------------------
# Screen picture
# --------------------------------------------------------------------------


async def test_missing_screenshot_does_not_break_the_request():
    """The frame may be missing — the screen is off or the window was closed."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.send_screenshot = True
    a.screenshot = lambda: None
    answer = await a.ask_now("question")
    assert answer.ok
    assert provider.images == [None]


async def test_answer_reports_the_image_size():
    """The dashboard must show what the picture cost."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.send_screenshot = True
    a.screenshot = lambda: b"x" * 50_000
    answer = await a.ask_now("question")
    assert answer.meta["image_kb"] == 48


def test_system_prompt_mentions_the_picture_only_when_it_is_attached():
    """The screenshot is mentioned only when it is really attached.

    Before, there was one prompt for all cases and it always talked about the picture,
    with the caveat "a rare extra", a crutch for the fact that most of
    the time there is simply no picture. Now there are two variants: if a picture came, it
    says directly how to analyze it; if not, there is not a word about the screen as a
    source, and nothing to wait for.
    """
    with_image = default_system(True)
    without_image = default_system(False)

    assert "A screenshot is attached to this request" in with_image
    assert "Do not reduce the picture to the text on it" in with_image

    assert "screenshot" not in without_image
    assert "app or site" not in without_image


def test_system_prompt_separates_a_direct_request():
    """A direct request from the user must not drown in "answer the interlocutor".

    The prompt strictly told the model to answer the interlocutor's last question, and to a
    direct question "what is on the screen" the model answered "the interlocutor's question
    was not recognized", even though the picture was right in front of it. Now a request can
    be anything, and the prompt tells the model to do exactly what it says, even with no
    utterances in the timeline. It is checked on both variants, since this part does not
    depend on the picture.
    """
    for system in (default_system(True), default_system(False)):
        assert "nothing beyond it, even if" in system
        assert "the conversation feed is empty" in system


def test_model_looks_at_the_screen_by_default():
    """Recognized text alone is not enough.

    OCR extracts lines, but what is drawn, where it is placed and what is going on gets
    lost. A measurement on a live screen: without the picture the model answered "not enough
    data", with the picture it analyzed the content.
    """
    assert Config().brain.send_screenshot is True


async def test_screenshot_goes_with_the_request_by_default():
    provider = FakeProvider()
    a = make_assistant(provider)
    a.screenshot = lambda: b"jpeg-bytes"
    await a.ask_now("what is on the screen")
    assert provider.images == [b"jpeg-bytes"]


async def test_screen_waits_for_a_press_when_the_question_was_spoken():
    """A spoken question is not answered by the picture, yet it is paid for every time.

    "Tell me about the GIL": the screen has nothing to do with it, and it costs 1000-1500
    tokens and +220 ms. In `ask` mode the frame waits for a press: the person sees before
    we do when something worth looking at appears on the screen.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.screenshot_mode = "ask"
    a.screenshot = lambda: b"jpeg-bytes"

    entry = a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM,
                    text="Tell me about the GIL in Python")
    )
    decision = a.gate.on_speech(entry, a.clock.now())
    assert decision.wake
    await a._ask(decision)

    assert provider.images == [None]


async def test_hotkey_ask_never_sends_the_screen_in_ask_mode():
    """The "Ask" button does not mean "ask about the screen": by itself it does not
    pull a frame, even when pressed by hand. Before, what decided was not the request but
    the trigger (`Trigger.HOTKEY`), shared by both buttons, and an ordinary question to the
    interlocutor unexpectedly went out with a screen analysis. Now the explicit
    `force_image` decides (the next test), not the way the question came in.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.screenshot_mode = "ask"
    a.screenshot = lambda: b"jpeg-bytes"

    await a.ask_now("what is on the screen")
    assert provider.images == [None]


async def test_force_image_sends_the_screen_even_in_ask_mode():
    """`force_image=True` — what `app.ask_about_screen` uses to ask for a frame right now,
    regardless of `screenshot_mode`."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.screenshot_mode = "ask"
    a.screenshot = lambda: b"jpeg-bytes"

    await a.ask_now("what is on the screen", force_image=True)
    assert provider.images == [b"jpeg-bytes"]


async def test_screen_goes_with_a_spoken_question_while_tracking_is_on():
    """Screen tracking is on: a frame goes with every request.

    That includes the auto-reply to a heard question, without a press: there is no need to
    guess whether the model saw the screen this time. A mirror of
    `test_screen_waits_for_a_press_when_the_question_was_spoken`, where in `ask` mode the
    same question goes without a frame.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.send_screenshot = True
    a.screenshot = lambda: b"jpeg-bytes"

    entry = a.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM,
                    text="Tell me about the GIL in Python")
    )
    decision = a.gate.on_speech(entry, a.clock.now())
    assert decision.wake
    await a._ask(decision)

    assert provider.images == [b"jpeg-bytes"]


def test_project_config_sends_the_screen_with_every_request(tmp_path):
    """The project's `argus.toml` does not hide the frame behind a press.

    It used to have `screenshot_mode = "ask"`, and a single line in the file silently
    overrode the default from the code, and no test noticed, because they all build
    `Config()` without a file. We read a copy in a temporary folder: next to the real file
    lie `.env` with keys and `argus.local.json` with this machine's edits, and the test must
    neither load them nor depend on them.
    """
    shutil.copy(Path(__file__).resolve().parents[1] / "argus.toml", tmp_path / "argus.toml")
    cfg = Config.load(tmp_path / "argus.toml")

    assert cfg.brain.send_screenshot is True
    assert cfg.brain.screenshot_mode == "always"


async def test_screenshot_can_still_be_switched_off():
    """The "Screen" checkbox turns off both the picture and recognition at once."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.send_screenshot = False
    a.screenshot = lambda: b"jpeg-bytes"
    await a.ask_now("question")
    assert provider.images == [None]


# --------------------------------------------------------------------------
# Cache warm-up
# --------------------------------------------------------------------------


async def test_warming_writes_the_cache_before_the_first_question():
    """Otherwise the first question pays for the cache write, with a second that the user can see.

    There are two variants, with a picture and without, and both are warmed up: the
    "Ask" button pressed by hand almost always comes with a frame, and the first
    such question of a session must not become a visible second.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.system_prompt = "the whole lecture"
    a.cfg.brain.system_prompt_with_image = "lecture with a frame"
    await a.warm()
    assert provider.warmed == [("the whole lecture", []), ("lecture with a frame", [])]


async def test_the_same_prompt_is_not_warmed_twice():
    """A write costs money. A second one for the same prefix gives nothing."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.system_prompt = "the whole lecture"
    a.cfg.brain.system_prompt_with_image = "lecture with a frame"
    await a.warm()
    await a.warm()
    assert len(provider.warmed) == 2


async def test_edited_prompt_is_warmed_again():
    """A settings edit is a new prefix, and the cache for it has not been written yet."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.system_prompt = "first lecture"
    a.cfg.brain.system_prompt_with_image = "first lecture with a frame"
    await a.warm()
    a.cfg.brain.system_prompt = "second lecture"
    a.cfg.brain.system_prompt_with_image = "second lecture with a frame"
    await a.warm()
    assert [text for text, _ in provider.warmed] == [
        "first lecture", "first lecture with a frame",
        "second lecture", "second lecture with a frame",
    ]


async def test_warming_in_the_background_does_not_block_the_caller():
    """Settings are saved instantly, while warming goes on at its own pace."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.system_prompt = "the whole lecture"
    a.cfg.brain.system_prompt_with_image = "lecture with a frame"
    a.warm_soon()
    assert provider.warmed == []      # has not made it yet, and was not supposed to
    await asyncio.sleep(0)
    assert provider.warmed == [("the whole lecture", []), ("lecture with a frame", [])]


async def test_pinned_pages_go_before_the_screen():
    """The lecture pages do not change, the screen frame does, and the order reflects that.

    The provider puts the cache point after the pinned pages; if the frame came first, it
    would reset the cache of the whole batch on every question.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.pinned = ["page-1".encode(), "page-2".encode()]
    await a.ask_now("question")
    assert provider.pinned == [["page-1".encode(), "page-2".encode()]]


async def test_warming_covers_the_pinned_pages():
    """These are what costs the most: a page as an image is fifteen hundred tokens."""
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.system_prompt = "lecture"
    a.cfg.brain.system_prompt_with_image = "lecture with a frame"
    a.pinned = ["page".encode()]
    await a.warm()
    assert provider.warmed == [
        ("lecture", ["page".encode()]),
        ("lecture with a frame", ["page".encode()]),
    ]


async def test_new_pages_are_warmed_again():
    """More pages were added — the prefix is different, and the cache for it has not been
    written yet.
    """
    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.system_prompt = "lecture"
    a.cfg.brain.system_prompt_with_image = "lecture with a frame"
    a.pinned = ["page".encode()]
    await a.warm()
    a.pinned = ["page".encode(), "one more".encode()]
    await a.warm()
    assert len(provider.warmed) == 4
