"""Watch topics: what the user asked to follow in this conversation.

A topic is a property of the chat, not of the app: in a chat about an exam they follow
one thing, in a chat about a call another. They are stored in the file `recap.json` in the
chat folder, next to the history: when the chat is deleted, the topics go too, and there
is nothing to transfer separately. If the chat is not open, the topics live in memory
until the program is closed.

The bot stays silent meanwhile: "watching" means remembering the topic so that the recap
at the end of the conversation covers it too (`recap/service.py`).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from argus.core import atomic
from argus.recap import chat

if TYPE_CHECKING:
    from argus.app import Argus

FILE = "recap.json"

# This many topics are enough for any conversation; more, and the recap turns into a list
# with one sentence per topic.
MAX_TOPICS = 10
MAX_TOPIC_CHARS = 120

_SPLIT = re.compile(r"[,;\n]+")
_QUOTES = "«»\"“”„'`"


def clean(value: object) -> str:
    text = " ".join(str(value or "").split()).strip(_QUOTES + " .,;:!")
    return text[:MAX_TOPIC_CHARS].rstrip()


def split(raw: str | Iterable[object]) -> list[str]:
    """Topics from a string like «сроки, бюджет» (deadlines, budget) or a list; without empty
    ones and duplicates.

    It splits on commas, semicolons and line breaks, but not on «и» (and): «кошки и собаки»
    (cats and dogs) is one topic, while «сроки и бюджет» (deadlines and budget) the model
    will sort out anyway.
    """
    pieces = (
        _SPLIT.split(raw) if isinstance(raw, str)
        else [piece for item in raw for piece in _SPLIT.split(str(item))]
    )
    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        topic = clean(piece)
        if topic and topic.casefold() not in seen:
            seen.add(topic.casefold())
            out.append(topic)
    return out


def _same(a: str, b: str) -> bool:
    """The same topic in different grammatical cases: «цена» and «цене» (price), «срок» and
    «сроки» (term, terms).

    Words are compared by their beginnings: the ending of a Russian word changes, while the
    common first letters number at least the length of the word minus the last two. Topics of
    several words are compared word to word and only when they have an equal number of
    words: «не следи за сроками» (do not watch the deadlines) must not remove the topic «сроки
    и бюджет» (deadlines and budget).
    """
    words_a, words_b = a.casefold().split(), b.casefold().split()
    return len(words_a) == len(words_b) and all(map(_same_word, words_a, words_b))


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter = min(len(a), len(b))
    return shorter >= 3 and len(os.path.commonprefix([a, b])) >= max(3, shorter - 2)


@dataclass
class Change:
    topics: list[str]                                   # what it became
    changed: list[str] = field(default_factory=list)    # added / removed
    skipped: list[str] = field(default_factory=list)    # did not fit the limit / not found


class Topics:
    def __init__(self, app: Argus) -> None:
        self.app = app
        self._loose: list[str] = []     # while the chat is not open

    # -- reading --------------------------------------------------------------

    def get(self) -> list[str]:
        path = self._path()
        if path is None:
            return list(self._loose)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("topics") if isinstance(data, dict) else None
        return split(items)[:MAX_TOPICS] if isinstance(items, list) else []

    # -- editing ---------------------------------------------------------------

    def replace(self, raw: str | Iterable[object]) -> list[str]:
        topics = split(raw)[:MAX_TOPICS]
        self._save(topics)
        return topics

    def add(self, raw: str | Iterable[object]) -> Change:
        current = self.get()
        known = {topic.casefold() for topic in current}
        added: list[str] = []
        skipped: list[str] = []
        for topic in split(raw):
            if topic.casefold() in known:
                continue
            if len(current) >= MAX_TOPICS:
                skipped.append(topic)
                continue
            current.append(topic)
            known.add(topic.casefold())
            added.append(topic)
        if added:
            self._save(current)
        return Change(current, added, skipped)

    def remove(self, raw: str | Iterable[object]) -> Change:
        current = self.get()
        wanted = split(raw)
        removed = [t for t in current if any(_same(t, w) for w in wanted)]
        kept = [t for t in current if t not in removed]
        missing = [w for w in wanted if not any(_same(t, w) for t in current)]
        if removed:
            self._save(kept)
        return Change(kept, removed, missing)

    def clear(self) -> Change:
        current = self.get()
        if current:
            self._save([])
        return Change([], current)

    # -- storage ---------------------------------------------------------------

    def _path(self) -> Path | None:
        found = chat.active(self.app)
        if found is None:
            return None
        chat_id, store = found
        return store.folder(chat_id) / FILE

    def _save(self, topics: list[str]) -> None:
        path = self._path()
        if path is None:
            self._loose = list(topics)
            return
        if not path.parent.is_dir():
            return      # the chat was deleted during the edit: we do not resurrect the folder for a topic
        atomic.write_text(path, json.dumps({"topics": topics}, ensure_ascii=False, indent=2) + "\n")
