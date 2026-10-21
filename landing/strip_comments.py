"""Removes comments from the code that is embedded in the landing page.

The app window is embedded in the page as is: together with all the developer notes (about
libraries, internals, somebody's words). They are not visible on screen, but they sit in the
source of the published page. Here they are cut out at build time; the app sources are not
touched.

Only what the parser has positively recognized as a comment is cut: `//` and `/* */` in code
and styles, `<!-- -->` in markup. Strings, template strings and regular expressions are
copied as is. If the parser makes a mistake, it will rather keep a comment than lose code:
cutting starts only where it is sure that it is outside a string and outside a regular
expression. The check is `check_strip.py`.
"""
from __future__ import annotations

import re

# After these characters "/" opens a regular expression rather than dividing.
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_WORD_BEFORE_REGEX = {"return", "typeof", "case", "do", "else", "in", "of", "void", "delete",
                      "throw", "new", "instanceof", "yield", "await"}
_WORD = re.compile(r"[A-Za-z0-9_$]")


class _Js:
    def __init__(self, src: str) -> None:
        self.s = src
        self.n = len(src)
        self.i = 0
        self.out: list[str] = []

    # -- helpers ---------------------------------------------------------

    def _line_is_blank_so_far(self) -> bool:
        """Only whitespace so far on the current output line."""
        for chunk in reversed(self.out):
            k = chunk.rfind("\n")
            if k >= 0:
                if chunk[k + 1:].strip():
                    return False
                return True
            if chunk.strip():
                return False
        return True

    def _trim_blank_tail(self) -> None:
        """Remove the trailing whitespace of the current output line."""
        while self.out:
            chunk = self.out[-1]
            stripped = chunk.rstrip(" \t")
            if stripped == chunk:
                return
            if stripped:
                self.out[-1] = stripped
                return
            self.out.pop()

    def _rest_of_line_is_blank(self, pos: int) -> bool:
        j = self.s.find("\n", pos)
        j = self.n if j < 0 else j
        return not self.s[pos:j].strip()

    # -- parsing ---------------------------------------------------------------

    def code(self, until_brace: bool = False) -> None:
        """Code up to the end of the text (or up to the `}` that closes a `${` in a template)."""
        s, n = self.s, self.n
        prev = ""          # the last significant character
        word = ""          # the last word, if the previous character is part of a word
        depth = 0
        while self.i < n:
            i = self.i
            c = s[i]
            nxt = s[i + 1] if i + 1 < n else ""
            if c == "/" and nxt == "/":
                alone = self._line_is_blank_so_far()
                j = s.find("\n", i)
                j = n if j < 0 else j
                self._trim_blank_tail()
                self.i = j + 1 if alone and j < n else j
                continue
            if c == "/" and nxt == "*":
                j = s.find("*/", i + 2)
                if j < 0:
                    # not closed: we do not take the risk, we leave it as is
                    self.out.append(s[i:])
                    self.i = n
                    return
                end = j + 2
                alone = self._line_is_blank_so_far() and self._rest_of_line_is_blank(end)
                if alone:
                    self._trim_blank_tail()
                    k = s.find("\n", end)
                    self.i = n if k < 0 else k + 1
                else:
                    body = s[i:end]
                    self.out.append("\n" if "\n" in body else " ")
                    self.i = end
                continue
            if c in "'\"":
                self._string(c)
                prev, word = c, ""
                continue
            if c == "`":
                self._template()
                prev, word = "`", ""
                continue
            if c == "/":
                if prev in _REGEX_AFTER or prev == "" or word in _WORD_BEFORE_REGEX:
                    self._regex()
                    prev, word = "/", ""
                else:
                    self.out.append("/")
                    self.i += 1
                    prev, word = "/", ""
                continue
            if until_brace:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0:
                        return
                    depth -= 1
            self.out.append(c)
            self.i += 1
            if c.isspace():
                continue
            if _WORD.match(c):
                word = (word + c) if _WORD.match(prev or " ") else c
            else:
                word = ""
            prev = c

    def _string(self, quote: str) -> None:
        s, n = self.s, self.n
        j = self.i + 1
        while j < n:
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch == quote or ch == "\n":
                j += 1
                break
            j += 1
        self.out.append(s[self.i:j])
        self.i = j

    def _template(self) -> None:
        s, n = self.s, self.n
        self.out.append("`")
        self.i += 1
        while self.i < n:
            ch = s[self.i]
            if ch == "\\":
                self.out.append(s[self.i:self.i + 2])
                self.i += 2
                continue
            if ch == "`":
                self.out.append("`")
                self.i += 1
                return
            if ch == "$" and s[self.i + 1:self.i + 2] == "{":
                self.out.append("${")
                self.i += 2
                self.code(until_brace=True)
                if self.i < n and s[self.i] == "}":
                    self.out.append("}")
                    self.i += 1
                continue
            self.out.append(ch)
            self.i += 1

    def _regex(self) -> None:
        s, n = self.s, self.n
        j = self.i + 1
        in_class = False
        while j < n:
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "\n":
                break
            if in_class:
                if ch == "]":
                    in_class = False
            elif ch == "[":
                in_class = True
            elif ch == "/":
                j += 1
                while j < n and s[j].isalpha():
                    j += 1
                break
            j += 1
        self.out.append(s[self.i:j])
        self.i = j


def strip_js(src: str) -> str:
    p = _Js(src)
    p.code()
    return "".join(p.out)


_CSS_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_css(src: str) -> str:
    """Styles: we remove `/* … */`. Quoted strings with "/*" inside do not occur here; `check_strip.py`
    verifies this.
    """
    out: list[str] = []
    pos = 0
    for m in _CSS_BLOCK.finditer(src):
        start, end = m.span()
        line_start = src.rfind("\n", 0, start) + 1
        line_end = src.find("\n", end)
        line_end = len(src) if line_end < 0 else line_end
        tail_blank = not src[end:line_end].strip()
        if tail_blank and not src[line_start:start].strip():
            # the comment takes the whole line: the line goes away with it
            out.append(src[pos:line_start])
            pos = min(line_end + 1, len(src))
            continue
        head = src[pos:start]
        out.append(head.rstrip(" \t") if tail_blank else head)
        glue = (not tail_blank and head[-1:] not in ("", " ", "\t", "\n")
                and src[end:end + 1] not in ("", " ", "\t", "\n"))
        out.append(" " if glue else "")
        pos = end
    out.append(src[pos:])
    return "".join(out)


_BLOCK = re.compile(r"(<(script|style)\b([^>]*)>)(.*?)(</\2\s*>)", re.DOTALL | re.IGNORECASE)
_HTML_COMMENT = re.compile(r"[ \t]*<!--(?!\[if)(?!legal).*?-->[ \t]*\n?", re.DOTALL)
_JS_TYPES = ("", "text/javascript", "module", "application/javascript")


def strip_html(doc: str) -> str:
    """The whole page: comments in the markup, scripts and styles."""
    out = []
    pos = 0
    for m in _BLOCK.finditer(doc):
        out.append(_HTML_COMMENT.sub("", doc[pos:m.start()]))
        open_tag, name, attrs, body, close_tag = m.groups()
        kind = re.search(r'type\s*=\s*["\']?([^"\'\s>]*)', attrs)
        kind = (kind.group(1).lower() if kind else "")
        if name.lower() == "style":
            body = strip_css(body)
        elif kind in _JS_TYPES:
            body = strip_js(body)
        out.append(open_tag + body + close_tag)
        pos = m.end()
    out.append(_HTML_COMMENT.sub("", doc[pos:]))
    return "".join(out)
