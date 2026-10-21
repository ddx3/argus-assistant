"""A single timeline of events: what was said and what was seen, in time order.

Speech and screen arrive on different channels and at different rates, but the model
needs one chronology. Here they are merged.

The main decision is that **the screen enters the timeline not whole, but only as what
has appeared on it**. The reason is what a screen really looks like: menus, panels,
tabs and the sidebar do not change for hours. A measurement on a live screen gave 51
meaningful blocks of text, and reading every two seconds that is 1500 repeated lines
per minute. Putting them into the prompt whole means paying for the same thing again
and again and drowning in repeats the one line the whole effort is for: a new
question, a new message, a new hint.

So the timeline stores the difference. The full screen state is always available
separately, if it is needed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from argus.core.events import ScreenEvent, Source, SpeechEvent

NS_PER_SEC = 1_000_000_000


class Kind(str, Enum):
    SPEECH = "speech"
    SCREEN = "screen"
    NOTE = "note"       # the program's own notes: "the screen changed completely"
    REPLY = "reply"     # the assistant's own answer, with the request it was given for


# Limits for the assistant's entries. The timeline is trimmed by the overall budget
# (`render`), but it trims from the start in whole lines: one answer longer than the
# budget would not fit at all, and everything else would vanish from the prompt along
# with it. Three thousand characters is a hundred lines of a formula or a screen of
# code: enough to continue from, and four such answers still fit in twelve thousand.
REPLY_CHARS = 3000
ASK_CHARS = 1500
CUT_MARK = "…(the rest is not shown)"


@dataclass(slots=True)
class Entry:
    """One timeline line."""

    t: int
    kind: Kind
    source: Source
    text: str
    confidence: float | None = None
    # Only for `Kind.REPLY`: what was asked, if the request was typed in the question line.
    # Empty when the answer was to a line from the timeline itself, which is already there.
    ask: str = ""

    @property
    def seconds(self) -> float:
        return self.t / NS_PER_SEC


class ScreenDigest:
    """Turns a stream of screen states into a stream of changes.

    It remembers what was on the screen and returns only the new lines. If almost
    everything changed, the window was switched, and that is worth reporting separately
    instead of dumping a hundred lines as "appeared".
    """

    def __init__(self, full_change_ratio: float = 0.7, max_new_lines: int = 10) -> None:
        self.full_change_ratio = full_change_ratio
        self.max_new_lines = max_new_lines
        self._known: set[str] = set()
        self._last_order: list[str] = []
        # What went into the timeline on the previous read. The interface needs it: without it
        # the panel cannot show what of the read content the model actually saw.
        self.last_new: list[str] = []

        self.updates = 0
        self.full_changes = 0

    def diff(self, event: ScreenEvent) -> tuple[list[str], bool]:
        """Return (new lines, whether the screen changed entirely)."""
        ordered = [
            b.text.strip()
            for b in sorted(event.blocks, key=lambda b: (b.box[1], b.box[0]))
            if b.text.strip()
        ]
        current = set(ordered)
        if not current:
            self.last_new = []
            return [], False

        new = [line for line in ordered if line not in self._known]
        new = self._most_useful(new)
        # The share that disappeared: if almost all of the previous content is gone, this is a
        # different window.
        gone = len(self._known - current) / len(self._known) if self._known else 0.0
        wholesale = bool(self._known) and gone >= self.full_change_ratio

        self._known = current
        self._last_order = ordered
        self.updates += 1
        if wholesale:
            self.full_changes += 1

        self.last_new = new
        return self.last_new, wholesale

    def _most_useful(self, lines: list[str]) -> list[str]:
        """Keep the most substantial lines, not the topmost ones.

        It used to take the first N from the top down, and the browser tab strip took all the
        places. On a live screen that gave "НD Смс X", "ЮDСм X", "XEC X": broken pieces of
        tab titles, three letters each. The real content lay below and never reached the
        timeline, and to such a prompt the model answered "the text is unreadable".

        Length is a rough but honest measure: a fragment is short, a phrase is long.
        We restore the reading order so that the timeline stays readable.
        """
        if len(lines) <= self.max_new_lines:
            return lines
        best = set(sorted(lines, key=len, reverse=True)[: self.max_new_lines])
        return [line for line in lines if line in best]

    @property
    def current_text(self) -> str:
        """The full state of the screen: for when you need the whole picture, not the difference."""
        return "\n".join(self._last_order)

    def reset(self) -> None:
        self._known.clear()
        self._last_order.clear()
        self.last_new = []


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n" + CUT_MARK


@dataclass
class Timeline:
    """A timeline of events with a window by time and by number of lines.

    The window is bounded on two sides, by count and by age. The first protects against a
    talkative interlocutor, the second against a long session, where something said an
    hour ago no longer helps and only takes up room.
    """

    max_entries: int = 400
    window_s: float = 900.0          # 15 minutes

    entries: deque[Entry] = field(default_factory=deque)
    digest: ScreenDigest = field(default_factory=ScreenDigest)
    dropped: int = 0
    _window: str | None = None

    def add_speech(self, event: SpeechEvent) -> Entry:
        entry = Entry(
            t=event.t,
            kind=Kind.SPEECH,
            source=event.source,
            text=event.text.strip(),
            confidence=event.confidence,
        )
        self._append(entry)
        return entry

    def add_screen(self, event: ScreenEvent) -> list[Entry]:
        """Put into the timeline what has appeared on the screen."""
        new_lines, wholesale = self.digest.diff(event)
        made: list[Entry] = []

        # A window change is an important turn in the conversation: the person switched from
        # the call to a task, from the task to the browser. The same line means different things
        # to the model in a terminal and in a chat.
        if event.window and event.window != self._window:
            self._window = event.window
            made.append(Entry(t=event.t, kind=Kind.NOTE, source=Source.SCREEN,
                              text=f"window: {event.window}"))
        elif wholesale:
            made.append(Entry(t=event.t, kind=Kind.NOTE, source=Source.SCREEN,
                              text="the screen changed"))
        if new_lines:
            made.append(Entry(t=event.t, kind=Kind.SCREEN, source=Source.SCREEN,
                              text="\n".join(new_lines)))
        for entry in made:
            self._append(entry)
        return made

    def add_reply(self, t: int, text: str, ask: str = "") -> Entry | None:
        """Put the assistant's own answer into the timeline, together with the request if one
        was typed.

        Without this, every call is the first one in the conversation for the model: it sees
        nothing of what it said itself, and to "make it harder" it answers "I need the
        previous text". The answer goes at the end like any other entry, so the timeline is
        still append-only and the prompt cache stays intact.
        """
        entry = Entry(
            t=t,
            kind=Kind.REPLY,
            source=Source.ASSISTANT,
            text=_clip(text, REPLY_CHARS),
            ask=_clip(ask, ASK_CHARS),
        )
        if not entry.text:
            return None
        self._append(entry)
        return entry

    def _append(self, entry: Entry) -> None:
        if not entry.text:
            return
        self.entries.append(entry)
        self._trim(entry.t)

    def _trim(self, now: int) -> None:
        cutoff = now - int(self.window_s * NS_PER_SEC)
        while self.entries and (
            len(self.entries) > self.max_entries or self.entries[0].t < cutoff
        ):
            self.entries.popleft()
            self.dropped += 1

    def since(self, t: int) -> list[Entry]:
        return [e for e in self.entries if e.t >= t]

    def last_speech(self, source: Source | None = None) -> Entry | None:
        for entry in reversed(self.entries):
            if entry.kind is Kind.SPEECH and (source is None or entry.source is source):
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)
