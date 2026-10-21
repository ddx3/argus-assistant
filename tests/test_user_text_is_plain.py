"""For people, no internals of the app: no models, no services, no "tokens" and "API".

Requirement: nowhere in the project should the technical implementation, models, APIs and
so on be mentioned. What the person sees (settings labels and pages, errors, documents,
the landing page, translations) names no model, no service and no library, and says
nothing about tokens, keys and the relay. Code, docstrings and the log are for the
developer and are not covered.

A guard over the places where such text lives. A new word in the `FORBIDDEN` list is also
an edit of this file: the test is deliberately one for all the places, so that the list
does not drift apart.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "argus"
STATIC = SRC / "ui" / "static"
I18N = STATIC / "i18n"

# Names of models, services and libraries: they show what the product is built on.
NAMES = (
    r"openai|anthropic|claude|gpt|gemini|whisper|elevenlabs|deepgram|liqpay|silero|rapidocr|tesseract"
    r"|argus\.toml|argus\.local|sync-voices|import-voice"
)
# Words about the internals: model, tokens, keys, relay, pipeline, engine, brain. The interface
# is English, so the English words are listed (and the German, French and Spanish ones for the
# dictionaries). The Russian roots at the start are language data: the Russian dictionary
# (`ru.json`) has Russian values, and they must be as clean as the English ones. There is no
# need to cut off "моделировать" (to model) with a three-character stem: there are no such
# words in the interface at all, so the whole root is taken.
WORDS = (
    r"модел|токен|релей|конвейер|движок|мозг|ключ[а-яё]*\s+api|api[- ]ключ"
    r"|\bapi\b|\btokens?\b|\bmodels?\b|\bmodell|\bmodelo\b|\bmodèle|\bmodèles\b|\brelay\b|\bhash(es)?\b|\bllm\b"
    r"|\bpipeline\b|\bjson\b|\bjeton|\bengine\b|\bbrain\b"
)
FORBIDDEN = re.compile(NAMES + "|" + WORDS, re.IGNORECASE)

# Pages and scripts: `api`, `json`, `WebSocket` legitimately appear in the code, so there
# we look only for names and for the words about the internals that cannot be code identifiers
# (the English ones, and the Russian roots for Russian text), and not in comments. "brain" is
# also the name of an icon in `account.js`: it counts only as a word of its own, not as a quoted
# or keyed identifier.
CODE_FORBIDDEN = re.compile(
    NAMES + r"|модел|токен|релей|конвейер|движок|мозг|ключ[а-яё]*\s+api|api[- ]ключ"
    r"|\bmodels?\b|\btokens?\b|\brelay\b|\bpipeline\b|\bengine\b|(?<![\"'])\bbrain\b(?![\"':])"
    r"|\bapi[- ]?keys?\b|\bkeys?\s+api\b",
    re.IGNORECASE,
)


def _found(text: str, pattern: re.Pattern = FORBIDDEN) -> list[str]:
    return sorted({m.group(0).lower() for m in pattern.finditer(text)})


# --------------------------------------------------------------------------
# Settings: labels, explanations, list items
# --------------------------------------------------------------------------


def test_the_settings_text_names_no_model_or_service():
    from argus.tts.settings import TTSSettings
    from argus.ui import admin

    bad = []
    for section in admin.sections(TTSSettings()):
        texts = [section.title, section.lead or ""]
        for setting in section.settings:
            texts += [setting.label or "", setting.hint or "", setting.placeholder or ""]
            texts += [label for _value, label in (setting.options or [])]
        for text in texts:
            if _found(text):
                bad.append((section.title, text[:80], _found(text)))
    assert not bad, bad


# --------------------------------------------------------------------------
# Translations: keys (English strings) and values in all languages
# --------------------------------------------------------------------------


def test_the_dictionaries_name_no_model_or_service():
    bad = []
    for path in sorted(I18N.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        items = (
            [(e["k"], e["k"]) for e in data["keys"]] if path.stem == "keys" else list(data.items())
        )
        for key, value in items:
            if _found(value) or _found(key):
                bad.append((path.stem, key[:60], value[:60], _found(value) + _found(key)))
    assert not bad, bad[:10]


# --------------------------------------------------------------------------
# App pages: visible text and code without comments
# --------------------------------------------------------------------------


def _without_comments(path: Path) -> str:
    sys.path.insert(0, str(ROOT / "landing"))
    try:
        from strip_comments import strip_html
    finally:
        sys.path.pop(0)
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".js":
        return strip_html(f"<script>{raw}</script>")
    if path.suffix == ".css":
        return strip_html(f"<style>{raw}</style>")
    return strip_html(raw)


@pytest.mark.parametrize(
    "name",
    ["index.html", "home.html", "record.html", "admin.html", "admin.js", "account.js", "chrome.js",
     "winbar.js", "legal-terms.html", "legal-privacy.html"],
)
def test_the_pages_name_no_model_or_service(name):
    assert not _found(_without_comments(STATIC / name), CODE_FORBIDDEN), name


# --------------------------------------------------------------------------
# Documents and the landing page
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["terms.html", "privacy.html"])
def test_the_documents_name_no_model_or_service(name):
    text = (ROOT / "legal" / "text" / name).read_text(encoding="utf-8")
    assert not _found(text), name


def test_the_landing_copy_names_no_model_or_service():
    bad = []
    for path in sorted((ROOT / "landing" / "copy").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            if isinstance(value, str) and _found(re.sub(r"<[^>]+>", " ", value)):
                bad.append((path.stem, key, _found(value)))
    assert not bad, bad


def test_the_landing_template_names_no_model_or_service():
    sys.path.insert(0, str(ROOT / "landing"))
    try:
        from strip_comments import strip_html
    finally:
        sys.path.pop(0)
    text = strip_html((ROOT / "landing" / "template.html").read_text(encoding="utf-8"))
    # a live page is markup with scripts; we look for what is not in the code
    assert not _found(text, CODE_FORBIDDEN)


def test_the_download_page_shows_no_build_internals():
    text = (ROOT / "deploy" / "server" / "downloads" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"commit", text, re.IGNORECASE), "a commit hash is of no use to a person"
    # The landing page lives at a hosted-artifact address that contains a service name: the
    # word is in the address itself, and it can be removed only by moving to our own domain.
    # The page text has no such names.
    text = re.sub(r"https://claude\.ai/artifact/\w+", "", text)
    assert not _found(text, CODE_FORBIDDEN)


# --------------------------------------------------------------------------
# Errors: what the code gives to the page and to the person
# --------------------------------------------------------------------------

# Exceptions whose text goes to the person as is, and the keys of dictionaries with such
# text.
HUMAN_EXCEPTIONS = {"HTTPException", "AccountError", "Denied", "BadEmail", "RecordError", "PdfError"}
HUMAN_KEYS = {"error", "detail", "reason"}


def _strings(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _human_messages() -> list[tuple[str, int, str]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            texts: list[str] = []
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name in HUMAN_EXCEPTIONS:
                    texts += [s for a in node.args for s in _strings(a)]
                    texts += [s for k in node.keywords for s in _strings(k.value)]
                texts += [s for k in node.keywords if k.arg in HUMAN_KEYS for s in _strings(k.value)]
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in HUMAN_KEYS and value is not None:
                        texts += _strings(value)
            for text in texts:
                out.append((str(path.relative_to(ROOT)), getattr(node, "lineno", 0), text))
    return out


def test_the_errors_people_see_name_no_model_or_service():
    # In an error text a word the person already knows is acceptable (account, password,
    # payment), but not a service name and not internals: those belong in the log.
    bad = [(p, line, text[:70], _found(text)) for p, line, text in _human_messages() if _found(text)]
    assert not bad, bad
