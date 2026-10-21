"""The app side: talking to the account server.

The client is tested on top of a real server, not a stub: this shows the whole path:
building the request, parsing the response, storing the token, behavior without a network.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from argus.account.client import Account, AccountError
from argus.server import billing, entitlements, pricing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"


# --------------------------------------------------------------------------
# Talking to the server
# --------------------------------------------------------------------------


@pytest.fixture
def account(tmp_path, monkeypatch):
    """A real client on top of a real server, but without a network.

    `httpx` is replaced with `TestClient` calls: this tests the whole path (building the
    request, parsing the response, storing the token), not a stub in place of the server.
    """
    db = connect(tmp_path / "accounts.sqlite")
    server = TestClient(build(db, SECRET, pay=billing.Stub()))

    def request(method, url, **kw):
        kw.pop("timeout", None)
        return server.request(method, url.replace("http://server", ""), **kw)

    monkeypatch.setattr("argus.account.client.httpx.request", request)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda url, **kw: request("POST", url, **kw))

    return Account("http://server", tmp_path), db


PASSWORD = "quite-a-long-password"


def sign_in(acc, email="user@example.com"):
    acc.register(email, PASSWORD)


def test_the_token_is_kept_and_survives_a_restart(account, tmp_path):
    acc, _ = account
    assert acc.signed_in is False
    sign_in(acc)
    assert acc.signed_in is True
    assert (tmp_path / "argus.account.json").exists()

    again = Account("http://server", tmp_path)
    assert again.signed_in is True
    assert again.email == "user@example.com"


def test_signing_out_removes_the_token(account, tmp_path):
    acc, _ = account
    sign_in(acc)
    acc.sign_out()
    assert acc.signed_in is False
    assert not (tmp_path / "argus.account.json").exists()


def test_the_state_shows_the_balance(account):
    acc, _ = account
    sign_in(acc)
    state = acc.state()
    assert state["signed_in"] is True
    assert state["right"]["allowed"] is True
    assert state["right"]["balance_micro"] == pricing.WELCOME_MICRO
    assert state["right"]["balance"] == "$0.30"


def test_a_refusal_arrives_as_text_for_a_human(account):
    """A person must read the reason, not an error code."""
    acc, db = account
    sign_in(acc)
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()

    right = acc.state()["right"]
    assert right["allowed"] is False
    assert "top up" in right["reason"]


def test_registering_twice_is_refused_with_a_reason(account):
    acc, _ = account
    sign_in(acc)
    with pytest.raises(AccountError) as e:
        acc.register("user@example.com", PASSWORD)
    assert "already exists" in str(e.value)


def test_request_reset_reaches_the_server_without_signing_in(account):
    """Recovery is the only action that does not need a sign-in: if it required one, a
    forgotten password could not be recovered."""
    acc, _ = account
    sign_in(acc)
    acc.forget()                              # as if the app had forgotten the sign-in
    acc.request_reset("user@example.com")      # does not raise, so it got through without a token


def test_no_network_is_shown_as_no_network_not_as_no_access(account, monkeypatch):
    """A failure caused by our own hiccup must not be shown as "access closed"."""
    acc, _ = account
    sign_in(acc)
    known = acc.state()["right"]

    import httpx

    def dead(*a, **kw):
        raise httpx.ConnectError("the network dropped")

    monkeypatch.setattr("argus.account.client.httpx.request", dead)
    state = acc.state()
    assert state["offline"] is True
    assert state["signed_in"] is True
    assert state["right"] == known  # we show the last known one


def test_a_dead_token_drops_the_login(account):
    """401 means the sign-in is no longer valid: a sign-in form is better than an eternal error."""
    acc, db = account
    sign_in(acc)
    db.execute("DELETE FROM sessions")
    db.commit()
    acc._access = ""  # force an exchange of the refresh token

    state = acc.state()
    assert acc.signed_in is False
    # And this is not "no connection": the person has to sign in again, not wait for the network.
    assert state["signed_in"] is False and state["offline"] is False


def test_a_top_up_reaches_the_client(account):
    acc, db = account
    sign_in(acc)
    entitlements.credit(db, 1, 10 * pricing.MICRO, "topup")
    right = acc.state()["right"]
    assert right["balance_micro"] == pricing.WELCOME_MICRO + 10 * pricing.MICRO


def test_the_price_list_is_readable_without_signing_in(account):
    """The price is shown to everyone — including someone still deciding whether to register."""
    acc, _ = account
    sheet = acc.pricing()
    assert sheet["payment"] == "stub"
    assert sheet["welcome"] == "$0.30"
    assert sheet["topups"] == list(pricing.TOPUP_PRESETS)


def test_checkout_asks_for_an_amount(account):
    acc, db = account
    sign_in(acc)
    order = acc.checkout(10)
    assert order["price"] == "$10"
    assert order["state"] == "new"


def test_checkout_tells_the_server_the_app_language(account, monkeypatch):
    """The checkout shows its payment page in the app's language; an empty language is not
    sent — an old server does not expect the extra field."""
    acc, _ = account
    sign_in(acc)
    sent = []
    real = acc._call

    def spy(method, path, **kw):
        sent.append((path, kw.get("json")))
        return real(method, path, **kw)

    monkeypatch.setattr(acc, "_call", spy)
    acc.checkout(10, "uk")
    acc.checkout(10)
    assert sent == [("/billing/checkout", {"dollars": 10, "lang": "uk"}),
                    ("/billing/checkout", {"dollars": 10})]


def test_checkout_refuses_an_amount_out_of_range_with_a_reason(account):
    acc, _ = account
    sign_in(acc)
    with pytest.raises(AccountError) as e:
        acc.checkout(2)
    assert "from $5" in str(e.value)


def test_reporting_recognition_charges_and_remembers_the_right(account):
    acc, db = account
    sign_in(acc)
    right = acc.report_stt(60)
    assert right["balance_micro"] == pricing.WELCOME_MICRO - pricing.charged(pricing.stt_cost(60))
    # And it is remembered as the last known one, until the next network poll.
    assert acc.known_right == right


def test_reporting_updates_the_fast_snapshot_too(account):
    """The dashboard reads the fast cache (`state_soon`), and it must learn that the money has
    run out immediately, not ten seconds later when the cache goes stale."""
    acc, db = account
    sign_in(acc)
    acc.state_soon()
    import time as _t
    deadline = _t.time() + 2
    while acc._soon is None and _t.time() < deadline:
        _t.sleep(0.01)
    assert acc._soon is not None

    db.execute("UPDATE accounts SET balance_micro = ?", (pricing.charged(pricing.stt_cost(60)),))
    db.commit()
    acc.report_stt(60)
    assert acc.state_soon()["right"]["allowed"] is False


def test_the_history_reaches_the_client(account):
    acc, db = account
    sign_in(acc)
    entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")
    rows = acc.history()["credits"]
    assert [r["kind"] for r in rows] == ["topup", "gift"]


def test_the_usage_series_reaches_the_client(account):
    acc, db = account
    sign_in(acc)
    acc.report_stt(60)
    out = acc.usage(180)
    assert len(out["daily"]) == 30
    assert sum(d["micro"] for d in out["daily"]) > 0


# --------------------------------------------------------------------------
# Terms, data copy, deletion
# --------------------------------------------------------------------------


def test_registration_sends_the_apps_terms_version(account):
    from argus.account.legal import TERMS_VERSION

    acc, db = account
    sign_in(acc)
    assert db.execute("SELECT terms_version FROM accounts").fetchone()[0] == TERMS_VERSION
    assert acc.state()["terms"] == TERMS_VERSION


def test_accepting_terms_is_remembered_on_the_server(account):
    acc, db = account
    # An account from before the terms: there is no version.
    from argus.server import accounts as server_accounts
    server_accounts.register(db, SECRET, "old@example.com", PASSWORD)
    acc.sign_in("old@example.com", PASSWORD)
    assert acc.state()["terms"] is None

    acc.accept_terms("2027-01-01")
    assert acc.state()["terms"] == "2027-01-01"


def test_export_returns_what_the_server_knows(account):
    acc, _ = account
    sign_in(acc)
    out = acc.export_data()
    assert out["account"]["email"] == "user@example.com"
    assert [c["kind"] for c in out["credits"]] == ["gift"]


def test_deleting_the_account_drops_the_local_login_too(account, tmp_path):
    acc, db = account
    sign_in(acc)
    acc.delete_account(PASSWORD)
    assert acc.signed_in is False
    assert not (tmp_path / "argus.account.json").exists()
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_a_wrong_password_does_not_delete_and_keeps_the_login(account):
    acc, db = account
    sign_in(acc)
    with pytest.raises(AccountError, match="password"):
        acc.delete_account("not-the-password")
    assert acc.signed_in is True
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


# --------------------------------------------------------------------------
# Password change (2026-10-08: the button in the "Account" tab)
# --------------------------------------------------------------------------


def test_changing_the_password_keeps_the_person_signed_in(account):
    # The server closes all sign-ins of the account, and our refresh token is dead after the
    # change. The client signs in again with the new password, otherwise the person would be
    # signed out of the app right after "Password changed".
    acc, _ = account
    sign_in(acc)
    old_refresh = acc._data["refresh"]

    acc.change_password(PASSWORD, "completely-new-password")

    assert acc.signed_in is True
    assert acc._data["refresh"] != old_refresh
    assert acc.state()["signed_in"] is True            # live sign-in: the server answers on the new token
    acc.sign_out()
    acc.sign_in("user@example.com", "completely-new-password")
    with pytest.raises(AccountError):
        acc.sign_in("user@example.com", PASSWORD)         # the old one is no longer valid


def test_a_wrong_current_password_does_not_change_anything(account):
    acc, _ = account
    sign_in(acc)
    with pytest.raises(AccountError, match="old password is incorrect"):
        acc.change_password("not-the-password-at-all", "completely-new-password")
    assert acc.signed_in is True
    acc.sign_out()
    acc.sign_in("user@example.com", PASSWORD)


def test_a_new_password_that_is_too_short_is_refused_with_the_reason(account):
    acc, _ = account
    sign_in(acc)
    with pytest.raises(AccountError, match="shorter"):
        acc.change_password(PASSWORD, "short")
    assert acc.signed_in is True


def test_the_local_password_route_answers_ok_only_when_it_worked():
    # The page accepts only an explicit `ok: true`: an app without this route answers with a body
    # without `ok`.
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "admin.py").read_text(encoding="utf-8")
    route = text[text.index('"/admin/account/password"'):text.index('"/admin/account/sign-in"')]
    assert 'return {"ok": True}' in route and 'return {"ok": False, "error": str(e)}' in route
    js = (Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static" / "account.js").read_text(encoding="utf-8")
    assert "if (out.ok !== true)" in js


# --------------------------------------------------------------------------
# Language of the emails
# --------------------------------------------------------------------------


def test_the_app_language_reaches_the_server_on_register_sign_in_and_reset(account):
    """Emails go out in the app's language: the client reports it on registration, sign-in and a
    recovery request.
    """
    acc, db = account
    acc.register("user@example.com", PASSWORD, lang="uk")
    assert db.execute("SELECT lang FROM accounts").fetchone()["lang"] == "uk"
    acc.sign_out()
    acc.sign_in("user@example.com", PASSWORD, lang="pl")
    assert db.execute("SELECT lang FROM accounts").fetchone()["lang"] == "pl"
    acc.request_reset("user@example.com", lang="de")
    assert db.execute("SELECT lang FROM accounts").fetchone()["lang"] == "de"


def test_without_a_language_nothing_is_sent_and_the_old_one_stays(account):
    acc, db = account
    acc.register("user@example.com", PASSWORD, lang="fr")
    acc.sign_out()
    acc.sign_in("user@example.com", PASSWORD)
    acc.request_reset("user@example.com")
    assert db.execute("SELECT lang FROM accounts").fetchone()["lang"] == "fr"


def test_the_local_routes_hand_the_app_language_to_the_client():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "admin.py").read_text(encoding="utf-8")
    for route, call in (('"/admin/account/register"', "acc.register"), ('"/admin/account/sign-in"', "acc.sign_in"),
                        ('"/admin/account/reset"', "acc.request_reset")):
        start = text.index(route)
        body = text[start:text.index("@web.", start + 10)]
        assert call in body and "lang=_mail_lang()" in body, route


# --------------------------------------------------------------------------
# Audit of 2026-10-16: the refresh token is readable only by the owner of the file
# --------------------------------------------------------------------------


def test_the_token_file_is_closed_to_other_users(tmp_path):
    import os
    import subprocess
    import sys

    from argus.account import client as account_client

    store = tmp_path / "argus.account.json"
    store.write_text("{}", encoding="utf-8")
    account_client._owner_only(store)

    if sys.platform == "win32":
        shown = subprocess.run(["icacls", str(store)], capture_output=True, text=True).stdout
        assert os.environ["USERNAME"].lower() in shown.lower()
        for stranger in ("Users", "Authenticated Users", "Everyone"):
            assert f"{stranger}:" not in shown.replace("BUILTIN\\", "").replace("NT AUTHORITY\\", ""), shown
        assert store.read_text(encoding="utf-8") == "{}"            # the owner can still read it
    else:
        assert (store.stat().st_mode & 0o077) == 0
