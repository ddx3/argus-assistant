"""The shared `chrome.js` and the `winbar.js` strip without a browser: the real scripts run
in `node` on a mini-DOM (`tests/data/front_dom.js`).

Pinned down (audit of 2026-10-14): a drop-down list does not send `change` for the same
item (fe-admin-chrome-8), waits for a fresh list before showing (-15) and does not close
on scrolling of the conversation timeline (-14); a file dropped outside the drop zone
does not open in place of the page (-7); plain text on copying: a quote of several
paragraphs, a numbered list after a code block, a link address (-10, -11); the mouse
wheel scrolls the tabs of a narrow window (css-design-12); the settings window rereads
the values on every reopening (-1); the red "Visible in recordings" mark
in the window strip (P5).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from argus.ui.server import STATIC

STATIC_DIR = Path(os.environ.get("ARGUS_FRONT_STATIC") or STATIC)
DOM_JS = Path(__file__).resolve().parent / "data" / "front_dom.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page JS")

PRELUDE = r"""
"use strict";
const fs = require("fs");
const assert = require("assert").strict;
const { makeEnv } = require(process.env.FRONT_DOM);
const text = (n) => fs.readFileSync(process.env.STATIC + "/" + n, "utf8");
const plain = (x) => JSON.parse(JSON.stringify(x));    // objects of the page belong to another world (realm) and are not compared strictly
const done = () => console.log("OK");
const fail = (e) => { console.error(e && e.stack || e); process.exit(1); };

// A page with one list: `chrome.js` is attached, as on a real page.
function page() {
  const env = makeEnv();
  env.run(text("chrome.js"), "chrome.js");
  const select = env.el("select");
  select.innerHTML = '<option value="a" selected>Alpha</option><option value="b">Beta</option>';
  const box = env.el("div");
  box.appendChild(select);
  env.document.body.appendChild(box);
  let changes = 0;
  select.addEventListener("change", () => { changes++; });
  const menu = () => env.document.body.querySelector(".combo-menu");
  const opts = () => env.document.body.querySelectorAll(".combo-menu .opt");
  return { env, select, box, menu, opts, changes: () => changes };
}
"""


def run_case(tmp_path: Path, body: str) -> None:
    script = tmp_path / "case.js"
    script.write_text(PRELUDE + "\n(async () => {\n" + body + "\n  done();\n})().catch(fail);\n", encoding="utf-8")
    env = {**os.environ, "FRONT_DOM": str(DOM_JS), "STATIC": str(STATIC_DIR)}
    done = subprocess.run(
        [shutil.which("node"), str(script)], capture_output=True, text=True, env=env, timeout=90, encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, (done.stderr or "") + (done.stdout or "")
    assert done.stdout.strip().endswith("OK")


# ---------------------------------------------------------------- drop-down list


@needs_node
def test_picking_the_item_that_is_already_selected_fires_no_change(tmp_path):
    """fe-admin-chrome-8: a native select sends no `change` for the same item, but ours did. A click
    on the selected microphone (to close the list) closed and reopened the audio in the middle
    of a conversation."""
    run_case(tmp_path, r"""
  const p = page();
  p.env.fire(p.select, p.env.ev("mousedown"));
  await p.env.settle();
  assert.equal(p.opts().length, 2);
  p.env.fire(p.opts()[0], p.env.ev("mousedown"));              // "Alpha" is the one that is selected
  assert.equal(p.changes(), 0, "the same item woke change");
  assert.equal(p.menu(), null, "the list stayed open");
  p.env.fire(p.select, p.env.ev("mousedown"));
  await p.env.settle();
  p.env.fire(p.opts()[1], p.env.ev("mousedown"));              // "Beta" is a different one
  assert.equal(p.changes(), 1);
  assert.equal(p.select.value, "b");
""")


@needs_node
def test_the_list_waits_for_its_refresh_and_a_click_elsewhere_cancels_the_opening(tmp_path):
    """fe-admin-chrome-15: the list owner rereads the options before showing it
    (`select._beforeOpen`); a click elsewhere while it waits cancels the opening, and the list
    does not pop up on its own later."""
    run_case(tmp_path, r"""
  const p = page();
  let gate = p.env.gate();
  p.select._beforeOpen = () => gate.promise.then(() => {
    p.select.innerHTML = '<option value="a" selected>Alpha</option><option value="b">Beta</option><option value="c">Gamma</option>';
  });
  p.env.fire(p.select, p.env.ev("mousedown"));
  await p.env.settle();
  assert.equal(p.menu(), null, "the list was built before the answer");
  gate.open();
  await p.env.settle();
  assert.equal(p.opts().length, 3, "the list does not have the fresh options");
  p.env.fire(p.env.document.body, p.env.ev("mousedown"));      // closed by clicking outside
  // now an opening that is canceled midway
  gate = p.env.gate();
  p.select._beforeOpen = () => gate.promise;
  p.env.fire(p.select, p.env.ev("mousedown"));
  await p.env.settle();
  p.env.fire(p.env.document.body, p.env.ev("mousedown"));
  gate.open();
  await p.env.settle();
  assert.equal(p.menu(), null, "the canceled opening showed the list anyway");
""")


@needs_node
def test_only_a_scroll_that_moves_the_list_closes_it(tmp_path):
    """fe-admin-chrome-14: the feed of utterances scrolls on every new line, and the microphone
    list used to close from under the cursor. A scroll of the page, or of the container that
    holds the select itself, closes it."""
    run_case(tmp_path, r"""
  const p = page();
  const feed = p.env.el("div");                                 // the conversation feed: nearby, the select is not inside it
  p.env.document.body.appendChild(feed);
  const open = async () => { p.env.fire(p.select, p.env.ev("mousedown")); await p.env.settle(); assert.ok(p.menu(), "the list did not open"); };
  const scroll = (target) => p.env.fire(target, p.env.ev("scroll", {}, false));
  await open();
  scroll(feed);
  assert.ok(p.menu(), "scrolling the feed closed the list");
  scroll(p.menu());
  assert.ok(p.menu(), "the list's own scrolling closed it");
  scroll(p.box);                                                // the container with the select itself
  assert.equal(p.menu(), null, "scrolling the container did not close the list");
  await open();
  scroll(p.env.document);                                       // the whole page
  assert.equal(p.menu(), null, "scrolling the page did not close the list");
""")


# ---------------------------------------------------------------- file outside the zone


@needs_node
def test_a_file_dropped_outside_the_drop_zone_does_not_open_in_the_window(tmp_path):
    """fe-admin-chrome-7: a file dropped outside the zone was opened by WebView2 instead of the page
    (frameless window, the only way back is Alt+Left). The drop zone accepts files itself;
    anything else we leave alone."""
    run_case(tmp_path, r"""
  const p = page();
  const zone = p.env.el("label");
  zone.addEventListener("dragover", (e) => e.preventDefault());
  zone.addEventListener("drop", (e) => e.preventDefault());
  p.env.document.body.appendChild(zone);
  const files = () => ({ dataTransfer: { types: ["Files"], dropEffect: "copy" } });
  let e = p.env.ev("dragover", files());
  p.env.fire(p.env.document.body, e);
  assert.equal(e.defaultPrevented, true, "dragover outside the zone was not canceled");
  assert.equal(e.dataTransfer.dropEffect, "none", "no 'not allowed' icon over the page");
  e = p.env.ev("drop", files());
  p.env.fire(p.env.document.body, e);
  assert.equal(e.defaultPrevented, true, "drop outside the zone was not canceled: the window will open the file");
  e = p.env.ev("dragover", files());
  p.env.fire(zone, e);                                          // over the drop zone: it accepted the drop on its own
  assert.equal(e.dataTransfer.dropEffect, "copy", "a ban was set over the drop zone");
  e = p.env.ev("dragover", { dataTransfer: { types: ["text/plain"], dropEffect: "copy" } });
  p.env.fire(p.env.document.body, e);                           // selected text dragged between fields: we leave it alone
  assert.equal(e.defaultPrevented, false, "dragging text is broken");
  e = p.env.ev("drop", { dataTransfer: { types: ["text/plain"] } });
  p.env.fire(p.env.document.body, e);
  assert.equal(e.defaultPrevented, false);
""")


# ---------------------------------------------------------------- settings window


WINDOW = r"""
  const env = makeEnv();
  const button = env.el("button", { id: "settings-btn" });
  env.document.body.appendChild(button);
  const log = [];
  env.window.argusAdmin = { mount: async () => { log.push("mount"); }, refresh: async () => { log.push("refresh"); }, flushSave() {} };
  env.run(text("chrome.js"), "chrome.js");
  const open = async () => { env.fire(button, env.ev("click")); await env.settle(); };
  const win = () => env.document.body.querySelector(".win");
"""


@needs_node
def test_the_window_rereads_the_values_each_time_it_is_opened_again(tmp_path):
    """fe-admin-chrome-1: while the window is closed, the values are changed from outside (a popup
    at the question field, another tab); the first opening has just read them itself, every
    next one rereads them."""
    run_case(tmp_path, WINDOW + r"""
  await open();
  assert.deepEqual(log, ["mount"], "the first opening rereads what has just been read");
  win().querySelector(".win-close").click();
  await env.advance(200);
  await open();
  assert.deepEqual(log, ["mount", "refresh"], "the repeated opening did not reread the values");
""")


@needs_node
def test_the_wheel_scrolls_the_tab_strip_of_a_narrow_window_sideways(tmp_path):
    """css-design-12: in a narrow window the tabs sit in one row and scroll sideways; the wheel did
    not scroll sideways, so "Payment" and "Account" on the right could not
    be reached with the mouse."""
    run_case(tmp_path, WINDOW + r"""
  await open();
  const list = win().querySelector(".win-tabs");
  list.scrollWidth = 1100; list.clientWidth = 690;              // narrow layout: the tabs do not fit
  let e = env.ev("wheel", { deltaX: 0, deltaY: 120 }, true);
  env.fire(list, e);
  assert.equal(list.scrollLeft, 120, "the vertical wheel did not turn into a horizontal one");
  assert.equal(e.defaultPrevented, true);
  e = env.ev("wheel", { deltaX: 90, deltaY: 10 }, true);        // the touchpad already scrolls sideways by itself
  env.fire(list, e);
  assert.equal(list.scrollLeft, 120);
  assert.equal(e.defaultPrevented, false);
  list.scrollWidth = 690;                                       // wide window: the list is vertical, there is nothing to scroll sideways
  e = env.ev("wheel", { deltaX: 0, deltaY: 120 }, true);
  env.fire(list, e);
  assert.equal(list.scrollLeft, 120);
  assert.equal(e.defaultPrevented, false, "the wheel in a vertical list was intercepted");
""")


# ---------------------------------------------------------------- copying an answer


COPY = r"""
  const env = makeEnv();
  env.run(text("chrome.js"), "chrome.js");
  const parts = (html) => plain(env.window.argusCopy.parts(html));
"""


@needs_node
def test_a_quote_of_several_paragraphs_keeps_them_apart_in_plain_text(tmp_path):
    """fe-admin-chrome-10: "> first paragraphsecond paragraph" (the two paragraphs run
    together) and "> Note:onetwo" (Note: one, two, run together).
    """
    run_case(tmp_path, COPY + r"""
  assert.equal(parts("<blockquote><p>first</p><p>second</p></blockquote>").text, "> first\n>\n> second");
  assert.equal(
    parts("<blockquote><p>Note:</p><ul><li>one</li><li>two</li></ul></blockquote>").text,
    "> Note:\n>\n> - one\n> - two");
  // a quote without blocks inside stays as it was: one line with a time link
  assert.equal(
    parts('<blockquote>"we take June" <span class="nb"><button class="cite">14:32</button></span></blockquote>'
      + '<p>The deadline was named at 14:40 <span class="nb"><button class="cite">14:41</button>.</span></p>').text,
    "> \"we take June\" (14:32)\n\nThe deadline was named at 14:40 (14:41).");
""")


@needs_node
def test_an_ordered_list_that_continues_after_a_block_keeps_its_numbers(tmp_path):
    """fe-admin-chrome-10: `<ol start="3">` after a code block was pasted as "1. 2."."""
    run_case(tmp_path, COPY + r"""
  assert.equal(parts('<ol start="3"><li>run</li><li>check</li></ol>').text, "3. run\n4. check");
  assert.equal(parts("<ol><li>one</li><li>two</li></ol>").text, "1. one\n2. two");
  assert.equal(parts("<ul><li>a</li><li>b</li></ul>").text, "- a\n- b");
""")


@needs_node
def test_a_copied_link_keeps_its_address_in_both_flavours(tmp_path):
    """fe-admin-chrome-11: from "See [the documentation](https://…)"
    only "See the documentation" was pasted. The address lives in `data-href` (the page parser),
    earlier it was in `title`; in HTML it becomes a real link."""
    run_case(tmp_path, COPY + r"""
  const url = "https://docs.python.org/3/library/asyncio.html";
  let got = parts('<p>See <span class="lnk" data-href="' + url + '">the documentation <span class="lnk-url">(docs.python.org)</span></span> and on</p>');
  assert.equal(got.text, "See the documentation (" + url + ") and on");
  assert.equal(got.html, '<p>See <a href="' + url + '">the documentation</a> and on</p>');
  got = parts('<p><span class="lnk" title="https://x.dev/a">caption</span></p>');
  assert.equal(got.text, "caption (https://x.dev/a)");
  got = parts('<p><span class="lnk" data-href="https://x.dev">https://x.dev</span></p>');
  assert.equal(got.text, "https://x.dev", "a caption equal to the address must not be printed twice");
  // no address in the markup: as on the page, a caption and a short address
  got = parts('<p><span class="lnk">caption <span class="lnk-url">(example.com)</span></span></p>');
  assert.equal(got.text, "caption (example.com)");
  assert.ok(!got.html.includes("<a "), got.html);
  // a foreign scheme does not become a link
  got = parts('<p><span class="lnk" data-href="javascript:alert(1)">ex</span></p>');
  assert.ok(!got.html.includes("<a ") && !got.html.includes("javascript"), got.html);
""")


# ---------------------------------------------------------------- window strip


WINBAR = r"""
  const start = (search, capture = { available: true, excluded: false }) => {
    const env = makeEnv();
    env.window.location.search = search;
    env.routes["/window/main"] = async () => ({ body: { ok: true, maximized: false } });
    env.routes["/window/capture"] = async () => ({ body: capture });
    env.run(text("winbar.js"), "winbar.js");
    return env;
  };
  const alarm = (env) => env.document.body.querySelector(".winbar-alert");
"""


@needs_node
def test_the_bar_shows_a_red_mark_when_the_window_could_not_be_hidden_from_recording(tmp_path):
    """P5/P2: the launch could not hide the window from screen recording and appended
    `capture=fail` to the address. The message window gets closed while the screen keeps being
    shown, so the mark in the bar stays lit as long as that is the case."""
    run_case(tmp_path, WINBAR + r"""
  const env = start("?desk=1&capture=fail");
  await env.settle();
  const mark = alarm(env);
  assert.ok(mark, "no mark");
  assert.equal(mark.hidden, false, "the mark is hidden although the window is visible in the recording");
  assert.equal(mark.textContent, "Visible in recordings");
  assert.equal(env.window.sessionStorage.getItem("argus-capture-fail"), "1", "the mark was not remembered for the next pages");
  const say = (detail) => env.window.dispatchEvent(new env.window.CustomEvent("argus-capture", { detail }));
  say({ ok: true, wanted: true });                              // the person successfully hid the window
  assert.equal(mark.hidden, true);
  assert.equal(env.window.sessionStorage.getItem("argus-capture-fail"), null);
  say({ ok: false, wanted: true });                             // and unsuccessfully tried once more
  assert.equal(mark.hidden, false);
""")


@needs_node
def test_the_mark_goes_out_when_the_window_is_in_fact_hidden_and_is_absent_without_the_flag(tmp_path):
    run_case(tmp_path, WINBAR + r"""
  let env = start("?desk=1&capture=fail", { available: true, excluded: true });
  await env.settle();
  assert.equal(alarm(env).hidden, true, "the window is hidden but the mark is lit");
  env = start("?desk=1");
  await env.settle();
  assert.equal(alarm(env).hidden, true, "the mark is lit for no reason");
  assert.equal(env.calls.filter((c) => c.url === "/window/capture").length, 0, "an extra state request");
""")
