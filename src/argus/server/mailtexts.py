"""The words of the emails and of the recovery page in the app's nine languages.

The source is `mail_texts/<language>.json` (one file per language, all with the same shape;
`tools/validate_mail_texts.py` and the tests check it). The email code (`mailevents.py`)
and the design (`mailtemplate.py`) hold no words, they only take them from here.

The app tells the server the person's language (the `lang` field at registration, sign-in,
and the recovery request); the server remembers it in `accounts.lang`. The app's value is
one of `LANGS`; everything else (`pt-BR`, `uk_UA`, empty, garbage) is reduced to one of
them or dropped: a language never reaches an email or a page as is.
"""
from __future__ import annotations

import json
from pathlib import Path

# The same order as in the app (`ui/static/i18n`): the project's home languages come first.
LANGS = ("ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl")

_DIR = Path(__file__).with_name("mail_texts")
DATA: dict[str, dict] = {lang: json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS}

# `TEXTS[event][language]` -> {subject, heading, paragraphs, ...}: the shape in which
# `mailevents` builds the emails.
TEXTS: dict[str, dict[str, dict]] = {
    event: {lang: DATA[lang]["events"][event] for lang in LANGS} for event in DATA["en"]["events"]
}


def normalize(raw: object) -> str:
    """Language from the request -> one of `LANGS` or `""`. `pt-BR`, `PT`, `uk_UA` -> `pt`, `pt`,
    `uk`; garbage -> `""`.
    """
    if not isinstance(raw, str):
        return ""
    code = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in LANGS else ""


def footer(lang: str) -> str:
    """The bottom line of the email; `{support}` is substituted by the layout."""
    return DATA[lang if lang in DATA else "en"]["footer"]


def reset_page(lang: str) -> dict[str, str]:
    """Labels of the "new password" page."""
    return DATA[lang if lang in DATA else "en"]["reset_page"]


def date_text(lang: str, year: int, month: int, day: int, time: str) -> str:
    """For example, in Russian: `15 октября 2026, 10:00 UTC`, by the rules of the language. The time
    is always UTC: the server does not know the person's time zone.
    """
    d = DATA[lang if lang in DATA else "en"]["date"]
    return d["format"].format(d=day, m=d["months"][month - 1], y=year, time=time)
