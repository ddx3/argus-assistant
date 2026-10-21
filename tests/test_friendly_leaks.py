"""`friendly.plain` does not let through paths, addresses, email and keys written inside a
phrase.

The module promises that addresses, files and keys do not reach the interface, but it
checked only Windows paths with a backslash, `http://` and exception names: a string with
"/Users/<user>/…", a bare `203.0.113.10:8421`, an email or `sk-…` counted as "for people"
and was shown as is. The account server sends such texts in `detail`, and the Mac version
brings paths with a forward slash.
"""

from __future__ import annotations

import pytest

from argus.core import friendly

LEAKS = [
    "could not open /Users/user/Library/Application Support/Argus/argus.log",
    "could not open C:/Users/user/argus.log",
    "server 203.0.113.10:8421 is not responding",
    "server 203.0.113.10 is not responding",
    "server localhost:8420 is not responding",
    "could not get in touch with localhost",
    "host server:8421 is not responding",
    "the email to user@example.com was not sent",
    "invalid key sk-proj-AbCdEf0123456789",
    "header Bearer abc123def456 was not accepted",
    "pass eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl is not valid",
    "order 123e4567-e89b-12d3-a456-426614174000 was not found",
    "did not open (/home/user/.config/argus/argus.json)",
]


@pytest.mark.parametrize("text", LEAKS)
def test_text_with_an_address_path_or_secret_is_not_plain(text):
    assert not friendly.plain(text)
    assert friendly.scrub(text) in {
        friendly.NETWORK, friendly.TIMEOUT, friendly.ACCESS, friendly.BUSY,
        friendly.DOWN, friendly.OTHER,
    }
    assert friendly.plain_or(text, "fallback phrase") == "fallback phrase"


@pytest.mark.parametrize("text", [
    "window not found",
    "busy with another program",
    "the meeting is at 12:30 — the answer will come later",
    'choose "yes/no" and try again',
    "and/or the first, or the second",
    "the answer to 1/2 of the question did not come out",
    "5 more seconds until the retry",
    "top up the balance — $5 short",
    "Version: 2 — update the program",
])
def test_ordinary_text_with_slashes_numbers_and_colons_still_passes(text):
    assert friendly.plain(text)
    assert friendly.scrub(text) == text


def test_a_windows_forward_slash_path_is_caught_like_the_backslash_one():
    assert not friendly.plain("the file C:\\Users\\user\\argus.log did not open")
    assert not friendly.plain("the file D:/projects/argus/argus.toml did not open")


def test_a_server_detail_with_internals_never_reaches_the_person(tmp_path, monkeypatch):
    """The account server sends a `detail`; the client shows it only if it is "for people"."""
    import httpx

    from argus.account.client import Account, AccountError

    acc = Account("http://server", tmp_path)
    detail = "the account user@example.com is blocked (203.0.113.10:8421)"
    monkeypatch.setattr("argus.account.client.httpx.request",
                        lambda *a, **kw: httpx.Response(403, json={"detail": detail}))
    try:
        acc.pricing()
    except AccountError as e:
        assert "@" not in str(e) and "8421" not in str(e)
    else:
        raise AssertionError("the refusal should have come as an error")
