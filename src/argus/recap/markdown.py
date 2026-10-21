"""Markdown from the model → safe HTML for printing.

The page draws answers with its own small function (JS), while the text gets into the PDF
from here: two parsers for the same markup are deliberate, because the PDF is built on
the server and does not depend on the page. The parsing is deliberately stunted: headings,
bold, italics, code, lists (including nested ones), tables, quotes, paragraphs. The model
almost never writes anything else, and a library for that is extra weight in the build.

Everything that came from the model is escaped **before** our own tags are placed: the
other way around would let foreign text into the page markup.
"""

from __future__ import annotations

import html
import re

_CODE = re.compile(r"`([^`\n]+)`")
# Bold and italics apply only if there is no space right after the opening asterisk and
# before the closing one: "2 * 3 * 4" is multiplication, not italics (the same in `inline`
# of the "Work with the recording" page and in `md` of the work screen). The word for italics is
# Latin letters and digits, like `\w` in JS: Cyrillic next to an asterisk does not count as
# a boundary, so the page and the PDF do not diverge.
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_ITALIC = re.compile(r"(?<![A-Za-z0-9_*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![A-Za-z0-9_*])")
# Heading: hashes, a space and the rest of the line as is. Closing hashes ("## Recap ##")
# are removed by `_heading_text` in code, not by a regular expression: the old
# `(.*?)\s*#*\s*$` on a line with thousands of spaces tried every way to split them
# (cubic time, and the app froze for minutes) and cut "## Language C#" down to
# "Language C".
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_ITEM = re.compile(r"^(\s*)([-*•]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")


# A time reference to an utterance: «(14:32)», «(14:32–14:35)», «(14:05, 14:06)». On the
# page this is a button that leads to the utterance in the recording text; on paper there is
# nothing to link to, and in a report such parentheses are noise.
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
_SPAN = rf"{_TIME}(?:\s*[–—-]\s*{_TIME})?"
_CITES = re.compile(rf"[ \t]*\(\s*{_SPAN}(?:\s*[,;]\s*{_SPAN})*\s*\)")


# Code is a fenced block and `inline`: parentheses with a time inside it
# («print((12:30))») are part of the code, not a reference to an utterance, and must not be
# cut out (on the page they also stay code).
_CODE_PARTS = re.compile(r"(^[ \t]*```.*?^[ \t]*```|`[^`\n]+`)", re.DOTALL | re.MULTILINE)


def strip_timecodes(text: str) -> str:
    """Remove the parenthesized time links from the answer text (for PDF). A time that is itself part
    of the meaning ("we meet at 11:00") is not inside a link bracket and stays; code is not
    touched."""
    parts = _CODE_PARTS.split(text)
    return "".join(part if n % 2 else _CITES.sub("", part) for n, part in enumerate(parts))


def _heading_text(rest: str) -> str:
    """Heading text without the closing hashes: "Summary ##" -> "Summary". Hashes at the end of a word
    ("C# language", "F#") are part of the text; closing ones count only if there is a space before
    them."""
    rest = rest.strip()
    bare = rest.rstrip("#")
    if bare != rest and (not bare or bare[-1].isspace()):
        return bare.rstrip()
    return rest


def inline(text: str) -> str:
    """Bold, italic and code inside a line. The text is escaped first."""
    out = html.escape(text, quote=False)
    # Code is hidden under placeholders before the rest of the parsing: inside it `*` and `**`
    # are just characters.
    kept: list[str] = []

    def hold(m: re.Match[str]) -> str:
        kept.append(f"<code>{m.group(1)}</code>")
        return f"\x01{len(kept) - 1}\x02"

    def back(m: re.Match[str]) -> str:
        n = int(m.group(1))
        return kept[n] if n < len(kept) else m.group(0)

    out = _CODE.sub(hold, out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    return re.sub(r"\x01(\d+)\x02", back, out)


def to_html(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
        elif line.startswith("```"):
            i, block = _fence(lines, i)
            out.append(block)
        elif (m := _HEADING.match(line)):
            # The document heading belongs to the page; the headings of the answer go one step lower.
            level = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{level}>{inline(_heading_text(m.group(2)))}</h{level}>")
            i += 1
        elif _RULE.match(line):
            out.append("<hr>")
            i += 1
        elif line.startswith(">"):
            i, block = _quote(lines, i)
            out.append(block)
        elif "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            i, block = _table(lines, i)
            out.append(block)
        elif _ITEM.match(raw):
            i, block = _list(lines, i)
            out.append(block)
        else:
            i, block = _paragraph(lines, i)
            out.append(block)
    return "\n".join(out)


def _fence(lines: list[str], i: int) -> tuple[int, str]:
    code: list[str] = []
    i += 1
    while i < len(lines) and not lines[i].strip().startswith("```"):
        code.append(lines[i])
        i += 1
    return i + 1, "<pre>" + html.escape("\n".join(code)) + "</pre>"


def _quote(lines: list[str], i: int) -> tuple[int, str]:
    body: list[str] = []
    while i < len(lines) and lines[i].strip().startswith(">"):
        body.append(lines[i].strip().lstrip(">").strip())
        i += 1
    return i, "<blockquote>" + "<br>".join(inline(b) for b in body) + "</blockquote>"


def _cells(row: str) -> list[str]:
    row = row.strip().removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in row.split("|")]


def _table(lines: list[str], i: int) -> tuple[int, str]:
    head = _cells(lines[i])
    i += 2
    rows: list[list[str]] = []
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        rows.append(_cells(lines[i]))
        i += 1
    thead = "".join(f"<th>{inline(c)}</th>" for c in head)
    tbody = "".join(
        "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return i, f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"


_CONTINUED = re.compile(r"^\s{2,}\S")

# A list item: (indent, whether numbered, number, text).
_Item = tuple[int, bool, int, str]


def _breaks(lines: list[str], i: int) -> bool:
    """What ends a paragraph and the continuation of a list item: an empty line, a block of its own,
    a heading, a rule, a quote, a table, a list item. The same conditions as `breaks` in `renderMd`
    of the "Work with the recording" page."""
    raw = lines[i]
    line = raw.strip()
    return bool(
        not line or line.startswith(("```", ">")) or _HEADING.match(line) or _RULE.match(line)
        or _ITEM.match(raw)
        or ("|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()))
    )


def _list(lines: list[str], i: int) -> tuple[int, str]:
    """A list with nesting by indentation: the same parsing as `renderItems` of the "Work with the
    recording" page (on screen and on paper the answer must not look different). An item
    continues with an indented line; an empty line between items does not end the list; an
    item indented less than the first ends the first list but does not disappear."""
    items: list[_Item] = []
    while i < len(lines):
        m = _ITEM.match(lines[i])
        if m:
            numbered = m.group(2)[0].isdigit()
            number = int(m.group(2)[:-1]) if numbered else 0
            items.append((len(m.group(1).expandtabs(4)), numbered, number, m.group(3)))
            i += 1
            continue
        if items and lines[i].strip() and _CONTINUED.match(lines[i]) and not _breaks(lines, i):
            indent, numbered, number, text = items[-1]
            items[-1] = (indent, numbered, number, text + "\n" + lines[i].strip())
            i += 1
            continue
        if not lines[i].strip():
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _ITEM.match(lines[j]):
                i = j
                continue
        break
    parts: list[str] = []
    pos = 0
    while pos < len(items):
        piece, pos = _render_items(items, pos, items[pos][0])
        parts.append(piece)
    return i, "".join(parts)


def _render_items(items: list[_Item], pos: int, indent: int) -> tuple[str, int]:
    """One list with nested ones. Another marker type at the same level ("-" after "1.") is already a
    different list; numbering that starts from something other than one begins with that number
    (`<ol start>`)."""
    first = items[pos]
    tag = "ol" if first[1] else "ul"
    parts = [f'<ol start="{first[2]}">' if tag == "ol" and first[2] != 1 else f"<{tag}>"]
    while pos < len(items) and items[pos][0] >= indent:
        level, numbered, _, text = items[pos]
        if level == indent and numbered != first[1]:
            break
        pos += 1
        nested = ""
        if pos < len(items) and items[pos][0] > level:
            nested, pos = _render_items(items, pos, items[pos][0])
        body = "<br>".join(inline(line) for line in text.split("\n"))
        parts.append(f"<li>{body}{nested}</li>")
    parts.append(f"</{tag}>")
    return "".join(parts), pos


def _paragraph(lines: list[str], i: int) -> tuple[int, str]:
    body: list[str] = []
    while i < len(lines) and (not body or not _breaks(lines, i)):
        body.append(lines[i].strip())
        i += 1
    return i, "<p>" + "<br>".join(inline(b) for b in body) + "</p>"
