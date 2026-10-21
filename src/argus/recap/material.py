"""The session recording: utterances from the log with the user's edits.

The log (`chats/<id>/history.jsonl`) belongs to the chats and is append-only: fixing a
line in it would mean rewriting a file that is owned by someone else and may be being
written to at that very moment. So the edits lie next to it, in `edits.json`: what was
corrected and what was removed, keyed by the utterance's timestamp. Everyone reads the
recording (model requests, the page, the PDF) by one path, `load`, and always sees the
already corrected text. The log itself stays intact: whatever was removed can be brought
back by erasing the edit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from argus.context.render import CHARS_PER_TOKEN
from argus.core import atomic
from argus.recap.lines import PAUSE_S, Line, from_history

if TYPE_CHECKING:
    from argus.chats.store import ChatStore

FILE = "edits.json"

# An utterance key is its timestamp from the log, as is. It comes from the browser and goes
# into the request path, so the format is checked strictly.
KEY = re.compile(r"\d{9,11}(?:\.\d+)?")

# Anything longer is not an utterance but a pasted paragraph; the log does not write that.
MAX_LINE_CHARS = 4000


def key_of(ts: float) -> str:
    return repr(float(ts))


def valid_key(key: object) -> bool:
    return isinstance(key, str) and KEY.fullmatch(key) is not None


@dataclass(frozen=True, slots=True)
class Entry:
    """An utterance with a key for edits."""

    key: str
    line: Line
    edited: bool = False


@dataclass(frozen=True, slots=True)
class Stats:
    lines: int
    chars: int
    tokens: int         # input per request: the whole recording goes to the model
    minutes: int        # speech time, without breaks in the recording
    first: float | None
    last: float | None


class Edits:
    """Edits of one session: `{"fix": {key: text}, "drop": [key, …]}`."""

    def __init__(self, store: ChatStore, chat_id: str) -> None:
        self.path: Path = store.folder(chat_id) / FILE

    def read(self) -> tuple[dict[str, str], set[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, set()
        if not isinstance(data, dict):
            return {}, set()
        fix = data.get("fix")
        drop = data.get("drop")
        return (
            {k: v for k, v in fix.items() if valid_key(k) and isinstance(v, str)}
            if isinstance(fix, dict) else {},
            {k for k in drop if valid_key(k)} if isinstance(drop, list) else set(),
        )

    def _write(self, fix: dict[str, str], drop: set[str]) -> None:
        if not self.path.parent.is_dir():
            return      # the session was deleted during the edit: we do not resurrect the folder for it
        atomic.write_text(
            self.path,
            json.dumps({"fix": fix, "drop": sorted(drop)}, ensure_ascii=False, indent=2) + "\n",
        )

    def fix(self, key: str, text: str) -> None:
        fix, drop = self.read()
        fix[key] = " ".join(text.split())[:MAX_LINE_CHARS]
        drop.discard(key)
        self._write(fix, drop)

    def drop(self, key: str) -> None:
        fix, drop = self.read()
        fix.pop(key, None)
        drop.add(key)
        self._write(fix, drop)


def load(store: ChatStore, chat_id: str) -> list[Entry]:
    """The session's utterances in order, with the edits already applied. Reads a file: call from a
    thread.
    """
    fix, drop = Edits(store, chat_id).read()
    out: list[Entry] = []
    seen: set[str] = set()
    for line in from_history(store.history(chat_id)):
        key = key_of(line.ts)
        # The timestamp is the time of the write to the log, and two utterances in the same clock
        # tick (both channels finished speaking at once, or the log was written with a delay) would
        # get one key: an edit or a "remove" of one would affect the other too. Each gets its own
        # timestamp: the one that came next is a microsecond later.
        shifted = line.ts
        while key in seen:
            shifted += 1e-6
            key = key_of(shifted)
        seen.add(key)
        if key in drop:
            continue
        if fix.get(key):
            out.append(Entry(key, Line(line.ts, line.source, fix[key]), edited=True))
        else:
            out.append(Entry(key, line))
    return out


def stats(lines: Iterable[Line]) -> Stats:
    items = list(lines)
    chars = sum(len(line.text) for line in items)
    seconds = sum(
        gap for a, b in pairwise(items)
        if (gap := b.ts - a.ts) < PAUSE_S
    )
    return Stats(
        lines=len(items),
        chars=chars,
        tokens=int(chars / CHARS_PER_TOKEN),
        minutes=round(seconds / 60) if items else 0,
        first=items[0].ts if items else None,
        last=items[-1].ts if items else None,
    )


def clock(ts: float) -> str:
    """Utterance time for the page: hours, minutes, seconds by the computer's clock."""
    return datetime.fromtimestamp(ts, UTC).astimezone().strftime("%H:%M:%S")
