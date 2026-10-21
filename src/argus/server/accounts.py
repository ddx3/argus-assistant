"""Accounts: registration, sign-in, sign-out.

Ordinary registration is an email and a password. No email confirmation, no binding to a
computer, no limits per network: people pay for access instead of passing checks.

What this honestly means: **the gift to the balance can be taken again by creating a new
email**. Before, a binding to the device protected against this; now it does not exist.
The trade-off is deliberate: simple registration against the cost of the gift. That is
why the gift is kept small: an amount that is no pity to give to one person twice
(`pricing.WELCOME_MICRO`).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from argus.server import emails, pricing, tokens, weakpass

log = logging.getLogger(__name__)

# The scrypt settings. They are taken with a margin: a password check must take a noticeable
# fraction of a second, otherwise guessing against a leaked database goes at millions per
# second.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
# The memory ceiling for scrypt. By default the library allows 32 MiB, and a raised `N`
# (2**15 and higher, OWASP asks for 2**17) would fail with "memory limit exceeded": the hash
# would not be computed at all, and the check of a new hash would silently say "wrong
# password".
SCRYPT_MAXMEM = 256 * 1024 * 1024

# scrypt is computed outside the event loop, in its own small pool: while it is busy, the
# server answers the others. The pool is its own, not the shared one, so that a flood of
# sign-ins does not push address resolution (DNS) for calls to providers out of the shared
# pool. No more than `MAX_HASH_QUEUE` checks may wait: beyond that it is "server busy", not
# a queue of minutes.
HASH_WORKERS = 2
MAX_HASH_QUEUE = 16

MIN_PASSWORD = 8

# How many sign-up gifts we hand out per day across the whole server. This exists
# precisely because nobody confirms their email and "creating one more" costs nothing:
# without a cap, a thousand emails would mean a thousand gifts (about $150 of our own
# money in a single evening). Past the cap, the account is still created, just without
# a gift, and the log gets a loud line. Set it with the `ARGUS_GIFT_DAILY_CAP` variable
# (0 means no gifts at all).
GIFT_DAILY_CAP = 100

# How many sign-ins (refresh tokens) one account may hold; the oldest are deleted. Without
# a cap, a loop of sign-ins would grow the `sessions` table without limit.
MAX_SESSIONS_PER_ACCOUNT = 20

# Half an hour. People open the link almost at once, because the email has just arrived;
# they do not keep it for later. A long lifetime would be a risk with no benefit: if the
# email was intercepted, the longer the link lives, the longer it can be used.
RESET_TTL_S = 30 * 60

# At most once a minute per account. Without this, the recovery form becomes a "flood
# someone else's mailbox" button. The address in the request is the account's own, so the
# emails go to the owner of that address, not to whoever presses the button.
RESET_COOLDOWN_S = 60


class Denied(Exception):
    """A refusal that can be shown to the person as is."""


class WrongPassword(Denied):
    """The password did not fit. The attempt counter counts such refusals (`throttle.py`); "new
    password too short" is also `Denied`, but it is not guessing."""


class Busy(Exception):
    """Password checks are queued: the server is busy now. To the person: "try again"."""


@dataclass(frozen=True)
class Login:
    account_id: int
    refresh: str
    access: str


def hash_password(password: str) -> str:
    """A password hash together with the salt and the parameters.

    The parameters lie next to the hash on purpose: when a year from now we raise `N`, the old
    hashes must keep being verified instead of cutting people off from signing in, and be
    recomputed with the new parameters at sign-in (`needs_rehash`).
    """
    salt = secrets.token_hex(16)
    raw = hashlib.scrypt(password.encode(), salt=salt.encode(),
                         n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                         maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt}${raw.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        raw = hashlib.scrypt(password.encode(), salt=salt.encode(),
                             n=int(n), r=int(r), p=int(p), dklen=len(want) // 2,
                             maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    # Constant-time comparison: a plain `==` leaks through the response time how many leading
    # bytes matched, and the hash can be guessed one byte at a time.
    return secrets.compare_digest(raw.hex(), want)


def needs_rehash(stored: str) -> bool:
    """The hash was computed with settings other than the current ones: after a successful sign-in it
    is time to update it.
    """
    try:
        kind, n, r, p, _salt, _want = stored.split("$")
        return (kind, int(n), int(r), int(p)) != ("scrypt", SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return False                       # we do not recompute garbage: it would fail the check anyway


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A dummy hash for sign-in with a nonexistent email: computed once, not on every request.
    Checking against it costs the same milliseconds as checking against a real one: exactly one
    check in both cases."""
    return hash_password("doesn't matter")


def _check_or_dummy(password: str, stored: str | None) -> bool:
    return check_password(password, stored if stored is not None else _dummy_hash())


_pool = ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="scrypt")
_waiting = 0
_waiting_lock = threading.Lock()


async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a heavy computation (scrypt) outside the event loop.

    Only the computation itself: work with the database stays on the loop, because there is
    one connection for everybody, and a write from two threads would mix transactions.
    """
    global _waiting
    with _waiting_lock:
        if _waiting >= MAX_HASH_QUEUE:
            raise Busy("the server is busy right now — try again in a minute")
        _waiting += 1
    try:
        return await asyncio.get_running_loop().run_in_executor(_pool, fn, *args)
    finally:
        with _waiting_lock:
            _waiting -= 1


def check_new_password(password: str, email: str = "") -> None:
    """Whether the password is suitable for a new one: not shorter than the minimum and not among
    those that are guessed first.
    """
    if len(password) < MIN_PASSWORD:
        raise Denied(f"password is shorter than {MIN_PASSWORD} characters")
    if weakpass.is_weak(password, email):
        raise Denied("the password is too simple — pick another one")


def register(db: sqlite3.Connection, secret: str, email: str, password: str,
             now: float | None = None, terms: str | None = None, gift: bool = True) -> Login:
    """Create an account. The gift to the balance is given right here.

    `terms` is the version of the terms that the user saw under the button. Here it is
    optional, so as not to require it from those who create accounts by hand (`grant`,
    tests); the HTTP layer makes it mandatory (`app.Registration`): over the network an
    account without accepted terms is not created.
    """
    now = time.time() if now is None else now
    key = _new_account_key(db, email, password)
    return _create_account(db, secret, email, key, hash_password(password), now, terms, gift)


async def register_async(db: sqlite3.Connection, secret: str, email: str, password: str,
                         now: float | None = None, terms: str | None = None,
                         gift: bool = True) -> Login:
    """The same as `register`, but scrypt is computed outside the event loop.

    While the hash is being computed, the loop is busy with other requests: the "email is
    free" check is done before the computation (a cheap refusal does not cost scrypt), and the
    unique index protects against a race of two simultaneous registrations for the same email
    (`_create_account`).
    """
    now = time.time() if now is None else now
    key = _new_account_key(db, email, password)
    pw_hash = await _off_loop(hash_password, password)
    return _create_account(db, secret, email, key, pw_hash, now, terms, gift)


def _new_account_key(db: sqlite3.Connection, email: str, password: str) -> str:
    """Everything that can be checked before the expensive hash: the address, the password, whether
    the email is free.
    """
    key = emails.check(email)
    check_new_password(password, email)

    if db.execute("SELECT id FROM accounts WHERE email_key = ?", (key,)).fetchone():
        # Not "no such email, or wrong password". Hiding that
        # an account exists only makes sense where the very fact of registration is a secret. Here
        # it only gets in the way: the person cannot tell whether to sign in or to register.
        raise Denied("an account with this email already exists — sign in")
    return key


def _take_gift(db: sqlite3.Connection, now: float) -> int:
    """How much to give to a new account: `WELCOME_MICRO` until the daily ceiling is used up,
    then 0.

    The counter grows in the same transaction as the account insert: if the insert fails, the
    gift is not spent either. It never decreases (see `db.SCHEMA`, `gift_budget`).
    """
    try:
        cap = int(os.environ.get("ARGUS_GIFT_DAILY_CAP", "").strip() or GIFT_DAILY_CAP)
    except ValueError:
        cap = GIFT_DAILY_CAP
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    db.execute("INSERT OR IGNORE INTO gift_budget (day, n) VALUES (?, 0)", (day,))
    given = int(db.execute("SELECT n FROM gift_budget WHERE day = ?", (day,)).fetchone()["n"])
    if given >= cap:
        log.warning("signup gift not granted: over the last day already %s, cap %s (ARGUS_GIFT_DAILY_CAP) — "
                    "abuse or a spike in signups", given, cap)
        return 0
    db.execute("UPDATE gift_budget SET n = n + 1 WHERE day = ?", (day,))
    return pricing.WELCOME_MICRO


def _create_account(db: sqlite3.Connection, secret: str, email: str, key: str,
                    pw_hash: str, now: float, terms: str | None, gift: bool = True) -> Login:
    present = _take_gift(db, now) if gift else 0
    try:
        cur = db.execute(
            "INSERT INTO accounts (email, email_key, password, created_at, balance_micro, "
            "terms_version, terms_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email.strip(), key, pw_hash, now, present,
             terms, now if terms else None),
        )
    except sqlite3.IntegrityError:
        # The same email was taken in the moments while the hash was being computed.
        db.rollback()
        raise Denied("an account with this email already exists — sign in") from None
    account_id = int(cur.lastrowid)
    # A gift is also a top-up and is recorded as one. Otherwise the new account's balance would
    # be the only one whose origin cannot be found in the log. The balance is already set
    # above; the row is needed only for the log.
    if present:
        db.execute(
            "INSERT INTO credits (account_id, kind, micro, note, at) VALUES (?, 'gift', ?, '', ?)",
            (account_id, present, now),
        )
    return _open_session(db, secret, account_id, now)


def _find_account(db: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    key = emails.check(email)
    return db.execute("SELECT * FROM accounts WHERE email_key = ?", (key,)).fetchone()


def _refuse_sign_in(row: sqlite3.Row | None, ok: bool) -> None:
    """The refusal text deliberately distinguishes the cases: this makes it clearer where to go next
    (register or try the password again), even though it hands whoever enumerates emails the fact
    of registration. The defense against enumeration is the attempt counter (`throttle.py`), not
    silence."""
    if row is None:
        raise Denied("no such account")
    if not ok:
        raise WrongPassword("wrong password")
    if row["blocked_at"] is not None:
        raise Denied("account is closed")


def _store_rehash(db: sqlite3.Connection, account_id: int, old: str, new: str) -> None:
    # `AND password = old`: if the password was changed while the new hash was being computed,
    # we do not overwrite it.
    db.execute("UPDATE accounts SET password = ? WHERE id = ? AND password = ?",
               (new, account_id, old))
    db.commit()


def sign_in(db: sqlite3.Connection, secret: str, email: str, password: str,
            now: float | None = None) -> Login:
    now = time.time() if now is None else now
    row = _find_account(db, email)

    # The password is checked even when the account does not exist. scrypt spends the same
    # milliseconds on a dummy hash as on a real one: exactly one check in both cases, so the
    # response time does not reveal whether such an email exists.
    ok = _check_or_dummy(password, None if row is None else row["password"])

    _refuse_sign_in(row, ok)
    if needs_rehash(row["password"]):
        _store_rehash(db, int(row["id"]), row["password"], hash_password(password))
    return _open_session(db, secret, int(row["id"]), now)


async def sign_in_async(db: sqlite3.Connection, secret: str, email: str, password: str,
                        now: float | None = None) -> Login:
    """The same as `sign_in`, but scrypt runs outside the event loop (a single sign-in with a
    nonexistent email would hold the whole server for about 0.2 s, and three a second would hang
    it for good)."""
    now = time.time() if now is None else now
    row = _find_account(db, email)
    stored = None if row is None else row["password"]
    ok = await _off_loop(_check_or_dummy, password, stored)

    if row is not None:
        # While the hash was being computed, the account could have been closed, deleted, or had
        # its password changed.
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (int(row["id"]),)).fetchone()
        if row is None or row["password"] != stored:
            ok = False
    _refuse_sign_in(row, ok)

    if needs_rehash(row["password"]):
        try:
            fresh = await _off_loop(hash_password, password)
        except Busy:
            pass                              # no hurry: it is recomputed at the next sign-in
        else:
            _store_rehash(db, int(row["id"]), row["password"], fresh)
    return _open_session(db, secret, int(row["id"]), now)


def _open_session(db: sqlite3.Connection, secret: str, account_id: int,
                  now: float) -> Login:
    raw, token_hash = tokens.new_refresh()
    db.execute(
        "INSERT INTO sessions (token_hash, account_id, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (token_hash, account_id, now, now + tokens.REFRESH_TTL_S),
    )
    # Expired sign-ins of this account are deleted, and no more than the cap of live ones
    # remain. Otherwise a loop of sign-ins would accumulate rows without limit.
    db.execute("DELETE FROM sessions WHERE account_id = ? AND expires_at < ?", (account_id, now))
    db.execute(
        "DELETE FROM sessions WHERE account_id = ? AND token_hash NOT IN "
        "(SELECT token_hash FROM sessions WHERE account_id = ? "
        " ORDER BY issued_at DESC, rowid DESC LIMIT ?)",
        (account_id, account_id, MAX_SESSIONS_PER_ACCOUNT),
    )
    db.commit()
    return Login(
        account_id=account_id,
        refresh=raw,
        access=tokens.sign_access(secret, account_id, now=now),
    )


def refresh(db: sqlite3.Connection, secret: str, token: str,
            now: float | None = None) -> str:
    """Exchange the long refresh token for a short pass."""
    now = time.time() if now is None else now
    row = db.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (tokens.hash_secret(token),)
    ).fetchone()
    if row is None or row["expires_at"] < now:
        raise Denied("you need to sign in again")
    account = db.execute(
        "SELECT blocked_at FROM accounts WHERE id = ?", (row["account_id"],)
    ).fetchone()
    if account is None or account["blocked_at"] is not None:
        raise Denied("account is closed")
    return tokens.sign_access(secret, int(row["account_id"]), now=now)


def sign_out(db: sqlite3.Connection, token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (tokens.hash_secret(token),))
    db.commit()


def change_password(db: sqlite3.Connection, account_id: int, old: str, new: str) -> None:
    """Change the password. All other sign-ins are closed at the same time.

    A password is most often changed because the old one may have leaked. Leaving the old
    sessions alive after that means not solving exactly the problem for which it was
    changed.
    """
    row = _password_row(db, account_id)
    if row is None or not check_password(old, row["password"]):
        raise WrongPassword("old password is incorrect")
    check_new_password(new, row["email"])
    _set_password(db, account_id, row["password"], hash_password(new))


async def change_password_async(db: sqlite3.Connection, account_id: int, old: str,
                                new: str) -> None:
    """The same as `change_password`, but scrypt runs outside the event loop."""
    row = _password_row(db, account_id)
    if row is None or not await _off_loop(check_password, old, row["password"]):
        raise WrongPassword("old password is incorrect")
    check_new_password(new, row["email"])
    _set_password(db, account_id, row["password"], await _off_loop(hash_password, new))


def _password_row(db: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT password, email FROM accounts WHERE id = ?",
                      (account_id,)).fetchone()


def _cancel_resets(db: sqlite3.Connection, account_id: int, now: float) -> None:
    """Cancel all still-live recovery links of this account.

    The password has been changed, so an email that someone else might have seen must not keep
    the door open for another half hour: "the password may have leaked, so I changed it"
    would otherwise not solve the problem for which it was changed.
    """
    db.execute("UPDATE password_resets SET used_at = ? WHERE account_id = ? AND used_at IS NULL",
               (now, account_id))


def _set_password(db: sqlite3.Connection, account_id: int, old_hash: str, new_hash: str,
                  now: float | None = None) -> None:
    now = time.time() if now is None else now
    # `AND password = old`: between checking the old password and writing the new one, the
    # hash computation (`change_password_async`) could have run, and the password could have
    # been changed in that time.
    cur = db.execute("UPDATE accounts SET password = ? WHERE id = ? AND password = ?",
                     (new_hash, account_id, old_hash))
    if cur.rowcount != 1:
        db.rollback()
        raise Denied("the password was just changed — sign in again")
    db.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    _cancel_resets(db, account_id, now)
    db.commit()


def begin_reset(db: sqlite3.Connection, email: str,
                now: float | None = None) -> tuple[str, str] | None:
    """A recovery request. Returns (the raw token, the address for the email).

    The address is taken from the account, not from what was typed in the request: you can
    type anything, which `emails.check` might let through in a different way, while the email
    goes exactly to the place where the account was created.

    `None` in two different cases, and from the outside they must look the same: there is no
    account with that email, or an email was just sent and it is too early to send again.
    Revealing the difference would let someone use the mail to enumerate which addresses we
    have registered. This is the same technique as in sign-in, except that the secret is not
    the password but the very fact that the account exists.
    """
    now = time.time() if now is None else now
    try:
        key = emails.check(email)
    except emails.BadEmail:
        return None
    row = db.execute("SELECT id, email FROM accounts WHERE email_key = ?", (key,)).fetchone()
    if row is None:
        return None
    account_id = int(row["id"])
    try:
        address = emails.ascii_address(row["email"])
    except emails.BadEmail:
        # The account was created while the address rules were softer. Such an address is no reason
        # to send an email that would go to the wrong place.
        log.warning("recovery: unusable account address for an email: %s", account_id)
        return None

    last = db.execute(
        "SELECT issued_at FROM password_resets WHERE account_id = ? "
        "ORDER BY issued_at DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if last is not None and now - last["issued_at"] < RESET_COOLDOWN_S:
        return None

    raw = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO password_resets (token_hash, account_id, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (tokens.hash_secret(raw), account_id, now, now + RESET_TTL_S),
    )
    db.commit()
    return raw, address


def start_reset(db: sqlite3.Connection, email: str, now: float | None = None) -> str | None:
    """A recovery request. Returns the raw token; the caller sends the email."""
    started = begin_reset(db, email, now)
    return started[0] if started else None


def _reset_target(db: sqlite3.Connection, raw_token: str, new_password: str,
                  now: float) -> tuple[str, int, str]:
    """Checks before the expensive hash: the password is suitable, the link is alive.

    Returns (link hash, account number, key of its email)."""
    if len(new_password) < MIN_PASSWORD:
        raise Denied(f"password is shorter than {MIN_PASSWORD} characters")

    token_hash = tokens.hash_secret(raw_token)
    row = db.execute(
        "SELECT * FROM password_resets WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None or row["used_at"] is not None or row["expires_at"] < now:
        raise Denied("link is invalid or expired — request a new one")

    account_id = int(row["account_id"])
    owner = db.execute("SELECT email, email_key FROM accounts WHERE id = ?",
                       (account_id,)).fetchone()
    check_new_password(new_password, owner["email"] if owner else "")
    return token_hash, account_id, str(owner["email_key"]) if owner else ""


def _apply_reset(db: sqlite3.Connection, token_hash: str, account_id: int, new_hash: str,
                 now: float) -> None:
    # The link is claimed FIRST, with a single conditional update. Two simultaneous requests
    # with the same link (the hash was computed outside the loop) cannot both set a password.
    claimed = db.execute(
        "UPDATE password_resets SET used_at = ? "
        "WHERE token_hash = ? AND used_at IS NULL AND expires_at >= ?",
        (now, token_hash, now),
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise Denied("link is invalid or expired — request a new one")
    db.execute("UPDATE accounts SET password = ? WHERE id = ?", (new_hash, account_id))
    # The same reason as for a password change from the settings: if the password was changed,
    # most likely the old one may have leaked, so the old sign-ins are closed together with it
    # instead of staying alive. The other recovery links are closed too.
    db.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    _cancel_resets(db, account_id, now)
    db.commit()


def finish_reset(db: sqlite3.Connection, raw_token: str, new_password: str,
                 now: float | None = None) -> str:
    """Set a new password from the link in the email. Returns the account's email key.

    We do not open a session: the recovery page lives in the browser, while the sign-in is
    needed by the app on another machine. The token cannot be passed from there, and there is
    no place and no reason to store it in the browser for the sake of one "done" screen. The
    user simply returns to the app and signs in with the new password.
    """
    now = time.time() if now is None else now
    token_hash, account_id, key = _reset_target(db, raw_token, new_password, now)
    _apply_reset(db, token_hash, account_id, hash_password(new_password), now)
    return key


async def finish_reset_async(db: sqlite3.Connection, raw_token: str, new_password: str,
                             now: float | None = None) -> str:
    """The same as `finish_reset`, but scrypt runs outside the event loop."""
    now = time.time() if now is None else now
    token_hash, account_id, key = _reset_target(db, raw_token, new_password, now)
    new_hash = await _off_loop(hash_password, new_password)
    _apply_reset(db, token_hash, account_id, new_hash, now)
    return key


def accept_terms(db: sqlite3.Connection, account_id: int, version: str,
                 now: float | None = None) -> None:
    """Remember that the user accepted the terms of this version.

    This is for those who created an account before the terms appeared, and for the case when
    the version has changed: the app shows the new text and after the press of "I accept"
    sends the version that it showed.
    """
    now = time.time() if now is None else now
    db.execute("UPDATE accounts SET terms_version = ?, terms_at = ? WHERE id = ?",
               (version, now, account_id))
    db.commit()


def export_data(db: sqlite3.Connection, account_id: int) -> dict:
    """Everything the server knows about the account, for the person who asks for it.

    We do not hand out the password and the tokens: the password hash is not the person's data
    but a server secret, and giving it out gives the person nothing. There are no texts of
    questions and answers here, for the same reason they are not on the server: we do not
    store them (`server/relay.py` writes only the number of tokens into the usage). Nor is
    there who behind the scenes answered and who accepted the payment: which model and which
    payment provider is our internal business, and what matters to the person is what and
    how much.
    """
    acc = db.execute(
        "SELECT email, created_at, terms_version, terms_at, balance_micro "
        "FROM accounts WHERE id = ?", (account_id,),
    ).fetchone()
    if acc is None:
        raise Denied("no account")

    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in db.execute(sql, (account_id,)).fetchall()]

    return {
        "account": dict(acc),
        "credits": rows("SELECT kind, micro, note, at FROM credits "
                        "WHERE account_id = ? ORDER BY at, id"),
        "orders": rows("SELECT amount, currency, state, created_at, paid_at "
                       "FROM orders WHERE account_id = ? ORDER BY created_at, id"),
        "usage": rows("SELECT kind, amount, at, charged_micro FROM usage "
                      "WHERE account_id = ? ORDER BY at, id"),
    }


def erase_account(db: sqlite3.Connection, account_id: int) -> None:
    """Erase an account: without a password, for a request that arrived to the operator by email
    (`erase.py`), and after the password check in `delete_account`.

    The email, the password hash, the sign-ins and the recovery requests disappear. The money
    and the usage stay, but without an owner: the rows are re-linked to `-id`. There are two
    reasons. The money report must add up even after the person has left (see `db.connect`),
    and orders are by law kept longer than the account itself. A negative number will not go
    to anyone.

    The account's own number will not go to anyone either: `accounts.id` is `AUTOINCREMENT`
    (`db._never_reuse_ids`). Before, the "newest" row was deleted, and the next registrant got
    the same number, and with it the still-live pass of the erased account, because the pass
    checks only that "a row with such a number exists".
    """
    row = db.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise Denied("no account")
    gone = -account_id
    for table in ("orders", "credits", "usage"):
        db.execute(f"UPDATE {table} SET account_id = ? WHERE account_id = ?",
                   (gone, account_id))
    db.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    db.execute("DELETE FROM password_resets WHERE account_id = ?", (account_id,))
    db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    db.commit()


def delete_account(db: sqlite3.Connection, account_id: int, password: str) -> None:
    """Delete an account at the person's request. The password is asked for again.

    What exactly is erased and what remains is in `erase_account`. The balance remainder
    disappears together with the account, and the app says so before the confirmation.
    """
    row = db.execute("SELECT password FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise Denied("no account")
    if not check_password(password, row["password"]):
        raise WrongPassword("password does not match")
    erase_account(db, account_id)


async def delete_account_async(db: sqlite3.Connection, account_id: int, password: str) -> None:
    """The same as `delete_account`, but scrypt runs outside the event loop."""
    row = db.execute("SELECT password FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise Denied("no account")
    if not await _off_loop(check_password, password, row["password"]):
        raise WrongPassword("password does not match")
    erase_account(db, account_id)
