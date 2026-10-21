"""Server emails: what happened to an account -> which email goes to the person.

Here is only the wording and the choice of details; the design is shared
(`mailtemplate.render`), and sending is in `mailer.py`. Each event is one function that
returns `(address, Message)` or `None` (no account, nobody to write to).

The emails sent now. Requirement (2026-10-15): not too much text everywhere, but also not
a single bare line. The middle ground: a heading, a couple of clear phrases, and the
needed details; no frames and no lines.

| event            | function           | what is in the email                                    |
|------------------|--------------------|---------------------------------------------------------|
| recovery request | `reset_password`   | a link button, the lifetime, "not you? ignore it"       |
| registration     | `welcome`          | the address, the gift on the balance, where to start    |
| password changed | `password_changed` | when, signed out everywhere, "not you? write to us"     |
| balance top-up   | `top_up`           | the amount, the date, the balance now (no order number) |
| account deleted  | `deleted`          | what was erased and what is gone                        |

The language is the language of the person's app (nine, as in the app itself): the client
sends `lang` at registration, sign-in, and the recovery request, the server remembers it in
`accounts.lang` (`remember_lang`) and writes all emails in it, including those that go out
unprompted (balance, payment). With no language (an old app, a link not from the app) it is
`ARGUS_MAIL_LANG`, `en` by default. The words live in `mail_texts/<language>.json`
(`mailtexts.py`), not here. Nothing foreign gets into the emails: the address, the amount,
and the order number are taken from the database, not from the request.

The emails contain no passwords and no links to the website (the website on the domain is
closed, and a link from an email is bait for forgeries), and no words about how the app is
built (models, keys, providers), as everywhere in visible text.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from argus.server import emails, mailtemplate, mailtexts, tokens

log = logging.getLogger(__name__)

LANGS = mailtexts.LANGS
TEXTS = mailtexts.TEXTS            # TEXTS[event][language] = {subject, heading, paragraphs, ...}
Sent = tuple[str, mailtemplate.Message]


def default_lang() -> str:
    lang = mailtexts.normalize(os.environ.get("ARGUS_MAIL_LANG", ""))
    return lang or "en"


def usd(micro: int | float) -> str:
    """Money as in the app: `$0.30`, `$5.00`."""
    return f"${micro / 1_000_000:.2f}"


def when(ts: float, lang: str) -> str:
    """A moment in time is always in UTC and marked as such: the server does not know the person's
    time zone.
    """
    d = datetime.fromtimestamp(ts, timezone.utc)
    return mailtexts.date_text(lang, d.year, d.month, d.day, f"{d:%H:%M}")


def _lang(lang: str | None) -> str:
    return mailtexts.normalize(lang) or default_lang()


def remember_lang(db: sqlite3.Connection, email: str, lang: str | None) -> str:
    """Remember the person's language (it came from the app); returns the accepted language or
    `""`.

    Garbage and unknown languages are dropped, and the previous language is not erased: an
    empty `lang` (an old app, a third-party request) changes nothing.
    """
    code = mailtexts.normalize(lang)
    if code:
        try:
            key = emails.normalize(email)
        except emails.BadEmail:
            return ""
        db.execute("UPDATE accounts SET lang = ? WHERE email_key = ?", (code, key))
        db.commit()
    return code


def stored_lang(db: sqlite3.Connection, account_id: int) -> str:
    """The language the app reported last time; empty means it never reported one."""
    row = db.execute("SELECT lang FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return mailtexts.normalize(row["lang"]) if row else ""


def _compose(event: str, lang: str, *, link: str | None = None, rows: list[tuple[str, str]] | None = None,
             **values: str) -> mailtemplate.Message:
    """Compose the event email in language `lang`. A paragraph that is empty after substitution is
    dropped.
    """
    t = TEXTS[event][lang]
    paragraphs = [text for text in (p.format(**values) for p in t["paragraphs"]) if text.strip()]
    return mailtemplate.render(
        subject=t["subject"], heading=t["heading"], paragraphs=paragraphs,
        button=(t["button"], link) if link else None, rows=rows,
        note=t.get("note", "").format(**values), lang=lang, footer=mailtexts.footer(lang))


# -- addresses -----------------------------------------------------------------------------------


def address_of(db: sqlite3.Connection, account_id: int) -> str | None:
    row = db.execute("SELECT email FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return str(row["email"]) if row else None


def _address_by_key(db: sqlite3.Connection, email: str) -> str | None:
    try:
        key = emails.normalize(email)
    except emails.BadEmail:
        return None
    row = db.execute("SELECT email FROM accounts WHERE email_key = ?", (key,)).fetchone()
    return str(row["email"]) if row else None


def stored_lang_by_email(db: sqlite3.Connection, email: str) -> str:
    try:
        key = emails.normalize(email)
    except emails.BadEmail:
        return ""
    row = db.execute("SELECT lang FROM accounts WHERE email_key = ?", (key,)).fetchone()
    return mailtexts.normalize(row["lang"]) if row else ""


def account_of_reset(db: sqlite3.Connection, raw_token: str) -> int | None:
    """Whose account a recovery link belongs to (the row remains after the link is used)."""
    row = db.execute("SELECT account_id FROM password_resets WHERE token_hash = ?",
                     (tokens.hash_secret(raw_token),)).fetchone()
    return int(row["account_id"]) if row else None


# -- emails --------------------------------------------------------------------------------------


def reset_password(address: str, link: str, minutes: int, lang: str | None = None) -> Sent:
    """The link is single and one-time; whoever did not ask simply does not open it."""
    return address, _compose("reset", _lang(lang), link=link, minutes=str(minutes))


def welcome(db: sqlite3.Connection, email: str, lang: str | None = None) -> Sent | None:
    """After registration: what kind of account it is, how much was gifted, where to start."""
    address = _address_by_key(db, email)
    if address is None:
        return None
    lang = _lang(lang or stored_lang_by_email(db, email))
    row = db.execute(
        "SELECT COALESCE(SUM(c.micro), 0) AS gift FROM credits c JOIN accounts a ON a.id = c.account_id "
        "WHERE a.email_key = ? AND c.kind = 'gift'", (emails.normalize(email),)).fetchone()
    gift = int(row["gift"]) if row else 0
    # No gift means the phrase about it is empty, and the paragraph drops out.
    sentence = TEXTS["welcome"][lang]["gift"].format(amount=usd(gift)) if gift > 0 else ""
    return address, _compose("welcome", lang, email=address, gift=sentence)


def password_changed(db: sqlite3.Connection, account_id: int, now: float | None = None,
                     lang: str | None = None) -> Sent | None:
    """The password was changed (from settings or by a link): the person must learn about it, even
    if someone else did it.
    """
    address = address_of(db, account_id)
    if address is None:
        return None
    lang = _lang(lang or stored_lang(db, account_id))
    moment = when(time.time() if now is None else now, lang)
    return address, _compose("password_changed", lang, email=address, when=moment)


def top_up(db: sqlite3.Connection, order_id: int, lang: str | None = None) -> Sent | None:
    """The payment is credited: sum, date and balance come from the database, not from the payment
    provider's request.
    """
    order = db.execute("SELECT account_id, amount, currency, paid_at FROM orders WHERE id = ?",
                       (order_id,)).fetchone()
    if order is None or order["paid_at"] is None:
        return None
    account = db.execute("SELECT email, balance_micro FROM accounts WHERE id = ?",
                         (order["account_id"],)).fetchone()
    if account is None:
        return None
    lang = _lang(lang or stored_lang(db, int(order["account_id"])))
    currency = str(order["currency"] or "USD").upper()
    cents = int(order["amount"])
    amount = f"${cents / 100:.2f}" if currency == "USD" else f"{cents / 100:.2f} {currency}"
    # No order number in the email, like any internal numbers and counters.
    rows = list(zip(TEXTS["top_up"][lang]["rows"],
                    [amount, when(float(order["paid_at"]), lang), usd(int(account["balance_micro"]))]))
    return str(account["email"]), _compose("top_up", lang, rows=rows)


def deleted(address: str, lang: str | None = None) -> Sent:
    """The account is deleted. The address is taken BEFORE deletion: afterwards it is no longer in
    the database.
    """
    return address, _compose("deleted", _lang(lang), email=address)


# -- low balance ---------------------------------------------------------------------------------

LOW_MICRO = 1_000_000      # $1.00: below this, after a top-up, the balance is "running low"


@dataclass(frozen=True)
class Due:
    """A balance email that is due: to whom, which one, and which level to mark after sending."""

    account_id: int
    address: str
    level: int
    message: mailtemplate.Message


def balances_due(db: sqlite3.Connection, lang: str | None = None) -> list[Due]:
    """Who is due a message about their balance. It only reads the database (and resets the marks
    of those whose balance has grown).

    It works from the state of the database, not from charge events: usage goes through
    several paths (client reports, the relay, holds), and hanging an email on each is
    fragile; a server restart also loses nothing. An email about each level goes out once
    (`accounts.low_notified`) until the balance grows back to `LOW_MICRO` and above (a
    top-up). Then the mark is reset, and the next drop gives an email again.

    Levels: "ran out" (the balance is zero) goes to those who have spent anything at least
    once; "running low" (below $1) goes only to those who have already topped up: for someone
    with only the $0.30 gift, "running low" would be noise, and "ran out" is enough for them.
    Blocked accounts are not written to.

    The database is used only from the thread where the server works with it: the sqlite
    connection is shared, and a write from a neighboring thread would diverge from the
    requests' writes. So sending (long, over the network) is separate, and `mark_notified` is
    called after it, also from the server thread.
    """
    db.execute("UPDATE accounts SET low_notified = 0 WHERE low_notified > 0 AND balance_micro >= ?", (LOW_MICRO,))
    db.commit()
    rows = db.execute(
        "SELECT a.id, a.email, a.balance_micro, a.low_notified, "
        "EXISTS(SELECT 1 FROM credits c WHERE c.account_id = a.id AND c.kind = 'topup') AS topped, "
        "EXISTS(SELECT 1 FROM usage u WHERE u.account_id = a.id) AS used, a.lang "
        "FROM accounts a WHERE a.balance_micro < ? AND a.blocked_at IS NULL", (LOW_MICRO,)).fetchall()
    due = []
    for row in rows:
        if not row["used"]:
            continue                      # a new account with a gift: has not spent anything yet
        balance = int(row["balance_micro"])
        level = 2 if balance <= 0 else (1 if row["topped"] else 0)
        if level == 0 or level <= int(row["low_notified"]):
            continue
        own = _lang(lang or row["lang"])
        message = _compose("empty", own) if level == 2 else _compose("low", own, balance=usd(balance))
        due.append(Due(int(row["id"]), str(row["email"]), level, message))
    return due


def mark_notified(db: sqlite3.Connection, account_id: int, level: int) -> None:
    """Mark that the email of this level went out: the next sweep will not repeat it."""
    db.execute("UPDATE accounts SET low_notified = ? WHERE id = ?", (level, account_id))
    db.commit()


def sweep_balances(db: sqlite3.Connection, send: Callable[[str, mailtemplate.Message], None],
                   lang: str | None = None) -> int:
    """All at once, in one thread: for checks and manual runs. The server goes in parts (`app.py`)."""
    sent = 0
    for item in balances_due(db, lang):
        try:
            send(item.address, item.message)
        except Exception as e:           # the email did not go out, so no mark; the next sweep will retry
            log.warning("balance email was not sent (account %s): %s", item.account_id, e)
            continue
        mark_notified(db, item.account_id, item.level)
        sent += 1
    return sent
