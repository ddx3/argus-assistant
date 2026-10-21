"""Preloading of heavy libraries: it does not interfere and can be switched off."""

from __future__ import annotations

import sys

from argus import prefetch


def test_prefetch_loads_the_heavy_libraries_in_the_background():
    thread = prefetch.start()

    assert thread is not None and thread.daemon, "a daemon thread: it does not keep the process from exiting"
    thread.join(60)
    assert not thread.is_alive()
    for name in ("numpy", "cv2", "onnxruntime"):
        assert name in sys.modules, f"{name} was not loaded ahead of time"


def test_prefetch_can_be_switched_off(monkeypatch):
    """To compare the launch with and without it on the same build."""
    monkeypatch.setenv("ARGUS_NO_PREFETCH", "1")
    assert prefetch.start() is None


def test_a_library_that_is_missing_does_not_break_the_launch(monkeypatch, caplog):
    """A real `import` in the right place will report it itself; here we stay silent."""
    monkeypatch.setattr(prefetch, "HEAVY", ("no_such_library_argus", "numpy"))

    prefetch._run()                                   # does not throw

    assert "numpy" in sys.modules
