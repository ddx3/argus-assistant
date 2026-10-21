"""The shared part of building the terms and the policy: languages, dates, operator
substitution, articles in all languages at once.

Only the standard library: the module is used by `legal/build.py` (the download page and the
copy inside the app) and by `landing/subpages.py` (the site pages), and the landing builder
has its own environment without the `argus` package.

The documents are written in English (`legal/text/<document>.html`, the original), and the
translations lie next to them by language (`legal/text/<lang>/<document>.html`). Requirement:
localize the terms and conditions into all languages — before that they were English only.
Each page carries ALL the languages at once — one `<article lang=…>` per language, one is
visible — and the choice is made by the page script: in the app by the interface language, on
the site by the language chosen on the landing page or in the browser and by the switcher. So
neither separate addresses per language, nor nginx changes, nor file selection on the app side
are needed.

Substitutions: in the English text — `{{version}}`, `{{name}}`, `{{email}}`, `{{law}}` (values
from `operator.json`); in the translations — only `{{version}}` (the date in the language's
format) and `{{email}}`: the operator and the law are written directly there, in the needed
grammatical case ("команда Argus" = "the Argus team", "применимым правом" = "under the
applicable law"), because an English phrase cannot be inflected inside a foreign sentence. If
the operator changes, you must edit the English text through `operator.json` and ALL the
translations by hand (`grep -rn "команда Argus" legal/text`).
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT = HERE / "text"

# The order and the set are as in the app (`ui/i18n.py: SUPPORTED`).
LANGS = ("ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl")
DEFAULT = "en"

SLOT = re.compile(r"\{\{(\w+)\}\}")

# What an empty operator field means — for the marker in the English document.
HINT = {
    "name": "operator's name",
    "email": "contact email",
    "country": "operator's country",
    "law": "governing law",
}

# Substitutions that are forbidden in translations (see the module docstring).
OPERATOR_ONLY = ("name", "law", "country")

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"],
    "uk": ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня",
           "листопада", "грудня"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
           "noviembre", "diciembre"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
           "novembro", "dezembro"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre"],
    "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
           "Kasım", "Aralık"],
    "pl": ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
           "października", "listopada", "grudnia"],
}
# How a date is written. `{d}` — day, `{m}` — month, `{y}` — year. The text may put the period
# after the year abbreviation ("г."/"р." and the like) itself: a double period collapses in
# `render`.
DATE_FORMAT = {
    "en": "{m} {d}, {y}",
    "ru": "{d} {m} {y} г.",
    "uk": "{d} {m} {y} р.",
    "es": "{d} de {m} de {y}",
    "pt": "{d} de {m} de {y}",
    "de": "{d}. {m} {y}",
    "fr": "{d} {m} {y}",
    "tr": "{d} {m} {y}",
    "pl": "{d} {m} {y} r.",
}

# The page's own captions in each language: the language name in the switcher, the document
# titles (as in the app interface, `ui/static/i18n/<lang>.json`), "download".
UI = {
    "en": {"name": "English", "terms": "Terms of Use", "privacy": "Privacy Policy", "download": "Download Argus"},
    "ru": {"name": "Русский", "terms": "Условия использования", "privacy": "Политика конфиденциальности",
           "download": "Скачать Argus"},
    "uk": {"name": "Українська", "terms": "Умови використання", "privacy": "Політика конфіденційності",
           "download": "Завантажити Argus"},
    "es": {"name": "Español", "terms": "Términos de uso", "privacy": "Política de privacidad",
           "download": "Descargar Argus"},
    "pt": {"name": "Português", "terms": "Termos de uso", "privacy": "Política de privacidade",
           "download": "Baixar o Argus"},
    "de": {"name": "Deutsch", "terms": "Nutzungsbedingungen", "privacy": "Datenschutzerklärung",
           "download": "Argus herunterladen"},
    "fr": {"name": "Français", "terms": "Conditions d'utilisation", "privacy": "Politique de confidentialité",
           "download": "Télécharger Argus"},
    "tr": {"name": "Türkçe", "terms": "Kullanım Koşulları", "privacy": "Gizlilik Politikası",
           "download": "Argus'u indir"},
    "pl": {"name": "Polski", "terms": "Warunki korzystania", "privacy": "Polityka prywatności",
           "download": "Pobierz Argus"},
}


def human_date(version: str, lang: str = DEFAULT) -> str:
    """`2026-10-06` -> «October 6, 2026» / «6 октября 2026 г.» / «6. Oktober 2026»…"""
    y, m, d = (int(x) for x in version.split("-"))
    day = "1er" if (lang == "fr" and d == 1) else str(d)
    return DATE_FORMAT[lang].format(d=day, m=MONTHS[lang][m - 1], y=y)


def doc_path(doc: str, lang: str, root: Path = TEXT) -> Path:
    """Document file: English is `text/<document>.html`, translations are
    `text/<language>/<document>.html`.
    """
    return root / f"{doc}.html" if lang == DEFAULT else root / lang / f"{doc}.html"


def translations(doc: str, root: Path = TEXT) -> list[str]:
    """Languages the document exists in (English always comes first, decided by whether its file
    exists, not by sort order).
    """
    return [lang for lang in LANGS if doc_path(doc, lang, root).is_file()]


def render(text: str, operator: dict, version: str, missing: set[str], lang: str = DEFAULT) -> str:
    """Substitute the values. An empty operator field becomes a yellow marker (the field name goes
    into `missing`).
    """

    def put(m: re.Match) -> str:
        key = m.group(1)
        if key == "version":
            return human_date(version, lang)
        if lang != DEFAULT and key in OPERATOR_ONLY:
            raise ValueError(f"{lang}: substitution {{{{{key}}}}} is forbidden in a translation - write the phrase out directly, in the required form")
        value = str(operator.get(key, "")).strip()
        if value:
            return html.escape(value, quote=False)
        missing.add(key)
        # Markup has no place in a `mailto:` address: it would break the attribute.
        if text[max(0, m.start() - 7):m.start()] == "mailto:":
            return f"TODO-{key}"
        return f'<mark class="todo">[{html.escape(HINT.get(key, key))}]</mark>'

    out = SLOT.sub(put, text)
    # A date ends with a period (as in "2026 г.", the `ru` form), and the sentence around it adds a
    # period too.
    return re.sub(r"(\d{4} (?:г|р|r)\.)\.", r"\1", out)     # the `ru` "г.", the `uk` "р.", the `pl` "r."


Footer = Callable[[str, str], str]


def articles(doc: str, operator: dict, version: str, missing: set[str], footer: Footer | None = None,
             root: Path = TEXT) -> tuple[str, list[str]]:
    """All the languages of a document: one `<article>` per language, the English one is visible
    (the others are `hidden`, and the script turns them on).

    `footer(lang, doc)` is the markup that closes the article (links to the neighboring document
    and "download" in this language): it differs between the download page and the landing pages.
    Returns (markup, languages without a translation).
    """
    if not doc_path(doc, DEFAULT, root).is_file():
        raise FileNotFoundError(doc_path(doc, DEFAULT, root))
    parts, absent = [], []
    for lang in LANGS:
        path = doc_path(doc, lang, root)
        if not path.is_file():
            absent.append(lang)
            continue
        body = render(path.read_text(encoding="utf-8"), operator, version, missing, lang).rstrip()
        hidden = "" if lang == DEFAULT else " hidden"
        tail = footer(lang, doc) if footer else ""
        parts.append(f'<article lang="{lang}" data-doc-lang="{lang}"{hidden}>\n{body}\n{tail}</article>')
    return "\n".join(parts), absent
