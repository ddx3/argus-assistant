"""Shared checks before a release goes out: the Windows build (`win/publish_win.py`), the Mac
build (`mac/publish_mac.py`) and the landing page (`landing/publish_landing.py`) are all
checked the same way.

Why this is a separate module. Every release has its own gate, but the key patterns and the
rules for what counts as a leak lived in two copies that drifted apart: the Mac check did not
look for the real values from `.env` and skipped everything binary, while the Windows check
missed ElevenLabs and Discord keys and PEM private keys. Now there is one copy.

Three kinds of checks.

* **Secrets.** The real values from the project's `.env` are searched byte by byte
  everywhere (the values are never printed). Strings that look like keys: a broad set in text,
  and a narrow one (almost no false positives) in binary files and inside PyInstaller's
  compressed code. Modules sit in the PYZ archive as zlib chunks, so a value baked into a
  constant cannot be found by a plain search.
* **Legal-text placeholders.** `<mark class="todo">` and `TODO-…` in the terms, the privacy
  policy and the help page mean a page without the operator's name and without a working mail
  address (`legal/build.py --release` fails on the same thing).
* **Wrong build flavor** (Windows): `claude.exe` and the subscription path to the model must
  not be shipped.

If a check fires on a harmless string, do not disable it. First read what exactly was found,
and only then add the file's relative path to `ALLOWED_FILES`.
"""

from __future__ import annotations

import email.utils
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path

# The broad set, for text files. Anything in a public build that looks like a key must not be
# there.
KEY_LIKE = re.compile(
    rb"(?:"
    rb"sk-[A-Za-z0-9_-]{20,}"                                   # OpenAI, including sk-proj-…
    rb"|sk-ant-[A-Za-z0-9_-]{16,}"                              # Anthropic
    rb"|sk_[0-9a-f]{32,}"                                       # ElevenLabs
    rb"|gh[pousr]_[A-Za-z0-9]{20,}"                             # GitHub tokens
    rb"|xi-[A-Za-z0-9]{24,}"
    rb"|[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"     # Discord bot token
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----"                     # PEM private keys (certificates are not matched)
    rb"|(?<![A-Za-z0-9+/])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/=])"  # AWS access keys: as a standalone word, not a piece of base64
    rb")"
)

# The narrow set, for binary files and archive internals: random bytes contain "sk-" followed
# by twenty letters by chance, so only the lengths of real keys are used.
#
# Deliberately left out (checked against the compressed code of all packages in the
# environment; false positives would stop EVERY release, and the only way around them would be
# to turn off the check for the whole `Argus.exe`):
# * AWS access keys: the font embedded in Pillow (`PIL/ImageFont.py`) is base64, and "AKIA…"
#   occurs in it by chance;
# * a bare PEM header: the words "BEGIN … PRIVATE KEY" sit as constants in library code
#   (`cryptography`, OpenSSH key parsing). A private key inside a binary file is a header
#   followed by a long base64 string, and that is what we look for.
STRICT_KEY_LIKE = re.compile(
    rb"(?:"
    rb"sk-ant-[A-Za-z0-9_-]{40,}"
    rb"|sk-proj-[A-Za-z0-9_-]{40,}"
    rb"|sk-[A-Za-z0-9]{40,}"
    rb"|sk_[0-9a-f]{40,}"
    rb"|gh[pousr]_[A-Za-z0-9]{36,}"
    rb"|[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]{0,1500}?[A-Za-z0-9+/]{64,}"
    rb")"
)

# A line of the form `NAME_KEY=long-value`: this is what a stray `.env` looks like when it ends
# up in a text file (LiqPay, SMTP, HMAC).
ENV_LINE = re.compile(rb"(?m)^[ \t]*[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*[ \t]*=[ \t]*[^\s'\"#]{16,}")

# Files the check fired on that turned out not to contain a key, and that were read by eye.
# Empty: add to it deliberately.
ALLOWED_FILES: tuple[str, ...] = ()

# zlib chunks in PYZ: the 0x78 header and one of the four compression levels.
_ZLIB_HEAD = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
_ZLIB_WINDOW = 1 << 22         # bytes after the header handed to the decompressor (a PYZ module is a few to a few dozen KB)
_ZLIB_OUT = 1 << 24
_ZLIB_BUDGET = 1 << 29
_WHOLE_FILE_LIMIT = 400_000_000
_BLOCK = 32 << 20
_OVERLAP = 1024

LEGAL_MARKS = (b'class="todo"', b"TODO-")


def _env_values(path: Path) -> list[bytes]:
    out: list[bytes] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                value = line.split("=", 1)[1].strip().strip("'\"")
                if len(value) >= 12:
                    out.append(value.encode("utf-8"))
    return out


# One-line secrets in the personal folder `~/.argus`: release signing keys, the Apple
# certificate password, mail and DNS tokens. Judged by file name; binary certificates (`.p12`,
# `.cer`) are not read.
_PERSONAL_SECRET_NAME = re.compile(r"(key|token|password|secret)", re.IGNORECASE)


def _personal_values(folder: Path) -> list[bytes]:
    out: list[bytes] = []
    if folder.is_dir():
        for path in folder.iterdir():
            if path.suffix.lower() in (".p12", ".cer", ".pfx", ".json") or not path.is_file():
                continue
            if not _PERSONAL_SECRET_NAME.search(path.name):
                continue
            try:
                value = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if 12 <= len(value) <= 400 and "\n" not in value:
                out.append(value.encode("utf-8"))
    return out


def secrets_from_env(repo: Path, personal: Path | None = None) -> list[bytes]:
    """The real secret values (what must never be in a release). They must not be printed.

    Sources: the project's `.env` (the app's keys), `deploy/server/.env` (the server's keys:
    OpenAI, ElevenLabs, the payment provider, mail; this file used to be skipped, so a build
    containing one of its values would have passed the check) and the one-line files in the
    personal folder `~/.argus` (release signing keys and the like). `personal` is another
    personal folder (used in tests).
    """
    out = _env_values(repo / ".env") + _env_values(repo / "deploy" / "server" / ".env")
    out += _personal_values(Path.home() / ".argus" if personal is None else personal)
    return out


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:4096]


def _inflated(data: bytes) -> Iterator[bytes]:
    """Contents of the zlib chunks found in a binary file (PyInstaller PYZ modules)."""
    view = memoryview(data)
    budget = _ZLIB_BUDGET             # total limit of decompressed data per file: a "bomb" in someone else's file must not hang the check
    for found in _ZLIB_HEAD.finditer(data):
        try:
            part = zlib.decompressobj().decompress(view[found.start():found.start() + _ZLIB_WINDOW], _ZLIB_OUT)
        except zlib.error:
            continue
        if len(part) >= 16:
            budget -= len(part)
            yield part
            if budget <= 0:
                return


def scan_data(name: str, data: bytes, secrets: Iterable[bytes] = ()) -> str:
    """What is wrong with the file contents. Empty means clean; otherwise the reason in words
    (without the value itself).
    """
    if name in ALLOWED_FILES:
        return ""
    text = _is_text(data)
    for secret in secrets:
        if secret in data:
            return f"in {name} there is a real value from .env — must not be distributed"
    if (KEY_LIKE if text else STRICT_KEY_LIKE).search(data):
        return f"in {name} there is a string that looks like a key — must not be distributed"
    if text and ENV_LINE.search(data):
        return f"in {name} there is a line of the form NAME_KEY=value — must not be distributed"
    if not text:
        for part in _inflated(data):
            if any(secret in part for secret in secrets):
                return f"the compressed code of {name} contains a real value from .env — must not be distributed"
            if STRICT_KEY_LIKE.search(part):
                return f"the compressed code of {name} has a string that looks like a key — must not be distributed"
    return ""


def scan_reader(name: str, reader, size: int, secrets: Iterable[bytes] = ()) -> str:
    """Like `scan_data`, but for a file open for reading (on disk or in an archive). A very large
    file is read in chunks (no search inside zlib)."""
    secrets = list(secrets)
    if size <= _WHOLE_FILE_LIMIT:
        return scan_data(name, reader.read(), secrets)
    tail = b""
    while chunk := reader.read(_BLOCK):
        block = tail + chunk
        problem = scan_data(name, block, secrets) if _is_text(block) else _scan_binary_block(name, block, secrets)
        if problem:
            return problem
        tail = block[-_OVERLAP:]
    return ""


def scan_file(path: Path, name: str, secrets: Iterable[bytes] = ()) -> str:
    """Like `scan_data`, but for a file on disk."""
    with path.open("rb") as f:
        return scan_reader(name, f, path.stat().st_size, secrets)


def _scan_binary_block(name: str, block: bytes, secrets: list[bytes]) -> str:
    if any(secret in block for secret in secrets):
        return f"in {name} there is a real value from .env — must not be distributed"
    if STRICT_KEY_LIKE.search(block):
        return f"in {name} there is a string that looks like a key — must not be distributed"
    return ""


def scan_tree(root: Path, secrets: Iterable[bytes] = ()) -> tuple[int, str]:
    """Check all files in a folder. Returns (number of files scanned, first problem found or empty)."""
    secrets = list(secrets)
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        problem = scan_file(path, path.relative_to(root).as_posix(), secrets)
        if problem:
            return scanned, problem
    return scanned, ""


def legal_problems(pages: Iterable[tuple[str, bytes]]) -> list[str]:
    """Placeholders standing in for the operator name and email in the terms, policy and help:
    `(name, content)` for each page.
    """
    out = []
    for name, data in pages:
        marks = [mark.decode() for mark in LEGAL_MARKS if mark in data]
        if marks:
            out.append(f"{name}: operator details are not filled in ({', '.join(marks)})")
    return out


def legal_message(problems: list[str]) -> str:
    return ("terms, privacy policy or help with placeholders instead of the operator name and email — "
            "fill in legal/operator.json and rebuild (python legal/build.py --release); "
            "to distribute it like this on purpose, use the --allow-legal-placeholders flag\n  " + "\n  ".join(problems))


# --- wrong build flavor ---

# Without ARGUS_BUILD_NO_SUBSCRIPTION the build ships `claude.exe` (about 217 MB; the
# subscription must not be distributed), and without ARGUS_BUILD_API_ONLY it ships the local
# recognition engines (`packaging/argus.spec`, `packaging/build_macos.sh`).
FORBIDDEN_FILES = ("claude.exe",)
FORBIDDEN_DIRS = ("claude_agent_sdk", "ctranslate2", "faster_whisper")


def flavor_problem(relative_paths: Iterable[str]) -> str:
    """Whether the list of paths contains anything that cannot be in a distributed build.
    Empty means no.
    """
    for path in relative_paths:
        parts = path.replace("\\", "/").lower().split("/")
        if parts[-1] in FORBIDDEN_FILES or any(part in FORBIDDEN_DIRS for part in parts[:-1] + [parts[-1]]):
            return (f"in build {path}: wrong build flavor, the subscription path to the model and the local engines must not be distributed "
                    "(ARGUS_BUILD_NO_SUBSCRIPTION=1 and ARGUS_BUILD_API_ONLY=1 are required)")
    return ""


# --- release number ---

# The publishing computer's clock must not differ from the server's by more than this: the
# release number is its reading.
CLOCK_TOLERANCE_S = 600


def server_time(header: str | None) -> float | None:
    """Server time from the `Date` header (None if it is missing or cannot be parsed)."""
    try:
        return email.utils.parsedate_to_datetime(header).timestamp()           # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def fetch_live(url: str, opener=urllib.request.urlopen) -> tuple[dict | None, float | None]:
    """The release description published right now (None if there is no release yet) and the server
    time from the `Date` header.
    """
    try:
        with opener(url, timeout=30) as r:
            raw, date = r.read(1 << 16), r.headers.get("Date")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, server_time(e.headers.get("Date"))
        raise SystemExit(f"failed to determine the released version: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"failed to determine the released version ({e}): the release number cannot be checked without it") from None
    try:
        info = json.loads(raw)
    except ValueError:
        info = None
    return (info if isinstance(info, dict) else None), server_time(date)


def next_build(live: dict | None, now: float | None = None, server_now: float | None = None) -> int:
    """Release number: this computer's clock, but never less than the published number plus one.

    The number is a clock reading, and an app that installed a release remembers it and rejects
    smaller ones (rollback protection). If the publishing computer's clock runs ahead, a release
    "from the future" would make every app silently ignore the following, correctly numbered
    ones. So: never less than the published number (+1), and refuse if the clock differs from the
    server's.
    """
    now = time.time() if now is None else now
    if server_now is not None and abs(now - server_now) > CLOCK_TOLERANCE_S:
        raise SystemExit(f"the clock on this computer differs from the server clock by {abs(now - server_now) / 60:.0f} min: "
                         "fix the time and retry (the release number is derived from the clock)")
    live_build = live.get("build") if isinstance(live, dict) else None
    floor = live_build + 1 if isinstance(live_build, int) and not isinstance(live_build, bool) else 0
    return max(int(now), floor)
