"""Entry point of the built app: the log, and starting without a settings file.

From the 2026-10-14 audit: `faulthandler` (`-X faulthandler` in `argus_mac.spec`) was
bound to the real stderr, and after `sys.stderr` was replaced by the log file, the stacks
from a crash did not end up there; the log grew without limit; without `argus.toml` next
to the program, `Config.load(None)` looked for it from the shortcut's working folder and
could pick up someone else's; a program in a folder without write permission crashed on
the first line and named a log that cannot exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGING_DIR = Path(__file__).parent.parent / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import argus_app  # noqa: E402 — the path was just added, above


def test_a_fatal_error_dumps_the_thread_stacks_into_the_log_file(tmp_path):
    """A real crash of the interpreter in a separate process: the stacks must end up in the log,
    not nowhere.
    """
    log = tmp_path / "argus.log"
    script = (
        "import sys, faulthandler\n"
        f"sys.path.insert(0, {str(PACKAGING_DIR)!r})\n"
        "import argus_app, pathlib\n"
        f"argus_app.log_path = lambda: pathlib.Path({str(log)!r})\n"
        "sys.stdout = None\nsys.stderr = None\n"          # like a windowed app without a console
        "argus_app.prepare_output()\n"
        "faulthandler._sigsegv()\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "Fatal Python error" in text and "most recent call first" in text


def test_the_log_is_rotated_when_it_gets_too_big(tmp_path):
    log = tmp_path / "argus.log"
    log.write_bytes(b"x" * 100)
    argus_app.rotate_log(log, max_bytes=1000)
    assert log.exists() and not (tmp_path / "argus.log.1").exists(), "a small log is left alone"
    for generation in range(5):                                   # five big logs in a row
        log.write_bytes(f"gen{generation}".encode() + b"x" * 2000)
        argus_app.rotate_log(log, max_bytes=1000, keep=3)
        assert not log.exists() or log.stat().st_size <= 1000
    assert (tmp_path / "argus.log.1").read_bytes().startswith(b"gen4")
    assert (tmp_path / "argus.log.2").read_bytes().startswith(b"gen3")
    assert (tmp_path / "argus.log.3").read_bytes().startswith(b"gen2")
    assert not (tmp_path / "argus.log.4").exists(), "copies older than three are not kept"
    argus_app.rotate_log(tmp_path / "no-such-file")                 # no file: does not crash


def test_the_log_goes_to_the_local_data_folder_when_the_program_folder_is_read_only(monkeypatch, tmp_path):
    """Unpacked into `C:\\Program Files`: a log cannot be created next to the program."""
    monkeypatch.setattr(argus_app.sys, "platform", "win32")
    monkeypatch.setattr(argus_app.sys, "frozen", True, raising=False)
    (tmp_path / "Program Files" / "Argus").mkdir(parents=True)
    monkeypatch.setattr(argus_app, "root", lambda: tmp_path / "Program Files" / "Argus")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(argus_app, "_writable", lambda folder: False)
    assert argus_app.log_path() == tmp_path / "Local" / "Argus" / "argus.log"
    monkeypatch.setattr(argus_app, "_writable", lambda folder: True)
    assert argus_app.log_path() == tmp_path / "Program Files" / "Argus" / "argus.log"


def test_a_really_read_only_folder_is_not_writable(tmp_path):
    assert argus_app._writable(tmp_path) is True
    assert argus_app._writable(tmp_path / "no-such-folder") is False


def test_the_failure_box_names_the_real_place_of_the_log(monkeypatch, tmp_path):
    if os.name != "nt":
        pytest.skip("the message window is Windows only")
    import ctypes

    shown = []
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *a: shown.append(a) or 1)
    monkeypatch.setattr(argus_app, "root", lambda: tmp_path / "Program Files" / "Argus")
    monkeypatch.setattr(argus_app, "log_path", lambda: tmp_path / "Local" / "Argus" / "argus.log")
    argus_app.report_startup_failure(RuntimeError("no permission"))
    assert str(tmp_path / "Local" / "Argus" / "argus.log") in shown[0][1]
    assert "next to the program" not in shown[0][1], "such a file cannot be there, so we do not name it"


def test_a_frozen_app_without_its_settings_file_says_so_instead_of_adopting_a_foreign_one(monkeypatch, tmp_path):
    """Without `argus.toml` next to it, `Config.load(None)` looked for it from the shortcut's
    folder (the desktop, System32) and could pick up a foreign one.
    """
    monkeypatch.setattr(argus_app, "prepare_output", lambda: None)
    monkeypatch.setattr(argus_app, "hide_child_consoles", lambda: None)
    monkeypatch.setattr(argus_app, "warm_gui", lambda: None)
    monkeypatch.setattr(argus_app, "seed_platform", lambda: None)
    monkeypatch.setattr(argus_app, "seed_data", lambda: [])
    monkeypatch.setattr(argus_app.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(argus_app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(argus_app, "root", lambda: tmp_path)          # there is no argus.toml here

    from argus.core import config as config_module

    def foreign(path=None):
        raise AssertionError("must not look for the settings from the working folder")

    monkeypatch.setattr(config_module.Config, "load", staticmethod(foreign))
    reported = []
    monkeypatch.setattr(argus_app, "report_startup_failure", reported.append)

    assert argus_app.main() == 1
    assert len(reported) == 1 and "argus.toml" in str(reported[0]) and str(tmp_path) in str(reported[0])
