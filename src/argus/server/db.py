"""Storage of the accounts server: schema and connection.

SQLite, not Postgres, and this is deliberate. Everything stored here is tens of thousands
of rows for tens of thousands of users: accounts, balances, usage. The load goes not to
the database but to calls to models, and there the bottleneck is the network. Moving to
Postgres will cost one file, whereas now we would pay for it with a running server on
every developer's machine.

Money is counted here and only here. A counter on the client is meaningless: the app is
distributed to people, and they can rewrite anything written in it.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

SCHEMA = """
-- Account: email and password, ordinary sign-up.
--
-- The email is not confirmed. This is a deliberate trade-off: confirmation filters out
-- some foreign and disposable mailboxes, but it costs an email for every newcomer,
-- a configured sender, and one more step where people drop off. While there is little
-- free here, the trade-off favours simplicity, and bringing confirmation back
-- will be cheaper than it is now.
CREATE TABLE IF NOT EXISTS accounts (
    -- AUTOINCREMENT: the number of a deleted account does not go to the next one. Without it SQLite
    -- hands the freed number of the newest row to a new person, and with it the still-live pass
    -- of the erased one, which only checks that "a row with such a number exists".
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT    NOT NULL,          -- as entered: to show and to pass to the payment provider
    email_key   TEXT    NOT NULL UNIQUE,   -- normalized: for comparison
    password    TEXT    NOT NULL,          -- scrypt, together with the salt and the parameters
    created_at  REAL    NOT NULL,
    free_left   INTEGER NOT NULL DEFAULT 0,  -- obsolete: gift seconds from the subscription era, not read
    blocked_at  REAL,                        -- not NULL means sign-in is closed
    -- Money on the account, in micro-dollars (`pricing.MICRO` per dollar). The only
    -- place that holds "how much is left": a top-up adds, spending
    -- subtracts, it never goes below zero. An integer, not a fraction, for the same reason
    -- as the cents at the payment provider: over a thousand small charges fractions drift apart.
    balance_micro INTEGER NOT NULL DEFAULT 0,
    -- Which version of the terms and policy the person accepted, and when (see `ADDED`).
    terms_version TEXT,
    terms_at    REAL,
    -- Which low-balance email has already gone out: 0 - none, 1 - "running low", 2 - "ran out".
    -- Reset to 0 when the balance has grown again (`mailevents.sweep_balances`).
    low_notified INTEGER NOT NULL DEFAULT 0,
    -- The app language of the person (`ru`, `uk`, `en`…), as the client reported it, and emails are written in it. Empty means it was not reported.
    lang        TEXT NOT NULL DEFAULT ''
);

-- Live sign-ins. The refresh token is stored as a hash: a stolen database must not
-- let whoever read it sign in as the person.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL,
    issued_at   REAL    NOT NULL,
    expires_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_account ON sessions (account_id);

-- Password recovery requests. The token is hashed, for the same reason as
-- the refresh token: a stolen database must not allow changing the password of another account.
--
-- `used_at` is a mark, not a deletion. The link from the email is opened twice not
-- only out of malice: the mail client itself follows links in advance to check them
-- for viruses, and the first visit would not be the person. The mark tells apart
-- "already used" and "never existed": the first says "request a
-- new one", the second may mean the link was forged.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash  TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL,
    issued_at   REAL    NOT NULL,
    expires_at  REAL    NOT NULL,
    used_at     REAL
);
CREATE INDEX IF NOT EXISTS password_resets_by_account ON password_resets (account_id, issued_at);

-- Top-up ledger: where the money of an account came from. The balance is the sum
-- of this table minus the sum of `usage.charged_micro` (allowing for the fact that it
-- never goes below zero). Without the ledger there is nothing to check "how much should be here"
-- against, and looking into a disputed payment starts with guessing.
--
--   gift   — a gift at sign-up
--   topup  — a paid order (`note` = order number)
--   grant  — a manual grant by the owner (`server/grant.py`)
--   legacy — a recalculation from the subscription era, once at the transition
CREATE TABLE IF NOT EXISTS credits (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    micro      INTEGER NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    at         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS credits_by_account ON credits (account_id, at);

-- How many sign-up gifts were given out per day (UTC). No personal data, on purpose: the counter does not
-- decrease when an account is deleted, otherwise the cycle "create, spend, delete, create again" would know no
-- limit. The daily cap is `accounts.GIFT_DAILY_CAP`, beyond it an account is created without a gift.
CREATE TABLE IF NOT EXISTS gift_budget (
    day TEXT PRIMARY KEY,
    n   INTEGER NOT NULL
);

-- Orders. Created before going to the payment provider and outliving it: without a row
-- created in advance, a returning payment has nothing to be matched with, and the money
-- has to be sorted out by hand from the correspondence with the bank.
--
-- `external_id` is unique not for tidiness: the provider repeats a webhook until it
-- gets an answer, and without this a single payment would have activated a subscription twice.
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL,
    plan        TEXT    NOT NULL,   -- always `topup`, the column is left over from subscriptions
    amount      INTEGER NOT NULL,   -- in the minimal currency units (cents)
    currency    TEXT    NOT NULL,
    provider    TEXT    NOT NULL,
    external_id TEXT,
    state       TEXT    NOT NULL,   -- new | paid | reversed | failed
    created_at  REAL    NOT NULL,
    paid_at     REAL,
    reversed_at REAL                -- when the payment provider returned the money to the person (state = reversed)
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_external
    ON orders (provider, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS orders_by_account ON orders (account_id, created_at);

-- Usage. Written after the fact, not on a promise: a row appears when the
-- seconds of audio have already gone to the model. Otherwise a dropped connection would leave
-- people free minutes that we paid for.
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,   -- stt_seconds | brain_tokens | tts_chars
    amount     REAL    NOT NULL,   -- in units of its own kind: seconds, tokens, characters
    at         REAL    NOT NULL,
    model      TEXT    NOT NULL DEFAULT '',
    -- What we paid and what was charged to the person. Side by side on purpose: the difference is
    -- the revenue, and it cannot be recomputed from the price list after the fact, because the
    -- price list may have changed since.
    cost_micro    INTEGER NOT NULL DEFAULT 0,
    charged_micro INTEGER NOT NULL DEFAULT 0,
    -- The number of the recognition report from the client (`POST /meter/stt`): a repeat of the same report (the answer
    -- was lost, the client sent it again) is not charged a second time. Empty for usage that
    -- the server counted itself, and for reports from old apps. The unique index on it is created
    -- by the migration (`_report_index`), not by this schema: an old database does not have the column yet.
    report_id     TEXT
);
CREATE INDEX IF NOT EXISTS usage_by_account ON usage (account_id, at);

-- Money held for a paid call (`entitlements.hold`): the row lives while the
-- call is running and is erased when it is closed. A leftover at server start is a call
-- that did not live to the end, and the money is returned (`entitlements.recover_holds`).
CREATE TABLE IF NOT EXISTS holds (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    micro      INTEGER NOT NULL,
    at         REAL    NOT NULL
);
"""

# Schema version in `PRAGMA user_version`. The migration of an old database (`_migrate`)
# runs once, in one transaction, and sets this number at its end. A new column below is
# not a reason to edit the condition but a reason to raise the number and add the column to
# `ADDED`.
#
#   1 - first version: the columns from `ADDED`, `accounts` with AUTOINCREMENT;
#   2 - `usage.report_id` and a unique index on (account, report number);
#   3 - `accounts.low_notified` (low-balance emails) and `accounts.lang` (email language).
#       Without this number, a version 2 database would not get the new columns, because
#       `_migrate` returns at once for it.
SCHEMA_VERSION = 3

# Columns that appeared after the first databases. `CREATE TABLE IF NOT EXISTS` knows
# nothing about them: the table already exists, and a new column will not arrive in it. The
# list exists so that updating the server does not require a manual `ALTER`.
ADDED = {
    "accounts": {
        "free_left": "INTEGER NOT NULL DEFAULT 0",
        "blocked_at": "REAL",
        # Appeared with the move to passwords. An empty value means an account from the days of
        # sign-in by code: it has no password and none can be guessed, yet its email is taken.
        # Such accounts are closed during the migration (below). An account that cannot be signed
        # into and cannot be created again is worse than a closed one: it looks working and
        # silently lets nobody in.
        "password": "TEXT NOT NULL DEFAULT ''",
        # Appeared with the move from a subscription to money on the balance. The recalculation of
        # old gift seconds and subscriptions is `_to_balances`, once, at the moment the column is
        # created.
        "balance_micro": "INTEGER NOT NULL DEFAULT 0",
        # Which version of the terms and policy the person accepted, and when. The version is the
        # date that the app showed under the registration button (`account/legal.py`). It is empty
        # for accounts created before the terms existed: the app offers them to accept the terms at
        # the next sign-in.
        "terms_version": "TEXT",
        "terms_at": "REAL",
        # Low-balance email: what has already been sent (see `CREATE TABLE accounts`).
        "low_notified": "INTEGER NOT NULL DEFAULT 0",
        # Email language (see `CREATE TABLE accounts`).
        "lang": "TEXT NOT NULL DEFAULT ''",
    },
    "usage": {
        "model": "TEXT NOT NULL DEFAULT ''",
        "cost_micro": "INTEGER NOT NULL DEFAULT 0",
        "charged_micro": "INTEGER NOT NULL DEFAULT 0",
        # Number of the client's recognition report, for a retry without a double charge.
        "report_id": "TEXT",
    },
    "orders": {
        # A payment reversal by the payment provider: the order is not erased and does not stay
        # "paid" either, but gets a mark by which the owner sees what to review (`billing.py`).
        "reversed_at": "REAL",
    },
}


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection with the schema already applied.

    `WAL` is used so that reading entitlements does not wait for a usage write: the server
    writes usage constantly and asks for entitlements even more often. We do not enable
    `foreign_keys`: the relations here are obvious, and cascading deletion of an account is
    not what we want, because usage must outlive its owner, or no report on money would add
    up.
    """
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Bring an old database to the current schema: in one transaction and only once.

    `ALTER TABLE ... ADD COLUMN` is committed immediately on its own, while the money
    recalculation (`_to_balances`) stayed in an open transaction until the end of `connect`.
    If the process died between them, the column would already exist on the next start, and
    gifts, owners, and subscriptions would never be recalculated. SQLite can roll back DDL
    too, so everything runs inside one `BEGIN IMMEDIATE`, and the readiness mark,
    `user_version`, is set in the same transaction: either the whole database has moved, or
    it is untouched.
    """
    if int(db.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        _add_missing_columns(db)
        _never_reuse_ids(db)
        _report_index(db)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.rollback()
        raise
    db.commit()


# Definition of the accounts table, the same as in `SCHEMA` but without "IF NOT EXISTS": it
# is used to rebuild an old table that lacks `AUTOINCREMENT`.
_ACCOUNTS_DDL = re.search(
    r"CREATE TABLE IF NOT EXISTS accounts \(.*?\n\);", SCHEMA, re.DOTALL
).group(0).replace("IF NOT EXISTS ", "")


def _never_reuse_ids(db: sqlite3.Connection) -> None:
    """Rebuild `accounts` with `AUTOINCREMENT` if the table was created without it.

    SQLite cannot add this to an existing table, only create a new one, copy the rows over,
    and drop the old one. It runs inside the `_migrate` transaction: either the whole
    database has moved, or nothing is touched. Numbers and values are carried over as they
    are (columns by name, not by order, since the order differs in old databases), and the
    `AUTOINCREMENT` counter sets itself to the largest number.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
    ).fetchone()
    if row is None or "AUTOINCREMENT" in str(row["sql"]).upper():
        return
    old = [(r["name"], r["type"]) for r in db.execute("PRAGMA table_info(accounts)")]
    db.execute("ALTER TABLE accounts RENAME TO accounts_old")
    db.execute(_ACCOUNTS_DDL)
    new = {r["name"] for r in db.execute("PRAGMA table_info(accounts)")}
    # Columns that are not in the current schema (a memory of earlier eras) are not silently
    # lost: they are carried over as they are, without constraints. Nobody reads them, but data
    # must not be thrown away when rebuilding a production database.
    for name, kind in old:
        if name not in new:
            db.execute(f'ALTER TABLE accounts ADD COLUMN "{name}" {kind}')
    names = ", ".join(f'"{name}"' for name, _ in old)
    db.execute(f"INSERT INTO accounts ({names}) SELECT {names} FROM accounts_old")
    db.execute("DROP TABLE accounts_old")


def _report_index(db: sqlite3.Connection) -> None:
    """One report number is one usage row per account.

    It runs after `_add_missing_columns`: in an old database the `report_id` column appears
    only there, and an index on it in `SCHEMA` would kill `executescript` before the
    migration. It is an ordinary (not partial) unique index: empty values in SQLite are not
    equal to each other, so rows without a number (relay usage, reports from old apps) do not
    interfere with the index. `IF NOT EXISTS` makes a repeated call harmless.
    """
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS usage_report ON usage (account_id, report_id)"
    )


def _add_missing_columns(db: sqlite3.Connection) -> None:
    for table, columns in ADDED.items():
        have = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                if table == "accounts" and name == "password":
                    # Close, all at once, everyone who was left without a password: see `ADDED`.
                    db.execute(
                        "UPDATE accounts SET blocked_at = ? WHERE password = ''",
                        (time.time(),),
                    )
                if table == "accounts" and name == "balance_micro":
                    _to_balances(db)


# How much a gift second cost in the old era: 300 seconds were five minutes, and the thirty
# cents of the new gift are the same 300 "seconds" at 0.1 cent each.
_LEGACY_GIFT_MICRO_PER_SECOND = 1_000

# Owner access from the subscription era ("owner", a hundred thousand hours) in money. Not
# infinity but a reserve, as it was: usage is still recorded and reduces it.
_LEGACY_OWNER_MICRO = 1_000 * 1_000_000


def _to_balances(db: sqlite3.Connection) -> None:
    """Convert old entitlements into money, once: the gift and subscriptions.

    - the remaining gift seconds turn into money at `_LEGACY_GIFT_MICRO_PER_SECOND`;
    - the owner (`plan = owner`) gets `_LEGACY_OWNER_MICRO`;
    - a paid subscription becomes the remainder of its seconds at the recognition price,
      exactly what the person paid for, but now as money. There were no paid subscriptions
      at the time of the move (the payment provider was off or a stub), but if there had been,
      the person must not lose them.

    It is called only at the moment the column is created, so it will not run a second time.
    After that the gift column is zeroed: nobody reads it any more, and a non-zero value
    would tempt someone to recalculate again.
    """
    from argus.server import pricing

    now = time.time()
    has_subs = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone() is not None

    per_second = pricing.charged(pricing.stt_cost(1))
    for acc in db.execute("SELECT id, free_left FROM accounts").fetchall():
        grants: list[tuple[str, int]] = []
        if acc["free_left"] > 0:
            grants.append(("gift", int(acc["free_left"]) * _LEGACY_GIFT_MICRO_PER_SECOND))
        if has_subs:
            sub = db.execute(
                "SELECT plan, until, seconds_left FROM subscriptions WHERE account_id = ?",
                (acc["id"],),
            ).fetchone()
            if sub is not None and sub["plan"] == "owner":
                grants.append(("grant", _LEGACY_OWNER_MICRO))
            elif sub is not None and sub["until"] > now and sub["seconds_left"] > 0:
                grants.append(("legacy", int(sub["seconds_left"]) * per_second))
        for kind, micro in grants:
            db.execute(
                "INSERT INTO credits (account_id, kind, micro, note, at) VALUES (?, ?, ?, 'recalculation', ?)",
                (acc["id"], kind, micro, now),
            )
            db.execute(
                "UPDATE accounts SET balance_micro = balance_micro + ? WHERE id = ?",
                (micro, acc["id"]),
            )
    db.execute("UPDATE accounts SET free_left = 0")
