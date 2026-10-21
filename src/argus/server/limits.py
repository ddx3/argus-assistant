"""Limits on incoming traffic: the size of the request body and the address a person came
from.

FastAPI reads and parses the whole body BEFORE it checks the pass, and uvicorn does not
limit the size: a few parallel gigabyte uploads would knock the container out of memory
even on endpoints that would answer 401 anyway. So the limit is set before everything
else, by the `Content-Length` header and by counting bytes while the body is still
arriving, and only what fit reaches parsing.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
from typing import Any

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from argus.server import tokens

# An ordinary request is a fraction of a kilobyte (sign-in, order, email, payment provider
# notification); 64 KB is a generous margin. Speech synthesis is up to 4000 characters of
# text, so it fits in this limit too.
SMALL_BODY = 64 * 1024
# A question with materials: up to 42 page images of 300-600 KB each, plus text. A big body
# is allowed only for someone with a genuine pass (the signature matches). Otherwise an
# anonymous POST with any header could occupy just as much memory.
BIG_BODY = 24 * 1024 * 1024
BIG_PATHS = frozenset({"/relay/brain/ask"})

# How many bytes of big bodies (larger than `SMALL_BODY`) are parsed on the server at the
# same time. Such a body is held in memory at several times its size; measured: a 20 MB body
# takes about 100 MB while the question runs (the bytes read, the parsed strings, the copies
# of images for the provider, the request body to it). A dozen parallel 20 MB questions from
# any registered person would knock the container out of memory (`mem_limit` in compose) and
# take sign-in, payment, and payment provider notifications down with it. The budget is
# counted in bytes, not in items: ordinary questions (hundreds of kilobytes) hardly use it
# up, while a few huge ones run into it. When you raise the budget, raise `mem_limit` too:
# more than five bytes of memory for every byte of budget.
BIG_BUDGET = 120 * 1024 * 1024

TOO_BIG_TEXT = "the request is too large — remove some of the materials"
BUSY_TEXT = "the server is busy right now — try again in a minute"


class TooBig(HTTPException):
    """The body is longer than the limit. It inherits HTTPException on purpose: FastAPI wraps any
    other exception raised while reading the body into "400: cannot parse the body"."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=TOO_BIG_TEXT)


class Overloaded(HTTPException):
    """The memory budget for large bodies is taken by other requests: refusal before parsing, not
    after.
    """

    def __init__(self) -> None:
        super().__init__(status_code=503, detail=BUSY_TEXT, headers={"Retry-After": "10"})


class BodyLimit:
    """ASGI middleware: a body longer than the limit is neither read nor parsed."""

    def __init__(self, app: ASGIApp, secret: str, small: int = SMALL_BODY,
                 big: int = BIG_BODY, big_paths: frozenset[str] = BIG_PATHS,
                 budget: int | None = None) -> None:
        self.app = app
        self.secret = secret
        self.small = small
        self.big = big
        self.big_paths = big_paths
        self.budget = BIG_BUDGET if budget is None else budget
        self._busy = 0                    # how many bytes of the budget are in use right now

    def _signed(self, authorization: str) -> bool:
        """The signature of the pass matches. We do not look at the expiry (`now=0`): a person with an
        expired pass must get a 401 and refresh it, not "request too large" for their honest question."""
        if not authorization.startswith("Bearer "):
            return False
        try:
            tokens.read_access(self.secret, authorization[7:], now=0.0)
        except Exception:  # noqa: BLE001 - any garbage in the header means "no pass"
            return False
        return True

    async def _refuse(self, send: Send, error: HTTPException) -> None:
        body = json.dumps({"detail": error.detail}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": error.status_code, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"connection", b"close"),      # an unread body does not spoil the connection: it will be closed
            *((name.lower().encode(), value.encode())
              for name, value in (error.headers or {}).items()),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":                  # the recognition socket measures its frames itself
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        limit = self.small
        if scope.get("path") in self.big_paths and self._signed(headers.get("authorization", "")):
            limit = self.big

        declared = headers.get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False                      # a broken header is cut off by the server itself
            if too_big:
                await self._refuse(send, TooBig())
                return

        seen = 0
        held = 0                                     # how many bytes of the budget are taken by this request
        started = False

        async def counted() -> Message:
            nonlocal seen, held
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:                     # a body without Content-Length: we count on the fly
                    raise TooBig()
                if seen > self.small:
                    # A big body occupies memory while it arrives and while it is parsed, so the budget is taken
                    # as the bytes come in, not when the whole body has arrived. Otherwise fifty uploads stuck
                    # at 23 MB of 24 would cost nothing by the count, yet together would hold more than a
                    # gigabyte and take the container down (audit 2026-10-16). No room means a refusal now; the
                    # refused request gives back everything it took (`finally` below), and the neighboring
                    # uploads continue instead of choking each other.
                    if self._busy + (seen - held) > self.budget:
                        raise Overloaded()
                    self._busy += seen - held
                    held = seen
            return message

        async def watched(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                # The browser must not "guess" the server's pages (payment, recovery).
                message = {**message, "headers": [
                    *message.get("headers", []), (b"x-content-type-options", b"nosniff")]}
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except (TooBig, Overloaded) as e:
            if started:
                raise
            await self._refuse(send, e)
        finally:
            self._busy -= held


def _bucket(ip: str | None) -> str | None:
    """Key for the attempt counters: IPv4 as is, IPv6 by its /64 network.

    A person or a hosting provider gets a whole /64 network (2**64 addresses), and a counter
    by exact address can be bypassed by taking a new address for each request. IPv4 embedded
    in IPv6 (`::ffff:1.2.3.4`) is reduced to the plain IPv4.
    """
    if not ip:
        return ip
    try:
        addr = ipaddress.ip_address(ip.strip().split("%", 1)[0])
    except ValueError:
        return ip
    if addr.version == 4:
        return str(addr)
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return f"{ipaddress.ip_network((addr, 64), strict=False).network_address}/64"


def client_ip(request: Any, trust_proxy: bool | str = False) -> str | None:
    """The key of the address the request came from (see `_bucket`): for counters and the log."""
    return _bucket(_raw_client_ip(request, trust_proxy))


def _raw_client_ip(request: Any, trust_proxy: bool | str = False) -> str | None:
    """The address a request came from.

    The server listens on the port directly, so the `X-Forwarded-For` header cannot be trusted:
    anyone can write it, and the attempt counter could be bypassed by changing it in every
    request. In front of the server stands our own nginx (`api.arguspilot.com`), but the old
    address `http://IP:8421` stays open directly for the builds already handed out, and the
    header can be forged through it just the same. So trust has two levels:

    * `ARGUS_PROXY_SECRET=<secret>` (a string): nginx adds the header `X-Argus-Proxy: <secret>`,
      and `X-Forwarded-For` is used only if the secret matches. A request without the secret
      came around the proxy: its address is the one the server itself sees.
    * `ARGUS_TRUST_PROXY=1` (`True`): always trust it, only if the direct port is closed.

    The last address in the header is used: the one our proxy appended, not the client.
    """
    if trust_proxy:
        if isinstance(trust_proxy, str):
            given = request.headers.get("x-argus-proxy", "")
            trusted = hmac.compare_digest(given.encode("utf-8"), trust_proxy.encode("utf-8"))
        else:
            trusted = True
        if trusted:
            last = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
            if last:
                return last
    return request.client.host if request.client else None
