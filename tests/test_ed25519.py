"""Ed25519 signature in pure Python: checked against the RFC 8032 vectors and against
`cryptography`.

Trust in updates rests on this signature (`update_trust.py`): a mistake here means either
that nobody can update or that a forgery is accepted, so we check not only "signed and
verified itself" but also foreign reference values.
"""

from __future__ import annotations

import os

import pytest

from argus import ed25519

# RFC 8032, section 7.1: TEST 1, TEST 2, TEST 3.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize(("seed", "public", "message", "signature"), VECTORS, ids=["empty", "one byte", "two bytes"])
def test_rfc8032_vectors(seed, public, message, signature):
    seed_b, public_b = bytes.fromhex(seed), bytes.fromhex(public)
    message_b, signature_b = bytes.fromhex(message), bytes.fromhex(signature)
    assert ed25519.public_key(seed_b) == public_b
    assert ed25519.sign(seed_b, message_b) == signature_b
    assert ed25519.verify(public_b, message_b, signature_b) is True


def test_a_changed_message_or_signature_is_rejected():
    seed = os.urandom(32)
    public = ed25519.public_key(seed)
    message = b"argus-update-v1\nwin\n1.0+abc\nArgus-win.zip"
    signature = ed25519.sign(seed, message)
    assert ed25519.verify(public, message, signature) is True
    assert ed25519.verify(public, message + b"!", signature) is False
    assert ed25519.verify(public, message, bytes([signature[0] ^ 1]) + signature[1:]) is False
    assert ed25519.verify(public, message, signature[:-1] + bytes([signature[-1] ^ 0x80])) is False
    other = ed25519.public_key(os.urandom(32))
    assert ed25519.verify(other, message, signature) is False


@pytest.mark.parametrize(
    ("public", "signature"),
    [(b"", b""), (b"\0" * 32, b"\0" * 64), (b"\xff" * 32, b"\xff" * 64), (b"x" * 31, b"y" * 64), (b"x" * 32, b"y" * 63)],
    ids=["empty", "zeros", "ones", "short key", "short signature"],
)
def test_garbage_never_raises_and_never_verifies(public, signature):
    assert ed25519.verify(public, b"msg", signature) is False


def test_a_signature_with_a_scalar_beyond_the_group_order_is_rejected():
    """A signature with S >= q is a forgeable form; strict verification rejects it."""
    seed = os.urandom(32)
    public = ed25519.public_key(seed)
    signature = ed25519.sign(seed, b"m")
    s = int.from_bytes(signature[32:], "little") + ed25519.Q
    if s < 2**256:
        forged = signature[:32] + s.to_bytes(32, "little")
        assert ed25519.verify(public, b"m", forged) is False


def test_it_agrees_with_the_cryptography_library():
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization

    for _ in range(5):
        seed = os.urandom(32)
        message = os.urandom(200)
        key = ed.Ed25519PrivateKey.from_private_bytes(seed)
        raw_public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert ed25519.public_key(seed) == raw_public
        theirs = key.sign(message)
        assert ed25519.sign(seed, message) == theirs
        assert ed25519.verify(raw_public, message, theirs) is True
