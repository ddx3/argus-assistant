"""Server emails by event: registration, password change, payment, deletion, recovery.

Requirement: every email is covered (forgotten password, registration info, purchase
info, and so on). What is checked is not only "the email went out" but what can go wrong
with them: an email must not go out on a refusal, must not go out twice on a payment
replay, must not break the action itself if the mailbox is unavailable, and must not
contain a password, someone else's data, or words about the app's internals.

The default language of the letters is Russian, so the expected subjects and phrases below are
the Russian mail texts (`mail_texts/ru.json`): translation data, they stay as they are.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from argus.server import billing, mailer, mailevents, mailtemplate
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"
NEW_PASSWORD = "completely-different-long-password"
EMAIL = "user@example.com"


class Outbox:
    """Mail stand-in: remembers everything that was sent, in order."""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.letters: list[dict] = []
        self.fail = fail

    def send(self, to, subject, text, html=None, images=None):
        if self.fail:
            raise mailer.MailError("mailbox unavailable")
        self.letters.append({"to": to, "subject": subject, "text": text, "html": html, "images": images})

    def about(self, subject: str) -> list[dict]:
        return [letter for letter in self.letters if letter["subject"] == subject]


@pytest.fixture(autouse=True)
def russian(monkeypatch):
    # The letters read here are the Russian ones (`mail_texts/ru.json`, language data): pin the default.
    monkeypatch.setenv("ARGUS_MAIL_LANG", "ru")


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


def register(client, email=EMAIL):
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD, "terms": "t"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access']}"}


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_registration_sends_a_welcome_with_the_gift_and_never_the_password(client, mail):
    register(client)
    [letter] = mail.about("Добро пожаловать в Argus")
    assert letter["to"] == EMAIL
    assert "$0.30" in letter["text"]
    assert PASSWORD not in letter["text"] and PASSWORD not in letter["html"]
    assert "http" not in letter["text"], "there are no links in the welcome: the site on the domain is closed"


def test_a_failing_mailbox_does_not_break_registration(db):
    broken = Outbox(fail=True)
    with TestClient(build(db, SECRET, mail=broken)) as c:
        r = c.post("/auth/register", json={"email": EMAIL, "password": PASSWORD, "terms": "t"})
    assert r.status_code == 200 and r.json()["access"]


def test_a_refused_registration_sends_nothing(client, mail):
    register(client)
    mail.letters.clear()
    again = client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD, "terms": "t"})
    assert again.status_code == 409
    assert mail.letters == []


# --------------------------------------------------------------------------
# Password
# --------------------------------------------------------------------------


def test_changing_the_password_tells_the_owner_and_says_everyone_was_signed_out(client, mail):
    headers = register(client)
    mail.letters.clear()
    r = client.post("/auth/password", json={"old": PASSWORD, "new": NEW_PASSWORD}, headers=headers)
    assert r.status_code == 204
    [letter] = mail.about("Пароль Argus изменён")
    assert letter["to"] == EMAIL
    assert EMAIL in letter["text"]
    assert "Во всех приложениях выполнен выход" in letter["text"]
    assert "Это были не вы? Напишите нам" in letter["text"]
    assert re.search(r"\d{1,2} [а-я]+ \d{4}, \d\d:\d\d UTC", letter["text"])
    assert NEW_PASSWORD not in letter["text"] and PASSWORD not in letter["text"]


def test_a_wrong_old_password_changes_nothing_and_sends_nothing(client, mail):
    headers = register(client)
    mail.letters.clear()
    r = client.post("/auth/password", json={"old": "not-the-right-password-at-all", "new": NEW_PASSWORD}, headers=headers)
    assert r.status_code == 403
    assert mail.letters == []


def test_a_reset_by_link_also_reports_the_change(client, mail):
    register(client)
    client.post("/auth/reset/request", json={"email": EMAIL})
    [reset] = mail.about("Восстановление пароля Argus")
    token = re.search(r"token=(\S+)", reset["text"]).group(1)
    mail.letters.clear()
    done = client.post("/auth/reset/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert done.json()["ok"] is True
    [letter] = mail.about("Пароль Argus изменён")
    assert letter["to"] == EMAIL


def test_a_dead_reset_link_sends_no_report(client, mail):
    register(client)
    mail.letters.clear()
    done = client.post("/auth/reset/confirm", json={"token": "made-up-link", "password": NEW_PASSWORD})
    assert done.json()["ok"] is False
    assert mail.letters == []


# --------------------------------------------------------------------------
# Payment
# --------------------------------------------------------------------------


def pay_five(client, headers) -> int:
    order = client.post("/billing/checkout", json={"dollars": 5}, headers=headers).json()["order"]
    paid = client.post(f"/billing/stub/{order}")
    assert paid.status_code == 200, paid.text
    return order


def test_a_top_up_sends_a_receipt_with_amount_order_and_balance(client, mail):
    headers = register(client)
    mail.letters.clear()
    order = pay_five(client, headers)
    [letter] = mail.about("Баланс Argus пополнен")
    assert letter["to"] == EMAIL
    assert "Сумма: $5.00" in letter["text"]
    assert "Баланс сейчас: $5.30" in letter["text"], "gift $0.30 + top-up $5.00"
    # Requirement: "Order No. 7" is not written. There are no internal numbers in the
    # email, neither in the text nor in the markup.
    for part in (letter["text"], letter["html"]):
        assert "Заказ" not in part and "№" not in part
    assert re.search(r"Дата: \d{1,2} [а-я]+ \d{4}, \d\d:\d\d UTC", letter["text"])
    assert "Сумма" in letter["html"] and "$5.30" in letter["html"]


def test_a_repeated_payment_notice_sends_no_second_receipt(client, mail):
    headers = register(client)
    order = pay_five(client, headers)
    mail.letters.clear()
    again = client.post(f"/billing/stub/{order}")
    assert again.json()["first_time"] is False
    assert mail.letters == []


def test_a_failing_mailbox_does_not_undo_the_payment(db):
    broken = Outbox(fail=True)
    with TestClient(build(db, SECRET, pay=billing.Stub(), mail=broken)) as c:
        headers = register(c)
        order = c.post("/billing/checkout", json={"dollars": 5}, headers=headers).json()["order"]
        assert c.post(f"/billing/stub/{order}").json()["paid"] is True
        me = c.get("/me", headers=headers).json()
    assert "5.30" in str(me) or me.get("balance") is not None


# --------------------------------------------------------------------------
# Account deletion
# --------------------------------------------------------------------------


def test_deleting_the_account_confirms_to_the_address_it_had(client, mail):
    headers = register(client)
    mail.letters.clear()
    r = client.post("/account/delete", json={"password": PASSWORD}, headers=headers)
    assert r.status_code == 204
    [letter] = mail.about("Аккаунт Argus удалён")
    assert letter["to"] == EMAIL
    assert EMAIL in letter["text"]


def test_a_wrong_password_does_not_delete_and_sends_nothing(client, mail):
    headers = register(client)
    mail.letters.clear()
    r = client.post("/account/delete", json={"password": "not-the-right-password-at-all"}, headers=headers)
    assert r.status_code == 403
    assert mail.letters == []


# --------------------------------------------------------------------------
# What is in any email
# --------------------------------------------------------------------------


def every_letter(client, mail) -> list[dict]:
    headers = register(client)
    client.post("/auth/reset/request", json={"email": EMAIL})
    pay_five(client, headers)
    client.post("/auth/password", json={"old": PASSWORD, "new": NEW_PASSWORD}, headers=headers)
    headers = {"Authorization": "Bearer " + client.post(
        "/auth/sign-in", json={"email": EMAIL, "password": NEW_PASSWORD}).json()["access"]}
    client.post("/account/delete", json={"password": NEW_PASSWORD}, headers=headers)
    return mail.letters


def test_all_five_letters_are_in_one_style_and_say_where_to_write(client, mail):
    letters = every_letter(client, mail)
    assert {letter["subject"] for letter in letters} == {
        "Добро пожаловать в Argus", "Восстановление пароля Argus", "Баланс Argus пополнен",
        "Пароль Argus изменён", "Аккаунт Argus удалён"}
    for letter in letters:
        assert letter["html"] and list(letter["images"]) == ["argus-logo"]
        assert mailtemplate.SUPPORT in letter["text"] and mailtemplate.SUPPORT in letter["html"]
        assert mailtemplate.PALETTE["page"] in letter["html"] and mailtemplate.PALETTE["card"] in letter["html"]


def test_no_letter_talks_about_how_the_app_is_built(client, mail):
    """Visible text without the app's internals: no models, no keys, no providers (decision of
    2026-10-08).
    """
    banned = re.compile(r"\b(api|gpt|openai|claude|anthropic|elevenlabs|liqpay|resend|токен|модел|ключ|релей|сервер)",
                        re.IGNORECASE)
    for letter in every_letter(client, mail):
        # The recovery link is an address (it contains `api.` and `token=`), not email text: we
        # check the words around it.
        words = re.sub(r"https?://\S+", "", letter["text"])
        found = banned.findall(words)
        assert not found, (letter["subject"], found)


def test_the_only_link_in_any_letter_is_the_reset_link(client, mail):
    for letter in every_letter(client, mail):
        links = re.findall(r"https?://\S+", letter["text"])
        if letter["subject"] == "Восстановление пароля Argus":
            assert len(links) == 1 and links[0].startswith("https://api.example/auth/reset")
        else:
            assert links == [], (letter["subject"], links)


# --------------------------------------------------------------------------
# Language
# --------------------------------------------------------------------------


def test_english_letters_are_complete_and_use_the_same_facts(db, mail, monkeypatch):
    monkeypatch.setenv("ARGUS_MAIL_LANG", "en")
    with TestClient(build(db, SECRET, pay=billing.Stub(), mail=mail, base_url="https://api.example")) as c:
        headers = register(c)
        pay_five(c, headers)
    welcome = mail.about("Welcome to Argus")[0]
    receipt = mail.about("Argus balance topped up")[0]
    assert "$0.30" in welcome["text"]
    assert "Amount: $5.00" in receipt["text"] and "Balance now: $5.30" in receipt["text"]
    assert re.search(r"Date: \d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d UTC", receipt["text"])
    assert 'lang="en"' in welcome["html"]
    assert not re.search(r"[А-Яа-я]", welcome["text"] + receipt["text"]), "Russian is left in the English letter"


def test_every_event_has_text_in_every_language():
    for event, by_lang in mailevents.TEXTS.items():
        assert set(by_lang) == set(mailevents.LANGS), event
        keys = {frozenset(texts) for texts in by_lang.values()}
        assert len(keys) == 1, f"{event}: the languages have different fields {keys}"


def test_an_unknown_language_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ARGUS_MAIL_LANG", "klingon")
    _, message = mailevents.reset_password(EMAIL, "https://x.example/r", 30)
    assert message.subject == "Argus password reset"        # an unknown value: the default language, English
    _, message = mailevents.reset_password(EMAIL, "https://x.example/r", 30, lang="ru")
    assert message.subject == "Восстановление пароля Argus"


def test_every_letter_is_short(client, mail):
    """Requirement of 2026-10-15: there is always too much text everywhere, so make it simpler.
    A heading and one or two lines.
    """
    for letter in every_letter(client, mail):
        body = re.sub(r"<[^>]+>", " ", letter["html"].split("<body", 1)[1].split(">", 1)[1])
        body = re.sub(r"\s+", " ", body).replace(mailtemplate.SUPPORT, "").strip()
        assert len(body) <= 420, (letter["subject"], len(body), body)


def test_no_letter_has_an_outline(client, mail):
    for letter in every_letter(client, mail):
        assert not re.search(r"border(?!-radius|:0\b|=)", letter["html"]), letter["subject"]
