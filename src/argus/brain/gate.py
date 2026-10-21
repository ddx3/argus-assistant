"""The decision whether to wake the model.

This follows directly from working through the subscription: it has limits in five-hour
windows, and a pipeline that pokes the model on every utterance burns them in an hour. So
between the timeline and the model there is a layer that costs microseconds and filters
out most of the triggers.

It is deliberately dumb: rule-based, not model-based. The reason is the asymmetry of
cost. A check costs nothing, while a false wake-up costs money, seconds of delay, and a
piece of the limit. Rules that err toward silence are worth more here than a smart model
that errs both ways and itself costs time.

A second consideration is debugging. When an answer did not arrive, you need to
understand why instantly. Rules have a clear answer for that as a string; a filter model
does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from argus.context.timeline import Entry, Kind
from argus.core.events import Source

NS_PER_SEC = 1_000_000_000

# Pause between semantic checks (`Gate.check_budget`): shorter than the pause between
# requests, but not zero, or every question-like utterance would cost a call.
CHECK_COOLDOWN_S = 2.0


class Trigger(str, Enum):
    """Why the model was woken."""

    HOTKEY = "hotkey"          # asked by hand
    QUESTION = "question"      # the other party asked a question
    SCREEN = "screen"          # something new appeared on the screen
    TIMER = "timer"            # nothing has been said for a long time


class ReplyMode(str, Enum):
    """What counts as a reason to answer an utterance, your own or the other party's.

    Five steps from the noisiest to complete silence: the design is not "on/off" but a ladder
    of trade-offs between how much it catches and how much extra pops onto the screen or
    burns the subscription limit. The same ladder serves both speech sources
    (`GateSettings.mic_mode` is the microphone, `.system_mode` is system audio, that is, the
    other party): each source has its own step, not one shared by both.
    """

    OFF = "off"                    # an utterance from this source is no reason at all
    ALL = "all"                    # any utterance from there, without a filter
    BY_FORM = "by_form"            # looks like a question grammatically, and that is all
    WAKE_WORD = "wake_word"        # only if the bot was addressed by name
    SEMANTIC = "semantic"          # the same, but the model is also asked whether it is worth it


# The address word is searched for on word boundaries, not as a substring: «Аргус» must
# not be caught inside a random «аргумент» (argument), while the word itself may be
# declined beyond recognition and not be found. This does not solve exact cases, but it
# catches the most common one («Аргус, ...») and the cost of a mistake here is silence, not
# an extra answer.
def _mentions_wake_word(text: str, wake_word: str) -> bool:
    word = wake_word.strip()
    if not word:
        return False
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


# A question in Russian does not always end with a mark: recognition often loses it, and
# live speech gets by with intonation alone. So we look at the beginning as well.
QUESTION_WORDS = {
    "что", "как", "какой", "какая", "какие", "какое", "каким", "почему", "зачем",
    "когда", "где", "куда", "откуда", "кто", "кого", "кому", "сколько", "чем",
    "чей", "можешь", "можете", "расскажи", "расскажите", "объясни", "объясните",
    "опиши", "опишите", "назови", "назовите", "перечисли", "перечислите",
    "в чём", "чём", "разница", "отличие",
    "what", "how", "why", "when", "where", "who", "which", "can", "could",
    "would", "explain", "describe", "tell",
}

# Orders count as a task even without a question mark. Only the imperative forms
# themselves, and only at the start of a sentence (or of its part after a comma): before,
# word stems were searched for anywhere, and «Мы решили запустить проект» (We decided to
# launch the project), «найдём время на обед» (we'll find time for lunch), and "We
# implemented the feature" woke the paid model on a perfectly ordinary phrase. They are
# listed together with the "-те" forms: the stem «реши» without a word boundary caught both
# «решили» (decided) and «решит» (will decide).
TASK_WORDS = {
    "напиши", "напишите", "реши", "решите", "реализуй", "реализуйте", "найди", "найдите",
    "посчитай", "посчитайте", "вычисли", "вычислите", "докажи", "докажите",
    "оптимизируй", "оптимизируйте", "сформулируй", "сформулируйте", "приведи", "приведите",
    "сравни", "сравните",
    "write", "implement", "solve", "calculate",
}

# Particles and introductory words that live speech starts with. They must be skipped
# before looking at the "first" word: «А что насчёт индексов?» (And what about indexes?)
# is a question, and most of them in conversation are like that.
FILLERS = {
    "а", "и", "но", "ну", "вот", "так", "ладно", "хорошо", "окей", "ок",
    "слушай", "слушайте", "скажи", "скажите", "кстати", "значит", "итак",
    "тогда", "теперь", "давай", "давайте", "эм", "э", "ага", "да", "ой",
    "ну-ка", "погоди", "погодите", "хм", "well", "so", "ok", "okay", "and",
}
MAX_FILLERS = 3


def _sentence_is_question(sentence: str) -> bool:
    s = sentence.strip()
    if not s:
        return False
    if s.endswith("?"):
        return True

    words = [w.strip(",.!;:—-–«»\"'") for w in s.lower().split()]
    words = [w for w in words if w]
    for word in words[: MAX_FILLERS + 1]:
        if word in QUESTION_WORDS:
            return True
        if word not in FILLERS:
            return False
    return False


# What can stand before an order: «А теперь напиши…» (And now write...), «Пожалуйста,
# реши…» (Please solve...).
TASK_LEAD = FILLERS | {"пожалуйста", "please", "now", "then"}

# «Давайте решим задачу» (Let's solve the problem) is a request too, but «решим» without
# «давайте» is just the future tense («Решим завтра», We'll decide tomorrow), and it is
# accepted only after this word.
LETS = {"давай", "давайте"}
LETS_WORDS = {
    "напишем", "решим", "реализуем", "найдём", "найдем", "посчитаем", "вычислим", "докажем",
    "оптимизируем", "сформулируем", "приведём", "приведем", "сравним",
}


def _sentence_is_task(sentence: str) -> bool:
    """An order is the first significant word of a sentence, or of a part of it after a comma, colon
    or dash.
    """
    for clause in re.split(r"[,:—–]+", sentence):
        words = [w.strip(",.!;:—-–«»\"'") for w in clause.lower().split()]
        lets = False
        for word in [w for w in words if w][: MAX_FILLERS + 1]:
            if word in TASK_WORDS or (lets and word in LETS_WORDS):
                return True
            lets = lets or word in LETS
            if word not in TASK_LEAD:
                break
    return False


def looks_like_question(text: str) -> bool:
    """Does this look like a question or a task.

    The question mark is the most reliable sign, but recognition loses it, and in live
    speech a question is often carried by intonation alone. So we also look at the first
    meaningful word: in Russian a question almost always starts with it.

    We check **every sentence**, not only the start of the utterance. A live run showed why
    this matters: the speech detector glued "Hello, this is a speech recognition check. Tell
    me about your experience with databases" into one utterance, and by the first word
    "Hello" the real question in the second sentence would have been lost entirely.
    """
    t = text.strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return any(
        _sentence_is_question(part) or _sentence_is_task(part)
        for part in re.split(r"[.!?;]+", t)
    )


@dataclass(slots=True)
class Decision:
    wake: bool
    reason: str
    trigger: Trigger | None = None
    # Who exactly asked: only for QUESTION. Before, the calling code itself dug the "last
    # utterance of the other party" out of the timeline and was always right, because the
    # source of the question could be only one. Now there are two (mic_mode and system_mode
    # below), and without an explicit source the calling code would guess whose utterance to
    # take.
    source: Source | None = None


@dataclass
class GateSettings:
    # Minimum idle time between requests. The main protection of the subscription limits.
    cooldown_s: float = 8.0

    # Ceiling on requests per session. Insurance against an infinite loop: if something goes
    # wrong, the limits will burn silently and it will be discovered at the worst moment, so
    # it is better to hit your own ceiling and say so.
    max_calls: int = 200

    # An utterance shorter than this is almost always «ага» (uh-huh), «понятно» (I see), or
    # «да» (yes): no reason to wake the model.
    min_question_chars: int = 12

    # How long a silence before the timer fires. 0 means never fire.
    idle_s: float = 0.0

    # How many new lines on the screen count as a trigger. A tab switch gives one or two,
    # while the appearance of a task gives many at once.
    screen_new_lines: int = 4

    react_to_screen: bool = False   # silent by default: the screen makes more noise than speech

    # Our own speech is ignored by default: there is no point answering yourself, and in a
    # video call with separate channels it is also simply someone else's utterance recorded in
    # the wrong place. It makes sense to turn it on where there are no separate channels at all
    # (an in-person conversation through a single microphone), and the other party's voice
    # physically lands in the same channel as your own.
    mic_mode: ReplyMode = ReplyMode.OFF

    # System audio, which is what the other party physically says, by default answers any
    # grammatical form of a question, including «Слышно меня?» (Can you hear me?): the bot must
    # answer a direct question, even a trivial one. A step lower or higher is already a
    # conscious choice by the person.
    system_mode: ReplyMode = ReplyMode.BY_FORM

    # The address word for ReplyMode.WAKE_WORD, shared by both sources: the bot's name does not
    # depend on where it was called from. Empty means the mode simply never fires (see
    # `_mentions_wake_word`), rather than crashing.
    wake_word: str = ""


@dataclass
class Gate:
    """The filter between the timeline and the model."""

    settings: GateSettings = field(default_factory=GateSettings)

    calls: int = 0
    # Of these, how many were at the person's request (hotkey, button). The `max_calls` ceiling
    # is insurance against a loop of automatic triggers, and it is counted for each kind
    # separately: otherwise a drift of false triggers would eat the ceiling, and an explicit
    # request from the person would be refused until the end of the session.
    manual_calls: int = 0
    last_call_ns: int = -10**18
    # Semantic checks (`ReplyMode.SEMANTIC`) are paid calls too, but they do not count as
    # requests: a "no" answer moves neither `calls` nor `last_call_ns`. They have their own
    # count and their own pause.
    checks: int = 0
    last_check_ns: int = -10**18
    # The bare text of the question, to compare with a new utterance in on_speech. Not the
    # whole prompt: that carries the conversation timeline and will not match itself.
    last_question: str = ""

    # Counters of refusals, so that it is visible why the model is silent.
    blocked_cooldown: int = 0
    blocked_limit: int = 0
    blocked_short: int = 0
    blocked_repeat: int = 0
    blocked_not_question: int = 0
    blocked_no_wake: int = 0
    # Set not by the gate itself (it is synchronous and does not reach the model) but by
    # assistant.py, after ReplyMode.SEMANTIC asked the model directly and got a "no". It lives
    # here to have a single place that answers "why is it silent", rather than a second counter
    # in another file.
    blocked_not_relevant: int = 0

    def _budget(self, now: int) -> Decision | None:
        """General limits that do not depend on the reason."""
        if self.calls - self.manual_calls >= self.settings.max_calls:
            self.blocked_limit += 1
            return Decision(False, f"limit of {self.settings.max_calls} requests reached")
        if (now - self.last_call_ns) < self.settings.cooldown_s * NS_PER_SEC:
            self.blocked_cooldown += 1
            left = self.settings.cooldown_s - (now - self.last_call_ns) / NS_PER_SEC
            return Decision(False, f"pause after the previous request, {left:.0f} s left")
        return None

    def on_hotkey(self, now: int) -> Decision:
        """A manual request. The pause does not apply: the person knows what they want."""
        if self.manual_calls >= self.settings.max_calls:
            self.blocked_limit += 1
            return Decision(False, "request limit reached")
        return Decision(True, "asked manually", Trigger.HOTKEY)

    def check_budget(self, now: int) -> Decision | None:
        """Whether a semantic check can be done now. `None` means it can, and it has been recorded.

        The same `max_calls` ceiling, but on its own count, and a pause shorter than for a
        request: the check answers with a short yes/no, and erring toward silence here is
        costlier than in the answer.
        """
        if self.checks >= self.settings.max_calls:
            self.blocked_limit += 1
            return Decision(False, "check limit reached")
        if (now - self.last_check_ns) < CHECK_COOLDOWN_S * NS_PER_SEC:
            self.blocked_cooldown += 1
            return Decision(False, "pause after the previous check")
        self.checks += 1
        self.last_check_ns = now
        return None

    def on_speech(self, entry: Entry, now: int) -> Decision:
        """An utterance from the microphone or system audio, by its own step.

        The source decides whose step looks at the utterance: `mic_mode` for your own speech,
        `system_mode` for the other party. `OFF` does not let an utterance from this source
        through at all; `ALL` and `WAKE_WORD` decide its fate themselves, bypassing the common
        "looks like a question" filter below, since they have their own trigger. `BY_FORM` and
        `SEMANTIC` go along this common path: a purely grammatical check, without the "is it
        chatter or not" analysis (that lived in `AssistGate`, not here, and now applies only to
        `SEMANTIC`, see `assist/gate.py`). The synchronous gate does not perform the semantic
        check for `SEMANTIC` itself: it needs a model and lives in `assistant.py`, on top of the
        decision from here.
        """
        if entry.kind is not Kind.SPEECH:
            return Decision(False, "not speech")

        mode = self.settings.mic_mode if entry.source is Source.ME else self.settings.system_mode
        if mode is ReplyMode.OFF:
            return Decision(
                False, "this is my own line" if entry.source is Source.ME else "reaction to the other party is off"
            )
        if mode is ReplyMode.ALL:
            blocked = self._budget(now)
            reason = "mic line" if entry.source is Source.ME else "their line"
            return blocked or Decision(True, reason, Trigger.QUESTION, source=entry.source)
        if mode is ReplyMode.WAKE_WORD:
            if not _mentions_wake_word(entry.text, self.settings.wake_word):
                self.blocked_no_wake += 1
                return Decision(False, "the bot was not called by name")
            blocked = self._budget(now)
            return blocked or Decision(True, "called by name", Trigger.QUESTION, source=entry.source)

        text = entry.text.strip()
        if len(text) < self.settings.min_question_chars:
            self.blocked_short += 1
            return Decision(False, f"too short ({len(text)} chars)")
        if not looks_like_question(text):
            self.blocked_not_question += 1
            return Decision(False, "doesn't look like a question")
        if text == self.last_question:
            self.blocked_repeat += 1
            return Decision(False, "this was already asked")

        blocked = self._budget(now)
        reason = "they asked a question" if entry.source is Source.THEM else "your own question aloud"
        return blocked or Decision(True, reason, Trigger.QUESTION, source=entry.source)

    def on_screen(self, new_lines: int, now: int) -> Decision:
        if not self.settings.react_to_screen:
            return Decision(False, "reaction to the screen is off")
        if new_lines < self.settings.screen_new_lines:
            return Decision(False, f"little new on screen ({new_lines} lines)")
        blocked = self._budget(now)
        return blocked or Decision(True, f"{new_lines} new lines on screen", Trigger.SCREEN)

    def on_idle(self, quiet_s: float, now: int) -> Decision:
        if self.settings.idle_s <= 0:
            return Decision(False, "timer trigger is off")
        if quiet_s < self.settings.idle_s:
            return Decision(False, f"only {quiet_s:.0f} s of silence")
        blocked = self._budget(now)
        return blocked or Decision(True, f"silence {quiet_s:.0f} s", Trigger.TIMER)

    def accept(self, now: int, question: str, manual: bool = False) -> None:
        """Record that a request took place.

        `question` is the bare reason for the request (`cause` at the caller), not the assembled
        prompt: see `last_question`. `manual` means it was asked by hand.
        """
        self.calls += 1
        if manual:
            self.manual_calls += 1
        self.last_call_ns = now
        self.last_question = question.strip()

    @property
    def blocked_total(self) -> int:
        return (
            self.blocked_cooldown
            + self.blocked_limit
            + self.blocked_short
            + self.blocked_repeat
            + self.blocked_not_question
            + self.blocked_no_wake
            + self.blocked_not_relevant
        )

    def stats(self) -> dict[str, int]:
        return {
            "requests": self.calls,
            "filtered out": self.blocked_total,
            "pause": self.blocked_cooldown,
            "limit": self.blocked_limit,
            "short": self.blocked_short,
            "repeat": self.blocked_repeat,
            "not a question": self.blocked_not_question,
            "not by name": self.blocked_no_wake,
            "not relevant": self.blocked_not_relevant,
        }
