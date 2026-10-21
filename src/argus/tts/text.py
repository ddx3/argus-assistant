"""Reply text -> what can be fed to the synthesizer.

A reply is written for the eyes (the overlay; `md()` on the page renders markdown), so
it has asterisks, bullets, and formulas. The synthesizer reads letters literally, and
"star star" or "dollar dollar" in the sound is worse than a gap. This is minimal
cleanup, not a full markdown parser: exactly what gets in the way of listening, no
more.
"""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
# A fence with no closing: the reply broke off in the middle of code, so everything
# after it is code too.
_OPEN_FENCE = re.compile(r"```.*\Z", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LATEX_BLOCK = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)
_LATEX_INLINE = re.compile(r"\$([^$\n]+)\$")
# `[text](address)` reads the text; `![caption](address)` reads the caption. The
# synthesizer would read the whole address letter by letter, and we would pay for every
# letter.
_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
# A bare address is read as the site only: "https://example.com/a/b?id=1" becomes
# "example.com".
_URL = re.compile(
    r"(?:https?://|www\.)([^\s/<>()\[\]]*[^\s/<>()\[\].,;:!?…])"
    # The path and query do not need to be read. A sentence-ending mark after the address is
    # not part of it.
    r"(?:[/?#][^\s<>()\[\]]*[^\s<>()\[\].,;:!?…]|[/?#])?"
)
# Asterisks are emphasis only if there is no space right after the opening one and right
# before the closing one: "2 * 3 * 4" is multiplication. Underscores count only at word
# boundaries: `my_config_value` is a name.
_STARS = re.compile(r"(\*\*\*|\*\*|\*)(?!\s)(.+?)(?<!\s)\1")
_UNDERSCORES = re.compile(r"(?<!\w)(___|__|_)(?!\s)(.+?)(?<!\s)\1(?!\w)")
_QUOTE = re.compile(r"^[ \t]*>+[ \t]?", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$\n?",
                        re.MULTILINE)
_TABLE_ROW = re.compile(r"^[ \t]*\|(.*?)\|?[ \t]*$", re.MULTILINE)
_HEADER = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# Chunks shorter than this are glued to the next neighbor. Without this, "Yes." "No?"
# "Well." would go out as three separate requests to ElevenLabs: three
# network round-trips for two words each, and the whole gain from splitting into chunks
# (the first sound earlier) would be eaten by overhead on trifles.
_MIN_CHUNK_CHARS = 40


def _table_row(m: re.Match[str]) -> str:
    """A table row "| Who | What |" becomes "Who, What": vertical bars are not read aloud."""
    cells = (cell.strip() for cell in m.group(1).split("|"))
    return ", ".join(cell for cell in cells if cell)


def speakable(text: str, max_chars: int) -> str:
    """Strip markdown and formulas, and truncate to the character budget.

    The truncation is not about the length of the sound (ElevenLabs deals with that
    itself) but about money: synthesis is paid per character, and without a cap a long
    reply goes to voice-over whole, even if the overlay has already cut it by its own
    rules.

    For a growing streamed reply (`tts/reply.py`), this cleanup is not called on the whole
    accumulated text at once but on a chunk of raw text that has already been cut out
    (`take_ready_sentences`). Otherwise it would not be idempotent on a growing prefix: it
    leaves `**bold` alone when the second `**` has not arrived yet, and once the pair
    arrives in the next piece, the whole fragment collapses and shifts everything after
    it, breaking any offset that was counted on the old layout.
    """
    # Code is what we least want to hear literally: reading a code block aloud is
    # pointless, while a short inline fragment is read as it is, without the backticks.
    out = _CODE_FENCE.sub(" код опущен ", text)
    out = _OPEN_FENCE.sub(" код опущен ", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _LATEX_BLOCK.sub(r"\1", out)
    out = _LATEX_INLINE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    out = _URL.sub(r"\1", out)
    out = _TABLE_SEP.sub("", out)
    out = _TABLE_ROW.sub(_table_row, out)
    out = _RULE.sub("", out)
    out = _QUOTE.sub("", out)
    out = _STARS.sub(r"\2", out)
    out = _UNDERSCORES.sub(r"\2", out)
    out = _HEADER.sub("", out)
    out = _BULLET.sub("", out)
    out = _NUMBERED.sub("", out)
    out = _WHITESPACE.sub(" ", out)
    out = _BLANK_LINES.sub("\n\n", out)
    out = out.strip()

    if len(out) <= max_chars:
        return out
    # We cut at a word boundary, not in the middle of a word: the synthesizer would either
    # not pronounce a cut-off word or pronounce it as a scrap of sound.
    cut = out[:max_chars].rsplit(" ", 1)[0]
    return cut or out[:max_chars]


_TERMINAL = re.compile(r"[.!?…]\s+")
_LIST_NUMBER = re.compile(r"(?:^|\n)[ \t]*\d{1,3}[.)]$")


def take_ready_sentences(text: str, *, final: bool) -> tuple[list[str], int]:
    """Split complete sentences off `text` as it grows. This is the version of
    `split_sentences` for a streamed reply that has not fully arrived yet.

    Short fragments are NOT glued to their neighbors here, unlike in `split_sentences`:
    on a stream the next chunk has not arrived, and waiting for it to glue would slow down
    exactly what we cut into chunks as they become ready for in the first place. The price
    is an extra short synthesis request for some "Yes." instead of gluing it to
    the next sentence. A separate network call is cheaper than a second of silence, which
    is what all of this was started to avoid.

    Returns (ready chunks, how many characters of `text` they cover). The remainder,
    `text[length:]`, may still grow with the next piece of text. `final=True` means the
    stream is over, and whatever is left is returned as it is, even without closing
    punctuation.
    """
    pieces: list[str] = []
    pos = 0
    # We do not split code between "```" fences: a period inside it is not the end of a
    # sentence, and half a block sent to synthesis separately would be voiced (and paid for)
    # as code. An odd number of fences means the last one is not closed yet. Everything
    # after it waits for the closing or for the end of the stream, and `speakable` will see
    # the block whole.
    fences = [m.start() for m in re.finditer("```", text)]
    spans = [(fences[i], fences[i + 1] + 3) for i in range(0, len(fences) - 1, 2)]
    if len(fences) % 2:
        spans.append((fences[-1], len(text) + 1))
    for m in _TERMINAL.finditer(text):
        if any(start <= m.start() < end for start, end in spans):
            continue
        # "1. " at the start of a line is a list item number, not the end of a sentence. Without
        # this, "Steps:\n1. Open\n2. Press" would go out in chunks, each with someone else's
        # number at the end.
        if _LIST_NUMBER.search(text[pos:m.start() + 1]):
            continue
        piece = text[pos:m.end()].strip()
        if piece:
            pieces.append(piece)
        pos = m.end()
    if final:
        tail = text[pos:].strip()
        if tail:
            pieces.append(tail)
        pos = len(text)
    return pieces, pos


def split_sentences(text: str) -> list[str]:
    """Already cleaned (`speakable`) text -> chunks for voicing one after another.

    This is not a linguistic parser, just an ordinary split at the end of a sentence. For
    "read the assistant's reply aloud" the accuracy is more than enough: a mistake (a split
    in the wrong place) costs one slightly odd pause, not the meaning. The point of
    splitting is not neatness but the pipeline: the first phrase is voiced and plays while
    the second is still being synthesized, instead of one long wait before the whole reply.
    """
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < _MIN_CHUNK_CHARS:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out
