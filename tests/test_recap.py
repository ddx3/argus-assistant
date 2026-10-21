"""The conversation recap: what it is assembled from, how it is split and what is asked of
the model.

The errors here are silent. A recap that has lost half of the conversation reads like a
real one; a request longer than the relay's ceiling goes out and comes back as a 422
only at the person's end; the phrase "watch the price", parsed as a
question, gets a polite answer about nothing. So what is checked is not "the function
returned a string" but the boundaries: how long the longest request is, what is dropped
on overflow, which phrases count as a command and which do not.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from argus.brain.base import Answer
from argus.core.events import Source
from argus.recap import chat, commands, engine, prompts
from argus.recap.lines import PAUSE_S, Line, chunks, from_history, render
from argus.recap.topics import MAX_TOPICS, Topics, split

ME, THEM = Source.ME, Source.THEM
BASE = datetime(2026, 10, 6, 14, 0, tzinfo=UTC).timestamp()

# The relay's `prompt` ceiling (`server/relay.py: MAX_PROMPT_CHARS`). A literal here, not an
# import: the test guards exactly what the server sees, and if the ceiling is changed, this
# number should be changed deliberately too.
RELAY_LIMIT = 20_000


def at(minutes: float, who: Source, text: str) -> Line:
    return Line(BASE + minutes * 60, who, text)


def talk(count: int, *, seed: int = 1, width: int = 90) -> list[Line]:
    """A long conversation: `count` utterances, sometimes with pauses. Each one is unique."""
    rng = random.Random(seed)
    out, minute = [], 0.0
    for i in range(count):
        minute += rng.choice([0.2, 0.3, 0.5, 1.0, 14.0 if i % 97 == 96 else 0.4])
        filler = " ".join(rng.choice(["terms", "budget", "launch", "test", "contract"]) for _ in range(width // 8))
        out.append(at(minute, ME if i % 3 == 0 else THEM, f"remark #{i}: {filler}"))
    return out


# --------------------------------------------------------------------------
# Utterances
# --------------------------------------------------------------------------


def test_history_keeps_only_spoken_lines():
    records = [
        {"k": "s", "ts": BASE, "src": "me", "text": "  hello,   how are you  "},
        {"k": "a", "ts": BASE + 1, "text": "bot answer"},                   # an answer is not an utterance
        {"k": "s", "ts": BASE + 2, "src": "them", "text": "   "},           # empty
        {"k": "s", "ts": BASE + 3, "src": "screen", "text": "button"},      # the screen is not speech
        {"k": "s", "ts": "yesterday", "src": "me", "text": "broken time"},
        {"k": "s", "ts": True, "src": "me", "text": "time is a boolean"},
        {"k": "s", "ts": BASE + 4, "src": "robot", "text": "foreign source"},
        {"k": "s", "ts": BASE + 5, "src": "them", "text": 42},
        "not a dict",
        {"k": "s", "ts": BASE + 6, "src": "them", "text": "fine"},
    ]

    got = from_history(records)

    assert [(line.source, line.text) for line in got] == [
        (ME, "hello, how are you"), (THEM, "fine"),
    ]


def test_render_marks_pauses_and_days():
    lines = [
        at(0, ME, "one"), at(1, THEM, "two"), at(31, ME, "three"), at(24 * 60 + 1, THEM, "four"),
    ]

    assert render(lines, UTC).splitlines() == [
        "— 2026-10-06 —",
        "[14:00] Me: one",
        "[14:01] Them: two",
        "— pause 30 min —",
        "[14:31] Me: three",
        "— pause 1410 min —",
        "— 2026-10-07 —",
        "[14:01] Them: four",
    ]


def test_a_short_gap_is_not_a_pause():
    gap = (PAUSE_S - 1) / 60
    assert "pause" not in render([at(0, ME, "one"), at(gap, THEM, "two")], UTC)


def test_chunks_fit_the_budget_and_keep_every_line_in_order():
    lines = talk(400)
    budget = 3000

    parts = chunks(lines, budget, UTC)

    assert len(parts) > 5
    assert [line for part in parts for line in part] == lines
    assert all(len(render(part, UTC)) <= budget for part in parts)


def test_chunks_count_the_day_and_pause_separators():
    """Separators take up space too: a part packed tight against the budget would otherwise
    overflow it at every pause."""
    lines = [at(i * 15, ME if i % 2 else THEM, "words " * 20) for i in range(60)]   # a pause on each one

    parts = chunks(lines, 1500, UTC)

    assert all(len(render(part, UTC)) <= 1500 for part in parts)


def test_a_line_longer_than_the_budget_is_cut_instead_of_breaking_the_request():
    parts = chunks([at(0, ME, "x" * 5000)], 300, UTC)

    assert len(parts) == 1
    assert parts[0][0].text.endswith("…")
    assert len(render(parts[0], UTC)) <= 300


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


# The recap commands are parsed in Russian and in English: the Russian phrases below (and the
# Russian topics they carry as `arg`) are language data, so they stay in Russian.
@pytest.mark.parametrize(
    ("text", "kind", "arg"),
    [
        ("итог", commands.RECAP, ""),
        ("Итог.", commands.RECAP, ""),
        ("итоги", commands.RECAP, ""),
        ("итог?", commands.RECAP, ""),
        ("итог сессии", commands.RECAP, ""),
        ("подведи итог", commands.RECAP, ""),
        ("Подведи итоги разговора", commands.RECAP, ""),
        ("пожалуйста, подведи итог", commands.RECAP, ""),
        ("перескажи разговор", commands.RECAP, ""),
        ("пересказ", commands.RECAP, ""),
        ("саммари", commands.RECAP, ""),
        ("/итог", commands.RECAP, ""),
        ("/recap", commands.RECAP, ""),
        ("recap", commands.RECAP, ""),
        ("summarize the call", commands.RECAP, ""),
        ("итог по срокам", commands.RECAP, "срокам"),
        ("итог по срокам и бюджету", commands.RECAP, "срокам и бюджету"),
        ("подведи итог по теме «бюджет»", commands.RECAP, "бюджет"),
        ("перескажи, что говорили про цены", commands.RECAP, "цены"),
        ("что решили про сроки?", commands.RECAP, "сроки"),
        ("summary of the call about pricing", commands.RECAP, "pricing"),
        ("recap for the meeting on pricing", commands.RECAP, "pricing"),
        ("следи за сроками", commands.WATCH, "сроками"),
        ("Слушай и следи за ценой.", commands.WATCH, "ценой"),
        ("слушай, следи за ценой", commands.WATCH, "ценой"),
        ("отслеживай риски", commands.WATCH, "риски"),
        ("следи за темой «риски»", commands.WATCH, "риски"),
        ("watch for deadlines", commands.WATCH, "deadlines"),
        ("не следи за ценой", commands.UNWATCH, "ценой"),
        ("хватит следить за сроками", commands.UNWATCH, "сроками"),
        ("перестань следить", commands.UNWATCH, ""),
        ("забудь все темы", commands.UNWATCH, ""),
        ("stop watching", commands.UNWATCH, ""),
        ("за чем следишь?", commands.TOPICS, ""),
        ("темы", commands.TOPICS, ""),
    ],
)
def test_commands_are_recognised(text, kind, arg):
    assert commands.parse(text) == commands.Command(kind, arg)


# Russian ordinary questions next to the English ones: language data, kept in Russian.
@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "какой итог у матча вчера?",
        "итоговая оценка",
        "расскажи про итог квартала",
        "что такое саммари",
        # Ordinary questions about a subject, not a conversation recap (chats-recap-10).
        "Summary of the French Revolution",
        "Summarize on blockchain",
        "Перескажи про Наполеона",
        "Перескажи про цены",
        "Watch the video again",
        "Track the price of bitcoin",
        "track my package",
        "итог по",                       # no topic named
        "следи",
        "следи за собой?",               # a question, not a request
        "track my package?",
        "watch out for the car",
        "Что такое GIL и зачем он нужен?",
        "итог?  а что, уже всё?",        # a question mark in the middle of the phrase
        "итог " + "очень " * 100,        # a long thought spoken aloud
    ],
)
def test_ordinary_questions_are_not_swallowed(text):
    assert commands.parse(text) is None


# --------------------------------------------------------------------------
# Topics
# --------------------------------------------------------------------------


class FakeStore:
    """What the recap needs from the chat storage: the chat folder and its journal."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def folder(self, chat_id: str) -> Path:
        return self.root / chat_id

    def history(self, chat_id: str) -> list[dict]:
        path = self.folder(chat_id) / "history.jsonl"
        if not path.is_file():
            return []
        return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines() if row]


@pytest.fixture
def open_chat(tmp_path, monkeypatch):
    """Open the chat `abc` in a temporary folder; return its folder."""
    folder = tmp_path / "abc"
    folder.mkdir()
    monkeypatch.setattr(chat, "active", lambda app: ("abc", FakeStore(tmp_path)))
    return folder


def test_topics_split_on_commas_not_on_and():
    assert split("deadlines, «budget»; cats and dogs\nDeadlines.") == ["deadlines", "budget", "cats and dogs"]


def test_topics_live_in_memory_when_no_chat_is_open(monkeypatch):
    monkeypatch.setattr(chat, "active", lambda app: None)
    topics = Topics(None)

    change = topics.add("deadlines, budget")

    assert change.changed == ["deadlines", "budget"]
    assert topics.get() == ["deadlines", "budget"]


def test_topics_are_saved_in_the_chat_folder_and_survive_a_restart(open_chat):
    Topics(None).add("deadlines, budget")

    assert json.loads((open_chat / "recap.json").read_text(encoding="utf-8")) == {
        "topics": ["deadlines", "budget"],
    }
    assert Topics(None).get() == ["deadlines", "budget"]      # a new object, as after a restart


def test_every_chat_has_its_own_topics(tmp_path, monkeypatch):
    (tmp_path / "abc").mkdir()
    (tmp_path / "def").mkdir()
    current = {"id": "abc"}
    monkeypatch.setattr(chat, "active", lambda app: (current["id"], FakeStore(tmp_path)))
    topics = Topics(None)

    topics.add("deadlines")
    current["id"] = "def"
    assert topics.get() == []
    topics.add("budget")
    current["id"] = "abc"

    assert topics.get() == ["deadlines"]


def test_a_topic_is_added_once_whatever_the_case(open_chat):
    topics = Topics(None)
    topics.add("Deadlines")

    change = topics.add("deadlines.")

    assert change.changed == []
    assert topics.get() == ["Deadlines"]


def test_topics_are_capped(open_chat):
    topics = Topics(None)

    change = topics.add(", ".join(f"topic {i}" for i in range(MAX_TOPICS + 2)))

    assert len(topics.get()) == MAX_TOPICS
    assert change.skipped == [f"topic {MAX_TOPICS}", f"topic {MAX_TOPICS + 1}"]


def test_removing_a_topic_forgives_the_case_ending(open_chat):
    # Russian inflection is the subject here ("цена" nominative, "цене" dative), so these words stay Russian.
    topics = Topics(None)
    topics.add("цена, сроки")

    change = topics.remove("цене")

    assert change.changed == ["цена"]
    assert topics.get() == ["сроки"]


def test_removing_an_unknown_topic_changes_nothing(open_chat):
    topics = Topics(None)
    topics.add("deadlines")

    change = topics.remove("weather")

    assert change.changed == [] and change.skipped == ["weather"]
    assert topics.get() == ["deadlines"]


def test_a_two_letter_word_does_not_remove_a_topic_by_accident(open_chat):
    topics = Topics(None)
    topics.add("release")

    assert topics.remove("re").changed == []


def test_a_word_does_not_remove_a_longer_topic_that_only_contains_it(open_chat):
    """"Stop watching deadlines" must not silently remove "deadlines and budget": the person
    asked to remove one thing, and two would vanish. It is
    better to say "no such topic": the answer shows how to name the one that exists."""
    topics = Topics(None)
    topics.add("deadlines and budget")

    change = topics.remove("deadlines")

    assert change.changed == [] and change.skipped == ["deadlines"]
    assert topics.get() == ["deadlines and budget"]


def test_clear_forgets_everything(open_chat):
    topics = Topics(None)
    topics.add("deadlines, budget")

    change = topics.clear()

    assert change.changed == ["deadlines", "budget"]
    assert topics.get() == []


def test_a_deleted_chat_is_not_brought_back_by_a_topic(open_chat):
    topics = Topics(None)
    open_chat.rmdir()

    topics.add("deadlines")

    assert not open_chat.exists()


def test_a_broken_topics_file_means_no_topics(open_chat):
    (open_chat / "recap.json").write_text("{not json", encoding="utf-8")

    assert Topics(None).get() == []


# --------------------------------------------------------------------------
# Requests to the model
# --------------------------------------------------------------------------


class Fake:
    """A provider that records the requests and answers according to the kind of request."""

    name = "fake"

    def __init__(self, *, fail_on: int = 0, delay: float = 0.0, notes_chars: int = 0) -> None:
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.fail_on = fail_on
        self.delay = delay
        self.notes_chars = notes_chars      # length of the notes if they "do not obey" the limit
        self.in_flight = 0
        self.peak = 0

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False):
        self.prompts.append(prompt)
        self.systems.append(system)
        number = len(self.prompts)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        if number == self.fail_on:
            return Answer(text="", provider=self.name, error="request limit")
        if "This is part" in prompt:
            text = f"- notes for request {number}"
        elif "Merge them into a single set of notes" in prompt:
            text = f"- merged notes for request {number}"
        else:
            text = "SUMMARY"
        if self.notes_chars and text != "SUMMARY":
            text = (text + " " + "note " * self.notes_chars)[: self.notes_chars]
        return Answer(text=text, provider=self.name, model="m1")


async def test_a_short_conversation_goes_in_one_request():
    provider = Fake()
    lines = [at(i, ME if i % 2 else THEM, f"line number {i} about the release dates") for i in range(6)]

    out = await engine.summarize(provider, lines)

    assert out.ok and out.text == "SUMMARY"
    assert (out.parts, out.calls, out.dropped) == (1, 1, 0)
    assert len(provider.prompts) == 1
    assert "line number 3 about the release dates" in provider.prompts[0]
    # The rules are in the request itself, not only in `system` (see `prompts.py`).
    assert "How to read the transcript" in provider.prompts[0]
    assert provider.systems == [prompts.ROLE]
    assert (out.provider, out.model) == ("fake", "m1")


async def test_too_little_said_means_no_request_at_all():
    provider = Fake()

    out = await engine.summarize(provider, [at(0, ME, "hello")])

    assert out.tiny and out.text == engine.TOO_LITTLE and out.calls == 0
    assert provider.prompts == []


async def test_a_long_conversation_never_exceeds_the_relay_limit():
    provider = Fake()
    lines = talk(1500)             # ~130 thousand characters, like two hours of conversation

    out = await engine.summarize(
        provider, lines, topics=["deadlines", "budget"], context="release call " * 80,
    )

    assert out.ok and out.text == "SUMMARY"
    assert out.parts > 1
    assert max(len(p) for p in provider.prompts) <= RELAY_LIMIT
    # Parts + the merge (if there was one) + the final request, and not a single utterance is
    # lost.
    assert out.calls == len(provider.prompts) >= out.parts + 1
    assert out.dropped == 0
    joined = "\n".join(provider.prompts)
    assert "remark #0:" in joined and "remark #1499:" in joined


async def test_the_longest_topics_and_context_still_leave_every_request_under_the_limit():
    provider = Fake()
    topics = [f"{i}-" + "term " * 24 for i in range(MAX_TOPICS)]     # about 120 characters each, all ten

    await engine.summarize(provider, talk(1500), topics=split(topics), context="context " * 400)

    assert max(len(p) for p in provider.prompts) <= RELAY_LIMIT


async def test_notes_that_do_not_fit_together_are_merged_in_rounds_before_the_summary():
    """The notes of the parts are ordered to be kept within fifteen hundred characters, but the
    model is free to write more. Twelve notes of 2500 each do not fit together into the final
    request, so they are first merged in groups."""
    provider = Fake(notes_chars=2500)

    out = await engine.summarize(provider, talk(4500))     # ~10 parts

    assert out.ok and out.text == "SUMMARY"
    merges = [p for p in provider.prompts if "Merge them into a single set of notes" in p]
    assert merges, "the notes did not fit together, and there was no merge"
    assert max(len(p) for p in provider.prompts) <= RELAY_LIMIT
    assert "Write a summary of the conversation from these notes" in provider.prompts[-1]
    # The final request got the already merged notes, not twelve raw ones.
    assert provider.prompts[-1].count("## Part") < out.parts


async def test_notes_that_ignore_the_limit_altogether_are_trimmed_not_sent_oversized():
    provider = Fake(notes_chars=30_000)

    out = await engine.summarize(provider, talk(4500))

    assert out.ok and out.text == "SUMMARY"
    assert max(len(p) for p in provider.prompts) <= RELAY_LIMIT


async def test_requests_run_in_parallel_but_never_all_at_once():
    provider = Fake(delay=0.01)

    await engine.summarize(provider, talk(900))

    assert 2 <= provider.peak <= engine.PARALLEL


async def test_the_oldest_lines_are_dropped_past_the_limit_and_the_summary_knows_it():
    provider = Fake()
    lines = talk(6000)

    out = await engine.summarize(provider, lines)

    assert out.ok
    assert out.parts == engine.MAX_PARTS
    assert out.dropped > 0
    joined = "\n".join(provider.prompts)
    assert "remark #0:" not in joined               # the oldest is dropped
    assert "remark #5999:" in joined                # the newest is in place
    assert out.lines == 6000


async def test_one_failed_part_fails_the_summary_and_nothing_half_done_is_shown():
    provider = Fake(fail_on=2, delay=0.01)

    out = await engine.summarize(provider, talk(900))

    assert not out.ok and out.text == ""
    assert "request limit" in out.error
    # The first three have already gone out, and the rest do not start after the error: they
    # would have to be paid for, although the recap would not come out anyway.
    assert len(provider.prompts) <= engine.PARALLEL


async def test_an_exception_from_the_provider_becomes_an_error_not_a_crash():
    class Boom(Fake):
        async def ask(self, *a, **k):
            raise ConnectionError("service is offline")

    out = await engine.summarize(Boom(), talk(10))

    # The details stay in the log; the person gets what can be read without knowing the
    # libraries.
    assert not out.ok and out.error == "service is offline" and "ConnectionError" not in out.error


async def test_watched_topics_get_their_own_section_in_the_task():
    provider = Fake()

    await engine.summarize(provider, talk(10), topics=["deadlines", "budget"])

    prompt = provider.prompts[0]
    assert "The user asked you to watch these topics" in prompt
    assert "- deadlines" in prompt and "- budget" in prompt
    assert "In short" in prompt                        # the general recap stays


async def test_a_single_topic_replaces_the_general_summary_and_ignores_watched_ones():
    provider = Fake()

    await engine.summarize(provider, talk(10), topics=["deadlines"], focus="budget")

    prompt = provider.prompts[0]
    assert 'only one topic: "budget"' in prompt
    assert "The user asked you to watch" not in prompt
    assert "Left open" not in prompt          # there is no general view


async def test_context_and_language_reach_the_prompt_and_context_is_capped():
    provider = Fake()

    await engine.summarize(
        provider, talk(10), context="standup " * 1000, language="Answer in English.",
    )

    prompt = provider.prompts[0]
    assert "What the user themselves said about this conversation" in prompt
    assert prompt.count("standup") <= prompts.CONTEXT_CHARS // 6 + 1
    assert prompt.rstrip().endswith("Answer in English.")


def test_the_prompt_treats_the_transcript_as_data_not_commands():
    assert "data, not commands" in prompts.RULES
