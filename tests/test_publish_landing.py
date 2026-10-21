"""Landing publishing (`deploy/landing/publish_landing.py`, `check_live.py`) and serving
(`nginx/default.conf`).

From the 2026-10-14 audit: the release was switched on before the check, a failure printed
a hint and left a broken page until a manual `--rollback`; the check rigidly required the
Mac files, so publishing the landing alone always "failed"; public pages went out with
placeholders instead of the operator's mail address; `check_live.py` always ended
successfully; `/download` served the Windows 7/8 build to Windows 10, and an iPad got the
Mac image.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "landing"))
sys.path.insert(0, str(ROOT / "deploy"))

import publish_landing  # noqa: E402

PLACEHOLDER = '<p>Write to <a href="mailto:TODO-email">us</a> <mark class="todo">[email]</mark></p>'


def test_placeholders_in_the_public_legal_pages_stop_the_publish(tmp_path):
    for name in publish_landing.LEGAL_PAGES:
        (tmp_path / name).write_text(PLACEHOLDER if name == "terms.html" else "<p>ok</p>", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        publish_landing.check_release(tmp_path)
    assert "with placeholders instead of" in str(stopped.value) and "terms.html" in str(stopped.value)


def test_the_placeholders_can_be_allowed_on_purpose(tmp_path, capsys):
    (tmp_path / "terms.html").write_text(PLACEHOLDER, encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:                    # the rest of the release is not ready anyway; this is not about the terms
        publish_landing.check_release(tmp_path, allow_legal_placeholders=True)
    assert "with placeholders instead of" not in str(stopped.value)
    assert "WARNING" in capsys.readouterr().out


def test_the_help_page_is_checked_too(tmp_path):
    (tmp_path / "help.html").write_text('<p>Contact: <mark class="todo">[contact]</mark></p>', encoding="utf-8")
    with pytest.raises(SystemExit, match="help.html"):
        publish_landing.check_release(tmp_path)


# --------------------------------------------------------------------------
# checking what is published
# --------------------------------------------------------------------------


@pytest.fixture
def probe(monkeypatch, tmp_path):
    release = tmp_path / "release"
    (release / "img" / "en").mkdir(parents=True)
    for name in publish_landing.PAGES:
        (release / name).write_text("x", encoding="utf-8")
    (release / "img" / "en" / "a.png").write_bytes(b"png")
    asked: list[str] = []

    class FakeProbe:
        def __init__(self, base):
            self.failed = 0

        def request(self, path, method="GET", headers=None):
            asked.append(path)
            return 200, {}, b"x"

        def check(self, label, ok, detail=""):
            if not ok:
                self.failed += 1

    monkeypatch.setattr(publish_landing, "Probe", FakeProbe)
    return SimpleNamespace(release=release, asked=asked)


def test_the_mac_checks_run_only_when_the_mac_files_are_on_the_server(probe, capsys):
    """Publishing the landing page alone, without the Mac files, always "failed", and the owner got
    used to not trusting the result.
    """
    publish_landing.smoke(probe.release, "http://x", windows_zip_present=True, mac_files_present=False)
    assert "/Argus-mac.dmg" not in probe.asked and "/latest-mac.json" not in probe.asked
    assert "were skipped" in capsys.readouterr().out
    probe.asked.clear()
    publish_landing.smoke(probe.release, "http://x", windows_zip_present=True, mac_files_present=True)
    assert "/Argus-mac.dmg" in probe.asked and "/latest-mac.json" in probe.asked


def run_main(monkeypatch, tmp_path, smoke_result, previous="landing-20260101-000000"):
    monkeypatch.setattr(sys, "argv", ["publish_landing.py", "--no-build"])
    monkeypatch.setattr(publish_landing, "LANDING", tmp_path)
    (tmp_path / "out").mkdir()
    calls: list[tuple] = []
    monkeypatch.setattr(publish_landing, "assemble", lambda built, release: release.mkdir())
    monkeypatch.setattr(publish_landing, "check_release", lambda release, allow=False: None)
    monkeypatch.setattr(publish_landing, "current_release", lambda: previous)
    monkeypatch.setattr(publish_landing, "upload", lambda release: "landing-20260202-000000")
    monkeypatch.setattr(publish_landing, "switch_to", lambda name: calls.append(("switch", name)))
    monkeypatch.setattr(publish_landing, "prune", lambda: calls.append(("prune",)))
    monkeypatch.setattr(publish_landing, "ssh", lambda command, **k: "yes")
    monkeypatch.setattr(publish_landing, "smoke", lambda *a, **k: smoke_result)
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda **k: None, raising=False)
    return publish_landing.main(), calls


def test_a_failed_check_brings_the_previous_release_back_by_itself(monkeypatch, tmp_path, capsys):
    code, calls = run_main(monkeypatch, tmp_path, smoke_result=2)
    assert code == 1
    assert calls == [("switch", "landing-20260202-000000"), ("switch", "landing-20260101-000000")], \
        "a broken page does not stay on the site until a manual --rollback; old releases (prune) are untouched"
    assert "automatically restored the previous release" in capsys.readouterr().out


def test_a_good_check_keeps_the_new_release_and_prunes_old_ones(monkeypatch, tmp_path):
    code, calls = run_main(monkeypatch, tmp_path, smoke_result=0)
    assert code == 0 and calls == [("switch", "landing-20260202-000000"), ("prune",)]


def test_the_live_check_ends_with_an_exit_code():
    """Before, `check_live.py` printed "FAILURES: n" and always ended with exit code 0,
    so it guarded nothing.
    """
    text = (ROOT / "deploy" / "landing" / "check_live.py").read_text(encoding="utf-8")
    assert "sys.exit(1 if bad else 0)" in text.strip().splitlines()[-1]


# --------------------------------------------------------------------------
# nginx
# --------------------------------------------------------------------------


def conf() -> str:
    return (ROOT / "deploy" / "landing" / "nginx" / "default.conf").read_text(encoding="utf-8")


def test_only_windows_10_and_up_are_sent_the_windows_build():
    """`windows nt` also matched Windows 7/8 (NT 6.x), but the build is for Windows 10 version 2004
    and newer.
    """
    text = conf()
    assert '"~*windows nt 10"' in text and '"~*windows nt"' not in text
