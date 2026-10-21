"""Rendering the timeline into text for the model.

One property of the format decides everything else: **the timeline is only ever
appended to at the end**. This is exactly the shape prompt caching needs: the model
does not recompute a stable beginning, and we pay only for what is new. Hence the
rules: do not change lines already written, do not reorder them, do not reword them.

Trimming old content is the only thing that breaks the cache, so it is done rarely
and in big chunks, not line by line.
"""

from __future__ import annotations

from argus.context.timeline import Entry, Kind, Timeline
from argus.core.events import Source

# Speaker names in the prompt. Short and unambiguous: the model must not guess who "ME"
# is, and must not mix up the roles if the word "I" appears in the text.
SPEAKER = {
    Source.ME: "Me",
    Source.THEM: "Them",
    Source.SCREEN: "Screen",
    Source.ASSISTANT: "Assistant",
}

# A request typed in the question line. This is not "Me", which is what the person
# said aloud: they are addressing the assistant itself, and the prompt says so directly.
ASK_LABEL = "Me → assistant"

# A rough estimate: this many characters per token in Russian text. An exact count is
# not needed here; it only has to keep us from overflowing the window.
CHARS_PER_TOKEN = 3.0


def timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _said(stamp: str, who: str, text: str) -> str:
    """A timeline line: on one line, and multiline text is indented under the label."""
    lines = text.split("\n")
    if len(lines) == 1:
        return f"[{stamp}] {who}: {lines[0]}"
    body = "\n".join(f"    {line}" for line in lines)
    return f"[{stamp}] {who}:\n{body}"


def render_entry(entry: Entry) -> str:
    stamp = timestamp(entry.seconds)
    if entry.kind is Kind.NOTE:
        return f"[{stamp}] ({entry.text})"
    if entry.kind is Kind.SCREEN:
        return _said(stamp, "Screen", entry.text)
    if entry.kind is Kind.REPLY:
        # A request and its answer are one exchange: under one timestamp and right next to each
        # other, so it is clear what exactly the answer was given to.
        reply = _said(stamp, SPEAKER[entry.source], entry.text)
        return f"{_said(stamp, ASK_LABEL, entry.ask)}\n{reply}" if entry.ask else reply
    return f"[{stamp}] {SPEAKER[entry.source]}: {entry.text}"


def render(timeline: Timeline, max_chars: int = 12_000) -> str:
    """Render the timeline into text within the budget.

    We trim from the start, not from the end: fresh content matters more. If something
    was dropped, we say so honestly with a line, otherwise the model would decide that the
    conversation began mid-sentence and start completing what was never said.
    """
    lines = [render_entry(e) for e in timeline]
    if not lines:
        return ""

    total = sum(len(line) + 1 for line in lines)
    if total <= max_chars:
        return "\n".join(lines)

    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    hidden = len(lines) - len(kept)
    return "\n".join([f"(earlier there were {hidden} more entries, they did not fit)", *kept])


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def render_context(timeline: Timeline, screen_now: str = "", max_chars: int = 12_000) -> str:
    """The full context for the model: the timeline plus the current state of the screen.

    The whole screen goes as a separate piece and **at the end**: it is rewritten on every
    call and, if it stood at the beginning, it would break the cache of the whole timeline.
    """
    parts = ["# What happened", render(timeline, max_chars)]
    if screen_now:
        parts += ["", "# What is on the screen now", screen_now]
    return "\n".join(parts)
