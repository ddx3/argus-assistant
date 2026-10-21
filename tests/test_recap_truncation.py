"""Recap and record: an answer cut off by the ceiling is not passed off as complete
(chats-recap-1, -7).

Before, the recap and the record requests went with the same answer ceiling as live
answers (2000), and a text cut off mid-word was saved, shown and went into the PDF as
finished, while it was paid for as a whole one. Here we check that the cut-off is visible,
that the final step has a wider ceiling while the shared settings of live answers are not
touched, and that the provider with a single live conversation does not get the
transcripts into it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from argus.brain.base import Answer
from argus.core.config import BrainConfig
from argus.core.events import Source
from argus.recap import engine
from argus.recap.lines import Line

BASE = datetime(2026, 10, 6, 14, 0, tzinfo=UTC).timestamp()


def talk(count: int) -> list[Line]:
    return [
        Line(BASE + i * 20, Source.ME if i % 2 else Source.THEM, f"line number {i} about the release schedule")
        for i in range(count)
    ]


def long_talk(count: int) -> list[Line]:
    """A conversation of several parts: ~110 characters per utterance."""
    filler = "deadline budget release test deal " * 3
    return [
        Line(BASE + i * 20, Source.ME if i % 2 else Source.THEM, f"line #{i}: {filler}")
        for i in range(count)
    ]


class Wide:
    """A provider that records the answer ceiling it was called with."""

    def __init__(self, name: str = "relay", max_tokens: int = 2000, **reply) -> None:
        self.name = name
        self.cfg = BrainConfig(max_tokens=max_tokens)
        self.ceilings: list[int] = []
        self.prompts: list[str] = []
        self.reply = reply          # `meta` and `text` of the answer

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False):
        self.prompts.append(prompt)
        self.ceilings.append(self.cfg.max_tokens)
        text = self.reply.get("text", "ANSWER")
        meta = dict(self.reply.get("meta", {}))
        if "This is part" in prompt:
            text = "- notes on the part"
            meta = dict(self.reply.get("partial_meta", {}))
        return Answer(text=text, provider=self.name, model="m", meta=meta)


def test_the_note_has_the_same_words_as_the_relay_note():
    """The card must not name the same trouble with two different phrases."""
    from argus.brain import relay_api

    assert engine.CUT_NOTE == relay_api.TRUNCATED_NOTE


async def test_a_cut_answer_from_the_relay_is_marked_once():
    provider = Wide(
        text="Summary: the deadlines slipped.\n\n" + engine.CUT_NOTE, meta={"truncated": True},
    )

    out = await engine.summarize(provider, talk(6))

    assert out.ok and out.truncated
    assert out.text == "Summary: the deadlines slipped.\n\n" + engine.CUT_NOTE
    assert out.text.count(engine.CUT_NOTE) == 1


async def test_a_cut_answer_from_a_direct_provider_is_found_by_its_token_count():
    """Direct providers give no truncation flag, but they do report how much they wrote."""
    provider = Wide(name="openai-api", text="Summary: deadlines", meta={"output_tokens": 8000})

    out = await engine.summarize(provider, talk(6))

    assert out.truncated
    assert out.text == "Summary: deadlines\n\n" + engine.CUT_NOTE


async def test_an_empty_answer_that_ate_the_whole_ceiling_says_why():
    """The ceiling went on reasoning and no words are left: the cause is lack of room, not "came
    back empty".
    """
    provider = Wide(name="openai-api", text="", meta={"output_tokens": 8000})

    out = await engine.summarize(provider, talk(6))

    assert not out.ok and out.error == engine.CUT_EMPTY


async def test_an_empty_answer_below_the_ceiling_is_just_empty():
    provider = Wide(name="openai-api", text="", meta={"output_tokens": 5})

    out = await engine.summarize(provider, talk(6))

    assert not out.ok and out.error == "the answer came back empty"


async def test_an_answer_below_the_ceiling_is_left_alone():
    provider = Wide(name="openai-api", text="Summary: deadlines", meta={"output_tokens": 7999})

    out = await engine.summarize(provider, talk(6))

    assert out.ok and not out.truncated and out.text == "Summary: deadlines"


async def test_the_relay_flag_beats_the_token_count():
    """The server's flag is more reliable than a guess: an answer exactly at the ceiling may have
    ended by itself.
    """
    provider = Wide(text="Summary", meta={"truncated": False, "output_tokens": 99_999})

    out = await engine.summarize(provider, talk(6))

    assert not out.truncated and out.text == "Summary"


async def test_a_cut_middle_step_is_marked_at_the_end_and_does_not_leak_into_the_next_prompt():
    provider = Wide(
        text="SUMMARY", partial_meta={"truncated": True},
    )
    # The notes for the part are cut off; it carries the relay's mark in its text.
    original = provider.ask

    async def ask(prompt, system=None, **kw):
        answer = await original(prompt, system)
        if "This is part" in prompt:
            answer.text += "\n\n" + engine.CUT_NOTE
        return answer

    provider.ask = ask

    out = await engine.summarize(provider, long_talk(900))

    assert out.parts > 1 and out.truncated
    assert out.text == "SUMMARY\n\n" + engine.CUT_NOTE
    assert not any(engine.CUT_NOTE in p for p in provider.prompts)


async def test_only_the_final_step_gets_the_wide_ceiling_and_the_shared_config_stays_put():
    provider = Wide()

    out = await engine.summarize(provider, long_talk(900))

    assert out.ok and out.parts > 1
    # Notes by parts get the ordinary ceiling, the final request the wide one.
    partial = [c for c, p in zip(provider.ceilings, provider.prompts) if "This is part" in p]
    assert partial and set(partial) == {2000}
    assert provider.ceilings[-1] == engine.FINAL_MAX_TOKENS
    # Live answers go with the same settings object, so it must not be left wide.
    assert provider.cfg.max_tokens == 2000


async def test_a_short_conversation_asks_once_with_the_wide_ceiling():
    provider = Wide(name="anthropic-api")

    await engine.perform(provider, talk(6), "notes")

    assert provider.ceilings == [engine.FINAL_MAX_TOKENS]
    assert provider.cfg.max_tokens == 2000


async def test_a_ceiling_that_is_already_wide_is_not_touched():
    provider = Wide(max_tokens=12_000)

    await engine.summarize(provider, talk(6))

    assert provider.ceilings == [12_000]


async def test_providers_without_a_settable_ceiling_are_asked_as_they_are():
    provider = Wide(name="fake")

    await engine.summarize(provider, talk(6))

    assert provider.ceilings == [2000]


async def test_a_one_conversation_provider_never_gets_the_transcripts(monkeypatch):
    """A one-conversation provider keeps a single conversation for the whole run: recap requests go
    into sessions of their own.
    """
    from argus.brain import claude_code

    made: list[object] = []

    class Throwaway:
        name = "claude-code"

        def __init__(self, cfg) -> None:
            self.cfg = cfg
            self.closed = False
            self.prompts: list[str] = []
            made.append(self)

        async def ask(self, prompt, system=None, image=None, pinned=(), deep=False):
            self.prompts.append(prompt)
            return Answer(text="SUMMARY", provider=self.name, model="m")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(claude_code, "ClaudeCodeProvider", Throwaway)
    shared = Wide(name="claude-code")

    out = await engine.summarize(shared, long_talk(900))

    assert out.ok and out.text == "SUMMARY"
    assert shared.prompts == []                       # the live conversation is untouched
    assert len(made) == out.calls > 1                 # one session per request
    assert all(session.closed for session in made)    # and none was left hanging
    assert all(len(session.prompts) == 1 for session in made)
