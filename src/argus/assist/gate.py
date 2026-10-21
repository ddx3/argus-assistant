"""The "question or chatter" filter, on top of the general one.

The core filters by the question mark and the first significant word. That was enough
while the answer went to a dashboard that nobody looks at. With an overlay everything is
different: it takes a corner of the screen and draws the eye, and a hint at the wrong time
costs more than no hint. Flooding the screen with text exactly when one has to listen to
the interviewer is the most expensive mistake of the channel.

So there are two extra rules here, both about live conversation, not about language. Both
err on the side of silence, since the cost of errors is asymmetric: a missed question
costs one second (you can press the hotkey), an extra answer costs attention at a moment
when there is none to spare.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from argus.assist.settings import Answering
from argus.brain.gate import Decision, Gate, ReplyMode
from argus.context.timeline import Entry, Kind
from argus.core.events import Source

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize(text: str) -> str:
    return _PUNCT.sub(" ", text.lower()).strip()


def is_small_talk(text: str, phrases: list[str]) -> bool:
    """The utterance consists entirely of politeness and connection checks.

    We check not "is the phrase inside" but "does it take up almost the whole utterance". The
    difference matters: "Слышно меня?" (Can you hear me?) is chatter, while "Расскажите, как
    вы поняли, что всё хорошо?" (Tell me how you understood that everything is fine?) is a
    real question in which the stop phrase turned up by chance. A substring search without
    this correction would silently swallow such questions, and it would look like a
    recognition fault.
    """
    norm = normalize(text)
    if not norm:
        return True
    for phrase in phrases:
        p = normalize(phrase)
        if p and p in norm and len(norm) <= len(p) + 12:
            return True
    return False


@dataclass
class AssistGate(Gate):
    """The shared filter plus what only this channel knows."""

    answering: Answering = field(default_factory=Answering)
    blocked_small_talk: int = 0
    blocked_few_words: int = 0

    def on_speech(self, entry: Entry, now: int) -> Decision:
        # Only `SEMANTIC` reaches the chatter and "too few words" checks: for it they are a free
        # pre-filter before the paid model call, so there is no point asking the model about
        # "Ага, понятно" (Uh-huh, I see), the form already shows it. `BY_FORM` deliberately does not
        # pass through this layer: its whole idea is to answer any grammatical question, including
        # "Слышно меня?" (Can you hear me?). It used to be the same step as ordinary text, and the
        # chatter filter muted exactly such short direct questions. `ALL` and `WAKE_WORD` decide the
        # fate of an utterance in the core itself (`Gate.on_speech`) on their own grounds (whether
        # it was addressed by name, not whether it "looks like a question"), and chatter and "too
        # few words" do not belong there: someone who said "Аргус, спасибо" (Argus, thanks) has made
        # an address, not a politeness to be filtered out.
        mode = self.settings.mic_mode if entry.source is Source.ME else self.settings.system_mode
        considered = mode is ReplyMode.SEMANTIC
        if entry.kind is Kind.SPEECH and considered:
            text = entry.text.strip()
            words = len(text.split())

            # Stop phrases first, then length, and the order here is for the sake of the counters.
            # Politeness is almost always short, and by length it would be filtered out first, and the
            # statistics would say "too short" instead of "chatter". The reason
            # for a refusal is what the counters exist for: "doesn't look like a question" and "pause
            # after the previous one" are fixed in different ways.
            if self.answering.ignore_small_talk and is_small_talk(
                text, self.answering.stop_phrases
            ):
                self.blocked_small_talk += 1
                return Decision(False, "politeness and connection check, not a question")

            # We count words, not characters, and this is not nitpicking. The core's threshold in
            # characters lets "Ага, понял, спасибо" (Uh-huh, got it, thanks) through (nineteen
            # characters) and cuts off "Что такое GIL?" (What is a GIL?), which is shorter.
            if words < self.answering.min_words:
                self.blocked_few_words += 1
                return Decision(False, f"too short ({words} words)")

        return super().on_speech(entry, now)

    @property
    def blocked_total(self) -> int:
        return super().blocked_total + self.blocked_small_talk + self.blocked_few_words

    def stats(self) -> dict[str, int]:
        out = super().stats()
        out["chatter"] = self.blocked_small_talk
        out["too short"] = self.blocked_few_words
        return out


def install(app, settings, keep_counters: bool = True) -> AssistGate:
    """Replace the filter in a running assistant, keeping the counters.

    The counters survive the edit on purpose: they are about the session, not about the
    setting. Zeroing them here would erase exactly the statistics for the sake of which the
    person went to tweak the filter.

    `keep_counters=False` is for when the session is different: another chat was opened
    (`argus/chats/`). The cap on requests and the "pause after the previous one" of one
    conversation must not cut off the first question of the next.
    """
    from argus.brain.gate import GateSettings

    old = getattr(app.assistant, "gate", None) if app.assistant else None

    # mic_mode, system_mode and wake_word do not belong in gate_brain: they are all about the
    # conversation ("whom to answer"), not about the engine, and they live in the channel
    # settings. We mix them into the core's GateSettings right here, in one place, instead of
    # having three sources of truth in different corners of the code.
    gate_settings = GateSettings(
        **(app.cfg.gate_brain.model_dump() if app.assistant else {}),
        mic_mode=settings.answering.mic_mode,
        system_mode=settings.answering.system_mode,
        wake_word=settings.answering.wake_word,
    )
    if app.assistant is None:
        return AssistGate(settings=gate_settings, answering=settings.answering)

    gate = AssistGate(settings=gate_settings, answering=settings.answering)
    if old is not None and keep_counters:
        for name in (
            "calls", "manual_calls", "checks", "last_check_ns",
            "last_call_ns", "last_question", "blocked_cooldown", "blocked_limit",
            "blocked_short", "blocked_repeat", "blocked_not_question", "blocked_no_wake",
            "blocked_not_relevant",
        ):
            setattr(gate, name, getattr(old, name))
        gate.blocked_small_talk = getattr(old, "blocked_small_talk", 0)
        gate.blocked_few_words = getattr(old, "blocked_few_words", 0)
    app.assistant.gate = gate
    return gate
