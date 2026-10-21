""""Copy answer" puts the formatted answer on the clipboard, not its
markdown.

Complaint (2026-10-08): "instead of bold text, ** ** gets copied". Both copy buttons, on
the working screen and on the "Recording" screen, wrote the answer text to the
clipboard as it came from the model, so `**Event loop**` with asterisks, `` `await` ``
with backticks and ```` ```python ```` above the code went into Word, mail and
messengers.

Now the clipboard gets two forms of the same answer (`argusCopy` in `chrome.js`): HTML
with real bold, lists, a table and code, for programs that understand it, and plain text
without markup for the rest (Notepad, an input line). Both are derived from the HTML the
page has already drawn, so they match what the person saw.

Separately, every code block in an answer has its own "Copy code" icon
(2026-10-08, requirement: a separate copy button for code, so that only the code can be
copied). It copies just the code, also in two forms: as is (editor, terminal) and as a
`<pre><code>` block (counting on Word, Slack and Notion to paste it as a code block), and
it does not get into the copy of the whole answer itself.

The parsing runs over a real DOM, and a bare `node` does not have one, so the parsing is
run in Microsoft Edge with no window (`--headless=new --dump-dom`, like the PDF in
`recap/pdf.py`): the page is assembled from the very functions that live in `index.html`,
`record.html` and `chrome.js`. If there is no Edge, these checks are skipped. The
clipboard write is checked with a stub clipboard: what exactly goes in, and in which
forms. The real permissions, the focus and the fallback path without a window and a
human gesture cannot be tested, so they were checked live.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from argus.recap.pdf import find_edge
from argus.ui.server import STATIC

needs_edge = pytest.mark.skipif(find_edge() is None, reason="needs Microsoft Edge to parse the answer in a real DOM")


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _braced(src: str, start: int) -> str:
    """The text from `start` to the brace that pairs the first `{` after it (braces are counted
    in the text).
    """
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
    """`const name = …;` up to the first semicolon at the end of a line."""
    m = re.search(rf"const {name} = .*?;\n", src, re.DOTALL)
    assert m, f"no const {name}"
    return m.group(0)


# ---------- page wiring: fast, without a browser ----------

@pytest.mark.parametrize("page", ["index.html", "record.html"])
def test_no_page_copies_the_raw_markdown_of_an_answer_any_more(page: str):
    """Raw markdown in the clipboard was precisely the breakage: asterisks instead of bold."""
    text = _text(page)
    assert "clipboard.writeText(" not in text, "the page writes to the clipboard directly, bypassing argusCopy"
    assert "copyText(" not in text


def test_the_helper_sits_in_the_script_both_answer_pages_load():
    chrome = _text("chrome.js")
    assert "window.argusCopy" in chrome
    for page in ("index.html", "record.html"):
        assert '<script src="/chrome.js" defer></script>' in _text(page), page


def test_the_helper_does_not_depend_on_the_settings_window_being_built():
    """The settings window in `chrome.js` has early exits (embedded view, a page without a
    settings button). Copying must not live in it — it is needed where they are absent too."""
    chrome = _text("chrome.js")
    head = chrome[: chrome.index("window.argusCopy")]
    assert head.count("\n})();") == 1, "copying sits in its own function, after the settings window"


def test_a_formula_carries_its_tex_so_plain_text_can_keep_it():
    """MathML will not give back TeX; without it, a formula would go into plain text as
    glued-together symbols.
    """
    assert 'data-tex="${esc(tex.trim())}"' in _text("index.html")


def test_the_code_icon_is_the_same_on_both_pages_and_has_its_style_in_the_shared_sheet():
    """The icon is one for both pages ("the buttons must be the same"): the markup is duplicated
    in two parsers, while the look and behavior live in the shared `chrome.css` and
    `chrome.js`."""
    assert _const(_text("index.html"), "CODE_COPY") == _const(_text("record.html"), "CODE_COPY")
    css = _text("chrome.css")
    assert ".codeblock > .codecopy" in css
    assert ".iconbtn.copied" in css and ".iconbtn.failed" in css and "svg.done" in css
    for page in ("index.html", "record.html"):
        assert '<link rel="stylesheet" href="/chrome.css">' in _text(page), page


def test_the_copy_buttons_have_no_hover_tooltip():
    """Requirement (2026-10-08): remove them altogether — the interface has no `title`
    attribute, the name for the screen reader is in `aria-label`, and there is no
    `element.title = …` in scripts either."""
    for page in ("index.html", "record.html"):
        code_icon = _const(_text(page), "CODE_COPY")
        assert "title=" not in code_icon and "aria-label=" in code_icon, page
    assert not re.search(r"\.title\s*=", _text("chrome.js"))


def test_each_answer_copy_button_hands_the_rendered_answer_and_itself_to_argus_copy():
    """The button hands over what is drawn (`md`, `renderMd`), not the raw text of the answer,
    and passes itself to `argusCopy`, so that its icon confirms the result."""
    work, record = _text("index.html"), _text("record.html")
    assert "argusCopy(md(a.text), btn)" in work
    assert "argusCopy(renderMd(result.text), btn)" in record


def test_nothing_turns_green_on_copying_any_more():
    """Requirement (2026-10-08): everywhere on copying, turn the button into a check mark rather
    than making it green, just a white check mark. No rule about `.copied` may have a green
    `--ok`."""
    for name in ("chrome.css", "index.html", "record.html"):
        text = _text(name)
        for rule in re.findall(r"[^{}]*\.copied[^{]*\{[^}]*\}", text):
            assert "--ok" not in rule, f"{name}: {rule.strip()}"


# ---------- parsing in a real DOM ----------

# A stub clipboard and three actions on the page: a click on the code icon, copying the
# whole answer. The script is asynchronous but gets by on microtasks alone (the stub
# answers immediately), so it writes the result before the `load` event that `--dump-dom`
# waits for.
HELPERS = r"""
const writes = [];
let broken = false;
window.ClipboardItem = class { constructor(kinds) { this.kinds = kinds; } };
window.Blob = class { constructor(parts, opts) { this.body = parts.join(""); this.type = opts.type; } };
Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
  write: async (items) => {
    if (broken) throw new DOMException("denied", "NotAllowedError");
    writes.push(Object.fromEntries(Object.entries(items[0].kinds).map(([k, b]) => [k, b.body])));
  },
} });
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

// What is visible on the button: the classes, how many checkmarks, which of the icons is
// shown and in what color (the styles are real, from chrome.css), and whether the icon
// itself is visible.
function marks(btn) {
  const icon = btn.querySelector(":scope > svg:not(.done)");
  const check = btn.querySelector(":scope > svg.done");
  const css = (el) => (el ? getComputedStyle(el) : null);
  return {
    classes: btn.className,
    checks: btn.querySelectorAll(":scope > svg.done").length,
    opacity: css(btn).opacity,
    background: css(btn).backgroundColor + "|" + css(btn).backgroundImage,
    iconDisplay: css(icon).display, iconStroke: css(icon).stroke,
    checkDisplay: check ? css(check).display : null, checkStroke: check ? css(check).stroke : null,
  };
}

function stage(html) {
  const box = document.createElement("div");
  box.innerHTML = html;
  document.body.append(box);
  return box;
}

async function clickCode(rendered, n, fail) {
  const box = stage(rendered);
  const btn = box.querySelectorAll(".codecopy")[n];
  const rest = marks(btn);
  writes.length = 0;
  broken = !!fail;
  btn.click();
  await settle();
  const res = { writes: writes.slice(), rest, after: marks(btn) };
  broken = false;
  box.remove();
  return res;
}

// The copy-answer button (as on the work screen and on the recording screen): the answer
// is copied through `argusCopy`, and the button is passed to it so that the icon confirms
// the result.
async function flashAnswer(rendered, fail, twice) {
  const box = stage('<button type="button" class="iconbtn act copy"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/></svg></button>');
  const btn = box.firstChild;
  writes.length = 0;
  broken = !!fail;
  const ok = await argusCopy(rendered, btn);
  if (twice) await argusCopy(rendered, btn);
  const res = { ok, writes: writes.slice(), after: marks(btn) };
  broken = false;
  box.remove();
  return res;
}

async function copyAll(rendered) {
  writes.length = 0;
  const ok = await argusCopy(rendered);
  return { ok, writes: writes.slice() };
}
"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css}">
<style>
:root {{ --text:#f2f4f7; --dim:#9aa1ad; --ok:#5fd68a; --bad:#f0716b; --bg:#0a0c10; --btn-hover:rgba(255,255,255,.09); }}
/* No transitions: the icon color changes over 0.12 s, and right after the class changes
   the computed color is still the old one. */
*, *::before, *::after {{ transition:none !important; }}
</style>
</head><body>
<pre id="out">pending</pre>
<script src="{temml}"></script>
<script src="{chrome}"></script>
<script>
const work = (() => {{
{work}
return md;
}})();
const record = (() => {{
{record}
return renderMd;
}})();
{helpers}
const cases = {cases};
(async () => {{
  let out;
  try {{
    out = [];
    for (const c of cases) {{
      const rendered = (c.page === "record" ? record : work)(c.md);
      const item = Object.assign({{ rendered }}, argusCopy.parts(rendered));
      if (c.click !== undefined) item.clicked = await clickCode(rendered, c.click, c.fail);
      if (c.flash) item.flashed = await flashAnswer(rendered, c.fail, c.twice);
      if (c.all) item.copied = await copyAll(rendered);
      out.push(item);
    }}
  }} catch (e) {{
    out = {{ error: String(e && e.stack || e) }};
  }}
  document.getElementById("out").textContent = JSON.stringify(out);
}})();
</script></body></html>
"""


def _run(cases: list[dict], tmp_path: Path) -> list[dict]:
    index, rec = _text("index.html"), _text("record.html")
    work = "\n".join([
        _const(index, "esc"), _const(index, "BLOCK_PH"), _const(index, "BLOCK_ANY"), _const(index, "CODE_COPY"),
        _function(index, "renderMath"), _function(index, "mdInline"), _function(index, "mdParagraph"),
        _function(index, "mdItems"), _function(index, "mdBlocks"), _function(index, "md"),
    ])
    # The parsing section on the "Recording" screen is taken whole, from the heading
    # to `api`: next to `renderMd` it holds its constants and helpers (`CODE_COPY`, `TIME`,
    # `CITES`...), and a list by names would lag behind the page.
    record = "\n".join([
        _const(rec, "esc"),
        rec[rec.index("// ---------- markdown parsing") : rec.index("async function api(")],
    ])
    page = tmp_path / "copy.html"
    page.write_text(PAGE.format(
        temml=(STATIC / "vendor/temml/temml.min.js").as_uri(),
        chrome=(STATIC / "chrome.js").as_uri(), css=(STATIC / "chrome.css").as_uri(),
        work=work, record=record, helpers=HELPERS,
        cases=json.dumps(cases, ensure_ascii=False).replace("</", "<\\/"),
    ), encoding="utf-8")
    done = subprocess.run(
        [str(find_edge()), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'profile'}",
         "--dump-dom", page.as_uri()],
        capture_output=True, timeout=90, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    shutil.rmtree(tmp_path / "profile", ignore_errors=True)
    dom = done.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.DOTALL)
    assert m, f"Edge did not return the page:\n{dom[:500]}\n{done.stderr.decode('utf-8', 'replace')[:500]}"
    result = json.loads(html.unescape(m.group(1)))
    assert not isinstance(result, dict), result["error"]
    return result


EVENT_LOOP = (
    "**Event loop** — a scheduler: it keeps a queue of ready coroutines, runs one until the nearest `await`, "
    "then switches to the next one.\n\n"
    "Example:\n\n"
    "```python\nasync def main():\n    await asyncio.gather(fetch(1), fetch(2))\n```"
)


@needs_edge
def test_the_reported_answer_keeps_bold_as_bold_and_leaves_no_markdown_in_plain_text(tmp_path: Path):
    """The very answer from the complaint, on both pages."""
    work, record = _run([{"page": "work", "md": EVENT_LOOP}, {"page": "record", "md": EVENT_LOOP}], tmp_path)
    for got in (work, record):
        assert "<b>Event loop</b>" in got["html"], "bold stayed bold"
        assert "<code>await</code>" in got["html"]
        assert "<pre><code>async def main():\n    await asyncio.gather(fetch(1), fetch(2))</code></pre>" in got["html"], \
            "the code is a `<pre><code>` block, like the \"Copy code\" icon"
        assert got["text"] == (
            "Event loop — a scheduler: it keeps a queue of ready coroutines, runs one until the nearest await, "
            "then switches to the next one.\n\n"
            "Example:\n\n"
            "async def main():\n    await asyncio.gather(fetch(1), fetch(2))"
        )
        assert "*" not in got["text"] and "`" not in got["text"]
        assert "<button" not in got["html"] and "<div" not in got["html"], "the code icon does not get into the copy of the answer"


@needs_edge
def test_nothing_of_the_page_style_travels_to_the_clipboard(tmp_path: Path):
    """Page classes on a dark theme would give white on white in Word; styles and scripts even
    more so.
    """
    got, = _run([{"page": "work", "md": "## Summary\n\n**important** and *not*\n\n- one\n- two"}], tmp_path)
    assert "class=" not in got["html"] and "data-" not in got["html"] and "style=" not in got["html"]
    assert "<script" not in got["html"]


@needs_edge
def test_a_heading_of_the_work_screen_is_its_own_paragraph_without_hashes(tmp_path: Path):
    got, = _run([{"page": "work", "md": "## Summary\nshort text"}], tmp_path)
    assert "<p><b>Summary</b></p>" in got["html"]
    assert got["text"] == "Summary\n\nshort text"


@needs_edge
def test_lists_keep_their_markers_in_plain_text_and_their_tags_in_html(tmp_path: Path):
    md = "What next:\n- first\n- second\n\nStep by step:\n1. one\n2. two"
    got, = _run([{"page": "work", "md": md}], tmp_path)
    assert "<ul><li>first</li><li>second</li></ul>" in got["html"]
    assert "<ol><li>one</li><li>two</li></ol>" in got["html"]
    assert got["text"] == "What next:\n\n- first\n- second\n\nStep by step:\n\n1. one\n2. two"


@needs_edge
def test_nested_lists_indent_two_spaces_per_level(tmp_path: Path):
    got, = _run([{"page": "record", "md": "- one\n  - nested\n  - more\n- two\n  1. step"}], tmp_path)
    assert got["text"] == "- one\n  - nested\n  - more\n- two\n  1. step"


@needs_edge
def test_a_table_is_a_real_table_in_html_and_tab_separated_in_plain_text(tmp_path: Path):
    md = "| Who | What |\n| --- | --- |\n| Ann | **contract** |\n| Boris | estimate |"
    got, = _run([{"page": "record", "md": md}], tmp_path)
    assert '<table border="1"' in got["html"] and "<th>Who</th>" in got["html"]
    assert "<td><b>contract</b></td>" in got["html"]
    assert got["text"] == "Who\tWhat\nAnn\tcontract\nBoris\testimate"


@needs_edge
def test_a_quote_and_a_time_reference_of_the_record_page_read_as_written(tmp_path: Path):
    """The time in an answer on the "Recording" page is a link-button to an utterance; in
    the clipboard it is "(14:32)" again.
    """
    got, = _run([{"page": "record", "md": "> \"we take June\" (14:32)\n\nThe deadline was named at 14:40 (14:41)."}], tmp_path)
    assert got["rendered"].count('class="cite"') == 2
    assert "<button" not in got["html"] and "(14:32)" in got["html"]
    assert got["text"] == "> \"we take June\" (14:32)\n\nThe deadline was named at 14:40 (14:41)."


@needs_edge
def test_a_list_and_a_range_of_times_read_as_the_model_wrote_them(tmp_path: Path):
    """On the "Recording" page "(15:01, 15:09)" is two buttons in one wrapper, and
    "(15:06–15:07)" is one: in the clipboard it is again one pair of parentheses, as in the
    text of the answer, not "(15:01) (15:09)"."""
    md = "They argued twice (15:01, 15:09), then for a long time (15:06–15:07)."
    got, = _run([{"page": "record", "md": md}], tmp_path)
    assert got["rendered"].count('class="cite"') == 3
    assert "<button" not in got["html"]
    assert got["text"] == md and "(15:01, 15:09)" in got["html"]


@needs_edge
def test_formulas_stay_formulas_in_plain_text(tmp_path: Path):
    """Previously the clipboard got the source `\\(…\\)`; glued-together MathML ("ab" instead of
    a fraction) would be a step back.
    """
    md = "A fraction \\(\\frac{a}{b}\\) in a line.\n\n$$x^2 + y^2 = z^2$$"
    got, = _run([{"page": "work", "md": md}], tmp_path)
    assert "<math" in got["html"], "in HTML the formula stays a formula"
    assert got["text"] == "A fraction \\(\\frac{a}{b}\\) in a line.\n\n$$x^2 + y^2 = z^2$$"


@needs_edge
def test_text_of_the_model_that_looks_like_markup_is_copied_as_text(tmp_path: Path):
    """The page escapes foreign text, and copying must not bring it to life."""
    got, = _run([{"page": "work", "md": 'A tag <img src=x onerror=alert(1)> and <b>not bold</b> & 2 < 3'}], tmp_path)
    assert "<img" not in got["html"] and "<b>not bold</b>" not in got["html"]
    assert got["text"] == 'A tag <img src=x onerror=alert(1)> and <b>not bold</b> & 2 < 3'


@needs_edge
def test_an_empty_answer_has_nothing_to_copy(tmp_path: Path):
    got, = _run([{"page": "work", "md": ""}], tmp_path)
    assert got["text"] == ""


TWO_BLOCKS = (
    "First, text with **bold**.\n\n"
    "```python\nfirst = 1\nprint(first)\n```\n\n"
    "Then the second block:\n\n"
    "```sh\nsecond --flag\n```"
)


@needs_edge
def test_every_code_block_has_its_own_copy_icon_right_after_the_code(tmp_path: Path):
    work, record = _run([{"page": "work", "md": TWO_BLOCKS}, {"page": "record", "md": TWO_BLOCKS}], tmp_path)
    for got in (work, record):
        rendered = got["rendered"]
        assert rendered.count('<div class="codeblock">') == 2 and rendered.count("codecopy") == 2
        for code in ("first = 1\nprint(first)", "second --flag"):
            assert re.search(
                rf'<div class="codeblock"><pre[^>]*>{re.escape(code)}</pre><button type="button" class="iconbtn act codecopy"',
                rendered,
            ), code


@needs_edge
def test_the_code_icon_is_always_shown_and_has_no_background_until_hovered(tmp_path: Path):
    """Requirement (2026-10-08): always show the copy button, and remove the background — it
    should appear only on hover. The styles are real, there is no mouse over the block: the
    icon is in place, and the background is transparent."""
    got = _run([{"page": page, "md": TWO_BLOCKS, "click": 0} for page in ("work", "record")], tmp_path)
    for item in got:
        rest = item["clicked"]["rest"]
        assert rest["opacity"] == "1" and rest["iconDisplay"] != "none"
        assert rest["background"] == "rgba(0, 0, 0, 0)|none"
        assert rest["checks"] == 0 and rest["classes"] == "iconbtn act codecopy"


def test_the_code_icon_has_no_background_rules_of_its_own():
    """The icon's fill is only the shared one, `.iconbtn:hover` (it also fades in smoothly): the
    code block's own was already a solid `--bg`, and it was ordered to be removed."""
    rules = re.findall(r"\.codeblock[^{}]*\.codecopy[^{}]*\{([^}]*)\}", _text("chrome.css"))
    assert rules, "no code icon rules found in chrome.css"
    assert all("background" not in body for body in rules)


@needs_edge
def test_the_code_icon_copies_only_that_code_both_as_it_is_and_as_a_code_block(tmp_path: Path):
    """Not the whole answer and not with a fence: exactly the code of its own block goes to the
    clipboard. Plain text is the code itself (for an editor, a terminal), HTML is a
    `<pre><code>` block (counting on pasting as a code block into Word, Slack, Notion)."""
    cases = [{"page": page, "md": TWO_BLOCKS, "click": n} for page in ("work", "record") for n in (0, 1)]
    got = _run(cases, tmp_path)
    for item, code in zip(got, ["first = 1\nprint(first)", "second --flag"] * 2):
        assert item["clicked"]["writes"] == [{"text/plain": code, "text/html": f"<pre><code>{code}</code></pre>"}]
        assert "copied" in item["clicked"]["after"]["classes"]


@needs_edge
def test_the_code_block_of_the_clipboard_escapes_what_looks_like_markup(tmp_path: Path):
    """Code with `<`, `>` and `&` is escaped in the HTML form, and in plain text stays as
    written.
    """
    md = '```python\nif a < b and c > d & e:\n    print("<b>")\n```'
    got = _run([{"page": page, "md": md, "click": 0} for page in ("work", "record")], tmp_path)
    for item in got:
        write, = item["clicked"]["writes"]
        assert write["text/plain"] == 'if a < b and c > d & e:\n    print("<b>")'
        assert write["text/html"] == '<pre><code>if a &lt; b and c &gt; d &amp; e:\n    print("&lt;b&gt;")</code></pre>'


WHITE = "rgb(242, 244, 247)"   # `--text` in the test page; the green `--ok` there is rgb(95, 214, 138)
RED = "rgb(240, 113, 107)"     # `--bad`


@needs_edge
def test_a_copy_turns_the_icon_into_a_white_check_not_a_green_one(tmp_path: Path):
    """Requirement (2026-10-08): everywhere on copying, turn the button into a check mark rather
    than making it green, just a white check mark. The same for the code icon (both pages)
    and for the "Copy answer" button."""
    cases = [{"page": page, "md": TWO_BLOCKS, "click": 0} for page in ("work", "record")]
    cases.append({"page": "work", "md": EVENT_LOOP, "flash": True})
    got = _run(cases, tmp_path)
    for after in [got[0]["clicked"]["after"], got[1]["clicked"]["after"], got[2]["flashed"]["after"]]:
        assert "copied" in after["classes"] and "failed" not in after["classes"]
        assert after["checks"] == 1
        assert after["iconDisplay"] == "none", "the former icon is hidden"
        assert after["checkDisplay"] == "block", "a check mark in its place"
        assert after["checkStroke"] == WHITE, "white, not green"
    assert got[2]["flashed"]["ok"] is True


@needs_edge
def test_a_second_copy_in_a_row_does_not_stack_check_marks(tmp_path: Path):
    got, = _run([{"page": "work", "md": EVENT_LOOP, "flash": True, "twice": True}], tmp_path)
    assert got["flashed"]["after"]["checks"] == 1
    assert "copied" in got["flashed"]["after"]["classes"]


@needs_edge
def test_a_copy_that_could_not_write_turns_the_icon_red_and_shows_no_check(tmp_path: Path):
    """A silent icon looks broken, and the interface has no hover hints — so the icon turns red."""
    got = _run([
        {"page": "work", "md": TWO_BLOCKS, "click": 0, "fail": True},
        {"page": "work", "md": EVENT_LOOP, "flash": True, "fail": True},
    ], tmp_path)
    code, answer = got[0]["clicked"], got[1]["flashed"]
    assert code["writes"] == [] and answer["writes"] == [] and answer["ok"] is False
    for after in (code["after"], answer["after"]):
        assert "failed" in after["classes"] and "copied" not in after["classes"]
        assert after["checks"] == 0 and after["checkDisplay"] is None
        assert after["iconDisplay"] != "none" and after["iconStroke"] == RED


@needs_edge
def test_copying_the_whole_answer_writes_both_kinds_and_leaves_the_code_icon_out(tmp_path: Path):
    got, = _run([{"page": "work", "md": EVENT_LOOP, "all": True}], tmp_path)
    assert got["copied"]["ok"] is True
    write, = got["copied"]["writes"]
    assert set(write) == {"text/html", "text/plain"}
    assert "<b>Event loop</b>" in write["text/html"] and "<button" not in write["text/html"]
    assert "*" not in write["text/plain"] and "codecopy" not in write["text/plain"]
