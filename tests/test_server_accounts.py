"""Accounts: registration, sign-in, balance.

Registration is ordinary: email and password. What is checked is not "the route answers
200" but what really breaks in such places: a password stored in the database as is,
sign-in with someone else's password, revocation of old sessions that gets lost on a
password change, money charged from the wrong place.

What is deliberately missing here: protection against repeated registration. The balance
gift can be taken again with a new email; this is an accepted trade-off, and the gift is
kept small for exactly that reason.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, emails, entitlements, pricing, tokens
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"
TERMS = "2026-10-06"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def http(db):
    with TestClient(build(db, SECRET)) as client:
        yield client


def register(client, email: str = "user@example.com", password: str = PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password, "terms": TERMS})


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_registration_gives_a_gift_on_the_balance(http, db):
    body = register(http).json()
    assert body["refresh"] and body["access"]
    assert body["right"]["allowed"] is True
    assert body["right"]["balance_micro"] == pricing.WELCOME_MICRO
    # The gift is recorded in the journal as a top-up, not just taken out of thin air.
    row = db.execute("SELECT kind, micro FROM credits").fetchone()
    assert (row["kind"], row["micro"]) == ("gift", pricing.WELCOME_MICRO)


def test_the_same_mailbox_cannot_be_registered_twice(http):
    assert register(http).status_code == 200
    again = register(http)
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


def test_case_does_not_make_a_second_account(http):
    """An address that differs only in letter case is one mailbox, and there must be one account."""
    assert register(http).status_code == 200
    assert register(http, email="USER@Example.com").status_code == 409


def test_dots_are_not_stripped(db):
    """On most mail providers, addresses that differ by a dot belong to different people."""
    assert emails.normalize("u.ser@example.com") != emails.normalize("user@example.com")


def test_a_short_password_is_refused(http):
    r = register(http, password="123")
    assert r.status_code == 409
    assert "shorter" in r.json()["detail"]


def test_nonsense_address_is_refused(http):
    for bad in ("no-at-sign", "@example.com", "kto@", "kto@localhost"):
        assert register(http, email=bad).status_code == 400, bad


# --------------------------------------------------------------------------
# Password
# --------------------------------------------------------------------------


def test_the_password_is_not_stored_as_is(db):
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    stored = db.execute("SELECT password FROM accounts").fetchone()["password"]
    assert PASSWORD not in stored
    assert stored.startswith("scrypt$")


def test_two_accounts_with_one_password_have_different_hashes(db):
    """Without a salt, identical passwords look like identical strings in the database."""
    accounts.register(db, SECRET, "a@example.com", PASSWORD)
    accounts.register(db, SECRET, "b@example.com", PASSWORD)
    a, b = (r["password"] for r in db.execute("SELECT password FROM accounts"))
    assert a != b


def test_sign_in_needs_the_right_password(http):
    register(http)
    ok = http.post("/auth/sign-in", json={"email": "user@example.com", "password": PASSWORD})
    assert ok.status_code == 200

    bad = http.post("/auth/sign-in", json={"email": "user@example.com", "password": "another"})
    assert bad.status_code == 403


def test_the_refusal_names_which_half_was_wrong(http):
    """Before, both cases answered the same, "the email or password does not fit",
    deliberately against email enumeration (the sign-in form would otherwise turn
    into a check of "does such an email exist"). At an explicit request this was relaxed back:
    the messages differ, and it is clearer to the person signing in what to do next, try the
    password again or go and register (see the comment in `accounts.sign_in`)."""
    register(http)
    unknown = http.post("/auth/sign-in",
                        json={"email": "nobody@example.com", "password": PASSWORD})
    wrong = http.post("/auth/sign-in",
                      json={"email": "user@example.com", "password": "another"})
    assert unknown.json()["detail"] == "no such account"
    assert wrong.json()["detail"] == "wrong password"


def test_changing_the_password_closes_other_sessions(http, db):
    """A password is changed when the old one may have leaked. The old sign-ins must die."""
    first = register(http).json()
    body = {"email": "user@example.com", "password": PASSWORD}
    second = http.post("/auth/sign-in", json=body).json()

    r = http.post("/auth/password", json={"old": PASSWORD, "new": "a-new-long-password"},
                  headers={"Authorization": f"Bearer {second['access']}"})
    assert r.status_code == 204

    for gone in (first["refresh"], second["refresh"]):
        assert http.post("/auth/refresh", json={"refresh": gone}).status_code == 401
    assert http.post("/auth/sign-in", json=body).status_code == 403
    assert http.post("/auth/sign-in", json={
        "email": "user@example.com", "password": "a-new-long-password"}).status_code == 200


def test_the_old_password_is_required_to_change_it(http):
    out = register(http).json()
    r = http.post("/auth/password", json={"old": "not this one", "new": "a-new-long-password"},
                  headers={"Authorization": f"Bearer {out['access']}"})
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


def test_a_forged_pass_is_not_accepted(http):
    out = register(http).json()
    payload, mac = out["access"].split(".")
    r = http.get("/me", headers={"Authorization": f"Bearer {payload}.{'A' * len(mac)}"})
    assert r.status_code == 401


def test_an_expired_pass_is_not_accepted(http):
    out = register(http).json()
    old = tokens.sign_access(SECRET, 1, ttl=-1)
    assert http.get("/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert http.get("/me", headers={"Authorization": f"Bearer {out['access']}"}).status_code == 200


def test_signing_out_kills_the_refresh_token(http):
    out = register(http).json()
    assert http.post("/auth/sign-out", json={"refresh": out["refresh"]}).status_code == 204
    assert http.post("/auth/refresh", json={"refresh": out["refresh"]}).status_code == 401


def test_the_refresh_token_is_stored_hashed(db):
    """A stolen database must not give a sign-in on behalf of a person."""
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    stored = db.execute("SELECT token_hash FROM sessions").fetchone()["token_hash"]
    assert stored != login.refresh
    assert stored == tokens.hash_secret(login.refresh)


def test_a_blocked_account_cannot_get_in(http, db):
    out = register(http).json()
    db.execute("UPDATE accounts SET blocked_at = ?", (time.time(),))
    db.commit()
    assert http.post("/auth/refresh", json={"refresh": out["refresh"]}).status_code == 401
    r = http.post("/auth/sign-in", json={"email": "user@example.com", "password": PASSWORD})
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Balance, spending and top-up
# --------------------------------------------------------------------------


def test_money_is_taken_away_as_it_is_used(http, db):
    out = register(http).json()
    # 100 seconds of recognition: cost times markup, no more and no less.
    take = entitlements.charge(db, 1, "stt_seconds", 100, pricing.stt_cost(100))
    assert take == pricing.charged(pricing.stt_cost(100))

    body = http.get("/me", headers={"Authorization": f"Bearer {out['access']}"}).json()
    assert body["right"]["balance_micro"] == pricing.WELCOME_MICRO - take
    assert body["spent"]["micro"] == take
    assert body["spent"]["by_kind"]["stt_seconds"]["units"] == 100


def test_when_the_money_ends_the_answer_says_what_to_do(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.charge(db, login.account_id, "stt_seconds", 1, pricing.WELCOME_MICRO)
    r = entitlements.right(db, login.account_id)
    assert r.allowed is False
    assert "top up" in r.reason


def test_a_top_up_brings_the_access_back(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.charge(db, login.account_id, "stt_seconds", 1, pricing.WELCOME_MICRO)
    assert entitlements.right(db, login.account_id).allowed is False

    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    r = entitlements.right(db, login.account_id)
    assert r.allowed is True
    assert r.balance_micro == 5 * pricing.MICRO


def test_the_balance_never_goes_below_zero(db):
    """One expensive answer against a balance of a single cent must not push the account below
    zero.
    """
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.charge(db, login.account_id, "brain_tokens", 1,
                        pricing.WELCOME_MICRO * 10)
    assert entitlements.balance(db, login.account_id) == 0


def test_the_overdraft_is_still_recorded_as_usage(db):
    """We paid anyway, so the spend is recorded in full, even beyond the balance."""
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    cost = pricing.WELCOME_MICRO * 10
    entitlements.charge(db, login.account_id, "brain_tokens", 1, cost)
    row = db.execute("SELECT cost_micro, charged_micro FROM usage").fetchone()
    assert row["cost_micro"] == cost
    assert row["charged_micro"] == pricing.charged(cost)


def test_the_charge_is_the_cost_times_the_markup(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    take = entitlements.charge(db, login.account_id, "stt_seconds", 3600,
                               pricing.stt_cost(3600))
    assert take == pytest.approx(pricing.stt_cost(3600) * pricing.MARKUP, abs=1)


def test_a_request_that_may_cost_more_than_the_balance_is_not_afforded(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    have = entitlements.balance(db, login.account_id)

    assert entitlements.afford(db, login.account_id, have).allowed is True
    short = entitlements.afford(db, login.account_id, have + 1)
    assert short.allowed is False
    assert "top up" in short.reason


def test_the_balance_is_marked_low_before_it_is_empty(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    assert entitlements.right(db, login.account_id).as_json()["low"] is True   # $0.30 < $0.50
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    assert entitlements.right(db, login.account_id).as_json()["low"] is False


def test_concurrent_charges_do_not_lose_money(db):
    """Two charges that read the same balance would overwrite each other: the person would spend
    twice as much, and half of it would be charged."""
    import threading

    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.credit(db, login.account_id, 100 * pricing.MICRO, "topup")
    start = entitlements.balance(db, login.account_id)

    rounds = 20
    barrier = threading.Barrier(rounds)

    def spend_one():
        barrier.wait()
        entitlements.charge(db, login.account_id, "stt_seconds", 60, pricing.stt_cost(60))

    threads = [threading.Thread(target=spend_one) for _ in range(rounds)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    one = pricing.charged(pricing.stt_cost(60))
    assert entitlements.balance(db, login.account_id) == start - rounds * one
    counted = db.execute(
        "SELECT COUNT(*) AS n, SUM(amount) AS s FROM usage WHERE account_id = ?",
        (login.account_id,),
    ).fetchone()
    assert counted["n"] == rounds, "every charge must be counted exactly once"
    assert counted["s"] == 60 * rounds


def test_the_balance_matches_the_journal(db):
    """Balance = all top-ups minus all charges: otherwise it holds money of unknown origin."""
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.credit(db, login.account_id, 7 * pricing.MICRO, "topup")
    entitlements.charge(db, login.account_id, "stt_seconds", 600, pricing.stt_cost(600))
    entitlements.charge(db, login.account_id, "tts_chars", 500, pricing.tts_cost(500))

    credited = db.execute("SELECT SUM(micro) AS s FROM credits").fetchone()["s"]
    charged = db.execute("SELECT SUM(charged_micro) AS s FROM usage").fetchone()["s"]
    assert entitlements.balance(db, login.account_id) == credited - charged


def test_a_zero_top_up_is_refused(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    with pytest.raises(ValueError):
        entitlements.credit(db, login.account_id, 0, "topup")


def test_usage_is_counted_by_kind(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    entitlements.charge(db, login.account_id, "stt_seconds", 300, pricing.stt_cost(300))
    assert entitlements.spent(db, login.account_id, "stt_seconds") == 300
    assert entitlements.spent(db, login.account_id, "tts_chars") == 0


def test_who_am_i_needs_a_pass(http):
    assert http.get("/me").status_code == 401


# --------------------------------------------------------------------------
# Moving from the old database
# --------------------------------------------------------------------------


def test_accounts_without_a_password_are_closed_not_left_half_alive(tmp_path):
    """A database from the days of sign-in by code: there is no password, and the email is taken.

    Such an account is worse than a closed one: it looks working, silently lets no one in, and
    does not allow registering again on the same email. The migration must close it
    explicitly.
    """
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            email_key TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL
        );
        INSERT INTO accounts (email, email_key, created_at)
        VALUES ('old-timer@example.com', 'old-timer@example.com', 0);
    """)
    old.commit()
    old.close()

    db = connect(path)
    row = db.execute("SELECT password, blocked_at FROM accounts").fetchone()
    assert row["password"] == ""
    assert row["blocked_at"] is not None, "the account without a password was left open"

    with pytest.raises(accounts.Denied):
        accounts.sign_in(db, SECRET, "old-timer@example.com", PASSWORD)


def test_a_fresh_account_is_not_closed_by_the_migration(tmp_path):
    """The flip side: the migration must not touch live accounts."""
    db = connect(tmp_path / "new.sqlite")
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    assert db.execute("SELECT blocked_at FROM accounts").fetchone()["blocked_at"] is None


def _old_subscription_db(path, rows: str):
    """A database from the subscription era: without `balance_micro`, with `free_left` and a
    subscription.
    """
    import sqlite3

    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL, email_key TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL DEFAULT 'x', created_at REAL NOT NULL,
            free_left INTEGER NOT NULL DEFAULT 0, blocked_at REAL
        );
        CREATE TABLE subscriptions (
            account_id INTEGER PRIMARY KEY, plan TEXT NOT NULL,
            until REAL NOT NULL, seconds_left INTEGER NOT NULL DEFAULT 0
        );
    """ + rows)
    old.commit()
    old.close()


def test_the_old_gift_is_converted_to_money_once(tmp_path):
    path = tmp_path / "old.sqlite"
    _old_subscription_db(path, """
        INSERT INTO accounts (id, email, email_key, created_at, free_left)
        VALUES (1, 'a@example.com', 'a@example.com', 0, 300),
               (2, 'b@example.com', 'b@example.com', 0, 0);
    """)
    db = connect(path)
    assert entitlements.balance(db, 1) == pricing.WELCOME_MICRO     # 300 s = $0.30
    assert entitlements.balance(db, 2) == 0
    assert db.execute("SELECT free_left FROM accounts WHERE id = 1").fetchone()[0] == 0

    # The second connection does not recalculate again: the column already exists.
    db.close()
    again = connect(path)
    assert entitlements.balance(again, 1) == pricing.WELCOME_MICRO
    assert again.execute("SELECT COUNT(*) FROM credits").fetchone()[0] == 1


def test_an_owner_subscription_becomes_a_big_balance(tmp_path):
    path = tmp_path / "old.sqlite"
    _old_subscription_db(path, """
        INSERT INTO accounts (id, email, email_key, created_at)
        VALUES (1, 'user@example.com', 'user@example.com', 0);
        INSERT INTO subscriptions VALUES (1, 'owner', 9999999999, 360000000);
    """)
    db = connect(path)
    assert entitlements.balance(db, 1) >= 1_000 * pricing.MICRO


def test_a_live_paid_subscription_is_not_lost_in_the_move(tmp_path):
    path = tmp_path / "old.sqlite"
    _old_subscription_db(path, f"""
        INSERT INTO accounts (id, email, email_key, created_at)
        VALUES (1, 'a@example.com', 'a@example.com', 0);
        INSERT INTO subscriptions VALUES (1, 'premium', {time.time() + 86400}, 36000);
    """)
    db = connect(path)
    # Ten hours of recognition at the current price: a person must not lose them.
    assert entitlements.balance(db, 1) == 36000 * pricing.charged(pricing.stt_cost(1))


def test_an_expired_subscription_is_not_converted(tmp_path):
    path = tmp_path / "old.sqlite"
    _old_subscription_db(path, f"""
        INSERT INTO accounts (id, email, email_key, created_at)
        VALUES (1, 'a@example.com', 'a@example.com', 0);
        INSERT INTO subscriptions VALUES (1, 'premium', {time.time() - 10}, 36000);
    """)
    assert entitlements.balance(connect(path), 1) == 0


def test_the_right_is_sent_without_subscription_leftovers(db):
    """The entitlement no longer has a plan or seconds: the person sees money."""
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    out = entitlements.right(db, login.account_id).as_json()
    assert set(out) == {"allowed", "balance_micro", "balance", "low", "reason"}
    assert out["balance"] == pricing.money(pricing.WELCOME_MICRO) == "$0.30"


# --------------------------------------------------------------------------
# Spending by day (chart)
# --------------------------------------------------------------------------


def test_usage_by_day_is_a_continuous_series_ending_today(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    now = 1_790_000_000.0
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup", now=now)
    entitlements.charge(db, login.account_id, "stt_seconds", 60, pricing.stt_cost(60),
                        now=now - 3 * 86400)
    entitlements.charge(db, login.account_id, "tts_chars", 100, pricing.tts_cost(100),
                        now=now)

    days = entitlements.usage_daily(db, login.account_id, 30, now=now)
    assert len(days) == 30
    assert days[-1]["micro"] == pricing.charged(pricing.tts_cost(100))
    assert days[-4]["micro"] == pricing.charged(pricing.stt_cost(60))
    assert sum(d["micro"] for d in days) == days[-1]["micro"] + days[-4]["micro"]
    # Days without spending go in as zeros and do not disappear; otherwise the axis is
    # stretched wrongly.
    assert days[0]["micro"] == 0 and days[-2]["micro"] == 0
    assert days[-1]["by_kind"] == {"tts_chars": days[-1]["micro"]}


def test_usage_days_follow_the_users_timezone(db):
    """Spending at one in the morning in a UTC+3 timezone is already today, not yesterday in UTC."""
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    midnight_utc = 1_790_000_000.0 - (1_790_000_000.0 % 86400)
    at = midnight_utc - 3600                      # 23:00 UTC the day before = 02:00 in the UTC+3 zone
    entitlements.charge(db, login.account_id, "stt_seconds", 60, pricing.stt_cost(60), now=at)

    now = midnight_utc + 3600
    utc = entitlements.usage_daily(db, login.account_id, 3, tz_minutes=0, now=now)
    msk = entitlements.usage_daily(db, login.account_id, 3, tz_minutes=180, now=now)
    assert utc[-1]["micro"] == 0 and utc[-2]["micro"] > 0     # in UTC it is yesterday
    assert msk[-1]["micro"] > 0                                # in the UTC+3 zone it is today


def test_a_nonsense_timezone_is_clamped(db):
    login = accounts.register(db, SECRET, "user@example.com", PASSWORD)
    a = entitlements.usage_daily(db, login.account_id, 5, tz_minutes=10**9, now=1_790_000_000.0)
    b = entitlements.usage_daily(db, login.account_id, 5, tz_minutes=14 * 60, now=1_790_000_000.0)
    assert [d["day"] for d in a] == [d["day"] for d in b]


def test_usage_endpoint_needs_a_pass_and_is_private(http, db):
    assert http.get("/billing/usage").status_code == 401
    out = register(http).json()
    entitlements.charge(db, 1, "stt_seconds", 60, pricing.stt_cost(60))
    other = register(http, email="other@example.com").json()

    mine = http.get("/billing/usage", headers={"Authorization": f"Bearer {out['access']}"}).json()
    theirs = http.get("/billing/usage", headers={"Authorization": f"Bearer {other['access']}"}).json()
    assert sum(d["micro"] for d in mine["daily"]) > 0
    assert sum(d["micro"] for d in theirs["daily"]) == 0


def test_usage_endpoint_limits_the_window(http):
    out = register(http).json()
    h = {"Authorization": f"Bearer {out['access']}"}
    assert len(http.get("/billing/usage?days=1000", headers=h).json()["daily"]) == 90
    assert len(http.get("/billing/usage?days=0", headers=h).json()["daily"]) == 1
