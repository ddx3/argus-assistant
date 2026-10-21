"""Words in the question line that are not questions to the model but recap commands.

«Итог» (recap), «итог по срокам» (recap on deadlines), «следи за ценой» (watch the
price), «за чем следишь» (what are you watching): the person tells the assistant what to
do with the conversation, and does not ask it about a subject. Such phrases are parsed
here, before the model: it is free, instant and the same every time, while the model
would answer something to «следи за ценой» but would not remember the topic.

The parsing is careful: a phrase must match a template **entirely** and be short.
Everything that did not match goes to the model as an ordinary question: it is better to
miss a command than to swallow a question the person really asked. So there is no
«слушай» (listen) without «следи за» (watch), and no «запомни про …» (remember about
...): too many ordinary questions begin that way.

Languages: Russian and English; the button in the interface works in any language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RECAP = "recap"         # recap; `arg` is one topic or empty (the whole conversation)
WATCH = "watch"         # watch the topic `arg`
UNWATCH = "unwatch"     # stop watching `arg`; empty means reset all topics
TOPICS = "topics"       # what we are watching right now

# Anything longer is no longer a command but a thought aloud.
MAX_LEN = 500

_POLITE = re.compile(r"^(?:пожалуйста|плиз|please)[,\s]+", re.IGNORECASE)
_TAIL = " .!?…"
_QUOTES = "«»\"“”„'`"
_THEME = re.compile(r"^(?:теме|темой|тему|темы)\s+", re.IGNORECASE)

_ASK = r"(?:(?:подведи|подвести|сделай|дай|напиши|нужен|нужно)\s+(?:мне\s+)?)?"
_WORD = r"(?:итог|итоги|саммари|сумари|пересказ)"
_ABOUT = r"(?:по|про|о|об)\s+(?:теме\s+)?"
_OF = r"(?:\s+(?:всего\s+)?(?:разговора|встречи|звонка|беседы|лекции|сессии|чата|созвона))?"
_CONVERSATION_EN = r"(?:call|conversation|meeting|lecture|chat|discussion|session)"
_STOP = (
    r"(?:не\s+следи|не\s+следите|перестань\s+следить|перестаньте\s+следить|"
    r"хватит\s+следить|прекрати\s+следить)"
)

# Order matters: the specific goes before the general («итог по срокам» (recap on
# deadlines) before «итог» (recap)). The third field says whether the phrase may be a
# question (a question mark at the end).
_TABLE: list[tuple[str, str, bool]] = [
    (UNWATCH, rf"{_STOP}\s+за\s+(?P<arg>.+)", False),
    (UNWATCH, r"(?:stop\s+(?:watching|tracking)|unwatch)\s+(?:for\s+)?(?P<arg>.+)", False),
    (UNWATCH, rf"{_STOP}(?:\s+ни\s+за\s+чем)?", False),
    (UNWATCH, r"(?:забудь|сбрось|очисти)\s+(?:все\s+)?темы", False),
    (UNWATCH, r"(?:stop\s+(?:watching|tracking)|unwatch)(?:\s+everything)?", False),
    (TOPICS, r"за\s+чем\s+(?:ты\s+)?след(?:ишь|им|ите)", True),
    (TOPICS, r"что\s+(?:ты\s+)?отслеживаешь", True),
    (TOPICS, r"(?:какие\s+)?темы|topics|what\s+are\s+you\s+(?:watching|tracking)", True),
    (RECAP, rf"{_ASK}{_WORD}\s+(?:мне\s+)?(?:только\s+)?{_ABOUT}(?P<arg>.+)", True),
    # «Перескажи про Наполеона» (Retell about Napoleon) is a question to the model, not a
    # recap of the conversation: a topic here is a command only together with a word saying
    # that it is about what was said («что говорили про …», what was said about ...).
    (RECAP, (
        rf"перескажи,?\s+(?:мне\s+)?(?:только\s+)?"
        rf"(?:что\s+)?(?:говорили|сказали|обсуждали|было)\s+{_ABOUT}(?P<arg>.+)"
    ), True),
    (RECAP, (
        rf"что\s+(?:там\s+)?(?:говорили|сказали|обсуждали|решили|было)\s+"
        rf"{_ABOUT}(?P<arg>.+)"
    ), True),
    # The same in English: "Summary of the French Revolution" is not a recap of the
    # conversation. The topic comes after a word about the conversation itself ("summary of
    # the call about pricing").
    (RECAP, (
        r"(?:recap|summary|summari[sz]e)\s+(?:of\s+|on\s+|about\s+|for\s+)(?:the\s+)?"
        rf"{_CONVERSATION_EN}\s+(?:about\s+|on\s+|regarding\s+|for\s+)(?P<arg>.+)"
    ), True),
    (RECAP, rf"{_ASK}{_WORD}{_OF}", True),
    (RECAP, (
        r"перескажи(?:\s+мне)?"
        r"(?:\s+(?:разговор|встречу|звонок|беседу|лекцию|созвон|всё|все))?"
    ), True),
    (RECAP, (
        r"(?:recap|summary|summari[sz]e|sum\s+up)"
        r"(?:\s+(?:the\s+)?(?:call|conversation|meeting|lecture|chat))?"
    ), True),
    (WATCH, (
        r"(?:(?:слушай|слушайте)[,\s]+(?:и\s+)?)?"
        r"(?:(?:следи|следите)\s+за|(?:отслеживай|отслеживайте)(?:\s+за)?)\s+(?P<arg>.+)"
    ), False),
    # Only stable phrases: bare "watch ..." and "track ..." are ordinary requests ("watch the
    # video again", "track the price of bitcoin"), and the model must hear them.
    (WATCH, r"(?:watch\s+for|keep\s+track\s+of|listen\s+for)\s+(?P<arg>.+)", False),
]
_RULES = [
    (kind, re.compile(rf"^(?:{pattern})$", re.IGNORECASE), asks) for kind, pattern, asks in _TABLE
]


@dataclass(frozen=True, slots=True)
class Command:
    kind: str
    arg: str = ""


def parse(text: str | None) -> Command | None:
    """A command from the question line, or `None` if it is an ordinary question."""
    if not text or len(text) > MAX_LEN:
        return None
    line = " ".join(text.split()).removeprefix("/")
    line = _POLITE.sub("", line)
    asks = line.rstrip().endswith("?")
    line = line.rstrip(_TAIL)
    # A question mark in the middle means a question about something, not a command.
    if not line or "?" in line:
        return None
    for kind, rule, may_ask in _RULES:
        found = rule.match(line)
        if found is None or (asks and not may_ask):
            continue
        if "arg" not in found.groupdict():
            return Command(kind)
        arg = _clean(found.group("arg"))
        if arg:
            return Command(kind, arg)
    return None


def _clean(arg: str) -> str:
    arg = " ".join(arg.split()).strip(_QUOTES + " .,;:!")
    return _THEME.sub("", arg).strip(_QUOTES + " .,;:!")
