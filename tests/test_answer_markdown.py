"""Parsing of markdown in the answer on the working screen (`md` in `index.html`).

Requirement (2026-10-08): tables are not rendered properly either; all kinds of answers
must be handled correctly and shown. The earlier parser knew bold, italic, flat lists
and code; tables, quotes, nested lists, the rule, strikethrough and links came out as
raw text. Now everything the model writes is parsed.

Parsing runs on a real DOM, so it is run in headless Microsoft Edge (`--dump-dom`), as in
`test_copy_answer.py`: the page is assembled from the very functions that live in
`index.html`. Without Edge the checks are skipped."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from argus.recap.pdf import find_edge
from argus.ui.server import STATIC

needs_edge = pytest.mark.skipif(find_edge() is None, reason="needs Microsoft Edge to parse the answer in a real DOM")

FENCE = "`" * 3
PAGE = """<!doctype html><html><head><meta charset="utf-8"><script src="{temml}"></script></head><body>
<pre id="out"></pre>
<script>
{functions}
const cases = {cases};
document.getElementById("out").textContent = JSON.stringify(cases.map((text) => {{
  const box = document.createElement("div");
  box.innerHTML = md(text);
  const q = (sel) => box.querySelectorAll(sel).length;
  return {{
    html: box.innerHTML, text: box.textContent,
    h: q("b.h"), p: q("p"), table: q("table"), th: q("th"), td: q("td"), tr: q("tr"), quote: q("blockquote"),
    ol: q("ol"), ul: q("ul"), nested: q("li > ul, li > ol"), li: q("li"), task: q("li.task"), hr: q("hr"),
    del: q("del"), lnk: q("span.lnk"), code: q("code"), pre: q("pre.code"), math: q("math"), b: q("b:not(.h)"),
    i: q("i"), a: q("a"), script: q("script"), img: q("img"),
    align: [...box.querySelectorAll("tbody tr:first-child td")].map((c) => c.style.textAlign),
    olStart: (box.querySelector("ol") || {{}}).start ?? null,
  }};
}}));
</script></body></html>"""


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


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


def _md(cases: list[str], tmp_path: Path) -> list[dict]:
    index = _text("index.html")
    functions = "\n".join([
        _const(index, "esc"), _const(index, "BLOCK_PH"), _const(index, "BLOCK_ANY"), _const(index, "CODE_COPY"),
        _function(index, "renderMath"), _function(index, "mdInline"), _function(index, "mdParagraph"),
        _function(index, "mdItems"), _function(index, "mdBlocks"), _function(index, "md"),
    ])
    page = tmp_path / "md.html"
    page.write_text(PAGE.format(
        temml=(STATIC / "vendor/temml/temml.min.js").as_uri(), functions=functions,
        cases=json.dumps(cases, ensure_ascii=False).replace("</", "<\\/"),
    ), encoding="utf-8")
    done = subprocess.run(
        [str(find_edge()), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'profile'}",
         "--dump-dom", page.as_uri()],
        capture_output=True, timeout=90, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # An Edge profile takes 100-200 MB per run, and pytest keeps the directories of the three
    # last runs: on drive C: that is noticeable (on 2026-10-08 it was nearly full). We clean up at
    # once.
    import shutil
    shutil.rmtree(tmp_path / "profile", ignore_errors=True)
    dom = done.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.DOTALL)
    assert m, dom[-400:]
    import html as _html
    return json.loads(_html.unescape(m.group(1)))


TABLE = "\n".join([
    "| Approach | Pros | Cons |",
    "|:--|:--:|--:|",
    "| Threads | `fast` | **races** |",
    "| Processes | isolation | expensive |",
])


@needs_edge
def test_a_table_becomes_a_table_with_aligned_columns_and_formatted_cells(tmp_path):
    (got,) = _md([TABLE], tmp_path)
    assert (got["table"], got["th"], got["td"], got["tr"]) == (1, 3, 6, 3)
    assert got["align"] == ["left", "center", "right"]
    assert got["code"] == 1 and got["b"] == 1                # markup inside the cells is parsed
    assert "|" not in got["text"] and "--" not in got["text"]


@needs_edge
def test_a_table_without_edge_pipes_and_with_short_rows_is_still_a_table(tmp_path):
    (got,) = _md(["Approach | Pros\n--- | ---\nthreads | fast\nprocesses |"], tmp_path)
    assert (got["table"], got["th"], got["td"]) == (1, 2, 4)  # a short row is padded with an empty cell


@needs_edge
def test_a_table_cell_may_hold_an_escaped_pipe(tmp_path):
    (got,) = _md(["| a | b |\n|---|---|\n| x \\| y | z |"], tmp_path)
    assert got["td"] == 2 and "x | y" in got["text"]


@needs_edge
def test_every_block_kind_a_model_writes_is_recognised(tmp_path):
    text = "\n".join([
        "## Heading", "", "A paragraph of two", "lines.", "", "> quote", "> second", "", "---", "",
        "1. first", "   - nested", "2. second", "", "- [x] done", "- [ ] not done", "",
        FENCE + "python", "def f(): return 1", FENCE, "", "$$E = mc^2$$",
    ])
    (got,) = _md([text], tmp_path)
    assert got["h"] == 1 and got["quote"] == 1 and got["hr"] == 1
    assert got["ol"] == 1 and got["nested"] == 1 and got["task"] == 2
    assert got["pre"] == 1 and got["math"] == 1
    for raw in ("##", "> ", "---", "- [", FENCE, "$$"):
        assert raw not in got["text"], raw


@needs_edge
def test_inline_markup_is_applied_and_left_alone_where_it_is_only_characters(tmp_path):
    (got,) = _md(["**bold**, *italic*, ***both***, ~~struck~~, `code`, [link](https://example.com), "
                  "snake_case_word, 2*3*4 and \\*asterisk\\*."], tmp_path)
    assert got["b"] == 2 and got["i"] == 2 and got["del"] == 1 and got["code"] == 1 and got["lnk"] == 1
    assert "snake_case_word" in got["text"] and "2*3*4" in got["text"] and "*asterisk*" in got["text"]
    assert got["a"] == 0                                      # the link leads nowhere: the app window is frameless


@needs_edge
def test_model_text_never_becomes_page_markup(tmp_path):
    (got,) = _md(["<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1))\n\n"
                  "| <b>a</b> |\n|---|\n| <i>b</i> |"], tmp_path)
    assert got["script"] == 0 and got["img"] == 0
    assert "<script>" in got["text"] and "<b>a</b>" in got["text"]      # shown as text


@needs_edge
def test_lists_keep_their_numbering_and_split_on_a_change_of_kind(tmp_path):
    (a, b) = _md(["3. third\n4. fourth", "- dot\n1. number"], tmp_path)
    assert a["olStart"] == 3 and a["li"] == 2
    assert b["ul"] == 1 and b["ol"] == 1                      # a different marker kind on the same level is a different list


@needs_edge
def test_a_list_straight_after_a_bold_line_and_a_loose_list_stay_whole(tmp_path):
    (a, b) = _md(["**What next:**\n- first\n- second", "- first\n\n- second\n\n- third"], tmp_path)
    assert a["ul"] == 1 and a["li"] == 2 and a["b"] == 1
    assert b["ul"] == 1 and b["li"] == 3                       # empty lines between items do not break the list


@needs_edge
def test_an_unfinished_code_fence_while_streaming_does_not_break_the_page(tmp_path):
    (got,) = _md(["Here is the code:\n" + FENCE + "python\ndef f():"], tmp_path)
    assert got["p"] >= 1 and got["script"] == 0               # until the block is closed, it is ordinary text


def test_the_answer_cards_have_styles_for_all_of_it():
    html = _text("index.html")
    for rule in (".io.output .tablewrap", ".io.output table", ".io.output th, .io.output td", ".io.output blockquote",
                 ".io.output hr", ".io.output del", ".io.output .lnk", ".io.output li.task"):
        assert rule in html, rule
    # copying an answer does not drag the table wrapper along
    assert 'doc.querySelectorAll("div.tablewrap")' in _text("chrome.js")


def test_an_answer_table_has_a_rounded_frame_in_the_app_style():
    """Requirement (2026-10-08): tables should have rounded corners, in the style of the app.
    A corner cannot be rounded with `border-collapse:collapse`, so the frame and the corner
    belong to the wrapper, and between the cells there are thin lines without an outer
    outline."""
    html = _text("index.html")
    assert "border:1px solid var(--line2);\n                          border-radius:8px; width:fit-content; max-width:100%; }" in html
    assert ".io.output table { border-collapse:separate; border-spacing:0;" in html
    assert "overflow-x:auto; overflow-y:hidden;" in html            # sideways yes, there is no vertical scrollbar
    assert ".io.output th:last-child, .io.output td:last-child { border-right:0; }" in html
    assert ".io.output tbody tr:last-child td { border-bottom:0; }" in html


def _scenarios():
    """Scenarios of the test session (`docs/make_format_test_chat.py`): the same texts the
    person will see in the app.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "docs" / "make_format_test_chat.py"
    spec = importlib.util.spec_from_file_location("make_format_test_chat", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_edge
def test_no_scenario_of_the_test_session_leaves_raw_markup_on_the_screen(tmp_path):
    """Requirement (2026-10-08): provide a test chat for the different scenarios. Every answer
    of the test session goes through the real page parser: no asterisks, no table dashes, no
    hash signs, no code fences and no formula dollar signs are left on the screen."""
    mk = _scenarios()
    got = _md([text for _title, _cause, text, _extra in mk.SCENARIOS], tmp_path)
    assert len(got) == len(mk.SCENARIOS) >= 12
    for (title, *_), g in zip(mk.SCENARIOS, got):
        for raw in ("**", "~~", "|---", "|:--", mk.FENCE, "$$", "\\(", "## ", "- [", "--- |"):
            assert raw not in g["text"], f'"{title}": {raw!r} is left on the screen'
        assert g["script"] == 0 and g["img"] == 0 and g["a"] == 0, title
    by_title = {title: g for (title, *_), g in zip(mk.SCENARIOS, got)}
    wide = next(g for t, g in by_title.items() if t.startswith("wide table"))
    assert (wide["table"], wide["th"], wide["td"]) == (1, 9, 27)
    lists = next(g for t, g in by_title.items() if t.startswith("nested"))
    assert lists["ol"] == 1 and lists["nested"] >= 3 and lists["task"] == 4
    formulas = next(g for t, g in by_title.items() if t.startswith("formulas"))
    assert formulas["math"] == 4 and "$5" in formulas["text"] and "$10" in formulas["text"]


def test_the_test_session_builder_makes_a_session_the_app_can_read(tmp_path):
    from argus.chats.store import ChatStore

    mk = _scenarios()
    folder = mk.build(tmp_path / "chats")
    store = ChatStore(tmp_path / "chats")
    chat = store.get(folder.name)
    assert chat is not None and chat.name == mk.NAME
    rows = store.history(folder.name)
    assert sum(1 for r in rows if r.get("k") == "a") == len(mk.SCENARIOS)
    assert sum(1 for r in rows if r.get("k") == "s") == len(mk.SPEECH)
    # a repeated run recreates the session instead of spawning copies
    again = mk.build(tmp_path / "chats")
    assert [c.name for c in store.list()].count(mk.NAME) == 1 and again.name != folder.name
