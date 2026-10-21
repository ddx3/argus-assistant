"""Terms and a person's data: acceptance at registration, a copy of the data, deletion.

What is checked is what really breaks: an account created without accepted terms; a
deleted account that still walks on its old pass; and the number of a deleted account that
goes to the next one together with someone else's history.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, pricing
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


def register(client, email="user@example.com", **over):
    body = {"email": email, "password": PASSWORD, "terms": TERMS, **over}
    return client.post("/auth/register", json=body)


def auth(body) -> dict:
    return {"Authorization": f"Bearer {body['access']}"}


# --------------------------------------------------------------------------
# Acceptance of the terms
# --------------------------------------------------------------------------


def test_registration_without_terms_is_refused(http, db):
    r = http.post("/auth/register", json={"email": "user@example.com", "password": PASSWORD})
    assert r.status_code == 422
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_registration_with_empty_terms_is_refused(http):
    assert register(http, terms="").status_code == 422


def test_the_accepted_version_and_time_are_stored(http, db):
    register(http)
    row = db.execute("SELECT terms_version, terms_at, created_at FROM accounts").fetchone()
    assert row["terms_version"] == TERMS
    assert row["terms_at"] == row["created_at"]


def test_me_reports_which_version_was_accepted(http):
    body = register(http).json()
    assert http.get("/me", headers=auth(body)).json()["terms"] == TERMS


def test_an_old_account_has_no_accepted_terms_until_it_accepts(http, db):
    # An account created before the terms existed: there is no version.
    login = accounts.register(db, SECRET, "old@example.com", PASSWORD)
    head = {"Authorization": f"Bearer {login.access}"}
    assert http.get("/me", headers=head).json()["terms"] is None

    assert http.post("/account/terms", json={"terms": "2027-01-01"}, headers=head).status_code == 204
    assert http.get("/me", headers=head).json()["terms"] == "2027-01-01"


def test_accepting_terms_needs_a_pass(http):
    assert http.post("/account/terms", json={"terms": TERMS}).status_code == 401


# --------------------------------------------------------------------------
# Copy of the data
# --------------------------------------------------------------------------


def test_export_has_the_account_and_its_money_but_no_secrets(http, db):
    body = register(http).json()
    out = http.get("/account/export", headers=auth(body)).json()

    assert out["account"]["email"] == "user@example.com"
    assert out["account"]["terms_version"] == TERMS
    assert out["account"]["balance_micro"] == pricing.WELCOME_MICRO
    assert [c["kind"] for c in out["credits"]] == ["gift"]
    # A password hash and tokens are the server's secrets, not the person's data.
    flat = str(out)
    assert "scrypt" not in flat
    assert body["refresh"] not in flat


def test_export_does_not_name_the_model_or_the_payment_provider(http, db):
    """The person gets what was used and how much, not which model answered or which payment
    provider took the payment.
    """
    from argus.server import entitlements

    body = register(http).json()
    account_id = db.execute("SELECT id FROM accounts").fetchone()["id"]
    entitlements.charge(db, account_id, "brain_tokens", 100, 5, model="gpt-5.6-luna")
    db.execute(
        "INSERT INTO orders (account_id, plan, amount, currency, provider, state, created_at) "
        "VALUES (?, 'topup', 500, 'USD', 'liqpay', 'paid', 1)", (account_id,),
    )
    db.commit()

    out = http.get("/account/export", headers=auth(body)).json()

    assert out["usage"] and out["orders"]
    assert all("model" not in row for row in out["usage"])
    assert all("provider" not in row for row in out["orders"])
    flat = str(out).lower()
    assert "gpt" not in flat and "liqpay" not in flat


def test_export_only_has_the_callers_own_rows(http):
    a = register(http, "a@example.com").json()
    register(http, "b@example.com")
    out = http.get("/account/export", headers=auth(a)).json()
    assert out["account"]["email"] == "a@example.com"
    assert len(out["credits"]) == 1


def test_export_needs_a_pass(http):
    assert http.get("/account/export").status_code == 401


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------


def test_deletion_needs_the_right_password(http, db):
    body = register(http).json()
    r = http.post("/account/delete", json={"password": "not-that-password"}, headers=auth(body))
    assert r.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_deletion_removes_the_person(http, db):
    body = register(http).json()
    r = http.post("/account/delete", json={"password": PASSWORD}, headers=auth(body))
    assert r.status_code == 204

    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == 0
    # The email can be taken again: there is no account with it any more.
    assert register(http).status_code == 200


def test_a_deleted_account_cannot_sign_in_or_refresh(http):
    body = register(http).json()
    http.post("/account/delete", json={"password": PASSWORD}, headers=auth(body))

    assert http.post("/auth/sign-in", json={"email": "user@example.com", "password": PASSWORD}
                     ).status_code == 403
    assert http.post("/auth/refresh", json={"refresh": body["refresh"]}).status_code == 401


def test_the_old_pass_stops_working_at_once(http):
    # A short pass lives fifteen minutes, but after deletion it is no good.
    body = register(http).json()
    http.post("/account/delete", json={"password": PASSWORD}, headers=auth(body))
    assert http.get("/me", headers=auth(body)).status_code == 401
    assert http.post("/meter/stt", json={"seconds": 5}, headers=auth(body)).status_code == 401


def test_money_history_survives_but_belongs_to_nobody(http, db):
    body = register(http).json()
    http.post("/account/delete", json={"password": PASSWORD}, headers=auth(body))

    rows = db.execute("SELECT account_id, kind FROM credits").fetchall()
    assert [(r["account_id"] < 0, r["kind"]) for r in rows] == [(True, "gift")]


def test_the_next_person_does_not_inherit_a_deleted_accounts_history(http, db):
    # `INTEGER PRIMARY KEY` without AUTOINCREMENT gave the number of a deleted row to the next
    # one, and with it someone else's top-ups and a still-live pass. Now the number is not
    # reused.
    first = register(http, "first@example.com").json()
    first_id = db.execute("SELECT id FROM accounts").fetchone()["id"]
    http.post("/account/delete", json={"password": PASSWORD}, headers=auth(first))

    second = register(http, "second@example.com").json()
    second_id = db.execute("SELECT id FROM accounts WHERE email_key = 'second@example.com'"
                           ).fetchone()["id"]
    assert second_id != first_id                      # the number did not go to the next one
    out = http.get("/account/export", headers=auth(second)).json()
    assert [c["kind"] for c in out["credits"]] == ["gift"]
    assert len(out["credits"]) == 1
    # And the erased person's pass does not work for the new person.
    assert http.get("/me", headers=auth(first)).status_code == 401


def test_deleting_one_account_leaves_the_others_alone(http, db):
    a = register(http, "a@example.com").json()
    b = register(http, "b@example.com").json()
    http.post("/account/delete", json={"password": PASSWORD}, headers=auth(a))

    assert http.get("/me", headers=auth(b)).status_code == 200
    assert db.execute("SELECT email FROM accounts").fetchone()["email"] == "b@example.com"
