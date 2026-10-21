"""Markdown parsing in an answer (`md` in `index.html`) without a browser: through `node`.

The `test_answer_markdown.py` tests run the same parsing in a real DOM through Edge, and
Edge on this machine returns empty pages (`docs/audit-2026-10-14.md`, fe-index-a-6): none
of their checks ran, and breakages of the parsing were pinned by nothing. `md` works on
strings; from the environment it needs only `window.temml` (Temml without a DOM returns
MathML as a string), so the functions are taken from the page by the same cutouts and run
in node, and the resulting HTML string is checked. Edge is left only for those checks
that need a real DOM.

What is pinned down: one check per breakage found by the audit (a backslash in code, a
fence language with `+`/`#`/`-`, code under a list item, a hash at the end of a heading,
formulas inside code, `$'` in a formula, Cyrillic next to an underscore, a link without a
hover hint) and a few checks that the old behavior stayed in place.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import subprocess

import pytest

from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the page JS"
)

FENCE = "`" * 3


def _page() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _braced(src: str, start: int) -> str:
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("no closing brace")


def _function(src: str, name: str) -> str:
    return _braced(src, src.index(f"function {name}("))


def _const(src: str, name: str) -> str:
    m = re.search(rf"const {name} = .*?;\n", src, re.DOTALL)
    assert m, f"no const {name}"
    return m.group(0)


def _md(cases: list[str]) -> list[str]:
    """What the page's `md` returns for each text."""
    index = _page()
    consts = "\n".join(_const(index, n) for n in ("esc", "BLOCK_PH", "BLOCK_ANY", "CODE_COPY"))
    funcs = "\n".join(
        _function(index, n)
        for n in ("renderMath", "mdInline", "mdParagraph", "mdItems", "mdBlocks", "md")
    )
    temml = (STATIC / "vendor/temml/temml.min.js").read_text(encoding="utf-8")
    script = (
        "const window = { temml: (function () {\n" + temml + "\nreturn temml; })() };\n"
        + consts + "\n" + funcs + "\n"
        + f"const cases = {json.dumps(cases)};\n"
        + "console.log(JSON.stringify(cases.map((c) => md(c))));\n"
    )
    done = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _codes(html: str) -> list[str]:
    """Texts of the code blocks from the finished HTML, as a person will see and copy them."""
    return [
        html_lib.unescape(m)
        for m in re.findall(r'<pre class="code">(.*?)</pre>', html, re.DOTALL)
    ]


# --------------------------------------------------------------------------
# Code and the backslash (fe-index-a-1)
# --------------------------------------------------------------------------


@needs_node
def test_a_backslash_inside_inline_code_stays_and_a_path_ending_in_one_does_not_break_the_span():
    """`\\.` was shown as `.`, and for `C:\\Users\\` the closing quote was read as escaped."""
    a, b, c = _md([
        "match a dot with `\\.` and use `^\\d+\\.\\s*$`",
        "open `C:\\Users\\`",
        "\\*asterisk\\* and \\`quote\\`",
    ])
    assert "<code>\\.</code>" in a and "<code>^\\d+\\.\\s*$</code>" in a
    assert "<code>C:\\Users\\</code>" in b and "`" not in b
    # outside code the backslash still escapes markup
    assert "*asterisk*" in c and "<i>" not in c and "`quote`" in c


# --------------------------------------------------------------------------
# Code blocks (fe-index-a-2, fe-index-a-3)
# --------------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize("info", ["c++", "c#", "objective-c", "python", "", "js title=x"])
def test_the_language_of_a_fence_never_lands_in_the_code(info):
    code = "#include <x>\nint main() { return 0; }"
    (got,) = _md([f"Here:\n{FENCE}{info}\n{code}\n{FENCE}\ndone"])
    assert _codes(got) == [code]
    assert FENCE not in got


@needs_node
def test_a_fence_under_a_list_item_gives_code_without_the_list_indent():
    """Copying code from a list item put the indent into the clipboard: in a Python file that is
    an IndentationError.
    """
    text = "\n".join([
        "1. Install:", f"   {FENCE}python", "   def f():", "       return 1", f"   {FENCE}", "2. Run it",
    ])
    (got,) = _md([text])
    assert _codes(got) == ["def f():\n    return 1"]
    assert '<ol start="2">' in got and "Run it" in got      # the numbering of the second piece did not go wrong


@needs_node
def test_a_fence_in_the_middle_of_a_line_and_an_empty_fence_still_make_blocks():
    a, b, c = _md([
        f"Here is the code: {FENCE}python\nprint(1)\n{FENCE}",
        f"{FENCE}\n{FENCE}",
        f"{FENCE}js\nlet a = 1;\n{FENCE}\n\ntext after",
    ])
    assert _codes(a) == ["print(1)"]
    assert _codes(b) == [""]
    assert _codes(c) == ["let a = 1;"] and "text after" in c


@needs_node
def test_two_blocks_and_the_text_between_them_stay_apart():
    (got,) = _md([f"{FENCE}sh\nls\n{FENCE}\nbetween\n{FENCE}sh\npwd\n{FENCE}"])
    assert _codes(got) == ["ls", "pwd"] and "between" in got


@needs_node
def test_an_unfinished_fence_while_streaming_is_plain_text_not_a_broken_page():
    (got,) = _md([f"Here is the code:\n{FENCE}python\ndef f():"])
    assert "<pre" not in got and "def f():" in got and "<script" not in got


# --------------------------------------------------------------------------
# Headings (fe-index-a-8)
# --------------------------------------------------------------------------


@needs_node
def test_a_heading_keeps_a_hash_that_belongs_to_the_text_and_loses_a_closing_one():
    a, b, c, d = _md(["## Implementation in C#", "### The F# language", "## Section ##", "# Summary #  "])
    assert '<b class="h">Implementation in C#</b>' in a
    assert '<b class="h">The F# language</b>' in b
    assert '<b class="h">Section</b>' in c
    assert '<b class="h">Summary</b>' in d


# --------------------------------------------------------------------------
# Formulas (fe-index-a-9, fe-index-a-10)
# --------------------------------------------------------------------------


@needs_node
def test_math_delimiters_inside_inline_code_are_code_not_formulas():
    a, b, c = _md([
        "In bash `$$` is the PID, and `$$` also works in Makefiles",
        "People write `\\(x^2\\)` in code",
        "And here is a formula $$E = mc^2$$ and \\(a+b\\) too",
    ])
    assert "<math" not in a and a.count("<code>$$</code>") == 2
    assert "<math" not in b and "<code>\\(x^2\\)</code>" in b
    assert c.count("<math") == 2                            # real formulas are still formulas


@needs_node
def test_dollar_signs_in_a_formula_do_not_splice_page_markup_into_the_attribute():
    r"""`$'` and `$` plus a backtick in a replacement string mean pieces of the source text: they
    occur in a real formula (`\text{$x$'s value}`) and inserted foreign markup into the
    attribute."""
    a, b = _md([r"\(\text{$x$'s value}\)", r"\(\text{$x$`s value}\)"])
    assert a.count("<math") == 1 and r'data-tex="\text{$x$' + "'s value}" + '"' in a
    assert b.count("<math") == 1 and r'data-tex="\text{$x$`s value}"' in b
    for got in (a, b):
        assert got.count("data-tex=") == 1                  # the attribute is not duplicated by a foreign piece


@needs_node
def test_a_price_in_dollars_is_still_not_a_formula():
    (got,) = _md(["It costs $5 or $10, as we agree"])
    assert "<math" not in got and "$5" in got and "$10" in got


# --------------------------------------------------------------------------
# Italics and underscore (fe-index-a-12)
# --------------------------------------------------------------------------


@needs_node
def test_underscores_inside_a_cyrillic_word_are_not_emphasis():
    # The inputs stay Cyrillic on purpose: the subject is how `md` treats an underscore next to
    # non-Latin letters.
    a, b, c, d = _md([
        "в файле имя_файла_конфига лежит ключ",
        "snake_case_word и __init__py",
        "это _курсив_ и __жирный__ текст",
        "(_скобки_) и «_кавычки_»",
    ])
    assert "имя_файла_конфига" in a and "<i>" not in a
    assert "snake_case_word" in b and "<i>" not in b
    assert "<i>курсив</i>" in c and "<b>жирный</b>" in c
    assert "<i>скобки</i>" in d and "<i>кавычки</i>" in d


# --------------------------------------------------------------------------
# Links (css-design-15, fe-index-a-11, fe-index-b-10, fe-pages-16)
# --------------------------------------------------------------------------


@needs_node
def test_a_link_shows_where_it_points_as_text_and_has_no_hover_tooltip():
    """The app has no hover tooltips (2026-10-08), and the link is not clickable: the address is
    visible as text.
    """
    a, b, c, d = _md([
        "See [React docs](https://react.dev/learn?x=1) there",
        "The site [react.dev](https://react.dev/learn) there",
        "Write to [us](mailto:team@example.com)",
        "Not a link [x](javascript:alert(1))",
    ])
    assert "title=" not in a and 'class="lnk"' in a
    assert "React docs" in a and "(react.dev)" in a
    assert "(react.dev)" not in b and "react.dev" in b       # the label already names the address
    assert "team@example.com" in c and "title=" not in c
    assert 'class="lnk"' not in d and "<a " not in d          # a foreign scheme is just text


@needs_node
def test_the_text_of_a_link_comes_out_whole_in_the_plain_text_copy():
    """For plain text the copy takes the text of the node: the label and the address side by side,
    not the label alone.
    """
    (got,) = _md(["[Docs](https://example.com/a)"])
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", got))
    assert text == "Docs (example.com)"


# --------------------------------------------------------------------------
# The old behavior stayed in place
# --------------------------------------------------------------------------


def _scenarios():
    """Scenarios of the test session (`docs/make_format_test_chat.py`): the same texts the person
    will see in the app.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs" / "make_format_test_chat.py"
    spec = importlib.util.spec_from_file_location("make_format_test_chat", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_node
def test_no_scenario_of_the_test_session_leaves_raw_markup_on_the_screen():
    """The same as the check in Edge (`test_answer_markdown.py`), but without a browser: no
    asterisks, no table dashes, no hash marks, no code fences and no formula dollars in the
    visible text of any scenario."""
    mk = _scenarios()
    got = _md([text for _title, _cause, text, _extra in mk.SCENARIOS])
    assert len(got) == len(mk.SCENARIOS) >= 12
    for (title, *_), html in zip(mk.SCENARIOS, got, strict=True):
        visible = html_lib.unescape(re.sub(r"<[^>]+>", "", html))
        for raw in ("**", "~~", "|---", "|:--", mk.FENCE, "$$", "\\(", "## ", "- [", "--- |"):
            assert raw not in visible, f"{title!r}: {raw!r} is left on the screen"
        assert "<script" not in html and "<img" not in html and "<a " not in html, title
    by_title = {title: html for (title, *_), html in zip(mk.SCENARIOS, got, strict=True)}
    wide = next(h for t, h in by_title.items() if t.startswith("wide table"))
    assert (wide.count("<table>"), len(re.findall(r"<th[ >]", wide)), len(re.findall(r"<td[ >]", wide))) == (1, 9, 27)
    lists = next(h for t, h in by_title.items() if t.startswith("nested"))
    assert lists.count("<ol>") == 1 and lists.count('<li class="task">') == 4
    formulas = next(h for t, h in by_title.items() if t.startswith("formulas"))
    assert formulas.count("<math") == 4 and "$5" in formulas and "$10" in formulas


@needs_node
def test_the_usual_answer_still_comes_out_as_before():
    table = "| a | b |\n|:--|--:|\n| **x** | `y` |"
    a, b, c, d, e = _md([
        "**bold**, *italic*, ~~struck~~ and `code`",
        table,
        "- one\n- two\n  - nested\n\n1. first\n2. second",
        "> quote\n\n---\n\ntext",
        "",
    ])
    assert "<b>bold</b>" in a and "<i>italic</i>" in a and "<del>struck</del>" in a
    assert "<code>code</code>" in a
    assert "<table>" in b and "text-align:left" in b and "text-align:right" in b
    assert "<b>x</b>" in b and "<code>y</code>" in b
    assert c.count("<ul>") == 2 and "<ol>" in c
    assert "<blockquote>" in d and "<hr>" in d
    assert e == ""


@needs_node
def test_model_text_never_becomes_page_markup():
    (got,) = _md(["<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1))"])
    assert "<script" not in got and "<img" not in got
    assert "&lt;script&gt;" in got
