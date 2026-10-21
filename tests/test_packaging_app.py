"""Entry point of the built `.exe`: what used to fail silently.

`packaging/argus_app.py` is not a package and is not on the default import path, so we
import it explicitly, only here. One thing is tested: a failed start (whether it is the
window itself or something before it) now leaves a trace on screen, not only in
`argus.log`, which you still have to find.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGING_DIR = Path(__file__).parent.parent / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import argus_app  # noqa: E402 — the path was just added, above


@pytest.mark.windows_only
def test_a_failed_start_shows_a_message_box_not_just_a_log(monkeypatch):
    """Without a console, an uncaught exception used to kill the process without a trace."""
    shown = []
    import ctypes

    # `argus_app.py` imports `ctypes` inside a function, not at module level, so we patch the
    # shared, cached `ctypes.windll.user32` directly. It is the same object the function will
    # see.
    monkeypatch.setattr(
        ctypes.windll.user32, "MessageBoxW",
        lambda *a: shown.append(a) or 1,
    )
    argus_app.report_startup_failure(RuntimeError("port is busy"))
    assert shown, "nothing was shown on the screen about the startup failure"
    assert "port is busy" in shown[0][1]


@pytest.mark.windows_only
def test_reporting_survives_a_broken_message_box(monkeypatch):
    """Showing at least something matters more than crashing while trying to show a refusal."""
    import ctypes

    def boom(*a):
        raise OSError("no user32")

    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", boom)
    argus_app.report_startup_failure(RuntimeError("something is off"))  # does not crash


def test_main_catches_a_failure_from_run_desktop_instead_of_vanishing(monkeypatch):
    """Before, `main()` caught nothing: any exception was a silent exit."""
    monkeypatch.setattr(argus_app, "prepare_output", lambda: None)
    monkeypatch.setattr(argus_app, "hide_child_consoles", lambda: None)
    monkeypatch.setattr(argus_app, "warm_gui", lambda: None)    # do not load .NET in the test
    monkeypatch.setattr(argus_app, "seed_platform", lambda: None)   # and do not touch the `platform` cache
    monkeypatch.setattr(argus_app.multiprocessing, "freeze_support", lambda: None)

    import argus.desktop as desktop
    import argus.update as update
    # Without this, the test depends on whether the real window mutex on this machine is
    # already taken (by another live Argus.exe), and on the network, which `maybe_self_update`
    # really uses. The test checks the handling of the exception from `run_desktop`, not these
    # two things before it.
    monkeypatch.setattr(desktop, "claim_instance_or_focus", lambda *a, **kw: True)
    monkeypatch.setattr(update, "maybe_self_update", lambda *a, **kw: False)
    monkeypatch.setattr(
        desktop, "run_desktop",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("port is busy")),
    )

    reported = []
    monkeypatch.setattr(argus_app, "report_startup_failure", reported.append)

    assert argus_app.main() == 1
    assert len(reported) == 1
    assert "port is busy" in str(reported[0])


@pytest.mark.skipif(sys.platform != "win32", reason="MessageBoxW exists only on Windows")
def test_message_box_flag_is_the_error_icon():
    """0x10 is `MB_ICONERROR`: not just any icon, but specifically the alarming one."""
    import inspect

    src = inspect.getsource(argus_app.report_startup_failure)
    assert "0x10" in src
