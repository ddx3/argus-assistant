"""Hide secrets and personal data in text before a model, a report, or a log line sees it.

This is a seatbelt, not a vault. It catches the shapes that leak most often (provider keys, bearer
tokens, passwords in URLs and `key=value` pairs, e-mail addresses, public IP addresses). Anything it
misses must still not matter, because the model has no way to send text anywhere: see `tools.py`.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL), "[private key]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "[anthropic key]"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}"), "[api key]"),
    (re.compile(r"\bsk_[0-9a-f]{24,}\b"), "[api key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[github token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[github token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws key]"),
    (re.compile(r"\b[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}\b"), "[bot token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[jwt]"),
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@"), r"\1[redacted]@"),
    (re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|token)[A-Za-z0-9_]*)\b(\s*[=:]\s*)(?!\[)(['\"]?)[^\s'\",;]{8,}\3"
    ), r"\1\2[redacted]"),
    (re.compile(r"\b([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b"), r"\1***@\2"),
]

# IPv4 addresses: keep loopback and private ranges (useful when reading a log), hide public ones.
_IPV4 = re.compile(r"\b(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])\b")


def _hide_public_ip(m: re.Match[str]) -> str:
    parts = [int(g) for g in m.groups()]
    if any(p > 255 for p in parts):
        return m.group(0)
    a, b = parts[0], parts[1]
    private = a in (10, 127, 0) or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31) or (a == 169 and b == 254)
    documentation = (a, b) in ((192, 0), (198, 51), (203, 0))
    return m.group(0) if private or documentation else "[ip]"


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return _IPV4.sub(_hide_public_ip, text)


# The shapes that are credentials beyond doubt: provider keys, private keys, tokens, passwords inside URLs. The looser
# patterns (a word like "token" followed by a value) are right for hiding text but wrong for judging code, where
# `token = settings.get("token")` is ordinary.
_SPECIFIC = _PATTERNS[:9] + [_PATTERNS[10]]


def contains_secret(text: str) -> bool:
    """True when `text` holds something that is certainly a credential."""
    return any(pattern.search(text) for pattern, _ in _SPECIFIC)
