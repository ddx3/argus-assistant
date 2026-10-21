"""Operator service commands on the server: block an account, export data, erase on request
by email.

The privacy policy promises a copy of the data and deletion within 30 days on request by
email. The operator does not have the person's password, and the code meant for people
(`/account/export`, `/account/delete`) requires it. Without these commands we would have
to write SQL by hand and could easily leave someone else's history not re-linked.
"""

from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, block, entitlements, erase, export, grant, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "quite-a-long-password"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


def register(db, email="user@example.com"):
    return accounts.register(db, SECRET, email, PASSWORD)


def run(monkeypatch, module, *args, db_path=None):
    if db_path is not None:
        monkeypatch.setenv("ARGUS_SERVER_DB", str(db_path))
    monkeypatch.setattr(sys, "argv", [module.__name__, *args])
    return module.main()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_export_gives_the_operator_what_the_person_would_get(db):
    login = register(db)
    grant.grant(db, "user@example.com", 5)
    entitlements.charge(db, login.account_id, "stt_seconds", 60, pricing.stt_cost(60))

    data = export.export(db, "  USER@example.com ")
    assert data == accounts.export_data(db, login.account_id)
    assert data["account"]["email"] == "user@example.com"
    assert [c["kind"] for c in data["credits"]] == ["gift", "grant"]
    assert "scrypt" not in json.dumps(data), "the password hash is a server secret, not the person's data"


def test_export_of_an_unknown_address_is_a_clear_refusal(db):
    with pytest.raises(SystemExit, match="does not exist"):
        export.export(db, "nobody@example.com")
    with pytest.raises(SystemExit):
        export.export(db, "not-an-address")


def test_the_export_command_prints_json_in_the_readable_alphabet(db, tmp_path, monkeypatch, capsys):
    register(db)
    db.close()
    assert run(monkeypatch, export, "user@example.com", db_path=tmp_path / "accounts.sqlite") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["account"]["email"] == "user@example.com"


# --------------------------------------------------------------------------
# Erasure on request by email
# --------------------------------------------------------------------------


def test_erase_without_a_password_does_what_the_button_does(db):
    login = register(db)
    grant.grant(db, "user@example.com", 5)
    entitlements.charge(db, login.account_id, "tts_chars", 100, pricing.tts_cost(100))
    credits = db.execute("SELECT COUNT(*) FROM credits").fetchone()[0]
    usage = db.execute("SELECT COUNT(*) FROM usage").fetchone()[0]

    info = erase.erase(db, "user@example.com", confirmed=True)

    assert info["id"] == login.account_id and info["credits"] == credits
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    # The money and the usage remain, but without an owner, so the money report still adds up.
    assert db.execute("SELECT COUNT(*) FROM credits WHERE account_id = ?",
                      (-login.account_id,)).fetchone()[0] == credits
    assert db.execute("SELECT COUNT(*) FROM usage WHERE account_id = ?",
                      (-login.account_id,)).fetchone()[0] == usage
    assert db.execute("SELECT COUNT(*) FROM credits WHERE account_id > 0").fetchone()[0] == 0


def test_erase_shows_what_will_happen_and_changes_nothing_without_the_confirmation(db):
    register(db)
    info = erase.erase(db, "user@example.com")
    assert info["balance_micro"] == pricing.WELCOME_MICRO
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_erase_of_the_wrong_address_touches_nobody_else(db):
    register(db, "a@example.com")
    register(db, "b@example.com")
    with pytest.raises(SystemExit, match="does not exist"):
        erase.erase(db, "c@example.com", confirmed=True)
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


def test_the_next_person_never_gets_the_number_or_the_history_of_an_erased_account(db):
    old = register(db, "old@example.com")
    erase.erase(db, "old@example.com", confirmed=True)
    new = register(db, "new@example.com")
    assert new.account_id != old.account_id
    export_of_new = export.export(db, "new@example.com")
    assert [c["kind"] for c in export_of_new["credits"]] == ["gift"]


def test_the_erase_command_asks_before_it_deletes(db, tmp_path, monkeypatch, capsys):
    register(db)
    db.close()
    path = tmp_path / "accounts.sqlite"
    assert run(monkeypatch, erase, "user@example.com", db_path=path) == 0
    assert "will be erased" in capsys.readouterr().out
    assert connect(path).execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1

    assert run(monkeypatch, erase, "user@example.com", "--yes", db_path=path) == 0
    assert capsys.readouterr().out.startswith("erased")
    assert connect(path).execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_the_erase_command_needs_exactly_one_address(monkeypatch):
    for args in ((), ("a@example.com", "b@example.com")):
        monkeypatch.setattr(sys, "argv", ["erase", *args])
        with pytest.raises(SystemExit, match="email required"):
            erase.main()


# --------------------------------------------------------------------------
# Block an account
# --------------------------------------------------------------------------


def test_a_blocked_account_loses_its_live_pass_at_once(db):
    login = register(db)
    with TestClient(build(db, SECRET)) as http:
        head = {"Authorization": f"Bearer {login.access}"}
        assert http.get("/me", headers=head).status_code == 200

        assert block.block(db, "USER@example.com") == login.account_id
        assert http.get("/me", headers=head).status_code == 401
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        with pytest.raises(accounts.Denied, match="closed"):
            accounts.sign_in(db, SECRET, "user@example.com", PASSWORD)


def test_a_blocked_account_can_be_opened_again(db):
    register(db)
    block.block(db, "user@example.com")
    block.block(db, "user@example.com", undo=True)
    accounts.sign_in(db, SECRET, "user@example.com", PASSWORD)


def test_blocking_leaves_other_people_alone_and_knows_the_time(db):
    register(db, "a@example.com")
    other = register(db, "b@example.com")
    block.block(db, "a@example.com", now=1_790_000_000.0)
    rows = {r["email"]: r["blocked_at"] for r in db.execute("SELECT email, blocked_at FROM accounts")}
    assert rows == {"a@example.com": 1_790_000_000.0, "b@example.com": None}
    with TestClient(build(db, SECRET)) as http:
        assert http.get("/me", headers={"Authorization": f"Bearer {other.access}"}).status_code == 200


def test_blocking_an_unknown_address_is_a_clear_refusal(db):
    with pytest.raises(SystemExit, match="does not exist"):
        block.block(db, "nobody@example.com")


def test_the_block_command_says_what_it_did(db, tmp_path, monkeypatch, capsys):
    register(db)
    db.close()
    path = tmp_path / "accounts.sqlite"
    assert run(monkeypatch, block, "user@example.com", db_path=path) == 0
    assert "closed" in capsys.readouterr().out
    assert run(monkeypatch, block, "user@example.com", "--undo", db_path=path) == 0
    assert "open" in capsys.readouterr().out
