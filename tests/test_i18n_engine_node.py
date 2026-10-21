"""The translation engine (`ui/static/i18n/engine.js`) in real execution: through `node`.

The earlier checks (`test_i18n.py`) look at whether the dictionaries are complete and
pages get the translation; nobody ran the engine's own string parsing, so the key order
and the reload condition on a language change (audit of 2026-10-14, fe-pages-9 and
fe-pages-11) lived unchecked.

The engine is loaded into node with fake `window`/`document`: from the environment it
needs almost nothing but them, and it takes the string to translate from the caller
(`window.argusT`). No node: the checks are skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from argus.ui import i18n
from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page JS")

DIR = STATIC / "i18n"
LANGUAGES = [code for code in i18n.SUPPORTED if code != "en"]

HARNESS = r"""
const fs = require("fs");
const SRC = fs.readFileSync(process.env.ENGINE_JS, "utf8");
const DIR = process.env.I18N_DIR;
const LANGS = JSON.parse(process.env.LANGS);

let unhandled = [];
process.on("unhandledRejection", (e) => { unhandled.push(String((e && e.message) || e)); });
const timers = [];
globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };

function load(lang, dict, extra) {
  const window = { ARGUS_LANG: lang, ARGUS_I18N: dict, addEventListener() {}, ...(extra || {}) };
  const document = { documentElement: {}, readyState: "complete", title: "", body: null, addEventListener() {} };
  class Observer { observe() {} }
  new Function("window", "document", "MutationObserver", SRC)(window, document, Observer);
  return window;
}

const SLOT = /\{[a-z_]+\}/g;
const out = { order: {}, apply: {} };

// ---- each key with placeholders recognizes itself first (fe-pages-9) ----
for (const lang of LANGS) {
  const dict = JSON.parse(fs.readFileSync(`${DIR}/${lang}.json`, "utf8"));
  const win = load(lang, dict);
  const wrong = [];
  let checked = 0;
  for (const key of Object.keys(dict)) {
    if (!SLOT.test(key) || key.indexOf("<") >= 0) { SLOT.lastIndex = 0; continue; }
    SLOT.lastIndex = 0;
    const sample = key.replace(SLOT, "ZZ");
    const want = dict[key].replace(SLOT, "ZZ");
    const got = win.argusT(sample);
    checked += 1;
    if (got !== want) wrong.push([key, got, want]);
  }
  out.order[lang] = { checked, wrong };
}

// ---- example from the audit ----
{
  const dict = JSON.parse(fs.readFileSync(`${DIR}/de.json`, "utf8"));
  const win = load("de", dict);
  out.cut = {
    got: win.argusT("abc — wait ZZ min"),
    want: dict["{what} — wait {n} min"].replace("{what}", "abc").replace("{n}", "ZZ"),
    plain: win.argusT("ZZ min"), plain_want: dict["{n} min"].replace("{n}", "ZZ"),
    both_keys: "{what} — wait {n} min" in dict && "{n} min" in dict,
  };
}

// ---- reload on language change (fe-pages-11) ----
async function apply(answer, how) {
  let reloaded = 0, stored = 0;
  globalThis.localStorage = { setItem() { stored += 1; } };
  globalThis.location = { reload() { reloaded += 1; } };
  timers.length = 0;
  unhandled = [];
  const response = (body) => ({ ok: true, clone() { return this; }, json: async () => body });
  const fetchStub = how === "network" ? async () => { throw new Error("no network"); }
    : how === "html" ? async () => ({ ok: true, clone() { return this; }, json: async () => { throw new SyntaxError("not JSON"); } })
    : async () => response(answer);
  const win = load("de", { "Yes": "Ja" }, { fetch: fetchStub });
  let caller = "resolved";
  try { await win.fetch("/admin/apply", { method: "POST", body: JSON.stringify({ "channel.assist.interface.language": "en" }) }); }
  catch (e) { caller = "rejected"; }
  await settle();
  while (timers.length) timers.shift()();
  await settle();
  return { reloaded, stored, caller, unhandled: unhandled.slice() };
}
(async () => {
  out.apply.accepted = await apply({ ok: true, changed: [] });
  out.apply.refused = await apply({ ok: false, errors: { "x": "not allowed" } });
  out.apply.network = await apply(null, "network");
  out.apply.html = await apply(null, "html");
  // the engine does not touch a foreign address or a foreign field
  {
    let reloaded = 0;
    globalThis.localStorage = { setItem() {} };
    globalThis.location = { reload() { reloaded += 1; } };
    timers.length = 0;
    const win = load("de", { "Yes": "Ja" }, { fetch: async () => ({ ok: true, clone() { return this; }, json: async () => ({ ok: true }) }) });
    await win.fetch("/admin/apply", { method: "POST", body: JSON.stringify({ "screen.fps": 5 }) });
    await win.fetch("/chats", { method: "POST", body: JSON.stringify({ name: "channel.assist.interface.language" }) });
    await settle();
    while (timers.length) timers.shift()();
    out.apply.other = { reloaded };
  }
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def seen() -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is needed to run the page JS")
    done = subprocess.run(
        ["node", "-"], input=HARNESS, capture_output=True, text=True, encoding="utf-8", check=False,
        env={**os.environ, "ENGINE_JS": str(DIR / "engine.js"), "I18N_DIR": str(DIR), "LANGS": json.dumps(LANGUAGES)},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --------------------------------------------------------------------------
# fe-pages-9: order of keys with placeholders
# --------------------------------------------------------------------------


@needs_node
def test_a_more_specific_key_is_not_shadowed_by_a_general_one(seen):
    """"{what} — wait {n} min" stood in the dictionary after the general "{n} min" and was
    shadowed, so the result was "abc — wait 5 Min." instead of the whole phrase translated.
    """
    cut = seen["cut"]
    assert cut["both_keys"], "the dictionary has no key pair from the audit example: pick another pair"
    assert cut["got"] == cut["want"], cut
    assert cut["plain"] == cut["plain_want"], "the general key stopped working"


@needs_node
@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_key_with_places_is_found_by_itself_first(seen, lang):
    """A string built from a key must be translated by exactly that key, not by another one that
    happens to match at the edge.
    """
    got = seen["order"][lang]
    assert got["checked"] > 10, "there are no keys with slots: the check checked nothing"
    assert not got["wrong"], got["wrong"][:5]


# --------------------------------------------------------------------------
# fe-pages-11: reload only after an accepted language edit
# --------------------------------------------------------------------------


@needs_node
def test_the_pages_reload_only_after_the_language_change_was_accepted(seen):
    accepted = seen["apply"]["accepted"]
    assert accepted["reloaded"] == 1 and accepted["stored"] == 1, accepted
    refused = seen["apply"]["refused"]
    assert refused["reloaded"] == 0 and refused["stored"] == 0, "a refusal from the server reloaded the pages and wiped the error"
    assert not refused["unhandled"]


@needs_node
def test_a_failed_request_neither_reloads_nor_leaves_an_unhandled_rejection(seen):
    for how in ("network", "html"):
        got = seen["apply"][how]
        assert got["reloaded"] == 0 and got["stored"] == 0, (how, got)
        assert not got["unhandled"], (how, got)
    assert seen["apply"]["network"]["caller"] == "rejected", "the caller must still receive the network failure as before"


@needs_node
def test_other_requests_do_not_reload_anything(seen):
    assert seen["apply"]["other"]["reloaded"] == 0
