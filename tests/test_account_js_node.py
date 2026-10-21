"""The "Account", "Payment" and "Usage" tabs (`account.js`)
run for real, through `node`.

The earlier tab checks (`test_billing_tab.py`) were substrings in the file text: none of
them ran `mountBilling`, `mountUsage` or `act()`. That is how escaping of a foreign server
response, silent sign-in failures, an "Account" tab that still showed the email after
sign-out, and a wrong "topped up" amount slipped through (audit of 2026-10-14,
fe-account-2/7/8/9/10/11/13/15/16). Here the whole file is loaded into node with a small
stand-in DOM (only what `account.js` touches), and the tabs get their own `get`/`post`, as
the page itself does. The timers that watch a payment are manual: the test decides how many
steps have passed.

Without node the checks are skipped, like `test_the_bar_script_parses`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the JS of the page")

HARNESS = r"""
const fs = require("fs");
const SRC = fs.readFileSync(process.env.ACCOUNT_JS, "utf8");

// ---------- fake DOM: exactly what account.js touches ----------
function attrsOf(s) {
  const a = {};
  const re = /([^\s=\/>"']+)(?:="([^"]*)")?/g;
  let m;
  while ((m = re.exec(s))) a[m[1]] = m[2] === undefined ? "" : m[2];
  return a;
}
function parse(owner, html) {
  const list = [];
  const re = /<([a-zA-Z][\w-]*)((?:\s[^<>]*)?)>/g;
  let m;
  while ((m = re.exec(html))) {
    const rest = html.slice(m.index + m[0].length);
    list.push(new Elem(owner, m[1].toLowerCase(), attrsOf(m[2].replace(/\/$/, "")), rest.split("<")[0].trim()));
  }
  return list;
}
class Elem {
  constructor(owner, tag, attrs, text) {
    this.owner = owner; this.tag = tag; this.attrs = attrs; this.textContent = text;
    this.dataset = {};
    for (const k of Object.keys(attrs)) {
      if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = attrs[k];
    }
    this.hidden = "hidden" in attrs;
    this.disabled = false;
    this.value = attrs.value === undefined ? "" : attrs.value;
    this.type = attrs.type || "";
    this.parentElement = { hidden: false };
    this.listeners = {};
    this.style = {};
    this.isConnected = true;
    const cls = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
    this._cls = cls;
    this.classList = {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
      toggle: (c, on) => { const want = on === undefined ? !cls.has(c) : !!on; if (want) cls.add(c); else cls.delete(c); return want; },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  querySelector(sel) { return this.owner.querySelector(sel); }
  querySelectorAll(sel) { return this.owner.querySelectorAll(sel); }
  focus() {}
  select() {}
  set innerHTML(v) { this._html = v; this.owner.adopt(parse(this.owner, v)); }
  get innerHTML() { return this._html || ""; }
}
function container() {
  return {
    els: [], _html: "", isConnected: true, clientWidth: 600, listeners: {},
    set innerHTML(v) { this._html = v; this.els = parse(this, v); },
    get innerHTML() { return this._html; },
    adopt(list) { this.els.push(...list); },
    querySelectorAll(sel) {
      let m;
      if ((m = /^\[data-act="([^"]+)"\]$/.exec(sel))) return this.els.filter((e) => e.attrs["data-act"] === m[1]);
      if ((m = /^\.([\w-]+)$/.exec(sel))) return this.els.filter((e) => e._cls.has(m[1]));
      if ((m = /^#([\w-]+)$/.exec(sel))) return this.els.filter((e) => e.attrs.id === m[1]);
      if ((m = /^([a-z]+)$/.exec(sel))) return this.els.filter((e) => e.tag === m[1]);
      throw new Error("the stand-in DOM does not know the selector " + sel);
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  };
}

// An unhandled rejection in the page is a silent breakage (the tab stays empty): we record
// it instead of crashing node.
let unhandled = [];
process.on("unhandledRejection", (e) => { unhandled.push(String((e && e.message) || e)); });

// ---------- time and network ----------
const timers = [];
globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
const settle = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r)); };
const tick = async () => { const fn = timers.shift(); if (fn) fn(); await settle(); };

function boot(win) {
  timers.length = 0;
  const window = { ...(win || {}) };
  const document = {
    createElement: () => new Elem(container(), "div", {}, ""),
    body: { appendChild() {} }, getElementById: () => null, addEventListener() {}, removeEventListener() {},
  };
  new Function("window", "document", SRC)(window, document);
  return window.argusAccount;
}
function fakeApi(routes) {
  const log = [];
  const run = async (kind, url, body) => {
    const path = url.split("?")[0];
    log.push([kind, path, body]);
    let r = routes[path];
    if (typeof r === "function") r = r(body);
    r = await r;
    if (r instanceof Error) throw r;
    return r;
  };
  const api = {
    get: (u) => run("GET", u), post: (u, b) => run("POST", u, b),
    onChange: () => log.push(["changed"]), toast: (t) => log.push(["toast", t]),
  };
  return { api, log };
}

const EVIL = '<img src=x onerror=alert(1)>';
const rightOk = (b) => ({ allowed: true, low: false, balance_micro: b });
const PRICE = (over) => ({
  ok: true, topups: [5, 10, 20, 50], min_topup: 5, max_topup: 500,
  stt_hour: "$1.00", stt_minute: "$0.02", stt_hour_micro: 1000000, tts_1000: "$0.50", tts_1000_micro: 500000,
  payment: true, answers: [{ title: "Regular answer", answer: "$0.01", answer_micro: 10000 }], ...(over || {}),
});
const spentOf = (over) => ({ days: 30, micro: 0, total: "$0.00", by_kind: {}, ...(over || {}) });
const count = (log, kind, path) => log.filter((x) => x[0] === kind && x[1] === path).length;
const q = (el, act) => el.querySelector(`[data-act="${act}"]`);

// ---------- scenarios ----------
const scenarios = {};

scenarios.escape_billing = async () => {
  const A = boot();
  const el = container();
  const { api } = fakeApi({
    "/admin/account": { signed_in: true, email: "a@b.c", right: rightOk(5e6), offline: false,
      spent: spentOf({ days: EVIL, micro: 1e6, total: "$1.00" }) },
    "/admin/account/pricing": PRICE({ topups: [EVIL, 5, 10], min_topup: EVIL, max_topup: EVIL, stt_hour: EVIL,
      answers: [{ title: EVIL, answer: EVIL, answer_micro: 10000 }] }),
    "/admin/account/history": { ok: true, credits: [
      { kind: "constructor", amount: EVIL, at: 1760313600 }, { kind: EVIL, amount: "$1", at: 1760313600 }] },
  });
  A.mountBilling(el, api);
  await settle();
  const html = el.innerHTML;
  return {
    raw_img: html.includes("<img"), escaped_img: html.includes("&lt;img"),
    tiles: (html.match(/data-dollars="(\d+)"/g) || []).length,
    days: html.includes("Spent in 30 days"), limits: html.includes('min="5" max="500"'),
    has_history: html.includes("Top-up history"),
  };
};

scenarios.escape_usage = async () => {
  const A = boot();
  const el = container();
  const daily = [{ day: EVIL, micro: 5000, by_kind: { stt_seconds: 5000 } }, { day: "2026-10-13", micro: 0, by_kind: {} }];
  const { api } = fakeApi({
    "/admin/account": { signed_in: true, email: "a", right: rightOk(1e6), offline: false,
      spent: spentOf({ micro: 5000, total: EVIL, by_kind: {
        [EVIL]: { units: 1, micro: 5, n: 1 }, constructor: { units: 1, micro: 4, n: 1 },
        stt_seconds: { units: 3600, micro: 5000, n: 1 } } }) },
    "/admin/account/usage": { ok: true, daily },
  });
  A.mountUsage(el, api);
  await settle();
  return { raw_img: el.innerHTML.includes("<img"), table: el.innerHTML.includes("ac-table"), chart: el.innerHTML.includes("ac-chart") };
};

scenarios.sign_in_errors = async () => {
  const A = boot();
  const el = container();
  let answer = null;
  let signed = false;
  const { api, log } = fakeApi({
    "/admin/account": () => (signed ? { signed_in: true, email: "me@site.org", right: rightOk(1e6) } : { signed_in: false }),
    "/admin/account/sign-in": () => answer,
  });
  A.mount(el, api);
  await settle();
  const shots = [];
  for (const ans of [{ detail: "there are no accounts in this build" }, new Error("not JSON"), { ok: false, error: "wrong password" }, null]) {
    answer = ans;
    q(el, "email").value = "me@site.org";
    q(el, "password").value = "secret-pass";
    await q(el, "go").onclick();
    await settle();
    const box = q(el, "error");
    shots.push({ text: box.textContent, shown: !box.hidden, button: q(el, "go").textContent,
      disabled: q(el, "go").disabled, changed: count(log, "changed") });
  }
  signed = true;
  answer = { ok: true, signed_in: true, email: "me@site.org" };
  q(el, "email").value = "me@site.org";
  q(el, "password").value = "secret-pass";
  await q(el, "go").onclick();
  await settle();
  return { shots, ok_changed: log.filter((x) => x[0] === "changed").length, signed_in_view: el.innerHTML.includes('data-act="out"') };
};

scenarios.sign_out = async () => {
  const A = boot();
  const el = container();
  let signed = true;
  let answer = new Error("no connection");
  const { api, log } = fakeApi({
    "/admin/account": () => (signed ? { signed_in: true, email: "me@site.org", right: rightOk(3e6), offline: false } : { signed_in: false }),
    "/admin/account/sign-out": () => { if (!(answer instanceof Error) && answer.ok) signed = false; return answer; },
  });
  A.mount(el, api);
  await settle();
  const first = el.innerHTML.includes('data-act="out"');
  await q(el, "out").onclick();
  await settle();
  const failed = { still_signed_view: el.innerHTML.includes('data-act="out"'), enabled: !q(el, "out").disabled,
    toast: log.filter((x) => x[0] === "toast").map((x) => x[1]), changed: count(log, "changed") };
  answer = { ok: true };
  await q(el, "out").onclick();
  await settle();
  return { first, failed, after: { form: el.innerHTML.includes('data-act="go"'), out: el.innerHTML.includes('data-act="out"'),
    changed: count(log, "changed") } };
};

scenarios.account_retry = async () => {
  const A = boot();
  const el = container();
  let fail = true;
  const { api } = fakeApi({ "/admin/account": () => (fail ? new Error("no connection") : { signed_in: false }) });
  A.mount(el, api);
  await settle();
  const first = { retry: el.innerHTML.includes('data-act="retry"'), text: el.innerHTML.includes("No connection") };
  fail = false;
  await q(el, "retry").onclick();
  await settle();
  return { first, after: { form: el.innerHTML.includes('data-act="go"'), retry: el.innerHTML.includes('data-act="retry"') } };
};

// Shared payment world: balance, history and checkout are all its own, and a scenario can
// change them directly.
function payWorld(extra) {
  const w = { balance: 10e6, history: [], checkout: { ok: true, pay_url: "https://pay.example/x", opened: true }, rightOf: null };
  const { api, log } = fakeApi({
    "/admin/account": () => ({ signed_in: true, email: "a@b.c", offline: false, spent: spentOf(),
      right: w.rightOf ? w.rightOf() : rightOk(w.balance) }),
    "/admin/account/pricing": PRICE(),
    "/admin/account/history": () => ({ ok: true, credits: w.history }),
    "/admin/account/checkout": () => w.checkout,
    ...(extra || {}),
  });
  w.api = api; w.log = log;
  return w;
}

scenarios.paid_amount = async () => {
  const A = boot();
  const el = container();
  const w = payWorld();
  const tab = A.mountBilling(el, w.api);
  await settle();
  w.balance -= 30000;                                      // while the person was choosing an amount, spending moved the balance
  await q(el, "go").onclick();
  await settle();
  const waiting = el.innerHTML.includes("Waiting for payment") && el.innerHTML.includes("— $10");
  w.balance += 10e6 - 12000;                               // the $10 payment went through, and in the meantime another $0.012 was spent
  w.history = [{ kind: "topup", amount: "$10.00", at: 1760313600 }];
  const h0 = count(w.log, "GET", "/admin/account/history");
  await tick();
  const paidHtml = el.innerHTML;
  const h1 = count(w.log, "GET", "/admin/account/history");
  await q(el, "done").onclick();
  await settle();
  return { waiting, paid: paidHtml.includes("Balance topped up by $10.00"), now: paidHtml.includes("Your balance is now $19.96"),
    history_reloaded: h1 > h0, history_row: paidHtml.includes("Top-up"),
    after_done: { pick: el.innerHTML.includes('data-act="go"'), history_row: el.innerHTML.includes("Top-up"),
      calls: count(w.log, "GET", "/admin/account/history") }, tab: typeof tab.refresh };
};

scenarios.no_balance_at_start = async () => {
  const A = boot();
  const el = container();
  const w = payWorld();
  let known = null;                                        // the server has no balance / the app started without a connection
  w.rightOf = () => (known === null ? { allowed: true } : rightOk(known));
  A.mountBilling(el, w.api);
  await settle();
  await q(el, "go").onclick();
  await settle();
  const waiting = el.innerHTML.includes("Waiting for payment");
  known = 3e6;
  await tick();                                            // the first response is the baseline
  const still = el.innerHTML.includes("Waiting for payment");
  known = 13e6;
  await tick();
  return { waiting, still, paid: el.innerHTML.includes("Balance topped up by $10.00"), now: el.innerHTML.includes("$13.00") };
};

async function toLate(w, el) {
  await q(el, "go").onclick();
  await settle();
  for (let i = 0; i < 300 && !el.innerHTML.includes("No payment confirmation yet"); i++) await tick();
  return el.innerHTML.includes("No payment confirmation yet");
}

scenarios.late_recheck = async () => {
  const A = boot();
  const el = container();
  const w = payWorld();
  A.mountBilling(el, w.api);
  await settle();
  const late = await toLate(w, el);
  w.balance -= 20000;                                      // the money has not arrived yet, spending continues: the account holds $9.98
  await q(el, "recheck").onclick();
  await settle();
  const stays = el.innerHTML.includes("No payment confirmation yet") && el.innerHTML.includes('9<span class="ac-cents">.98');
  w.balance += 10e6;                                       // the money arrived later
  await q(el, "recheck").onclick();
  await settle();
  return { late, stays, paid: el.innerHTML.includes("Balance topped up by $10.00"), card_gone: !el.innerHTML.includes("No payment confirmation yet") };
};

scenarios.late_reentry = async () => {
  const A = boot();
  const el = container();
  const w = payWorld();
  const tab = A.mountBilling(el, w.api);
  await settle();
  const late = await toLate(w, el);
  w.balance += 10e6;
  await tab.refresh();                                     // the tab was opened again
  await settle();
  const paid = el.innerHTML.includes("Balance topped up by $10.00");
  const keep = tab.refresh();                              // we leave the payment outcome alone
  await settle();
  return { late, paid, kept: el.innerHTML.includes("Balance topped up by $10.00"), kept_result: keep === undefined };
};

scenarios.checkout_errors = async () => {
  const A = boot();
  const el = container();
  const w = payWorld();
  A.mountBilling(el, w.api);
  await settle();
  w.checkout = { detail: "Request rejected: it didn't come from the app." };
  await q(el, "go").onclick();
  await settle();
  const pickErr = { text: q(el, "error").textContent, shown: !q(el, "error").hidden, waiting: el.innerHTML.includes("Waiting for payment"),
    button: q(el, "go").textContent };
  w.checkout = { ok: true, pay_url: "https://pay.example/x", opened: true };
  await q(el, "go").onclick();
  await settle();
  const inWaiting = el.innerHTML.includes("Waiting for payment");
  w.checkout = { ok: false, error: "the payment service is unavailable right now" };
  await q(el, "again").onclick();
  await settle();
  const waitErr = { text: q(el, "error").textContent, shown: !q(el, "error").hidden, still: el.innerHTML.includes("Waiting for payment"),
    button: q(el, "again").textContent };
  w.checkout = new Error("a placeholder page instead of JSON");
  await q(el, "again").onclick();
  await settle();
  return { pickErr, inWaiting, waitErr, thrown: { text: q(el, "error").textContent, shown: !q(el, "error").hidden } };
};

scenarios.offline_numbers = async () => {
  const A = boot();
  const billing = container();
  const usage = container();
  const offline = { signed_in: true, email: "a@b.c", offline: true, right: rightOk(5e6), error: "no connection" };
  const online = { signed_in: true, email: "a@b.c", offline: false, right: rightOk(5e6), spent: spentOf() };
  let state = offline;
  const { api } = fakeApi({
    "/admin/account": () => state, "/admin/account/pricing": PRICE(), "/admin/account/history": { ok: true, credits: [] },
    "/admin/account/usage": { ok: true, daily: [] },
  });
  A.mountBilling(billing, api);
  A.mountUsage(usage, api);
  await settle();
  const off = {
    spent_cell: /Spent in 30 days<\/span><b>([^<]*)<\/b>/.exec(billing.innerHTML)?.[1],
    usage_big: /class="ac-big">([^<]*)</.exec(usage.innerHTML)?.[1], usage_note: usage.innerHTML.includes("No connection: no data"),
    billing_note: billing.innerHTML.includes("No connection — showing the last known data."),
  };
  state = online;
  const billing2 = container();
  const usage2 = container();
  A.mountBilling(billing2, api);
  A.mountUsage(usage2, api);
  await settle();
  const on = {
    spent_cell: /Spent in 30 days<\/span><b>([^<]*)<\/b>/.exec(billing2.innerHTML)?.[1],
    usage_big: /class="ac-big">([^<]*)</.exec(usage2.innerHTML)?.[1], usage_note: usage2.innerHTML.includes("No connection: no data"),
  };
  return { off, on };
};

scenarios.locale = async () => {
  // English is the source language: its page has no dictionary. Other pages get the engine's
  // `argusT`, which maps the English unit labels ("5 h", "3 min", "12K") into the language.
  const units = (h, min, k) => (s) => s.replace(/ h$/, h).replace(/ min$/, min).replace(/K$/, k);
  const ruDict = units(" ч", " мин", " тыс.");
  const deDict = units(" Std.", " Min.", " Tsd.");
  const el = (lang, tr) => ({ ARGUS_LANG: lang, ...(tr ? { argusT: tr } : {}) });
  const run = async (win) => {
    const A = boot(win);
    const box = container();
    const { api } = fakeApi({
      "/admin/account": { signed_in: true, email: "a@b.c", right: rightOk(1234567890), offline: false, spent: spentOf() },
      "/admin/account/pricing": PRICE(),
      "/admin/account/history": { ok: true, credits: [{ kind: "topup", amount: "$5.00", at: 1760313600 }] },
    });
    A.mountBilling(box, api);
    await settle();
    return box.innerHTML;
  };
  const en = await run(el("en"));
  const de = await run(el("de", deDict));
  const ru = await run(el("ru", ruDict));
  const date = (lang) => new Date(1760313600 * 1000).toLocaleDateString(lang, { day: "numeric", month: "short", year: "numeric" });
  // "окт" is the Russian abbreviation of October, "ч" the Russian hour: language data.
  return {
    en_date: en.includes(date("en")), de_date: de.includes(date("de")), ru_date: ru.includes(date("ru")),
    en_ru_month: /окт/.test(en), en_units: /\d h<\/td>/.test(en) || / h</.test(en), ru_units: / ч</.test(ru),
    en_group: en.includes('1,234<span class="ac-cents">'), ru_group: /1[\s  ]234<span class="ac-cents">/.test(ru),
  };
};

(async () => {
  const out = {};
  for (const [name, fn] of Object.entries(scenarios)) {
    unhandled = [];
    try { out[name] = await fn(); } catch (e) { out[name] = { crashed: String((e && e.stack) || e) }; }
    await settle();
    if (unhandled.length) out[name] = { ...out[name], unhandled };
  }
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def seen() -> dict:
    """All scenarios in a single node run: what the tabs drew and how they behaved."""
    if shutil.which("node") is None:
        pytest.skip("node is needed to run the JS of the page")
    done = subprocess.run(
        ["node", "-"], input=HARNESS, capture_output=True, text=True, encoding="utf-8", check=False,
        env={**os.environ, "ACCOUNT_JS": str(STATIC / "account.js")},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def scenario(seen: dict, name: str) -> dict:
    got = seen[name]
    assert "crashed" not in got, got.get("crashed")
    assert "unhandled" not in got, f"unhandled rejection in the page: {got['unhandled']}"
    return got


# --------------------------------------------------------------------------
# fe-account-2: a foreign server response does not become markup
# --------------------------------------------------------------------------


@needs_node
def test_a_hostile_price_never_becomes_markup(seen):
    """Top-up amounts, bounds and the number of days are coerced to numbers, everything else
    goes through `esc`.
    """
    got = scenario(seen, "escape_billing")
    assert not got["raw_img"], "a foreign tag got into the markup of the Payment tab"
    assert got["escaped_img"], "the price rows must be shown, but escaped"
    assert got["tiles"] == 2, "amount tiles: only the real numbers from the reply"
    assert got["days"] and got["limits"], "dirty bounds and the \"days\" are replaced with the former numbers"
    assert got["has_history"], "the journal key `constructor` must not break the history table"


@needs_node
def test_hostile_usage_data_never_becomes_markup(seen):
    got = scenario(seen, "escape_usage")
    assert not got["raw_img"]
    assert got["table"] and got["chart"], "the tab is drawn despite the garbage in the reply"


# --------------------------------------------------------------------------
# fe-account-7: sign-in and registration do not stay silent
# --------------------------------------------------------------------------


@needs_node
def test_sign_in_shows_why_it_failed_in_every_kind_of_failure(seen):
    got = scenario(seen, "sign_in_errors")
    detail, thrown, refused, nothing = got["shots"]
    assert detail["text"] == "there are no accounts in this build" and detail["shown"], "the reply `{detail}` was taken for a sign-in"
    assert thrown["text"] == "Can't reach the app" and thrown["shown"], "a failure to parse the reply went silent"
    assert refused["text"] == "wrong password" and refused["shown"]
    assert nothing["text"] == "Failed" and nothing["shown"]
    for shot in got["shots"]:
        assert shot["button"] == "Sign in" and not shot["disabled"], "the button must be released"
        assert shot["changed"] == 0, "a refusal must not tell the page that the sign-in is done"
    assert got["ok_changed"] == 1 and got["signed_in_view"], "success is still success"


# --------------------------------------------------------------------------
# fe-account-8 and fe-account-4: sign-out and connection in the "Account" tab
# --------------------------------------------------------------------------


@needs_node
def test_sign_out_redraws_the_account_tab(seen):
    got = scenario(seen, "sign_out")
    assert got["first"]
    assert got["failed"]["still_signed_view"] and got["failed"]["enabled"] and got["failed"]["changed"] == 0
    assert got["failed"]["toast"] == ["Can't reach the app"], "a failed sign-out must explain itself"
    assert got["after"]["form"] and not got["after"]["out"], "after sign-out the tab still shows the email and \"Sign out\""
    assert got["after"]["changed"] == 1


@needs_node
def test_the_account_tab_offers_retry_when_the_app_does_not_answer(seen):
    got = scenario(seen, "account_retry")
    assert got["first"]["retry"] and got["first"]["text"]
    assert got["after"]["form"] and not got["after"]["retry"]


# --------------------------------------------------------------------------
# fe-account-9, 10, 11: payment
# --------------------------------------------------------------------------


@needs_node
def test_the_paid_amount_is_the_order_and_not_a_difference_of_balances(seen):
    """Recognition was spending money while the payment was in progress: "$9.99" instead of
    "$10.00" would be untrue about the payment.
    """
    got = scenario(seen, "paid_amount")
    assert got["waiting"]
    assert got["paid"] and got["now"]
    assert got["history_reloaded"] and got["history_row"], "the top-up history was left without the new row"
    assert got["after_done"]["pick"] and got["after_done"]["history_row"]
    assert got["after_done"]["calls"] >= 3, "\"Done\" must read the history again"


@needs_node
def test_waiting_for_the_money_starts_even_when_the_balance_was_unknown(seen):
    got = scenario(seen, "no_balance_at_start")
    assert got["waiting"] and got["still"], "the first reply is the baseline, not \"credited\""
    assert got["paid"] and got["now"], "\"Waiting for payment\" hung forever when the balance did not arrive at start"


@needs_node
def test_recheck_on_the_late_card_looks_at_the_balance(seen):
    got = scenario(seen, "late_recheck")
    assert got["late"]
    assert got["stays"], "no money yet: the card stays, the balance above it is fresh"
    assert got["paid"] and got["card_gone"], "\"Check now\" did not move it to \"topped up\""


@needs_node
def test_returning_to_the_tab_after_the_late_card_checks_the_balance(seen):
    got = scenario(seen, "late_reentry")
    assert got["late"] and got["paid"]
    assert got["kept"] and got["kept_result"], "the payment outcome is left alone"


@needs_node
def test_checkout_failures_are_shown_where_the_person_looks(seen):
    got = scenario(seen, "checkout_errors")
    assert got["pickErr"]["text"] == "Request rejected: it didn't come from the app."
    assert got["pickErr"]["shown"] and not got["pickErr"]["waiting"], "`{detail}` was taken for an order"
    assert got["pickErr"]["button"] == "Pay $10"
    assert got["inWaiting"]
    assert got["waitErr"]["text"] == "the payment service is unavailable right now" and got["waitErr"]["shown"]
    assert got["waitErr"]["still"] and got["waitErr"]["button"] == "Open the payment page again"
    assert got["thrown"]["text"].startswith("Can't reach the app") and got["thrown"]["shown"]


# --------------------------------------------------------------------------
# fe-account-13 and fe-account-15: no connection and by page language
# --------------------------------------------------------------------------


@needs_node
def test_without_a_connection_the_spending_is_a_dash_and_not_zero(seen):
    got = scenario(seen, "offline_numbers")
    assert got["off"]["spent_cell"] == "—" and got["off"]["usage_big"] == "—"
    assert got["off"]["usage_note"] and got["off"]["billing_note"]
    assert got["on"]["spent_cell"] == "$0" and got["on"]["usage_big"] == "$0", "a real zero spending is still \"$0\""
    assert not got["on"]["usage_note"]


@needs_node
def test_dates_numbers_and_units_follow_the_page_language(seen):
    got = scenario(seen, "locale")
    assert got["en_date"] and got["de_date"] and got["ru_date"]
    assert not got["en_ru_month"], "the English page showed a Russian month"
    assert got["en_units"] and got["ru_units"]
    assert got["en_group"] and got["ru_group"]
