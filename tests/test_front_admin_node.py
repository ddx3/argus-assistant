"""The settings window (`admin.js`) without a browser: the real script runs in `node` on a
mini-DOM (`tests/data/front_dom.js`).

Before this, the window was checked only by searching for strings in the file text
(`test_settings_window.py`): an autosave race, long Cyrillic text, a batch of files at
once and a stale microphone list all passed green (audit of 2026-10-14,
fe-admin-chrome-17). Here the settings page is built from the schema as the application
serves it, the fields are real (the markup is parsed and written as in a browser), and
only the edges are replaced: `fetch` with the "application's" answers, the clock and the
timers.

Pinned down: an edit made while a save is in progress is not erased by the response
(fe-admin-chrome-3); one rejected value does not hold back the other edits and is visible
on any tab (-6); long Cyrillic text goes out (-5); several files at once are all
uploaded, and an error in one does not break the others (-2, -13); key combinations:
shift without a modifier, AltGr, and the "revert" request reaches the server on close
(-12, -9); microphone choice: a fresh list before showing, and the same item reopens
nothing (-15, -8); a refusal of the "Invisible on recording" switch
is visible (P5); the window rereads the values on opening, and a page from another
session does not write (-1, -4).

Each case is a separate `node` process with its own scenario; the checks are inside the
scenario (`assert`), and the error is visible in the output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from argus.ui.server import STATIC

# `ARGUS_FRONT_STATIC` is where to look instead of the working static files (this is how
# we checked that the tests fail on a copy before the fixes).
STATIC_DIR = Path(os.environ.get("ARGUS_FRONT_STATIC") or STATIC)
DOM_JS = Path(__file__).resolve().parent / "data" / "front_dom.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page JS")

PRELUDE = r"""
"use strict";
const fs = require("fs");
const assert = require("assert").strict;
const { makeEnv } = require(process.env.FRONT_DOM);
const text = (n) => fs.readFileSync(process.env.STATIC + "/" + n, "utf8");

const P = {
  name: "chat.name", ctx: "channel.assist.profile.context", lang: "channel.assist.profile.language",
  think: "channel.assist.answering.think_longer", ask: "channel.assist.hotkeys.ask",
};
const SECTIONS = [
  { id: "about", title: "Context", lead: "", widget: "", badge: "", scope: "chat", settings: [
    { path: P.name, label: "Session name", hint: "", kind: "string", hot: true, placeholder: "", rows: 1 },
    { path: P.ctx, label: "", hint: "", kind: "text", hot: true, placeholder: "", rows: 4 },
  ] },
  { id: "language", title: "Language", lead: "", widget: "", badge: "", scope: "chat", settings: [
    { path: P.lang, label: "Language", hint: "", kind: "choice", hot: true, placeholder: "", options: [
      { value: "ru", label: "Русский" }, { value: "en", label: "English" }] },
  ] },
  { id: "answering", title: "When to answer", lead: "", widget: "", badge: "", scope: "chat", settings: [
    { path: P.think, label: "Think longer", hint: "", kind: "bool", hot: true, placeholder: "" },
  ] },
  { id: "hotkeys", title: "Hotkeys", lead: "", widget: "", badge: "", scope: "app", settings: [
    { path: P.ask, label: "Ask", hint: "", kind: "hotkey", hot: true, placeholder: "" },
  ] },
  { id: "materials", title: "Materials", lead: "", widget: "materials", badge: "", scope: "chat", settings: [] },
  { id: "audio", title: "Audio", lead: "", widget: "audio-check", badge: "", scope: "app", settings: [] },
  { id: "window", title: "Window", lead: "", widget: "window-visibility", badge: "", scope: "app", settings: [] },
];

// The settings page on a fake app: `server` holds the values, `gate` holds the response to
// a save, `reject(values)` returns per-field errors (like `/admin/apply` when a value is
// not accepted).
async function boot(opts = {}) {
  const env = makeEnv();
  const server = {
    values: { [P.name]: "Meeting", [P.ctx]: "", [P.lang]: "ru", [P.think]: false, [P.ask]: "ctrl+alt+a" },
    chat: "chat" in opts ? opts.chat : "A", gate: null, reject: null,
    devices: { inputs: [{ name: "Mic A", default: true }], current: "Mic A", pinned: "" },
    capture: (body) => ({ body: { ok: true, excluded: body.excluded, detail: "" } }),
  };
  Object.assign(server.values, opts.values || {});
  // a map from key position to the letter on this layout, as Chromium returns it
  // (`navigator.keyboard`)
  if (opts.layout) env.window.navigator.keyboard = { getLayoutMap: async () => opts.layout, addEventListener() {} };
  const schema = () => ({
    title: "Argus", subtitle: "", channel: "assist", accent: "#4fb8d8", file: "x", version: { number: "1.0", build: "" },
    ...(opts.noChat ? {} : { chat: server.chat }),
    sections: opts.sections || SECTIONS, values: { ...server.values }, overridden: [], restart_pending: [],
  });
  env.routes["/admin/schema"] = async () => ({ body: schema() });
  env.routes["/admin/apply"] = async (body) => {
    const gate = server.gate;
    if (gate) await gate.promise;
    const errors = server.reject ? server.reject(body.values) : null;
    if (errors) return { body: { ok: false, errors } };
    if (body.chat !== undefined && body.chat !== server.chat) return { body: { ok: false, stale: true } };
    Object.assign(server.values, body.values);
    return { body: { ok: true, changed: Object.keys(body.values), restart: [], overridden: [], values: { ...server.values } } };
  };
  env.routes["/admin/assist/materials"] = async () => ({ body: { ok: true, items: [], enabled: true } });
  env.routes["/audio/devices"] = async () => ({ body: JSON.parse(JSON.stringify(server.devices)) });
  env.routes["/control"] = async () => ({ body: { ok: true } });
  env.routes["/admin/hotkeys/capture"] = async () => ({ body: { held: true } });
  env.routes["/window/capture"] = async (body) => (body ? server.capture(body) : { body: { available: true, excluded: false } });
  if (opts.window) {
    // as in the app: the ⚙ button, `chrome.js` builds the window and brings the settings tabs
    // from `admin.js` into it
    const button = env.el("button", { id: "settings-btn" });
    env.document.body.appendChild(button);
    env.run(text("admin.js"), "admin.js");
    env.run(text("chrome.js"), "chrome.js");
    const open = async () => { env.fire(button, env.ev("click")); await env.settle(); };
    await open();
    return { env, server, open, win: () => env.document.body.querySelector(".win") };
  }
  if (opts.chrome) env.run(text("chrome.js"), "chrome.js");
  env.run(text("admin.js"), "admin.js");
  const root = env.el("div");
  env.document.body.appendChild(root);
  await env.window.argusAdmin.mount(root, {});
  await env.settle();
  const row = (path) => root.querySelector(`.f[data-path="${path}"]`);
  return {
    env, server, root, row,
    box: (path) => row(path).querySelector("input, textarea"),
    select: (path) => row(path).querySelector("select"),
    type(path, value) { const b = this.box(path); b.value = value; env.fire(b, env.ev("input")); },
    posts: () => env.posts("/admin/apply"),
    bar: () => ({ hidden: root.querySelector("#unsaved").hidden, text: root.querySelector("#unsaved-n").textContent }),
  };
}
const done = () => console.log("OK");
const fail = (e) => { console.error(e && e.stack || e); process.exit(1); };
"""


def run_case(tmp_path: Path, body: str) -> None:
    """One scenario — one `node` process; a failed check is visible in the output."""
    script = tmp_path / "case.js"
    script.write_text(PRELUDE + "\n(async () => {\n" + body + "\n  done();\n})().catch(fail);\n", encoding="utf-8")
    env = {**os.environ, "FRONT_DOM": str(DOM_JS), "STATIC": str(STATIC_DIR)}
    done = subprocess.run(
        [shutil.which("node"), str(script)], capture_output=True, text=True, env=env, timeout=90, encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, (done.stderr or "") + (done.stdout or "")
    assert done.stdout.strip().endswith("OK")


# ---------------------------------------------------------------- autosave


@needs_node
def test_an_edit_made_while_a_save_is_in_flight_survives_the_reply(tmp_path):
    """fe-admin-chrome-3: the reply to a request erased the whole queue, not just what was sent,
    and repainted the fields with the old value; the write waited for a new keypress that
    nobody made."""
    run_case(tmp_path, r"""
  const t = await boot();
  t.server.gate = t.env.gate();
  t.type(P.name, "New name");
  await t.env.advance(700);                       // the first request went out and is hanging: the app is thinking
  assert.equal(t.posts().length, 1);
  t.type(P.ctx, "edit in flight");               // the person keeps typing
  const gate = t.server.gate; t.server.gate = null;
  gate.open();
  await t.env.settle();
  assert.equal(t.box(P.ctx).value, "edit in flight", "the reply rolled the field back, the edit in flight is lost");
  await t.env.advance(700);
  assert.equal(t.posts().length, 2, "the edit in flight never went out");
  assert.deepEqual(t.posts()[0].body.values, { [P.name]: "New name" });
  assert.deepEqual(t.posts()[1].body.values, { [P.ctx]: "edit in flight" });
  assert.equal(t.server.values[P.ctx], "edit in flight");
  assert.equal(t.bar().hidden, true);
""")


@needs_node
def test_a_second_save_does_not_start_while_one_is_in_flight(tmp_path):
    """The autosave pause may expire while the first request is still going: a second request
    does not go out on top of it, but follows when the first is finished (otherwise they
    overtake each other)."""
    run_case(tmp_path, r"""
  const t = await boot();
  t.server.gate = t.env.gate();
  t.type(P.name, "A");
  await t.env.advance(700);
  t.type(P.ctx, "B");
  await t.env.advance(700);                       // the pause expired in the middle of a request in flight
  assert.equal(t.posts().length, 1, "the second request went out on top of the first");
  const gate = t.server.gate; t.server.gate = null;
  gate.open();
  await t.env.settle();
  assert.equal(t.posts().length, 2, "the second request did not follow");
  assert.deepEqual(t.posts()[1].body.values, { [P.ctx]: "B" });
  assert.equal(t.server.values[P.ctx], "B");
""")


@needs_node
def test_a_value_changed_back_while_a_save_is_in_flight_is_not_rolled_back_by_the_reply(tmp_path):
    """The person typed "A", the save went out, and they put the previous "Meeting"
    back without waiting for the reply: the reply carries "A", and without a separate edit
    the field would silently roll back to "A"."""
    run_case(tmp_path, r"""
  const t = await boot();
  t.server.gate = t.env.gate();
  t.type(P.name, "A");
  await t.env.advance(700);
  t.type(P.name, "Meeting");                      // restored the previous value
  const gate = t.server.gate; t.server.gate = null;
  gate.open();
  await t.env.settle();
  assert.equal(t.box(P.name).value, "Meeting");
  await t.env.advance(700);
  assert.equal(t.server.values[P.name], "Meeting", "the return to the previous value did not reach the app");
""")


# ---------------------------------------------------------------- application refusal


@needs_node
def test_one_rejected_value_does_not_block_the_other_edits_and_is_visible_on_every_tab(tmp_path):
    """fe-admin-chrome-6: the application applies the set as a whole or not at all; an empty
    session name rejected all edits from other tabs, and the message stood only at its own
    row."""
    run_case(tmp_path, r"""
  const t = await boot();
  t.server.reject = (v) => (v[P.name] === "" ? { [P.name]: "the name cannot be empty" } : null);
  t.type(P.name, "");
  t.select(P.lang).value = "en";
  t.env.fire(t.select(P.lang), t.env.ev("change"));
  await t.env.advance(700);
  const posts = t.posts();
  assert.equal(posts.length, 2, "after the refusal the other edits were not sent");
  assert.deepEqual(posts[1].body.values, { [P.lang]: "en" });
  assert.equal(t.server.values[P.lang], "en");
  const bar = t.bar();
  assert.equal(bar.hidden, false, "there is no 'not saved' bar");
  assert.ok(bar.text.includes("Session name") && bar.text.includes("the name cannot be empty"), bar.text);
  assert.equal(t.row(P.name).querySelector(".err").hidden, false);
  assert.equal(t.box(P.name).value, "", "the rejected field is cleared");
  // fixed the field: it goes out, the bar disappears
  t.type(P.name, "Another");
  await t.env.advance(700);
  assert.equal(t.server.values[P.name], "Another");
  assert.equal(t.bar().hidden, true);
  assert.equal(t.row(P.name).querySelector(".err").hidden, true);
""")


@needs_node
def test_a_refusal_that_names_no_field_keeps_the_queue_and_says_so(tmp_path):
    """The server answered with a non-field error (a malformed request): there is nothing to
    name, but silence is not allowed; the queue is intact.
    """
    run_case(tmp_path, r"""
  const t = await boot();
  t.env.routes["/admin/apply"] = async () => ({ status: 400, body: { detail: "values must be a set of settings" } });
  t.type(P.ctx, "text");
  await t.env.advance(700);
  assert.equal(t.posts().length, 1, "a refusal that names no field loops the request");
  assert.equal(t.bar().hidden, false);
  assert.ok(t.bar().text.includes("values must be a set of settings"), t.bar().text);
  // the app came to its senses: the next edit carries the old one along too
  t.env.routes["/admin/apply"] = async (b) => { t.server.values = { ...t.server.values, ...b.values }; return { body: { ok: true, values: t.server.values, overridden: [], restart: [] } }; };
  t.type(P.name, "More");
  await t.env.advance(700);
  assert.equal(t.server.values[P.ctx], "text");
  assert.equal(t.server.values[P.name], "More");
  assert.equal(t.bar().hidden, true);
""")


@needs_node
def test_a_long_cyrillic_text_is_saved_because_keepalive_counts_bytes(tmp_path):
    """fe-admin-chrome-5: the 64 KiB limit of `keepalive` is in bytes; 33 thousand Cyrillic
    characters are already 66 KB, and the request was rejected on the spot while the edit
    silently disappeared."""
    run_case(tmp_path, r"""
  const t = await boot();
  const real = t.env.window.fetch;
  // like a browser: a keepalive request longer than 64 KiB is rejected on the spot
  t.env.window.fetch = async (url, init = {}) => {
    if (init.keepalive && Buffer.byteLength(init.body || "") > 65536) throw new TypeError("Failed to fetch");
    return real(url, init);
  };
  const big = "\u044f".repeat(33000);       // Cyrillic "ya": two bytes in UTF-8
  t.type(P.ctx, big);
  await t.env.advance(700);
  assert.equal(t.server.values[P.ctx], big, "the long context was not saved");
  assert.equal(t.posts().at(-1).init.keepalive || false, false, "the long request went out with keepalive");
  assert.equal(t.bar().hidden, true);
  // a short edit still goes out with keepalive: it must survive the page closing
  t.type(P.name, "Short");
  await t.env.advance(700);
  assert.equal(t.posts().at(-1).init.keepalive, true);
""")


@needs_node
def test_a_save_the_browser_refuses_is_retried_and_a_dead_server_is_shown(tmp_path):
    """A keepalive request did not get through — one retry as an ordinary one. If that did not
    get through either — "not saved" in the strip, the queue is intact and
    goes out with the next edit (previously — an unhandled rejection and silence)."""
    run_case(tmp_path, r"""
  const t = await boot();
  const real = t.env.window.fetch;
  t.env.window.fetch = async (url, init = {}) => {
    if (String(url) === "/admin/apply" && init.keepalive) throw new TypeError("Failed to fetch");
    return real(url, init);
  };
  t.type(P.name, "One");
  await t.env.advance(700);
  assert.equal(t.server.values[P.name], "One", "the retry without keepalive did not happen");
  t.env.window.fetch = async (url, init = {}) => { if (String(url) === "/admin/apply") throw new TypeError("Failed to fetch"); return real(url, init); };
  t.type(P.ctx, "Two");
  await t.env.advance(700);
  assert.equal(t.bar().hidden, false);
  assert.ok(t.bar().text.includes("can't reach the app"), t.bar().text);
  assert.equal(t.server.values[P.ctx], "");
  t.env.window.fetch = real;
  t.type(P.name, "Three");
  await t.env.advance(700);
  assert.equal(t.server.values[P.ctx], "Two", "what was not saved earlier did not go out with the next edit");
  assert.equal(t.server.values[P.name], "Three");
  assert.equal(t.bar().hidden, true);
""")


@needs_node
def test_closing_the_window_waits_for_the_save_in_flight_and_for_what_was_typed_since(tmp_path):
    """`flushSave` (window close, Ctrl+S) returns the real end, not "a request is already in
    progress".
    """
    run_case(tmp_path, r"""
  const t = await boot();
  t.server.gate = t.env.gate();
  t.type(P.name, "One");
  await t.env.advance(700);
  t.type(P.ctx, "Two");
  const flushed = t.env.window.argusAdmin.flushSave();
  let finished = false;
  flushed.then(() => { finished = true; });
  await t.env.settle();
  assert.equal(finished, false, "flushSave returned while the request is still in flight");
  const gate = t.server.gate; t.server.gate = null;
  gate.open();
  await flushed;
  assert.equal(t.server.values[P.name], "One");
  assert.equal(t.server.values[P.ctx], "Two", "an unsaved edit remained after flushSave");
""")


# ---------------------------------------------------------------- another session and rereading


@needs_node
def test_the_page_sends_its_session_and_a_foreign_page_cannot_write(tmp_path):
    """fe-admin-chrome-4: a tab stayed on session A, the application is already on B — the edit
    must not land in B. The page names its session; the application answers with a `stale`
    refusal; after that the page does not write."""
    run_case(tmp_path, r"""
  const t = await boot();
  t.type(P.name, "Own");
  await t.env.advance(700);
  assert.equal(t.posts()[0].body.chat, "A", "the page did not name its session");
  t.server.chat = "B";                            // another session was opened in a neighboring tab
  t.type(P.ctx, "foreign context");
  await t.env.advance(700);
  assert.equal(t.posts().length, 2);
  assert.equal(t.server.values[P.ctx], "");
  assert.equal(t.bar().hidden, false);
  assert.ok(t.bar().text.includes("another session"), t.bar().text);
  t.type(P.ctx, "once more");
  await t.env.advance(700);
  assert.equal(t.posts().length, 2, "the page of another session kept writing");
""")


@needs_node
def test_a_server_that_does_not_name_the_session_is_still_served_as_before(tmp_path):
    """While the application does not report the session in the schema, the page adds nothing
    extra to the request.
    """
    run_case(tmp_path, r"""
  const t = await boot({ noChat: true });
  t.type(P.name, "As before");
  await t.env.advance(700);
  assert.equal("chat" in t.posts()[0].body, false);
  assert.equal(t.server.values[P.name], "As before");
""")


@needs_node
def test_reopening_the_window_rereads_the_values_but_keeps_unsent_edits(tmp_path):
    """fe-admin-chrome-1: while the window was closed, "Think longer" and the
    language were changed in the popup next to the question field; the window held a copy
    from the first read."""
    run_case(tmp_path, r"""
  const t = await boot();
  assert.equal(t.row(P.think).querySelector(".switch").classList.contains("on"), false);
  t.type(P.ctx, "did not manage to send");            // still in the queue
  t.server.values[P.think] = true;                // changed from outside
  t.server.values[P.lang] = "en";
  t.server.values[P.ctx] = "foreign";
  await t.env.window.argusAdmin.refresh();
  assert.equal(t.row(P.think).querySelector(".switch").classList.contains("on"), true, "the switch was not reread");
  assert.equal(t.select(P.lang).value, "en", "the list was not reread");
  assert.equal(t.box(P.ctx).value, "did not manage to send", "the unsaved edit was wiped by the reread");
""")


@needs_node
def test_reopening_the_window_for_another_session_stops_writes(tmp_path):
    run_case(tmp_path, r"""
  const t = await boot();
  t.server.chat = null;                           // the session was closed or the app was restarted
  await t.env.window.argusAdmin.refresh();
  assert.equal(t.bar().hidden, false);
  t.type(P.ctx, "into the shared file");
  await t.env.advance(700);
  assert.equal(t.posts().length, 0, "a page without a session writes session settings to the shared file");
""")


# ---------------------------------------------------------------- materials


@needs_node
def test_every_file_of_a_multiple_choice_is_uploaded(tmp_path):
    """fe-admin-chrome-2: `input.files` is live, and `input.value = ""` emptied it while the
    first file was being sent.
    """
    run_case(tmp_path, r"""
  const t = await boot();
  const sent = [];
  t.env.routes["/admin/assist/materials"] = async (b, init) => {
    if (init.method === "POST") sent.push(init.body.name);
    return { body: { ok: true, items: sent.map((name) => ({ name })), enabled: true } };
  };
  const input = t.root.querySelector("#mt-file");
  input.files = [{ name: "a.pdf", size: 10 }, { name: "b.docx", size: 10 }, { name: "c.png", size: 10 }];
  t.env.fire(input, t.env.ev("change"));
  await t.env.settle();
  assert.deepEqual(sent, ["a.pdf", "b.docx", "c.png"], "not all files went out");
""")


@needs_node
def test_a_failed_file_is_reported_by_name_and_the_rest_still_go(tmp_path):
    """fe-admin-chrome-13: a failure of one file left "Parsing…" forever and skipped
    the rest; a file that is too large and an unreadable type are cut off before upload."""
    run_case(tmp_path, r"""
  const t = await boot();
  const sent = [];
  t.env.routes["/admin/assist/materials"] = async (b, init) => {
    if (init.method !== "POST") return { body: { ok: true, items: [], enabled: true } };
    if (init.body.name === "b.pdf") throw new TypeError("Failed to fetch");
    sent.push(init.body.name);
    return { body: { ok: true, items: sent.map((name) => ({ name })), enabled: true } };
  };
  const input = t.root.querySelector("#mt-file");
  input.files = [
    { name: "a.pdf", size: 10 }, { name: "b.pdf", size: 10 }, { name: "c.pdf", size: 10 },
    { name: "huge.pdf", size: 101 * 1024 * 1024 }, { name: "virus.exe", size: 10 },
  ];
  t.env.fire(input, t.env.ev("change"));
  await t.env.settle();
  assert.deepEqual(sent, ["a.pdf", "c.pdf"], "after the failure the other files did not go out, or extra ones did");
  const sum = t.root.querySelector("#mt-sum").innerHTML;
  assert.ok(!sum.includes("Parsing"), "the 'Parsing…' label was left hanging: " + sum);
  assert.ok(sum.includes("b.pdf") && sum.includes("couldn't be uploaded"), sum);
  assert.ok(sum.includes("huge.pdf") && sum.includes("100 MB"), sum);
  assert.ok(sum.includes("virus.exe"), sum);
""")


# ---------------------------------------------------------------- key combinations


async_hotkey = r"""
  const t = await boot();
  const btn = t.row(P.ask).querySelector(".hk");
  const press = (fields) => t.env.fire(t.env.window, t.env.ev("keydown", {
    ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, key: "", getModifierState: () => false, ...fields,
  }));
  const start = async () => { t.env.fire(btn, t.env.ev("click")); await t.env.settle(); };
"""


@needs_node
def test_shift_alone_is_not_a_hotkey_modifier(tmp_path):
    """fe-admin-chrome-12: shift+a, shift+space, shift+1 would take ordinary input away from all
    programs; with shift alone only F-keys are suitable."""
    run_case(tmp_path, async_hotkey + r"""
  await start();
  press({ code: "KeyA", key: "A", shiftKey: true });
  assert.equal(btn.textContent, "Needs ctrl, alt, or win");
  press({ code: "Space", key: " ", shiftKey: true });
  press({ code: "Digit1", key: "!", shiftKey: true });
  await t.env.advance(700);
  assert.equal(t.posts().length, 0, "a combination with shift alone was recorded");
  press({ code: "F5", key: "F5", shiftKey: true });
  await t.env.advance(700);
  assert.equal(t.posts().at(-1).body.values[P.ask], "shift+f5");
""")


@needs_node
def test_altgr_letters_are_refused_when_recording_a_hotkey(tmp_path):
    """fe-admin-chrome-9 (interface): ctrl+alt+A on a Polish layout is AltGr+A ("ą"), it must not
    be taken away.
    """
    run_case(tmp_path, async_hotkey + r"""
  await start();
  press({ code: "KeyS", key: "ś", ctrlKey: true, altKey: true, getModifierState: (k) => k === "AltGraph" });
  assert.ok(btn.textContent.includes("AltGr"), btn.textContent);
  await t.env.advance(700);
  assert.equal(t.posts().length, 0);
  // on a layout without AltGr the same combination works
  press({ code: "KeyS", key: "s", ctrlKey: true, altKey: true });
  await t.env.advance(700);
  assert.equal(t.posts().at(-1).body.values[P.ask], "ctrl+alt+s");
""")


@needs_node
def test_the_hotkey_button_shows_the_letter_printed_on_the_key_for_this_layout(tmp_path):
    """fe-admin-chrome-16 (display): the combination is stored by the PLACE of the key, and the
    application itself converts it to a code for the layout (`hotkeys.py`); on a French
    layout "A" sits where Q is — the button showed "Q", while "A" is what must be pressed."""
    run_case(tmp_path, r"""
  const shown = (t) => t.row(P.ask).querySelector(".hk").textContent;
  let t = await boot({ values: { [P.ask]: "ctrl+alt+q" }, layout: new Map([["KeyQ", "a"], ["KeyA", "q"]]) });
  assert.equal(shown(t), "ctrl+alt+a");
  // Cyrillic (the Q position holds U+0439) and digits outside the letters stay as recorded
  t = await boot({ values: { [P.ask]: "ctrl+alt+q" }, layout: new Map([["KeyQ", "\u0439"]]) });
  assert.equal(shown(t), "ctrl+alt+q");
  // without a layout map (not Chromium): as recorded
  t = await boot({ values: { [P.ask]: "ctrl+alt+q" } });
  assert.equal(shown(t), "ctrl+alt+q");
""")


@needs_node
def test_the_resume_request_survives_the_page_closing(tmp_path):
    """The "restore the combinations" (and "release") request goes with keepalive: the page may
    be closed right after the assignment.
    """
    run_case(tmp_path, async_hotkey + r"""
  await start();
  press({ code: "KeyD", key: "d", ctrlKey: true, altKey: true });
  await t.env.settle();
  const holds = t.env.posts("/admin/hotkeys/capture");
  assert.deepEqual(holds.map((c) => c.body.on), [true, false]);
  assert.ok(holds.every((c) => c.init.keepalive === true), "the hotkeys request is without keepalive");
""")


# ---------------------------------------------------------------- microphone and window


@needs_node
def test_the_microphone_list_is_fresh_the_first_time_it_opens_and_the_same_pick_does_nothing(tmp_path):
    """fe-admin-chrome-15 and -8, together with `chrome.js`: the list is reread BEFORE it is
    shown (a headset was plugged in since last time), and a click on an already chosen
    microphone does not send `/control` (it does not reopen the audio in the middle of a
    conversation)."""
    run_case(tmp_path, r"""
  const t = await boot({ chrome: true });
  t.server.devices = { inputs: [{ name: "Mic A", default: true }, { name: "Headset B", default: false }], current: "Mic A", pinned: "" };
  const mic = t.root.querySelector("#ac-mic");
  assert.equal(typeof mic._beforeOpen, "function", "the microphone list has no reread before showing");
  t.env.fire(mic, t.env.ev("mousedown"));
  await t.env.settle();
  const menu = t.env.document.body.querySelector(".combo-menu");
  assert.ok(menu, "the list did not open");
  const labels = menu.querySelectorAll(".opt").map((o) => o.textContent);
  assert.ok(labels.some((l) => l.includes("Headset B")), "the new device did not make it into the first showing: " + labels);
  const picked = menu.querySelectorAll(".opt").find((o) => o.classList.contains("sel"));
  t.env.fire(picked, t.env.ev("mousedown"));
  await t.env.settle();
  assert.equal(t.env.posts("/control").length, 0, "choosing the same microphone reopened the audio");
  t.env.fire(mic, t.env.ev("mousedown"));
  await t.env.settle();
  const other = t.env.document.body.querySelectorAll(".combo-menu .opt").find((o) => o.textContent.includes("Headset B"));
  t.env.fire(other, t.env.ev("mousedown"));
  await t.env.settle();
  assert.deepEqual(t.env.posts("/control").map((c) => c.body), [{ mic_device: "Headset B" }]);
""")


@needs_node
def test_a_refused_hide_from_recording_is_visible_and_the_switch_follows_the_truth(tmp_path):
    """P5/P2: when the application refused, the "Invisible on recording"
    switch stayed silent and stayed as pressed; now — the reason under the row, the switch
    follows the real state, and the window bar gets an event."""
    run_case(tmp_path, r"""
  const t = await boot();
  const seen = [];
  t.env.window.addEventListener("argus-capture", (e) => seen.push(e.detail));
  t.server.capture = () => ({ body: { ok: false, excluded: false, detail: "window not found" } });
  const sw = t.root.querySelector("#wc-switch");
  t.env.fire(sw, t.env.ev("click"));
  await t.env.settle();
  assert.equal(sw.classList.contains("on"), false, "the switch stayed on");
  const err = t.root.querySelector("#wc-err");
  assert.equal(err.hidden, false);
  assert.equal(err.textContent, "window not found");
  const plain = (x) => JSON.parse(JSON.stringify(x));        // objects from the page belong to another world (realm) and are not compared strictly
  assert.deepEqual(plain(seen), [{ ok: false, wanted: true }]);
  t.server.capture = (b) => ({ body: { ok: true, excluded: b.excluded, detail: "" } });
  t.env.fire(sw, t.env.ev("click"));
  await t.env.settle();
  assert.equal(sw.classList.contains("on"), true);
  assert.equal(err.hidden, true);
  assert.deepEqual(plain(seen.at(-1)), { ok: true, wanted: true });
""")


@needs_node
def test_in_the_real_window_the_unsaved_bar_is_seen_on_every_tab_and_reopening_rereads_the_values(tmp_path):
    """The window as a whole, as in the application (`chrome.js` + `admin.js`, tabs on the
    left): the application's refusal is visible above ALL tabs, and reopening the window
    pulls in what was changed from outside (fe-admin-chrome-6 and -1)."""
    run_case(tmp_path, r"""
  const t = await boot({ window: true, sections: SECTIONS.filter((s) => s.id !== "window") });
  const win = t.win();
  assert.ok(win.querySelectorAll(".win-tab").length >= 5, "the settings tabs were not built");
  const shown = (el) => { for (let n = el; n && n.nodeType === 1; n = n.parentNode) if (n.hidden) return false; return true; };
  const bar = win.querySelector("#unsaved");
  assert.ok(bar && bar.hidden, "there is no 'not saved' bar in the window");
  // the app refuses; the person managed to move to another tab
  t.server.reject = (v) => (v[P.name] === "" ? { [P.name]: "the name cannot be empty" } : null);
  const name = win.querySelector(`.f[data-path="${P.name}"] input`);
  name.value = "";
  t.env.fire(name, t.env.ev("input"));
  win.querySelector('[data-tab="language"]').click();
  await t.env.advance(700);
  assert.equal(bar.hidden, false);
  assert.ok(shown(bar), "the 'not saved' bar is hidden along with the tab");
  assert.ok(win.querySelector("#unsaved-n").textContent.includes("the name cannot be empty"));
  // and the window leads to the rejected field: the tab with it is open again
  const current = win.querySelector('.win-tab[aria-selected="true"]');
  assert.equal(current.getAttribute("data-tab"), "context", "the window stayed on a tab without the rejected field");
  // closed, changed from outside, opened: the window shows the fresh value
  t.server.reject = null;
  win.querySelector(".win-close").click();
  await t.env.advance(200);
  t.server.values[P.think] = true;
  t.server.values[P.lang] = "en";
  await t.open();
  assert.equal(win.querySelector(`.f[data-path="${P.think}"] .switch`).classList.contains("on"), true,
    "the window showed the old value of 'Think longer'");
  assert.equal(win.querySelector(`.f[data-path="${P.lang}"] select`).value, "en");
""")


def test_the_unsaved_bar_is_part_of_the_window_shell_too():
    """The "not saved" bar is part of the window (above all tabs) and is on the
    real `/admin` too.
    """
    js = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    assert "if (WIN) return `${restart}${unsaved}<div id=\"main\"></div>`;" in js
    assert "${restart}${unsaved}\n    </header>" in js
    chrome = (STATIC_DIR / "chrome.js").read_text(encoding="utf-8")
    assert "window.argusAdmin?.refresh?.()" in chrome, "the window does not reread the values on opening"
