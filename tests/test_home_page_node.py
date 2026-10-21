"""The home page with the session list (`home.html`) in real execution: through `node`.

Its whole script is loaded into node with a fake DOM, the way `test_account_js_node.py`
loads `account.js`. What is pinned down is what the audit of 2026-10-14 found by
execution (fe-pages-8, 10, 12): the day headings and the service name "New session"
by page language, a click on a row with Ctrl/Shift and with the middle
button, and a dialog with a question that cannot be cancelled while the action is
running. No node: the checks are skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the JS of the page")

HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.env.HOME_HTML, "utf8");
const script = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/.exec(html)[1];

let unhandled = [];
process.on("unhandledRejection", (e) => { unhandled.push(String((e && e.message) || e)); });
const settle = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r)); };

function boot(lang, tr, chats, running) {
  const els = {};
  const docListeners = {};
  const location = { host: "127.0.0.1:8420", href: "" };
  const calls = [];
  const gates = {};                                         // "METHOD /path" -> function, returns the response later
  const el = (id) => {
    if (!els[id]) {
      const listeners = {};
      const cls = new Set();
      els[id] = {
        id, hidden: false, disabled: false, value: "", textContent: "", innerHTML: "", dataset: {}, style: {}, isConnected: true,
        classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
          toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); } },
        listeners, addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
        focus() { document.activeElement = this; }, select() {}, setAttribute() {}, removeAttribute() {},
        requestSubmit() { return this.onsubmit && this.onsubmit({ preventDefault() {} }); },
        closest() { return null; }, querySelectorAll() { return []; }, replaceChildren() {}, appendChild() {},
      };
    }
    return els[id];
  };
  const document = {
    getElementById: el, activeElement: null, hidden: false, body: { dataset: {} },
    addEventListener(t, f) { (docListeners[t] = docListeners[t] || []).push(f); },
    querySelector: () => el("__q"), querySelectorAll: () => [], createElement: () => el("__c"),
  };
  const fetch = async (url, init) => {
    const method = (init && init.method) || "GET";
    calls.push(`${method} ${url}`);
    const key = `${method} ${url}`;
    const body = gates[key] ? await gates[key]() : url === "/chats" ? { chats, running } : { ok: true };
    return { json: async () => body };
  };
  class WebSocket { constructor(u) { this.url = u; } }
  const window = { ARGUS_LANG: lang, addEventListener() {}, ...(tr ? { argusT: tr } : {}) };
  const api = new Function("window", "document", "fetch", "WebSocket", "CSS", "location",
    script + "\nreturn { dayLabel, chatName, dialogCancel: () => dialogCancel, rename, chats: () => chats, ask };")(
    window, document, fetch, WebSocket, { escape: (s) => s }, location);
  return { api, el, docListeners, location, calls, gates };
}

// The Russian translations (the page is translated from its English text):
const RU = { "Today": "Сегодня", "Yesterday": "Вчера", "New session": "Новая сессия" };
const tr = (s) => (RU[s] !== undefined ? RU[s] : s);
const hasCyr = (s) => /[А-Яа-яЁё]/.test(s);
const out = {};
const NOW = Math.floor(Date.now() / 1000);
const sec = (d) => Math.floor(d.getTime() / 1000);

(async () => {
  // ---- days and names (fe-pages-8) ----
  {
    const w = boot("ru", tr, [], false);
    const d = new Date();
    const yesterday = sec(new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1, 12));
    const old = sec(new Date(2020, 9, 5, 12));
    const wEn = boot("en", null, [], false);
    const wDe = boot("de", (s) => s, [], false);
    out.days = {
      today: w.api.dayLabel(NOW), yesterday: w.api.dayLabel(yesterday),
      ru_old: w.api.dayLabel(old), en_old: wEn.api.dayLabel(old), de_old: wDe.api.dayLabel(old),
      en_today: wEn.api.dayLabel(NOW),
    };
    out.names = {
      unnamed: w.api.chatName({ name: "New session", unnamed: true }),
      numbered: w.api.chatName({ name: "New session 2", unnamed: true }),
      own: w.api.chatName({ name: "New session", unnamed: false }),
      other: w.api.chatName({ name: "Interview", unnamed: true }),
      en: wEn.api.chatName({ name: "New session", unnamed: true }),
    };
  }

  // ---- click on a row (fe-pages-10) ----
  const chats = [{ id: "c1", name: "First", updated: NOW, active: false }, { id: "c2", name: "Second", updated: NOW, active: true }];
  const click = (extra) => {
    const card = { dataset: { id: "c1" } };
    let prevented = false;
    const e = { target: { closest: (sel) => (sel === ".chat" ? card : null) }, ctrlKey: false, metaKey: false, shiftKey: false,
      button: 0, preventDefault() { prevented = true; }, ...(extra || {}) };
    return { e, prevented: () => prevented };
  };
  {
    out.click = {};
    for (const [name, extra] of [["plain", {}], ["ctrl", { ctrlKey: true }], ["shift", { shiftKey: true }], ["meta", { metaKey: true }]]) {
      const w = boot("en", null, chats, false);
      await settle();
      const c = click(extra);
      w.el("list").listeners.click[0](c.e);
      await settle();
      out.click[name] = { prevented: c.prevented(), href: w.location.href, calls: w.calls.filter((x) => x.startsWith("GET /chat")) };
    }
    // recognition is running in another session: with Ctrl we also ask instead of navigating
    const w = boot("en", null, chats, true);
    await settle();
    const c = click({ ctrlKey: true });
    w.el("list").listeners.click[0](c.e);
    await settle();
    out.click.running = { prevented: c.prevented(), href: w.location.href, asking: w.el("dialog").hidden === false,
      title: w.el("dialog-title").textContent };
    // the middle button is `auxclick`
    const a = { target: { closest: (sel) => (sel === "a.open" ? {} : null) }, prevented: false, preventDefault() { this.prevented = true; } };
    w.el("list").listeners.auxclick[0](a);
    const b = { target: { closest: () => null }, prevented: false, preventDefault() { this.prevented = true; } };
    w.el("list").listeners.auxclick[0](b);
    out.aux = { link: a.prevented, elsewhere: b.prevented };
  }

  // ---- dialog with a question: it cannot be canceled while the action is running
  // (fe-pages-12) ----
  {
    const w = boot("en", null, chats, false);
    await settle();
    let release;
    w.gates["PATCH /chats/c1"] = () => new Promise((r) => { release = () => r({ ok: true }); });
    const listCalls = () => w.calls.filter((x) => x === "GET /chats").length;
    const before = listCalls();
    const done = w.api.rename(w.api.chats()[0]);
    await settle();
    const opened = w.el("dialog").hidden === false;
    w.el("dialog-input").value = "New name";
    w.el("dialog-form").onsubmit({ preventDefault() {} });
    await settle();
    // Esc, a click on the dimmed backdrop, "Cancel": none of these closes the window
    // while it waits for a response
    w.docListeners.keydown[0]({ key: "Escape" });
    w.el("dialog").listeners.mousedown[0]({ target: w.el("dialog") });
    w.el("dialog-cancel").onclick();
    const cancelDisabled = w.el("dialog-cancel").disabled;
    await settle();
    const stillOpen = w.el("dialog").hidden === false;
    release();
    await done;
    await settle();
    const patches = () => w.calls.filter((x) => x.startsWith("PATCH")).length;
    out.dialog = { opened, stillOpen, cancelDisabled, closed: w.el("dialog").hidden === true,
      reloaded: listCalls() > before, cancelReady: w.api.dialogCancel() === null, patches: patches() };
    // a normal cancel, when nothing is running, still works as before
    const second = w.api.rename(w.api.chats()[0]);
    await settle();
    w.docListeners.keydown[0]({ key: "Escape" });
    await second;
    out.dialog.plain_cancel = { closed: w.el("dialog").hidden === true, patches: patches() };
  }
  await settle();
  out.unhandled = unhandled;
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def seen() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is needed to run the JS of the page")
    done = subprocess.run(
        ["node", "-"], input=HARNESS, capture_output=True, text=True, encoding="utf-8", check=False,
        env={**os.environ, "HOME_HTML": str(STATIC / "home.html")},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --------------------------------------------------------------------------
# fe-pages-8: days and the "New session" name by page language
# --------------------------------------------------------------------------


@needs_node
def test_day_headings_follow_the_language_of_the_page(seen):
    days = seen["days"]
    assert days["today"] == "Сегодня" and days["yesterday"] == "Вчера", '"Today" and "Yesterday" go through argusT'
    assert "Oct" in days["en_old"] and "2020" in days["en_old"] and not re.search("[А-Яа-я]", days["en_old"]), days
    assert "окт" in days["ru_old"] and "2020" in days["ru_old"], "on a Russian page the heading stays Russian"
    assert days["en_today"] == "Today", "without the translation engine the line stays as it is"
    assert "Okt" in days["de_old"]


@needs_node
def test_the_default_session_name_is_shown_in_the_language_of_the_page(seen):
    names = seen["names"]
    assert names["unnamed"] == "Новая сессия" and names["numbered"] == "Новая сессия 2"
    assert names["own"] == "New session", "a name the person gave themselves is left alone, even if it is the same"
    assert names["other"] == "Interview" and names["en"] == "New session"


# --------------------------------------------------------------------------
# fe-pages-10: a click on a row always goes through `openChat`
# --------------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize("how", ["plain", "ctrl", "shift", "meta"])
def test_a_click_on_a_row_never_follows_the_link_by_itself(seen, how):
    """Following the link opens the session with a GET request and stops recognition in the
    previous one, bypassing the question.
    """
    got = seen["click"][how]
    assert got["prevented"], "a click with a modifier key left the link to the browser"
    assert got["href"] == "/chat/c1"


@needs_node
def test_a_click_with_ctrl_still_asks_before_stopping_the_recognition(seen):
    got = seen["click"]["running"]
    assert got["prevented"] and got["asking"] and got["href"] == "", got
    assert got["title"] == "Stop recognition?"


@needs_node
def test_the_middle_button_on_the_name_does_not_open_a_new_window(seen):
    assert seen["aux"] == {"link": True, "elsewhere": False}


# --------------------------------------------------------------------------
# fe-pages-12: the dialog with a question
# --------------------------------------------------------------------------


@needs_node
def test_the_dialog_cannot_be_cancelled_while_its_request_runs(seen):
    got = seen["dialog"]
    assert got["opened"] and got["stillOpen"], "Esc, the dimmed backdrop or \"Cancel\" closed the window in the middle of a request"
    assert got["cancelDisabled"]
    assert got["closed"] and got["patches"] == 1, got
    assert got["reloaded"], "after the action the list is read again: a renamed or deleted item shows at once"
    assert got["cancelReady"]


@needs_node
def test_the_dialog_can_still_be_cancelled_when_nothing_is_running(seen):
    assert seen["dialog"]["plain_cancel"] == {"closed": True, "patches": 1}
    assert not seen["unhandled"], seen["unhandled"]
