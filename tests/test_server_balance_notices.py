"""The emails "balance is running low" and "balance has run out".

The list of emails was extended: before this, a person learned about an empty balance only
when recognition had already stopped. The email goes by the state of the database
(`mailevents.balances_due`), not by charge events: spending goes through several paths,
and a server restart must lose nothing. What is checked is what is easy to break here: an
email must not go out twice, to a newcomer with a gift it is too early, to a blocked
account never, and a mail failure must not lose the email.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, billing, entitlements, mailer, mailevents
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"


class Outbox:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.letters: list[tuple[str, str, str]] = []
        self.fail = fail

    def send(self, to, subject, text, html=None, images=None):
        if self.fail:
            raise mailer.MailError("mail unavailable")
        self.letters.append((to, subject, text))


@pytest.fixture(autouse=True)
def russian(monkeypatch):
    # The subjects below are the Russian texts (language data, `mail_texts/ru.json`): pin the default language.
    monkeypatch.setenv("ARGUS_MAIL_LANG", "ru")


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


def make(db, email="user@example.com", *, topup=False, used=True, balance=None) -> int:
    """An account with a gift; optionally it topped up, spent something, and the balance is set as
    needed.
    """
    accounts.register(db, SECRET, email, PASSWORD, terms="t")
    account_id = int(db.execute("SELECT id FROM accounts WHERE email_key = ?", (email,)).fetchone()["id"])
    if topup:
        entitlements.credit(db, account_id, 5_000_000, "topup", note="test")
    if used:
        entitlements.charge(db, account_id, "stt_seconds", 1, 10)
    if balance is not None:
        db.execute("UPDATE accounts SET balance_micro = ? WHERE id = ?", (balance, account_id))
        db.commit()
    return account_id


def sweep(db, box: Outbox, **kw) -> int:
    return mailevents.sweep_balances(db, lambda to, msg: box.send(to, msg.subject, msg.text), **kw)


def test_a_new_account_with_the_gift_untouched_gets_nothing(db):
    make(db, used=False)               # gift $0.30, nothing spent
    box = Outbox()
    assert sweep(db, box) == 0 and box.letters == []


def test_a_gift_only_account_gets_just_the_empty_letter_and_only_at_zero(db):
    account_id = make(db, balance=100_000)          # $0.10 left, no top-ups
    box = Outbox()
    assert sweep(db, box) == 0, "\"running low\" for a newcomer with a gift is noise"
    db.execute("UPDATE accounts SET balance_micro = 0 WHERE id = ?", (account_id,))
    db.commit()
    assert sweep(db, box) == 1
    [(to, subject, text)] = box.letters
    assert to == "user@example.com" and subject == "Баланс Argus закончился"
    assert "пополнить" in text.lower()


def test_someone_who_topped_up_gets_low_then_empty(db):
    account_id = make(db, topup=True, balance=800_000)       # $0.80, below a dollar
    box = Outbox()
    assert sweep(db, box) == 1
    assert box.letters[0][1] == "Баланс Argus заканчивается" and "$0.80" in box.letters[0][2]
    db.execute("UPDATE accounts SET balance_micro = 0 WHERE id = ?", (account_id,))
    db.commit()
    assert sweep(db, box) == 1
    assert box.letters[1][1] == "Баланс Argus закончился"


def test_each_level_goes_out_once(db):
    make(db, topup=True, balance=800_000)
    box = Outbox()
    assert sweep(db, box) == 1
    assert sweep(db, box) == 0 and sweep(db, box) == 0
    assert len(box.letters) == 1


def test_after_a_top_up_the_next_fall_writes_again(db):
    account_id = make(db, topup=True, balance=0)
    box = Outbox()
    assert sweep(db, box) == 1                                   # ran out
    entitlements.credit(db, account_id, 5_000_000, "topup", note="more")   # topped up
    assert sweep(db, box) == 0
    assert db.execute("SELECT low_notified FROM accounts WHERE id = ?", (account_id,)).fetchone()[0] == 0
    db.execute("UPDATE accounts SET balance_micro = 0 WHERE id = ?", (account_id,))
    db.commit()
    assert sweep(db, box) == 1 and len(box.letters) == 2


def test_a_failed_send_is_retried_not_lost(db):
    account_id = make(db, balance=0)
    broken = Outbox(fail=True)
    assert sweep(db, broken) == 0
    assert db.execute("SELECT low_notified FROM accounts WHERE id = ?", (account_id,)).fetchone()[0] == 0
    working = Outbox()
    assert sweep(db, working) == 1, "mail is fixed — the email must go out"


def test_a_blocked_account_is_not_written_to(db):
    account_id = make(db, balance=0)
    db.execute("UPDATE accounts SET blocked_at = ? WHERE id = ?", (time.time(), account_id))
    db.commit()
    box = Outbox()
    assert sweep(db, box) == 0 and box.letters == []


def test_the_letter_follows_the_language_setting(db, monkeypatch):
    make(db, balance=0)
    monkeypatch.setenv("ARGUS_MAIL_LANG", "en")
    box = Outbox()
    sweep(db, box)
    assert box.letters[0][1] == "Your Argus balance is empty"


def test_the_server_sends_them_by_itself(db, monkeypatch):
    """A background sweep inside the server itself: the email goes out without anyone's request."""
    make(db, balance=0)
    monkeypatch.setenv("ARGUS_BALANCE_SWEEP_S", "0.05")
    box = Outbox()
    with TestClient(build(db, SECRET, pay=billing.Stub(), mail=box)):
        deadline = time.time() + 5
        while not box.letters and time.time() < deadline:
            time.sleep(0.05)
    assert [subject for _, subject, _ in box.letters] == ["Баланс Argus закончился"]


def test_without_real_mail_the_server_does_not_pretend_to_send(db, monkeypatch):
    account_id = make(db, balance=0)
    monkeypatch.setenv("ARGUS_BALANCE_SWEEP_S", "0.05")
    with TestClient(build(db, SECRET)):               # mail is `Off` by default
        time.sleep(0.4)
    assert db.execute("SELECT low_notified FROM accounts WHERE id = ?", (account_id,)).fetchone()[0] == 0


def test_the_background_sweep_can_be_switched_off(db, monkeypatch):
    make(db, balance=0)
    monkeypatch.setenv("ARGUS_BALANCE_SWEEP_S", "0")
    box = Outbox()
    with TestClient(build(db, SECRET, mail=box)):
        time.sleep(0.3)
    assert box.letters == []
