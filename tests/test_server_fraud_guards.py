"""Gaming and abuse (audit 2026-10-16): gifts, emails, orders, sign-ins, addresses.

Nobody confirms the email at registration, so the protection is ceilings: for the whole
server, per address, per mailbox. Each test holds one ceiling and has been checked by
"mutation" (remove the protection and the test fails).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, emails, entitlements, limits, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"
TERMS = "2026-10-06"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def http(db):
    with TestClient(build(db, SECRET)) as client:
        yield client


def register(client, email):
    return client.post("/auth/register", json={"email": email, "password": PASSWORD, "terms": TERMS})


# --------------------------------------------------------------------------
# The gift at registration
# --------------------------------------------------------------------------

def test_the_daily_gift_budget_runs_out_and_accounts_still_open(db, monkeypatch):
    monkeypatch.setenv("ARGUS_GIFT_DAILY_CAP", "2")
    for n in range(3):
        accounts.register(db, SECRET, f"user{n}@example.com", PASSWORD)
    balances = [entitlements.balance(db, i) for i in (1, 2, 3)]
    assert balances == [pricing.WELCOME_MICRO, pricing.WELCOME_MICRO, 0]
    # With no gift there is no row about it in the journal either: the balance still equals the
    # journal.
    assert db.execute("SELECT COUNT(*) FROM credits WHERE kind = 'gift'").fetchone()[0] == 2


def test_deleting_an_account_does_not_give_the_gift_budget_back(db, monkeypatch):
    """The loop "sign up, spend, delete, sign up again" must not be able to run without a limit."""
    monkeypatch.setenv("ARGUS_GIFT_DAILY_CAP", "1")
    accounts.register(db, SECRET, "first@example.com", PASSWORD)
    accounts.erase_account(db, 1)
    accounts.register(db, SECRET, "first@example.com", PASSWORD)          # the same email, again
    assert entitlements.balance(db, 2) == 0


def test_zero_cap_means_no_gifts_at_all(db, monkeypatch):
    monkeypatch.setenv("ARGUS_GIFT_DAILY_CAP", "0")
    accounts.register(db, SECRET, "someone@example.com", PASSWORD)
    assert entitlements.balance(db, 1) == 0


def test_a_new_day_opens_a_new_budget(db, monkeypatch):
    monkeypatch.setenv("ARGUS_GIFT_DAILY_CAP", "1")
    day = 86400.0 * 20000
    accounts.register(db, SECRET, "a@example.com", PASSWORD, now=day)
    accounts.register(db, SECRET, "b@example.com", PASSWORD, now=day + 60)
    accounts.register(db, SECRET, "c@example.com", PASSWORD, now=day + 86400)
    assert [entitlements.balance(db, i) for i in (1, 2, 3)] == [pricing.WELCOME_MICRO, 0, pricing.WELCOME_MICRO]


def test_one_address_gets_three_gifts_a_day_and_the_rest_open_empty(http, db):
    for n in range(5):
        assert register(http, f"farm{n}@example.com").status_code == 200
    balances = [entitlements.balance(db, i) for i in range(1, 6)]
    assert balances == [pricing.WELCOME_MICRO] * 3 + [0, 0]


# --------------------------------------------------------------------------
# Email and addresses
# --------------------------------------------------------------------------

def test_a_kelvin_sign_cannot_take_over_a_real_address():
    """The Kelvin sign (U+212A) becomes a plain "k" after lowercasing: it would take the key of
    someone else's `kevin@…`, while the letter would go to "Kevin".
    """
    with pytest.raises(emails.BadEmail):
        emails.check("Kevin@gmail.com")
    with pytest.raises(emails.BadEmail):
        emails.normalize("kévin@example.com")
    assert emails.check("Kevin@Gmail.com") == "kevin@gmail.com"      # an ordinary letter case is still one mailbox


def test_ipv6_addresses_are_counted_by_their_network_not_one_by_one():
    a = limits._bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    b = limits._bucket("2001:db8:1:2:1111:2222:3333:4444")
    other = limits._bucket("2001:db8:1:3::1")
    assert a == b == "2001:db8:1:2::/64" and other != a
    assert limits._bucket("203.0.113.7") == "203.0.113.7"
    assert limits._bucket("::ffff:203.0.113.7") == "203.0.113.7"
    assert limits._bucket(None) is None
    assert limits._bucket("not-an-address") == "not-an-address"


def test_the_counter_key_of_a_request_uses_the_network_for_ipv6():
    from types import SimpleNamespace

    def request(host):
        return SimpleNamespace(headers={}, client=SimpleNamespace(host=host))

    assert limits.client_ip(request("2001:db8:7:7:1::1")) == limits.client_ip(request("2001:db8:7:7:9::9"))
    assert limits.client_ip(request("203.0.113.9")) == "203.0.113.9"


# --------------------------------------------------------------------------
# Sign-ins, password change, orders
# --------------------------------------------------------------------------

def test_signing_in_too_often_is_refused_even_with_the_right_password(http):
    """Every sign-in costs a scrypt hash: a stream of successful sign-ins would tie up the pool
    for everyone.
    """
    assert register(http, "busy@example.com").status_code == 200
    codes = [http.post("/auth/sign-in", json={"email": "busy@example.com", "password": PASSWORD}).status_code
             for _ in range(62)]
    assert codes[:59].count(200) == 59 and 429 in codes[59:]


def test_an_account_keeps_a_bounded_number_of_logins(db):
    accounts.register(db, SECRET, "many@example.com", PASSWORD)
    for _ in range(accounts.MAX_SESSIONS_PER_ACCOUNT + 8):
        accounts.sign_in(db, SECRET, "many@example.com", PASSWORD)
    left = db.execute("SELECT COUNT(*) FROM sessions WHERE account_id = 1").fetchone()[0]
    assert left == accounts.MAX_SESSIONS_PER_ACCOUNT


def test_expired_logins_of_the_account_are_swept_on_the_next_sign_in(db):
    accounts.register(db, SECRET, "old@example.com", PASSWORD, now=1000.0)
    accounts.sign_in(db, SECRET, "old@example.com", PASSWORD, now=1000.0 + 10 * 365 * 86400)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_a_stream_of_top_up_orders_is_cut_off(http):
    access = register(http, "orders@example.com").json()["access"]
    codes = [http.post("/billing/checkout", json={"dollars": 5}, headers={"Authorization": f"Bearer {access}"}).status_code
             for _ in range(8)]
    assert 429 in codes
    assert codes.count(429) >= 1 and codes[0] != 429


def test_passwords_cannot_be_changed_in_a_loop(http):
    access = register(http, "loop@example.com").json()["access"]
    head = {"Authorization": f"Bearer {access}"}
    current = PASSWORD
    codes = []
    for n in range(12):
        new = f"new-password-number-{n}-long"
        r = http.post("/auth/password", json={"old": current, "new": new}, headers=head)
        codes.append(r.status_code)
        if r.status_code == 204:
            current = new
    assert codes[:10].count(204) == 10 and 429 in codes[10:]


# --------------------------------------------------------------------------
# Prolonged failures of the model call
# --------------------------------------------------------------------------


def test_slow_failures_of_the_model_call_close_new_questions_for_a_while(db, monkeypatch):
    """A call that broke off after a long wait costs the person nothing, but the provider may
    already have produced part of the answer: two such failures in a row, and new questions are
    closed. Fast refusals (the provider said "no" right away) do not count."""
    import asyncio

    from argus.server import relay

    monkeypatch.setattr(relay, "SLOW_FAILURE_S", 0.05)
    delay = {"s": 0.2}

    async def sink(req):
        await asyncio.sleep(delay["s"])
        raise RuntimeError("the provider did not wait")

    monkeypatch.setattr(relay, "_ask_openai", sink)
    with TestClient(build(db, SECRET)) as client:
        access = register(client, "slow@example.com").json()["access"]
        entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")
        head = {"Authorization": f"Bearer {access}"}
        body = {"provider": "openai-api", "model": "gpt-5.6-luna", "prompt": "hello"}

        delay["s"] = 0.0                                                   # fast refusals are not counted
        assert [client.post("/relay/brain/ask", json=body, headers=head).status_code for _ in range(3)] == [502] * 3
        delay["s"] = 0.2                                                   # but slow ones are
        assert client.post("/relay/brain/ask", json=body, headers=head).status_code == 502
        assert client.post("/relay/brain/ask", json=body, headers=head).status_code == 502
        closed = client.post("/relay/brain/ask", json=body, headers=head)
        assert closed.status_code == 429 and "wait" in closed.json()["detail"]
        assert entitlements.balance(db, 1) == pricing.WELCOME_MICRO + 5 * pricing.MICRO   # no money was taken
