"""The account client without a network and with a foreign response: only `AccountError`
comes out.

The access pass lives in memory, so the very first request after the app starts (and every
one after fifteen minutes) first exchanges the refresh token, which is a separate trip to
the network. Before, its failure escaped as a raw `httpx.ConnectError`: `/control` answered
500 instead of "no connection, let through", and the model, with no network, killed the
loop of answers to speech.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.account import client as client_module
from argus.account.client import Account, AccountError, json_of
from argus.core import friendly
from argus.server import billing
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "fairly-long-password"


def _restarted(tmp_path) -> Account:
    """The application after launch: the refresh token is on disk, the pass is not in memory."""
    (tmp_path / "argus.account.json").write_text(
        json.dumps({"refresh": "r", "email": "user@example.com"}), encoding="utf-8")
    acc = Account("http://server", tmp_path)
    assert acc._access == ""
    return acc


def _dead_network(monkeypatch):
    def dead(*a, **kw):
        raise httpx.ConnectError("[WinError 10061] No connection could be made because the target machine actively refused it")

    monkeypatch.setattr("argus.account.client.httpx.request", dead)
    monkeypatch.setattr("argus.account.client.httpx.post", dead)


# --------------------------------------------------------------------------
# Token exchange without a network
# --------------------------------------------------------------------------


def test_state_without_network_and_without_a_cached_pass_is_offline_not_a_crash(
        tmp_path, monkeypatch):
    """The only path where `_pass()` goes to the network: right after launch there is no cache
    yet.
    """
    acc = _restarted(tmp_path)
    _dead_network(monkeypatch)

    state = acc.state()

    assert state["offline"] is True
    assert state["signed_in"] is True
    assert state["error"] == friendly.NETWORK
    assert acc.signed_in is True          # the sign-in is not lost: there is no network, not no access


def test_every_authenticated_call_reports_a_dead_network_as_an_account_error(
        tmp_path, monkeypatch):
    acc = _restarted(tmp_path)
    _dead_network(monkeypatch)
    for call in (acc.history, acc.usage, lambda: acc.checkout(5), acc.export_data,
                 lambda: acc.report_stt(5), acc.access_token):
        with pytest.raises(AccountError) as e:
            call()
        assert str(e.value) == friendly.NETWORK


def test_the_error_of_the_network_library_is_not_shown_to_the_person(tmp_path, monkeypatch):
    acc = _restarted(tmp_path)
    _dead_network(monkeypatch)
    with pytest.raises(AccountError) as e:
        acc.access_token()
    assert "WinError" not in str(e.value) and "httpx" not in str(e.value).lower()


def test_a_dead_network_does_not_forget_the_login(tmp_path, monkeypatch):
    acc = _restarted(tmp_path)
    _dead_network(monkeypatch)
    acc.state()
    assert (tmp_path / "argus.account.json").exists()


def test_a_page_instead_of_json_is_no_connection_not_a_decode_error(tmp_path, monkeypatch):
    """A hotel Wi-Fi login page or a proxy stub answers 200 and HTML."""
    acc = _restarted(tmp_path)
    portal = httpx.Response(200, text="<html><body>Log in to the network</body></html>")
    monkeypatch.setattr("argus.account.client.httpx.post", lambda *a, **kw: portal)
    monkeypatch.setattr("argus.account.client.httpx.request", lambda *a, **kw: portal)

    state = acc.state()

    assert state["offline"] is True
    assert state["error"] == friendly.NETWORK


def test_a_refresh_answer_without_a_pass_is_an_account_error(tmp_path, monkeypatch):
    acc = _restarted(tmp_path)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda *a, **kw: httpx.Response(200, json={"oops": 1}))
    with pytest.raises(AccountError):
        acc.access_token()


def test_pressing_start_without_a_network_lets_the_person_in(tmp_path, monkeypatch):
    """`POST /control {running: true}`: "no connection — let them in" (`ui.server._no_right`).
    Previously the raw `ConnectError` from the token exchange never reached this branch, and
    the button answered 500."""
    from argus.app import Argus
    from argus.core.config import Config
    from argus.ui.server import _no_right

    (tmp_path / "argus.account.json").write_text(
        json.dumps({"refresh": "r", "email": "user@example.com"}), encoding="utf-8")
    cfg = Config()
    cfg.root = tmp_path
    cfg.account.server = "http://server"
    app = Argus(cfg, with_screen=False, with_brain=False)
    _dead_network(monkeypatch)

    assert _no_right(app) == ""                      # we let it through: this is our own network hiccup
    assert _no_right(app, soon=True) == ""


def test_a_bad_address_in_the_settings_is_an_account_error(tmp_path):
    """A typo in `[account] server` must not crash the callers with a raw exception."""
    (tmp_path / "argus.account.json").write_text(
        json.dumps({"refresh": "r", "email": "user@example.com"}), encoding="utf-8")
    acc = Account("not an address", tmp_path)
    state = acc.state()
    assert state["offline"] is True


def test_json_of_wants_an_object_with_the_expected_fields():
    ok = httpx.Response(200, json={"a": 1})
    assert json_of(ok, "a") == {"a": 1}
    with pytest.raises(AccountError):
        json_of(httpx.Response(200, text="<html>"))
    with pytest.raises(AccountError):
        json_of(httpx.Response(200, json=[1, 2]))
    with pytest.raises(AccountError):
        json_of(ok, "b")


# --------------------------------------------------------------------------
# A short pass is revoked, the refresh token is alive
# --------------------------------------------------------------------------


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A real client on top of a real server, but without a network."""
    db = connect(tmp_path / "accounts.sqlite")
    server = TestClient(build(db, SECRET, pay=billing.Stub()))
    seen: list[tuple[str, str, object]] = []

    def request(method, url, **kw):
        seen.append((method, url.replace("http://server", ""), kw.pop("timeout", None)))
        return server.request(method, url.replace("http://server", ""), **kw)

    monkeypatch.setattr("argus.account.client.httpx.request", request)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda url, **kw: request("POST", url, **kw))
    acc = Account("http://server", tmp_path)
    acc.register("user@example.com", PASSWORD)
    return acc, seen


def test_a_rejected_pass_is_exchanged_again_instead_of_forgetting_the_login(served, tmp_path):
    """The server was deployed with a different secret, or the clock shifted: the short pass is
    no good, but the refresh token is alive. Previously the first 401 erased the sign-in —
    and the password had to be entered again."""
    acc, _ = served
    acc._access = "garbage-pass"
    acc._access_until = time.time() + 900

    state = acc.state()

    assert acc.signed_in is True
    assert state["signed_in"] is True and state["offline"] is False
    assert (tmp_path / "argus.account.json").exists()
    assert acc._access != "garbage-pass"


def test_a_dead_refresh_token_still_drops_the_login(served):
    acc, _ = served
    acc._data["refresh"] = "dead-token"
    acc._access = "garbage-pass"
    acc._access_until = time.time() + 900

    state = acc.state()

    assert acc.signed_in is False
    assert state["signed_in"] is False and state["offline"] is False


def test_a_refusal_after_a_fresh_pass_does_not_forget_the_login(served, monkeypatch):
    """A fresh pass, yet the server still refuses: this is not "the sign-in is dead" — the
    refresh token has just worked."""
    acc, _ = served
    real = client_module.httpx.request

    def refuse_me(method, url, **kw):
        if url.endswith("/me"):
            return httpx.Response(401, json={"detail": "sign-in required"})
        return real(method, url, **kw)

    monkeypatch.setattr("argus.account.client.httpx.request", refuse_me)

    state = acc.state()

    assert acc.signed_in is True
    assert state["signed_in"] is True


# --------------------------------------------------------------------------
# How long we wait for an answer
# --------------------------------------------------------------------------


def test_the_relay_gets_a_long_wait_and_everything_else_the_short_one(served):
    """A paid answer arrives when the upstream has finished; the server completes it and
    charges even if the client has left. Hanging up before the server means paying for an
    answer you did not see."""
    acc, seen = served
    acc.state()
    with pytest.raises(AccountError):
        acc._call("POST", "/relay/brain/ask", auth=True, json={})     # 422: the body is empty
    me = next(t for m, p, t in seen if p == "/me")
    relay = next(t for m, p, t in seen if p == "/relay/brain/ask")
    assert me == client_module.TIMEOUT_S
    assert isinstance(relay, httpx.Timeout)
    assert relay.read >= 150.0                    # the server's ceiling for a call is 150 s
    assert relay.connect == client_module.TIMEOUT_S


def test_the_waits_are_nested_server_inside_client_inside_the_assistant_guard():
    """Hanging up before the server means paying for an answer you did not see; and the
    assistant's guard must not fire before the call itself. When changing one timeout, do
    not forget the neighboring ones."""
    from argus.brain.assistant import ASK_GUARD_S
    from argus.server.relay import UPSTREAM_DEADLINE_S

    assert client_module.RELAY_READ_TIMEOUT_S > UPSTREAM_DEADLINE_S
    assert ASK_GUARD_S > client_module.RELAY_READ_TIMEOUT_S + client_module.TIMEOUT_S


def test_an_explicit_timeout_wins(served):
    acc, seen = served
    acc._call("GET", "/pricing", timeout=3.0)
    assert [t for m, p, t in seen if p == "/pricing"] == [3.0]


# --------------------------------------------------------------------------
# A foreign response for calls that do not parse the body
# --------------------------------------------------------------------------


def test_a_redirect_or_a_page_is_not_a_success_for_calls_without_a_body_too(
        tmp_path, monkeypatch):
    """A hotel Wi-Fi login answers any request with a redirect or a page. A call that does not
    parse the response body used to take this for "succeeded": the account was "deleted"
    only in the app, and stayed on the server."""
    acc = _restarted(tmp_path)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda *a, **kw: httpx.Response(200, json={"access": "a"}))
    for portal in (
        httpx.Response(302, headers={"location": "http://wifi.example/login"}),
        httpx.Response(200, html="<html>Log in to the network</html>"),
    ):
        monkeypatch.setattr("argus.account.client.httpx.request",
                            lambda *a, _p=portal, **kw: _p)
        with pytest.raises(AccountError) as e:
            acc.delete_account("password")
        assert str(e.value) == friendly.NETWORK
        assert acc.signed_in is True                  # no deletion happened, the sign-in is not erased
        assert (tmp_path / "argus.account.json").exists()
