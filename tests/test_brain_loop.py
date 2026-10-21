"""The assistant loop does not die from a single error, and the filter does not waste money.

While the assistant exists, only it puts utterances into the timeline
(`Argus._pump_speech`). A dead `run()` froze the timeline: spoken questions did not reach
the model, and the hotkey answered from a stale context, with no line in the log or on
the page.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from argus.app import Argus
from argus.brain.assistant import Assistant
from argus.brain.base import Answer
from argus.brain.gate import Gate, GateSettings, ReplyMode, Trigger, looks_like_question
from argus.context.timeline import Entry, Kind, Timeline
from argus.core.bus import Bus
from argus.core.clock import ReplayClock
from argus.core.config import Config
from argus.core.events import Source, SpeechEvent

NS = 1_000_000_000


class FakeProvider:
    name = "fake"

    def __init__(self, reply: str = "answer", fail_first: bool = False) -> None:
        self.reply = reply
        self.fail_first = fail_first
        self.prompts: list[str] = []

    async def start(self) -> None:
        pass

    async def warm(self, system=None, pinned: Sequence[bytes] = ()) -> None:
        pass

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False) -> Answer:
        self.prompts.append(prompt)
        return Answer(text=self.reply, provider=self.name, latency_ms=1.0)

    async def close(self) -> None:
        pass


def make_assistant(provider, **gate) -> Assistant:
    cfg = Config()
    for key, value in gate.items():
        setattr(cfg.gate_brain, key, value)
    clock = ReplayClock.__new__(ReplayClock)
    clock._t = 0
    clock._t0_wall = 0
    return Assistant(Bus(asyncio.get_event_loop()), cfg, clock, Timeline(), provider)


def spoken(text: str, t_s: float = 1.0, source: Source = Source.THEM) -> SpeechEvent:
    return SpeechEvent(t=int((t_s - 0.5) * NS), t_end=int(t_s * NS), source=source, text=text)


async def _feed(items):
    for item in items:
        yield item


# --------------------------------------------------------------------------
# One error does not end the loop
# --------------------------------------------------------------------------


async def test_one_broken_phrase_does_not_end_the_speech_loop(caplog):
    provider = FakeProvider()
    a = make_assistant(provider, cooldown_s=0)
    real = a.gate.on_speech
    calls = {"n": 0}

    def flaky(entry, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the gate broke on the first line")
        return real(entry, now)

    a.gate.on_speech = flaky
    events = [spoken("Tell me about databases", 1), spoken("What is an index in a database", 2)]

    await a._watch_speech(_feed(events))

    assert calls["n"] == 2                                  # the second utterance reached the gate
    assert len(provider.prompts) == 1                       # and called the model
    assert "assistant could not parse the utterance" in caplog.text    # while the first left a line in the log


async def test_a_failure_while_asking_does_not_end_the_speech_loop(monkeypatch):
    provider = FakeProvider()
    a = make_assistant(provider, cooldown_s=0)
    real_ask = a._ask
    seen = []

    async def flaky_ask(decision, *args, **kwargs):
        seen.append(decision)
        if len(seen) == 1:
            raise ValueError("failure in the middle of the request")
        return await real_ask(decision, *args, **kwargs)

    monkeypatch.setattr(a, "_ask", flaky_ask)
    events = [spoken("Tell me about databases", 1), spoken("What is an index in a database", 2)]

    await a._watch_speech(_feed(events))

    assert len(seen) == 2 and len(provider.prompts) == 1


async def test_one_broken_frame_does_not_end_the_screen_loop(monkeypatch, caplog):
    a = make_assistant(FakeProvider())
    seen = []

    async def flaky(event):
        seen.append(event)
        if len(seen) == 1:
            raise RuntimeError("the frame could not be parsed")

    monkeypatch.setattr(a, "_on_screen", flaky)
    await a._watch_screen(_feed(["one", "two"]))

    assert seen == ["one", "two"]
    assert "assistant could not parse the screen frame" in caplog.text


async def test_the_running_loop_survives_a_broken_phrase():
    """The same through the real bus and the real `run()`."""
    provider = FakeProvider()
    a = make_assistant(provider, cooldown_s=0)
    real = a.gate.on_speech
    calls = {"n": 0}

    def flaky(entry, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("broke")
        return real(entry, now)

    a.gate.on_speech = flaky
    task = asyncio.create_task(a.run())
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        a.bus.publish(spoken("Tell me about databases", 1))
        a.bus.publish(spoken("What is an index in a database", 2))
        for _ in range(100):
            if provider.prompts:
                break
            await asyncio.sleep(0.01)
        assert len(provider.prompts) == 1
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------
# A model failure is visible on the page
# --------------------------------------------------------------------------


@pytest.fixture
def live_app() -> Argus:
    return Argus(Config(), with_screen=False, with_brain=False)


async def test_a_failed_ask_reaches_the_page_as_a_card_with_its_error(live_app):
    """The state snapshot is what the page draws: a card with `error`, and the "thinking"
    indicator goes out.
    """
    from argus.ui.server import snapshot

    class Offline(FakeProvider):
        async def ask(self, *args, **kwargs) -> Answer:
            return Answer(text="", provider=self.name, error="no connection")

    app = live_app
    app.assistant = Assistant(app.bus, app.cfg, app.clock, app.timeline, provider=Offline())
    pump = asyncio.create_task(app._pump_answers(app.bus.subscribe(Answer, maxsize=16)))
    try:
        await app.assistant.ask_now("question")
        for _ in range(100):
            if snapshot(app)["answers"]:
                break
            await asyncio.sleep(0.01)
        card = snapshot(app)["answers"][-1]
        assert card["error"] == "no connection" and card["text"] == ""
        assert card["cause"] == "question"                      # "ask again" knows what it is about
        assert snapshot(app)["thinking"] is None
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


# --------------------------------------------------------------------------
# Supervision of the loop in the application
# --------------------------------------------------------------------------


class _Dying:
    """A helper whose `run()` dies: the first `deaths` times with an error, then it lives."""

    def __init__(self, deaths: int) -> None:
        self.deaths = deaths
        self.runs = 0

    async def run(self) -> None:
        self.runs += 1
        if self.runs <= self.deaths:
            raise RuntimeError("the loop died")
        await asyncio.sleep(3600)


def _app_with(assistant) -> Argus:
    app = Argus.__new__(Argus)          # `__init__` pulls in audio and screen; only the supervision is needed
    app._tasks = []
    app._assistant_deaths = []
    app.assistant = assistant
    app.ASSISTANT_RESTART_DELAY_S = 0.01
    return app


async def _until(predicate, seconds: float = 2.0) -> bool:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_a_dead_assistant_loop_is_logged_and_started_again(caplog):
    dying = _Dying(deaths=1)
    app = _app_with(dying)
    app._start_assistant()

    assert await _until(lambda: dying.runs == 2)
    assert "assistant loop died" in caplog.text
    for task in app._tasks:
        task.cancel()
    await asyncio.gather(*app._tasks, return_exceptions=True)


async def test_stopping_the_app_is_not_a_death():
    dying = _Dying(deaths=0)
    app = _app_with(dying)
    app._start_assistant()
    await asyncio.sleep(0.05)
    for task in app._tasks:
        task.cancel()
    await asyncio.gather(*app._tasks, return_exceptions=True)
    await asyncio.sleep(0.1)
    assert dying.runs == 1


async def test_a_loop_that_dies_again_and_again_is_not_restarted_forever():
    dying = _Dying(deaths=10**6)
    app = _app_with(dying)
    app._start_assistant()

    await _until(lambda: dying.runs > Argus.ASSISTANT_DEATHS_PER_MIN, seconds=3.0)
    await asyncio.sleep(0.2)
    assert dying.runs == Argus.ASSISTANT_DEATHS_PER_MIN + 1


async def test_nothing_is_started_while_the_app_is_stopping():
    """`stop()` cancels the tasks and only then clears the list: a restart scheduled before the
    stop must not slip into this window and leave the loop alive after exit."""
    dying = _Dying(deaths=1)
    app = _app_with(dying)
    other = asyncio.create_task(asyncio.sleep(3600))         # a neighboring task of the application
    app._tasks.append(other)
    app._start_assistant()
    await asyncio.sleep(0)                # the assistant loop died, the restart is scheduled in 10 ms

    other.cancel()                        # `stop()` has already started cancelling
    await asyncio.gather(other, return_exceptions=True)
    await asyncio.sleep(0.2)

    assert dying.runs == 1


async def test_nothing_is_started_after_the_app_stopped():
    dying = _Dying(deaths=1)
    app = _app_with(dying)
    app._start_assistant()
    await asyncio.sleep(0)               # the first run died, the restart waits its turn
    app._tasks.clear()                   # this is how `stop()` leaves the list
    await asyncio.sleep(0.2)
    assert dying.runs == 1


# --------------------------------------------------------------------------
# Filter: a command only at the start of a sentence
# --------------------------------------------------------------------------


# The Russian samples below are deliberate: the gate's task words and "let's" forms are Russian
# imperatives (language data), and the comments give the meaning.
@pytest.mark.parametrize("text", [
    "Мы решили запустить проект в марте.",              # We decided to launch the project in March.
    "Вчера мы найдём время на обед",                    # Yesterday we will find time for lunch
    "We implemented the feature last week",
    "The writer finished the book yesterday",
    "It was solved by the team in June",
    "Я напишу вам завтра после обеда",                  # I will write to you tomorrow after lunch
    "Они вычислили координаты ещё вчера",               # They calculated the coordinates yesterday
    "Он сравнил результаты двух тестов",                # He compared the results of two tests
    "Мы приведём всё в порядок к пятнице",              # We will tidy everything up by Friday
])
def test_an_ordinary_statement_does_not_wake_the_paid_model(text):
    assert not looks_like_question(text)


@pytest.mark.parametrize("text", [
    "Напишите функцию, которая разворачивает список",   # Write a function that reverses a list
    "Реализуйте бинарный поиск",                        # Implement a binary search
    "Реши задачу про два поезда",                       # Solve the problem about two trains
    "Найди ошибку в этом коде",                         # Find the error in this code
    "А теперь посчитай сложность",                      # And now calculate the complexity
    "Пожалуйста, оптимизируйте этот запрос",            # Please optimize this query
    "Понятно, напиши ещё тест",                         # I see, write one more test
    "Задача такая: сформулируйте определение предела",  # The task is: formulate the definition of a limit
    "Write a function that reverses a list",
    "Okay, implement a binary search",
    "Solve this equation for x",
    "Calculate the integral of x squared",
])
def test_an_order_at_the_start_of_a_sentence_still_wakes_the_model(text):
    assert looks_like_question(text)


@pytest.mark.parametrize("text", [
    "Давайте решим задачу про два поезда",              # Let's solve the problem about two trains
    "А теперь давай напишем функцию сортировки",        # And now let's write a sorting function
    "Ну давайте посчитаем сложность вместе",            # Well, let's calculate the complexity together
])
def test_lets_do_it_together_is_a_request(text):
    assert looks_like_question(text)


@pytest.mark.parametrize("text", [
    "Мы решим задачу про два поезда завтра",            # We will solve the problem about two trains tomorrow
    "Решим это завтра после обеда",                     # We will sort this out tomorrow after lunch
    "Найдём время на обед в четверг",                   # We will find time for lunch on Thursday
    "Давайте продолжим в понедельник",                  # Let's continue on Monday
])
def test_the_future_tense_without_lets_is_a_statement(text):
    assert not looks_like_question(text)


def test_an_order_in_the_second_sentence_is_found():
    assert looks_like_question("Hello, this is a connection check. Write a sorting function")


# --------------------------------------------------------------------------
# Request cap: automation does not take a person's button away
# --------------------------------------------------------------------------

def _entry(text: str) -> Entry:
    return Entry(t=NS, kind=Kind.SPEECH, source=Source.THEM, text=text)


def test_automatic_triggers_cannot_use_up_the_hotkey():
    gate = Gate(GateSettings(max_calls=3, cooldown_s=0))
    for i in range(3):
        assert gate.on_speech(_entry("Tell me about databases"), now=(i + 1) * 100 * NS).wake
        gate.accept((i + 1) * 100 * NS, f"question {i}")                       # automatically
    assert not gate.on_speech(_entry("Tell me about databases"), now=999 * NS).wake
    d = gate.on_hotkey(now=1000 * NS)
    assert d.wake and d.trigger is Trigger.HOTKEY


def test_the_hotkey_has_its_own_ceiling():
    gate = Gate(GateSettings(max_calls=2, cooldown_s=0))
    for i in range(2):
        assert gate.on_hotkey(now=i * NS).wake
        gate.accept(i * NS, "question", manual=True)
    d = gate.on_hotkey(now=10 * NS)
    assert not d.wake and "limit" in d.reason
    assert gate.stats()["requests"] == 2


async def test_the_assistant_counts_a_hotkey_ask_as_manual():
    a = make_assistant(FakeProvider(), cooldown_s=0)
    await a.ask_now("question")
    assert a.gate.calls == 1 and a.gate.manual_calls == 1
    # The automatic path (a spoken question) is not marked as manual.
    await a._watch_speech(_feed([spoken("Tell me about databases", 5)]))
    assert a.gate.calls == 2 and a.gate.manual_calls == 1


# --------------------------------------------------------------------------
# The semantic check costs money too
# --------------------------------------------------------------------------


class _Judge(FakeProvider):
    """Answers "no" to a yes/no check and counts the calls."""

    def __init__(self) -> None:
        super().__init__(reply="no")


def _semantic(provider, **gate) -> Assistant:
    a = make_assistant(provider, **gate)
    a.gate.settings.system_mode = ReplyMode.SEMANTIC
    return a


async def test_semantic_checks_have_their_own_pause():
    provider = _Judge()
    a = _semantic(provider, cooldown_s=0)
    events = [spoken(f"Tell me about database number {i}", 1) for i in range(5)]
    await a._watch_speech(_feed(events))
    # The clock stands at zero: the pause between checks has not passed even once, except the
    # first.
    assert len(provider.prompts) == 1


async def test_semantic_checks_have_their_own_ceiling():
    provider = _Judge()
    a = _semantic(provider, cooldown_s=0, max_calls=3)
    a.gate.last_check_ns = -10**18
    for i in range(6):
        a.clock._t = (i + 1) * 100 * NS          # the clock runs: the pause passes every time
        await a._watch_speech(_feed([spoken(f"Tell me about database number {i}", i + 1)]))
    assert len(provider.prompts) == 3
    assert a.gate.checks == 3
    assert a.gate.calls == 0                     # a "no" does not count as a request


async def test_no_check_is_paid_while_the_model_is_busy():
    provider = _Judge()
    a = _semantic(provider, cooldown_s=0)
    a._busy = True
    await a._watch_speech(_feed([spoken("Tell me about a database", 1)]))
    assert provider.prompts == []
    assert a.gate.checks == 0


async def test_a_yes_still_wakes_the_model():
    provider = FakeProvider(reply="yes")
    a = _semantic(provider, cooldown_s=0)
    await a._watch_speech(_feed([spoken("Tell me about a database", 1)]))
    assert len(provider.prompts) == 2             # the check and the answer itself
    assert a.gate.calls == 1 and a.gate.checks == 1


# --------------------------------------------------------------------------
# Another chat was opened in the middle of a stream: the old answer is not completed
# --------------------------------------------------------------------------


async def test_a_stream_stops_when_another_chat_is_opened():
    pulled = []

    class Endless(FakeProvider):
        async def ask_stream(self, prompt, system=None, image=None, pinned=(), deep=False,
                             *, info=None):
            for i in range(1000):
                pulled.append(i)
                yield f"piece{i} "
                await asyncio.sleep(0.005)

    a = make_assistant(Endless())
    task = asyncio.create_task(a.ask_now("question"))
    await asyncio.sleep(0.05)
    await a.reset()
    answer = await asyncio.wait_for(task, 2)

    assert answer.done is True
    assert len(pulled) < 100                       # the stream is closed, not drained to the end
    assert a._busy is False
