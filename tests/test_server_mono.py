"""plata by mono: order, redirect to the payment page, notification, and the person's
return.

The payment provider is mocked (`httpx.MockTransport`): the test plays the part of mono.
It checks what breaks money: a notification that was not sent by mono; a payment of the
wrong amount or in another currency; a repeat; a lost notification; a test token in
production; questions to the provider that must not be allowed without limit.

The main rule held here: **money is credited only on mono's answer to our question about
the invoice**, not on what the notification says.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.server import billing, entitlements, mailtexts, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"
BASE = "https://pay.example.com"
TOKEN = "uSe5P-live-token-mono-123"


class FakeMono:
    """The mono till: remembers the invoices issued and answers questions about them. The test
    sets the invoice status.
    """

    def __init__(self) -> None:
        self.invoices: dict[str, dict] = {}
        self.removed: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.tokens: set[str] = set()
        self.fail_create = False
        self.fail_status = False
        self.details = {"merchantId": "12o4Vv7EWy", "merchantName": "Argus", "edrpou": "3805705351"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.tokens.add(request.headers.get("x-token", ""))
        if path == "/api/merchant/invoice/create":
            if self.fail_create:
                return httpx.Response(500, json={"errCode": "INTERNAL_ERROR"})
            body = json.loads(request.content)
            n = len(self.invoices) + 1
            invoice = f"p2_TEST{n}"
            self.invoices[invoice] = {
                "invoiceId": invoice, "status": "created", "amount": body["amount"], "ccy": body.get("ccy", 980),
                "reference": body["merchantPaymInfo"]["reference"], "request": body}
            return httpx.Response(200, json={"invoiceId": invoice, "pageUrl": f"https://pay.mbnk.biz/{invoice}"})
        if path == "/api/merchant/invoice/status":
            if self.fail_status:
                return httpx.Response(503, text="busy")
            invoice = request.url.params.get("invoiceId", "")
            if invoice not in self.invoices:
                return httpx.Response(404, json={"errCode": "NOT_FOUND"})
            return httpx.Response(200, json={k: v for k, v in self.invoices[invoice].items() if k != "request"})
        if path == "/api/merchant/invoice/remove":
            self.removed.append(json.loads(request.content)["invoiceId"])
            return httpx.Response(200, json={})
        if path == "/api/merchant/details":
            return httpx.Response(200, json=self.details)
        return httpx.Response(404, json={"errCode": "NOT_FOUND"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def set(self, invoice: str, **changes) -> None:
        self.invoices[invoice].update(changes)

    def count(self, path: str) -> int:
        return sum(1 for _, p in self.calls if p == path)


@pytest.fixture
def kassa():
    return FakeMono()


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


def make(kassa, **kw) -> billing.Mono:
    return billing.Mono(TOKEN, BASE, http=kassa.client(), **kw)


@pytest.fixture
def mail():
    class Outbox:
        name = "fake"

        def __init__(self) -> None:
            self.letters: list[dict] = []

        def send(self, to, subject, text, html=None, images=None):
            self.letters.append({"to": to, "subject": subject, "text": text})

    return Outbox()


@pytest.fixture
def client(db, kassa, mail):
    with TestClient(build(db, SECRET, pay=make(kassa), mail=mail, base_url=BASE), follow_redirects=False) as c:
        yield c


def register(client, email="user@example.com") -> dict:
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD, "terms": "t"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access']}"}


def order(client, headers, dollars: int = 5) -> dict:
    r = client.post("/billing/checkout", json={"dollars": dollars}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def open_payment(client, order_: dict):
    """The person pressed "pay": our page creates an invoice and sends them to the
    mono page.
    """
    r = client.get(f"/billing/mono/{order_['order']}")
    assert r.status_code == 302, r.text
    return r.headers["location"]


def notify(client, invoice: str, reference: str = "", status: str = "success", **extra):
    body = {"invoiceId": invoice, "status": status, "reference": reference, **extra}
    return client.post("/billing/mono/callback", content=json.dumps(body))


def balance(db, account_id: int = 1) -> int:
    return entitlements.balance(db, account_id)


# -- order and redirect -------------------------------------------------------------


def test_checkout_leads_to_our_page_not_to_mono_directly(client):
    headers = register(client)
    made = order(client, headers)
    assert made["pay_url"] == f"{BASE}/billing/mono/{made['order']}"


def test_the_payment_page_creates_an_invoice_in_dollars_and_sends_the_person_to_mono(client, kassa):
    headers = register(client)
    made = order(client, headers, 5)
    assert open_payment(client, made) == "https://pay.mbnk.biz/p2_TEST1"
    body = kassa.invoices["p2_TEST1"]["request"]
    assert body["amount"] == 500 and body["ccy"] == 840, "the invoice is in dollars, in cents"
    assert body["merchantPaymInfo"]["reference"] == f"argus-{made['order']}"
    assert body["redirectUrl"] == f"{BASE}/billing/mono/back/{made['order']}"
    assert body["webHookUrl"] == f"{BASE}/billing/mono/callback"
    assert body["paymentType"] == "debit" and 0 < body["validity"] <= 24 * 3600
    assert kassa.tokens == {TOKEN}, "requests go with our token"


def test_nothing_personal_goes_to_mono(client, kassa):
    headers = register(client)
    open_payment(client, order(client, headers))
    sent = json.dumps(kassa.invoices["p2_TEST1"]["request"])
    assert "user@example.com" not in sent and "customerEmails" not in sent


def test_opening_the_payment_page_again_replaces_the_old_unpaid_invoice(client, kassa):
    headers = register(client)
    made = order(client, headers)
    open_payment(client, made)
    assert open_payment(client, made) == "https://pay.mbnk.biz/p2_TEST2"
    assert kassa.removed == ["p2_TEST1"], "an order has one live invoice"


def test_a_paid_order_is_not_paid_twice(client, db, kassa):
    headers = register(client)
    made = order(client, headers)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success")
    notify(client, "p2_TEST1", f"argus-{made['order']}")
    page = client.get(f"/billing/mono/{made['order']}")
    assert page.status_code == 200 and "Back to Argus" in page.text
    assert kassa.count("/api/merchant/invoice/create") == 1


def test_a_failing_mono_gives_a_polite_page_and_charges_nothing(client, kassa, db):
    headers = register(client)
    made = order(client, headers)
    kassa.fail_create = True
    page = client.get(f"/billing/mono/{made['order']}")
    assert page.status_code == 502 and "Nothing was charged" in page.text
    assert "p2_" not in page.text and TOKEN not in page.text
    assert db.execute("SELECT external_id FROM orders").fetchone()["external_id"] is None


def test_other_providers_orders_have_no_mono_page(db, kassa, mail):
    with TestClient(build(db, SECRET, pay=billing.Stub(BASE), mail=mail, base_url=BASE), follow_redirects=False) as c:
        headers = register(c)
        made = order(c, headers)
        assert c.get(f"/billing/mono/{made['order']}").status_code == 404
        assert c.post("/billing/mono/callback", content=b"{}").status_code == 404


def test_an_unknown_order_has_no_page(client):
    register(client)
    assert client.get("/billing/mono/999").status_code == 404


# -- notification -------------------------------------------------------------------


def test_a_real_payment_credits_the_balance_once_and_sends_the_receipt(client, db, kassa, mail):
    headers = register(client)
    before = balance(db)
    made = order(client, headers, 5)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success")
    first = notify(client, "p2_TEST1", f"argus-{made['order']}")
    assert first.status_code == 200 and first.json()["result"] == "paid"
    assert balance(db) == before + 5 * 100 * pricing.CENT
    again = notify(client, "p2_TEST1", f"argus-{made['order']}")
    assert again.json()["result"] == "ignored" or again.json()["result"] == "again"
    assert balance(db) == before + 5 * 100 * pricing.CENT, "a repeated notice does not double the money"
    assert db.execute("SELECT state, external_id FROM orders").fetchone()["external_id"] == "mono-p2_TEST1"
    receipts = [m for m in mail.letters if m["subject"] == mailtexts.TEXTS["top_up"]["en"]["subject"]]
    assert len(receipts) == 1, "there is one top-up email, a repeated notice does not send a second"


def test_the_notice_alone_proves_nothing_mono_must_say_so(client, db, kassa):
    """A forged "success" notice, while mono says the invoice is not paid: no money."""
    headers = register(client)
    before = balance(db)
    made = order(client, headers)
    open_payment(client, made)
    reply = notify(client, "p2_TEST1", f"argus-{made['order']}", status="success", amount=500, ccy=840)
    assert reply.status_code == 200 and reply.json()["result"] == "waiting"
    assert balance(db) == before


def test_a_notice_about_an_invoice_we_never_made_does_not_even_reach_mono(client, db, kassa):
    register(client)
    before = len(kassa.calls)
    reply = notify(client, "p2_FORGED", "argus-1", status="success")
    assert reply.status_code == 200 and reply.json()["result"] == "ignored"
    assert len(kassa.calls) == before, "a forged invoice does not cost us a question to the till"


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b"null", b'"x"'])
def test_garbage_in_the_notice_is_refused(client, body):
    assert client.post("/billing/mono/callback", content=body).status_code == 400


def test_an_oversized_notice_is_refused(client):
    assert client.post("/billing/mono/callback", content=b"x" * (billing.MAX_FORM_BYTES + 1)).status_code == 413


def test_a_notice_with_a_hostile_invoice_number_is_ignored(client, kassa):
    register(client)
    before = len(kassa.calls)
    for bad in ("../../etc", "a b", "p2;DROP", "x" * 100, "", None, 5):
        assert notify(client, bad, "argus-1").json()["result"] == "ignored"
    assert len(kassa.calls) == before


def test_the_wrong_sum_is_not_credited(client, db, kassa):
    headers = register(client)
    before = balance(db)
    made = order(client, headers, 5)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success", amount=50)          # paid $0.50 instead of $5
    assert notify(client, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "rejected"
    assert balance(db) == before


def test_the_wrong_currency_is_not_credited(client, db, kassa):
    headers = register(client)
    before = balance(db)
    made = order(client, headers, 5)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success", ccy=980)            # 500 minor units of the local currency, not 500 cents
    assert notify(client, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "rejected"
    assert balance(db) == before


def test_a_paid_dollar_invoice_that_mono_reports_in_hryvnias_is_credited(client, db, kassa):
    """This is how the real mono sandbox answers (2026-10-17): an invoice for 500 cents, after
    payment, has `amount` 22350 and `ccy` 980.
    """
    headers = register(client)
    before = balance(db)
    made = order(client, headers, 5)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success", amount=22350, ccy=980)
    assert notify(client, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "paid"
    assert balance(db) == before + 5 * 100 * pricing.CENT, "the balance is in the order's dollars, not in the till's hryvnias"


@pytest.mark.parametrize("hryvnias", [500, 5000, 9999, 50001, 2_000_000])
def test_a_hryvnia_amount_far_from_any_real_rate_is_not_credited(client, db, kassa, hryvnias):
    headers = register(client)
    before = balance(db)
    made = order(client, headers, 5)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success", amount=hryvnias, ccy=980)
    assert notify(client, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "rejected"
    assert balance(db) == before


def test_a_boolean_or_text_amount_is_not_an_amount(client, db, kassa):
    headers = register(client)
    made = order(client, headers, 5)
    open_payment(client, made)
    for bad in (True, "500", None, 500.0):
        kassa.set("p2_TEST1", status="success", amount=bad, ccy=840)
        assert notify(client, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "rejected", bad


def test_an_invoice_for_another_order_is_not_credited_to_this_one(client, db, kassa):
    headers = register(client)
    a = order(client, headers, 5)
    b = order(client, headers, 5)
    open_payment(client, a)
    open_payment(client, b)
    kassa.set("p2_TEST1", status="success")
    notify(client, "p2_TEST1", f"argus-{b['order']}")           # the notification names order b, but the invoice belongs to order a
    states = {r["id"]: r["state"] for r in db.execute("SELECT id, state FROM orders")}
    assert states[a["order"]] == "paid" and states[b["order"]] == "new", "we trust the till's answer, not the notice"


def test_a_failing_status_question_asks_mono_to_repeat_the_notice(client, db, kassa):
    headers = register(client)
    made = order(client, headers)
    open_payment(client, made)
    kassa.fail_status = True
    reply = notify(client, "p2_TEST1", f"argus-{made['order']}")
    assert reply.status_code == 502, "not 200: otherwise mono will not repeat it and the payment is lost"


def test_the_number_of_questions_to_mono_is_capped(db, kassa, mail, monkeypatch):
    monkeypatch.setattr("argus.server.app.MONO_ASKS_PER_MINUTE", 3)
    with TestClient(build(db, SECRET, pay=make(kassa), mail=mail, base_url=BASE), follow_redirects=False) as c:
        headers = register(c)
        made = order(c, headers)
        open_payment(c, made)
        codes = [notify(c, "p2_TEST1", f"argus-{made['order']}").status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429, 429]


# -- the person's return ------------------------------------------------------------


def test_coming_back_credits_a_payment_whose_notice_was_lost(client, db, kassa):
    headers = register(client)
    before = balance(db)
    made = order(client, headers, 10)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success")
    page = client.get(f"/billing/mono/back/{made['order']}")
    assert page.status_code == 200 and "Back to Argus" in page.text
    assert balance(db) == before + 10 * 100 * pricing.CENT


def test_coming_back_without_paying_credits_nothing(client, db, kassa):
    headers = register(client)
    before = balance(db)
    made = order(client, headers)
    open_payment(client, made)
    client.get(f"/billing/mono/back/{made['order']}")
    assert balance(db) == before


def test_the_return_page_never_breaks_when_mono_is_down(client, kassa):
    headers = register(client)
    made = order(client, headers)
    open_payment(client, made)
    kassa.fail_status = True
    assert client.get(f"/billing/mono/back/{made['order']}").status_code == 200


# -- refund -------------------------------------------------------------------------


def test_a_refund_by_mono_takes_the_money_back_from_the_balance(client, db, kassa):
    headers = register(client)
    made = order(client, headers, 5)
    open_payment(client, made)
    kassa.set("p2_TEST1", status="success")
    notify(client, "p2_TEST1", f"argus-{made['order']}")
    paid_balance = balance(db)
    kassa.set("p2_TEST1", status="reversed")
    assert notify(client, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "refund"
    assert balance(db) == paid_balance - 5 * 100 * pricing.CENT
    assert db.execute("SELECT state FROM orders").fetchone()["state"] == "reversed"
    kassa.set("p2_TEST1", status="success")                      # a late "success" will not credit a second time
    notify(client, "p2_TEST1", f"argus-{made['order']}")
    assert balance(db) == paid_balance - 5 * 100 * pricing.CENT


# -- local-currency invoice (if dollars are not enabled) ----------------------------


def test_a_hryvnia_invoice_is_priced_at_the_given_rate_and_checked_against_it(db, kassa, mail):
    provider = make(kassa, ccy=980, uah_per_usd="44.8")
    with TestClient(build(db, SECRET, pay=provider, mail=mail, base_url=BASE), follow_redirects=False) as c:
        headers = register(c)
        before = balance(db)
        made = order(c, headers, 5)
        open_payment(c, made)
        body = kassa.invoices["p2_TEST1"]["request"]
        assert body["ccy"] == 980 and body["amount"] == 22400, "$5 × 44.8 = 224.00 UAH = 22400 kopecks"
        kassa.set("p2_TEST1", status="success", amount=22000)     # paid less
        assert notify(c, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "rejected"
        kassa.set("p2_TEST1", status="success", amount=22400)
        assert notify(c, "p2_TEST1", f"argus-{made['order']}").json()["result"] == "paid"
        assert balance(db) == before + 5 * 100 * pricing.CENT, "the balance stays in dollars"


def test_a_token_with_foreign_characters_is_refused_up_front(monkeypatch):
    """The token goes into a header, and a header is ASCII only: otherwise the very first payment
    would fail with a 500.
    """
    with pytest.raises(ValueError):
        billing.Mono("tökén", BASE)
    monkeypatch.setenv("ARGUS_BILLING", "mono")
    monkeypatch.setenv("ARGUS_MONO_PROBE", "0")
    monkeypatch.setenv("ARGUS_MONO_TOKEN", "tökén")
    assert billing.from_env(BASE).name == "off"


def test_hryvnia_mode_needs_a_rate():
    with pytest.raises(ValueError):
        billing.Mono(TOKEN, BASE, ccy=980)
    with pytest.raises(ValueError):
        billing.Mono(TOKEN, BASE, ccy=978)


# -- test token ---------------------------------------------------------------------


def test_sandbox_payments_on_a_public_server_credit_only_the_owners_accounts(db, kassa, mail):
    provider = make(kassa, sandbox=True, sandbox_accounts=frozenset({2}))
    with TestClient(build(db, SECRET, pay=provider, mail=mail, base_url=BASE), follow_redirects=False) as c:
        stranger = register(c, "a@example.com")          # account 1
        owner = register(c, "b@example.com")             # account 2
        for who, account in ((stranger, 1), (owner, 2)):
            made = order(c, who, 5)
            open_payment(c, made)
            invoice = f"p2_TEST{made['order']}"
            kassa.set(invoice, status="success")
            reply = notify(c, invoice, f"argus-{made['order']}")
            assert reply.json()["result"] == ("waiting" if account == 1 else "paid"), account


def test_from_env_builds_mono_and_never_half_builds(monkeypatch):
    monkeypatch.setenv("ARGUS_BILLING", "mono")
    monkeypatch.setenv("ARGUS_MONO_PROBE", "0")
    monkeypatch.delenv("ARGUS_MONO_TOKEN", raising=False)
    monkeypatch.delenv("ARGUS_MONO_CCY", raising=False)
    monkeypatch.delenv("ARGUS_MONO_UAH_PER_USD", raising=False)
    monkeypatch.delenv("ARGUS_MONO_SANDBOX", raising=False)
    assert billing.from_env(BASE).name == "off", "no token - no till"
    monkeypatch.setenv("ARGUS_MONO_TOKEN", TOKEN)
    assert billing.from_env("").name == "off", "no public address - no till"
    good = billing.from_env(BASE)
    assert good.name == "mono" and good.ccy == 840 and good.sandbox is False
    monkeypatch.setenv("ARGUS_MONO_CCY", "980")
    assert billing.from_env(BASE).name == "off", "hryvnias without a rate - no till"
    monkeypatch.setenv("ARGUS_MONO_UAH_PER_USD", "44.8")
    assert billing.from_env(BASE).ccy == 980
    monkeypatch.setenv("ARGUS_MONO_UAH_PER_USD", "not a number")
    assert billing.from_env(BASE).name == "off"
    monkeypatch.setenv("ARGUS_MONO_CCY", "978")
    assert billing.from_env(BASE).name == "off", "we do not handle euros"


def test_a_test_token_is_recognised_and_closed_to_strangers(monkeypatch):
    monkeypatch.setenv("ARGUS_BILLING", "mono")
    monkeypatch.setenv("ARGUS_MONO_TOKEN", TOKEN)
    monkeypatch.delenv("ARGUS_MONO_CCY", raising=False)
    monkeypatch.delenv("ARGUS_MONO_SANDBOX", raising=False)
    monkeypatch.setenv("ARGUS_SANDBOX_ACCOUNTS", "7")
    monkeypatch.setattr(billing.Mono, "details", lambda self: {"edrpou": billing.Mono.TEST_EDRPOU})
    provider = billing.from_env(BASE)
    assert provider.sandbox is True and provider.sandbox_accounts == frozenset({7})
    monkeypatch.setattr(billing.Mono, "details", lambda self: {"edrpou": "3805705351"})
    assert billing.from_env(BASE).sandbox is False


def test_the_explicit_sandbox_switch_works_without_asking_mono(monkeypatch):
    monkeypatch.setenv("ARGUS_BILLING", "mono")
    monkeypatch.setenv("ARGUS_MONO_TOKEN", TOKEN)
    monkeypatch.setenv("ARGUS_MONO_SANDBOX", "1")
    monkeypatch.delenv("ARGUS_SANDBOX_ACCOUNTS", raising=False)
    monkeypatch.setattr(billing.Mono, "details", lambda self: pytest.fail("the test mode is set explicitly: no need to ask"))
    provider = billing.from_env(BASE)
    assert provider.sandbox is True and provider.sandbox_accounts == frozenset()


def test_the_token_never_appears_in_logs_or_errors(client, kassa, caplog):
    headers = register(client)
    made = order(client, headers)
    kassa.fail_create = True
    with caplog.at_level("DEBUG"):
        page = client.get(f"/billing/mono/{made['order']}")
    assert TOKEN not in caplog.text and TOKEN not in page.text


def test_pricing_says_payment_is_on_without_naming_the_provider(client):
    shown = client.get("/pricing").json()
    assert shown["payment"] == "on"
