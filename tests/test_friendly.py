"""An error shown to a person as a clear phrase, with no traces of what is under the hood
of the application.

Raw exception text ("RateLimitError: Error code: 429", "[Errno 11001] getaddrinfo
failed", an environment variable name) does not reach the interface. The application's
own short phrases, written for people, pass as they are.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from argus.core import friendly

# What must not be in text meant for people.
FORBIDDEN = (
    "openai", "anthropic", "claude", "gpt", "whisper", "elevenlabs", "errno", "error",
    "exception", "traceback", "http", "api", "key", "websocket", "json", "getaddrinfo",
)


class _Sdk(Exception):
    """Like the exceptions of the openai/anthropic clients: the response code lies in
    `status_code`.
    """

    def __init__(self, text: str, status_code: int) -> None:
        super().__init__(text)
        self.status_code = status_code


def test_own_phrase_passes_through():
    assert friendly.explain(RuntimeError("window not found")) == "window not found"
    assert friendly.scrub("busy with another program") == "busy with another program"
    assert friendly.plain_or(RuntimeError("the panel did not appear in time"), "-") \
        == "the panel did not appear in time"


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), friendly.NETWORK),
        (ConnectionResetError("Connection reset by peer"), friendly.NETWORK),
        (OSError("server rejected WebSocket connection"), friendly.NETWORK),
        # Windows in Russian talks about the network in Russian, and "подключение" (connection)
        # contains "ключ" (key). Language data: a localized Windows message.
        (ConnectionRefusedError("[WinError 1225] Удалённый компьютер отклонил это сетевое подключение"),
         friendly.NETWORK),
        (asyncio.TimeoutError(), friendly.TIMEOUT),
        (httpx.ReadTimeout("The read operation timed out"), friendly.TIMEOUT),
        (_Sdk("RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit'}}", 429),
         friendly.BUSY),
        (_Sdk("AuthenticationError: Incorrect API key provided: sk-123", 401), friendly.ACCESS),
        (_Sdk("APIStatusError: Error code: 529 - overloaded_error", 529), friendly.DOWN),
        (_Sdk("InternalServerError: Error code: 503", 503), friendly.DOWN),
        (RuntimeError("no key: the OPENAI_API_KEY environment variable is empty."), friendly.ACCESS),
        (ValueError("invalid literal for int() with base 10: 'abc'"), friendly.OTHER),
        (AttributeError("'NoneType' object has no attribute 'model'"), friendly.OTHER),
    ],
)
def test_service_errors_become_one_short_phrase(exc, expected):
    got = friendly.explain(exc)
    assert got == expected
    assert not any(word in got.lower() for word in FORBIDDEN), got


@pytest.mark.parametrize("word", ["подключение", "включить", "отключено", "переключатель"])
def test_a_key_inside_another_word_is_not_a_key_problem(word):
    """"Ключ" (key) is looked for at the start of a word: in "подключение" (connection) and
    "включить" (to turn on) it is not about an access key. The words stay Russian: they are
    language data (the access words include the Russian one for "key").
    """
    got = friendly.explain(RuntimeError(f"failure: {word} English words"), trust_text=False)
    assert got == friendly.OTHER


def test_a_russian_windows_network_error_is_a_network_problem_for_any_exception_type():
    # Language data: a localized (Russian) Windows message.
    exc = ConnectionResetError("[WinError 10054] Удалённый хост принудительно разорвал существующее подключение")
    assert friendly.explain(exc, trust_text=False) == friendly.NETWORK


def test_status_is_read_from_the_text_when_the_exception_does_not_carry_it():
    assert friendly.scrub("server rejected the connection: HTTP 503") == friendly.DOWN
    assert friendly.scrub("Error code: 429 - too many requests") == friendly.BUSY


@pytest.mark.parametrize(
    "text",
    [
        "RuntimeError: something broke",                             # exception name
        "no key: the OPENAI_API_KEY environment variable is empty",  # environment variable
        "the request to OpenAI failed",                              # service name
        "the server answered at http://203.0.113.10:8080",           # address
        "the file C:\\Users\\user\\argus.log did not open",         # path
        "x" * 400,                                                   # library output glued to a prefix
    ],
)
def test_text_with_internals_is_not_plain(text):
    assert not friendly.plain(text)
    assert friendly.scrub(text) in {
        friendly.NETWORK, friendly.TIMEOUT, friendly.ACCESS, friendly.BUSY,
        friendly.DOWN, friendly.OTHER,
    }


def test_hardware_errors_never_claim_a_network_problem():
    """Audio and screen break on the computer itself: "no connection" here would be a lie."""
    got = friendly.plain_or(OSError("[Errno -9999] Unanticipated host error"), "the stream stopped")
    assert got == "the stream stopped"
    assert friendly.plain_or(None, "the stream stopped") == "the stream stopped"


def test_nothing_to_say_is_still_a_sentence():
    assert friendly.explain(None) == friendly.OTHER
    assert friendly.scrub("") == friendly.OTHER
    assert friendly.scrub(None) == friendly.OTHER
