"""Access and refresh tokens.

Everything is signed or hashed with one server secret. There is no cryptography of our
own here, only `hmac` and `hashlib` from the standard library. The token format is simple
on purpose, because only we will parse it, and an extra dependency in this place costs
more than the saving of twenty lines.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ACCESS_TTL_S = 15 * 60          # short: a stolen token lives a quarter of an hour
REFRESH_TTL_S = 90 * 24 * 3600  # long: a person should not have to sign in every day


class BadToken(ValueError):
    """The token is not ours, is damaged, or has expired."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_access(secret: str, account_id: int,
                ttl: int = ACCESS_TTL_S, now: float | None = None) -> str:
    """Short-lived pass.

    It lives for a quarter of an hour, so it does not need to be revoked or checked against
    the database: a stolen one expires before it can be of use. Revocation lives on the long
    token, which sits in `sessions` and is deleted on sign-out.
    """
    now = time.time() if now is None else now
    body = {"a": account_id, "e": int(now + ttl)}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{mac}"


def read_access(secret: str, token: str, now: float | None = None) -> dict:
    """Parse a pass. Raises `BadToken`: silently letting it through is not allowed."""
    now = time.time() if now is None else now
    try:
        payload, mac = token.split(".")
    except ValueError:
        raise BadToken("the token is in the wrong format") from None
    want = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    # Constant-time comparison: a plain `==` leaks through the response time how many leading
    # bytes of the signature were guessed, and the signature can be found in hundreds of
    # attempts instead of an astronomical number.
    # Bytes, not strings: `compare_digest` on strings with a non-ASCII character (and the
    # header comes from anyone) fails with a TypeError, and we would get a 500 instead of a 401.
    if not hmac.compare_digest(mac.encode("utf-8", "replace"), want.encode("ascii")):
        raise BadToken("the signature does not match")
    body = json.loads(_unb64(payload))
    if body.get("e", 0) < now:
        raise BadToken("the token has expired")
    return body


def new_refresh() -> tuple[str, str]:
    """Refresh token and its hash for the database.

    Only the hash is in the database: a stolen database must not give sign-in. It is the same
    technique as with passwords, but simpler: the token is random anyway, so there is nothing
    to salt.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_secret(raw)


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
