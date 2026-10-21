"""A guard for the local server against foreign pages and a spoofed address.

The server listens only on the loopback interface, but "only loopback" does not mean
"only us". Any page in the person's browser can knock on `127.0.0.1:port`: with a form,
with `fetch(..., {mode: 'no-cors'})`, with an image. Such a request needs no permission
and no body: an empty POST goes through as it is. Without the guard, a foreign page
could ask the model (with a screenshot) at the person's expense, record sound, "accept"
the recording notice on the person's behalf, make the windows visible in screen
sharing, and plant files into the materials, which go into every question. And DNS
spoofing (`evil.example` -> 127.0.0.1) makes such a page "same-origin" for reading too:
chats, transcripts, settings.

Two rules, both in one ASGI layer, before the routes, for `http` and `websocket`:

* **Host**: only ours: `127.0.0.1`, `localhost`, `[::1]` and, if a specific `ui.host`
  is set in the settings, that one. The port is the one we listen on (or no port). DNS
  spoofing brings a foreign name into `Host`, and it does not pass.
* **Origin**: for everything that changes state (not GET/HEAD/OPTIONS) and for the
  WebSocket handshake: `Origin`, if present, must be ours, and `Sec-Fetch-Site`, if
  present, must be `same-origin` or `none`. A request with no `Origin` at all passes:
  that is how the hotkeys (`desktop.py`, urllib) and tests call, while a browser always
  sends `Origin` on a request that records.

A refusal is a 403 with a short phrase in English; the WebSocket is closed before
`accept()`. `frame-ancestors 'self'` is added to all responses: a foreign page cannot
embed our windows in its own frame to steer the person's clicks.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable

log = logging.getLogger(__name__)

# The names under which the person and the app window open their own server.
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

# Methods that do not change state. Only Host is checked for them.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Where a request from an app page can come from: from our own page, or from the person
# directly (the address bar, a bookmark). Everything else is foreign.
OWN_FETCH_SITES = frozenset({"same-origin", "none"})

DENIED_TEXT = "Request rejected: it didn't come from the app."

# How often a refusal goes into the log: the first one immediately, the rest are counted
# and written in one line.
LOG_EVERY_S = 30.0

# An address "name[:port]": a domain, IPv4, or IPv6 in brackets. Nothing extra, no login
# and no path: such values do not come in Host and Origin from a browser.
_AUTHORITY = re.compile(r"^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::([0-9]{1,5}))?$")

_WATCHED = (b"host", b"origin", b"sec-fetch-site", b"sec-fetch-mode")
_FRAME_POLICY = (b"content-security-policy", b"frame-ancestors 'self'")


def parse_authority(value: str) -> tuple[str, int | None] | None:
    """`127.0.0.1:8420` -> (`127.0.0.1`, 8420); not an address — `None`."""
    found = _AUTHORITY.match(value.strip().lower())
    if found is None:
        return None
    return found.group(1), int(found.group(2)) if found.group(2) else None


def _bracketed(host: str) -> str:
    """An IPv6 address in Host is written in brackets: `::1` -> `[::1]`."""
    host = host.strip().lower()
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _concrete(host: str) -> bool:
    """A concrete address, not "listen on all": `0.0.0.0` and `::` cannot be a name."""
    return host.strip().lower() not in ("", "0.0.0.0", "::", "[::]", "*")


class LocalGuard:
    """ASGI layer: lets only the application's own windows reach it."""

    def __init__(self, app, *, port: int | None = None, hosts: Iterable[str] = ()) -> None:
        self.app = app
        # The port from the settings is a fallback: the real port comes in the request itself
        # (`scope["server"]`), and starting with a different `--port` does not interfere with the
        # check.
        self.port = port
        self.hosts = frozenset(
            {*LOOPBACK_NAMES, *(_bracketed(h) for h in hosts if _concrete(h))}
        )
        self._noted = float("-inf")          # when a refusal was last written to the log
        self._muted = 0                      # how many refusals have been silenced since then

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        verdict = self.verdict(scope)
        if verdict:
            self._note(verdict)
            await self._refuse(scope, receive, send)
            return
        if kind == "http":
            send = _with_frame_policy(send)
        await self.app(scope, receive, send)

    def _note(self, verdict: str) -> None:
        """Write to the log no more than once per `LOG_EVERY_S`: a page in a loop would fill it with
        thousands of lines.
        """
        now = time.monotonic()
        if now - self._noted < LOG_EVERY_S:
            self._muted += 1
            return
        more = f" (and {self._muted} more like it)" if self._muted else ""
        log.warning("rejected a foreign request: %s%s", verdict, more)
        self._noted, self._muted = now, 0

    # -- decision ----------------------------------------------------------

    def verdict(self, scope) -> str:
        """Empty — let the request in. Otherwise — a short reason the request was rejected (for the
        log).
        """
        seen: dict[bytes, str | None] = {}
        for key, value in scope.get("headers", ()):
            if key in _WATCHED:
                # Two identical headers: different parsers will pick different ones, so we trust neither.
                seen[key] = None if key in seen else value.decode("latin-1")
        # The path is in quotes through `repr`: a page could plant a newline in it and pass its
        # own line off as a log line.
        where = f"{scope.get('method', 'WS')} {str(scope.get('path', ''))[:80]!r}"
        ports = self._ports(scope)

        host = seen.get(b"host")
        if host is None or not self._ours(parse_authority(host), ports, port_required=False):
            return f"{where}, Host={(host or '')[:80]!r}"

        method = scope.get("method", "GET").upper()
        if scope["type"] == "http" and method in SAFE_METHODS:
            # A foreign page cannot show what it reads (there is no CORS), but it can make us do work:
            # `<img src="http://127.0.0.1:port/screen.mjpg">` starts screen capture and encoding for
            # every tag, and `/windows?thumbs=true` captures all the windows. We reject subresources of
            # foreign pages (an image, a script, `fetch`); navigation by a link (`navigate`) and our
            # own pages pass.
            site = seen.get(b"sec-fetch-site")
            mode = seen.get(b"sec-fetch-mode")
            # A browser always sends `Sec-Fetch-Mode` together with `Sec-Fetch-Site`; without it (not
            # a browser) we let the request pass.
            if site is not None and site not in OWN_FETCH_SITES and mode is not None and mode != "navigate":
                return f"{where}, Sec-Fetch-Site={(site or '')[:20]!r}"
            return ""
        if b"origin" in seen:
            origin = seen[b"origin"]
            if origin is None or not self._own_origin(origin, ports):
                return f"{where}, Origin={(origin or '')[:80]!r}"
        if b"sec-fetch-site" in seen and seen[b"sec-fetch-site"] not in OWN_FETCH_SITES:
            return f"{where}, Sec-Fetch-Site={(seen[b'sec-fetch-site'] or '')[:20]!r}"
        return ""

    def _ports(self, scope) -> set[int]:
        server = scope.get("server")
        if isinstance(server, (tuple, list)) and len(server) == 2 and isinstance(server[1], int):
            return {server[1]}
        return {self.port} if self.port else set()

    def _ours(self, parsed, ports: set[int], *, port_required: bool) -> bool:
        if parsed is None:
            return False
        name, port = parsed
        if name not in self.hosts:
            return False
        if port is None:
            return not port_required
        return not ports or port in ports

    def _own_origin(self, origin: str, ports: set[int]) -> bool:
        scheme, sep, rest = origin.strip().lower().partition("://")
        if not sep or scheme != "http":
            return False
        # An Origin without a port means port 80, which is some other program; our port is always
        # above 1024.
        return self._ours(parse_authority(rest), ports, port_required=True)

    # -- refusal ------------------------------------------------------------

    async def _refuse(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            # First take the "websocket.connect", then close: before accept() the server turns this
            # into an ordinary handshake refusal.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"ok": False, "error": DENIED_TEXT}, ensure_ascii=False).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _with_frame_policy(send):
    async def wrapped(message) -> None:
        if message["type"] == "http.response.start":
            message = {**message, "headers": [*message.get("headers", ()), _FRAME_POLICY]}
        await send(message)

    return wrapped
