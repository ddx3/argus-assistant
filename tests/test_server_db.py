"""The server database: migrating the old schema, all or nothing; account numbers are not
reused.

The migration runs on the live database with people's money, so it is checked on a
database of the old shape: rows are not lost, numbers and values stay the same, a unique
email stays unique, and a migration cut off in the middle leaves no half-done result.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from argus.server import accounts, entitlements, pricing
from argus.server import db as dbmod
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"


def old_database(path, balance_column: bool = True) -> None:
    """A database as the previous version left it: `accounts` without AUTOINCREMENT and without the
    terms columns, `orders` without a refund mark, several rows with gaps in the numbers."""
    balance = ", balance_micro INTEGER NOT NULL DEFAULT 0" if balance_column else ""
    cols = ", balance_micro" if balance_column else ""

    def money(value: int) -> str:
        return f", {value}" if balance_column else ""

    old = sqlite3.connect(path)
    old.executescript(f"""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            email_key TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            created_at REAL NOT NULL,
            free_left INTEGER NOT NULL DEFAULT 0,
            blocked_at REAL{balance}
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, plan TEXT NOT NULL,
            amount INTEGER NOT NULL, currency TEXT NOT NULL, provider TEXT NOT NULL,
            external_id TEXT, state TEXT NOT NULL, created_at REAL NOT NULL, paid_at REAL
        );
        CREATE TABLE credits (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, kind TEXT NOT NULL,
            micro INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', at REAL NOT NULL
        );
        INSERT INTO accounts (id, email, email_key, password, created_at, blocked_at{cols})
        VALUES (1, 'A@example.com', 'a@example.com', 'scrypt$x', 10.0, NULL{money(123456)}),
               (2, 'b@example.com', 'b@example.com', 'scrypt$y', 20.0, 5.0{money(0)}),
               (7, 'c@example.com', 'c@example.com', 'scrypt$z', 30.0, NULL{money(999)});
        INSERT INTO orders (id, account_id, plan, amount, currency, provider, external_id,
                            state, created_at, paid_at)
        VALUES (1, 7, 'topup', 500, 'USD', 'liqpay', 'liqpay-1', 'paid', 31.0, 32.0);
        INSERT INTO credits (account_id, kind, micro, at) VALUES (7, 'topup', 5000000, 32.0);
    """)
    old.commit()
    old.close()


def table_sql(db, name: str) -> str:
    return db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]


# --------------------------------------------------------------------------
# Account numbers are not reused
# --------------------------------------------------------------------------


def test_a_fresh_database_never_hands_out_a_deleted_number_again(tmp_path):
    db = connect(tmp_path / "new.sqlite")
    a = accounts.register(db, SECRET, "a@example.com", PASSWORD)
    b = accounts.register(db, SECRET, "b@example.com", PASSWORD)
    accounts.delete_account(db, b.account_id, PASSWORD)
    c = accounts.register(db, SECRET, "c@example.com", PASSWORD)
    assert c.account_id > b.account_id > a.account_id, "the number of the deleted account went to the new one"


def test_an_old_database_is_rebuilt_and_loses_nothing(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    db = connect(path)

    assert "AUTOINCREMENT" in table_sql(db, "accounts").upper()
    rows = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    assert [(r["id"], r["email"], r["email_key"], r["password"], r["created_at"],
             r["blocked_at"], r["balance_micro"]) for r in rows] == [
        (1, "A@example.com", "a@example.com", "scrypt$x", 10.0, None, 123456),
        (2, "b@example.com", "b@example.com", "scrypt$y", 20.0, 5.0, 0),
        (7, "c@example.com", "c@example.com", "scrypt$z", 30.0, None, 999),
    ]
    # The columns that the old database lacked are added.
    assert {"terms_version", "terms_at"} <= set(rows[0].keys())
    assert "reversed_at" in {r["name"] for r in db.execute("PRAGMA table_info(orders)")}
    # Neighboring tables are untouched.
    order = db.execute("SELECT account_id, state FROM orders").fetchone()
    assert (order["account_id"], order["state"]) == (7, "paid")
    assert db.execute("SELECT SUM(micro) FROM credits").fetchone()[0] == 5_000_000
    assert db.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'accounts_old'").fetchone()


def test_after_the_rebuild_the_newest_number_is_not_reused(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    db = connect(path)
    db.execute("DELETE FROM accounts WHERE id = 7")
    db.commit()
    login = accounts.register(db, SECRET, "d@example.com", PASSWORD)
    assert login.account_id > 7


def test_the_mailbox_stays_unique_after_the_rebuild(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    db = connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO accounts (email, email_key, password, created_at) "
                   "VALUES ('x', 'a@example.com', 'p', 1)")
    db.rollback()
    with pytest.raises(accounts.Denied, match="already exists"):
        accounts.register(db, SECRET, "A@Example.com", PASSWORD)


def test_the_rebuild_happens_once(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    old_database(path)
    connect(path).close()

    def boom(db):
        raise AssertionError("a database of the right version must not be migrated again")

    monkeypatch.setattr(dbmod, "_never_reuse_ids", boom)
    monkeypatch.setattr(dbmod, "_add_missing_columns", boom)
    connect(path).close()


# --------------------------------------------------------------------------
# The migration is all or nothing
# --------------------------------------------------------------------------


def test_a_migration_cut_in_the_middle_leaves_the_old_database_whole(tmp_path, monkeypatch):
    """A column was added at once, but the recalculation of money waited for the end of `connect`:
    if the process dies between them, on the next start the column is already there, but the
    gifts were never recalculated."""
    path = tmp_path / "old.sqlite"
    old_database(path, balance_column=False)
    raw = sqlite3.connect(path)
    raw.execute("UPDATE accounts SET free_left = 300 WHERE id = 1")      # a gift from the subscription era
    raw.commit()
    raw.close()

    def dies(db):
        raise RuntimeError("the process died in the middle of the migration")

    monkeypatch.setattr(dbmod, "_to_balances", dies)
    with pytest.raises(RuntimeError):
        connect(path)

    after = sqlite3.connect(path)
    columns = {r[1] for r in after.execute("PRAGMA table_info(accounts)")}
    assert "balance_micro" not in columns, "the column stayed, and there was no recalculation"
    assert "AUTOINCREMENT" not in after.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'accounts'").fetchone()[0].upper()
    assert after.execute("PRAGMA user_version").fetchone()[0] == 0
    assert not after.execute("SELECT 1 FROM sqlite_master WHERE name = 'accounts_old'").fetchone()
    assert after.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3
    after.close()

    monkeypatch.undo()
    db = connect(path)                              # the next start finishes the job
    assert entitlements.balance(db, 1) == pricing.WELCOME_MICRO
    assert db.execute("SELECT COUNT(*) FROM credits WHERE kind = 'gift'").fetchone()[0] == 1


def test_the_version_is_set_in_the_same_transaction_as_the_migration(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    old_database(path)

    def dies(db):
        raise RuntimeError("died on the rebuild")

    monkeypatch.setattr(dbmod, "_never_reuse_ids", dies)
    with pytest.raises(RuntimeError):
        connect(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 0
    columns = {r[1] for r in check.execute("PRAGMA table_info(accounts)")}
    assert "terms_version" not in columns, "the terms column is left over from the interrupted migration"
    check.close()


# --------------------------------------------------------------------------
# Recognition report number (schema version 2)
# --------------------------------------------------------------------------


def version_one_database(path) -> None:
    """A database as version 1 left it: live, with spending, but without `usage.report_id` and its
    index.
    """
    db = connect(path)
    accounts.register(db, SECRET, "a@example.com", PASSWORD)
    entitlements.charge(db, 1, "stt_seconds", 12.5, 300, model="m")
    entitlements.charge(db, 1, "tts_chars", 100, 12)
    # A table of the old shape: rebuilt by hand (`DROP COLUMN` stumbles on a comment in the
    # schema).
    db.execute("DROP INDEX usage_report")
    db.execute("ALTER TABLE usage RENAME TO usage_new")
    db.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
               "kind TEXT NOT NULL, amount REAL NOT NULL, at REAL NOT NULL, "
               "model TEXT NOT NULL DEFAULT '', cost_micro INTEGER NOT NULL DEFAULT 0, "
               "charged_micro INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO usage SELECT id, account_id, kind, amount, at, model, cost_micro, "
               "charged_micro FROM usage_new")
    db.execute("DROP TABLE usage_new")
    db.execute("CREATE INDEX usage_by_account ON usage (account_id, at)")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()


def usage_columns(db) -> set[str]:
    return {r["name"] for r in db.execute("PRAGMA table_info(usage)")}


def report_index(db):
    return db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'usage_report'").fetchone()


def test_a_fresh_database_has_the_report_column_and_a_unique_index(tmp_path):
    db = connect(tmp_path / "new.sqlite")
    assert "report_id" in usage_columns(db)
    assert "UNIQUE" in report_index(db)["sql"].upper()
    assert db.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION == 3


def test_a_version_one_database_gets_the_column_and_the_index_and_keeps_its_rows(tmp_path):
    path = tmp_path / "v1.sqlite"
    version_one_database(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "report_id" not in {r[1] for r in check.execute("PRAGMA table_info(usage)")}
    check.close()

    db = connect(path)

    assert "report_id" in usage_columns(db)
    assert report_index(db) is not None
    assert db.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    rows = db.execute("SELECT kind, amount, model, cost_micro, charged_micro, report_id "
                      "FROM usage ORDER BY id").fetchall()
    assert [(r["kind"], r["amount"], r["model"], r["cost_micro"], r["charged_micro"], r["report_id"])
            for r in rows] == [
        ("stt_seconds", 12.5, "m", 300, 600, None),
        ("tts_chars", 100.0, "", 12, 24, None),
    ]
    # Money is untouched: the balance is the gift minus what was charged above.
    assert entitlements.balance(db, 1) == pricing.WELCOME_MICRO - 600 - 24


def test_after_the_migration_a_repeated_report_is_not_charged_again(tmp_path):
    path = tmp_path / "v1.sqlite"
    version_one_database(path)
    db = connect(path)
    before = entitlements.balance(db, 1)
    assert entitlements.charge(db, 1, "stt_seconds", 10, 500, report_id="after-migrate-1") == 1000
    assert entitlements.charge(db, 1, "stt_seconds", 10, 500, report_id="after-migrate-1") == 0
    assert entitlements.balance(db, 1) == before - 1000


def test_the_report_migration_is_all_or_nothing(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite"
    version_one_database(path)

    def dies(db):
        raise RuntimeError("died on the index")

    monkeypatch.setattr(dbmod, "_report_index", dies)
    with pytest.raises(RuntimeError):
        connect(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "report_id" not in {r[1] for r in check.execute("PRAGMA table_info(usage)")}, \
        "the column is left over from the interrupted migration"
    assert check.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 2
    check.close()

    monkeypatch.undo()
    db = connect(path)                                   # the next start finishes the job
    assert "report_id" in usage_columns(db) and report_index(db) is not None
    assert db.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION


def test_the_report_index_can_be_applied_again_without_harm(tmp_path):
    db = connect(tmp_path / "new.sqlite")
    dbmod._report_index(db)
    dbmod._report_index(db)
    db.commit()
    assert report_index(db) is not None


def test_a_database_older_than_version_one_also_lands_on_the_new_shape(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    db = connect(path)
    assert "report_id" in usage_columns(db) and report_index(db) is not None
    assert db.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION


def test_a_usage_table_of_an_old_shape_gets_the_column_added_by_the_migration(tmp_path):
    """`usage` also comes in a very old shape (before the price columns): both of them and the
    report number arrive with the migration.
    """
    path = tmp_path / "old.sqlite"
    old_database(path)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
                "kind TEXT NOT NULL, amount REAL NOT NULL, at REAL NOT NULL)")
    raw.execute("INSERT INTO usage (account_id, kind, amount, at) VALUES (7, 'stt_seconds', 3, 1)")
    raw.commit()
    raw.close()
    db = connect(path)
    assert {"model", "cost_micro", "charged_micro", "report_id"} <= usage_columns(db)
    assert db.execute("SELECT report_id FROM usage").fetchone()[0] is None
    assert report_index(db) is not None


def test_an_old_account_without_a_password_is_still_closed_by_the_migration(tmp_path):
    """Closing accounts without a password is part of the same move and was not lost."""
    path = tmp_path / "ancient.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT NOT NULL,
                               email_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
        INSERT INTO accounts (email, email_key, created_at)
        VALUES ('old@example.com', 'old@example.com', 0);
    """)
    old.commit()
    old.close()
    db = connect(path)
    row = db.execute("SELECT password, blocked_at FROM accounts").fetchone()
    assert row["password"] == "" and row["blocked_at"] is not None
    assert "AUTOINCREMENT" in table_sql(db, "accounts").upper()
    assert time.time() - row["blocked_at"] < 60


def test_a_column_left_from_an_older_era_is_carried_over_not_dropped(tmp_path):
    """A production database may hold columns of earlier eras (a device binding and so on): the code
    does not read them, but rebuilding the table must not silently throw the data away."""
    path = tmp_path / "old.sqlite"
    old_database(path)
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE accounts ADD COLUMN device_hash TEXT")
    raw.execute("ALTER TABLE accounts ADD COLUMN trial_until REAL NOT NULL DEFAULT 0")
    raw.execute("UPDATE accounts SET device_hash = 'abc', trial_until = 99.5 WHERE id = 7")
    raw.commit()
    raw.close()

    db = connect(path)
    row = db.execute("SELECT device_hash, trial_until, balance_micro FROM accounts WHERE id = 7"
                     ).fetchone()
    assert (row["device_hash"], row["trial_until"], row["balance_micro"]) == ("abc", 99.5, 999)
    assert "AUTOINCREMENT" in table_sql(db, "accounts").upper()
    # And new accounts are still created: the extra columns do not get in their way.
    accounts.register(db, SECRET, "new@example.com", PASSWORD)


def test_a_version_two_database_gets_the_language_and_notice_columns_and_keeps_its_accounts(tmp_path):
    """The production database was at version 2: without the bumped version number, the email
    language column would not have appeared (rehearsal, 2026-10-15).
    """
    path = tmp_path / "v2.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, "
                "email_key TEXT NOT NULL UNIQUE, password TEXT NOT NULL, created_at REAL NOT NULL, "
                "free_left INTEGER NOT NULL DEFAULT 0, blocked_at REAL, balance_micro INTEGER NOT NULL DEFAULT 0, "
                "terms_version TEXT, terms_at REAL)")
    old.execute("INSERT INTO accounts (email, email_key, password, created_at, balance_micro) "
                "VALUES ('a@example.com', 'a@example.com', 'x', 1.0, 300000)")
    old.execute("PRAGMA user_version = 2")
    old.commit()
    old.close()

    db = connect(path)

    columns = {r["name"] for r in db.execute("PRAGMA table_info(accounts)")}
    assert {"lang", "low_notified"} <= columns
    row = db.execute("SELECT email, lang, low_notified, balance_micro FROM accounts").fetchone()
    assert (row["email"], row["lang"], row["low_notified"], row["balance_micro"]) == ("a@example.com", "", 0, 300000)
    assert db.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION == 3
