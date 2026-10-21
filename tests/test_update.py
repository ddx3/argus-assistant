"""Self-update: no network, nothing Windows-specific: the logic of deciding "what counts as
an update", the check of the description's signature and of the archive's integrity. The
unpacking itself (`_apply`, PowerShell) is not tested here: it is Windows-only and needs
a live process, so a test would either check nothing or rewrite the PowerShell in Python.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from argus import update


def test_the_script_does_not_redeclare_the_reserved_pid_variable():
    """`$Pid` is a reserved PowerShell variable (the id of the current process); a parameter
    with such a name is not declared at all: the script fails at parameter parsing, before
    the first line of the body, with return code 1 and without a single word about the
    cause. This version failed exactly that way once.
    """
    assert "[int]$Pid" not in update._UPDATE_SCRIPT
    assert "Wait-Process -Id $OldPid" in update._UPDATE_SCRIPT


def test_read_version_missing_file_is_empty(tmp_path):
    assert update.read_version(tmp_path) == ""


def test_read_version_strips_whitespace(tmp_path):
    (tmp_path / update.VERSION_NAME).write_text("  abc123  \n", encoding="utf-8")
    assert update.read_version(tmp_path) == "abc123"


def test_a_build_needs_the_exe_inside(tmp_path):
    zip_path = tmp_path / "build.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Argus/Argus.exe", b"fake-exe-bytes")
        z.writestr("Argus/argus.toml", b"[brain]\n")

    assert update._looks_like_a_build(zip_path)


def test_not_a_build_without_the_exe(tmp_path):
    zip_path = tmp_path / "build.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Argus/readme.txt", "nothing here".encode("utf-8"))

    assert not update._looks_like_a_build(zip_path)


def test_a_corrupt_file_is_not_a_build(tmp_path):
    zip_path = tmp_path / "not-a-zip.zip"
    zip_path.write_bytes("this network does not care that this is not a zip".encode("utf-8"))

    assert not update._looks_like_a_build(zip_path)


@pytest.fixture(autouse=True)
def private_temp(monkeypatch, tmp_path):
    """Leftover cleanup (`_sweep_leftovers`) and the stub's temporary folders go in the test's own
    folder, not in the computer's real temporary folder."""
    own = tmp_path / "_tmp"
    own.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(own))


def answer_with(monkeypatch, body: bytes, chunk: int = 4096):
    """A fake `httpx.stream` that returns `body` in chunks: the server's response to the release
    description request.
    """

    @contextmanager
    def fake_stream(method, url, **kwargs):
        class Response:
            def raise_for_status(self):
                pass

            def iter_bytes(self, chunk_size=0):
                for i in range(0, len(body), chunk):
                    yield body[i:i + chunk]

        yield Response()

    monkeypatch.setattr(update.httpx, "stream", fake_stream)


def test_fetch_latest_survives_a_dead_server(monkeypatch):
    def broken_stream(*a, **k):
        raise httpx.ConnectError("no connection")

    monkeypatch.setattr(update.httpx, "stream", broken_stream)

    assert update.fetch_latest("http://example.invalid") is None


def test_fetch_latest_returns_the_parsed_answer(monkeypatch):
    answer_with(monkeypatch, b'{"commit": "abc123", "zip": "Argus-win.zip"}')

    assert update.fetch_latest("http://example.invalid") == {
        "commit": "abc123", "zip": "Argus-win.zip",
    }


def test_a_hostile_answer_never_escapes_fetch_latest(monkeypatch):
    """The response comes over bare http: whatever is in it, startup must not fail (and fixing
    that with an update is not possible).
    """
    hostile = [
        b"[" * 200_000,                                   # RecursionError during parsing
        b'{"version": "' + b"\xff\xfe" + b'"}',           # not UTF-8
        b"\x00\x01\x02",
        b'{"version": "\\ud800"}',                         # a lone surrogate (parsing passes, then encoding breaks)
        b"x" * (update.MAX_MANIFEST_BYTES + 1),            # over the limit
        b"",
    ]
    for body in hostile:
        answer_with(monkeypatch, body)
        info = update.fetch_latest("http://example.invalid")
        assert info is None or isinstance(info, dict | list)


def test_the_manifest_is_read_with_a_size_cap(monkeypatch):
    """We do not read as much as we are given: the excess is discarded before it all got into
    memory.
    """
    served = []

    @contextmanager
    def endless(method, url, **kwargs):
        class Response:
            def raise_for_status(self):
                pass

            def iter_bytes(self, chunk_size=0):
                for _ in range(10_000):
                    served.append(1)
                    yield b" " * 8192

        yield Response()

    monkeypatch.setattr(update.httpx, "stream", endless)
    assert update.fetch_latest("http://example.invalid") is None
    assert len(served) < 20, "stopped at the limit instead of reading an endless reply to the end"


def test_a_slow_trickle_is_cut_off_by_the_total_deadline(monkeypatch):
    """The network timeout is per wait; a slow trickle with no total deadline would hold the
    splash screen forever.
    """
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0           # the deadline was set at zero, and the very first chunk arrived "after 100 s"

    monkeypatch.setattr(update, "time", SimpleNamespace(monotonic=fake_monotonic, time=time.time, sleep=time.sleep))
    answer_with(monkeypatch, b'{"a": 1}', chunk=1)
    assert update.fetch_latest("http://example.invalid") is None


@pytest.mark.parametrize("version", ["\ud800", "\udfff" + "x", "a" * 5000])
def test_a_manifest_with_an_unencodable_version_is_refused_not_crashed_on(release_signer, version):
    from argus import update_trust

    info = dict(release_signer.manifest("win").info, version=version)
    assert update_trust.problem_with_manifest(info, "win") != ""


def test_a_hostile_manifest_does_not_stop_the_launch(win, monkeypatch):
    """Even if something did get through before parsing, startup goes on with the current
    version.
    """
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: {"version": "\ud800", "zip": "Argus-win.zip"})
    assert update.maybe_self_update("http://example.invalid", win.root) is False

    def explode(*a, **k):
        raise RecursionError("too deep")

    monkeypatch.setattr(update, "check_release", explode)
    assert update.maybe_self_update("http://example.invalid", win.root) is False


def test_each_system_asks_its_own_file(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert update.latest_name() == "latest.json"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert update.latest_name() == "latest-mac.json"


# --------------------------------------------------------------------------
# What counts as an update
# --------------------------------------------------------------------------


@pytest.fixture
def win(monkeypatch, tmp_path, release_signer):
    """A Windows app with a folder, version `1.0+old`, and a server returning a signed
    description.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    # PowerShell is not available everywhere these tests run (macOS in CI): the substitution is
    # a dummy, all that is needed is "it exists".
    monkeypatch.setattr(update, "_powershell", lambda: "powershell.exe")
    (tmp_path / update.VERSION_NAME).write_text("1.0+old", encoding="utf-8")
    served = SimpleNamespace(manifest=release_signer.manifest("win"), asked=[])
    monkeypatch.setattr(
        update, "fetch_latest",
        lambda server, name="latest.json", timeout=2.0: served.asked.append(name) or served.manifest.info,
    )
    return SimpleNamespace(root=tmp_path, served=served, signer=release_signer)


def test_a_signed_newer_release_is_an_update(win):
    release = update.check_release("http://example.invalid", win.root)
    assert release is not None and release.version == "1.0+new" and release.build == 2000
    assert win.served.asked == ["latest.json"]


def test_the_same_version_is_not_an_update(win):
    (win.root / update.VERSION_NAME).write_text("1.0+new", encoding="utf-8")
    assert update.check_release("http://example.invalid", win.root) is None


def test_an_unsigned_manifest_is_never_an_update(win, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: {"version": "1.0+evil", "zip": "Argus-win.zip"})
    assert update.check_release("http://example.invalid", win.root) is None


def test_a_forged_manifest_is_never_an_update(win):
    win.served.manifest.info["size"] = 5
    assert update.check_release("http://example.invalid", win.root) is None


def test_a_dead_server_is_not_an_update(win, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: None)
    assert update.check_release("http://example.invalid", win.root) is None


def test_an_old_signed_release_is_a_rollback_not_an_update(win):
    """The old release has a genuine signature too, so it cannot be slipped in instead of the new
    one.
    """
    (win.root / update.STATE_NAME).write_text('{"build": 3000}', encoding="utf-8")
    assert update.check_release("http://example.invalid", win.root) is None


def test_a_release_that_was_being_installed_counts_only_when_the_version_matches(win):
    release = update.check_release("http://example.invalid", win.root)
    update.note_pending(win.root, release)
    # the swap failed: the version is the old one, the release is not counted, and it will be
    # tried again
    assert update.installed_build(win.root) == 0
    assert update.check_release("http://example.invalid", win.root) is not None
    # the swap succeeded: the new program sees its version next to the record
    update.note_pending(win.root, release)
    (win.root / update.VERSION_NAME).write_text("1.0+new", encoding="utf-8")
    assert update.installed_build(win.root) == 2000
    assert update.installed_build(win.root) == 2000, "and remembers it"


def test_a_broken_state_file_is_just_unknown(win):
    (win.root / update.STATE_NAME).write_text("not json", encoding="utf-8")
    assert update.installed_build(win.root) == 0
    (win.root / update.STATE_NAME).write_text("[1, 2]", encoding="utf-8")
    assert update.installed_build(win.root) == 0


# --------------------------------------------------------------------------
# Download: hash and size from the signed description
# --------------------------------------------------------------------------


def serve(monkeypatch, payload: bytes):
    """A fake `httpx.stream`: returns `payload` in chunks of 100 bytes."""
    urls = []

    @contextmanager
    def fake_stream(method, url, **kwargs):
        urls.append(url)

        class Response:
            def raise_for_status(self):
                pass

            def iter_bytes(self, chunk_size=0):
                for i in range(0, len(payload), 100):
                    yield payload[i:i + 100]

        yield Response()

    monkeypatch.setattr(update.httpx, "stream", fake_stream)
    return urls


def release_of(win) -> update.Release:
    return update.check_release("http://example.invalid", win.root)


def test_the_download_must_match_the_signed_hash_and_size(win, monkeypatch, tmp_path):
    urls = serve(monkeypatch, win.served.manifest.payload)
    seen = []
    dest = tmp_path / "u.zip"
    update.download_release("http://example.invalid/", release_of(win), dest, lambda done, total: seen.append((done, total)))
    assert dest.read_bytes() == win.served.manifest.payload
    assert urls == ["http://example.invalid/Argus-win.zip"]
    assert seen[-1] == (900, 900) and seen[0][0] == 100


def test_a_swapped_archive_is_deleted_and_refused(win, monkeypatch, tmp_path):
    fake = bytearray(win.served.manifest.payload)
    fake[0] ^= 1                                            # same size, different hash
    serve(monkeypatch, bytes(fake))
    dest = tmp_path / "u.zip"
    with pytest.raises(update.UpdateError, match="doesn't match"):
        update.download_release("http://example.invalid", release_of(win), dest)
    assert not dest.exists()


def test_a_truncated_archive_is_refused(win, monkeypatch, tmp_path):
    serve(monkeypatch, win.served.manifest.payload[:-1])
    with pytest.raises(update.UpdateError):
        update.download_release("http://example.invalid", release_of(win), tmp_path / "u.zip")


def test_an_archive_bigger_than_signed_is_cut_off_early(win, monkeypatch, tmp_path):
    """We do not download gigabytes if the server returns more than was signed."""
    serve(monkeypatch, win.served.manifest.payload + b"x" * 500)
    dest = tmp_path / "u.zip"
    with pytest.raises(update.UpdateError, match="larger than"):
        update.download_release("http://example.invalid", release_of(win), dest)
    assert not dest.exists()


def test_a_dead_network_while_downloading_is_a_clear_error(win, monkeypatch, tmp_path):
    def broken(*a, **k):
        raise httpx.ConnectError("no connection")

    monkeypatch.setattr(update.httpx, "stream", broken)
    with pytest.raises(update.UpdateError, match="couldn't download"):
        update.download_release("http://example.invalid", release_of(win), tmp_path / "u.zip")


# --------------------------------------------------------------------------
# Startup: by itself, without questions
# --------------------------------------------------------------------------


def test_no_server_configured_means_no_update(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    assert update.maybe_self_update("", tmp_path) is False


def test_dev_run_never_self_updates(tmp_path, monkeypatch):
    """A working copy from source is updated with `git pull`; there is no reason to get
    involved here, even if the address of the update server was left in the config by
    mistake.
    """
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    assert update.maybe_self_update("http://example.invalid", tmp_path) is False


def test_same_version_does_not_trigger_a_download(win, monkeypatch):
    (win.root / update.VERSION_NAME).write_text("1.0+new", encoding="utf-8")

    def unreachable(*a, **kw):
        raise AssertionError("must not download: the version is the same")

    monkeypatch.setattr(update, "download_release", unreachable)
    assert update.maybe_self_update("http://example.invalid", win.root) is False


def make_build_zip(dest):
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("Argus/Argus.exe", b"exe")


def test_a_verified_release_is_handed_to_the_swap(win, monkeypatch):
    applied = []
    monkeypatch.setattr(update, "download_release", lambda server, release, dest, progress=None: make_build_zip(dest))
    monkeypatch.setattr(update, "_apply", lambda zip_path, root, release: applied.append((zip_path, root, release)) or True)

    assert update.maybe_self_update("http://example.invalid", win.root) is True
    assert applied and applied[0][1] == win.root
    assert applied[0][2].sha256 == win.served.manifest.info["sha256"], "the signed hash goes to the script"
    assert applied[0][0].parent.name.startswith("argus-update-"), "the archive is in its own random folder, not under a shared name"
    pending = update._read_state(win.root)["pending"]
    assert pending == {"version": "1.0+new", "build": 2000}, "what is being installed is recorded before the swap"


def test_a_bad_download_does_not_apply_or_crash(win, monkeypatch):
    serve(monkeypatch, b"\x00" * 900)                        # the size is right, the hash is not

    def unreachable(*a, **kw):
        raise AssertionError("must not apply a bad archive")

    monkeypatch.setattr(update, "_apply", unreachable)
    assert update.maybe_self_update("http://example.invalid", win.root) is False


def test_an_archive_that_is_not_a_build_is_not_applied(win, monkeypatch):
    junk = "this is not a zip".encode("utf-8")
    manifest = win.signer.manifest("win", payload=junk)
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: manifest.info)
    serve(monkeypatch, junk)
    monkeypatch.setattr(update, "_apply", lambda *a: (_ for _ in ()).throw(AssertionError("must not apply")))
    assert update.maybe_self_update("http://example.invalid", win.root) is False


def test_a_swap_process_that_did_not_start_is_not_trusted(win, monkeypatch):
    monkeypatch.setattr(update, "download_release", lambda server, release, dest, progress=None: make_build_zip(dest))
    monkeypatch.setattr(update, "_apply", lambda zip_path, root, release: False)
    assert update.maybe_self_update("http://example.invalid", win.root) is False
    assert not list(Path(tempfile.gettempdir()).glob("argus-update-*")), "the temporary folder is removed if the swap did not start"


def test_a_folder_we_cannot_write_to_blocks_the_update(win, monkeypatch):
    """What is faked here is not `os.access` but the write itself: on Windows
    `os.access(folder, W_OK)` is always "yes".
    """
    real_write = Path.write_bytes

    def refuse(self, data):
        if self.name.startswith(".write-test-") and self.parent == win.root:
            raise PermissionError("no permission")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", refuse)

    def unreachable(*a, **kw):
        raise AssertionError("must not download: there is nowhere to write")

    monkeypatch.setattr(update, "download_release", unreachable)
    assert update.maybe_self_update("http://example.invalid", win.root) is False
    assert "no permission" in update.cannot_install_reason(win.root)


def _make_read_only(folder: Path) -> bool:
    """Make a folder truly read-only: on Windows, a ban on creating files (ACL), otherwise file
    permissions.
    """
    import getpass
    import os
    import subprocess

    if os.name == "nt":
        who = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
        done = subprocess.run(["icacls", str(folder), "/deny", f"{who}:(WD,AD)"], capture_output=True, text=True)
        return done.returncode == 0
    folder.chmod(0o500)
    return True


def _make_writable(folder: Path) -> None:
    import os
    import subprocess

    if os.name == "nt":
        import getpass

        who = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
        subprocess.run(["icacls", str(folder), "/remove:d", who], capture_output=True, text=True)
    else:
        folder.chmod(0o700)


def test_a_really_read_only_folder_blocks_the_update(win):
    """Without faking anything: the folder is really closed for writing (like `Program Files` for
    an ordinary user).
    """
    folder = win.root / "ro"
    folder.mkdir()
    if not _make_read_only(folder):
        pytest.skip("could not make the folder read-only")
    try:
        if update._can_write_here(folder):
            pytest.skip("the ban has no effect (running as administrator?)")
        assert "no permission" in update.cannot_install_reason(folder)
    finally:
        _make_writable(folder)


def test_a_missing_powershell_blocks_the_update_before_any_download(win, monkeypatch):
    monkeypatch.setattr(update, "_powershell", lambda: "")
    monkeypatch.setattr(update, "download_release", lambda *a, **k: (_ for _ in ()).throw(AssertionError("do not download")))
    assert "PowerShell" in update.cannot_install_reason(win.root)
    assert update.maybe_self_update("http://example.invalid", win.root) is False


def test_a_source_checkout_is_not_installable(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    assert "run from source" in update.cannot_install_reason(tmp_path)


def test_a_signed_hash_that_does_not_belong_to_the_archive_is_refused(win, monkeypatch, tmp_path):
    """The signature is genuine, but the archive it points to is someone else's: the hash is
    computed over the downloaded bytes, not taken on trust.
    """
    other = b"other-bytes" * 80
    info = win.signer.manifest("win", payload=other).info
    assert info["sha256"] == hashlib.sha256(other).hexdigest()
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: info)
    serve(monkeypatch, win.served.manifest.payload[:len(other)].ljust(len(other), b"x"))   # same size, different bytes
    dest = tmp_path / "u.zip"
    with pytest.raises(update.UpdateError, match="doesn't match"):
        update.download_release("http://example.invalid", release_of(win), dest)
    assert not dest.exists()


def test_powershell_comes_from_the_system_folder_not_from_the_current_one(tmp_path, monkeypatch):
    """On Windows `shutil.which` also looks in the current folder: a `powershell.exe` planted
    there would be executed during the update.
    """
    root = tmp_path / "Windows"
    real = root / "System32" / "WindowsPowerShell" / "v1.0"
    real.mkdir(parents=True)
    (real / "powershell.exe").write_bytes(b"")
    planted = tmp_path / "Downloads"
    planted.mkdir()
    (planted / "powershell.exe").write_bytes(b"")
    monkeypatch.setenv("SystemRoot", str(root))
    monkeypatch.setattr(update.shutil, "which", lambda name: str(planted / "powershell.exe"))
    assert update._powershell() == str(real / "powershell.exe")
