"""LiqPay: order, redirect to the payment gateway, payment notification.

What is checked is what breaks money: a notification that was not sent by LiqPay; a
payment for a smaller amount than the order; a repeat; a test status on live keys; a
return page that would credit money merely because someone landed on it.

The signature in the tests is computed by the tests' own code, not by the code from
`billing`, plus two control values computed separately: a test that checks the code
against itself would not see an error that is the same in both places.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from argus.server import billing, entitlements, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"

PUBLIC = "i12345678901"
PRIVATE = "private-key-liqpay"
BASE = "https://pay.example.com"


def sign(data: str, algorithm: str = "sha1", key: str = PRIVATE) -> str:
    raw = (key + data + key).encode("utf-8")
    return base64.b64encode(hashlib.new(algorithm, raw).digest()).decode("ascii")


def packed(body: dict, algorithm: str = "sha1", key: str = PRIVATE) -> dict:
    """The fields in the form LiqPay sends them: `data` and `signature`."""
    data = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")
    return {"data": data, "signature": sign(data, algorithm, key)}


def notice(order: dict, status: str = "success", amount=None, currency: str = "USD",
           **extra) -> dict:
    """Payment notice for the order `order` (the response of `/billing/checkout`)."""
    body = {
        "status": status,
        "order_id": f"argus-{order['order']}",
        "payment_id": 1234567890,
        "amount": order["amount"] / 100 if amount is None else amount,
        "currency": currency,
        "public_key": PUBLIC,
        **extra,
    }
    return packed(body)


def signed_in(client, email="user@example.com"):
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD, "terms": "t"})
    assert r.status_code == 200, r.text
    return r.json()["access"]


def auth(access):
    return {"Authorization": f"Bearer {access}"}


def checkout(client, access, dollars=10, **extra):
    r = client.post("/billing/checkout", json={"dollars": dollars, **extra},
                    headers=auth(access))
    assert r.status_code == 200, r.text
    return r.json()


def path_of(url: str) -> str:
    assert url.startswith(BASE), url
    return url[len(BASE):]


def form_of(page: str) -> dict[str, str]:
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page))


def params_of(page: str) -> dict:
    return json.loads(base64.b64decode(form_of(page)["data"]))


def state_of(db, order_id=1) -> str:
    return db.execute("SELECT state FROM orders WHERE id = ?", (order_id,)).fetchone()["state"]


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def shop(db):
    """Server with LiqPay on live keys."""
    with TestClient(build(db, SECRET, pay=billing.LiqPay(PUBLIC, PRIVATE, BASE))) as client:
        yield client, db


@pytest.fixture
def sandbox(db):
    """Server with LiqPay on test keys."""
    provider = billing.LiqPay(PUBLIC, PRIVATE, BASE, sandbox=True)
    with TestClient(build(db, SECRET, pay=provider)) as client:
        yield client, db


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------


def test_the_signature_matches_answers_worked_out_separately():
    liq = billing.LiqPay("pub", "sandbox_secret_key", BASE)
    data = "eyJ2ZXJzaW9uIjozLCJhbW91bnQiOjV9"
    assert liq.sign(data) == "/qq7PECdieWJD4s5ezoPfN3ncPM="
    sha3 = billing.LiqPay("pub", "sandbox_secret_key", BASE, digest="sha3_256")
    assert sha3.sign(data) == "jfE/H/Z2YkEdnG0HWF4G6ULR7A3GCNPqjV9dzjHCNvc="


def test_a_signature_in_the_other_hash_is_recognised_too():
    """The LiqPay documentation says SHA3-256, but their examples and SDK use SHA1: we accept
    either one.
    """
    liq = billing.LiqPay("pub", "sandbox_secret_key", BASE)
    data = "eyJ2ZXJzaW9uIjozLCJhbW91bnQiOjV9"
    assert liq.genuine(data, "/qq7PECdieWJD4s5ezoPfN3ncPM=")
    assert liq.genuine(data, "jfE/H/Z2YkEdnG0HWF4G6ULR7A3GCNPqjV9dzjHCNvc=")
    assert not liq.genuine(data, "AAAA")
    assert not liq.genuine(data + "x", "/qq7PECdieWJD4s5ezoPfN3ncPM=")


def test_a_signature_with_foreign_letters_is_refused_not_a_crash():
    """`compare_digest` raises an error on non-ASCII strings; all we want is a plain refusal."""
    liq = billing.LiqPay("pub", "k", BASE)
    assert liq.genuine("data", "sígnaturé") is False


def test_an_unknown_hash_is_refused_at_once():
    with pytest.raises(ValueError):
        billing.LiqPay("pub", "k", BASE, digest="md5")


# --------------------------------------------------------------------------
# Order and redirect to LiqPay
# --------------------------------------------------------------------------


def test_the_price_list_says_the_till_works_without_naming_it(shop):
    """The app cares about one thing only, whether there is a till; whose it is, the server's
    responses do not say.
    """
    client, _ = shop
    sheet = client.get("/pricing").json()
    assert sheet["payment"] == "on"
    assert "liqpay" not in str(sheet).lower()


def test_the_order_leads_to_our_page_not_to_the_till_directly(shop):
    client, db = shop
    order = checkout(client, signed_in(client), 10)
    assert order["state"] == "new"
    assert order["pay_url"] == f"{BASE}/billing/liqpay/{order['order']}"
    # A signature and a key in the address the person sees are not needed and must not be there.
    assert "signature" not in order["pay_url"]
    assert PRIVATE not in order["pay_url"]
    assert db.execute("SELECT provider FROM orders").fetchone()["provider"] == "liqpay"


def test_the_page_sends_a_signed_form_to_liqpay(shop):
    client, _ = shop
    order = checkout(client, signed_in(client), 10)
    page = client.get(path_of(order["pay_url"])).text

    assert 'action="https://www.liqpay.ua/api/3/checkout"' in page
    assert 'method="POST"' in page
    form = form_of(page)
    assert form["signature"] == sign(form["data"])

    params = params_of(page)
    assert params["version"] == 3
    assert params["public_key"] == PUBLIC
    assert params["action"] == "pay"
    assert params["amount"] == 10 and isinstance(params["amount"], int)
    assert params["currency"] == "USD"
    assert params["order_id"] == f"argus-{order['order']}"
    assert params["language"] == "en"
    assert params["result_url"] == f"{BASE}/billing/done"
    assert params["server_url"] == f"{BASE}/billing/liqpay/callback"
    assert "sandbox" not in params
    assert PRIVATE not in page


def test_the_page_is_tested_mode_on_the_test_keys(sandbox):
    client, _ = sandbox
    order = checkout(client, signed_in(client), 5)
    assert params_of(client.get(path_of(order["pay_url"])).text)["sandbox"] == 1


@pytest.mark.parametrize("app_language, liqpay_language", [
    ("uk", "uk"), ("ru", "en"), ("en", "en"), ("de", "en"), ("pl", "en"), ("", "en"),
])
def test_the_payment_page_follows_the_app_language(shop, app_language, liqpay_language):
    """LiqPay's page exists only in `uk` and English."""
    client, _ = shop
    order = checkout(client, signed_in(client), 5, lang=app_language)
    page = client.get(path_of(order["pay_url"])).text
    assert params_of(page)["language"] == liqpay_language


def test_a_garbage_language_changes_nothing_but_the_language(shop):
    client, _ = shop
    access = signed_in(client)
    order = checkout(client, access, 5, lang="<script>")
    assert "?" not in order["pay_url"]
    page = client.get(f"/billing/liqpay/{order['order']}?lang=<script>").text
    assert params_of(page)["language"] == "en"
    assert "<script>alert" not in page


def test_a_paid_order_gets_no_second_payment_form(shop):
    client, _ = shop
    order = checkout(client, signed_in(client), 10)
    client.post("/billing/liqpay/callback", data=notice(order))
    page = client.get(path_of(order["pay_url"])).text
    assert "<form" not in page
    assert "Back to Argus" in page


def test_an_unknown_order_has_no_payment_page(shop):
    client, _ = shop
    assert client.get("/billing/liqpay/999").status_code == 404


def test_the_page_of_a_stub_order_is_not_a_liqpay_page(db):
    """An order created by a different till must not be shown a LiqPay page."""
    with TestClient(build(db, SECRET, pay=billing.LiqPay(PUBLIC, PRIVATE, BASE))) as client:
        signed_in(client)
        db.execute(
            "INSERT INTO orders (account_id, plan, amount, currency, provider, state, created_at) "
            "VALUES (1, 'topup', 500, 'USD', 'stub', 'new', ?)", (time.time(),))
        db.commit()
        assert client.get("/billing/liqpay/1").status_code == 404


# --------------------------------------------------------------------------
# Payment notification
# --------------------------------------------------------------------------


def test_a_genuine_notice_credits_the_balance_once(shop):
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)

    first = client.post("/billing/liqpay/callback", data=notice(order))
    assert first.status_code == 200
    assert first.json()["result"] == "paid"
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO
    assert state_of(db) == "paid"

    # LiqPay repeats the notification until it gets an answer.
    again = client.post("/billing/liqpay/callback", data=notice(order))
    assert again.status_code == 200
    assert again.json()["result"] == "again"
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO


def test_the_credit_is_written_with_the_payment_id(shop):
    client, db = shop
    order = checkout(client, signed_in(client), 5)
    client.post("/billing/liqpay/callback", data=notice(order, payment_id=777))
    row = db.execute("SELECT external_id FROM orders").fetchone()
    assert row["external_id"] == "liqpay-777"


def test_the_same_payment_cannot_cover_two_orders(shop):
    """Unique index on the payment number: someone else's replay under a different order will
    not go through.
    """
    client, db = shop
    access = signed_in(client)
    first = checkout(client, access, 10)
    second = checkout(client, access, 10)
    client.post("/billing/liqpay/callback", data=notice(first, payment_id=42))
    before = entitlements.balance(db, 1)

    r = client.post("/billing/liqpay/callback", data=notice(second, payment_id=42))
    assert r.status_code == 200
    assert r.json()["result"] == "again"
    assert entitlements.balance(db, 1) == before


def test_a_notice_signed_with_the_other_hash_is_taken(shop):
    client, _ = shop
    order = checkout(client, signed_in(client), 5)
    body = notice(order)
    fields = packed(json.loads(base64.b64decode(body["data"])), algorithm="sha3_256")
    assert client.post("/billing/liqpay/callback", data=fields).json()["result"] == "paid"


def test_a_notice_can_arrive_as_json_too(shop):
    client, _ = shop
    order = checkout(client, signed_in(client), 5)
    r = client.post("/billing/liqpay/callback", json=notice(order))
    assert r.json()["result"] == "paid"


def test_a_notice_with_a_wrong_signature_is_refused(shop):
    """The address is open to everyone: without a valid signature, anyone could "pay" for
    themselves.
    """
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 500)

    forged = notice(order)
    forged["signature"] = sign(forged["data"], key="someone-elses-key")
    r = client.post("/billing/liqpay/callback", data=forged)
    assert r.status_code == 400
    assert entitlements.balance(db, 1) == before
    assert state_of(db) == "new"


def test_a_notice_edited_after_signing_is_refused(shop):
    """The signature covers the data: replace the amount and the signature no longer matches."""
    client, _ = shop
    order = checkout(client, signed_in(client), 5)
    honest = notice(order, amount=1)
    body = json.loads(base64.b64decode(honest["data"]))
    body["amount"] = 5
    tampered = {"data": base64.b64encode(json.dumps(body).encode()).decode(),
                "signature": honest["signature"]}
    assert client.post("/billing/liqpay/callback", data=tampered).status_code == 400


def test_a_notice_with_no_fields_is_refused(shop):
    client, _ = shop
    assert client.post("/billing/liqpay/callback", data={}).status_code == 400
    assert client.post("/billing/liqpay/callback", content=b"").status_code == 400


def test_a_signed_notice_that_is_not_a_payment_is_refused(shop):
    client, _ = shop
    for junk in ("not base64!!", base64.b64encode(b"not json").decode(),
                 base64.b64encode(b"[1, 2]").decode()):
        fields = {"data": junk, "signature": sign(junk)}
        assert client.post("/billing/liqpay/callback", data=fields).status_code == 400


def test_a_huge_notice_is_refused_before_it_is_read(shop):
    client, _ = shop
    r = client.post("/billing/liqpay/callback", content=b"x" * 70_000,
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_a_smaller_payment_does_not_credit_a_bigger_order(shop):
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 50)

    r = client.post("/billing/liqpay/callback", data=notice(order, amount=5))
    assert r.status_code == 200
    assert r.json()["result"] == "rejected"
    assert entitlements.balance(db, 1) == before
    assert state_of(db) == "new"


def test_a_payment_in_another_currency_is_not_credited(shop):
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)
    r = client.post("/billing/liqpay/callback", data=notice(order, currency="UAH"))
    assert r.json()["result"] == "rejected"
    assert entitlements.balance(db, 1) == before
    assert state_of(db) == "new"


@pytest.mark.parametrize("status", [
    "failure", "error", "processing", "wait_secure", "3ds_verify", "hold_wait", "",
])
def test_an_unfinished_or_failed_payment_credits_nothing(shop, status):
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)

    r = client.post("/billing/liqpay/callback", data=notice(order, status=status))
    assert r.status_code == 200
    assert r.json()["result"] == "waiting"
    assert entitlements.balance(db, 1) == before
    assert state_of(db) == "new"


def test_a_payment_waiting_for_the_daily_settlement_is_credited_once(shop):
    """`wait_compensation`: the card has been charged, but the money reaches the till tomorrow.
    This is a real payment, and the next `success` notice for it will not double the balance."""
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)

    first = client.post("/billing/liqpay/callback", data=notice(order, status="wait_compensation"))
    assert first.json()["result"] == "paid"
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO

    later = client.post("/billing/liqpay/callback", data=notice(order))
    assert later.json()["result"] == "again"
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO


def test_money_taken_for_a_shop_that_is_not_verified_is_not_credited(shop, caplog):
    """`wait_accept`: the person has been charged, but the company has not passed verification,
    so the payment may be reversed and no balance is granted against it; the journal says so."""
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)

    r = client.post("/billing/liqpay/callback", data=notice(order, status="wait_accept"))
    assert r.json()["result"] == "waiting"
    assert entitlements.balance(db, 1) == before
    assert state_of(db) == "new"
    assert "wait_accept" in caplog.text


def test_a_failed_attempt_does_not_block_a_later_success(shop):
    """On the LiqPay page the payment for the same order can be retried."""
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)
    client.post("/billing/liqpay/callback", data=notice(order, status="failure"))
    client.post("/billing/liqpay/callback", data=notice(order))
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO


def test_a_refund_takes_the_money_back_off_the_balance(shop):
    """Stolen card: the thief topped up $10, spent it, and the card was disputed. The money must
    be taken back, otherwise we are left with neither the money nor a trace (audit 2026-10-16)."""
    client, db = shop
    order = checkout(client, signed_in(client), 10)
    client.post("/billing/liqpay/callback", data=notice(order))
    after_payment = entitlements.balance(db, 1)
    assert after_payment == pricing.WELCOME_MICRO + 10 * pricing.MICRO

    r = client.post("/billing/liqpay/callback", data=notice(order, status="reversed"))
    assert r.json()["result"] == "refund"
    assert entitlements.balance(db, 1) == pricing.WELCOME_MICRO
    # The journal gets a row with a minus; the balance still equals the journal.
    kinds = [(r["kind"], r["micro"]) for r in db.execute("SELECT kind, micro FROM credits ORDER BY id")]
    assert kinds[-1] == ("reversal", -10 * pricing.MICRO)


def test_a_refund_after_the_money_was_spent_leaves_a_debt_that_blocks_paid_calls(shop):
    """The refunded money is already spent: the balance goes negative, paid calls are closed, and
    a top-up pays off the debt.
    """
    client, db = shop
    order = checkout(client, signed_in(client), 10)
    client.post("/billing/liqpay/callback", data=notice(order))
    entitlements.charge(db, 1, "stt_seconds", 3600, 9 * pricing.MICRO)   # spent almost everything

    client.post("/billing/liqpay/callback", data=notice(order, status="reversed"))
    left = entitlements.balance(db, 1)
    assert left < 0
    assert not entitlements.hold(db, 1, 1_000).allowed

    again = checkout(client, auth_token(client), 5)
    client.post("/billing/liqpay/callback", data=notice(again, payment_id=999))
    assert entitlements.balance(db, 1) == left + 5 * pricing.MICRO     # the debt is paid off first


def auth_token(client, email="user@example.com"):
    r = client.post("/auth/sign-in", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access"]


def test_a_refund_leaves_a_durable_mark_not_just_a_log_line(shop, caplog):
    """The container log rotates while the person keeps spending the refunded money: the case
    must be investigated from the database, and the order must not look "paid"."""
    client, db = shop
    order = checkout(client, signed_in(client), 10)
    client.post("/billing/liqpay/callback", data=notice(order))
    assert state_of(db) == "paid"

    client.post("/billing/liqpay/callback", data=notice(order, status="reversed"))
    row = db.execute("SELECT state, paid_at, reversed_at FROM orders").fetchone()
    assert row["state"] == "reversed"
    assert row["paid_at"] is not None and row["reversed_at"] is not None
    assert "refunded by the payment processor" in caplog.text
    assert db.execute("SELECT COUNT(*) FROM orders WHERE state = 'reversed'").fetchone()[0] == 1


def test_a_replayed_success_after_a_refund_credits_nothing(shop):
    client, db = shop
    order = checkout(client, signed_in(client), 10)
    client.post("/billing/liqpay/callback", data=notice(order))
    client.post("/billing/liqpay/callback", data=notice(order, status="reversed"))
    after_refund = entitlements.balance(db, 1)

    again = client.post("/billing/liqpay/callback", data=notice(order))
    assert again.json()["result"] == "again"
    assert entitlements.balance(db, 1) == after_refund
    assert state_of(db) == "reversed"
    # A repeat of the refund notification itself breaks nothing.
    twice = client.post("/billing/liqpay/callback", data=notice(order, status="reversed"))
    assert twice.json()["result"] == "refund"


def test_a_refund_that_arrives_before_the_payment_closes_the_order(shop):
    """Till notices arrive out of order: a refund that comes before "success" closes the order,
    and a late "success" credits nothing."""
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)
    r = client.post("/billing/liqpay/callback", data=notice(order, status="reversed"))
    assert r.json()["result"] == "refund"
    assert state_of(db) == "reversed"
    late = client.post("/billing/liqpay/callback", data=notice(order))
    assert late.json()["result"] == "again"
    assert entitlements.balance(db, 1) == before


def test_the_test_status_on_a_public_server_pays_only_the_owners_accounts(db, monkeypatch):
    """Anyone can pay with LiqPay's test card: on a public server its payment is credited only to
    accounts from `ARGUS_SANDBOX_ACCOUNTS` (by default, to nobody)."""
    for name, value in (("ARGUS_BILLING", "liqpay"), ("ARGUS_LIQPAY_PUBLIC_KEY", PUBLIC.replace("i", "sandbox_i")),
                        ("ARGUS_LIQPAY_PRIVATE_KEY", PRIVATE), ("ARGUS_SERVER_URL", BASE)):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ARGUS_SANDBOX_ACCOUNTS", raising=False)
    provider = billing.from_env()
    assert provider.sandbox is True and provider.sandbox_accounts == frozenset()

    with TestClient(build(db, SECRET, pay=provider)) as client:
        access = signed_in(client)
        before = entitlements.balance(db, 1)
        order = checkout(client, access, 500)
        body = {"status": "sandbox", "order_id": f"argus-{order['order']}", "payment_id": 7,
                "amount": 500, "currency": "USD", "public_key": provider.public_key}
        r = client.post("/billing/liqpay/callback", data=packed(body))
        assert r.json()["result"] == "waiting"
        assert entitlements.balance(db, 1) == before          # the $500 for free did not come out

        # The owner added the account to the list: its test payments work.
        provider.sandbox_accounts = frozenset({1})
        r = client.post("/billing/liqpay/callback", data=packed(body))
        assert r.json()["result"] == "paid"
        assert entitlements.balance(db, 1) == before + 500 * pricing.MICRO


def test_the_owners_list_is_read_from_the_environment(monkeypatch):
    assert billing._account_ids("1, 2;x,  7 ,-3,") == frozenset({1, 2, 7})
    assert billing._account_ids("") == frozenset()


def test_the_test_status_is_not_a_payment_on_real_keys(shop):
    """Otherwise a test payment would credit real money."""
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)
    r = client.post("/billing/liqpay/callback", data=notice(order, status="sandbox"))
    assert r.json()["result"] == "waiting"
    assert entitlements.balance(db, 1) == before


def test_the_test_status_is_a_payment_on_the_test_keys(sandbox):
    client, db = sandbox
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)
    r = client.post("/billing/liqpay/callback", data=notice(order, status="sandbox"))
    assert r.json()["result"] == "paid"
    assert entitlements.balance(db, 1) == before + 10 * pricing.MICRO


def test_a_notice_about_a_foreign_order_is_taken_and_ignored(shop):
    """A number that is not ours and an order that does not exist get "accepted", not a 500:
    there is nothing to retry.
    """
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)

    for body in (
        {"status": "success", "order_id": "shop-1", "amount": 10, "currency": "USD"},
        {"status": "success", "order_id": "argus-999", "amount": 10, "currency": "USD"},
        {"status": "success", "order_id": "argus-1x", "amount": 10, "currency": "USD"},
        {"status": "success", "amount": 10, "currency": "USD"},
    ):
        r = client.post("/billing/liqpay/callback", data=packed(body))
        assert r.status_code == 200, body
        assert r.json()["result"] == "rejected", body
    assert entitlements.balance(db, 1) == before
    assert state_of(db, order["order"]) == "new"


def test_a_notice_under_another_merchant_key_is_ignored(shop):
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)
    r = client.post("/billing/liqpay/callback", data=notice(order, public_key="i99999999999"))
    assert r.json()["result"] == "rejected"
    assert entitlements.balance(db, 1) == before


def test_a_notice_for_an_order_of_another_till_is_ignored(db):
    with TestClient(build(db, SECRET, pay=billing.LiqPay(PUBLIC, PRIVATE, BASE))) as client:
        signed_in(client)
        before = entitlements.balance(db, 1)
        db.execute(
            "INSERT INTO orders (account_id, plan, amount, currency, provider, state, created_at) "
            "VALUES (1, 'topup', 500, 'USD', 'stub', 'new', ?)", (time.time(),))
        db.commit()
        body = {"status": "success", "order_id": "argus-1", "amount": 5, "currency": "USD"}
        r = client.post("/billing/liqpay/callback", data=packed(body))
        assert r.json()["result"] == "rejected"
        assert entitlements.balance(db, 1) == before


# --------------------------------------------------------------------------
# The person's return
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "post"])
def test_the_return_page_credits_nothing(shop, method):
    """The page can be reached by its address without paying, so it has no right to credit
    anything.
    """
    client, db = shop
    access = signed_in(client)
    before = entitlements.balance(db, 1)
    order = checkout(client, access, 10)

    r = getattr(client, method)("/billing/done")
    assert r.status_code == 200
    assert "Back to Argus" in r.text
    assert entitlements.balance(db, 1) == before
    assert state_of(db, order["order"]) == "new"


# --------------------------------------------------------------------------
# Other payment gateways and LiqPay do not get in each other's way
# --------------------------------------------------------------------------


def test_the_liqpay_routes_do_not_exist_under_another_till(db):
    for pay in (billing.Stub(), billing.Off()):
        with TestClient(build(db, SECRET, pay=pay)) as client:
            assert client.get("/billing/liqpay/1").status_code == 404
            assert client.post("/billing/liqpay/callback", data={}).status_code == 404


def test_the_stub_page_does_not_exist_under_liqpay(shop):
    """Otherwise the stub would put money on the balance, bypassing the till."""
    client, _ = shop
    assert client.get("/billing/stub/1").status_code == 404
    assert client.post("/billing/stub/1").status_code == 404


# --------------------------------------------------------------------------
# Enabling from the environment
# --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    for name in ("ARGUS_BILLING", "ARGUS_LIQPAY_PUBLIC_KEY", "ARGUS_LIQPAY_PRIVATE_KEY",
                 "ARGUS_LIQPAY_SANDBOX", "ARGUS_LIQPAY_HASH", "ARGUS_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)

    def setup(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return setup


def test_liqpay_is_switched_on_by_the_environment(env):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY=PUBLIC,
        ARGUS_LIQPAY_PRIVATE_KEY=PRIVATE, ARGUS_SERVER_URL=BASE + "/")
    till = billing.from_env()
    assert isinstance(till, billing.LiqPay)
    assert till.base == BASE
    assert till.sandbox is False
    assert till.digest == "sha1"


def test_the_address_may_come_as_an_argument(env):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY=PUBLIC,
        ARGUS_LIQPAY_PRIVATE_KEY=PRIVATE)
    assert billing.from_env(BASE).name == "liqpay"


def test_the_test_keys_switch_the_test_mode_on_by_themselves(env):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY="sandbox_i123",
        ARGUS_LIQPAY_PRIVATE_KEY="sandbox_k", ARGUS_SERVER_URL=BASE)
    assert billing.from_env().sandbox is True


def test_the_test_mode_can_be_asked_for_by_name(env):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY=PUBLIC,
        ARGUS_LIQPAY_PRIVATE_KEY=PRIVATE, ARGUS_SERVER_URL=BASE, ARGUS_LIQPAY_SANDBOX="1")
    assert billing.from_env().sandbox is True


def test_the_test_mode_is_loud_in_the_log(env, caplog):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY="sandbox_i123",
        ARGUS_LIQPAY_PRIVATE_KEY="sandbox_k", ARGUS_SERVER_URL=BASE)
    billing.from_env()
    assert "LIQPAY IN SANDBOX" in caplog.text


def test_the_other_hash_can_be_asked_for(env):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY=PUBLIC,
        ARGUS_LIQPAY_PRIVATE_KEY=PRIVATE, ARGUS_SERVER_URL=BASE,
        ARGUS_LIQPAY_HASH="SHA3_256")
    assert billing.from_env().digest == "sha3_256"


@pytest.mark.parametrize("missing", [
    "ARGUS_LIQPAY_PUBLIC_KEY", "ARGUS_LIQPAY_PRIVATE_KEY", "ARGUS_SERVER_URL",
])
def test_without_a_key_or_an_address_there_is_no_till_and_the_log_says_why(env, caplog, missing):
    """Not a crash: accounts and relays work without a till. But not a till that would accept a
    payment and be unable to credit it either: without a public address LiqPay has nowhere to
    send the notice."""
    values = {"ARGUS_BILLING": "liqpay", "ARGUS_LIQPAY_PUBLIC_KEY": PUBLIC,
              "ARGUS_LIQPAY_PRIVATE_KEY": PRIVATE, "ARGUS_SERVER_URL": BASE}
    del values[missing]
    env(**values)
    assert billing.from_env().name == "off"
    assert missing in caplog.text


def test_an_unknown_hash_in_the_environment_means_no_till(env, caplog):
    env(ARGUS_BILLING="liqpay", ARGUS_LIQPAY_PUBLIC_KEY=PUBLIC,
        ARGUS_LIQPAY_PRIVATE_KEY=PRIVATE, ARGUS_SERVER_URL=BASE, ARGUS_LIQPAY_HASH="md5")
    assert billing.from_env().name == "off"
    assert "ARGUS_LIQPAY_HASH" in caplog.text


def test_the_stub_still_works_as_before_on_a_local_address(env):
    env(ARGUS_BILLING="stub", ARGUS_ALLOW_STUB="1", ARGUS_SERVER_URL="http://127.0.0.1:8421")
    till = billing.from_env()
    assert till.name == "stub"
    assert till.checkout(7, 500, "a@b.c") == "http://127.0.0.1:8421/billing/stub/7"


def test_the_stub_is_refused_on_a_public_address(env):
    env(ARGUS_BILLING="stub", ARGUS_ALLOW_STUB="1", ARGUS_SERVER_URL=BASE)
    assert billing.from_env().name == "off"
