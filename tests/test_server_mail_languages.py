"""Emails and the recovery page in the person's language (the app's nine languages).

Requirement: emails must arrive in the language in which the person uses the app. The app
reports the language (`lang`) at registration, at sign-in and in a recovery request; the
server remembers it (`accounts.lang`) and writes everything in it, including the emails
that go out without a request (payment, low balance).

What is checked here: all nine languages have the same shape of texts (otherwise an email
would silently fail in one of the languages), the Latin-script ones have no Cyrillic, the
form of address is polite, there are no words about the app's internals, no name and no
country; and on the server, the language is remembered, garbage is dropped, emails go in
the remembered language, and the recovery link carries the language.
"""

from __future__ import annotations

import calendar
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.server import billing, mailevents, mailtemplate, mailtexts
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"
EMAIL = "user@example.com"
LANGS = ("ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl")
CYRILLIC = {"ru", "uk"}
EVENTS = ("reset", "welcome", "password_changed", "top_up", "low", "empty", "deleted")
SLOT = re.compile(r"\{(\w+)\}")


class Outbox:
    name = "fake"

    def __init__(self) -> None:
        self.letters: list[dict] = []

    def send(self, to, subject, text, html=None, images=None):
        self.letters.append({"to": to, "subject": subject, "text": text, "html": html, "images": images})


@pytest.fixture(autouse=True)
def default_language(monkeypatch):
    monkeypatch.delenv("ARGUS_MAIL_LANG", raising=False)


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def mail():
    return Outbox()


@pytest.fixture
def client(db, mail):
    with TestClient(build(db, SECRET, pay=billing.Stub(), mail=mail, base_url="https://api.example")) as c:
        yield c


def register(client, lang=None, email=EMAIL):
    body = {"email": email, "password": PASSWORD, "terms": "t"}
    if lang is not None:
        body["lang"] = lang
    r = client.post("/auth/register", json=body)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access']}"}


def stored(db, email=EMAIL) -> str:
    return db.execute("SELECT lang FROM accounts WHERE email = ?", (email,)).fetchone()["lang"]


def leaves(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from leaves(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from leaves(item)


# -- the texts themselves -----------------------------------------------------------------


def test_there_is_a_file_for_every_language_of_the_app():
    folder = Path(mailtexts.__file__).parent / "mail_texts"
    assert sorted(p.stem for p in folder.glob("*.json")) == sorted(LANGS)
    assert set(mailtexts.LANGS) == set(LANGS)
    ui = Path(mailtexts.__file__).parents[1] / "ui" / "static" / "i18n"
    assert {p.stem for p in ui.glob("??.json")} >= set(LANGS) - {"en"}, "the mail language is not one of the app's languages"


@pytest.mark.parametrize("lang", LANGS)
def test_a_language_has_the_same_shape_as_english_and_the_same_placeholders(lang):
    en, tr = mailtexts.DATA["en"], mailtexts.DATA[lang]

    def shape(node):
        if isinstance(node, dict):
            return {k: shape(v) for k, v in node.items()}
        if isinstance(node, list):
            return [shape(v) for v in node]
        return "s"

    # The numbers of paragraphs and lines must match, and the months must be exactly twelve.
    assert shape({k: v for k, v in tr.items() if k != "date"}) == shape({k: v for k, v in en.items() if k != "date"})
    assert len(tr["date"]["months"]) == 12 and len(set(tr["date"]["months"])) == 12
    for slot in ("{d}", "{m}", "{y}", "{time}"):
        assert slot in tr["date"]["format"]
    a = dict(enumerate(leaves({k: v for k, v in en.items() if k != "date"})))
    b = dict(enumerate(leaves({k: v for k, v in tr.items() if k != "date"})))
    for i in a:
        assert sorted(SLOT.findall(a[i])) == sorted(SLOT.findall(b[i])), (lang, a[i], b[i])


@pytest.mark.parametrize("lang", LANGS)
def test_the_script_matches_the_language(lang):
    text = " ".join(leaves(mailtexts.DATA[lang]))
    has_cyrillic = bool(re.search("[А-Яа-яІіЇїЄєҐґ]", text))
    assert has_cyrillic == (lang in CYRILLIC)


def test_a_translation_is_not_a_copy_of_english():
    en = mailtexts.DATA["en"]["events"]
    for lang in LANGS:
        if lang == "en":
            continue
        same = [e for e in EVENTS if mailtexts.DATA[lang]["events"][e]["subject"] == en[e]["subject"]]
        assert not same, f"{lang}: the subject is still English for {same}"


def test_nothing_in_the_letters_says_how_the_app_is_built():
    banned = re.compile(
        r"\b(api|gpt|openai|claude|anthropic|elevenlabs|liqpay|resend|token|токен|модел|model|релей|relay|"
        r"ключ|schlüssel|clave|chave|clé|anahtar|klucz)", re.IGNORECASE)
    for lang in LANGS:
        data = mailtexts.DATA[lang]
        found = banned.findall(" ".join(leaves({"events": data["events"], "footer": data["footer"]})))
        assert not found, (lang, found)
        assert not banned.findall(" ".join(leaves(data["reset_page"]))), lang


def test_nobody_is_addressed_informally():
    """Requirement: letters are written with the formal "you". Small markers of the informal
    "you" per language (not a full analysis, but common words).
    """
    informal = {
        "ru": r"\b(ты|тебя|тебе|твой|твоя|твоё|твои|нажми|введи|открой|зайди)\b",
        "uk": r"\b(ти|тебе|тобі|твій|твоя|твоє|твої|натисни|введи|відкрий)\b",
        "es": r"\b(tú|tu|tus|ti|puedes|tienes|haz|abre)\b",
        "pt": r"\b(tu|teu|tua|teus|tuas|você|clica|abre)\b",
        "de": r"\b(du|dein|deine|deinen|deinem|dir|dich|klicke|öffne)\b",
        "fr": r"\b(tu|toi|ton|ta|tes|clique|ouvre)\b",
        "tr": r"\b(sen|senin|sana|seni|bas|tıkla|aç)\b",
        "pl": r"\b(ty|ciebie|tobie|twój|twoje|twoja|twoich|naciśnij|kliknij|otwórz)\b",
        "en": r"$^",
    }
    for lang, rx in informal.items():
        text = " ".join(leaves({"events": mailtexts.DATA[lang]["events"], "reset_page": mailtexts.DATA[lang]["reset_page"]}))
        found = re.findall(rx, text, re.IGNORECASE)
        assert not found, (lang, found)


# -- words and dates ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("ru", "ru"), ("PT", "pt"), ("pt-BR", "pt"), ("uk_UA", "uk"), (" de ", "de"), ("en-US", "en"),
     ("", ""), ("xx", ""), ("klingon", ""), ("<script>", ""), (None, ""), (5, ""), ("-", "")],
)
def test_language_codes_are_normalised_or_dropped(raw, expected):
    assert mailtexts.normalize(raw) == expected


def test_dates_follow_the_rules_of_the_language_and_are_utc():
    moment = calendar.timegm((2026, 10, 15, 9, 5, 0))
    assert mailevents.when(moment, "ru") == "15 октября 2026, 09:05 UTC"
    assert mailevents.when(moment, "en") == "15 Oct 2026, 09:05 UTC"
    assert mailevents.when(moment, "de") == "15. Oktober 2026, 09:05 UTC"
    assert mailevents.when(moment, "es") == "15 de octubre de 2026, 09:05 UTC"
    for lang in LANGS:
        assert mailevents.when(moment, lang).endswith("09:05 UTC"), lang


def test_every_event_renders_in_every_language(db):
    """Every letter is built in every language without errors, no placeholders are left, and
    there is at least one paragraph.
    """
    for lang in LANGS:
        sent = [
            mailevents.reset_password(EMAIL, "https://api.example/auth/reset?lang=" + lang + "#token=x", 30, lang),
            mailevents.deleted(EMAIL, lang),
            (EMAIL, mailevents._compose("welcome", lang, email=EMAIL, gift=mailtexts.TEXTS["welcome"][lang]["gift"].format(amount="$0.30"))),
            (EMAIL, mailevents._compose("password_changed", lang, email=EMAIL, when=mailevents.when(1.8e9, lang))),
            (EMAIL, mailevents._compose("top_up", lang, rows=[("a", "b")])),
            (EMAIL, mailevents._compose("low", lang, balance="$0.40")),
            (EMAIL, mailevents._compose("empty", lang)),
        ]
        for _, message in sent:
            assert not SLOT.search(message.text), (lang, message.subject)
            assert f'lang="{lang}"' in message.html
            assert mailtemplate.SUPPORT in message.text
        footers = {m.text.rsplit("\n", 1)[-1] for _, m in sent}
        assert len(footers) == 1 and "{support}" not in footers.pop()


# -- the server remembers the language ----------------------------------------------------


def test_registration_remembers_the_language_and_the_welcome_is_in_it(client, mail, db):
    register(client, "de")
    assert stored(db) == "de"
    [letter] = mail.letters
    assert letter["subject"] == mailtexts.TEXTS["welcome"]["de"]["subject"]
    assert 'lang="de"' in letter["html"]


def test_an_old_app_without_a_language_gets_the_default(client, mail, db):
    register(client)
    assert stored(db) == ""
    assert mail.letters[0]["subject"] == mailtexts.TEXTS["welcome"]["en"]["subject"]


def test_a_hostile_language_is_dropped_not_stored_and_not_shown(client, mail, db):
    register(client, "<b>x</b>")
    assert stored(db) == ""
    assert "<b>x</b>" not in mail.letters[0]["html"] + mail.letters[0]["text"]
    r = client.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD, "terms": "t",
                                            "lang": "x" * 40})
    assert r.status_code == 422, "a too long language field: the request is refused, not written to the database"


def test_signing_in_updates_the_language_and_an_empty_one_keeps_it(client, mail, db):
    register(client, "fr")
    client.post("/auth/sign-in", json={"email": EMAIL, "password": PASSWORD, "lang": "pl"})
    assert stored(db) == "pl"
    client.post("/auth/sign-in", json={"email": EMAIL, "password": PASSWORD})
    assert stored(db) == "pl", "an old app without a language must not erase the language"
    client.post("/auth/sign-in", json={"email": EMAIL, "password": PASSWORD, "lang": "nonsense"})
    assert stored(db) == "pl"


def test_a_wrong_password_does_not_change_the_language(client, db):
    register(client, "fr")
    r = client.post("/auth/sign-in", json={"email": EMAIL, "password": "not-the-right-password-at-all", "lang": "tr"})
    assert r.status_code == 403
    assert stored(db) == "fr", "a stranger cannot change the language of the letters without knowing the password"


def test_reset_request_uses_the_request_language_and_puts_it_in_the_link(client, mail, db):
    register(client, "es")
    mail.letters.clear()
    r = client.post("/auth/reset/request", json={"email": EMAIL, "lang": "tr"})
    assert r.status_code == 204
    [letter] = mail.letters
    assert letter["subject"] == mailtexts.TEXTS["reset"]["tr"]["subject"]
    link = re.search(r"https://\S+", letter["text"]).group(0)
    assert link.startswith("https://api.example/auth/reset?lang=tr#token=")
    assert stored(db) == "tr"


def test_reset_request_for_a_stranger_does_not_create_an_account_language(client, mail, db):
    """A request for someone else's address writes nothing to the database and, as before, does
    not reveal whether an account exists.
    """
    r = client.post("/auth/reset/request", json={"email": "nobody@example.com", "lang": "de"})
    assert r.status_code == 204 and mail.letters == []
    assert db.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0


def test_reset_without_a_language_falls_back_to_the_stored_one(client, mail):
    register(client, "uk")
    mail.letters.clear()
    client.post("/auth/reset/request", json={"email": EMAIL})
    [letter] = mail.letters
    assert letter["subject"] == mailtexts.TEXTS["reset"]["uk"]["subject"]
    assert "/auth/reset?lang=uk#token=" in letter["text"]


def test_a_reset_for_an_account_without_a_language_keeps_the_old_plain_link(client, mail):
    register(client)
    mail.letters.clear()
    client.post("/auth/reset/request", json={"email": EMAIL})
    link = re.search(r"https://\S+", mail.letters[0]["text"]).group(0)
    assert link.startswith("https://api.example/auth/reset#token=")


def test_later_letters_use_the_stored_language(client, mail, db):
    headers = register(client, "pt")
    mail.letters.clear()
    client.post("/auth/password", json={"old": PASSWORD, "new": "completely-different-long-password"}, headers=headers)
    [changed] = mail.letters
    assert changed["subject"] == mailtexts.TEXTS["password_changed"]["pt"]["subject"]
    assert re.search(r"\d{1,2} de [a-zç]+ de \d{4}, \d\d:\d\d UTC", changed["text"])


def test_top_up_is_in_the_stored_language(client, mail):
    headers = register(client, "de")
    mail.letters.clear()
    order = client.post("/billing/checkout", json={"dollars": 5}, headers=headers).json()["order"]
    assert client.post(f"/billing/stub/{order}").status_code == 200
    [letter] = mail.letters
    assert letter["subject"] == mailtexts.TEXTS["top_up"]["de"]["subject"]
    assert "Betrag: $5.00" in letter["text"] or "Betrag" in letter["text"]
    assert re.search(r"\d{1,2}\. [A-Za-zä]+ \d{4}, \d\d:\d\d UTC", letter["text"])


def test_balance_notices_are_in_the_stored_language(client, db):
    register(client, "de")
    account = db.execute("SELECT id FROM accounts WHERE email = ?", (EMAIL,)).fetchone()["id"]
    db.execute("INSERT INTO usage (account_id, kind, amount, at) VALUES (?, 'stt_seconds', 10, ?)",
               (account, time.time()))
    db.execute("UPDATE accounts SET balance_micro = 0 WHERE id = ?", (account,))
    db.commit()
    [due] = mailevents.balances_due(db)
    assert due.message.subject == mailtexts.TEXTS["empty"]["de"]["subject"]
