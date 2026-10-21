"""Ed25519 signature (RFC 8032) in pure Python, for verifying updates.

Why our own module instead of `cryptography`: the signature has to be verified in the
built app, and pulling in a whole library with binary wheels for a single 64-byte check
would put the build at risk (it might not make it into the package, and then updating
would silently stop working for everyone). Here we use only the standard library. Speed
does not matter: the check runs once per update, and on a modern computer it takes tens
of milliseconds.

We can also sign (`sign`): update publishing (`deploy/`) and the tests use that; the app
itself has no private key and must never have one. The code is a direct transcription of
the reference implementation from RFC 8032 (section 6), checked against the vectors from
it and against `cryptography` (`tests/test_ed25519.py`). Signing time depends on the
secret: for publishing from one's own computer that is not a threat, but the module
should not be used for signing in an environment where someone can measure timing.
"""

from __future__ import annotations

import hashlib

# The field and group of the edwards25519 curve.
P = 2**255 - 19
Q = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
assert _GX is not None
G = (_GX, _GY, 1, _GX * _GY % P)          # base point in extended coordinates
_IDENTITY = (0, 1, 1, 0)


def _add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s: int, p: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = _IDENTITY
    while s > 0:
        if s & 1:
            result = _add(result, p)
        p = _add(p, p)
        s >>= 1
    return result


def _equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    if (p[0] * q[2] - q[0] * p[2]) % P != 0:
        return False
    return (p[1] * q[2] - q[1] * p[2]) % P == 0


def _small_order(p: tuple[int, int, int, int]) -> bool:
    """A point of order 1, 2, 4 or 8: with such "keys" a signature can be forged without the private
    key.
    """
    return _equal(_mul(8, p), _IDENTITY)


def _compress(p: tuple[int, int, int, int]) -> bytes:
    zinv = _inv(p[2])
    x = p[0] * zinv % P
    y = p[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes) -> tuple[int, int, int, int] | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    """Public key (32 bytes) from the private one (`seed`, also 32 bytes)."""
    if len(seed) != 32:
        raise ValueError("the Ed25519 private key is 32 bytes")
    a, _ = _expand(seed)
    return _compress(_mul(a, G))


def sign(seed: bytes, message: bytes) -> bytes:
    """Message signature (64 bytes)."""
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, G))
    r = int.from_bytes(_sha512(prefix + message), "little") % Q
    big_r = _compress(_mul(r, G))
    h = int.from_bytes(_sha512(big_r + pub + message), "little") % Q
    s = (r + h * a) % Q
    return big_r + int.to_bytes(s, 32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Is the signature valid? Any parse error is simply "no"; no exceptions escape."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        a = _decompress(public)
        if a is None:
            return False
        r = _decompress(signature[:32])
        if r is None:
            return False
        # As in libsodium: a key and an R point of small order are not accepted (otherwise, for
        # example, a key of zeros with a signature of zeros would pass verification on every fourth
        # message).
        if _small_order(a) or _small_order(r):
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= Q:
            return False
        h = int.from_bytes(_sha512(signature[:32] + public + message), "little") % Q
        # [s]G == R + [h]A  (no multiplication by the cofactor: a strict check)
        return _equal(_mul(s, G), _add(r, _mul(h, a)))
    except Exception:  # noqa: BLE001 — the check must not crash on foreign data
        return False
