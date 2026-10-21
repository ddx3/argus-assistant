"""An error for a person, in one understandable phrase.

The raw exception text ("RateLimitError: Error code: 429 …", "[Errno 11001]
getaddrinfo failed", an environment variable name, a service address) does not reach
the interface: it tells a person nothing, and it gives away what the app has under the
hood, which services, libraries and protocols stand behind it. The details stay in the
log, written by whoever caught the error.

Three functions for three cases:

* `explain(exc)`: it was not possible to reach a service (an answer, a recap, the
  account). A general phrase is chosen by the kind of error: "no connection", "no
  access" and so on;
* `scrub(text)`: the same for a ready-made string (a reason from a background thread);
* `plain_or(x, fallback)`: something failed on the computer itself (audio, screen,
  window). General phrases about the network would be a lie here, so it is either our
  own phrase or the one the caller named.

A string written for people (a short plain sentence without traces of internals) passes as it
is: our own "window not found" and "busy with another program" are better than any general phrase.

The phrases are deliberately short and have no "try again" advice: they appear on the
answer card, in the complaints strip and in the account forms, where the informal and
formal ways of addressing the reader differ.
"""

from __future__ import annotations

import asyncio
import re
import socket

NETWORK = "no connection"
TIMEOUT = "no answer came in time"
ACCESS = "no access"
BUSY = "too many requests in a row"
DOWN = "the service is unavailable right now"
OTHER = "something went wrong"

# Beyond this, "written for people" no longer happens: a long string is almost always a
# library's output glued to our prefix.
PLAIN_MAX = 160

# Traces of internals: names of exceptions and codes, addresses, files, environment
# variables, names of services and libraries. A single such trace is enough to keep the
# string from being shown.
_LEAK = re.compile(
    r"Error\b|Exception\b|Errno|Traceback|\bHTTP\b|\bAPI\b|\bJSON\b|"
    r"https?://|wss?://|\.py\b|[A-Za-z]:[\\/]|\\\\|0x[0-9a-fA-F]{2,}|"
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|"
    r"OpenAI|Anthropic|Claude|GPT|Whisper|ElevenLabs|Realtime|WebSocket|WebView|"
    # The idioms of Python's own messages ("invalid literal for int() ...", "'NoneType' object has no attribute").
    r"\w\(\)|NoneType|__\w+__|\bobject has\b|\bunexpected keyword\b|\bpositional argument|"
    r"WASAPI|windows-capture|\bOCR\b|\bSTT\b|\bTTS\b|\bVAD\b|\bonnx|"
    # Paths on Mac and Linux ("/Users/name/Library/…"): an absolute path of two or more parts
    # that starts after a space, bracket or quote. "and/or" and "1/2" do not match.
    r"(?:^|[\s(\[«\"'])/[\w.\-]+(?:/[\w.\-]+)+|"
    # Addresses: IPv4 (with or without a port), "host:port" with a Latin name (a time like
    # "12:30" does not match), localhost.
    r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\blocalhost\b|"
    r"\b(?=[\w.\-]*[A-Za-z])[\w.\-]+:\d{2,5}\b|"
    # Mail, keys and passes: sk-…, "Bearer …", a three-part token (JWT), UUID.
    r"[\w.+\-]+@[\w\-]+\.[\w.\-]+|\bsk-[\w\-]{8,}|\bBearer\b|"
    r"\beyJ[\w\-]{5,}\.[\w\-]+\.[\w\-]+|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)

# «Error code: 429», «HTTP 503», «status 401», «status_code=500».
_STATUS_IN_TEXT = re.compile(
    r"(?:error code|http|status(?:[ _]code)?|code)\D{0,3}([45]\d\d)\b", re.I
)

# The word "ключ" (key) is matched from the start of the word: otherwise "по**ключ**ение"
# (connection) and "в**ключ**ить" (turn on) in a localized Windows message about a
# network failure ("The remote computer refused this network connection") would pass for
# a problem with a key. "No permission to ..." is our own macOS wording (mac/permissions.py).
_ACCESS_WORDS = re.compile(
    r"\bключ|\bkey\b|_key\b|api[ _-]?key|unauthori[sz]ed|forbidden|authenticat|(?<!no )permission", re.I
)
_NETWORK_WORDS = (
    "connect", "getaddrinfo", "name or service", "network", "unreachable", "dns", "ssl",
    "certificate", "handshake", "websocket", "socket", "reset by peer", "broken pipe",
    "disconnected", "eof occurred", "remote end closed", "no connection",
)


def plain(text: str) -> bool:
    """Written for people: short, without traces of the internals, and not a library's description
    of a network or access failure (those are better answered by a general phrase, see `_classify`)."""
    text = text.strip()
    if not text or len(text) > PLAIN_MAX:
        return False
    low = text.lower()
    if _ACCESS_WORDS.search(low) or any(w in low for w in _NETWORK_WORDS) or "timed out" in low or "timeout" in low:
        return False
    return _LEAK.search(text) is None


def explain(exc: BaseException | None, *, trust_text: bool = True) -> str:
    """It was not possible to reach a service; here is what can be shown to the person.

    `trust_text=False` means do not show even an exception text written in Russian, and
    choose a general phrase by the kind of error. This is what recognition does, because its
    internal messages (such as "the session did not start") tell a person nothing.
    """
    if exc is None:
        return OTHER
    text = str(exc).strip()
    if trust_text and plain(text):
        return text
    return _classify(exc, text)


def scrub(text: str | None) -> str:
    """A ready string: show it if it is meant for people, otherwise a generic phrase."""
    text = (text or "").strip()
    if plain(text):
        return text
    return _classify(None, text)


def plain_or(what: BaseException | str | None, fallback: str) -> str:
    """Our own phrase if it is meant for people, otherwise the one the caller named.

    For failures on the computer itself (audio, screen, windows) the general "no connection"
    and "no access" would be a lie here.
    """
    text = str(what).strip() if what is not None else ""
    return text if plain(text) else fallback


# --------------------------------------------------------------------------


def _status(exc: BaseException | None, text: str) -> int | None:
    """The service's response code: from the exception itself (the httpx, openai and anthropic
    clients keep it in `status_code` or `response.status_code`) or from the text."""
    if exc is not None:
        for obj in (exc, getattr(exc, "response", None)):
            code = getattr(obj, "status_code", None)
            if isinstance(code, int):
                return code
    found = _STATUS_IN_TEXT.search(text)
    return int(found.group(1)) if found else None


def by_status(code: int) -> str:
    """A generic phrase by the service's response code, for when the response has no text of its own."""
    if code in (401, 403):
        return ACCESS
    if code in (408, 504):
        return TIMEOUT
    if code == 429:
        return BUSY
    if code >= 500:
        return DOWN
    return OTHER


def _classify(exc: BaseException | None, text: str) -> str:
    code = _status(exc, text)
    if code is not None:
        return by_status(code)

    name = type(exc).__name__.lower() if exc is not None else ""
    hay = f"{name} {text}".lower()

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in hay or "timed out" in hay:
        return TIMEOUT
    if "rate limit" in hay or "ratelimit" in hay or "too many" in hay or "quota" in hay:
        return BUSY
    if "overloaded" in hay or "unavailable" in hay:
        return DOWN
    if _ACCESS_WORDS.search(hay):
        return ACCESS
    if isinstance(exc, (ConnectionError, socket.gaierror)) or any(w in hay for w in _NETWORK_WORDS):
        return NETWORK
    return OTHER
