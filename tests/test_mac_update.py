"""Self-update of the `.app`: the decision whether to download, the signature check and the
swap script itself.

Signing, `ditto` and a real bundle exist only on macOS, so the system calls are faked.
The swap script is written in bash and runs for real if bash is available (on Windows
this is Git Bash): its logic (wait, swap, roll back on failure) does not depend on
macOS.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus import host, update
from argus.mac import update as mac_update


@pytest.fixture(autouse=True)
def private_temp(monkeypatch, tmp_path):
    """The cleanup of leftovers at startup (`update._sweep_leftovers`) walks the temporary folder;
    in the test it walks its own.
    """
    import tempfile

    own = tmp_path / "_tmp"
    own.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(own))


def run_result(returncode=0, stderr="", stdout=""):
    return SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)


# --------------------------------------------------------------------------
# Where updating is possible
# --------------------------------------------------------------------------


def test_a_program_outside_an_app_bundle_cannot_update():
    assert "not running from an .app bundle" in mac_update.cannot_update_reason(None)


def test_a_program_run_from_downloads_cannot_update_itself():
    """The system puts it in "read-only" mode (App Translocation): there is nothing to replace."""
    path = Path("/private/var/folders/xx/AppTranslocation/ABC/d/Argus.app")
    assert "move it to Applications" in mac_update.cannot_update_reason(path)


def test_a_folder_we_cannot_write_to_blocks_the_update(monkeypatch, tmp_path):
    monkeypatch.setattr(mac_update.os, "access", lambda path, mode: False)
    assert "no permission" in mac_update.cannot_update_reason(tmp_path / "Argus.app")


def test_a_normal_install_may_update(tmp_path):
    assert mac_update.cannot_update_reason(tmp_path / "Argus.app") == ""


# --------------------------------------------------------------------------
# Archive and signature
# --------------------------------------------------------------------------


def make_zip(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"x")
    return path


def test_a_build_needs_the_app_executable_inside(tmp_path):
    good = make_zip(tmp_path / "a.zip", ["Argus.app/Contents/MacOS/Argus", "Argus.app/Contents/Info.plist"])
    assert mac_update.looks_like_a_build(good)
    assert not mac_update.looks_like_a_build(make_zip(tmp_path / "b.zip", ["Argus/Argus.exe"]))
    (tmp_path / "c.zip").write_bytes(b"not a zip")
    assert not mac_update.looks_like_a_build(tmp_path / "c.zip")


def signer(verify_rc=0, mine_req='identifier "com.argus.assistant" and certificate leaf = H"AA"',
           theirs_req=None, mine_id="com.argus.assistant", theirs_id="com.argus.assistant"):
    """A stub `codesign`: the signature check, the code requirement and the identifier of two
    packages.
    """
    theirs_req = mine_req if theirs_req is None else theirs_req

    def run(cmd, **kw):
        current = "current.app" in cmd[-1]
        if "--verify" in cmd:
            return run_result(returncode=verify_rc)
        if "-r-" in cmd:
            req = mine_req if current else theirs_req
            return run_result(stdout=f"designated => {req}\n" if req else "")
        ident = mine_id if current else theirs_id
        return run_result(stderr=f"Identifier={ident}\nTeamIdentifier=not set\n")

    return run


def test_a_correctly_signed_build_with_the_same_key_is_trusted():
    assert mac_update.verify_bundle(Path("new.app"), Path("current.app"), signer()) == ""


def test_a_broken_signature_is_refused():
    assert "does not match" in mac_update.verify_bundle(Path("new.app"), Path("current.app"), signer(verify_rc=1))


def test_a_build_signed_with_another_key_is_refused_when_the_running_one_has_a_stable_key():
    """Microphone and screen permissions are tied to the signing key: a new build with a different
    key is a different program.
    """
    why = mac_update.verify_bundle(
        Path("new.app"), Path("current.app"), signer(theirs_req='identifier "com.argus.assistant" and certificate leaf = H"BB"')
    )
    assert "different key" in why


def test_a_trial_signed_program_may_update_because_the_release_signature_vouches_for_it():
    """Authenticity is held by the release signature, not by Apple's signature: without a permanent
    key, updating is allowed (the permissions will have to be granted again, since for a trial
    signature they are tied to the build hash)."""
    ad_hoc = 'cdhash H"abc"'
    assert mac_update.verify_bundle(
        Path("new.app"), Path("current.app"), signer(mine_req=ad_hoc, theirs_req='cdhash H"def"')
    ) == ""


def test_another_program_is_not_an_update(tmp_path):
    """The signature is valid, but it is a different package: replacing Argus with it would pass
    the other checks.
    """
    why = mac_update.verify_bundle(Path("new.app"), Path("current.app"), signer(theirs_id="com.argus.other"))
    assert "a different app" in why
    unreadable = mac_update.verify_bundle(Path("new.app"), Path("current.app"), signer(theirs_id=""))
    assert "a different app" in unreadable, "could not read the identifier: we do not trust it"


def test_the_bundle_identifier_is_read_from_codesign_output():
    out = "Identifier=com.argus.assistant\nTeamIdentifier=ABCDE12345\n"
    assert mac_update.bundle_identifier(Path("x.app"), lambda *a, **k: run_result(stderr=out)) == "com.argus.assistant"
    assert mac_update.bundle_identifier(Path("x.app"), lambda *a, **k: run_result(stderr="")) == ""


def test_the_designated_requirement_is_read_from_either_stream():
    line = 'designated => identifier "com.argus.assistant" and certificate leaf = H"AA"'
    assert mac_update.designated_requirement(Path("x.app"), lambda *a, **k: run_result(stdout=line)) == line.split("=> ")[1]
    assert mac_update.designated_requirement(Path("x.app"), lambda *a, **k: run_result(stderr="# " + line)) == line.split("=> ")[1]
    assert mac_update.designated_requirement(Path("x.app"), lambda *a, **k: run_result(stdout="")) == ""

    def broken(*a, **k):
        raise FileNotFoundError("codesign")

    assert mac_update.designated_requirement(Path("x.app"), broken) == ""


def test_only_a_constant_key_counts_as_a_stable_identity():
    assert mac_update.has_stable_identity('identifier "x" and certificate leaf = H"AA"')
    assert mac_update.has_stable_identity('identifier "x" and anchor apple generic and certificate leaf[subject.OU] = "T"')
    assert not mac_update.has_stable_identity('cdhash H"abc"')
    assert not mac_update.has_stable_identity("")


def test_ditto_extracts_the_bundle_and_failure_is_none(tmp_path):
    def good(cmd, **kw):
        (Path(cmd[-1]) / "Argus.app" / "Contents" / "MacOS").mkdir(parents=True)
        return run_result()

    assert mac_update.extract(tmp_path / "u.zip", tmp_path / "out", good) == tmp_path / "out" / "Argus.app"
    assert mac_update.extract(tmp_path / "u.zip", tmp_path / "out2", lambda *a, **k: run_result(1)) is None


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


@pytest.fixture
def on_mac(monkeypatch, tmp_path, release_signer):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    bundle = tmp_path / "Applications" / "Argus.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(host, "app_bundle", lambda: bundle)
    monkeypatch.setattr(host, "logs_dir", lambda: tmp_path / "logs")
    root = tmp_path / "data"
    root.mkdir()
    (root / update.VERSION_NAME).write_text("1.0+old", encoding="utf-8")
    manifest = release_signer.manifest("mac")
    calls = SimpleNamespace(asked=[], downloaded=[], swapped=[], manifest=manifest)
    monkeypatch.setattr(
        update, "fetch_latest",
        lambda server, name="latest.json", timeout=2.0: calls.asked.append(name) or calls.manifest.info,
    )

    def fake_download(server, release, dest, progress=None):
        calls.downloaded.append(f"{server.rstrip('/')}/{release.zip}")
        make_zip(dest, ["Argus.app/Contents/MacOS/Argus"])

    monkeypatch.setattr(update, "download_release", fake_download)
    monkeypatch.setattr(mac_update, "extract", lambda zip_path, into, run=None: _fake_unpack(into))
    monkeypatch.setattr(mac_update, "verify_bundle", lambda new, cur, run=None: "")

    def fake_spawn(new_app, bundle_, work, log_path, alive=None):
        calls.swapped.append((new_app, bundle_, work, log_path))
        calls.alive = alive
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(mac_update, "spawn_swap", fake_spawn)
    monkeypatch.setattr(mac_update.time, "sleep", lambda s: None)
    return SimpleNamespace(root=root, bundle=bundle, calls=calls, signer=release_signer)


def _fake_unpack(into: Path) -> Path:
    app = into / "Argus.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    return app


def test_the_mac_asks_its_own_latest_file(on_mac):
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is True
    assert on_mac.calls.asked == ["latest-mac.json"]
    assert on_mac.calls.downloaded == ["http://example.invalid/Argus-mac.zip"]


def test_a_new_version_downloads_verifies_and_hands_over_to_the_swap(on_mac):
    assert update.maybe_self_update("http://example.invalid/", on_mac.root) is True
    new_app, bundle, work, log_path = on_mac.calls.swapped[0]
    assert bundle == on_mac.bundle and new_app.name == "Argus.app"
    assert log_path.name == "update.log"
    assert update._read_state(on_mac.root)["pending"] == {"version": "1.0+new", "build": 2000}


def test_the_same_version_downloads_nothing(on_mac):
    (on_mac.root / update.VERSION_NAME).write_text("1.0+new", encoding="utf-8")
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False
    assert not on_mac.calls.downloaded


def test_where_updating_is_impossible_nothing_is_downloaded(on_mac, monkeypatch):
    monkeypatch.setattr(host, "app_bundle", lambda: Path("/private/var/AppTranslocation/x/Argus.app"))
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False
    assert not on_mac.calls.downloaded


def test_an_unsigned_release_is_never_installed_even_on_a_properly_signed_program(on_mac, monkeypatch):
    """Before, trust rested on Apple's signature; now it rests on the release signature: without it
    we install nothing.
    """
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: {"version": "1.0+evil", "zip": "Argus-mac.zip"})
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False
    assert not on_mac.calls.downloaded and not on_mac.calls.swapped


def test_a_release_for_windows_is_not_taken_by_a_mac(on_mac, monkeypatch):
    windows = on_mac.signer.manifest("win")
    monkeypatch.setattr(update, "fetch_latest", lambda *a, **k: windows.info)
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False
    assert not on_mac.calls.downloaded


def test_an_untrusted_build_is_never_swapped_in(on_mac, monkeypatch):
    monkeypatch.setattr(mac_update, "verify_bundle", lambda new, cur, run=None: "the signature does not match")
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False
    assert not on_mac.calls.swapped


def test_garbage_instead_of_an_archive_is_dropped(on_mac, monkeypatch):
    monkeypatch.setattr(
        update, "download_release", lambda server, release, dest, progress=None: dest.write_bytes("garbage".encode("utf-8"))
    )
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False
    assert not on_mac.calls.swapped


def test_a_dead_network_keeps_the_current_version(on_mac, monkeypatch):
    def broken(server, release, dest, progress=None):
        raise update.UpdateError("couldn't download the update")

    monkeypatch.setattr(update, "download_release", broken)
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False


def test_a_swap_that_dies_at_once_is_reported_not_trusted(on_mac, monkeypatch):
    monkeypatch.setattr(mac_update, "spawn_swap", lambda *a: SimpleNamespace(poll=lambda: 1))
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False


def test_the_temporary_folder_is_removed_when_the_install_fails(on_mac, monkeypatch):
    seen = {}

    def broken(server, release, dest, progress=None):
        seen["work"] = dest.parent
        raise update.UpdateError("no connection")

    monkeypatch.setattr(update, "download_release", broken)
    with pytest.raises(update.UpdateError):
        mac_update.install_release("http://example.invalid", update.check_release("http://example.invalid", on_mac.root), on_mac.root)
    assert not seen["work"].exists()


def test_no_server_or_a_dev_run_never_updates(on_mac, monkeypatch):
    assert update.maybe_self_update("", on_mac.root) is False
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is False


def test_windows_still_asks_the_old_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    asked = []
    monkeypatch.setattr(update, "fetch_latest", lambda server, name="latest.json", timeout=2.0: asked.append(name) or None)
    assert update.maybe_self_update("http://example.invalid", tmp_path) is False
    assert asked == ["latest.json"]


# --------------------------------------------------------------------------
# The swap script, for real
# --------------------------------------------------------------------------

BASH = shutil.which("bash")


def _git_bash_ok() -> bool:
    if BASH is None:
        return False
    try:
        out = subprocess.run([BASH, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


needs_bash = pytest.mark.skipif(not _git_bash_ok(), reason="bash is needed")


def bash_path(path: Path) -> str:
    """The path as bash understands it (on Windows, `/c/Users/…`)."""
    text = str(path.resolve())
    if sys.platform == "win32" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def run_swap(tmp_path: Path, new_exists: bool = True):
    app = tmp_path / "Programs" / "Argus.app"
    app.mkdir(parents=True)
    (app / "marker").write_text("old", encoding="utf-8")
    work = tmp_path / "work"
    new = work / "new" / "Argus.app"
    if new_exists:
        new.mkdir(parents=True)
        (new / "marker").write_text("new", encoding="utf-8")
    else:
        work.mkdir()
    script = tmp_path / "swap.sh"
    script.write_text(mac_update.SWAP_SCRIPT, encoding="utf-8", newline="\n")
    log = tmp_path / "update.log"
    done = subprocess.run(
        [BASH, bash_path(script), "99999999", bash_path(new), bash_path(app), bash_path(work), bash_path(log)],
        capture_output=True, text=True, timeout=60,
    )
    return app, work, log, done


@needs_bash
def test_the_swap_script_replaces_the_bundle_and_cleans_up(tmp_path):
    app, work, log, done = run_swap(tmp_path)
    assert (app / "marker").read_text() == "new"
    assert not work.exists()
    assert not list(app.parent.glob("Argus.app.old.*")), "the spare copy is removed"
    assert "package replaced" in log.read_text(encoding="utf-8")


@needs_bash
def test_the_swap_script_puts_the_old_bundle_back_when_the_swap_fails(tmp_path):
    app, work, log, done = run_swap(tmp_path, new_exists=False)
    assert (app / "marker").read_text() == "old", "the person must not be left without the app"
    assert "restoring the previous package" in log.read_text(encoding="utf-8")


def test_the_swap_script_waits_for_the_old_process_and_opens_the_new_app():
    text = mac_update.SWAP_SCRIPT
    assert 'kill -0 "$OLD_PID"' in text and "/usr/bin/open" in text
    assert text.index('kill -0 "$OLD_PID"') < text.index('/bin/mv "$APP" "$BACKUP"')


def test_the_swap_is_told_where_the_new_program_reports_that_it_started(on_mac):
    assert update.maybe_self_update("http://example.invalid", on_mac.root) is True
    assert on_mac.calls.alive == on_mac.root / update.ALIVE_NAME


def test_the_program_reports_that_it_started_on_every_system(on_mac, monkeypatch):
    """The swap script waits for the mark (macOS and Windows); it is written on every launch,
    before the update check (even without a server address).
    """
    assert update.maybe_self_update("", on_mac.root) is False
    assert (on_mac.root / update.ALIVE_NAME).is_file()
    (on_mac.root / update.ALIVE_NAME).unlink()
    monkeypatch.setattr(sys, "platform", "win32")
    update.maybe_self_update("", on_mac.root)
    assert (on_mac.root / update.ALIVE_NAME).is_file()


def run_swap_with_alive(tmp_path: Path, report_after: float | None, tries: int = 6):
    """The swap script with waiting for the mark. `report_after` is the number of seconds after
    which the "new program" writes it (None means never).
    """
    import threading
    import time

    app = tmp_path / "Programs" / "Argus.app"
    app.mkdir(parents=True)
    (app / "marker").write_text("old", encoding="utf-8")
    work = tmp_path / "work"
    new = work / "new" / "Argus.app"
    new.mkdir(parents=True)
    (new / "marker").write_text("new", encoding="utf-8")
    script = tmp_path / "swap.sh"
    script.write_text(mac_update.SWAP_SCRIPT, encoding="utf-8", newline="\n")
    log = tmp_path / "update.log"
    alive = tmp_path / "data" / "update-alive"
    alive.parent.mkdir()
    def new_program():
        """Like a new program: it appears after the swap (when the script has "opened" the package) and
        after a while writes the mark.
        """
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                if (app / "marker").read_text(encoding="utf-8") == "new":
                    break
            except OSError:
                pass
            time.sleep(0.02)
        time.sleep(report_after)
        # We write more than once: the script erases the old marker right after the swap, and
        # under load that could happen after our write. The real program writes the marker on
        # every start.
        for _ in range(150):
            alive.write_text("1", encoding="utf-8")
            time.sleep(0.1)
            if log.exists() and "the new program started" in log.read_text(encoding="utf-8"):
                return

    if report_after is not None:
        threading.Thread(target=new_program, daemon=True).start()
    import os

    done = subprocess.run(
        [BASH, bash_path(script), "99999999", bash_path(new), bash_path(app), bash_path(work), bash_path(log), bash_path(alive)],
        capture_output=True, text=True, timeout=60, env=dict(os.environ, ARGUS_ALIVE_TRIES=str(tries)),
    )
    return app, work, log, done


@needs_bash
def test_a_new_program_that_reports_in_keeps_the_swap_and_drops_the_backup(tmp_path):
    app, work, log, done = run_swap_with_alive(tmp_path, report_after=0.6, tries=20)
    assert (app / "marker").read_text() == "new"
    assert not list(app.parent.glob("Argus.app.old.*")), "the previous package is removed only after the mark"
    assert not work.exists()
    assert "the new program started" in log.read_text(encoding="utf-8")


@needs_bash
def test_a_new_program_that_never_starts_is_rolled_back(tmp_path):
    """CI launches the executable directly, while people use `open`: a build that fails only that
    way used to stay in place forever, and the old package had already been deleted."""
    app, work, log, done = run_swap_with_alive(tmp_path, report_after=None, tries=3)
    assert (app / "marker").read_text() == "old", "the previous package is put back"
    assert not list(app.parent.glob("Argus.app.old.*"))
    assert done.returncode == 4
    assert "restoring the previous package" in log.read_text(encoding="utf-8")


def test_the_swap_script_removes_the_backup_only_after_the_new_program_reports_in():
    text = mac_update.SWAP_SCRIPT
    assert text.index('/usr/bin/open "$APP"') < text.index('[ -f "$ALIVE" ] && break') < text.index('/bin/rm -rf "$BACKUP" "$WORK"\nelse')
