"""The person's address behind nginx: the proxy secret and a forged `X-Forwarded-For`.

Attempt counters (login, registration, recovery) are kept per address. The server now
sits behind nginx (`api.arguspilot.com`), and without trusting it all people look like
one address, so the first person guessing passwords would lock everyone out of login. But
the old direct address `http://IP:8421` stays for builds that were already handed out, and
"always trust the header" would let someone forge it there and bypass the counter. So
nginx signs the request with a secret header, and only a signed request is trusted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.server import limits
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PROXY = "proxy-secret-from-nginx-0123456789abcdef"      # in production it is a random hex string (headers are ASCII only)
PASSWORD = "reasonably-long-password"


class Req:
    def __init__(self, peer="10.0.0.9", **headers):
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}
        self.client = type("C", (), {"host": peer})() if peer else None


def test_without_trust_the_forwarded_header_is_ignored():
    assert limits.client_ip(Req(x_forwarded_for="1.2.3.4")) == "10.0.0.9"


def test_plain_trust_takes_the_last_forwarded_address():
    assert limits.client_ip(Req(x_forwarded_for="6.6.6.6, 1.2.3.4"), True) == "1.2.3.4"


def test_with_a_secret_only_a_signed_request_is_trusted():
    signed = Req(x_forwarded_for="1.2.3.4", x_argus_proxy=PROXY)
    assert limits.client_ip(signed, PROXY) == "1.2.3.4"
    assert limits.client_ip(Req(x_forwarded_for="1.2.3.4"), PROXY) == "10.0.0.9", "no signature: it came past nginx"
    assert limits.client_ip(Req(x_forwarded_for="1.2.3.4", x_argus_proxy="foreign"), PROXY) == "10.0.0.9"


def test_a_signed_request_without_a_forwarded_address_falls_back_to_the_peer():
    assert limits.client_ip(Req(x_argus_proxy=PROXY), PROXY) == "10.0.0.9"


def test_no_peer_and_no_header_is_none():
    assert limits.client_ip(Req(peer=None), PROXY) is None


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


def signup(client, n: int, headers: dict | None = None):
    return client.post("/auth/register", json={"email": f"user{n}@example.com", "password": PASSWORD, "terms": "t"},
                       headers=headers or {})


def test_spoofing_the_forwarded_header_on_the_direct_port_does_not_dodge_the_signup_limit(db, monkeypatch):
    """Direct port: every request with a "new address" in the header still counts as one person."""
    monkeypatch.setenv("ARGUS_PROXY_SECRET", PROXY)
    with TestClient(build(db, SECRET)) as client:
        codes = [signup(client, n, {"X-Forwarded-For": f"9.9.9.{n}"}).status_code for n in range(25)]
    assert 429 in codes, "the sign-up counter was bypassed by forging the header"


def test_through_nginx_each_person_has_their_own_limit(db, monkeypatch):
    """Through nginx (the header is signed), people from different addresses do not get in each
    other's way.
    """
    monkeypatch.setenv("ARGUS_PROXY_SECRET", PROXY)
    with TestClient(build(db, SECRET)) as client:
        codes = [signup(client, n, {"X-Forwarded-For": f"9.9.9.{n}", "X-Argus-Proxy": PROXY}).status_code
                 for n in range(25)]
    assert 429 not in codes and set(codes) == {200}
