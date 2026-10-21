"""The built app on macOS: where data, the log, the seeds and the error window live.

The `.app` is signed: any file added inside it breaks the signature, and the system
mounts an app launched from "Downloads" read-only. So everything mutable (settings,
recordings, chats, sign-in, the log) lives in `~/Library/…`, and the bundle holds only
immutable seeds, from which the data folder is filled on the first launch and on every
version change. This file checks that layout, without macOS and without the build
itself.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGING_DIR = Path(__file__).parent.parent / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import argus_app  # noqa: E402 — the path was just added, above

from argus import host  # noqa: E402


@pytest.fixture
def on_mac(monkeypatch, tmp_path):
    """Platform macOS, a built app, the home folder in a temporary one."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# --------------------------------------------------------------------------
# What lives where
# --------------------------------------------------------------------------


def test_data_and_logs_live_in_the_users_library(on_mac):
    assert host.user_data_dir() == on_mac / "Library" / "Application Support" / "Argus"
    assert host.logs_dir() == on_mac / "Library" / "Logs" / "Argus"
    assert argus_app.root() == host.user_data_dir()
    assert argus_app.log_path() == host.logs_dir() / "argus.log"


def test_on_windows_everything_stays_next_to_the_exe(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(Path("C:/Apps/Argus/Argus.exe")))
    assert argus_app.root() == Path("C:/Apps/Argus")
    assert argus_app.log_path() == Path("C:/Apps/Argus/argus.log")
    assert argus_app.seed_data() == []


def test_running_from_sources_the_root_is_the_current_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    assert argus_app.root() == tmp_path


def test_the_bundle_is_found_from_the_executable(on_mac, monkeypatch):
    exe = on_mac / "Applications" / "Argus.app" / "Contents" / "MacOS" / "Argus"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert host.app_bundle() == exe.parents[2].resolve()
    assert host.app_bundle().suffix == ".app"


def test_outside_a_bundle_there_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))
    assert host.app_bundle() is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert host.app_bundle() is None


# --------------------------------------------------------------------------
# Seeds
# --------------------------------------------------------------------------


def make_seed(base: Path, version: str = "1.0+aaa", toml: str = "[brain]\n") -> Path:
    seed = base / "seed"
    (seed / "models").mkdir(parents=True, exist_ok=True)
    (seed / "argus.toml").write_text(toml, encoding="utf-8")
    (seed / ".env").write_text("OPENAI_API_KEY=k\n", encoding="utf-8")
    (seed / "models" / "silero_vad.onnx").write_bytes(b"onnx")
    (seed / "argus.local.json").write_text(json.dumps({"channel": {"tts": {}}}), encoding="utf-8")
    (seed / "VERSION").write_text(version, encoding="utf-8")
    return seed


def test_the_first_launch_lays_out_everything_from_the_seed(tmp_path):
    seed, data = make_seed(tmp_path), tmp_path / "data"
    done = host.seed_user_data(seed, data)
    assert {"argus.toml", ".env", "VERSION", "argus.local.json"} <= set(done)
    assert (data / "models" / "silero_vad.onnx").read_bytes() == b"onnx"
    assert (data / host.SEED_VERSION_MARK).read_text() == "1.0+aaa"


def test_a_second_launch_of_the_same_version_touches_nothing(tmp_path):
    seed, data = make_seed(tmp_path), tmp_path / "data"
    host.seed_user_data(seed, data)
    (data / "argus.toml").write_text("[brain]\nprovider = 'mine'\n", encoding="utf-8")
    assert host.seed_user_data(seed, data) == []
    assert "mine" in (data / "argus.toml").read_text(), "the person's edits are not overwritten without a version change"


def test_a_new_version_refreshes_the_files_but_keeps_the_personal_ones(tmp_path):
    seed, data = make_seed(tmp_path), tmp_path / "data"
    host.seed_user_data(seed, data)
    (data / "argus.local.json").write_text(json.dumps({"mine": True}), encoding="utf-8")
    (data / "argus.account.json").write_text("{}", encoding="utf-8")
    (data / "sessions").mkdir()
    (data / "sessions" / "keep.txt").write_text("recording", encoding="utf-8")

    make_seed(tmp_path, version="1.0+bbb", toml="[brain]\nprovider = 'new'\n")
    done = host.seed_user_data(seed, data)

    assert "argus.toml" in done and "argus.local.json" not in done
    assert "new" in (data / "argus.toml").read_text()
    assert json.loads((data / "argus.local.json").read_text()) == {"mine": True}
    assert (data / "argus.account.json").exists() and (data / "sessions" / "keep.txt").exists()
    assert (data / host.SEED_VERSION_MARK).read_text() == "1.0+bbb"


def test_a_lost_config_is_laid_out_again_even_on_the_same_version(tmp_path):
    seed, data = make_seed(tmp_path), tmp_path / "data"
    host.seed_user_data(seed, data)
    (data / "argus.toml").unlink()
    assert "argus.toml" in host.seed_user_data(seed, data)


def test_a_file_that_cannot_be_copied_is_skipped_not_fatal(tmp_path, monkeypatch):
    seed, data = make_seed(tmp_path), tmp_path / "data"
    real = host.shutil.copy2

    def picky(src, dst, *a, **k):
        if str(src).endswith(".env"):
            raise PermissionError("no permission")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(host.shutil, "copy2", picky)
    done = host.seed_user_data(seed, data)
    assert "argus.toml" in done and ".env" not in done


def test_the_entry_point_lays_out_the_seed_from_the_bundle(on_mac, monkeypatch):
    seed = make_seed(on_mac / "bundle")
    monkeypatch.setattr(host, "seed_dir", lambda: seed)
    done = argus_app.seed_data()
    assert "argus.toml" in done
    assert (host.user_data_dir() / "argus.toml").exists()


def test_without_a_seed_there_is_nothing_to_lay_out(on_mac, monkeypatch):
    monkeypatch.setattr(host, "seed_dir", lambda: None)
    assert argus_app.seed_data() == []


def test_the_seed_is_searched_in_both_places_pyinstaller_may_put_data(on_mac, monkeypatch):
    bundle = on_mac / "Argus.app"
    resources = bundle / "Contents" / "Resources" / "seed"
    resources.mkdir(parents=True)
    monkeypatch.setattr(host, "app_bundle", lambda: bundle)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle / "Contents" / "Frameworks"), raising=False)
    assert host.seed_dir() == resources
    frameworks = bundle / "Contents" / "Frameworks" / "seed"
    frameworks.mkdir(parents=True)
    assert host.seed_dir() == frameworks


# --------------------------------------------------------------------------
# The log and the error window
# --------------------------------------------------------------------------


def test_the_app_without_a_terminal_writes_its_log_to_the_library(on_mac, monkeypatch):
    out, err = sys.stdout, sys.stderr
    try:
        monkeypatch.setattr(sys, "stdout", io.StringIO())     # there is a stream, but no terminal
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        argus_app.prepare_output()
        print("log check")
        sys.stdout.flush()
        assert "log check" in (host.logs_dir() / "argus.log").read_text(encoding="utf-8")
    finally:
        if sys.stdout is not out:
            sys.stdout.close()
        sys.stdout, sys.stderr = out, err
        import logging

        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)


def test_a_failed_start_shows_a_system_alert_on_macos(on_mac, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    argus_app.report_startup_failure(RuntimeError("port is busy"))
    assert calls and calls[0][:2] == ["osascript", "-e"]
    script = calls[0][2]
    assert script.startswith('display alert "Argus" message ') and script.endswith("as critical")
    assert "port is busy" in script and "Console" in script


def test_quotes_in_the_error_cannot_break_the_alert_script(on_mac, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    argus_app.report_startup_failure(RuntimeError('he said "no" and \\ left'))
    script = calls[0][2]
    assert '\\"no\\"' in script and "\\\\" in script


def test_a_broken_alert_does_not_hide_the_original_failure(on_mac, monkeypatch):
    def broken(*a, **k):
        raise OSError("no osascript")

    monkeypatch.setattr(subprocess, "run", broken)
    argus_app.report_startup_failure(RuntimeError("port is busy"))        # does not crash


# --------------------------------------------------------------------------
# Build files: what fails not in a test, but half an hour later at Apple's
# --------------------------------------------------------------------------

SPEC = (PACKAGING_DIR / "argus_mac.spec")
BUILD_SCRIPT = (PACKAGING_DIR / "build_macos.sh")
ENTITLEMENTS = (PACKAGING_DIR / "macos" / "entitlements.plist")


def test_the_entitlements_are_plain_ascii_and_exactly_the_three_needed():
    """Apple: the entitlements list must be ASCII, without a BOM. Cyrillic in a comment passes
    `codesign`, but notarization then answers "Embedded entitlements are invalid"."""
    import plistlib

    raw = ENTITLEMENTS.read_bytes()
    raw.decode("ascii")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert plistlib.loads(raw) == {
        "com.apple.security.device.audio-input": True,
        "com.apple.security.cs.allow-unsigned-executable-memory": True,
        "com.apple.security.cs.disable-library-validation": True,
    }


def test_the_build_script_refuses_entitlements_that_apple_would_refuse():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'plutil -lint "$ENTITLEMENTS"' in script
    assert "[^[:print:][:space:]]" in script, "non-ASCII in the entitlements is caught before signing, not after notarization"


def test_the_minimum_system_is_the_one_the_locked_wheels_need():
    """The onnxruntime library from the lock file is built for macOS 14: it will not load on 13."""
    assert '"LSMinimumSystemVersion": "14.0"' in SPEC.read_text(encoding="utf-8")


def test_only_the_build_script_signs_for_real():
    """PyInstaller would sign everything a second time, which is an extra trip to Apple's time
    server, where a single failure brings down the build.
    """
    spec = SPEC.read_text(encoding="utf-8")
    assert "codesign_identity=None" in spec
    assert "codesign_identity=IDENTITY" not in spec
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "sign() { retry 3 codesign" in script, "a network failure during signing is retried instead of failing the build"


def test_the_helper_gets_no_rights_it_does_not_need():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    lines = [ln for ln in script.splitlines() if "argus-syscap" in ln and ln.strip().startswith("sign ")]
    assert len(lines) == 1 and "--entitlements" not in lines[0]


def test_windows_files_never_get_into_the_mac_bundle():
    spec = SPEC.read_text(encoding="utf-8")
    assert '".dll"' in spec and "a.binaries = [" in spec and "a.datas = [" in spec


def test_the_dmg_is_notarized_too_not_only_the_app():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'notarize "$OUT/Argus-mac.dmg"' in script and 'stapler staple "$OUT/Argus-mac.dmg"' in script


def test_the_workflow_does_not_cancel_a_running_notarization_and_needs_full_history():
    flow = (PACKAGING_DIR.parent / ".github" / "workflows" / "macos.yml").read_text(encoding="utf-8")
    assert "cancel-in-progress: false" in flow
    assert "fetch-depth: 0" in flow, "the build number is counted by commits; on a shallow clone it is always 1"
    assert "permissions:" in flow and "contents: read" in flow
    assert "bash packaging/build_macos.sh" in flow, "from Windows the file arrives without the execute bit"


def test_the_dmg_is_compressed_with_lzma_because_it_is_downloaded_over_a_slow_link():
    assert "-format ULMO" in BUILD_SCRIPT.read_text(encoding="utf-8")


def test_the_download_page_has_a_mac_card_and_no_commit_hash():
    """The page is generated by `deploy/mac/publish_mac.py`; a commit hash on it is of no use to a
    person.
    """
    page = (PACKAGING_DIR.parent / "deploy" / "mac" / "index.src.html").read_text(encoding="utf-8")
    assert 'id="mac"' in page and 'href="Argus-mac.dmg"' in page
    assert "{{MAC_VERSION}}" in page and "commit" not in page.lower()
    assert "<!--legal-->" in page and "<!--/legal-->" in page, "the links to the documents are behind markers: until they exist, the page goes out without them"
