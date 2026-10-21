"""Terms and policy in all the application languages.

Requirement (2026-10-15): localize the rules and terms into all languages; before, the
documents were English only. The original is English (`legal/text/<document>.html`),
translations are `legal/text/<language>/<document>.html`; each page carries all the
languages at once, and the page script shows one (`legal/template.html`,
`landing/subpages.py`).

The meaning of a translation cannot be checked (the project has no lawyer, this is
written in `legal/README.md`), so the test guards what breaks silently: a lost paragraph
or term, foreign markup, the operator substituted into a foreign sentence, the operator's
name and country (it is forbidden to write them anywhere), and also the choice of language
by the page: the application shows the interface language, the site shows the chosen one,
then the browser language, then English.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from argus.account.legal import DOCS
from argus.ui.i18n import SUPPORTED

ROOT = Path(__file__).resolve().parents[1]
TEXT = ROOT / "legal" / "text"
NODE = shutil.which("node")
OPERATOR = {"name": "Acme Labs", "email": "legal@acme.example", "law": "the law of Freedonia"}


def load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validator = load("validate_translation", ROOT / "legal" / "validate_translation.py")
legal_render = load("legal_render", ROOT / "legal" / "legal_render.py")
builder = load("legal_build", ROOT / "legal" / "build.py")

TRANSLATED = [lang for lang in SUPPORTED if lang != "en"]


def test_the_languages_of_the_documents_are_the_languages_of_the_app():
    """A UI language was added, so the documents must get a translation instead of silently
    staying English.
    """
    assert tuple(legal_render.LANGS) == tuple(SUPPORTED)
    assert set(validator.LANGS) == set(TRANSLATED)


@pytest.mark.parametrize("lang", TRANSLATED)
def test_both_documents_are_translated(lang):
    for doc in DOCS:
        assert (TEXT / lang / f"{doc}.html").is_file(), f"no translation {lang}/{doc}"


@pytest.mark.parametrize("lang", TRANSLATED)
def test_a_translation_keeps_the_structure_numbers_and_placeholders_of_the_original(lang):
    assert validator.check(lang) == []


@pytest.mark.parametrize("lang", TRANSLATED)
def test_a_translation_says_the_english_version_prevails(lang):
    """A translation is for convenience; if the two differ, the original is the one that holds.
    This is the sentence added in the second line.
    """
    for doc in DOCS:
        meta = re.search(r'<p class="meta">(.*?)</p>', (TEXT / lang / f"{doc}.html").read_text(encoding="utf-8"), re.S).group(1)
        meta_en = re.search(r'<p class="meta">(.*?)</p>', (TEXT / f"{doc}.html").read_text(encoding="utf-8"), re.S).group(1)
        assert len(meta) > len(meta_en) * 1.3, f"{lang}/{doc}: the version line has no note about the English original"


def test_the_operator_is_not_described_as_a_person_in_any_language():
    """The operator is identified only by the service name: no legal form of an individual is written."""
    # The Ukrainian and Russian words of the list ("підприєм", "предпринимател") are language data.
    banned = re.compile(r"sole (?:proprietor|trader)|individual entrepreneur|"
                        r"einzelunternehmer|entrepreneur individuel|empresario individual|empres[aá]rio individual|"
                        r"şahıs|jednoosobow|підприєм|предпринимател", re.IGNORECASE)
    for path in sorted(TEXT.rglob("*.html")):
        assert not banned.search(path.read_text(encoding="utf-8")), path.relative_to(ROOT)
    operator = (ROOT / "legal" / "operator.json").read_text(encoding="utf-8")
    assert not banned.search(operator)
    assert "country" not in json.loads(operator)


@pytest.mark.parametrize("lang", TRANSLATED)
def test_a_translation_cannot_pull_the_operator_in_as_an_english_phrase(lang):
    for slot in ("name", "law", "country"):
        with pytest.raises(ValueError):
            legal_render.render("{{%s}}" % slot, OPERATOR, "2026-10-14", set(), lang)


@pytest.mark.parametrize("lang, want", [
    ("en", "October 14, 2026"), ("ru", "14 октября 2026 г."), ("uk", "14 жовтня 2026 р."),
    ("es", "14 de octubre de 2026"), ("pt", "14 de outubro de 2026"), ("de", "14. Oktober 2026"),
    ("fr", "14 octobre 2026"), ("tr", "14 Ekim 2026"), ("pl", "14 października 2026 r."),
])
def test_the_date_is_written_the_way_the_language_writes_it(lang, want):
    assert legal_render.human_date("2026-10-14", lang) == want


def test_the_first_of_the_month_in_french_is_premier():
    assert legal_render.human_date("2026-10-01", "fr") == "1er octobre 2026"


def test_a_sentence_after_an_abbreviated_year_does_not_get_a_second_dot():
    # Russian input on purpose: the Russian year abbreviation "г." is what is being tested.
    out = legal_render.render("Версия от {{version}}. Далее.", OPERATOR, "2026-10-14", set(), "ru")
    assert out == "Версия от 14 октября 2026 г. Далее."
    assert ".." not in legal_render.render("Wersja z dnia {{version}}.", OPERATOR, "2026-10-14", set(), "pl")


def test_a_missing_translation_is_reported_and_blocks_a_release(tmp_path):
    root = tmp_path / "text"
    shutil.copytree(TEXT, root)
    shutil.rmtree(root / "pl")
    parts, absent = legal_render.articles("terms", OPERATOR, "2026-10-14", set(), root=root)
    assert absent == ["pl"] and 'data-doc-lang="pl"' not in parts


# ------------------------------------------------------------------------ built pages


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    out = tmp_path_factory.mktemp("legal")
    assert builder.build(((out, ""),), OPERATOR) == set()
    return {doc: (out / f"{doc}.html").read_text(encoding="utf-8") for doc in DOCS}


@pytest.mark.parametrize("doc", DOCS)
def test_the_page_carries_every_language_and_shows_english_without_scripts(pages, doc):
    page = pages[doc]
    articles = re.findall(r'<article lang="(\w+)" data-doc-lang="\1"( hidden)?>', page)
    assert [lang for lang, _ in articles] == list(SUPPORTED)
    assert [lang for lang, hidden in articles if not hidden] == ["en"]
    assert page.count('<button type="button" data-lang=') == len(SUPPORTED)
    assert "{{" not in page and "Acme Labs" in page


@pytest.mark.parametrize("doc", DOCS)
def test_the_language_data_in_the_page_is_valid_json(pages, doc):
    ui = json.loads(re.search(r"var UI = (\{.*?\});\n", pages[doc], re.S).group(1))
    assert set(ui) == set(SUPPORTED)
    assert all({"name", "terms", "privacy", "download"} <= set(v) for v in ui.values())
    langs = json.loads(re.search(r"var LANGS = (\[.*?\]);", pages[doc]).group(1))
    assert langs == list(SUPPORTED)


@pytest.mark.parametrize("doc", DOCS)
def test_every_article_ends_with_links_in_its_own_language(pages, doc):
    other = "privacy" if doc == "terms" else "terms"
    for lang in SUPPORTED:
        article = re.search(rf'<article lang="{lang}".*?</article>', pages[doc], re.S).group(0)
        assert f'<a href="{other}.html">{legal_render.UI[lang][other]}</a>' in article, lang
        assert legal_render.UI[lang]["download"] in article, lang


def test_the_language_bar_is_flat_text_without_outlines(pages):
    css = re.search(r"\.langbar\{.*?article\[hidden\]", pages["terms"], re.S).group(0)
    assert "border:0" in css and not re.search(r"border(?!:0)\s*:", css) and "box-shadow" not in css


@pytest.mark.parametrize("doc", DOCS)
def test_the_landing_pages_carry_every_language_too(doc):
    sys.path.insert(0, str(ROOT / "landing"))
    import subpages

    body, missing = subpages.doc_body(doc)
    assert missing == set()
    assert body.count("data-doc-lang=") == len(SUPPORTED)
    assert body.count('class="doc-other"') == len(SUPPORTED)
    assert "SUB_JS" not in body and 'article[data-doc-lang]' in subpages.SUB_JS


# ------------------------------------------------------------------------ language choice by the page script

HARNESS = r"""
const langs = JSON.parse(process.env.LANGS);
const articles = langs.map((l) => ({ hidden: null, getAttribute: () => l }));
globalThis.document = { documentElement: {}, title: "", querySelectorAll(sel) {
  return sel === "article[data-doc-lang]" ? articles : []; } };
globalThis.location = { pathname: process.env.PATHNAME, search: process.env.SEARCH };
if (process.env.APPLANG !== undefined) globalThis.window = { ARGUS_LANG: process.env.APPLANG || undefined };
// Node 24 itself has its own `navigator` with the system language: without an explicit
// override the test would depend on the machine.
Object.defineProperty(globalThis, "navigator", { value: process.env.NAV ? { language: process.env.NAV } : undefined, configurable: true });
if (process.env.SAVED) globalThis.localStorage = { getItem: () => process.env.SAVED, setItem() {} };
new Function(process.env.SCRIPT)();
console.log(articles.filter((a) => a.hidden === false).map((a) => a.getAttribute()).join(","), globalThis.document.documentElement.lang, globalThis.document.title);
"""


def shown(page: str, pathname: str, search: str = "", **env) -> tuple[str, str, str]:
    script = next(s for s in re.findall(r"<script>(.*?)</script>", page, re.S) if "argus.legal.lang" in s)
    run_env = {**os.environ, "LANGS": json.dumps(list(SUPPORTED)), "PATHNAME": pathname, "SEARCH": search, "SCRIPT": script}
    run_env.update({k.upper(): v for k, v in env.items()})
    out = subprocess.run([NODE, "-e", HARNESS], capture_output=True, text=True, encoding="utf-8", env=run_env, timeout=60,
                         check=True)
    visible, lang, title = out.stdout.strip().split(" ", 2)
    return visible, lang, title


@pytest.mark.skipif(NODE is None, reason="node is needed to run the page script")
@pytest.mark.parametrize("path, search, env, want", [
    ("/p/legal-terms", "", {"applang": "de"}, "de"),           # in the application: the interface language
    ("/p/legal-terms", "", {"applang": ""}, "en"),             # language not named: falls back to English (legal/template.html)
    ("/p/legal-terms", "", {"applang": "xx"}, "en"),           # a foreign language is treated as not named
    ("/p/legal-terms", "?embed=1", {"applang": "tr"}, "tr"),   # a window over the page
    ("/p/legal-terms", "?lang=es", {"applang": "de"}, "es"),   # an explicit `?lang=` wins
    ("/terms.html", "?lang=fr", {}, "fr"),                     # on the site: the language from the address
    ("/terms.html", "?lang=xx", {"nav": "pl-PL"}, "pl"),       # a foreign one in the address: the browser language
    ("/terms.html", "", {"nav": "tr-TR", "saved": "uk"}, "uk"),# the person's choice beats the browser language
    ("/terms.html", "", {"nav": "ja-JP"}, "en"),               # no language: English
    ("/terms.html", "", {}, "en"),                             # no browser data: English
])
def test_the_page_script_picks_the_language(pages, path, search, env, want):
    visible, lang, title = shown(pages["terms"], path, search, **env)
    assert visible == want and lang == want
    assert title == legal_render.UI[want]["terms"] + " — Argus"


# The legal text uses the polite form (decision of 2026-10-15): the application interface
# is on informal "you" in places, but the terms and policy read as documents. The ru, uk
# and pt languages (the pt "você" form) are already polite.
INFORMAL = {
    "de": r"\b(du|dich|dir|dein\w*|euch)\b",
    "fr": r"\b(tu|toi|ton|tes)\b",
    "es": r"\b(tú|tu|tus|ti|contigo)\b",
    "tr": r"\b(sen|senin|sana|seni)\b",
    "pl": r"\b(ty|twój|twoja|twoje|twoim|twojego|twoich|twojej)\b",
}


@pytest.mark.parametrize("lang", sorted(INFORMAL))
def test_the_documents_address_the_reader_formally(lang):
    rx = re.compile(INFORMAL[lang], re.IGNORECASE)
    for doc in DOCS:
        text = re.sub(r"<[^>]+>", " ", (TEXT / lang / f"{doc}.html").read_text(encoding="utf-8"))
        found = rx.findall(text)
        assert not found, f"{lang}/{doc}: informal address {found[:5]}"
