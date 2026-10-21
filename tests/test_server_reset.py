"""Password recovery: request, email, link, new password.

What gets tested is not "the route responds" but what can break unnoticed here: the
response to a request must not differ depending on whether the account exists; a link
must not work twice, after its expiry, or for someone else's account; changing the
password must close the old logins, like the ordinary change in settings; the page from
the email must not let what arrived in the address into its JS without escaping.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, mailer
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"
NEW_PASSWORD = "another-fairly-long-password"


RESET_SUBJECT = "Argus password reset"


class FakeMailer:
    """Catches letters instead of sending them, so the path is tested without real mail."""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        # `sent` holds the recovery emails: these tests are about them. Other server emails (the
        # welcome email at registration and the like) pile up in `other` so that they do not
        # confuse the count.
        self.sent: list[tuple[str, str, str]] = []
        self.other: list[tuple[str, str, str]] = []
        self.html: list[str | None] = []
        self.images: list[dict[str, bytes] | None] = []
        self.fail = fail

    def send(self, to: str, subject: str, text: str, html: str | None = None,
             images: dict[str, bytes] | None = None) -> None:
        if self.fail:
            raise mailer.MailError("mail unavailable")
        (self.sent if subject == RESET_SUBJECT else self.other).append((to, subject, text))
        self.html.append(html)
        self.images.append(images)


@pytest.fixture(autouse=True)
def english_mail(monkeypatch):
    """Emails are written in the server's default language; pin it so the subject and texts below are English."""
    monkeypatch.setenv("ARGUS_MAIL_LANG", "en")


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def mail():
    return FakeMailer()


@pytest.fixture
def http(db, mail):
    app = build(db, SECRET, mail=mail, base_url="https://argus.example")
    with TestClient(app) as client:
        yield client


def register(client, email: str = "user@example.com", password: str = PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password, "terms": "t"})


def link_from(mail: FakeMailer) -> str:
    """The link from the text part of the last letter: a separate line that starts with `http`."""
    lines = [line.strip() for line in mail.sent[-1][2].splitlines() if line.strip().startswith("http")]
    assert len(lines) == 1, "the email must have exactly one link line"
    return lines[0]


def token_from(mail: FakeMailer) -> str:
    link = link_from(mail)
    assert "token=" in link
    return link.split("token=", 1)[1]


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


def test_a_real_account_gets_a_letter_with_a_working_link(http, mail):
    register(http)
    r = http.post("/auth/reset/request", json={"email": "user@example.com"})
    assert r.status_code == 204
    assert len(mail.sent) == 1
    to, _subject, text = mail.sent[0]
    assert to == "user@example.com"
    assert "link" in text.lower() or "token=" in text
    # The token is in the part after "#": it does not go to the server and does not end up in
    # its request log.
    assert link_from(mail).startswith("https://argus.example/auth/reset#token=")


def test_an_unknown_email_gets_the_same_answer_and_no_letter(http, mail):
    """The answer must not differ, otherwise the mail would be used to enumerate registered
    addresses.
    """
    r = http.post("/auth/reset/request", json={"email": "no-such@example.com"})
    assert r.status_code == 204
    assert mail.sent == []


def test_a_second_request_too_soon_sends_nothing(http, mail):
    """The recovery form must not become a "flood someone else's mailbox" button."""
    register(http)
    http.post("/auth/reset/request", json={"email": "user@example.com"})
    r = http.post("/auth/reset/request", json={"email": "user@example.com"})
    assert r.status_code == 204            # the response is exactly the same; the secret is not in the response
    assert len(mail.sent) == 1             # and the email went out only once


def test_a_request_after_the_cooldown_sends_again(db, mail):
    register(TestClient(build(db, SECRET, mail=mail)))
    now = time.time()
    accounts.start_reset(db, "user@example.com", now=now)
    accounts.start_reset(db, "user@example.com",
                         now=now + accounts.RESET_COOLDOWN_S + 1)
    assert (
        db.execute("SELECT COUNT(*) c FROM password_resets").fetchone()["c"] == 2
    )


def test_a_failed_letter_does_not_crash_the_request(http, mail):
    """The request is already recorded by the time of sending: a failed letter must not turn an
    honest 204 into a 500."""
    register(http)
    mail.fail = True
    r = http.post("/auth/reset/request", json={"email": "user@example.com"})
    assert r.status_code == 204


# --------------------------------------------------------------------------
# The link and the password change
# --------------------------------------------------------------------------


def test_the_link_actually_sets_a_new_password(http, mail):
    register(http)
    http.post("/auth/reset/request", json={"email": "user@example.com"})
    token = token_from(mail)

    r = http.post("/auth/reset/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert r.json() == {"ok": True}

    denied = http.post("/auth/sign-in", json={"email": "user@example.com", "password": PASSWORD})
    assert denied.status_code == 403
    allowed = http.post("/auth/sign-in", json={"email": "user@example.com", "password": NEW_PASSWORD})
    assert allowed.status_code == 200


def test_the_link_works_only_once(http, mail):
    register(http)
    http.post("/auth/reset/request", json={"email": "user@example.com"})
    token = token_from(mail)

    first = http.post("/auth/reset/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert first.json()["ok"] is True
    second = http.post("/auth/reset/confirm",
                       json={"token": token, "password": "one-more-password"})
    assert second.json()["ok"] is False


def test_an_expired_link_is_refused(db, mail):
    client = TestClient(build(db, SECRET, mail=mail))
    register(client)
    now = time.time()
    raw = accounts.start_reset(db, "user@example.com", now=now)
    with pytest.raises(accounts.Denied, match="expired"):
        accounts.finish_reset(db, raw, NEW_PASSWORD,
                              now=now + accounts.RESET_TTL_S + 1)


def test_an_unknown_token_is_refused(http):
    r = http.post("/auth/reset/confirm",
                  json={"token": "never-issued-this", "password": NEW_PASSWORD})
    assert r.json() == {"ok": False, "error": "link is invalid or expired — request a new one", "code": "link"}


def test_a_short_new_password_is_refused(http, mail):
    register(http)
    http.post("/auth/reset/request", json={"email": "user@example.com"})
    token = token_from(mail)
    r = http.post("/auth/reset/confirm", json={"token": token, "password": "short"})
    assert r.json()["ok"] is False
    # The token is not burned by a failed attempt: a short password can be fixed and the same
    # link submitted again.
    ok = http.post("/auth/reset/confirm", json={"token": token, "password": NEW_PASSWORD})
    assert ok.json()["ok"] is True


def test_resetting_the_password_closes_other_sessions(http, mail):
    """Same reason as with changing the password from settings: if it was changed, most likely it
    is because the old one may have leaked."""
    body = register(http).json()
    access = body["access"]
    assert http.get("/me", headers={"Authorization": f"Bearer {access}"}).status_code == 200

    http.post("/auth/reset/request", json={"email": "user@example.com"})
    http.post("/auth/reset/confirm", json={"token": token_from(mail), "password": NEW_PASSWORD})

    denied = http.post("/auth/refresh", json={"refresh": body["refresh"]})
    assert denied.status_code == 401


# --------------------------------------------------------------------------
# The page from the email
# --------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", [
    'x"};fetch(1);//',
    "</script><img src=x onerror=alert(document.domain)>",
    "<!--<script>",
])
def test_the_page_never_puts_the_address_into_its_own_code(http, hostile):
    """The token comes from the address, which means from a link that anyone could have crafted.
    `json.dumps` does not touch `</script>`: such a link closed the script early and ran
    foreign markup on our domain. Now the server does not put the token into the page at all:
    the page is the same for any address, and its own script reads the token."""
    r = http.get("/auth/reset", params={"token": hostile})
    assert r.status_code == 200
    plain = http.get("/auth/reset")
    assert r.text == plain.text
    assert "onerror" not in r.text and hostile not in r.text
    assert r.text.count("</script>") == 1


def test_the_page_renders_for_a_real_token(http, mail):
    register(http)
    http.post("/auth/reset/request", json={"email": "user@example.com"})
    token = token_from(mail)
    r = http.get("/auth/reset", params={"token": token})
    assert r.status_code == 200
    assert token not in r.text
    assert "New password" in r.text
    # The page script reads the token from "#", and emails sent earlier carry it in "?".
    assert "location.hash" in r.text and "location.search" in r.text


def test_the_page_runs_only_its_own_script(http):
    """Even if an injection ever turned up in the page, the browser would not run a foreign
    script.
    """
    import base64
    import hashlib
    import re

    r = http.get("/auth/reset")
    policy = r.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>", r.text, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in policy
    assert "default-src 'none'" in policy and "'unsafe-inline'" not in policy.split(
        "style-src")[0]
    assert "connect-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_an_old_letter_with_the_token_in_the_query_still_has_a_page(http):
    """Letters sent before the move to "#" stay valid for another half hour: the page reads them."""
    assert http.get("/auth/reset", params={"token": "something"}).status_code == 200


# --------------------------------------------------------------------------
# The email goes to the address the account was created with, and does not get in the
# server's way
# --------------------------------------------------------------------------


def test_the_letter_goes_to_the_address_of_the_account_not_to_what_was_typed(http, mail):
    register(http, email="User@Example.com")
    http.post("/auth/reset/request", json={"email": "  USER@example.COM "})
    # The local part stays as it was registered; the domain follows the domain rules, in
    # lowercase.
    assert [to for to, _, _ in mail.sent] == ["User@example.com"]


def test_an_address_with_a_foreign_domain_gets_it_in_ascii(http, mail):
    register(http, email="user@bücher.de")
    http.post("/auth/reset/request", json={"email": "user@bücher.de"})
    assert [to for to, _, _ in mail.sent] == ["user@xn--bcher-kva.de"]


def test_an_old_account_with_a_strange_address_gets_no_letter(http, db, mail):
    """An address created under lax rules could pull someone else's mailbox out of the header."""
    register(http)
    db.execute("UPDATE accounts SET email = 'a<victim@gmail.com', "
               "email_key = 'a<victim@gmail.com'")
    db.commit()
    assert http.post("/auth/reset/request",
                     json={"email": "a<victim@gmail.com"}).status_code == 204
    assert mail.sent == []


def test_the_letter_is_sent_off_the_event_loop_and_after_the_answer(db, mail):
    """A hung SMTP does not hold up the server, and the response time does not reveal whether an
    account exists.
    """
    import threading

    seen = {}

    class Slow(FakeMailer):
        def send(self, to, subject, text, html=None, images=None):
            seen["thread"] = threading.current_thread()
            super().send(to, subject, text, html, images)

    slow = Slow()
    with TestClient(build(db, SECRET, mail=slow)) as client:
        register(client)
        client.post("/auth/reset/request", json={"email": "user@example.com"})
    assert len(slow.sent) == 1
    assert seen["thread"] is not threading.main_thread()


def test_reset_requests_from_one_address_are_limited(http, mail):
    """Accounts are free, but letters cost us our daily mail limit and other people's mailboxes."""
    for i in range(10):
        register(http, email=f"user{i}@example.com")
        assert http.post("/auth/reset/request",
                         json={"email": f"user{i}@example.com"}).status_code == 204
    again = http.post("/auth/reset/request", json={"email": "user0@example.com"})
    assert again.status_code == 429
    assert "wait" in again.json()["detail"]


# --------------------------------------------------------------------------
# Old links and races
# --------------------------------------------------------------------------


def test_asking_twice_leaves_one_working_link_after_the_newer_is_used(db):
    """Asked twice, two letters arrived; after the password is changed through one of them, the
    other must not open the door again, otherwise "the password may have leaked" is not
    solved."""
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    now = time.time()
    older = accounts.start_reset(db, "user@example.com", now=now)
    newer = accounts.start_reset(db, "user@example.com", now=now + accounts.RESET_COOLDOWN_S + 1)
    accounts.finish_reset(db, newer, NEW_PASSWORD, now=now + 100)
    with pytest.raises(accounts.Denied, match="invalid"):
        accounts.finish_reset(db, older, "yet-another-password", now=now + 101)


def test_changing_the_password_in_settings_kills_the_links_already_sent(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    raw = accounts.start_reset(db, "user@example.com")
    accounts.change_password(db, login.account_id, PASSWORD, NEW_PASSWORD)
    with pytest.raises(accounts.Denied, match="invalid"):
        accounts.finish_reset(db, raw, "yet-another-password")


async def test_two_requests_with_one_link_set_the_password_once(db):
    """The hash is computed off the event loop: both requests manage to pass the "link is still
    valid" check.
    """
    import asyncio

    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    raw = accounts.start_reset(db, "user@example.com")
    results = await asyncio.gather(
        accounts.finish_reset_async(db, raw, "first-new-password"),
        accounts.finish_reset_async(db, raw, "second-new-password"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, accounts.Denied) for r in results) == 1
    assert sum(isinstance(r, str) for r in results) == 1
    winner = "first-new-password" if isinstance(results[0], str) else "second-new-password"
    accounts.sign_in(db, SECRET, "user@example.com", winner)


def test_a_reset_lifts_the_sign_in_lock_of_the_owner(http, mail):
    """The link from the letter proves the mailbox is theirs: the lock caused by someone else's
    attempts does not stand in the owner's way.
    """
    register(http)
    for _ in range(5):
        http.post("/auth/sign-in", json={"email": "user@example.com", "password": "not-the-password-1"})
    locked = http.post("/auth/sign-in", json={"email": "user@example.com", "password": PASSWORD})
    assert locked.status_code == 429

    http.post("/auth/reset/request", json={"email": "user@example.com"})
    http.post("/auth/reset/confirm", json={"token": token_from(mail), "password": NEW_PASSWORD})
    allowed = http.post("/auth/sign-in", json={"email": "user@example.com",
                                               "password": NEW_PASSWORD})
    assert allowed.status_code == 200
