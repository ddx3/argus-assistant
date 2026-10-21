"""Interface translations: the `i18n/` tools work and every English string has a key.

`test_i18n.py` checks that the dictionaries are complete (against `keys.json`). This one
checks what comes earlier:
- `keys.json` is built from `keys_manual.py` and the settings page (otherwise a text edit
  did not reach the dictionaries);
- every key is still in the code (dead keys piled up: subscriptions, the "Data" window);
- the dictionaries pass `i18n/validate.py` (placeholders, tags, Cyrillic, length);
- there is no English text without a key on the pages and in server refusals: such text
  stays English in all eight other languages, and without knowing a foreign language it is
  impossible to notice (the home screen and the "Recording" screen lived like that without
  a translation).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from argus.ui import i18n

ROOT = Path(__file__).resolve().parents[1]
DIR = Path(i18n.STATIC) / "i18n"
LANGUAGES = [code for code in i18n.SUPPORTED if code != "en"]


def _tool(name: str):
    """A module from `i18n/` by path: the names `validate` and `gen_keys` are too generic to put
    them on `sys.path`.
    """
    spec = importlib.util.spec_from_file_location(f"i18n_tool_{name}", ROOT / "i18n" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen_keys = _tool("gen_keys")
validate = _tool("validate")
scan_pages = _tool("scan_pages")


def test_keys_json_is_what_the_tools_build():
    # `keys_manual.py` was edited and `python i18n/gen_keys.py` was forgotten: the dictionaries
    # do not learn about the new string.
    built = gen_keys.collect()
    saved = json.loads((DIR / "keys.json").read_text(encoding="utf-8"))["keys"]
    assert [e["k"] for e in built] == [e["k"] for e in saved], "the keys differ: run python i18n/gen_keys.py"
    assert built == saved, "the notes or places differ: run python i18n/gen_keys.py"


def test_every_key_is_still_in_the_code(capsys):
    assert gen_keys.check(gen_keys.collect()) == 0, capsys.readouterr().out


def test_no_key_is_listed_twice():
    keys = [e["k"] for e in gen_keys.collect()]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_dictionary_passes_the_validator(lang):
    assert validate.check(lang) == []


def test_the_home_and_record_pages_are_scanned_for_keys():
    # Pages that were not in `SOURCES` forever "found all the keys in place" and lost
    # translations.
    names = {p.name for p in gen_keys.SOURCES}
    assert {"home.html", "record.html", "account.js", "admin.js", "index.html"} <= names


@pytest.mark.parametrize("name", scan_pages.PAGES)
def test_a_page_has_no_english_text_without_a_key(name):
    # A new English string in a page without an entry in `i18n/keys_manual.py` stays English in
    # any language.
    keys = scan_pages.Keys()
    found = scan_pages.uncovered_page(name, keys)
    assert not found, (
        f"{name}: no translation key (add it to i18n/keys_manual.py, run python i18n/gen_keys.py, translate the dictionaries):\n"
        + "\n".join(f"  line {ln}: {text}" for ln, text in found))


# What the scanner finds but there is nothing to translate:
# - a refusal that is assembled from pieces: the wait time is inserted by
#   `throttle.wait_text` ("a minute", "14 min", "3 h"), and so there are three of them in the
#   keys: "{what} — wait a minute", "… {n} min", "… {n} h";
# - the successful answer of the "Invisible on recording" switch on Mac: the page shows only
#   refusals (`cErr` in `admin.js`);
# - the answer to the payment provider's webhook, which is read by a program and not by a person.
ASSEMBLED_MESSAGES = {
    ("server/app.py", "{x} — wait {x}"),
    ("server/app.py", "bad request body"),
    ("server/relay.py", "several replies in a row did not arrive in time — wait {x}"),
    ("mac/desktop.py", "windows marked as hidden from screen recording ({x}){x}"),
}


def test_a_message_from_the_server_has_a_key():
    # The texts of refusals (`HTTPException`, `Denied`, `{"error": …}`) are shown by the page as
    # they are. Without a key, a person using German reads the English "no such session".
    keys = scan_pages.Keys()
    found = [(rel, ln, text) for rel, ln, text in scan_pages.uncovered_python(keys)
             if (rel, text) not in ASSEMBLED_MESSAGES]
    assert not found, "refusals without a translation key (i18n/keys_manual.py, runtime):\n" + "\n".join(
        f"  {rel}:{ln}: {text}" for rel, ln, text in found)


# ---------------------------------------------------------------- the scanner itself


def _scanner_over(tmp_path, monkeypatch, files: dict[str, str]):
    keys = scan_pages.Keys()                       # keys are read before the page directory is replaced
    monkeypatch.setattr(scan_pages, "STATIC", tmp_path)
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return keys, {name: scan_pages.uncovered_page(name, keys) for name in files}


def test_the_scanner_finds_new_text_in_markup_and_in_code(tmp_path, monkeypatch):
    _keys, found = _scanner_over(tmp_path, monkeypatch, {
        "new.html": '<p>A brand new paragraph</p><input placeholder="A new hint"><script>'
                    'el.textContent = "A new label from code"; el.innerHTML = `<b>One more ${n} line</b>`;'
                    '</script>',
        "new.js": 'const f = (ok) => ok ? "Done" : `No such key: ${why}`;',
    })
    texts = {t for _ln, t in found["new.html"]}
    assert {"A brand new paragraph", "A new hint", "A new label from code"} <= texts
    assert any(t.startswith("One more {x} line") for t in texts)
    assert [t for _ln, t in found["new.js"]] == ["No such key: {x}"]


def test_the_scanner_does_not_ask_for_logs_comments_selectors_and_translated_text(tmp_path, monkeypatch):
    _keys, found = _scanner_over(tmp_path, monkeypatch, {
        "ok.js": "// A comment in English\n"
                 "/* and a block comment */\n"
                 'console.warn("only to the console", e);\n'
                 'throw new Error("only to the log");\n'
                 'const css = `/* color */ .a { color: red }`;\n'
                 'const sel = el.closest(".io.output:not(.err), .q-row p"), cls = "note warn", url = "/chat/" + id;\n'
                 'const a = "Done", b = `${n} min`, c = "Today" + "";\n',
    })
    assert found["ok.js"] == []


def test_the_scanner_glues_strings_joined_with_a_plus(tmp_path, monkeypatch):
    # The home dialog assembles its text from two strings, and the key is the whole markup.
    _keys, found = _scanner_over(tmp_path, monkeypatch, {
        "glued.js": 'const t = `Recognition is running in the session “<b data-i18n="off">${name}</b>”. ` +\n'
                    '  `If you open another session, it will stop.`;\n',
    })
    assert found["glued.js"] == []


def test_the_scanner_matches_keys_the_way_the_engine_does():
    keys = scan_pages.Keys()
    assert keys.has("Today")
    assert keys.has("5 min") and keys.has("$0.36 / hour")                     # keys with {placeholders}
    assert keys.has("too many sign-in attempts — wait 14 min")               # "{what} — wait {n} min"
    assert keys.has('“<b data-i18n="off">Work</b>” will be deleted along with its answer history and its own '
                    "materials. This can't be undone.", html=True)
    assert not keys.has("No dictionary has this string")


# ------------------------------------------------- the real engine on real dictionaries

NODE = shutil.which("node")

ENGINE_HARNESS = r"""
const fs = require("fs");
const SRC = fs.readFileSync(process.env.ENGINE_JS, "utf8");
const dict = JSON.parse(fs.readFileSync(process.env.DICT, "utf8"));
const html = JSON.parse(process.env.HTML_CASES);
const text = JSON.parse(process.env.TEXT_CASES);

function boot(lang, body) {
  const window = { ARGUS_LANG: lang, ARGUS_I18N: dict, addEventListener() {} };
  const document = { documentElement: {}, readyState: "complete", title: "", body, addEventListener() {} };
  class Observer { observe() {} }
  new Function("window", "document", "MutationObserver", SRC)(window, document, Observer);
  return window;
}

// an element with nested tags: the engine compares its whole markup with the key
// (`htmlElement`)
const out = { html: [], text: [] };
for (const [inner, tags] of html) {
  const p = { nodeType: 1, tagName: "P", innerHTML: inner, parentNode: null, firstChild: null,
              children: tags.map((t) => ({ tagName: t })), getAttribute() { return null; }, setAttribute() {} };
  boot(process.env.LANG_CODE, p);
  out.html.push(p.innerHTML);
}
const win = boot(process.env.LANG_CODE, null);
for (const s of text) out.text.push(win.argusT(s));
console.log(JSON.stringify(out));
"""


def _engine(lang: str, html_cases: list, text_cases: list) -> dict:
    env = {**os.environ, "ENGINE_JS": str(DIR / "engine.js"), "DICT": str(DIR / f"{lang}.json"),
           "LANG_CODE": lang, "HTML_CASES": json.dumps(html_cases), "TEXT_CASES": json.dumps(text_cases)}
    done = subprocess.run([NODE, "-e", ENGINE_HARNESS], capture_output=True, text=True, encoding="utf-8", env=env,
                          timeout=60, check=True)
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="node is needed to run the engine")
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_engine_translates_what_the_home_and_record_pages_really_draw(lang):
    # The home dialogs and the "Recording" banner put markup with the session name into an
    # element, in `<b data-i18n="off">`; the key is the whole markup. Checked with the real
    # `engine.js` and the real language dictionary: a key with a tag and a placeholder is found,
    # the name stays untouched, and the translation is put in place.
    d = json.loads((DIR / f"{lang}.json").read_text(encoding="utf-8"))
    stop = ('Recognition is running in the session “<b data-i18n="off">Work</b>”. '
            "If you open another session, it will stop.")
    delete = ('“<b data-i18n="off">Exam &amp; Co</b>” will be deleted along with its answer history and its own '
              "materials. This can't be undone.")
    switch = ('Recording is running in the session “<b data-i18n="off">Call</b>”. '
              "If you switch here, it will stop.")
    got = _engine(lang, [[stop, ["B"]], [delete, ["B"]], [switch, ["B"]]], [
        "Today", "5 min", "$0.36 / hour", "$0.36 / min", "Waiting for payment — $5", "Pay $10", "up to 1 200",
        "too many sign-in attempts — wait 14 min", "too many sign-in attempts — wait a minute",
        "your balance is empty — top up from $5, it's in the settings", "Balance topped up by $5",
    ])

    def fill(key: str, **slots: str) -> str:
        value = d[key]
        for name, v in slots.items():
            value = value.replace("{" + name + "}", v)
        return value

    stop_key = ('Recognition is running in the session “<b data-i18n="off">{name}</b>”. '
                "If you open another session, it will stop.")
    delete_key = ('“<b data-i18n="off">{name}</b>” will be deleted along with its answer history and its own '
                  "materials. This can't be undone.")
    switch_key = ('Recording is running in the session “<b data-i18n="off">{name}</b>”. '
                  "If you switch here, it will stop.")
    assert got["html"] == [fill(stop_key, name="Work"), fill(delete_key, name="Exam &amp; Co"),
                           fill(switch_key, name="Call")]
    assert got["text"] == [
        d["Today"], fill("{n} min", n="5"), fill("{price} / hour", price="$0.36"), fill("{price} / min", price="$0.36"),
        fill("Waiting for payment — {sum}", sum="$5"), fill("Pay {sum}", sum="$10"), fill("up to {n}", n="1 200"),
        # the value placeholder is also a known string: "too many sign-in attempts" is translated
        # inside the phrase
        fill("{what} — wait {n} min", what=d["too many sign-in attempts"], n="14"),
        fill("{what} — wait a minute", what=d["too many sign-in attempts"]),
        fill("your balance is empty — top up from {sum}, it's in the settings", sum="$5"),
        fill("Balance topped up by {sum}", sum="$5"),
    ]
