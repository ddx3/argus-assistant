"""Manual balance top-up: not a product and not an HTTP handle, but it must really put money
in and leave a trace in the journal.
"""

from __future__ import annotations

import pytest

from argus.server import accounts, entitlements, grant, pricing
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


def test_grant_puts_the_money_on_the_balance(db):
    accounts.register(db, SECRET, "user@example.com", "fairly-long-password")
    before = entitlements.balance(db, 1)

    grant.grant(db, "user@example.com", 25)

    assert entitlements.balance(db, 1) == before + 25 * pricing.MICRO
    assert entitlements.right(db, 1).allowed


def test_grant_defaults_to_a_hundred_dollars(db):
    accounts.register(db, SECRET, "user@example.com", "fairly-long-password")
    before = entitlements.balance(db, 1)
    grant.grant(db, "user@example.com")
    assert entitlements.balance(db, 1) == before + 100 * pricing.MICRO


def test_grant_leaves_a_trace_in_the_journal(db):
    """Money without a journal entry is money of unknown origin."""
    accounts.register(db, SECRET, "user@example.com", "fairly-long-password")
    grant.grant(db, "user@example.com", 5)
    row = db.execute("SELECT kind, micro FROM credits WHERE kind = 'grant'").fetchone()
    assert row["micro"] == 5 * pricing.MICRO


def test_grant_needs_an_existing_account(db):
    with pytest.raises(SystemExit):
        grant.grant(db, "nobody-here@example.com", 5)


def test_a_grant_is_spent_like_any_other_money(db):
    """Granted money is ordinary money: it is spent at the same price list and it runs out."""
    accounts.register(db, SECRET, "user@example.com", "fairly-long-password")
    grant.grant(db, "user@example.com", 1)

    entitlements.charge(db, 1, "stt_seconds", 3600, pricing.stt_cost(3600))

    assert entitlements.balance(db, 1) < pricing.WELCOME_MICRO + 1 * pricing.MICRO
