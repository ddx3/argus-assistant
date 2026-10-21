"""Payment intake: topping up the balance. A provider is plugged in as one class.

A person pays any amount from `pricing.MIN_TOPUP` dollars, and all of it lands on the
balance one to one. The markup is taken not here but when usage is charged
(`pricing.MARKUP`): a top-up is just money, and money must not change the price depending
on how it was deposited.

The payment provider is plata by mono (monobank acquiring), enabled with
`ARGUS_BILLING=mono`. Until 2026-10-16 it was LiqPay (`ARGUS_BILLING=liqpay`), but it
refused activation, and the class stayed in the code. The order, the provider's answer,
crediting the money, and protection against a repeated webhook do not depend on the
provider. Exactly two things change: where to send the person to pay, and how to check
that what came back is really a payment. Both are collected in one class per provider.

## The order is created BEFORE going to the payment provider

Otherwise a returning payment has nothing to be matched with: we have the money and no
answer to the question of whom to credit it to. Sorting that out afterwards means going
through the bank correspondence by hand, and it is better not to.

## The webhook arrives several times

Any provider repeats a notification until it gets a clear answer: the network blinked, our
server restarted. So crediting goes through `external_id` with a unique index: the same
payment cannot go through a second time, and the money is not doubled. This is not
overcautiousness but an ordinary Tuesday.

## The stub

`Stub` is not "not done yet" but a working tool: it lets you go through the whole top-up
path on your own machine without setting up a legal entity. It is enabled **only** by the
explicit variable `ARGUS_BILLING=stub` **and** a second one, `ARGUS_ALLOW_STUB=1`, with an
empty or local `ARGUS_SERVER_URL`. The stub page is open to everyone and puts money in
without payment, so one line forgotten in the production `.env` is not enough
(`from_env`). The default is `Off`, in which the payment button is not shown at all,
because a button that leads nowhere is worse than no button.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from argus.server import entitlements, pricing

log = logging.getLogger(__name__)

# The `orders.plan` column is left over from subscriptions; for a top-up it is always the
# same.
TOPUP = "topup"


class BillingError(Exception):
    """A refusal from the payment provider. The text is shown to the person."""


@dataclass(frozen=True)
class Order:
    id: int
    account_id: int
    amount: int          # cents
    state: str
    pay_url: str = ""

    @property
    def price(self) -> str:
        return f"{pricing.SYMBOL}{self.amount // 100}"

    def as_json(self) -> dict:
        return {
            "order": self.id,
            "amount": self.amount,
            "price": self.price,
            "state": self.state,
            "pay_url": self.pay_url,
        }


class Provider(Protocol):
    """The payment provider. Everything we want from it."""

    name: str

    def checkout(self, order_id: int, amount: int, email: str, lang: str = "") -> str:
        """Where to send the person to pay. `amount` is cents, `lang` is the app language ("ru", "de"):
        the provider's payment page is not required to support it, but a page that supports only two
        languages must be told which one to show."""
        ...


class Off:
    """No payment provider. The only honest behavior is to say so out loud."""

    name = "off"

    def checkout(self, order_id: int, amount: int, email: str, lang: str = "") -> str:
        raise BillingError("payment isn't connected yet — write to us and we'll top up your balance by hand")


class Stub:
    """Stub: the payment is confirmed by a click on our own page.

    It is needed to go through the whole path before a provider is chosen: order, redirect,
    return, crediting, repeated webhook. Without it, this half of the code would run for the
    first time in production.
    """

    name = "stub"

    def __init__(self, base: str = "") -> None:
        self.base = base.rstrip("/")

    def checkout(self, order_id: int, amount: int, email: str, lang: str = "") -> str:
        return f"{self.base}/billing/stub/{order_id}"


def start(db: sqlite3.Connection, provider: Provider, account_id: int,
          dollars: int, email: str = "", now: float | None = None,
          lang: str = "") -> Order:
    """Create an order for `dollars` dollars and get the payment address."""
    now = time.time() if now is None else now
    # In Python, `bool` is an `int`, and "top up by True dollar" is not an amount.
    if isinstance(dollars, bool) or not isinstance(dollars, int):
        raise BillingError("the amount must be a whole number of dollars")
    if not pricing.MIN_TOPUP <= dollars <= pricing.MAX_TOPUP:
        raise BillingError(
            f"top-up from {pricing.SYMBOL}{pricing.MIN_TOPUP} "
            f"to {pricing.SYMBOL}{pricing.MAX_TOPUP}"
        )
    cents = dollars * 100

    cur = db.execute(
        "INSERT INTO orders (account_id, plan, amount, currency, provider, "
        "state, created_at) VALUES (?, ?, ?, ?, ?, 'new', ?)",
        (account_id, TOPUP, cents, pricing.CURRENCY, provider.name, now),
    )
    order_id = int(cur.lastrowid)
    db.commit()

    try:
        url = provider.checkout(order_id, cents, email, lang)
    except BillingError:
        db.execute("UPDATE orders SET state = 'failed' WHERE id = ?", (order_id,))
        db.commit()
        raise
    return Order(order_id, account_id, cents, "new", url)


def paid(db: sqlite3.Connection, order_id: int, external_id: str,
         now: float | None = None) -> bool:
    """Payment confirmed: credit the money. `False` means it was already credited.

    We return `False` rather than raise: a repeated webhook is not an error, and answering it
    with a 500 would make the provider keep sending it for another day. It needs "accepted",
    and it will get it.
    """
    now = time.time() if now is None else now
    row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise BillingError("no such order")
    if row["state"] in ("paid", "reversed"):
        # `reversed`: the money was already returned to the person. A repeat of the success
        # notification must not credit it a second time, just like a repeat of any other.
        return False
    if row["plan"] != TOPUP:
        # An order from the subscription era that was not paid back then. Money for it, if it
        # arrives, is money for something that no longer exists: it cannot be credited at the plan's
        # price, and it cannot be swallowed silently either.
        raise BillingError(f"order '{row['plan']}' is from the old price list — sort it out by hand")

    try:
        db.execute(
            "UPDATE orders SET state = 'paid', external_id = ?, paid_at = ? WHERE id = ?",
            (external_id or f"order-{order_id}", now, order_id),
        )
    except sqlite3.IntegrityError:
        # The same payment under a different order: the unique index did not allow it.
        db.rollback()
        return False

    # One transaction with the "paid" mark (a shared connection, one commit in `credit`): either
    # the order is paid and the money is there, or neither.
    entitlements.credit(
        db, int(row["account_id"]), int(row["amount"]) * pricing.CENT,
        "topup", note=f"order {order_id}", now=now,
    )
    log.info("balance topped up: account %s, %s", row["account_id"],
             pricing.money(int(row["amount"]) * pricing.CENT))
    return True


class LiqPay:
    """LiqPay: Visa and Mastercard cards, Apple Pay, Google Pay.

    **Payment path.** `checkout` returns the address of *our* page `/billing/liqpay/<order>`,
    not LiqPay's: it builds the form and submits it to them itself. Their documentation
    allows calling their page only this way, with a POST. The key and the signature stay
    with us; they are not in the address the person sees. The person pays on the LiqPay
    page, and we learn about the payment from the **notification** to `server_url`
    (`liqpay_callback`). The person's return to `result_url` credits nothing: that page can
    be reached by address without paying.

    **Currency and language.** The page shows dollars, not the local currency, so a person
    with a foreign card does not have to guess the rate. LiqPay's page comes only in `uk`
    and English: the `uk` interface gets the first, all others (including `ru`) get the
    second.

    **Signature** is `base64(hash(private + data + private))`. The LiqPay documentation text
    says SHA3-256, while all their code examples and the official SDKs use SHA1, and what
    the server really accepts can be seen only on the sandbox. So we accept notifications
    signed with either one (neither can be forged without the secret key), and we sign our
    own requests with the one from the SDK. `ARGUS_LIQPAY_HASH=sha3_256` switches it if
    LiqPay answers "invalid signature" to it.

    **Sandbox.** Keys from the dashboard's "test" section start with `sandbox_`; a payment
    made with them is a test and arrives with the status `sandbox`. That status counts as a
    payment *only* in the sandbox: on production keys nobody accepts it, otherwise a test
    payment would credit real money. Like the stub, the sandbox in production gives access
    away for free, so the log shouts about it at startup.

    We do not give LiqPay the person's email: it does not need it, and in the privacy policy
    it would be one more item.
    """

    name = "liqpay"
    CHECKOUT = "https://www.liqpay.ua/api/3/checkout"
    # Their order number is a string. With a prefix, the LiqPay dashboard shows at once whose it
    # is, and a foreign number in a notification cannot be confused with ours.
    PREFIX = "argus-"
    DIGESTS = ("sha1", "sha3_256")

    def __init__(self, public_key: str, private_key: str, base: str,
                 sandbox: bool = False, digest: str = "sha1",
                 sandbox_accounts: frozenset[int] | None = None) -> None:
        if digest not in self.DIGESTS:
            raise ValueError(f"signature {digest!r}: only {', '.join(self.DIGESTS)} are supported")
        self.public_key = public_key
        self._private = private_key
        # Addresses that LiqPay calls itself must be complete: it will not understand a relative
        # path, and for it `localhost` is not our server.
        self.base = base.rstrip("/")
        self.sandbox = sandbox
        self.digest = digest
        # Whose test payments the sandbox may credit. `None` means any (development and tests). On a
        # public server (`_liqpay_from_env`) it is only the accounts from the owner's list, usually
        # empty: anyone can pay with the LiqPay test card, and without this list anyone who
        # downloaded the app could top up their balance by $500 at a time and spend our keys (audit
        # 2026-10-16).
        self.sandbox_accounts = sandbox_accounts

    # -- signature ---------------------------------------------------------

    def _sign(self, digest: str, data: str) -> str:
        raw = (self._private + data + self._private).encode("utf-8")
        return base64.b64encode(hashlib.new(digest, raw).digest()).decode("ascii")

    def sign(self, data: str) -> str:
        """The signature of our request."""
        return self._sign(self.digest, data)

    def genuine(self, data: str, signature: str) -> bool:
        """Is the signature of the incoming notification ours? The comparison takes constant time."""
        got = signature.encode("utf-8")
        return any(
            hmac.compare_digest(self._sign(digest, data).encode("ascii"), got)
            for digest in self.DIGESTS
        )

    # -- what goes to LiqPay -------------------------------------------------

    def params(self, order_id: int, amount: int, lang: str = "") -> dict:
        """Payment parameters. `amount` is cents, `lang` is the app language."""
        dollars = amount // 100 if amount % 100 == 0 else amount / 100
        params = {
            "version": 3,
            "public_key": self.public_key,
            "action": "pay",
            "amount": dollars,
            "currency": pricing.CURRENCY,
            "description": f"Argus balance top-up {pricing.SYMBOL}{dollars}",
            "order_id": f"{self.PREFIX}{order_id}",
            # LiqPay's page exists only in `uk` and English. The `uk` interface gets the first, all the
            # others get the second, `ru` included: the `uk` page for the `ru` interface was rejected
            # (2026-10-07).
            "language": "uk" if lang == "uk" else "en",
            "result_url": f"{self.base}/billing/done",
            "server_url": f"{self.base}/billing/liqpay/callback",
        }
        if self.sandbox:
            params["sandbox"] = 1
        return params

    def pack(self, body: dict) -> dict[str, str]:
        """The request body into the fields `data` and `signature`. The LiqPay notification is built the
        same way; the demo provider that plays its role assembles it in the same manner."""
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
        data = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return {"data": data, "signature": self.sign(data)}

    def form(self, order_id: int, amount: int, lang: str = "") -> dict[str, str]:
        """Form fields for LiqPay: where to send, `data` and `signature`."""
        return {"action": self.CHECKOUT, **self.pack(self.params(order_id, amount, lang))}

    def checkout(self, order_id: int, amount: int, email: str, lang: str = "") -> str:
        code = lang.strip().lower()[:2]
        query = f"?lang={code}" if code.isascii() and code.isalpha() else ""
        return f"{self.base}/billing/liqpay/{order_id}{query}"

    # -- what came from LiqPay ------------------------------------------------

    def read(self, data: str, signature: str) -> dict:
        """Parse a notification. `BillingError` means it is not from LiqPay."""
        # The signature is computed over the `data` string exactly as it arrived, before any
        # parsing: any "fix it up and parse it" step before the check is a loophole.
        if not self.genuine(data, signature):
            raise BillingError("the signature doesn't match")
        try:
            body = json.loads(base64.b64decode(data, validate=True))
        except (ValueError, RecursionError):      # both invalid base64 and invalid JSON
            raise BillingError("the data cannot be parsed") from None
        if not isinstance(body, dict):
            raise BillingError("the data cannot be parsed")
        return body

    def order_of(self, body: dict) -> int | None:
        """Our order number from the notification. `None` means the order is not ours."""
        raw = str(body.get("order_id", ""))
        tail = raw[len(self.PREFIX):]
        if not raw.startswith(self.PREFIX) or not (tail.isascii() and tail.isdigit()):
            return None
        return int(tail)

    @property
    def paying(self) -> frozenset[str]:
        """Statuses in which the person has paid (in the sandbox, the test one too).

        `success` and `wait_compensation`: LiqPay describes the second as "payment successful,
        will be credited in the daily settlement". The card is charged, but the money reaches our
        account tomorrow. Not accepting it would mean failing to credit a real payment.
        We do not accept `wait_accept`: the person is charged, but the company has not passed
        verification, and the payment may be reversed.
        """
        base = {"success", "wait_compensation"}
        return frozenset(base | {"sandbox"}) if self.sandbox else frozenset(base)


def _same_money(body: dict, cents: int, currency: str) -> bool:
    """Did exactly the amount, in the currency of the order, arrive?

    Without this, paying for a $5 order would credit a $500 order. The amount in the
    notification cannot be forged, but the order it is attached to is a matter of our own
    code, and the check must be against what we have recorded.
    """
    try:
        got = Decimal(str(body.get("amount")))
        same_sum = got * 100 == cents
    except InvalidOperation:
        return False
    return same_sum and str(body.get("currency", "")).upper() == currency.upper()


def _mark_reversed(db: sqlite3.Connection, provider: Provider, order_id: int,
                   now: float | None) -> None:
    """The order was reversed by the payment provider (a refund or a card dispute): mark it and
    take the money off the balance.

    Previously the balance was left alone "on purpose". That meant a stolen card could top up
    $500, spend it, wait for the dispute, and we would be left with neither the money nor a
    trace (audit 2026-10-16). Now the reversed amount is taken off. If the person has already
    spent it, the balance goes negative and paid calls are closed until they top up
    (`entitlements.reclaim`).

    A reversal that arrives before the payment (provider notifications arrive out of order)
    also closes the order: a late "success" will not credit it (`paid` skips reversed
    orders).
    """
    now = time.time() if now is None else now
    row = db.execute(
        "SELECT account_id, amount, state FROM orders WHERE id = ? AND provider = ?",
        (order_id, provider.name),
    ).fetchone()
    if row is None or row["state"] not in ("paid", "new"):
        return
    was_paid = row["state"] == "paid"
    db.execute(
        "UPDATE orders SET state = 'reversed', reversed_at = ? WHERE id = ? AND state = ?",
        (now, order_id, row["state"]),
    )
    if not was_paid:
        db.commit()
        log.warning("%s: refund for order %s, which is not yet marked as paid — closed without "
                    "crediting", provider.name, order_id)
        return
    micro = int(row["amount"]) * pricing.CENT
    left = entitlements.reclaim(db, int(row["account_id"]), micro, "reversal",
                                note=f"refund for order {order_id}", now=now)
    log.error("%s: refunded by the payment processor, order: %s, account: %s, deducted: %s, balance now: %s",
              provider.name, order_id, row["account_id"], pricing.money(micro), pricing.money(left))


def liqpay_callback(db: sqlite3.Connection, provider: LiqPay, data: str,
                    signature: str, now: float | None = None) -> str:
    """LiqPay notification: verify it and, if the payment is real, credit it.

    `BillingError` is raised only when the notification is not from LiqPay (the signature
    does not match, or the data cannot be parsed): that gets a refusal. Everything else (an
    order that is not ours, the wrong amount, an unfinished payment) gets the answer
    "accepted", like a repeat does. The same notification tomorrow would give the same
    result, and a 500 would make LiqPay send it again and again. Each such case leaves a
    line in the log, and the money waits for a manual review instead of being lost.

    Returns one word saying what happened: `paid` (credited), `again` (already credited),
    `waiting` (the payment is unfinished or failed), `refund` (the money was returned to the
    person, so there is nothing to credit and it needs a manual review), `rejected` (not our
    order, or the wrong amount).
    """
    body = provider.read(data, signature)
    status = str(body.get("status", ""))
    order_id = provider.order_of(body)
    if order_id is None:
        log.warning("LiqPay: notification is not about our order (order_id=%r)", body.get("order_id"))
        return "rejected"

    key = body.get("public_key")
    if key is not None and key != provider.public_key:
        log.error("LiqPay: order arrived under a foreign key: %s", order_id)
        return "rejected"

    if status == "reversed":
        # The balance may already have gone to answers, and how much to take back is a person's
        # decision. But the trace stays not only in the log (which rotates): the order gets a
        # "reversed" mark, and `SELECT * FROM orders WHERE state = 'reversed'` is the list of what
        # to review.
        _mark_reversed(db, provider, order_id, now)
        log.warning("LiqPay: refund for order %s — handle manually", order_id)
        return "refund"
    if status not in provider.paying:
        if status == "wait_accept":
            # The person has been charged, but the company is not yet activated: the payment may be
            # reversed, so we do not give out balance against it. Seeing this in production means the
            # keys are production keys and activation is not finished.
            log.warning("LiqPay: wait_accept status for order %s — the company did not pass "
                        "verification, not crediting", order_id)
        else:
            log.info("LiqPay: order %s, status %s — this is not a payment", order_id, status)
        return "waiting"

    row = db.execute("SELECT amount, currency, provider, account_id FROM orders WHERE id = ?",
                     (order_id,)).fetchone()
    if row is None or row["provider"] != provider.name:
        log.error("LiqPay: payment for order %s, but we have no such order", order_id)
        return "rejected"
    if (status == "sandbox" and provider.sandbox_accounts is not None
            and int(row["account_id"]) not in provider.sandbox_accounts):
        # The LiqPay test card is open to everyone: on a public server its payment is credited only
        # to accounts from the owner's list (`ARGUS_SANDBOX_ACCOUNTS`), and to the others nothing.
        log.warning("LiqPay: test payment not credited, order: %s, account: %s — the account is not "
                    "in ARGUS_SANDBOX_ACCOUNTS", order_id, row["account_id"])
        return "waiting"
    if not _same_money(body, int(row["amount"]), row["currency"]):
        log.error("LiqPay: order: %s, amount in cents: %s, currency: %s, but received: %s %s — not crediting",
                  order_id, row["amount"], row["currency"],
                  body.get("amount"), body.get("currency"))
        return "rejected"

    payment = body.get("payment_id")
    external = f"liqpay-{payment}" if payment else f"liqpay-order-{order_id}"
    try:
        fresh = paid(db, order_id, external, now)
    except BillingError as e:
        log.error("LiqPay: order paid but not credited (%s): %s", order_id, e)
        return "rejected"
    return "paid" if fresh else "again"


class Mono:
    """plata by mono (monobank acquiring): Visa and Mastercard cards, Apple Pay, Google Pay, the
    monobank app.

    **Payment path.** `checkout` returns the address of *our* page `/billing/mono/<order>`.
    On first visit that page creates an invoice at mono (`create_invoice`) and redirects the
    person to their payment page. The token key stays with us. mono picks the payment page
    language itself from the browser's first language: `uk` and `ru` get the `uk` page,
    everything else gets the English one (checked against the script of their page on
    2026-10-16), so there is no language parameter here.

    **Money only on the provider's own answer.** We do *not* take the notification
    (`/billing/mono/callback`) on trust, and we do not parse the ECDSA signature. It only
    hints which invoice to ask about, and we ask mono ourselves (`status`, our token) and
    credit based on its answer (`mono_settle`). That answer cannot be forged, and a forged
    notification gets nothing except one extra question to the provider, and the number of
    such questions is limited. If a notification is lost, the return page picks the order up
    (`/billing/mono/back/<order>`).

    **Currency.** By default the invoice is issued in dollars (`ccy` 840). Requirement: the
    buyer must see only dollars (2026-10-16). The mono documentation does not say whether
    payment in $ is enabled for our merchant. If it is not, set `ARGUS_MONO_CCY=980` and
    `ARGUS_MONO_UAH_PER_USD=<rate>`: the invoice is in the local currency at that rate, and
    the balance is still in dollars.

    **Test token.** A token from `api.monobank.ua` works in a sandbox: any card goes through,
    and there is no money. Left in production, it would give the balance away to everyone (as
    happened with the LiqPay sandbox, audit 2026-10-16). So such a merchant is detected
    (`details`: the same EDRPOU as in the documentation) or declared explicitly with
    `ARGUS_MONO_SANDBOX=1`, and on a public server its payments are credited only to accounts
    from `ARGUS_SANDBOX_ACCOUNTS`.

    We do not give mono the person's email: it is needed only for fiscal receipts, and we did
    not connect those.
    """

    name = "mono"
    API = "https://api.monobank.ua"
    PREFIX = "argus-"
    VALIDITY_S = 3600           # an invoice lives for an hour: people rarely think longer, and fewer invoices are left hanging
    TEST_EDRPOU = "4242424242"  # EDRPOU of the test merchant from the mono documentation

    def __init__(self, token: str, base: str, ccy: int = 840, uah_per_usd: Decimal | str | None = None,
                 sandbox: bool = False, sandbox_accounts: frozenset[int] | None = None,
                 http: httpx.Client | None = None) -> None:
        if ccy not in (840, 980):
            raise ValueError("invoice currency: 840 (dollars) or 980 (hryvnia)")
        if ccy == 980 and not uah_per_usd:
            raise ValueError("an invoice in hryvnia needs an exchange rate (ARGUS_MONO_UAH_PER_USD)")
        if not token.isascii() or not token.strip():
            raise ValueError("the provider token must be Latin letters and digits (it goes in a request header)")
        self._token = token
        self.base = base.rstrip("/")
        self.ccy = ccy
        self.uah_per_usd = Decimal(str(uah_per_usd)) if uah_per_usd else None
        self.sandbox = sandbox
        self.sandbox_accounts = sandbox_accounts
        self.http = http or httpx.Client(timeout=15.0)

    # -- requests to mono ---------------------------------------------------------

    def _call(self, method: str, path: str, **kw) -> dict:
        """A request to the mono API. Any refusal is a `BillingError` with human-readable text; the
        details go to the log.
        """
        try:
            r = self.http.request(method, self.API + path,
                                  headers={"X-Token": self._token, "X-Cms": "argus"}, **kw)
        except httpx.HTTPError as e:
            log.error("mono: %s %s is not responding: %s", method, path, type(e).__name__)
            raise BillingError("the payment provider isn't responding — try again in a minute") from None
        if r.status_code != 200:
            log.error("mono: %s %s → %s %s", method, path, r.status_code, r.text[:300])
            if r.status_code == 429:
                raise BillingError("the payment provider asks to retry in a minute")
            raise BillingError("the payment provider didn't accept the order — write to us")
        try:
            data = r.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def invoice_amount(self, cents: int) -> tuple[int, int]:
        """(currency code, amount in its minor units) for an order of `cents` cents."""
        if self.ccy == 840:
            return 840, cents
        assert self.uah_per_usd is not None
        kopecks = (Decimal(cents) * self.uah_per_usd).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return 980, int(kopecks)

    def create_invoice(self, order_id: int, cents: int) -> tuple[str, str]:
        """Create an invoice in mono: (invoice number, address of its payment page)."""
        ccy, amount = self.invoice_amount(cents)
        dollars = cents // 100 if cents % 100 == 0 else cents / 100
        out = self._call("POST", "/api/merchant/invoice/create", json={
            "amount": amount,
            "ccy": ccy,
            "merchantPaymInfo": {
                "reference": f"{self.PREFIX}{order_id}",
                "destination": f"Argus balance top-up {pricing.SYMBOL}{dollars}",
            },
            "redirectUrl": f"{self.base}/billing/mono/back/{order_id}",
            "webHookUrl": f"{self.base}/billing/mono/callback",
            "validity": self.VALIDITY_S,
            "paymentType": "debit",
        })
        invoice, url = out.get("invoiceId"), out.get("pageUrl")
        if not (isinstance(invoice, str) and invoice and isinstance(url, str) and url.startswith("https://")):
            log.error("mono: invoice creation response without a number or address: %r", out)
            raise BillingError("the payment provider didn't accept the order — write to us")
        return invoice, url

    def remove_invoice(self, invoice_id: str) -> None:
        """Revoke an unpaid invoice (a new one replaces it). If that fails, no harm: the invoice expires
        by itself.
        """
        try:
            self._call("POST", "/api/merchant/invoice/remove", json={"invoiceId": invoice_id})
        except BillingError:
            pass

    def status(self, invoice_id: str) -> dict:
        """What mono *itself* says about the invoice. The only source of truth for crediting."""
        out = self._call("GET", "/api/merchant/invoice/status", params={"invoiceId": invoice_id})
        if out.get("invoiceId") != invoice_id:
            raise BillingError("the payment provider answered about a different invoice")
        return out

    def details(self) -> dict:
        """The merchant data of the token (needed to recognize a test token). No answer: empty."""
        try:
            return self._call("GET", "/api/merchant/details")
        except BillingError:
            return {}

    # -- Provider ---------------------------------------------------------------

    def checkout(self, order_id: int, amount: int, email: str, lang: str = "") -> str:
        return f"{self.base}/billing/mono/{order_id}"

    # -- what the payment provider says ------------------------------------------------------

    def order_of(self, body: dict) -> int | None:
        """Our order number from `reference`. `None` means the invoice is not ours."""
        raw = str(body.get("reference", ""))
        tail = raw[len(self.PREFIX):]
        if not raw.startswith(self.PREFIX) or not (tail.isascii() and tail.isdigit()):
            return None
        return int(tail)

    # A plausible exchange rate of the local currency to the dollar. It is needed to check the
    # dollar amount of a *paid* invoice, which mono reports in the local currency (see
    # `same_money`). It is wider than any real rate and narrower than an error of an order of
    # magnitude.
    UAH_PER_USD_BAND = (20, 100)

    def same_money(self, body: dict, cents: int) -> bool:
        """Is the paid invoice for the sum we issued for this order?

        **Verified on the mono sandbox on 2026-10-17:** an invoice issued in dollars (`ccy` 840,
        500 cents), after payment, answers in `invoice/status` already in the local currency:
        `amount` 22350, `ccy` 980 (at mono's rate at the moment of payment), and the dollars
        disappear from the answer. So a dollar invoice is checked like this: an answer in dollars must
        be the exact sum; an answer in the local currency must be within a reasonable band around the
        rate (`UAH_PER_USD_BAND`). The exact sum is guaranteed by the fact that we created the
        invoice ourselves, for exactly this sum and under this order's `reference`: there is no
        foreign invoice in our account, and the payer cannot change the invoice sum. An invoice in the
        local currency (`ccy` 980 in the settings) is checked down to the last minor unit."""
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int):
            return False
        want_ccy, want = self.invoice_amount(cents)
        if body.get("ccy") == want_ccy:
            return amount == want
        if self.ccy == 840 and body.get("ccy") == 980:
            low, high = self.UAH_PER_USD_BAND
            return cents * low <= amount <= cents * high
        return False


def mono_invoice_ok(invoice_id: object) -> bool:
    """The invoice number looks like a mono invoice number (`p2_9ZgpZVsl3`): letters, digits, `_`,
    `-`.
    """
    return (isinstance(invoice_id, str) and 0 < len(invoice_id) <= 64
            and invoice_id.isascii() and all(c.isalnum() or c in "_-" for c in invoice_id))


def mono_settle(db: sqlite3.Connection, provider: Mono, body: dict, now: float | None = None) -> str:
    """mono's answer about an invoice (`provider.status`, not the notification!) -> credit if the
    payment is real.

    Returns a word: `paid` (credited), `again` (already credited), `waiting` (not paid),
    `refund` (the money was returned), `rejected` (not our order or the wrong amount). It does
    not raise `BillingError`: anything that can go wrong here is a reason for a log line, not
    for an error to the payment provider.
    """
    invoice = str(body.get("invoiceId", ""))
    status = str(body.get("status", ""))
    order_id = provider.order_of(body)
    if order_id is None or not mono_invoice_ok(invoice):
        log.warning("mono: invoice not for our order: %r (reference=%r)", invoice, body.get("reference"))
        return "rejected"
    row = db.execute("SELECT amount, currency, provider, account_id, state FROM orders WHERE id = ?",
                     (order_id,)).fetchone()
    if row is None or row["provider"] != provider.name:
        log.error("mono: payment for order %s, but we have no such order", order_id)
        return "rejected"
    if status == "reversed":
        _mark_reversed(db, provider, order_id, now)
        log.warning("mono: refund for order %s — handle manually", order_id)
        return "refund"
    if status != "success":
        log.info("mono: order %s, status %s — this is not a payment", order_id, status)
        return "waiting"
    if provider.sandbox and provider.sandbox_accounts is not None \
            and int(row["account_id"]) not in provider.sandbox_accounts:
        log.warning("mono: test payment not credited, order: %s, account: %s — the account is not in "
                    "ARGUS_SANDBOX_ACCOUNTS", order_id, row["account_id"])
        return "waiting"
    if row["currency"].upper() != pricing.CURRENCY or not provider.same_money(body, int(row["amount"])):
        log.error("mono: order: %s, amount in cents: %s, currency: %s, but received: %s %s — not crediting",
                  order_id, row["amount"], row["currency"], body.get("amount"), body.get("ccy"))
        return "rejected"
    if provider.ccy == 840 and body.get("ccy") == 980:
        # For accounting: how much mono counted in the local currency for this dollar order (the
        # rate at the time of payment).
        log.info("mono: paid, order: %s, amount: %s; mono counts %.2f UAH (rate %.2f)", order_id,
                 pricing.money(int(row["amount"]) * pricing.CENT), int(body["amount"]) / 100,
                 int(body["amount"]) / int(row["amount"]))
    try:
        fresh = paid(db, order_id, f"mono-{invoice}", now)
    except BillingError as e:
        log.error("mono: order paid but not credited (%s): %s", order_id, e)
        return "rejected"
    if not fresh and row["state"] == "new":
        # The order is already closed although we did not know about it: either a second payment
        # against the same order (the person paid two invoices).
        log.error("mono: order paid again, order: %s, new invoice: %s — refund the money manually", order_id, invoice)
    return "paid" if fresh else "again"


# We parse the LiqPay form (the notification, the redirect to our demo payment page) our
# own way: `python-multipart`, which FastAPI requires for `Form`, was not installed on the
# server, and pulling in a dependency for two fields is pointless. A real notification is a
# few hundred bytes; the limit exists so that megabytes cannot be sent to an address open
# to everyone.
MAX_FORM_BYTES = 64 * 1024


def form_fields(body: bytes) -> dict[str, str]:
    """Form (or JSON object) fields as a dict of strings. If it cannot be parsed: empty."""
    if body.lstrip().startswith(b"{"):
        try:
            raw = json.loads(body)
        except (ValueError, RecursionError):      # deep nesting is not a "ValueError"
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, str)}
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return {}
    return {k: v[0] for k, v in parsed.items() if v}


def _liqpay_from_env(root: str) -> Provider:
    """LiqPay from the environment. If something is missing, there is no payment provider rather
    than a crash: accounts and relays work without it, and the log says what is not set."""
    public = os.environ.get("ARGUS_LIQPAY_PUBLIC_KEY", "").strip()
    private = os.environ.get("ARGUS_LIQPAY_PRIVATE_KEY", "").strip()
    digest = os.environ.get("ARGUS_LIQPAY_HASH", "").strip().lower() or "sha1"

    missing = [name for name, value in (
        ("ARGUS_LIQPAY_PUBLIC_KEY", public),
        ("ARGUS_LIQPAY_PRIVATE_KEY", private),
        # Without a public address, LiqPay has nowhere to send notifications: the person would pay,
        # and the money would not reach the balance.
        ("ARGUS_SERVER_URL", root),
    ) if not value]
    if missing:
        log.error("ARGUS_BILLING=liqpay, but not set: %s — payment disabled",
                  ", ".join(missing))
        return Off()
    if digest not in LiqPay.DIGESTS:
        log.error("ARGUS_LIQPAY_HASH=%s: supported values are %s — payment disabled",
                  digest, ", ".join(LiqPay.DIGESTS))
        return Off()

    sandbox = (public.startswith("sandbox_")
               or os.environ.get("ARGUS_LIQPAY_SANDBOX", "").strip() == "1")
    allowed: frozenset[int] | None = None
    if sandbox:
        if _is_local_address(root):
            # Own machine (development, checking): a test payment is credited to anyone.
            log.warning("LIQPAY IN SANDBOX: payments are test ones, balance is topped up without money")
        else:
            # Public server: anyone can pay with the test card, and if such payments were credited to
            # everyone, anyone who downloaded the app could top up the balance by $500 at a time and
            # spend our keys. We credit only the owner's accounts (by default, nobody).
            allowed = _account_ids(os.environ.get("ARGUS_SANDBOX_ACCOUNTS", ""))
            log.warning("LIQPAY IN SANDBOX on a public server: test payments are credited only "
                        "to accounts %s (ARGUS_SANDBOX_ACCOUNTS)", sorted(allowed) or "— nobody")
    return LiqPay(public, private, root, sandbox=sandbox, digest=digest, sandbox_accounts=allowed)


def _mono_from_env(root: str) -> Provider:
    """plata by mono from the environment. If something is missing, there is no payment provider
    rather than a crash.
    """
    token = os.environ.get("ARGUS_MONO_TOKEN", "").strip()
    raw_ccy = os.environ.get("ARGUS_MONO_CCY", "").strip() or "840"
    rate = os.environ.get("ARGUS_MONO_UAH_PER_USD", "").strip()
    missing = [name for name, value in (("ARGUS_MONO_TOKEN", token),
                                        # Without a public address, mono has nowhere to send notifications and nowhere to return
                                        # the person.
                                        ("ARGUS_SERVER_URL", root)) if not value]
    if missing:
        log.error("ARGUS_BILLING=mono, but not set: %s — payment disabled", ", ".join(missing))
        return Off()
    try:
        ccy = int(raw_ccy)
        provider = Mono(token, root, ccy=ccy, uah_per_usd=rate or None)
    except (ValueError, InvalidOperation) as e:
        log.error("ARGUS_BILLING=mono: %s — payment disabled", e)
        return Off()
    sandbox = os.environ.get("ARGUS_MONO_SANDBOX", "").strip() == "1"
    if not sandbox and os.environ.get("ARGUS_MONO_PROBE", "1").strip() != "0":
        # A test token from `api.monobank.ua` in production would give the balance away to everyone,
        # so we recognize it by the merchant.
        sandbox = str(provider.details().get("edrpou", "")) == Mono.TEST_EDRPOU
    allowed: frozenset[int] | None = None
    if sandbox:
        provider.sandbox = True
        if _is_local_address(root):
            log.warning("MONO IN SANDBOX: payments are test ones, balance is topped up without money")
        else:
            allowed = _account_ids(os.environ.get("ARGUS_SANDBOX_ACCOUNTS", ""))
            provider.sandbox_accounts = allowed
            log.warning("MONO IN SANDBOX on a public server: test payments are credited only "
                        "to accounts %s (ARGUS_SANDBOX_ACCOUNTS)", sorted(allowed) or "— nobody")
    return provider


def _account_ids(raw: str) -> frozenset[int]:
    """"1, 2" -> {1, 2}. Anything that is not a number is skipped."""
    return frozenset(int(part) for part in raw.replace(";", ",").split(",")
                     if part.strip().isascii() and part.strip().isdigit())


def _is_local_address(url: str) -> bool:
    """The address is empty or points to this same machine."""
    if not url.strip():
        return True
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def from_env(base: str = "") -> Provider:
    """The payment provider from an environment variable. The default is "no payment provider"."""
    kind = os.environ.get("ARGUS_BILLING", "").strip().lower()
    root = base or os.environ.get("ARGUS_SERVER_URL", "")
    if kind == "stub":
        # The stub puts money on the balance at the press of a button, without real money, and its
        # page is open to everyone. If the line `ARGUS_BILLING=stub` stays in the production `.env`
        # (for example, in a saved "before the payment provider" copy kept for rollback), anyone
        # could top up their balance by $500 endlessly. So one variable is not enough: a second,
        # explicit one is needed, and the server address must be empty or our own (local).
        # Otherwise there is simply no payment provider.
        if (os.environ.get("ARGUS_ALLOW_STUB", "").strip() != "1"
                or not _is_local_address(root)):
            log.error("ARGUS_BILLING=stub rejected: the stub is allowed only by ARGUS_ALLOW_STUB=1 "
                      "with an empty or local ARGUS_SERVER_URL (currently %r) — payment disabled",
                      root)
            return Off()
        # Loud: left in production, it gives access away for free to anyone who wants it.
        log.warning("PAYMENT IN STUB MODE: balance is topped up without money")
        return Stub(root)
    if kind == "liqpay":
        return _liqpay_from_env(root)
    if kind == "mono":
        return _mono_from_env(root)
    return Off()
