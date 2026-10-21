"""Naming a session from its description.

The person can skip the name when creating a session (starting right away), and then
the app comes up with one itself, from the "Conversation context" text or, if there is
none, from the first question and answer.
This module holds only the words: who runs it and when is decided by `ChatManager`
(`manager.py`).

There are two paths. A model comes up with a better name, but it may be unavailable
(not signed in to an account, no network, a stub instead of a model). In that case
`derive_title` takes the beginning of the description itself. The model's answer is
not taken on faith: `clean_title` keeps one short line and discards anything that
does not look like a name, and then the fallback path runs as well.
"""

from __future__ import annotations

import re

# A description shorter than this is not a description yet but the start of a phrase:
# no title can be made from "Interv", so we keep waiting.
NAME_MIN_CHARS = 20

# A longer title does not fit in the session list (the title column is two lines) and
# reads worse than the description itself.
TITLE_MAX = 60
FALLBACK_MAX = 48

# The description goes to the model whole, up to this limit: the beginning is enough
# for a title, and there is no reason to pay for pages of a resume to get three words.
PROMPT_CHARS = 1500

TITLE_SYSTEM = (
    "You come up with short titles for conversations from their description. "
    "Reply with one title only: two to six words, in the language of the description, "
    "no quotation marks, no full stop at the end, no explanations."
)

_TRIM = " \t\r\n\"'`*_«»“”„‹›"
_END = " .!?…,;:-–—"
_PREFIX = re.compile(r"^(название|заголовок|title)\s*[:：\-–—]\s*", re.IGNORECASE)
_SENTENCE = re.compile(r"[.!?…](?:\s|$)")


def title_prompt(text: str) -> str:
    return "Conversation description:\n" + text.strip()[:PROMPT_CHARS]


def _has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _cut_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Cut at a word, not in the middle of one; but if there is hardly a space, cut as is.
    head, sep, _ = cut.rpartition(" ")
    cut = (head if sep and len(head) >= limit // 2 else cut).rstrip(_END)
    # We do not leave a preposition or conjunction at the end ("...on design and"): a title
    # that breaks off at "and" reads as unfinished.
    while True:
        head, sep, last = cut.rpartition(" ")
        if sep and len(last) <= 2 and len(head) >= limit // 2:
            cut = head.rstrip(_END)
        else:
            return cut


def clean_title(raw: str) -> str:
    """Title from the model's answer. Empty means it does not look like a title."""
    line = next((x.strip() for x in str(raw or "").splitlines() if x.strip()), "")
    line = _PREFIX.sub("", line).strip(_TRIM).rstrip(_END)
    line = _cut_at_word(" ".join(line.split()), TITLE_MAX)
    return line if len(line) >= 3 and _has_letters(line) else ""


def derive_title(text: str, limit: int = FALLBACK_MAX) -> str:
    """A title from the start of the description, without a model: the first sentence, and
    a long one is cut at a punctuation mark or at a word.

    "Backend developer, five years of Python. I'm interviewing for…" →
    "Backend developer, five years of Python";
    "I'm taking a database exam: normal forms, indexes…" →
    "I'm taking a database exam".
    """
    first = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    match = _SENTENCE.search(first)
    if match and match.start() >= 8:
        first = first[:match.start()]
    first = " ".join(first.split()).strip(_TRIM).rstrip(_END)

    if len(first) > limit:
        cut = first[:limit]
        soft = max(cut.rfind(mark) for mark in (":", ";", " — ", " – ", ","))
        # We take the soft separator only if at least half of the limit has been collected
        # before it: a six-character start like "Taking," will not make a title.
        first = cut[:soft] if soft >= limit // 2 else _cut_at_word(first, limit)
        first = first.strip(_TRIM).rstrip(_END)

    if len(first) < 3 or not _has_letters(first):
        return ""
    return first[0].upper() + first[1:]
