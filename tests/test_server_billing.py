"""The payment gateway: order, payment, webhook replay, balance top-up.

What is checked is not "the route answers 200" but what breaks money: a payment credited
twice, a payment without an order, a pay button where there is no gateway, an amount that
must not exist.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from argus.server import billing, entitlements, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


PASSWORD = "reasonably-long-password"


def signed_in(client, email="user@example.com"):
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD, "terms": "t"})
    assert r.status_code == 200, r.text
    return r.json()["access"]


def auth(access):
    return {"Authorization": f"Bearer {access}"}


@pytest.fixture
def shop(db):
    """A server with a stub payment provider."""
    app = build(db, SECRET, pay=billing.Stub())
    with TestClient(app) as client:
        yield client, db


@pytest.fixture
def closed(db):
    """A server without a payment provider, which is the default."""
    with TestClient(build(db, SECRET)) as client:
        yield client, db


# --------------------------------------------------------------------------
# Price list
# --------------------------------------------------------------------------


def test_the_price_list_says_whether_the_till_works(shop, closed):
    client, _ = shop
    body = client.get("/pricing").json()
    assert body["payment"] == "stub"
    assert body["topups"] == list(pricing.TOPUP_PRESETS)
    assert body["min_topup"] == pricing.MIN_TOPUP

    dark, _ = closed
    assert dark.get("/pricing").json()["payment"] is None


def test_the_price_list_names_every_paid_thing(shop):
    client, _ = shop
    body = client.get("/pricing").json()
    assert body["stt_hour"].startswith("$")
    assert body["tts_1000"].startswith("$")
    assert [a["title"] for a in body["answers"]] == [t for t, _ in pricing.ANSWER_TIERS]


# --------------------------------------------------------------------------
# Top-up
# --------------------------------------------------------------------------


def test_a_top_up_goes_on_the_balance_one_to_one(shop):
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)

    order = client.post("/billing/checkout", json={"dollars": 10},
                        headers=auth(access)).json()
    assert order["state"] == "new"
    assert order["price"] == "$10"
    assert order["amount"] == 1000
    assert order["pay_url"].endswith(f"/billing/stub/{order['order']}")
    # Nothing was added before payment.
    assert entitlements.balance(db, 1) == before

    assert client.post(f"/billing/stub/{order['order']}").json()["first_time"] is True
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO


def test_a_top_up_brings_a_broke_account_back(shop):
    client, db = shop
    access = signed_in(client)
    entitlements.charge(db, 1, "stt_seconds", 1, pricing.WELCOME_MICRO)
    assert entitlements.right(db, 1).allowed is False

    order = client.post("/billing/checkout", json={"dollars": 5},
                        headers=auth(access)).json()
    client.post(f"/billing/stub/{order['order']}")
    assert entitlements.right(db, 1).allowed is True


def test_the_same_payment_cannot_be_counted_twice(shop):
    """The provider repeats a webhook until it gets an answer. This is an ordinary Tuesday."""
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = client.post("/billing/checkout", json={"dollars": 20},
                        headers=auth(access)).json()

    first = client.post(f"/billing/stub/{order['order']}").json()
    second = client.post(f"/billing/stub/{order['order']}").json()
    assert first["first_time"] is True
    assert second["first_time"] is False

    assert entitlements.balance(db, 1) == before + 20 * pricing.MICRO


def test_a_top_up_is_written_to_the_journal_with_its_order(shop):
    client, db = shop
    access = signed_in(client)
    order = client.post("/billing/checkout", json={"dollars": 5},
                        headers=auth(access)).json()
    client.post(f"/billing/stub/{order['order']}")

    row = db.execute("SELECT kind, micro, note FROM credits WHERE kind = 'topup'").fetchone()
    assert row["micro"] == 5 * pricing.MICRO
    assert str(order["order"]) in row["note"]


@pytest.mark.parametrize("dollars", [0, 1, 4, 501, 100_000])
def test_an_amount_outside_the_limits_is_refused(shop, dollars):
    client, db = shop
    access = signed_in(client)
    r = client.post("/billing/checkout", json={"dollars": dollars}, headers=auth(access))
    assert r.status_code in (400, 422)
    # A refusal leaves no orders behind: the check happens before writing to the database.
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_a_fractional_amount_is_refused(shop):
    client, _ = shop
    access = signed_in(client)
    r = client.post("/billing/checkout", json={"dollars": 7.5}, headers=auth(access))
    assert r.status_code == 422


def test_the_smallest_and_largest_amounts_pass(shop):
    client, _ = shop
    access = signed_in(client)
    for dollars in (pricing.MIN_TOPUP, pricing.MAX_TOPUP):
        r = client.post("/billing/checkout", json={"dollars": dollars}, headers=auth(access))
        assert r.status_code == 200, r.text


def test_paying_needs_a_login(shop):
    client, _ = shop
    assert client.post("/billing/checkout", json={"dollars": 5}).status_code == 401


def test_a_missing_order_is_not_paid(shop):
    client, _ = shop
    assert client.post("/billing/stub/99999").status_code == 400


def test_an_order_from_the_subscription_era_is_not_credited(shop):
    """Money for a plan that no longer exists cannot be credited at the plan's price, and cannot be
    silently swallowed: the order waits for manual review."""
    client, db = shop
    signed_in(client)
    db.execute(
        "INSERT INTO orders (account_id, plan, amount, currency, provider, state, created_at) "
        "VALUES (1, 'premium', 2700, 'USD', 'stub', 'new', ?)", (time.time(),))
    db.commit()
    before = entitlements.balance(db, 1)

    assert client.post("/billing/stub/1").status_code == 400
    assert entitlements.balance(db, 1) == before


# --------------------------------------------------------------------------
# Without a payment gateway
# --------------------------------------------------------------------------


def test_without_a_till_the_answer_says_so(closed):
    """Not a "server error", but text from which it is clear what to do."""
    client, _ = closed
    access = signed_in(client)
    r = client.post("/billing/checkout", json={"dollars": 5}, headers=auth(access))
    assert r.status_code == 400
    assert "isn't connected" in r.json()["detail"]


def test_the_stub_page_does_not_exist_without_the_stub(closed):
    """Otherwise it would be found at the direct address, and money would be put on the balance for
    free.
    """
    client, _ = closed
    assert client.get("/billing/stub/1").status_code == 404
    assert client.post("/billing/stub/1").status_code == 404


def test_the_stub_page_warns_what_it_is(shop):
    client, _ = shop
    access = signed_in(client)
    order = client.post("/billing/checkout", json={"dollars": 5},
                        headers=auth(access)).json()
    page = client.get(f"/billing/stub/{order['order']}").text
    assert "No money is taken" in page
    assert "ARGUS_BILLING=stub" in page


def test_the_till_is_off_unless_asked_for(monkeypatch):
    """A stub left in production hands out money for free."""
    monkeypatch.delenv("ARGUS_BILLING", raising=False)
    monkeypatch.delenv("ARGUS_ALLOW_STUB", raising=False)
    monkeypatch.delenv("ARGUS_SERVER_URL", raising=False)
    assert billing.from_env().name == "off"
    monkeypatch.setenv("ARGUS_BILLING", "stub")
    monkeypatch.setenv("ARGUS_ALLOW_STUB", "1")
    assert billing.from_env().name == "stub"


def test_one_line_in_the_env_file_is_not_enough_to_open_the_stub(monkeypatch, caplog):
    """A `.env` saved from "before the payment provider" with `ARGUS_BILLING=stub` must not hand out
    money again at an open address: the stub page asks nobody."""
    monkeypatch.setenv("ARGUS_BILLING", "stub")
    monkeypatch.delenv("ARGUS_ALLOW_STUB", raising=False)
    monkeypatch.delenv("ARGUS_SERVER_URL", raising=False)
    assert billing.from_env().name == "off"
    assert "ARGUS_ALLOW_STUB" in caplog.text


@pytest.mark.parametrize("address, allowed", [
    ("", True), ("http://127.0.0.1:8421", True), ("http://localhost:8421/", True),
    ("http://[::1]:8421", True),
    ("http://203.0.113.10:8421", False), ("https://pay.example.com", False),
    ("http://127.0.0.1.evil.example:8421", False),
])
def test_the_stub_needs_the_second_switch_and_a_local_address(monkeypatch, address, allowed):
    monkeypatch.setenv("ARGUS_BILLING", "stub")
    monkeypatch.setenv("ARGUS_ALLOW_STUB", "1")
    monkeypatch.setenv("ARGUS_SERVER_URL", address)
    assert (billing.from_env().name == "stub") is allowed


def test_the_second_switch_must_be_exactly_one(monkeypatch):
    monkeypatch.setenv("ARGUS_BILLING", "stub")
    monkeypatch.delenv("ARGUS_SERVER_URL", raising=False)
    for value in ("", "0", "yes", "true"):
        monkeypatch.setenv("ARGUS_ALLOW_STUB", value)
        assert billing.from_env().name == "off", value


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


def test_the_order_is_written_before_the_till(shop):
    """Without a row created in advance, a returning payment has nothing to be matched against."""
    client, db = shop
    access = signed_in(client)
    client.post("/billing/checkout", json={"dollars": 10}, headers=auth(access))

    row = db.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "new"
    assert row["amount"] == 1000
    assert row["currency"] == "USD"
    assert row["paid_at"] is None


def test_the_history_is_visible_to_its_owner(shop):
    client, _ = shop
    access = signed_in(client)
    order = client.post("/billing/checkout", json={"dollars": 20},
                        headers=auth(access)).json()
    client.post(f"/billing/stub/{order['order']}")

    rows = client.get("/billing/orders", headers=auth(access)).json()["orders"]
    assert len(rows) == 1
    assert rows[0]["state"] == "paid"
    assert rows[0]["amount"] == 2000
    assert rows[0]["paid_at"] == pytest.approx(time.time(), abs=30)


# --------------------------------------------------------------------------
# Top-up history
# --------------------------------------------------------------------------


def test_the_history_shows_the_gift_and_the_top_ups(shop):
    client, db = shop
    access = signed_in(client)
    order = client.post("/billing/checkout", json={"dollars": 10},
                        headers=auth(access)).json()
    client.post(f"/billing/stub/{order['order']}")

    rows = client.get("/billing/history", headers=auth(access)).json()["credits"]
    assert [(r["kind"], r["amount"]) for r in rows] == [("topup", "$10.00"), ("gift", "$0.30")]
    assert rows[0]["micro"] == 10 * pricing.MICRO


def test_the_history_matches_the_balance_before_any_spending(shop):
    client, db = shop
    access = signed_in(client)
    order = client.post("/billing/checkout", json={"dollars": 20},
                        headers=auth(access)).json()
    client.post(f"/billing/stub/{order['order']}")
    rows = client.get("/billing/history", headers=auth(access)).json()["credits"]
    assert sum(r["micro"] for r in rows) == entitlements.balance(db, 1)


def test_the_history_is_private(shop):
    client, _ = shop
    mine = signed_in(client)
    other = signed_in(client, "other@example.com")
    order = client.post("/billing/checkout", json={"dollars": 50},
                        headers=auth(other)).json()
    client.post(f"/billing/stub/{order['order']}")
    rows = client.get("/billing/history", headers=auth(mine)).json()["credits"]
    assert [r["kind"] for r in rows] == ["gift"]


def test_the_history_needs_a_login(shop):
    client, _ = shop
    assert client.get("/billing/history").status_code == 401
