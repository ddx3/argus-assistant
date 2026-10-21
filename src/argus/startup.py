"""Startup timestamps, so that `argus.log` shows where the start time goes.

We count not from the import of this module but from process creation: the PyInstaller
loader and Python itself start before the first line of our code, and without this "window
in 1.2 s" would be honest only for the part we can see. The system provides the process
creation time.

The mark is a single line in the log (`startup: window shown — 1180 (ms since process
start)`): it shows whether a slow startup has returned, without a stopwatch and without a
separate build.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from ctypes import wintypes

log = logging.getLogger("argus.startup")

_mac_started: float | None = None


def _mac_process_start() -> float | None:
    """When the process was created (seconds since the epoch) on macOS. We ask `ps` once and
    remember the answer, including a failure.

    `ps` gives the time to the second: coarse for marks like "window after 1180 ms", but
    enough to show that a start drags on for seconds rather than fractions of a second.
    """
    global _mac_started
    if _mac_started is None:
        try:
            out = subprocess.run(
                ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
                capture_output=True, text=True, timeout=1, env={**os.environ, "LC_ALL": "C"},
            ).stdout.strip()
            _mac_started = time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
        except (OSError, ValueError, subprocess.SubprocessError):
            # We remember a failure too: otherwise every `mark` would start `ps` again, and a hung `ps`
            # would hold the very thread that is trying to show the window for the whole wait, each
            # time.
            _mac_started = -1.0
    return None if _mac_started < 0 else _mac_started


def process_age_ms() -> float | None:
    """How many milliseconds the process has lived. `None` means the system did not answer, or
    this is neither Windows nor macOS.

    Our own `WinDLL`, not the shared `ctypes.windll.kernel32`: argument types are set by hand
    here (the process pseudo-handle is -1, and as an ordinary `int` it does not reach a 64-bit
    call), and the shared object must not share them, because the rest of the app uses it.
    """
    if sys.platform == "darwin":
        started = _mac_process_start()
        return None if started is None else max(0.0, (time.time() - started) * 1000.0)
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]

        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(
            k32.GetCurrentProcess(),
            ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        now = wintypes.FILETIME()
        k32.GetSystemTimeAsFileTime(ctypes.byref(now))
    except (OSError, AttributeError, ValueError):
        return None

    def ticks(ft: wintypes.FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    return (ticks(now) - ticks(created)) / 10_000.0    # FILETIME units, 100 ns each


def mark(what: str) -> None:
    """Write to the log how much time has passed from the process launch to `what`."""
    age = process_age_ms()
    log.info("startup: %s — %s (ms since process start)", what, "?" if age is None else f"{age:.0f}")
