"""Whom to trust on update: the signature of the build description (`latest.json`,
`latest-mac.json`).

The update comes from an address in the settings, and while that is a bare `http://`, a
substitution along the way (anyone on the same network as the person, the provider, a
malicious router) cannot be told from the real file. So the build description is signed
with the **release private key**, which is held only by whoever publishes updates
(`~/.argus/update-signing.key`, `deploy/`), and the public part is baked into the app
(`TRUSTED_KEYS`). The app takes an update only if the signature matches and the
downloaded archive matches the signed hash and size.

What is signed is not "a file" but a string made of the description fields
(`manifest_message`): platform, version, archive name, sha256, size and release number.
Altering any field breaks the signature; a Windows build cannot be passed off as a Mac
build, nor an old signed release as a new one (the release number only grows, see
`update.py: check_release` and `installed_build`).

Earlier builds of the app do not verify the signature and ignore the extra description
fields: they install the first update to a build with verification as before.

If the private key is lost, there will be nothing to update the apps already installed
with: a new build would have to be handed out by hand. So `TRUSTED_KEYS` can hold several
keys (a spare is put into the build in advance and stored separately).
"""

from __future__ import annotations

import base64
import binascii
import re
import sys
from pathlib import Path

from argus import ed25519

# Public release keys (base64, 32 bytes each). The first is the main one; the others are
# spares. A release description signed with any of them is accepted: losing the main key
# does not leave the apps installed with people without updates. The private part of the
# spare (`SPARE_SIGNING_KEY_PATH`) was created on 2026-10-14 by the project's own code
# (`argus.ed25519`) and is stored separately from the main one: a copy of it goes on media
# that does not sit next to this computer. The signed string (`manifest_message`) did not
# change with the second key, so apps already installed check it as before. Apps without
# this entry do not know the spare key: a release signed only with it they will not accept,
# so the spare key is usable only if a build containing it has already been distributed.
TRUSTED_KEYS: tuple[str, ...] = (
    "YoaCob87LWaX3jj1Dd8jU40wE/ZVMVuCWZB8zbGdT4E=",
    "lZPeJvEXHK+6iSc6Y/hInEtkoe6XZIdyRPc1eBjHtPU=",
)

PREFIX = "argus-update-v1"
SIGNING_KEY_PATH = Path.home() / ".argus" / "update-signing.key"
SPARE_SIGNING_KEY_PATH = Path.home() / ".argus" / "update-signing-spare.key"

# The archive name goes into the address: only a simple file name, with no paths and no
# foreign addresses. `\Z`, not `$`: "$" also matches before a trailing newline, and
# "Argus-win.zip\n" would pass the check.
ZIP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.zip\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

# A release description is a few hundred bytes; more than that is not it (see
# `update.fetch_latest`).
MAX_VERSION_LEN = 200


def platform_name() -> str:
    """Which system the build this app looks for is meant for: `win` or `mac`."""
    return "mac" if sys.platform == "darwin" else "win"


def manifest_message(platform: str, info: dict) -> bytes:
    """The string that gets signed. The fields are in a fixed order, one per line."""
    return "\n".join((
        PREFIX,
        platform,
        str(info["version"]),
        str(info["zip"]),
        str(info["sha256"]),
        str(int(info["size"])),
        str(int(info["build"])),
    )).encode("utf-8")


def problem_with_manifest(info: object, platform: str, keys: tuple[str, ...] | None = None) -> str:
    """Whether the build description can be trusted. Empty means yes; otherwise the reason in words
    (for the log).
    """
    if not isinstance(info, dict):
        return "the build description cannot be parsed"
    for field in ("version", "zip", "sha256", "size", "build", "sig"):
        if field not in info:
            return f"the build description has no field '{field}' — the update is not signed"
    version, zip_name, sha256 = info["version"], info["zip"], info["sha256"]
    if not isinstance(version, str) or not version.strip() or "\n" in version or len(version) > MAX_VERSION_LEN:
        return "the build description has an invalid version"
    try:
        # A lone surrogate ("\ud800") from JSON cannot be encoded to UTF-8, and the message for the
        # signature would fail with `UnicodeEncodeError` before the signature was even checked: a
        # single such string sent the launch into an error.
        version.encode("utf-8")
    except UnicodeError:
        return "the build description has an invalid version"
    if not isinstance(zip_name, str) or not ZIP_NAME_RE.match(zip_name):
        return "the build description has an invalid archive name"
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        return "the build description has an invalid hash"
    for field in ("size", "build"):
        value = info[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"the build description has an invalid field '{field}'"
    try:
        signature = base64.b64decode(str(info["sig"]), validate=True)
    except (binascii.Error, ValueError):
        return "the signature of the build description cannot be parsed"
    try:
        message = manifest_message(platform, info)
    except (UnicodeError, ValueError, TypeError):
        return "the build description cannot be parsed"
    for key in TRUSTED_KEYS if keys is None else keys:
        try:
            public = base64.b64decode(key, validate=True)
        except (binascii.Error, ValueError):
            continue
        if ed25519.verify(public, message, signature):
            return ""
    return "the signature of the build description does not match — the update cannot be trusted"


def sign_manifest(info: dict, platform: str, seed: bytes) -> dict:
    """The build description with a signature. Called by the publishing step (`deploy/`); it never
    happens inside the app.
    """
    out = {key: info[key] for key in ("version", "zip", "sha256", "size", "build")}
    out["sig"] = base64.b64encode(ed25519.sign(seed, manifest_message(platform, out))).decode("ascii")
    return out


def load_signing_seed(path: Path | None = None) -> bytes:
    """The release private key from the computer of whoever publishes. If the file is missing: a
    clear error.
    """
    path = path or SIGNING_KEY_PATH
    try:
        seed = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    except (OSError, binascii.Error, ValueError) as e:
        raise SystemExit(f"no release private key ({path}): {e}") from e
    if len(seed) != 32:
        raise SystemExit(f"release private key {path} is not 32 bytes")
    return seed
