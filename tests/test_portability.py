"""The package must import where there is no Windows.

The app is also built for macOS, and there is no `pyaudiowpatch`, no `bettercam`, no
`windows-capture`, no `pywin32`, no `comtypes` and no `ctypes.windll` there. Any module
that pulls them in at import level brings the whole startup down: no window, no log,
"it does not open".

The check runs in a clean interpreter, not in the pytest process: to remove `windll` and
block the Windows packages, you first have to load the standard library and the
third-party packages (they use `_winapi` and `msvcrt` themselves), and doing that in the
shared interpreter would break neighboring tests. Two passes:

1. The Windows packages are blocked, the platform is real: this catches `import
   pyaudiowpatch` and the like at module level.
2. Everything is already loaded; the platform is declared `darwin`, `windll` is removed,
   and the `argus` modules are loaded anew: this catches branches like
   `if sys.platform == "win32"` and uses of `ctypes.windll` at module level.

We do not fake the platform before the second pass: it would break `asyncio` and
`subprocess`. Real macOS is checked by CI (`.github/workflows/macos.yml`); here is
everything that can be caught from Windows.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Modules that need Windows by definition: they are called only from there.
WINDOWS_ONLY = {
    "argus.capture.app_loopback",   # WASAPI process loopback, COM
}

SCRIPT = textwrap.dedent(
    r"""
    import asyncio, ctypes, ctypes.wintypes, json, logging, multiprocessing, pathlib  # noqa: F401
    import smtplib, sqlite3, ssl, subprocess, sys, threading, tomllib, urllib.request, uuid  # noqa: F401
    import webbrowser, zipfile, shutil, tempfile, platform, locale  # noqa: F401

    BLOCKED = {
        "pyaudiowpatch", "bettercam", "windows_capture", "win32gui", "win32ui",
        "win32api", "win32con", "win32process", "win32com", "pywintypes", "pythoncom",
        "comtypes", "clr", "clr_loader", "pythonnet", "winreg",
    }

    class Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ImportError(f"{fullname}: no such package on this system")
            return None

    sys.meta_path.insert(0, Blocker())
    for name in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
        del sys.modules[name]

    sys.path.insert(0, SRC)
    import argus, pkgutil, importlib

    skip = set(json.loads(SKIP))
    failed = {}
    seen = {"count": 0}

    def walk(tag):
        for info in pkgutil.walk_packages(
            argus.__path__, "argus.", onerror=lambda n: failed.setdefault(f"{tag} {n}", "walk")
        ):
            if info.name in skip:
                continue
            try:
                importlib.import_module(info.name)
                seen["count"] += 1
            except BaseException as e:
                failed[f"{tag} {info.name}"] = f"{type(e).__name__}: {e}"

    # Pass 1: the real platform, only the Windows packages are blocked.
    walk("[packages]")

    # Pass 2: as on a Mac — no windll, platform darwin, argus modules anew.
    for name in ("windll", "oledll", "WinDLL", "OleDLL", "WINFUNCTYPE", "WinError",
                 "GetLastError", "get_last_error", "set_last_error", "FormatError",
                 "DllCanUnloadNow", "DllGetClassObject"):
        if hasattr(ctypes, name):
            delattr(ctypes, name)
    for name in [m for m in sys.modules if m == "argus" or m.startswith("argus.")]:
        del sys.modules[name]
    sys.platform = "darwin"
    import argus, pkgutil, importlib
    walk("[darwin]")

    print("RESULT" + json.dumps({"failed": failed, "count": seen["count"]}, ensure_ascii=False))
    """
)


def test_every_module_imports_without_windows():
    code = SCRIPT.replace("SRC", repr(str(SRC))).replace("SKIP", repr(json.dumps(sorted(WINDOWS_ONLY))))
    run = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True, encoding="utf-8", timeout=240,
    )
    line = next((x for x in run.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line is not None, f"the check did not run:\n{run.stdout[-1500:]}\n{run.stderr[-2500:]}"
    result = json.loads(line[len("RESULT"):])
    failed = result["failed"]
    assert not failed, "these do not import without Windows:\n" + "\n".join(
        f"  {name}: {why}" for name, why in sorted(failed.items())
    )
    # A check that loaded nothing can always be green: the package has hundreds of modules, and
    # two passes over them are hundreds of imports, not a couple.
    assert result["count"] > 150, f"only {result['count']} modules were loaded"
