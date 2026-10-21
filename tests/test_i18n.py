"""Interface languages: one list, full dictionaries, pages get the translation.

There are three places for languages and they must agree: the list of answers in
`assist/prompt.py` (both the interface languages and the recognition languages are built
from it), `SUPPORTED` in `ui/i18n.py` and the dictionaries in `ui/static/i18n/`. This is
checked here so that a new language cannot be added in one place and forgotten in the
others.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from argus.assist import prompt as assist_prompt
from argus.ui import admin, i18n

ROOT = Path(__file__).resolve().parents[1]
DIR = Path(i18n.STATIC) / "i18n"
sys.path.insert(0, str(ROOT / "i18n"))

PLACE = re.compile(r"\{[a-z_]+\}")
TAG = re.compile(r"</?[a-z]+[^>]*>")


def _keys() -> list[str]:
    return [e["k"] for e in json.loads((DIR / "keys.json").read_text(encoding="utf-8"))["keys"]]


def test_one_list_of_languages():
    answers = [code for code, _label, _rule in assist_prompt.LANGUAGES if code]
    assert tuple(answers) == i18n.SUPPORTED
    assert [c for c, _ in admin.APP_LANGUAGES] == ["auto", *i18n.SUPPORTED]
    assert [c for c, _ in admin.RECOGNITION_LANGUAGES] == ["", *i18n.SUPPORTED]


def test_every_answer_language_has_its_own_rule():
    for code, _label, rule in assist_prompt.LANGUAGES:
        assert rule.strip(), code
    rules = [r for _c, _l, r in assist_prompt.LANGUAGES]
    assert len(set(rules)) == len(rules), "two languages share the same rule"


@pytest.mark.parametrize("lang", [c for c in i18n.SUPPORTED if c != "en"])
def test_dictionary_is_complete(lang):
    d = json.loads((DIR / f"{lang}.json").read_text(encoding="utf-8"))
    for key in _keys():
        assert key in d, f"{lang}: no translation for {key[:60]!r}"
        value = d[key]
        assert isinstance(value, str) and value.strip(), f"{lang}: empty translation for {key[:60]!r}"
        assert sorted(PLACE.findall(key)) == sorted(PLACE.findall(value)), f"{lang}: slots of {key[:60]!r}"
        assert sorted(TAG.findall(key)) == sorted(TAG.findall(value)), f"{lang}: tags of {key[:60]!r}"


@pytest.mark.parametrize(
    ("setting", "system", "expected"),
    [
        ("es", "ru", "es"),            # the person's choice matters more than the system language
        ("auto", "ru", "ru"),
        ("auto", "de", "de"),
        ("auto", "pt", "pt"),
        ("auto", "be", "ru"),          # there is no translation for that language, they read in Russian
        ("auto", "zh", "en"),          # no translation: English
        ("auto", "", "en"),
        ("", "pl", "pl"),              # empty: same as "auto"
        ("klingon", "fr", "fr"),       # an unknown value is treated like "auto"
    ],
)
def test_resolve(setting, system, expected):
    assert i18n.resolve(setting, system) == expected


def test_default_is_auto():
    from argus.assist.settings import Interface

    assert Interface().language == "auto"


def test_english_gets_only_the_marker():
    html = "<html><head><title>x</title></head><body>Microphone</body></html>"
    out = i18n.inject(html, "en")
    assert 'window.ARGUS_LANG="en"' in out
    assert "ARGUS_I18N=" not in out


def test_other_language_gets_dictionary_and_engine():
    html = "<!doctype html><html><head><title>x</title></head><body>Microphone</body></html>"
    out = i18n.inject(html, "es")
    assert out.index("ARGUS_I18N=") < out.index("<title>"), "the dictionary must come before the page code"
    assert "Micrófono" in out
    assert "MutationObserver" in out
    assert i18n.inject(out, "es") == out, "a second insertion changes nothing"


def _client(language: str) -> TestClient:
    web = FastAPI()

    @web.get("/", response_class=HTMLResponse)
    def page():
        return "<html><head></head><body>Microphone</body></html>"

    @web.get("/data")
    def data():
        return JSONResponse({"x": "Microphone"})

    class Cfg:
        channel = {"assist": {"interface": {"language": language}}}

    class Argus:
        cfg = Cfg()

    i18n.install(web, Argus())
    return TestClient(web)


def test_middleware_injects_only_into_html():
    c = _client("de")
    page = c.get("/")
    assert page.status_code == 200
    assert "ARGUS_I18N=" in page.text
    assert int(page.headers["content-length"]) == len(page.content)
    assert c.get("/data").json() == {"x": "Microphone"}


def test_middleware_follows_the_setting():
    assert 'ARGUS_LANG="uk"' in _client("uk").get("/").text
    assert "ARGUS_I18N=" not in _client("en").get("/").text


def test_strip_gives_back_the_file():
    html = "<!doctype html><html><head><title>x</title></head><body>Microphone</body></html>"
    for lang in ("en", "es"):
        assert i18n.strip(i18n.inject(html, lang)) == html


def test_every_dictionary_loads_on_the_page():
    for lang in i18n.SUPPORTED:
        out = i18n.inject("<head></head>", lang)
        assert f'ARGUS_LANG="{lang}"' in out
