"""Markdown parsing on the "Recording" page (`renderMd` in `record.html`) without a
browser: through `node`.

Before the 2026-10-14 audit no check executed this page's JS (in `test_record.py` there
are substrings of its text), and silent parsing bugs lived for years: a numbered list
after a blank line restarted from 1 (fe-pages-2), "2 * 3 * 4" turned into italics and was
lost (fe-pages-6), items with a smaller indent disappeared (fe-pages-7), and a time in
brackets inside code turned into a button and ate the brackets (fe-pages-14).

`recap/markdown.py` does the same in the PDF: it is a second parse of the same markup,
written separately on purpose. So that they do not drift apart again, the same samples
are run through both, and the result must match letter for letter.

`renderMd` works on strings: the functions are taken from the page by the same cut-outs
as in `test_index_markdown_node.py` and are executed in node. With no node, the checks
are skipped.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from argus.recap import markdown
from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the JS of the page")

FENCE = "`" * 3


def _page() -> str:
    return (STATIC / "record.html").read_text(encoding="utf-8")


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


def _render(cases: list[str]) -> list[str]:
    """What the page's `renderMd` returns for each text."""
    page = _page()
    # The whole parsing section, from the heading to `api`, as in `test_copy_answer.py`: next
    # to `renderMd` it holds its constants and helpers, and a list by names would lag behind the
    # page at the first edit.
    section = page[page.index("// ---------- markdown parsing") : page.index("async function api(")]
    script = (
        _const(page, "esc") + "\n" + section + "\n"
        + f"const cases = {json.dumps(cases)};\n"
        + "console.log(JSON.stringify(cases.map((c) => renderMd(c))));\n"
    )
    done = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _pdf(text: str) -> str:
    """The same as the page, but through the PDF parser; blocks are separated by a newline, which we
    remove.
    """
    return markdown.to_html(text).replace("\n", "")


# --------------------------------------------------------------------------
# Lists (fe-pages-2, fe-pages-7)
# --------------------------------------------------------------------------

STEPS = "1. First\n\n2. Second\n\n3. Third"


@needs_node
def test_a_numbered_list_with_blank_lines_between_steps_stays_one_list():
    """The model puts a blank line between steps: three lists of one item each were numbered "1."
    every time.
    """
    (got,) = _render([STEPS])
    assert got == "<ol><li>First</li><li>Second</li><li>Third</li></ol>"
    assert _pdf(STEPS) == got


@needs_node
def test_an_indented_line_continues_the_step_above_it():
    text = "1. Step one\n   explanation of the step\n2. Step two"
    (got,) = _render([text])
    assert got == "<ol><li>Step one<br>explanation of the step</li><li>Step two</li></ol>"
    assert _pdf(text) == got


@needs_node
def test_a_list_that_starts_from_another_number_keeps_it():
    (got,) = _render(["3. third\n4. fourth"])
    assert got.startswith('<ol start="3">')
    assert _pdf("3. third\n4. fourth") == got


@needs_node
def test_items_less_indented_than_the_first_one_are_not_lost():
    """` - a` followed by `- b`, `- c`: only `a` stayed in the list, the rest vanished silently."""
    text = " - a\n- b\n- c"
    (got,) = _render([text])
    for item in ("<li>a</li>", "<li>b</li>", "<li>c</li>"):
        assert item in got
    assert _pdf(text) == got


@needs_node
def test_another_kind_of_marker_on_the_same_level_is_another_list():
    text = "1. one\n2. two\n- dot"
    (got,) = _render([text])
    assert got == "<ol><li>one</li><li>two</li></ol><ul><li>dot</li></ul>"
    assert _pdf(text) == got


# --------------------------------------------------------------------------
# Asterisks (fe-pages-6)
# --------------------------------------------------------------------------


@needs_node
def test_multiplication_stars_stay_and_real_emphasis_still_works():
    plain, big_o, emphasis = _render([
        "Total 2 * 3 * 4 = 24 hours", "O(n * m), and the second one has O(n * k)", "this is *important* and **very** important",
    ])
    assert plain == "<p>Total 2 * 3 * 4 = 24 hours</p>"
    assert big_o == "<p>O(n * m), and the second one has O(n * k)</p>"
    assert emphasis == "<p>this is <i>important</i> and <b>very</b> important</p>"


@needs_node
def test_stars_inside_inline_code_are_not_emphasis():
    text = "`a ** b ** c` and `*args` and `*kwargs*`"
    (got,) = _render([text])
    assert got == "<p><code>a ** b ** c</code> and <code>*args</code> and <code>*kwargs*</code></p>"
    assert _pdf(text) == got


# --------------------------------------------------------------------------
# Identical on the page and in the PDF
# --------------------------------------------------------------------------

SAMPLES = [
    STEPS,
    "1. Step one\n   explanation of the step\n2. Step two",
    "- one\n\n- two\n- three\n\nthen a paragraph",
    "- a\n  - nested\n  - more\n- b",
    "1) bracket\n2) two",
    "## Summary\n\ntext\n\n1. one\n2. two",
    "First line\nsecond line\n\n- one\n- two **bold**",
    "| Who | What |\n|---|---|\n| Me | plan |\n| Anna | docs |",
    "> quote\n\n---\n\nparagraph",
    "word*italic*word and 2*3*4",
    "**Decided:** release on the twentieth, *if* we make it",
    "1. a\n   - nested\n   - more\n2. b",
]


@needs_node
def test_the_page_and_the_pdf_render_the_same_samples_the_same_way():
    shown = _render(SAMPLES)
    for text, got in zip(SAMPLES, shown, strict=True):
        assert _pdf(text) == got, text


# --------------------------------------------------------------------------
# Time in brackets (fe-pages-14)
# --------------------------------------------------------------------------


@needs_node
def test_time_in_brackets_becomes_a_link_to_the_line_and_keeps_the_dot_with_it():
    single, span, many = _render([
        "We decided on Friday (12:30).", "We argued (15:06–15:07) for long", "Both versions (15:01, 15:09)",
    ])
    assert '<span class="nb"><button type="button" class="cite" data-t="12:30">12:30</button>.</span>' in single
    assert 'class="cite" data-t="15:06">15:06–15:07</button>' in span
    assert many.count('class="cite"') == 2 and 'data-t="15:01"' in many and 'data-t="15:09"' in many


@needs_node
def test_time_in_brackets_inside_code_stays_code():
    """In a `print((12:30))` block the parenthesis was eaten by the button, and the "copy" icon put
    different code into the clipboard.
    """
    fenced, inline_code, mixed = _render([
        f"{FENCE}\nprint((12:30))\n{FENCE}", "a call `(12:30)` in code", "both `(12:30)`, and (12:31)",
    ])
    assert "<pre>print((12:30))</pre>" in fenced and "cite" not in fenced
    assert "<code>(12:30)</code>" in inline_code and "cite" not in inline_code
    assert "<code>(12:30)</code>" in mixed and mixed.count('class="cite"') == 1 and 'data-t="12:31"' in mixed


@needs_node
def test_the_models_text_never_becomes_page_markup():
    (got,) = _render(['<script>alert(1)</script> and **<b onclick="x">**'])
    assert "<script>" not in got and "<b onclick" not in got


# --------------------------------------------------------------------------
# PDF only: the same in `recap/markdown.py`
# --------------------------------------------------------------------------


def test_the_pdf_keeps_numbering_across_blank_lines_and_continuations():
    assert markdown.to_html(STEPS) == "<ol><li>First</li><li>Second</li><li>Third</li></ol>"
    assert markdown.to_html("1. one\n   explanation") == "<ol><li>one<br>explanation</li></ol>"
    assert markdown.to_html("5. five\n6. six").startswith('<ol start="5">')


def test_the_pdf_does_not_drop_items_less_indented_than_the_first():
    html = markdown.to_html(" - a\n- b\n- c")
    assert all(f"<li>{x}</li>" in html for x in "abc")


def test_the_pdf_cuts_time_links_but_leaves_code_alone():
    cut = markdown.strip_timecodes
    assert cut("We decided (12:30) and (15:01, 15:09).") == "We decided and."
    assert cut(f"{FENCE}\nprint((12:30))\n{FENCE}\nand this (12:30) goes") == f"{FENCE}\nprint((12:30))\n{FENCE}\nand this goes"
    assert cut("a call `(12:30)` and (12:31)") == "a call `(12:30)` and"


def test_the_pdf_keeps_multiplication_stars_and_code_with_stars():
    assert markdown.to_html("Total 2 * 3 * 4 = 24") == "<p>Total 2 * 3 * 4 = 24</p>"
    assert markdown.to_html("`a ** b ** c`") == "<p><code>a ** b ** c</code></p>"
    assert markdown.to_html("this is *important*") == "<p>this is <i>important</i></p>"


# --------------------------------------------------------------------------
# Loading and polling the server (fe-pages-13)
# --------------------------------------------------------------------------

POLL_HARNESS = r"""
const calls = [];
let state = null, busy = false, redo = null, editing = null;
const document = { hidden: false };
const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
let now = 1000000;
Date.now = () => now;
let server = null;
const api = async () => { calls.push("api"); if (server instanceof Error) throw server; return JSON.parse(JSON.stringify(server)); };
const drawHead = () => calls.push("head");
const drawLines = () => calls.push("lines");
const drawThread = (tail, stick) => calls.push(["thread", tail, stick]);
const busyTurn = () => "BUSY";
const turns = { innerHTML: "" }, sourcebtn = { hidden: false };
const $ = (id) => (id === "turns" ? turns : sourcebtn);
const esc = (s) => String(s);
const id = "c1";
__SOURCE__
const snap = (over) => ({ recording: false, busy: false, lines: [{}], results: [], stats: { lines: 1 }, ...(over || {}) });
const take = () => { const c = calls.splice(0); return c; };
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const next = async () => { const t = timers.shift(); if (t) { now += t.ms; t.fn(); } await flush(); };
const out = {};

(async () => {
  // recording is in progress: the lock lifts by itself when it ends
  server = snap({ recording: true });
  await load(); out.rec_first = take(); out.rec_timer = timers.map((t) => t.ms);
  server = snap({ recording: true, lines: [{}, {}] });                 // only the utterances grow
  await next(); out.rec_same = take(); out.rec_again = timers.length;
  server = snap({ recording: false, lines: [{}, {}] });
  await next(); out.rec_done = take(); out.rec_after = timers.length; out.state_recording = state.recording;

  // a request from the previous page load is still running
  timers.length = 0;
  server = snap({ busy: true });
  await load(); out.busy_first = take(); out.busy_timer = timers.length;
  server = snap({ results: [{ id: "r1", text: "answer" }] });
  await next(); out.busy_done = take(); out.busy_after = timers.length;

  // its own request is running: a redraw under it would break the waiting card
  timers.length = 0;
  server = snap({ recording: true });
  await load(); take();
  busy = true; await next(); out.own_busy = { calls: take(), timers: timers.length };
  busy = false;

  // connection lost: a background load does not touch the page, the first one reports it
  // as it is
  timers.length = 0; server = snap({ recording: true });
  await load(); take();
  server = new Error("no connection"); await next(); out.bg_fail = { calls: take(), keeps: state.recording, timers: timers.length };
  state = null; turns.innerHTML = ""; timers.length = 0; await load(); out.first_fail = { html: turns.innerHTML, hidden: sourcebtn.hidden, timers: timers.length };

  // return to the tab: no more often than once every couple of seconds
  server = snap(); state = null; await load(); take();
  refreshSoon(); await flush(); out.soon_now = take();
  now += 3000; refreshSoon(); await flush(); out.soon_later = take();
  document.hidden = true; now += 3000; refreshSoon(); await flush(); out.soon_hidden = take();
  console.log(JSON.stringify(out));
})();
"""


def _poll_script() -> str:
    page = _page()
    parts = [
        re.search(r"let pollTimer = 0, loading = false, lastLoad = 0;\n", page).group(0),
        _const(page, "looks"), _function(page, "poll"), _braced(page, page.index("async function load(")),
        _const(page, "refreshSoon"),
    ]
    return POLL_HARNESS.replace("__SOURCE__", "\n".join(parts))


@pytest.fixture(scope="module")
def polled() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is needed to run the JS of the page")
    done = subprocess.run(
        ["node", "-"], input=_poll_script(), capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_recording_lock_is_lifted_by_itself_when_the_recording_stops(polled):
    """A tab opened during recording held the lock and the disabled field until it was refreshed by
    hand.
    """
    assert polled["rec_first"] == ["api", "head", "lines", ["thread", "", True]]
    assert polled["rec_timer"] == [4000], "while recording, the state is read every few seconds"
    assert polled["rec_same"] == ["api", "head"], "growing lines must not redraw the list and reset the scroll"
    assert polled["rec_again"] == 1
    assert polled["rec_done"] == ["api", "head", "lines", ["thread", "", False]], "the recording ended: everything is redrawn, without scrolling"
    assert polled["rec_after"] == 0 and polled["state_recording"] is False, "polling must stop"


@needs_node
def test_a_request_still_running_on_the_server_is_shown_and_waited_for(polled):
    assert polled["busy_first"] == ["api", "head", "lines", ["thread", "BUSY", True]]
    assert polled["busy_timer"] == 1
    assert polled["busy_done"] == ["api", "head", "lines", ["thread", "", False]], "the finished answer takes the place of the waiting"
    assert polled["busy_after"] == 0


@needs_node
def test_a_reload_never_redraws_under_our_own_request_or_after_a_failed_read(polled):
    assert polled["own_busy"] == {"calls": [], "timers": 1}, "the page waits for its own request itself"
    assert polled["bg_fail"] == {"calls": ["api"], "keeps": True, "timers": 1}, "a background failure does not touch the page"
    assert polled["first_fail"]["html"].count("no connection") == 1 and polled["first_fail"]["hidden"] is True
    assert polled["first_fail"]["timers"] == 0


@needs_node
def test_coming_back_to_the_window_reloads_but_not_more_often_than_every_few_seconds(polled):
    assert polled["soon_now"] == [], "just loaded, too early to repeat"
    assert polled["soon_later"][:1] == ["api"]
    assert polled["soon_hidden"] == [], "a hidden tab does not read"


# --------------------------------------------------------------------------
# Random documents: the page and the PDF do not diverge (and lose nothing)
# --------------------------------------------------------------------------


def _random_documents(count: int, seed: int = 7) -> list[str]:
    """Lists with different markers and indents, blank lines, continuations, bold, italics, code,
    multiplication.
    """
    import random

    rng = random.Random(seed)
    words = ["alpha", "beta", "gamma7", "x*y", "2 * 3", "`c`", "**b**", "*i*", "a ** b", "*a*b*"]
    markers = ["-", "*", "1.", "2)", "10.", "•"]

    def line() -> str:
        roll = rng.random()
        indent = " " * rng.choice([0, 0, 0, 1, 2, 3, 4, 6])
        word = rng.choice(words)
        if roll < 0.45:
            return f"{indent}{rng.choice(markers)} {word}"
        if roll < 0.60:
            return ""
        if roll < 0.75:
            return f"{indent}{word} continued"
        return word

    return ["\n".join(line() for _ in range(rng.randint(1, 25))) for _ in range(count)]


@needs_node
def test_the_page_and_the_pdf_agree_on_random_lists_and_emphasis():
    """The `<p class="sub">` heading exists only on the page (a subheading made of one bold line);
    everything else is the same.
    """
    documents = _random_documents(200)
    for text, got in zip(documents, _render(documents), strict=True):
        assert _pdf(text) == got.replace('<p class="sub">', "<p>"), text


@needs_node
def test_no_list_item_is_lost_on_the_page_or_in_the_pdf():
    documents = _random_documents(200, seed=11)
    for text, got in zip(documents, _render(documents), strict=True):
        pdf = markdown.to_html(text)
        for line in text.split("\n"):
            m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+([A-Za-z0-9]+)$", line)
            if m:
                assert m.group(1) in got and m.group(1) in pdf, (m.group(1), text)
