"""The page from the recovery email: the real page script, run under `node`.

The server no longer inserts the token into the page code (a `</script>` inside it
executed someone else's markup); the script itself reads it from the address. So it is
the script that gets tested: where it takes the token from, what it sends to
`/auth/reset/confirm`, what it tells the person, and whether it can inject markup. There
is no browser here: `document`, `location`, `history` and `fetch` are stubbed the way a
real window drives them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from argus.server.app import RESET_PAGE

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to run the page JS"
)

SCRIPT = re.search(r"<script>(.*?)</script>", RESET_PAGE, re.DOTALL).group(1)

HARNESS = r"""
const SCRIPT = %(script)s;
const SCENARIOS = %(scenarios)s;

async function run(s) {
  const elements = {};
  const make = (id, extra = {}) => (elements[id] = Object.assign(
    { hidden: false, className: "", textContent: "", value: "", disabled: false,
      listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } }, extra));
  make("f"); make("go"); make("msg"); make("h"); make("password", { value: s.password });
  let sent = null, replaced = null, replaceCalls = 0;
  const root = { lang: "" };
  globalThis.document = { getElementById: (id) => elements[id], documentElement: root, title: "" };
  Object.defineProperty(globalThis, "navigator", { value: { languages: s.languages }, configurable: true });
  globalThis.location = { hash: s.hash, search: s.search, pathname: "/auth/reset" };
  globalThis.history = { replaceState: (a, b, url) => { replaced = url; replaceCalls++; } };
  globalThis.fetch = async (url, opts) => {
    sent = { url, method: opts.method, body: JSON.parse(opts.body) };
    if (s.network) throw new TypeError("network");
    return { json: async () => s.response };
  };
  (new Function(SCRIPT))();
  if (s.submit && elements.f.listeners.submit) {
    await elements.f.listeners.submit({ preventDefault() {} });
  }
  return { sent, replaced, replaceCalls, formHidden: elements.f.hidden, lang: root.lang, title: document.title,
           heading: elements.h.textContent, placeholder: elements.password.placeholder, button: elements.go.textContent,
           msg: elements.msg.textContent, cls: elements.msg.className,
           goDisabled: elements.go.disabled };
}

(async () => {
  const out = [];
  for (const s of SCENARIOS) out.push(await run(s));
  console.log(JSON.stringify(out));
})();
"""


def run(*scenarios: dict) -> list[dict]:
    base = {"hash": "", "search": "", "password": "a-new-long-password", "languages": ["en-US"],
            "response": {"ok": True}, "network": False, "submit": True}
    text = HARNESS % {
        "script": json.dumps(SCRIPT),
        "scenarios": json.dumps([{**base, **s} for s in scenarios]),
    }
    done = subprocess.run(["node", "-e", text], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_token_comes_from_the_part_after_the_hash():
    (out,) = run({"hash": "#token=abc-DEF_123"})
    assert out["sent"] == {"url": "/auth/reset/confirm", "method": "POST",
                           "body": {"token": "abc-DEF_123", "password": "a-new-long-password"}}
    assert out["cls"] == "msg ok" and "Password saved" in out["msg"]
    assert out["formHidden"] is True


@needs_node
def test_after_success_the_token_leaves_the_address_bar():
    (out,) = run({"hash": "#token=abc"})
    assert out["replaced"] == "/auth/reset" and out["replaceCalls"] == 1


@needs_node
def test_an_old_letter_with_the_token_in_the_query_still_works():
    (out,) = run({"search": "?token=old-one"})
    assert out["sent"]["body"]["token"] == "old-one"


@needs_node
def test_the_hash_wins_over_the_query():
    (out,) = run({"hash": "#token=new", "search": "?token=old"})
    assert out["sent"]["body"]["token"] == "new"


@needs_node
def test_without_a_token_the_form_is_hidden_and_the_person_is_told_what_to_do():
    # The form is hidden and there is nothing to submit it with, so the scenario does not press
    # the button.
    for one in run({"submit": False}, {"hash": "#other=1", "submit": False}):
        assert one["formHidden"] is True
        assert one["cls"] == "msg bad" and "request a new" in one["msg"]
        assert one["sent"] is None


@needs_node
def test_a_hostile_address_is_only_ever_data():
    """A string from the address goes to the server as is and is never inserted anywhere as
    markup.
    """
    hostile = "</script><img src=x onerror=alert(document.domain)>"
    (out,) = run({"hash": "#token=" + hostile})
    assert out["sent"]["body"]["token"] == hostile
    assert out["msg"] == "Password saved. Go back to the app and sign in again."
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "new Function", "setTimeout(\"", "location ="):
        assert sink not in SCRIPT, f"the page script inserts text via {sink}"


@needs_node
def test_a_refusal_of_the_server_is_shown_and_the_form_stays():
    (out,) = run({"hash": "#token=abc", "response": {
        "ok": False, "code": "link", "error": "link is invalid or expired — request a new one"}})
    assert out["cls"] == "msg bad" and "request a new" in out["msg"]
    assert out["formHidden"] is False and out["goDisabled"] is False
    assert out["replaceCalls"] == 0


@needs_node
def test_no_connection_is_shown_and_the_button_comes_back():
    (out,) = run({"hash": "#token=abc", "network": True})
    assert out["msg"] == "No connection" and out["cls"] == "msg bad"
    assert out["goDisabled"] is False


def test_the_page_script_matches_the_policy_hash_and_has_no_server_side_placeholder():
    """There is a single, unchanging script: `Content-Security-Policy` rests on it, and it has no
    place left where the server would substitute a token."""
    assert RESET_PAGE.count("<script>") == 1
    assert "{token}" not in RESET_PAGE


# -- page language -----------------------------------------------------------------

LANGS = ("ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl")


@needs_node
def test_the_link_language_wins_then_the_browser_then_english():
    ask = {"hash": "#token=abc", "submit": False}
    by_link, by_browser, by_browser_region, fallback, junk = run(
        {**ask, "search": "?lang=de", "languages": ["fr-FR"]},
        {**ask, "languages": ["tr-TR", "en"]},
        {**ask, "search": "?lang=pt-BR", "languages": ["ru"]},
        {**ask, "languages": ["ja-JP", "zh"]},
        {**ask, "search": "?lang=<script>", "languages": []})
    assert by_link["lang"] == "de" and by_link["heading"] == "Neues Passwort"
    assert by_browser["lang"] == "tr"
    assert by_browser_region["lang"] == "pt"
    assert fallback["lang"] == "en" and fallback["heading"] == "New password"
    assert junk["lang"] == "en"


@needs_node
def test_every_language_fills_the_whole_page():
    outs = run(*({"hash": "#token=abc", "submit": False, "search": f"?lang={lang}"} for lang in LANGS))
    seen = set()
    for lang, out in zip(LANGS, outs):
        assert out["lang"] == lang
        for field in ("title", "heading", "placeholder", "button"):
            assert out[field].strip(), (lang, field)
        seen.add(out["heading"])
    assert len(seen) == len(LANGS), "two languages have the same heading — the translation is missing"


@needs_node
def test_a_too_short_password_is_stopped_on_the_page_in_the_persons_language():
    outs = run(*({"hash": "#token=abc", "search": f"?lang={lang}", "password": "short"} for lang in LANGS))
    for lang, out in zip(LANGS, outs):
        assert out["sent"] is None, "a short password must not go to the server"
        assert out["cls"] == "msg bad" and "8" in out["msg"] and "{min}" not in out["msg"], lang
        assert out["goDisabled"] is False


@needs_node
def test_the_servers_refusal_codes_become_sentences_in_the_persons_language():
    codes = ("short", "weak", "link", "busy", "limit", "")
    outs = run(*({"hash": "#token=abc", "search": "?lang=en",
                  "response": {"ok": False, "code": code, "error": "raw server text"}} for code in codes))
    msgs = [o["msg"] for o in outs]
    assert all(m and "raw server text" not in m and "{min}" not in m for m in msgs), msgs
    assert len(set(msgs[:5])) == 5, "different reasons — different captions"


def test_every_language_is_embedded_in_the_page_and_no_text_can_close_the_script():
    from argus.server import mailtexts
    for lang in mailtexts.LANGS:
        assert mailtexts.reset_page(lang)["heading"] in RESET_PAGE
    assert "</" not in SCRIPT, "the page script must not contain closing tags"
    assert "{{" not in RESET_PAGE
