"""Page scripts must parse.

The breakage this was written for cost an hour of searching and looked not like an error
but like a blank page. In `admin.js` the backslashes were lost when the file was written,
and `join("
")` turned into a string literal torn by a real line break. The browser could not parse
the file as a whole, so not a single function ran, and it could be seen only in the
developer console, which the app does not have.

There is no full JavaScript parsing here and none is needed. One thing is checked: whether
a string literal is torn by a line break. That is exactly how code breaks when it is
written through a shell that eats backslashes, and there is almost no other way to get
this error.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src" / "argus"

# After these characters a slash starts a regular expression, not a division.
# Without this rule `/[&<>"]/g` is read as a string and gives a false alarm.
BEFORE_REGEX = set("(,=:[!&|?{};\n+*%~^<>") | {"return", "typeof", "case", "in", "of"}


def unterminated_strings(code: str) -> list[tuple[int, str]]:
    """String literals cut off by the end of the line."""
    bad: list[tuple[int, str]] = []
    _scan(code, 0, 1, bad, nested=False)
    return bad


def _scan(code: str, i: int, line: int, bad: list[tuple[int, str]], nested: bool) -> tuple[int, int]:
    """Walk the code from `i` and collect torn strings into `bad`.

    With `nested` it is the inside of a `${...}` of a template literal: it stops at the brace that
    closes it. Templates are written inside templates all the time (`${ok ? `...` : `...`}`), and
    an apostrophe in the text of the inner one (English text has them) must not be read as the
    start of a string. Returns the position and the line number where the walk ended.
    """
    depth = 0
    prev = "\n"                      # the start of the part behaves like a line break
    n = len(code)
    while i < n:
        ch = code[i]

        if ch == "\n":
            line += 1
            prev = "\n"
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue

        if ch == "/" and i + 1 < n and code[i + 1] == "/":
            while i < n and code[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and code[i + 1] == "*":
            end = code.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += code.count("\n", i, end)
            i = end
            prev = "/"
            continue
        if ch == "/" and (prev in BEFORE_REGEX or _word_before(code, i) in BEFORE_REGEX):
            i = _skip_regex(code, i)
            prev = "/"
            continue

        if ch == "`":
            i, line = _skip_template(code, i, line, bad)
            prev = "`"
            continue

        if ch in "\"'":
            start_line = line
            j = i + 1
            while j < n and code[j] != ch:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == "\n":
                    text = code.splitlines()[start_line - 1].strip()
                    bad.append((start_line, text[:90]))
                    break
                j += 1
            i = j + 1
            prev = ch
            continue

        if nested and ch in "{}":
            if ch == "}" and depth == 0:
                return i + 1, line
            depth += 1 if ch == "{" else -1
        prev = ch
        i += 1
    return i, line


def _word_before(code: str, i: int) -> str:
    head = code[:i].rstrip()
    match = re.search(r"[A-Za-z_$]+$", head)
    return match.group(0) if match else ""


def _skip_regex(code: str, i: int) -> int:
    j = i + 1
    in_class = False
    while j < len(code):
        c = code[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            return j + 1
        elif c == "\n":
            return j          # not a regular expression: let it be parsed further
        j += 1
    return j


def _skip_template(code: str, i: int, line: int, bad: list[tuple[int, str]]) -> tuple[int, int]:
    j = i + 1
    while j < len(code):
        if code[j] == "\\":
            j += 2
            continue
        if code[j] == "\n":
            line += 1
        elif code[j] == "`":
            return j + 1, line
        elif code[j] == "$" and code[j + 1:j + 2] == "{":
            # An expression inside the template is code again, with strings and templates of its own.
            j, line = _scan(code, j + 2, line, bad, nested=True)
            continue
        j += 1
    return j, line


def scripts() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.js") if "__pycache__" not in p.parts)


def inline_scripts(html: Path) -> list[tuple[int, str]]:
    """`<script>` fragments directly in the markup: the very same thing breaks them."""
    text = html.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.DOTALL):
        line = text.count("\n", 0, m.start()) + 1
        out.append((line, m.group(1)))
    return out


@pytest.mark.parametrize("path", scripts(), ids=lambda p: p.name)
def test_script_has_no_string_broken_by_a_newline(path: Path):
    bad = unterminated_strings(path.read_text(encoding="utf-8"))
    assert not bad, f"{path.name}: a string is cut off by a line break: " + "; ".join(
        f"line {n}: {t}" for n, t in bad
    )


def test_framework_touches_only_its_own_rows():
    """A `.f` row is drawn by both the framework and the channel block: there is one grid on the
    page.

    But the error node and the settings path exist only on rows drawn by the framework itself,
    inside `.fields`. Walking over all the `.f` rows in a row once already broke saving: it
    stumbled on a row of the audio block and never reached the end, so the edit went to the
    server while the "not saved" bar stayed hanging, and it looked like
    "saving does not work".
    """
    js = (ROOT / "ui" / "static" / "admin.js").read_text(encoding="utf-8")
    assert '$$(".f")' not in js, "the framework walks over all the rows of the page, not only its own"


@pytest.mark.parametrize(
    "path",
    sorted(ROOT.rglob("*.html")),
    ids=lambda p: p.name,
)
def test_inline_script_has_no_string_broken_by_a_newline(path: Path):
    for offset, code in inline_scripts(path):
        bad = unterminated_strings(code)
        assert not bad, f"{path.name}: near line {offset + bad[0][0] - 1}: {bad[0][1]}"


@pytest.mark.parametrize("name", ["index.html", "home.html", "record.html"])
def test_dashboard_styles_bare_input_by_type(name):
    """`input[type=...]` without a class or an `id` hits the whole page, not only its own field.

    Every page is checked into which `chrome.js` inserts the "Account" tab with a
    password field: the Recording screen had such a selector, and the eye that switches the
    password to visible text repainted the field (audit 2026-10-14, css-design-9).

    That is exactly how the password field in the account panel shrank: the question line was
    styled with a bare `input[type=text]`, while the account panel is the same page and the
    same document, and at the moment the eye switched the password to visible text, a foreign
    rule with a fixed width and its own padding overrode the password field's rule, which had
    equal weight but a later place in the file. From the outside it looked like "the field
    twitches when you click the eye".
    """
    page = (ROOT / "ui" / "static" / name).read_text(encoding="utf-8")
    css = re.search(r"<style>(.*?)</style>", page, re.S)
    assert css, f"{name} has no <style> of its own"
    without_comments = re.sub(r"/\*.*?\*/", "", css.group(1), flags=re.S)
    # Each declaration block is its own group of comma-separated selectors. A bare one is bad
    # exactly when ONE of them, without any wrapping (`.ask `, `#id ` before it), is by itself
    # equal to `input[type=...]`.
    bare = [
        sel.strip()
        for block in without_comments.split("{")[:-1]
        for sel in block.rsplit("}", 1)[-1].split(",")
        if re.fullmatch(r"input\[type=\w+\]", sel.strip())
    ]
    assert not bare, f"bare selector by field type: {bare}; narrow it to an #id or a .class"


def test_the_record_search_field_uses_the_shared_field_tokens():
    """The text search on the "Recording" page is one of the app's fields: size, corner,
    edge, and fill come from the shared `--field-*` tokens (chrome.css), not from its own
    numbers (audit of 2026-10-14, css-design-9: it had 7px, `--field`, and `--line`, and the
    field stood out from the rest). The same rule for the sign-in fields, the dialog, and the
    search on the home page is checked in `test_flat_windows.py`; it did not reach this page."""
    page = (ROOT / "ui" / "static" / "record.html").read_text(encoding="utf-8")
    css = re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)
    style = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    # The rule of the field itself, not `.s-open #find` (that one sets only the width and the
    # margins around it).
    rule = re.search(r"(?:^|\})\s*#find\s*\{([^{}]*)\}", style)
    assert rule, "no rule for #find"
    for token in ("--field-h", "--field-r", "--field-edge", "--field-fill"):
        assert f"var({token})" in rule.group(1), f"the search field has no {token}"
