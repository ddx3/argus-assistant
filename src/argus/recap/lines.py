"""The conversation as text: what a recap is assembled from and how it is cut into parts.

Where the utterances come from. The live timeline (`context/timeline.py`) remembers only
the last ~15 minutes and 400 entries: that is enough for a real-time answer, but not for
a recap. The whole conversation lies in the log of the open chat
(`chats/<id>/history.jsonl`, entries `{"k": "s", "ts", "src", "text"}`), and the recap
takes the utterances from there. If there is no open chat, the timeline remains.

Time in the text is wall-clock time, hours and minutes. One chat spans several
recognition sessions and even several days, and "at minute 12" means nothing there, while
"at 14:32" does.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo

from argus.context.render import SPEAKER
from argus.context.timeline import Kind
from argus.core.events import Source

# The pause after which a separator line appears in the text. It matters for the model to
# know that the recording was interrupted here: after a break it could be a different
# conversation.
PAUSE_S = 600

# How many characters everything except the utterance itself takes: "[14:32] Them: "
# and the line break. We take it with a margin, not tightly.
LABEL_CHARS = 24

# A separator line ("— pause 25 min —", "— 2026-10-06 —") also with a
# margin.
SEPARATOR_CHARS = 24


@dataclass(frozen=True, slots=True)
class Line:
    """One utterance. `ts` is wall-clock time in seconds (`time.time`)."""

    ts: float
    source: Source
    text: str


def from_history(records: Iterable[dict]) -> list[Line]:
    """Utterances from the chat log. Anything that is not an utterance or is truncated is skipped: a
    summary must not fail because of one damaged line."""
    out: list[Line] = []
    for record in records:
        if not isinstance(record, dict) or record.get("k") != "s":
            continue
        ts, text = record.get("ts"), record.get("text")
        if isinstance(ts, bool) or not isinstance(ts, int | float) or not isinstance(text, str):
            continue
        try:
            source = Source(record.get("src"))
        except ValueError:
            continue
        text = " ".join(text.split())
        if text and source is not Source.SCREEN:
            out.append(Line(float(ts), source, text))
    # The log is written in order of arrival, so the order is already by time; the sort is
    # stable and changes nothing until the timestamps diverge.
    out.sort(key=lambda line: line.ts)
    return out


def from_timeline(entries: Iterable, clock) -> list[Line]:
    """Utterances from the live timeline, for when no chat is open. The timeline's time counts from
    the pipeline start; the clock converts it to wall-clock time."""
    out: list[Line] = []
    for entry in entries:
        if entry.kind is not Kind.SPEECH or entry.source is Source.SCREEN:
            continue
        text = " ".join(entry.text.split())
        if text:
            out.append(Line(clock.to_wall(entry.t).timestamp(), entry.source, text))
    return out


def render(lines: Iterable[Line], tz: tzinfo | None = None) -> str:
    """The transcript for the model: `[14:32] Me: …`, with a separator between pauses and
    between days.
    """
    out: list[str] = []
    day = None
    prev: float | None = None
    for line in lines:
        moment = datetime.fromtimestamp(line.ts, tz)
        if prev is not None and line.ts - prev >= PAUSE_S:
            out.append(f"— pause {round((line.ts - prev) / 60)} min —")
        if moment.date() != day:
            day = moment.date()
            out.append(f"— {day.isoformat()} —")
        out.append(f"[{moment:%H:%M}] {SPEAKER[line.source]}: {line.text}")
        prev = line.ts
    return "\n".join(out)


def chunks(lines: Iterable[Line], budget: int, tz: tzinfo | None = None) -> list[list[Line]]:
    """Cut into parts in order, each of which, after `render`, is no longer than `budget`
    characters. It cuts at utterance boundaries, not in the middle: an utterance is the unit
    of meaning, and from recognition it is no longer than a few sentences anyway.

    One utterance longer than the budget (this does not happen, but the request must not fail
    because of it) is cut off with an ellipsis.
    """
    parts: list[list[Line]] = []
    current: list[Line] = []
    used = 0
    # The longest utterance that still fits alone in its part: together with the label and the
    # date at the start of the part.
    room = max(budget - LABEL_CHARS - SEPARATOR_CHARS, 2)
    for line in lines:
        if len(line.text) > room:
            line = replace(line, text=line.text[: room - 1] + "…")
        cost = len(line.text) + LABEL_CHARS
        if current:
            prev = current[-1]
            # In `render` a separator appears on a pause and on a change of day; and a date at the
            # start of every part. We count both so that the part fits.
            if line.ts - prev.ts >= PAUSE_S:
                cost += SEPARATOR_CHARS
            if _day(line.ts, tz) != _day(prev.ts, tz):
                cost += SEPARATOR_CHARS
            if used + cost > budget:
                parts.append(current)
                current, used = [], 0
        if not current:
            cost = len(line.text) + LABEL_CHARS + SEPARATOR_CHARS
        current.append(line)
        used += cost
    if current:
        parts.append(current)
    return parts


def _day(ts: float, tz: tzinfo | None):
    return datetime.fromtimestamp(ts, tz).date()
