"""Trust in the update: the signature of the release description.

The update goes over `http://`, and anything that cannot be verified is counted as a
forgery. Here: what is accepted and what is not, so that no field of the description can
be replaced unnoticed.
"""

from __future__ import annotations

import base64

import pytest

from argus import ed25519, update_trust


@pytest.fixture
def info(release_signer):
    return release_signer.manifest("win").info


def test_a_signed_manifest_is_accepted(info):
    assert update_trust.problem_with_manifest(info, "win") == ""


def test_the_message_that_is_signed_is_stable():
    """This is a contract between the publishing step and the apps already installed on people's
    machines: it cannot be changed without distributing a new build, so we guard it verbatim."""
    info = {"version": "1.0+abc", "zip": "Argus-win.zip", "sha256": "a" * 64, "size": 12, "build": 5}
    assert update_trust.manifest_message("win", info) == (
        b"argus-update-v1\nwin\n1.0+abc\nArgus-win.zip\n" + b"a" * 64 + b"\n12\n5"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("version", "1.0+evil"), ("zip", "Other.zip"), ("sha256", "b" * 64), ("size", 1), ("build", 99999)],
)
def test_changing_any_signed_field_breaks_the_signature(info, field, value):
    forged = dict(info, **{field: value})
    assert "does not match" in update_trust.problem_with_manifest(forged, "win")


def test_a_manifest_for_another_platform_is_refused(info):
    assert "does not match" in update_trust.problem_with_manifest(info, "mac")


def test_an_unsigned_old_style_manifest_is_refused():
    why = update_trust.problem_with_manifest({"version": "1.0+x", "zip": "Argus-win.zip"}, "win")
    assert "is not signed" in why


def test_a_signature_from_somebody_elses_key_is_refused(release_signer, info):
    stranger = bytes(range(100, 132))
    forged = update_trust.sign_manifest({k: info[k] for k in ("version", "zip", "sha256", "size", "build")}, "win", stranger)
    assert "does not match" in update_trust.problem_with_manifest(forged, "win")


@pytest.mark.parametrize("zip_name", ["../Argus.zip", "a/b.zip", "http://evil.example/x.zip", "x.exe", "", ".zip", "a b.zip", "x" * 200 + ".zip"])
def test_an_archive_name_that_is_not_a_plain_file_is_refused(release_signer, zip_name):
    """The name goes into an address: a foreign server or a path outside will not pass, even if
    the signature is genuine.
    """
    signed = release_signer.manifest("win", zip_name=zip_name).info
    assert "archive name" in update_trust.problem_with_manifest(signed, "win")


@pytest.mark.parametrize(
    "patch",
    [{"sha256": "XYZ"}, {"sha256": "A" * 64}, {"size": -1}, {"size": "12"}, {"size": True}, {"build": 1.5}, {"version": ""},
     {"version": "1\n2"}, {"sig": "not base64"}, {"sig": ""}],
)
def test_malformed_fields_are_refused_not_crashed_on(info, patch):
    assert update_trust.problem_with_manifest(dict(info, **patch), "win") != ""


@pytest.mark.parametrize("garbage", [None, [], "text", 5, {}])
def test_garbage_instead_of_a_manifest(garbage):
    assert update_trust.problem_with_manifest(garbage, "win") != ""


def test_any_of_several_trusted_keys_may_sign(release_signer, info, monkeypatch):
    """A spare key is in the app in advance: losing the main one does not leave anyone without
    updates.
    """
    spare = base64.b64encode(ed25519.public_key(bytes(range(50, 82)))).decode()
    monkeypatch.setattr(update_trust, "TRUSTED_KEYS", (spare, *update_trust.TRUSTED_KEYS))
    assert update_trust.problem_with_manifest(info, "win") == ""
    spare_signed = update_trust.sign_manifest(
        {k: info[k] for k in ("version", "zip", "sha256", "size", "build")}, "win", bytes(range(50, 82))
    )
    assert update_trust.problem_with_manifest(spare_signed, "win") == ""


def test_the_platform_follows_the_system(monkeypatch):
    monkeypatch.setattr(update_trust.sys, "platform", "darwin")
    assert update_trust.platform_name() == "mac"
    monkeypatch.setattr(update_trust.sys, "platform", "win32")
    assert update_trust.platform_name() == "win"


def test_the_signing_key_loads_and_bad_files_are_a_clear_error(tmp_path):
    good = tmp_path / "k"
    good.write_text(base64.b64encode(bytes(32)).decode(), encoding="ascii")
    assert update_trust.load_signing_seed(good) == bytes(32)
    short = tmp_path / "s"
    short.write_text(base64.b64encode(b"123").decode(), encoding="ascii")
    with pytest.raises(SystemExit):
        update_trust.load_signing_seed(short)
    with pytest.raises(SystemExit):
        update_trust.load_signing_seed(tmp_path / "no-such-file")


def test_the_embedded_public_key_matches_the_signing_key_of_this_machine():
    """The machine that publishes has the key, and the public key embedded in the app must match
    it, otherwise no app will accept a published update."""
    if not update_trust.SIGNING_KEY_PATH.is_file():
        pytest.skip("there is no private release key on this computer")
    seed = update_trust.load_signing_seed()
    assert base64.b64encode(ed25519.public_key(seed)).decode() == update_trust.TRUSTED_KEYS[0]


def test_every_embedded_key_is_a_valid_public_key():
    for key in update_trust.TRUSTED_KEYS:
        assert len(base64.b64decode(key, validate=True)) == 32


def test_the_main_and_the_spare_key_are_both_embedded_and_different():
    """The second key is embedded in advance (update-8): a spare key that is not recorded in the
    apps people already have is worth nothing.
    """
    assert len(update_trust.TRUSTED_KEYS) >= 2
    assert len(set(update_trust.TRUSTED_KEYS)) == len(update_trust.TRUSTED_KEYS)


def test_a_manifest_signed_by_either_of_the_two_embedded_keys_verifies(monkeypatch):
    """Each embedded key gets its own pair: a signature from either is accepted, a foreign one is
    not. Real private keys are not involved in the tests; what is checked is how the app
    iterates over the `TRUSTED_KEYS` list of two entries."""
    seeds = [bytes(range(1, 33)), bytes(range(60, 92))]
    embedded = tuple(base64.b64encode(ed25519.public_key(seed)).decode() for seed in seeds)
    monkeypatch.setattr(update_trust, "TRUSTED_KEYS", embedded)
    info = {"version": "1.0+abc", "zip": "Argus-win.zip", "sha256": "a" * 64, "size": 12, "build": 5}
    for seed in seeds:
        signed = update_trust.sign_manifest(info, "win", seed)
        assert update_trust.problem_with_manifest(signed, "win") == ""
        assert "does not match" in update_trust.problem_with_manifest(signed, "mac")
    stranger = update_trust.sign_manifest(info, "win", bytes(range(100, 132)))
    assert "does not match" in update_trust.problem_with_manifest(stranger, "win")


def test_the_message_signed_with_the_spare_key_is_the_same_as_with_the_main_one(monkeypatch):
    """The signed string for the second key does not change: apps already installed check it as
    before.
    """
    info = {"version": "1.0+abc", "zip": "Argus-win.zip", "sha256": "a" * 64, "size": 12, "build": 5}
    first, second = bytes(range(1, 33)), bytes(range(60, 92))
    for seed in (first, second):
        signed = update_trust.sign_manifest(info, "win", seed)
        assert ed25519.verify(
            ed25519.public_key(seed), update_trust.manifest_message("win", info), base64.b64decode(signed["sig"]),
        )


def test_the_spare_key_of_this_machine_matches_the_embedded_one():
    """The private spare key is stored next to the main one (`~/.argus`), and the embedded second
    entry must match it: otherwise a release signed with the spare key on the day the main one
    is lost would be accepted by no app. A description signed with it passes verification
    against the real key list, with no substitution."""
    if not update_trust.SPARE_SIGNING_KEY_PATH.is_file():
        pytest.skip("there is no spare release key on this computer")
    seed = update_trust.load_signing_seed(update_trust.SPARE_SIGNING_KEY_PATH)
    assert base64.b64encode(ed25519.public_key(seed)).decode() == update_trust.TRUSTED_KEYS[1]
    info = {"version": "1.0+abc", "zip": "Argus-win.zip", "sha256": "a" * 64, "size": 12, "build": 5}
    assert update_trust.problem_with_manifest(update_trust.sign_manifest(info, "win", seed), "win") == ""
