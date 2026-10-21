"""The working screen (`index.html`) without a browser: the whole page script runs in `node`
on a fake environment.

Before this, neither `ask()`, nor `pendingAsk`, nor the paste handler, nor the socket
closing had executable checks: the tests `test_question_arrow.py` and `test_shot_view.py`
search for strings in the page text (audit of 2026-10-14, fe-index-b-7), and an "eternal
thinking card" with an erased draft passed a green suite. Here the page runs whole, as the
browser sees it, with the same handlers and the same `render`/`renderChat`/`ask`, and only
the edges are replaced: a document with stub elements, `fetch` with answers set by the
test, a socket, a clock and timers. If there is a typo at the top level of the script or a
reference to something not yet declared, execution fails at once.

What is pinned down: a refusal to a question is visible with its reason (not an eternal
"thinking") and returns the typed text to the field; "again" and "?" do not
occupy the input field; Enter does not breed requests on top of one in progress; "Think
longer" is read before every question; paste does not touch other fields or
Excel text; a socket drop is visible, and the frame stream opens again; a `/control`
refusal does not break the panel; window choice; keyboard buttons; POST bodies.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the JS of the page"
)


def page_script(root=STATIC) -> str:
    """The page's main script: from `const $ = …` to the closing `</script>`."""
    html = (root / "index.html").read_text(encoding="utf-8")
    start = html.index("<script>\nconst $ = (id) => document.getElementById(id);")
    end = html.index("</script>", start)
    return html[start + len("<script>\n") : end]


# A fake environment. An element is an object with real `value`/`hidden`/`classList`/
# attributes and handler recording; everything else the page touches on the way is empty
# methods. The clock and timers are our own: `advance(ms)` moves time.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const clock = { t: 1700000000000 };
const timers = [];
let timerSeq = 0;
const fakeSetTimeout = (fn, ms) => { const id = ++timerSeq; timers.push({ id, at: clock.t + (ms || 0), fn, live: true }); return id; };
const fakeClearTimeout = (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; };
function advance(ms) {
  const end = clock.t + ms;
  for (;;) {
    const due = timers.filter((t) => t.live && t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    clock.t = Math.max(clock.t, due.at);
    due.live = false;
    due.fn();
  }
  clock.t = end;
}
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };

const listeners = new Map();
const addListener = (id, type, fn) => { const k = id + ":" + type; if (!listeners.has(k)) listeners.set(k, []); listeners.get(k).push(fn); };
async function fire(id, type, evt) {
  const out = [];
  for (const fn of listeners.get(id + ":" + type) || []) out.push(fn(evt));
  await Promise.all(out);
}

const decode = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
function makeEl(id) {
  const attrs = {};
  const classes = new Set();
  const el = {
    id, value: "", checked: false, disabled: false, hidden: false, placeholder: "", innerHTML: "",
    dataset: {}, style: {}, children: [], childNodes: [], scrollHeight: 0, scrollTop: 0, clientHeight: 0,
    classList: {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { const want = on === undefined ? !classes.has(c) : !!on; if (want) classes.add(c); else classes.delete(c); return want; },
    },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    removeAttribute: (k) => { delete attrs[k]; },
    hasAttribute: (k) => k in attrs,
    toggleAttribute: () => {},
    addEventListener: (type, fn) => addListener(id, type, fn),
    closest: () => null, querySelector: () => null, querySelectorAll: () => [], contains: () => false,
    focus() { el.focused = true; }, blur() {}, scrollIntoView() {}, remove() {}, append() {}, prepend() {}, appendChild() {}, before() {},
    click() { return el.onclick ? el.onclick({ target: el }) : undefined; },
    _attrs: attrs, _classes: classes,
  };
  el.textContent = "";
  // as in a real DOM: `img.src = …` sets the `src` attribute
  Object.defineProperty(el, "src", { get: () => (("src" in attrs) ? attrs.src : ""), set: (v) => { attrs.src = String(v); } });
  return el;
}
const elements = new Map();
const getEl = (id) => { if (!elements.has(id)) elements.set(id, makeEl(id)); return elements.get(id); };

const document = makeEl("#document");
Object.assign(document, {
  getElementById: getEl, readyState: "complete", visibilityState: "visible", title: "",
  body: makeEl("body"), documentElement: makeEl("html"), head: makeEl("head"),
  querySelector: () => makeEl("any"),
  createElement: () => {
    const e = makeEl("<new>");
    Object.defineProperty(e, "textContent", { get() { return decode(e.innerHTML.replace(/<[^>]+>/g, "")); }, set(v) { e.innerHTML = String(v); } });
    // the window strip is built from `innerHTML` and finds its buttons by class: we hand out
    // one stub per selector
    const subs = {};
    e.querySelector = (sel) => {
      if (!e.innerHTML.includes(sel.replace(/^[.#]/, ""))) return null;
      return subs[sel] || (subs[sel] = makeEl("sub:" + sel));
    };
    return e;
  },
});

// network: `routes[address](body) -> { status?, body?, text? }`; `calls` is everything that
// went out
const calls = [];
const routes = {};
const fakeFetch = async (url, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({ url, method: init.method || "GET", body, headers: init.headers || {}, rawBody: init.body });
  const route = routes[url] || routes[String(url).split("?")[0]];
  if (!route) return { ok: false, status: 404, json: async () => ({ detail: "no such route" }) };
  const res = await route(body, init);
  const status = res.status ?? 200;
  return {
    ok: status >= 200 && status < 300, status,
    json: async () => { if (res.text !== undefined) throw new SyntaxError("not JSON"); return JSON.parse(JSON.stringify(res.body)); },
  };
};

class FakeWS {
  constructor(url) { this.url = url; FakeWS.all.push(this); }
  addEventListener(type, fn) { addListener("ws" + (FakeWS.all.indexOf(this)), type, fn); }
}
FakeWS.all = [];
const ws = () => FakeWS.all[FakeWS.all.length - 1];
async function openWS(sock) {
  if (sock.onopen) sock.onopen();
  await fire("ws" + FakeWS.all.indexOf(sock), "open", {});
}

const temmlSrc = fs.readFileSync(TEMML_PATH, "utf8");
const temml = new Function(temmlSrc + "\nreturn temml;")();

class FakeDate extends Date { static now() { return clock.t; } }
const sandbox = {
  console, document, fetch: fakeFetch, WebSocket: FakeWS, Date: FakeDate, temml,
  setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout, setInterval: () => 0, clearInterval: () => {},
  requestAnimationFrame: (fn) => fakeSetTimeout(fn, 16),
  navigator: {}, location: { pathname: PATHNAME, search: "", hash: "", host: "127.0.0.1:8420" },
  localStorage: { getItem: () => null, setItem: () => {} }, history: { replaceState() {} },
  MutationObserver: class { observe() {} disconnect() {} },
  Audio: class { play() { return Promise.resolve(); } pause() {} removeAttribute() {} load() {} },
  alert: () => {}, innerWidth: 900, innerHeight: 700,
};
sandbox.window = sandbox;
sandbox.addEventListener = (type, fn) => addListener("window", type, fn);
if (WITH_APP) sandbox.pywebview = {};
const ctx = vm.createContext(sandbox);
const ev = (code) => vm.runInContext(code, ctx);
const $el = getEl;

routes["/admin/schema"] = async () => ({ body: { values: { "channel.assist.answering.think_longer": false }, sections: [] } });
routes["/audio/devices"] = async () => ({ body: { inputs: [], current: "", pinned: "" } });

vm.runInContext(PAGE, ctx, { filename: "index.html" });

const snap = (over = {}) => Object.assign({
  controls: { running: false, sources: { mic: true, loopback: true, screen: false }, loopback_windows: [] },
  problems: [], account_hint: "", answers: [], thinking: null, shots: [], speech: [], speech_b: [], past_speech: [],
  levels: { me: { rms: 0 }, them: { rms: 0 } }, engines: {}, admin: true, chat: null, can_paste_image: true,
  notice_ask: false,
}, over);
const push = async (over) => { const s = snap(over); ws().onmessage({ data: JSON.stringify(s) }); await settle(); return s; };
const answer = (text, extra = {}) => Object.assign({ text, latency_ms: 120, error: "", cause: "", image_kb: 0, audio_ids: [] }, extra);
const chat = () => $el("chat").innerHTML;
"""


def run(body: str, *, solo: str = "", app: bool = False, root=STATIC):
    """Run `body` (the body of an async JS function) on top of the page and return what it
    returned.

    `solo` is a detached window (`/panel/<name>`), `app` is a page inside the app window
    (`pywebview` is present)."""
    pathname = f"/panel/{solo}" if solo else "/chat/abc"
    script = (
        f"const TEMML_PATH = {json.dumps(str(root / 'vendor/temml/temml.min.js'))};\n"
        f"const PAGE = {json.dumps(page_script(root))};\n"
        f"const PATHNAME = {json.dumps(pathname)};\n"
        f"const WITH_APP = {'true' if app else 'false'};\n"
        + HARNESS
        + "\n(async () => {\n" + body + "\n})().then((r) => console.log(JSON.stringify(r)),"
        + " (e) => { console.error(e && e.stack || e); process.exit(2); });\n"
    )
    done = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --------------------------------------------------------------------------
# The page starts at all
# --------------------------------------------------------------------------


@needs_node
def test_the_whole_page_script_runs_to_the_end_and_draws_a_snapshot():
    """A typo, or a reference to something not yet declared at the top level, brings down the whole
    script, and all the buttons with it. Here it runs to the end and draws a snapshot with one
    answer."""
    got = run("""
      await settle();
      await push({ answers: [answer("**Done**")], controls: { running: true, sources: { mic: true, loopback: false, screen: true }, loopback_windows: [] } });
      return { chat: chat(), checked: $el("power")._attrs["aria-checked"], on: $el("power").classList.contains("on") };
    """)
    assert "<b>Done</b>" in got["chat"]
    assert got["checked"] == "true" and got["on"] is True


# --------------------------------------------------------------------------
# ask(): a refusal is visible, the field and the photo come back (fe-index-b-1)
# --------------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    ("reply", "shown"),
    [
        ({"body": {"text": "", "error": "the assistant is still answering"}}, "the assistant is still answering"),
        ({"body": {"text": "", "error": "the request ceiling is exhausted"}}, "the request ceiling is exhausted"),
        ({"status": 500, "text": "Internal Server Error"}, "Didn't work — try again"),
        ({"status": 422, "body": {"detail": [{"msg": "field required"}]}}, "Didn't work — try again"),
        ({"status": 413, "body": {"detail": "the photo is too large"}}, "the photo is too large"),
    ],
)
def test_a_refused_question_shows_the_reason_instead_of_thinking_forever(reply, shown):
    """The server refuses with code 200 and an `error` (the request ceiling, "the assistant is still
    answering") or with an HTTP error, and the failed answer does not
    reach the feed: without reading the answer, the "thinking" card stayed forever."""
    got = run("""
      routes["/ask"] = async () => (REPLY);
      await push({});
      $el("q").value = "what is an index?";
      await ev("ask('/ask', null)");
      await settle();
      await push({});                                   // snapshot after snapshot, the state is the same
      await push({}); await push({}); await push({});
      return { chat: chat(), q: $el("q").value, disabled: $el("ask").disabled };
    """.replace("REPLY", json.dumps(reply)))
    assert "thinking" not in got["chat"], "the thinking card is still there"
    assert shown in got["chat"] and "what is an index?" in got["chat"]
    assert 'io output err' in got["chat"]
    assert got["q"] == "what is an index?", "the typed text did not return to the field"
    assert got["disabled"] is False


@needs_node
def test_a_refusal_because_the_assistant_is_busy_stands_beside_the_thinking_card_until_an_answer_comes():
    """Most often the refusal is "the assistant is still answering" to the
    previous question: the previous card keeps going, and the reason for refusing the new one
    stands under it and leaves together with the new answer in the feed."""
    got = run("""
      const busy = { t: 1, cause: "first question", reason: "", kind: "ask" };
      await push({ thinking: busy });
      routes["/ask"] = async () => ({ body: { text: "", error: "the assistant is still answering" } });
      $el("q").value = "second question";
      await ev("ask('/ask', null)"); await settle();
      await push({ thinking: busy });
      const both = chat();
      await push({ answers: [answer("ready", { cause: "first question" })] });
      return { both, after: chat() };
    """)
    assert "thinking" in got["both"] and "first question" in got["both"]
    assert "the assistant is still answering" in got["both"] and "second question" in got["both"]
    assert "the assistant is still answering" not in got["after"] and "ready" in got["after"]


@needs_node
def test_a_lost_connection_to_the_app_is_a_reason_too():
    got = run("""
      routes["/ask"] = async () => { throw new TypeError("Failed to fetch"); };
      await push({});
      $el("q").value = "hello";
      await ev("ask('/ask', null)");
      await settle();
      return { chat: chat(), q: $el("q").value };
    """)
    assert "Can't reach the app" in got["chat"] and "thinking" not in got["chat"]
    assert got["q"] == "hello"


@needs_node
def test_a_failure_while_drawing_the_question_does_not_leave_the_ask_button_locked():
    """The button is disabled right before the request: a failure while drawing the question above
    it must not leave the button disabled until the page is reloaded."""
    got = run("""
      await push({});
      routes["/ask"] = async () => ({ body: { text: "", error: "" } });
      ev("state = { controls: null, answers: null }");           // rendering the utterance will fail
      $el("q").value = "question";
      try { await ev("ask('/ask', null)"); } catch (e) { /* that is how it should be */ }
      return { disabled: $el("ask").disabled };
    """)
    assert got == {"disabled": False}


@needs_node
def test_a_refused_question_gives_the_photo_back_and_does_not_overwrite_a_new_draft():
    got = run("""
      routes["/ask"] = async () => ({ body: { text: "", error: "the assistant is still answering" } });
      await push({});
      ev("setAttached({ url: 'data:image/jpeg;base64,QUJD', b64: 'QUJD' })");
      $el("q").value = "what is in the photo?";
      const photo = ev("takeAttached()");
      // while waiting for the refusal, the person started a new question
      routes["/ask"] = async () => { $el("q").value = "typing something else already"; return { body: { text: "", error: "the assistant is still answering" } }; };
      await ev(`ask('/ask', ${JSON.stringify(photo)})`);
      await settle();
      return { q: $el("q").value, attach: ev("attached") && ev("attached.b64"), hidden: $el("attach").hidden };
    """)
    assert got["q"] == "typing something else already", "the new typed text was overwritten by the returned one"
    assert got["attach"] == "QUJD" and got["hidden"] is False, "the photo did not come back"


@needs_node
def test_the_thinking_card_goes_away_when_the_list_is_full_and_the_answer_replaces_the_oldest():
    """The feed keeps the last 20: when it is full, the number of answers does not grow, and
    comparing lengths did not remove the entry.
    """
    got = run("""
      const old = Array.from({ length: 20 }, (_, i) => answer("old " + i, { latency_ms: 100 + i }));
      await push({ answers: old });
      routes["/ask"] = async () => ({ body: { text: "new", error: "" } });
      $el("q").value = "question";
      const p = ev("ask('/ask', null)");
      await p; await settle();
      const during = chat();                             // the request is over, the snapshot is still old
      const fresh = [...old.slice(1), answer("new answer", { latency_ms: 777, cause: "question" })];
      await push({ answers: fresh });                    // the length is the same, 20, but the last one is different
      return { during, after: chat() };
    """)
    assert "thinking" in got["during"], "the entry vanished before the snapshot with the answer, so the card would have flickered"
    assert "thinking" not in got["after"] and "new answer" in got["after"]


@needs_node
def test_the_thinking_card_does_not_outlive_a_finished_request_by_more_than_two_seconds():
    got = run("""
      await push({ answers: [answer("earlier")] });
      routes["/ask"] = async () => ({ body: { text: "", error: "" } });     // success, but the feed has nothing to show
      $el("q").value = "question";
      await ev("ask('/ask', null)"); await settle();
      await push({ answers: [answer("earlier")] });
      const soon = chat();
      advance(2100);
      await push({ answers: [answer("earlier")] });
      return { soon, later: chat() };
    """)
    assert "thinking" in got["soon"] and "thinking" not in got["later"]


# --------------------------------------------------------------------------
# The question field is not occupied by "again" and "?" (fe-index-b-2)
# --------------------------------------------------------------------------


def _retry_click(idx: int = 0) -> str:
    return f"""
      const retry = {{ disabled: false, classList: {{ contains: (c) => c === "retry" }},
                      closest: (s) => (s === ".turn" ? {{ dataset: {{ idx: "{idx}" }} }} : null) }};
      const evt = {{ target: {{ closest: (s) => (s === ".actions button" ? retry : null) }} }};
    """


@needs_node
def test_again_does_not_touch_the_question_field_and_sends_the_question_of_the_card():
    got = run(_retry_click() + """
      await push({ answers: [answer("a reply", { cause: "what is two plus two?" })] });
      let release;
      routes["/ask"] = () => new Promise((res) => { release = () => res({ body: { text: "", error: "" } }); });
      $el("q").value = "draft of the next question";
      const p = fire("chat", "click", evt);
      await settle();
      $el("q").value = "draft of the next question, finished while waiting";
      const during = { disabledBtn: retry.disabled };
      release(); await p; await settle();
      const sent = calls.filter((c) => c.url === "/ask").map((c) => c.body.question);
      return { q: $el("q").value, sent, during, btn: retry.disabled };
    """)
    assert got["q"] == "draft of the next question, finished while waiting"
    assert got["sent"] == ["what is two plus two?"]
    assert got["during"]["disabledBtn"] is True and got["btn"] is False


@needs_node
def test_again_on_an_answer_without_a_real_question_asks_by_hand_with_no_text():
    got = run(_retry_click() + """
      await push({ answers: [answer("a reply", { cause: "asked manually" })] });
      routes["/ask"] = async () => ({ body: { text: "", error: "" } });
      $el("q").value = "mine";
      await fire("chat", "click", evt); await settle();
      return { sent: calls.filter((c) => c.url === "/ask").map((c) => c.body.question), q: $el("q").value };
    """)
    assert got["sent"] == [None] and got["q"] == "mine"


@needs_node
def test_the_question_mark_beside_a_line_asks_about_it_and_leaves_the_field_alone():
    got = run("""
      await push({ speech: [{ t: 12.5, source: "them", text: "and how much does it cost?", confidence: 0.9 }] });
      let release;
      routes["/ask"] = () => new Promise((res) => { release = () => res({ body: { text: "", error: "" } }); });
      $el("q").value = "my unfinished question";
      const row = { classList: { contains: () => false }, dataset: { t: "12.5" } };
      const btn = { disabled: false, closest: (s) => (s === ".row" ? row : null) };
      const p = fire("raw", "click", { target: { closest: (s) => (s === ".ask-raw" ? btn : null) } });
      await settle();
      $el("q").value = "my unfinished question, finished while waiting";
      release(); await p; await settle();
      return { sent: calls.filter((c) => c.url === "/ask").map((c) => c.body.question), q: $el("q").value, btn: btn.disabled };
    """)
    assert got["sent"] == ["and how much does it cost?"]
    assert got["q"] == "my unfinished question, finished while waiting" and got["btn"] is False


# --------------------------------------------------------------------------
# Enter does not breed requests (fe-index-b-1)
# --------------------------------------------------------------------------


@needs_node
def test_enter_sends_once_and_ignores_repeat_composition_and_a_request_in_flight():
    got = run("""
      await push({});
      let release;
      routes["/ask"] = () => new Promise((res) => { release = () => res({ body: { text: "", error: "" } }); });
      const key = (over) => fire("q", "keydown", Object.assign({ key: "Enter", repeat: false, isComposing: false }, over));
      $el("q").value = "first";
      await key({ repeat: true }); await key({ isComposing: true }); await key({ key: "a" });
      const none = calls.filter((c) => c.url === "/ask").length;
      await key({}); await settle();
      $el("q").value = "second";
      await key({}); await settle();                    // the previous one is still on its way
      const inFlight = calls.filter((c) => c.url === "/ask").length;
      release(); await settle();
      return { none, inFlight, q: $el("q").value };
    """)
    assert got["none"] == 0
    assert got["inFlight"] == 1, "Enter on top of a question in flight sent a second one"
    assert got["q"] == "second"


# --------------------------------------------------------------------------
# "Think longer" is read before every question (fe-index-b-3, fe-admin-chrome-1)
# --------------------------------------------------------------------------


@needs_node
def test_think_longer_is_read_from_the_server_before_every_question():
    got = run("""
      await push({});
      let deep = true;
      routes["/admin/schema"] = async () => ({ body: { values: { "channel.assist.answering.think_longer": deep }, sections: [] } });
      routes["/ask"] = async () => ({ body: { text: "", error: "" } });
      const ask1 = async (q) => { $el("q").value = q; await ev("ask('/ask', null)"); await settle(); advance(2100); };
      await ask1("one");
      deep = false;                                     // turned off in the settings window
      await ask1("two");
      routes["/admin/schema"] = async () => ({ status: 404, body: { detail: "no" } });
      await ask1("three");                                // the settings server did not respond: the last known value stays
      return calls.filter((c) => c.url === "/ask").map((c) => c.body.deep);
    """)
    assert got == [True, False, False]


# --------------------------------------------------------------------------
# Paste from the clipboard (fe-index-b-5, fe-index-b-6)
# --------------------------------------------------------------------------


def _paste_event(*, target: str, text: str = "", image: bool = True) -> str:
    return f"""
      const prevented = [];
      const evt = {{
        target: {{ closest: (s) => ({target}) }},
        clipboardData: {{
          getData: (t) => (t === "text/plain" ? {json.dumps(text)} : ""),
          items: {"[{ kind: 'file', type: 'image/png', getAsFile: () => ({}) }]" if image else "[]"},
        }},
        preventDefault: () => prevented.push(1),
      }};
    """


@needs_node
def test_a_picture_on_the_clipboard_becomes_an_attachment_when_it_is_only_a_picture():
    got = run(_paste_event(target="null") + """
      await push({});
      ev("jpegFromBlob = async () => ({ url: 'data:image/jpeg;base64,QUJD', b64: 'QUJD' })");
      await fire("#document", "paste", evt); await settle();
      return { prevented: prevented.length, attached: ev("attached && attached.b64") };
    """)
    assert got == {"prevented": 1, "attached": "QUJD"}


@needs_node
def test_pasting_cells_or_text_that_carry_a_picture_pastes_the_text_not_the_picture():
    """Excel and Word put both text and a snapshot picture into the clipboard: the paid model
    would receive the picture instead of the text.
    """
    got = run(_paste_event(target="null", text="A1\\tB1") + """
      await push({});
      ev("jpegFromBlob = async () => ({ url: 'data:image/jpeg;base64,QUJD', b64: 'QUJD' })");
      await fire("#document", "paste", evt); await settle();
      return { prevented: prevented.length, attached: ev("attached") };
    """)
    assert got == {"prevented": 0, "attached": None}


@needs_node
def test_pasting_into_another_field_is_left_alone():
    """Pasting into a field of the settings window or the recap topics must not take the photo and
    the focus away from the question line.
    """
    got = run(_paste_event(target="s.startsWith('input') ? { id: 'recap-topics' } : null") + """
      await push({});
      ev("jpegFromBlob = async () => ({ url: 'data:image/jpeg;base64,QUJD', b64: 'QUJD' })");
      await fire("#document", "paste", evt); await settle();
      return { prevented: prevented.length, attached: ev("attached"), focused: !!$el("q").focused };
    """)
    assert got == {"prevented": 0, "attached": None, "focused": False}


@needs_node
def test_pasting_into_the_question_field_itself_still_attaches_the_picture():
    got = run(_paste_event(target="s.startsWith('input') ? $el('q') : null") + """
      await push({});
      ev("jpegFromBlob = async () => ({ url: 'data:image/jpeg;base64,QUJD', b64: 'QUJD' })");
      await fire("#document", "paste", evt); await settle();
      return { prevented: prevented.length, attached: ev("attached && attached.b64") };
    """)
    assert got == {"prevented": 1, "attached": "QUJD"}


@needs_node
def test_two_failed_pastes_in_a_row_leave_the_real_placeholder_behind():
    got = run(_paste_event(target="null") + """
      await push({});
      $el("q").placeholder = "Ask your own question…";
      ev("jpegFromBlob = async () => { throw new Error('broken picture'); }");
      await fire("#document", "paste", evt); await settle();
      const first = $el("q").placeholder;
      advance(1000);
      await fire("#document", "paste", evt); await settle();       // second failure while the first is still pending
      advance(2500);
      const mid = $el("q").placeholder;                            // the first one's timer has already elapsed
      advance(1000);
      return { first, mid, last: $el("q").placeholder };
    """)
    assert got["first"] == "Couldn't paste the image"
    assert got["last"] == "Ask your own question…", "the error message stayed as the hint forever"


# --------------------------------------------------------------------------
# Control panel: a /control refusal, the "Reconnect audio" button
# (fe-index-a-16, fe-index-a-13)
# --------------------------------------------------------------------------


@needs_node
def test_a_refused_control_is_explained_and_does_not_break_the_panel():
    got = run("""
      await push({ controls: { running: true, sources: { mic: true, loopback: true, screen: false }, loopback_windows: [] } });
      routes["/control"] = async () => ({ status: 500, text: "Internal Server Error" });
      await ev("control({ reopen_audio: true })"); await settle();
      const a = { text: $el("alarm-text").innerHTML, hidden: $el("alarm").hidden };
      routes["/control"] = async () => ({ status: 500, body: { detail: "no input device" } });
      await ev("control({ reopen_audio: true })"); await settle();
      const b = $el("alarm-text").innerHTML;
      routes["/control"] = async () => ({ body: { detail: "no input" } });          // 200, but without sources
      await ev("control({ mic: false })"); await settle();
      const kept = ev("state.controls.running");
      routes["/control"] = async () => { throw new TypeError("Failed to fetch"); };
      await ev("control({ mic: false })"); await settle();
      const c = $el("alarm-text").innerHTML;
      return { a, b, kept, c };
    """)
    assert got["a"] == {"text": "Didn't work — try again", "hidden": False}
    assert got["b"] == "no input device"
    assert got["kept"] is True, "a reply without sources was put into the panel state"
    assert "Can't reach the app" in got["c"]


@needs_node
def test_the_message_stays_until_it_expires_then_the_snapshot_clears_the_alarm():
    got = run("""
      await push({});
      routes["/control"] = async () => ({ status: 500, text: "x" });
      await ev("control({ mic: false })"); await settle();
      await push({});                                   // a healthy snapshot: it used to hide the window immediately
      const during = $el("alarm").hidden;
      advance(8100);
      await push({});
      return { during, after: $el("alarm").hidden };
    """)
    assert got == {"during": False, "after": True}


@needs_node
def test_the_reopen_button_shows_only_next_to_an_audio_problem():
    got = run("""
      const rows = {};
      await push({ problems: ["the Ctrl+Alt+A shortcut is taken by another program"], audio_problem: false });
      rows.hotkey = $el("reopen").hidden;
      await push({ problems: ["audio: stream stopped"], audio_problem: true });
      rows.audio = $el("reopen").hidden;
      await push({ problems: ["audio: stream stopped"] });          // old server: no flag
      rows.legacy = $el("reopen").hidden;
      await push({ account_hint: "you need to sign in — it is in the settings" });
      rows.hint = $el("reopen").hidden;
      return rows;
    """)
    assert got == {"hotkey": True, "audio": False, "legacy": False, "hint": True}


@needs_node
def test_the_account_toast_is_kept_for_a_while_instead_of_vanishing_with_the_next_snapshot():
    got = run("""
      await push({});
      ev("window.argusAccountHooks.toast('You are signed in')");
      await push({});
      const kept = { text: $el("alarm-text").innerHTML, hidden: $el("alarm").hidden };
      ev("window.argusAccountHooks.onChange()");
      return { kept, cleared: $el("alarm").hidden };
    """)
    assert got["kept"] == {"text": "You are signed in", "hidden": False}
    assert got["cleared"] is True


# --------------------------------------------------------------------------
# Connection (fe-index-a-7)
# --------------------------------------------------------------------------


@needs_node
def test_a_dropped_socket_is_visible_and_the_screen_stream_is_reopened_after_it_returns():
    got = run("""
      const first = ws();
      await openWS(first);
      await push({ controls: { running: true, sources: { mic: true, loopback: true, screen: true }, loopback_windows: [] } });
      const stream0 = $el("shot").src;
      first.onclose(); advance(500);
      const early = $el("alarm").hidden;                // the server restart is short: we do not raise an alarm
      advance(1200);
      const lost = { hidden: $el("alarm").hidden, text: $el("alarm-text").innerHTML, offline: document.body.classList.contains("offline") };
      first.onclose(); advance(1000);                    // failed retries do not overwrite the message
      const stillLost = $el("alarm").hidden;
      await openWS(ws());
      const beforeSnapshot = $el("shot").src;
      await push({ controls: { running: true, sources: { mic: true, loopback: true, screen: true }, loopback_windows: [] } });
      const reopened = $el("shot").src;                  // first snapshot of the new socket
      return { stream0, early, lost, stillLost, beforeSnapshot, reopened, back: { hidden: $el("alarm").hidden, offline: document.body.classList.contains("offline") } };
    """)
    assert got["stream0"] == "/screen.mjpg"
    assert got["early"] is True
    assert got["lost"] == {"hidden": False, "text": "Can't reach the app — reconnecting…", "offline": True}
    assert got["stillLost"] is False
    assert got["beforeSnapshot"] == "/screen.mjpg"
    assert got["reopened"].startswith("/screen.mjpg?"), "the frame stream was not opened again"
    assert got["back"] == {"hidden": True, "offline": False}


@needs_node
def test_closing_the_socket_because_the_page_is_leaving_is_not_a_dropped_link():
    got = run("""
      const first = ws();
      await openWS(first); await push({});
      await fire("window", "pagehide", {});
      first.onclose(); advance(3000);
      return { hidden: $el("alarm").hidden, offline: document.body.classList.contains("offline") };
    """)
    assert got == {"hidden": True, "offline": False}


# --------------------------------------------------------------------------
# Screen reader (css-design-3)
# --------------------------------------------------------------------------


@needs_node
def test_the_screen_reader_hears_only_a_new_finished_answer_not_the_history_or_a_streaming_one():
    got = run("""
      const old = answer("old answer", { latency_ms: 100 });
      await push({ answers: [old] });
      const history = $el("chat-live").textContent;
      await push({ answers: [old, answer("", { latency_ms: 0 })] });                 // the streaming one is still going
      const streaming = $el("chat-live").textContent;
      await push({ answers: [old, answer("**New** answer", { latency_ms: 250 })] });
      const fresh = $el("chat-live").textContent;
      await push({ answers: [old, answer("**New** answer", { latency_ms: 250 })] });
      const again = $el("chat-live").textContent;
      await push({ answers: [old, answer("**New** answer", { latency_ms: 250 }), answer("", { latency_ms: 90, error: "the assistant is still answering" })] });
      return { history, streaming, fresh, again, error: $el("chat-live").textContent };
    """)
    assert got["history"] == "" and got["streaming"] == ""
    assert got["fresh"] == "New answer" and got["again"] == "New answer"
    assert got["error"] == "the assistant is still answering"


@needs_node
def test_enter_and_space_press_the_switch_and_the_source_parts_but_nothing_else():
    got = run("""
      const pressed = [];
      const part = (name) => ({ click: () => pressed.push(name) });
      const hit = (name) => (s) => (s.includes("#power") ? part(name) : null);
      const key = (k, over = {}) => fire("#document", "keydown", Object.assign({ key: k, repeat: false, target: { closest: hit("power") }, preventDefault: () => pressed.push("prevented") }, over));
      await key("Enter"); await key(" "); await key("a"); await key("Enter", { repeat: true });
      await key("Enter", { target: { closest: () => null } });
      return pressed;
    """)
    assert got == ["prevented", "power", "prevented", "power"]


# --------------------------------------------------------------------------
# Window choice (fe-index-a-14, fe-index-a-15, fe-index-a-16)
# --------------------------------------------------------------------------

WINDOWS = {
    "windows": [
        {"title": "", "label": "Whole screen", "width": 0, "height": 0, "thumb": ""},
        {"title": "Chrome", "label": "Chrome", "width": 1920, "height": 1080, "thumb": ""},
    ]
}


@needs_node
def test_the_audio_picker_has_no_whole_screen_card_and_no_card_shows_sizes_or_counts():
    got = run("""
      routes["/windows"] = async () => ({ body: WINDOWS });
      await push({ controls: { running: true, sources: { mic: true, loopback: true, screen: true }, loopback_windows: ["Chrome", "Zoom"] } });
      await ev("openPicker('audio')"); await settle();
      const audio = $el("cards").innerHTML;
      await ev("openPicker('screen')"); await settle();
      const screen = $el("cards").innerHTML;
      routes["/windows"] = async () => ({ status: 500, text: "x" });
      await ev("openPicker('screen')"); await settle();
      return { audio, screen, failed: $el("cards").innerHTML };
    """.replace("WINDOWS", json.dumps(WINDOWS)))
    assert "Chrome" in got["audio"] and "Whole screen" not in got["audio"]
    assert "Whole screen" in got["screen"] and "Chrome" in got["screen"]
    for html in (got["audio"], got["screen"]):
        assert "1920" not in html and "whole monitor" not in html and "<small>" not in html
    assert "Couldn't get the window list" in got["failed"] and "No windows found" not in got["failed"]


@needs_node
def test_a_failed_device_list_does_not_throw_an_unhandled_error():
    got = run("""
      routes["/audio/devices"] = async () => ({ status: 500, body: { detail: "x" } });
      await ev("loadMics()"); await settle();
      return "ok";
    """)
    assert got == "ok"


# --------------------------------------------------------------------------
# POST bodies and the frame stream (addition to the task, fe-index-b-8)
# --------------------------------------------------------------------------


@needs_node
def test_the_notice_and_the_recap_post_a_json_body():
    """The local server does not accept a POST without a JSON body: a foreign page must not press
    this on the person's behalf.
    """
    got = run("""
      routes["/legal/notice"] = async () => ({ body: { ok: true } });
      routes["/recap/topics"] = async () => ({ body: { ok: true, topics: [] } });
      routes["/recap"] = async () => ({ body: { ok: true, shown: true } });
      routes["/control"] = async () => ({ body: { running: true, sources: { mic: true, loopback: true, screen: false } } });
      await push({});
      await $el("notice-ok").onclick(); await settle();
      await $el("recap-run").onclick(); await settle();
      const post = (url) => calls.filter((c) => c.url === url && c.method === "POST");
      return { notice: post("/legal/notice"), recap: post("/recap") };
    """)
    for sent in got["notice"] + got["recap"]:
        assert sent["rawBody"] == "{}" and sent["headers"] == {"Content-Type": "application/json"}
    assert len(got["notice"]) == 1 and len(got["recap"]) == 1


@needs_node
@pytest.mark.parametrize(("solo", "stream"), [("", True), ("screen", True), ("live", False), ("chat", False)])
def test_the_screen_stream_starts_only_where_there_is_a_place_to_show_it(solo, stream):
    """In the "all at once" window the screen panel is hidden behind an arrow, and the
    connection stream must not be kept open for nothing.
    """
    got = run("""
      await settle();
      return $el("shot").src;
    """, solo=solo)
    assert got == ("/screen.mjpg" if stream else "")


@needs_node
def test_the_live_window_opens_the_stream_with_the_screen_drawer_and_drops_it_on_close():
    """The arrow slides out the screen panel and the frame stream opens; when the panel is removed,
    it closes: while the panel is hidden, the server does not capture or compress frames twelve
    times a second for anyone. A connection drop with the panel closed does not open the stream
    either."""
    got = run("""
      routes["/window/resize"] = async () => ({ body: { ok: true } });
      await settle();
      const closed = $el("shot").src;
      const first = ws();
      await openWS(first); first.onclose(); advance(1000);
      await openWS(ws());                              // the connection is back, the panel is closed
      await push({});
      const reconnected = $el("shot").src;
      await fire("sub:.screen-toggle", "click", {}); await settle();
      const opened = $el("shot").src;
      const open = document.body.hasAttribute("data-screen-open");
      await fire("sub:.screen-toggle", "click", {}); await settle();
      return { closed, reconnected, opened, open, after: $el("shot").src, still: document.body.hasAttribute("data-screen-open") };
    """, solo="live", app=True)
    assert got == {"closed": "", "reconnected": "", "opened": "/screen.mjpg", "open": True,
                   "after": "", "still": False}


# --------------------------------------------------------------------------
# Breakages of the page core: the shell
# --------------------------------------------------------------------------


def test_the_harness_reads_the_current_page():
    """The parsing itself: the main script was found and is not empty (otherwise all the tests
    above would be checking emptiness).
    """
    assert "async function ask(" in page_script() and len(page_script()) > 50_000
