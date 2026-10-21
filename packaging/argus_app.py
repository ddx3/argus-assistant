"""Entry point of the built app.

It differs from `argus run` in two things, and both are about the app having no terminal. It
does not parse arguments — there is nobody to pass them. And it computes the root from
itself, not from the current folder: a desktop shortcut starts the program with any current
folder whatsoever, and next to the exe lie `argus.toml`, `.env`, `models` and `sessions`.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def root() -> Path:
    """The folder next to which the settings, weights and recordings lie.

    On macOS this is `~/Library/Application Support/Argus`, not a folder next to the program: the
    `.app` is signed, and a file added inside breaks the signature; besides, an app launched from
    "Downloads" is mounted by the system read-only. The folder is filled with the seed data from
    the package (`seed_data`) before the settings are first read.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            from argus import host

            return host.user_data_dir()
        return Path(sys.executable).parent
    return Path.cwd()


# The log does not grow without limit: at startup, if it is larger than this, it moves to
# `argus.log.1` (and so on up to `.3`).
MAX_LOG_BYTES = 5_000_000
LOG_BACKUPS = 3

# The open log lives as long as the process: `faulthandler` writes to it, and a closed
# descriptor means lost stacks.
_LOG_HANDLE = None


def _writable(folder: Path) -> bool:
    """Whether a file can really be created in the folder (`os.access` on Windows does not look at
    the folder's permissions).
    """
    probe = folder / f".write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def log_path() -> Path:
    """Where the log lives. On macOS — in `~/Library/Logs/Argus`, where Console and support look
    for it.

    On Windows — next to the program, and if it cannot be written there (unpacked into
    `C:\\Program Files`), — in `%LOCALAPPDATA%\\Argus`: otherwise the app crashed on its very first
    line, and there was nothing to say why.
    """
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        from argus import host

        return host.logs_dir() / "argus.log"
    here = root()
    if sys.platform == "win32" and getattr(sys, "frozen", False) and here.is_dir() and not _writable(here):
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Argus" / "argus.log"
    return here / "argus.log"


def rotate_log(path: Path, max_bytes: int = MAX_LOG_BYTES, keep: int = LOG_BACKUPS) -> None:
    """If the log is larger than `max_bytes`, move it to `.1` (older ones shift, the oldest is
    erased). If that fails, carry on silently.
    """
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return
        for number in range(keep, 0, -1):
            source = path if number == 1 else path.with_name(f"{path.name}.{number - 1}")
            if source.exists():
                os.replace(source, path.with_name(f"{path.name}.{number}"))
    except OSError:
        pass


def seed_data() -> list[str]:
    """macOS: lay out the seed data from the package into the data folder (first launch and a new
    version).

    On Windows the seed data lies next to `Argus.exe` from the start (`packaging/stage.py`), so
    there is nothing to do. Returns what was laid out — for the log.
    """
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return []
    from argus import host

    seed = host.seed_dir()
    if seed is None:
        return []
    return host.seed_user_data(seed, host.user_data_dir())


def prepare_output() -> None:
    """Give the output somewhere to go.

    Two different troubles, and both bring down the launch before the window appears. In an app
    built without a console `sys.stdout` is `None` altogether, and the very first line of the
    startup report fails with `AttributeError`. In one built with a console it exists, but speaks
    in the system encoding — and the output here is not plain ASCII (dashes, ellipses, folder
    names), and `charmap` dies on it.

    Hence: if there is no stream, we write to a file next to the program (which is also the only
    thing we can ask a person to send when "it doesn't open"); if there is a stream, we switch it
    to UTF-8 and allow replacing what cannot be encoded.
    """
    import logging

    # On macOS an app started from Finder has no terminal: the streams are either `None` or lead
    # nowhere — the log to a file is always needed.
    app_without_terminal = sys.platform == "darwin" and getattr(sys, "frozen", False)
    if sys.stdout is None or sys.stderr is None or app_without_terminal:
        # Nobody is there to close it, and there is no need: it lives exactly as long as the program,
        # and closing it would mean losing the last lines.
        global _LOG_HANDLE
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(path)
        handle = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = handle
        _LOG_HANDLE = handle
        # `-X faulthandler` (argus_mac.spec) binds the dump to the real stderr (descriptor 2) before
        # this replacement; for an app started from Finder it is /dev/null, and thread stacks did not
        # reach the log on a crash. We enable it again — into the file.
        try:
            import faulthandler

            faulthandler.enable(file=handle, all_threads=True)
        except (OSError, ValueError, RuntimeError):
            pass
    else:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def hide_child_consoles() -> None:
    """Forbid child processes from creating their own windows.

    The app is built as a windowed one, it has no console. But the helper executable that brings
    up the subscription path to the model is a console program, and Windows gives it a window: on
    every launch a black rectangle pops up over the screen. For a channel whose whole point is to
    be invisible, this is the worst possible bug.

    The flag is set once for the whole process, not at the launch site: the helper is started not
    by our code but by `anyio` inside the SDK, and there is nothing to reach its call with. Flags
    already set by the caller are kept — it is not our business to cancel them.
    """
    if sys.platform != "win32":
        return

    import subprocess

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):          # type: ignore[no-untyped-def]
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | no_window
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched          # type: ignore[method-assign]


def report_startup_failure(error: BaseException) -> None:
    """Show on screen what otherwise only `argus.log` would show.

    Without a console an uncaught exception simply kills the process — no window, no line on the
    screen, the shortcut "doesn't open", and why is known only to a file whose existence you
    still have to guess. `MessageBoxW` is one of the few things that can be shown before any
    window: Python itself and WebView2 may not be ready by that moment, but the system dialog is
    always ready.
    """
    import ctypes
    import traceback

    try:
        place = log_path()
    except Exception:                              # noqa: BLE001 — saying at least something matters more
        place = None
    if sys.platform == "darwin":
        where = "in argus.log (Console → Reports → Logs)"
    elif place is not None and place.parent != root():
        where = f"in {place}"           # writing next to the program is not allowed — the log lives elsewhere
    else:
        where = "in argus.log next to the program"
    # To the person — one clear phrase: the raw exception text (library names, addresses, paths)
    # does not go on screen. The full text and stack stay in the log (`print_exception` below).
    # The path in parentheses ("(C:\...)") is cut out of our own phrase instead of rejecting the
    # phrase as a whole: "Settings file argus.toml not found… Unpack the archive
    # again" is more useful to the person than a generic excuse.
    try:
        import re

        from argus.core import friendly

        text_of = re.sub(r"\s*\([^()]*[\\/][^()]*\)", "", str(error))
        shown = friendly.plain_or(text_of, "something went wrong")
    except Exception:                              # noqa: BLE001 — saying at least something matters more
        shown = "something went wrong"
    text = f"Failed to start:\n{shown}\n\nDetails — {where}."
    if sys.platform == "darwin":
        # A system window without the app's own window: `osascript` exists on any macOS and shows it
        # even when pywebview is not up yet.
        try:
            import json
            import subprocess

            subprocess.run(
                ["osascript", "-e",
                 f"display alert \"Argus\" message {json.dumps(text, ensure_ascii=False)} as critical"],
                timeout=120, check=False, capture_output=True,
            )
        except Exception:                         # noqa: BLE001 — showing at least something matters more
            pass
    else:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "Argus", 0x10)  # MB_ICONERROR
        except Exception:                         # noqa: BLE001 — showing at least something matters more
            pass
    try:
        traceback.print_exception(type(error), error, error.__traceback__)
    except Exception:                             # noqa: BLE001 — before `prepare_output` and
        pass                                       # `sys.stderr` may not be set yet


def seed_platform() -> None:
    """Do not ask the system for what is already known — on every launch.

    On Windows, `platform.uname()` in Python 3.12 makes two WMI queries (~80 ms, and it also loads
    an antivirus provider), and it is called when loading both `onnxruntime` and `pywebview`. We
    know the answer without queries: the Windows build version is in `sys.getwindowsversion()`,
    the rest is in environment variables. We put it into the cache of `platform` itself; if that
    fails (another Python version, another layout), we stay silent and everything works as before.
    """
    try:
        import platform

        build = sys.getwindowsversion().build  # type: ignore[attr-defined]
        machine = (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or "AMD64"
        )
        platform._uname_cache = platform.uname_result(  # type: ignore[attr-defined]
            "Windows", os.environ.get("COMPUTERNAME", ""),
            "11" if build >= 22000 else "10", f"10.0.{build}", machine,
        )
    except Exception:                              # noqa: BLE001 — we will ask the system, as before
        pass


def warm_gui() -> None:
    """Load WebView and .NET in advance, in a thread of its own, while the settings are being read.

    Before the first window this is the longest startup chain: `import webview`, the .NET runtime
    (`clr`), WinForms — about 650 ms, and they used to run only after the settings and the
    instance lock. The settings (pydantic) take the CPU, while .NET spends most of its time
    loading libraries and bringing up the runtime — one does not hinder the other, and together
    they take less time than one after the other.

    It creates nothing: the window is born in `run_desktop`, on the main thread, and its
    `import webview` simply finds the module already loaded (or waits for it — import locks are
    per module). If it fails, we stay silent: the same `import` in `run_desktop` will report it
    for real, with a message box.
    """
    import threading

    def run() -> None:
        try:
            import webview  # noqa: F401

            if sys.platform == "darwin":
                # The pywebview platform module (`cocoa`) is not loaded here: on import it creates
                # `NSApplication`, which is allowed only on the main thread. We load what it needs — the PyObjC
                # wrappers, which are heavy and safe.
                import AppKit  # noqa: F401
                import Foundation  # noqa: F401
                import Quartz  # noqa: F401
                import WebKit  # noqa: F401
            else:
                from webview.platforms import winforms  # noqa: F401
        except Exception:                          # noqa: BLE001 — the real import will report it itself
            pass

    threading.Thread(target=run, name="argus-gui-warm", daemon=True).start()


async def _nothing() -> None:
    """A pipeline that does not exist: the update took over the launch, and there is nothing more to
    bring up here. The window closes by itself when this coroutine ends."""


def _landing(cfg) -> str:  # type: ignore[no-untyped-def]
    """Where to open the page: see `argus.landing`. Called when the server has already responded, not
    before the window appears."""
    from argus.landing import landing_path

    return landing_path(cfg)


def main() -> int:
    # Must come before everything else: otherwise in the built form every child process
    # would start the app again.
    multiprocessing.freeze_support()
    try:
        # The whole body in one block, not just the `run_desktop` call: `prepare_output` can fail too
        # (a folder with no write permission — installed in Program Files), and so can the config
        # loading, not only the launch itself. Not catching this here would mean going back to the same
        # thing — the silence on screen that the block was created for.
        prepare_output()
        hide_child_consoles()
        seed_platform()
        seed_data()         # macOS: seed data from the package -> data folder; empty on Windows
        warm_gui()

        # Before the window — only what it cannot be shown without: the settings and the
        # single-instance lock. The pipeline (audio, network, models), the update check and all their
        # heavy imports move to a background thread and run with the window already on screen. Before,
        # `argus.cli` (`typer`, `rich`) and `argus.runner` (the whole pipeline) stood here — the window
        # waited seconds for them, and a person who could not see it clicked the shortcut again and
        # again.
        from argus.core.config import Config
        from argus.desktop import claim_instance_or_focus, run_desktop
        from argus.startup import mark

        here = root()
        config = here / "argus.toml"
        if not config.exists() and getattr(sys, "frozen", False):
            # `Config.load(None)` looks for `argus.toml` from the current folder upward: for a shortcut
            # that is the desktop or System32 — somebody else's file would be picked up, and the data
            # (sessions, keys, login) would scatter into the wrong folders. Better to say so directly.
            raise FileNotFoundError(
                f"Settings file argus.toml not found next to the program ({here}). Unpack the archive again."
            )
        cfg = Config.load(config if config.exists() else None)
        mark("settings read")

        # Before the update check, not after: otherwise a repeated launch while the first copy is
        # already running downloaded and unpacked the archive over the running program every time — see
        # the docstring of `claim_instance_or_focus`.
        if not claim_instance_or_focus(
            cfg, on_stuck=lambda text: report_startup_failure(RuntimeError(text))
        ):
            return 0
        mark("instance claimed")

        # Heavy libraries are read from disk in advance, but only when the window is already on screen
        # (`on_shown`): started earlier, the prefetch shared the interpreter with the window and pushed
        # it back by ~0.1 s (measured). See `argus/prefetch.py`.
        from argus import prefetch

        def start_pipeline(stop):  # type: ignore[no-untyped-def]
            # Called in the pipeline thread, not the main one: the update check goes to the network
            # (`update.TIMEOUT_S`), and an empty window must not be held because of it — while it runs, the
            # splash screen is already on screen.
            from argus.update import maybe_self_update

            if maybe_self_update(cfg.update.server, cfg.root):
                # The update has already started as a separate process that waits until exactly this one
                # closes — there is no longer any need, and it is harmful, to bring up the pipeline here:
                # keeping the files busy would prevent it from overwriting them. When the pipeline ends, the
                # window closes by itself.
                return _nothing()

            from argus.runner import run_app

            mark("pipeline: modules loaded")
            return run_app(cfg, cfg.ui.port, open_browser=False, stop_signal=stop)

        return run_desktop(
            cfg,
            start_pipeline,
            cfg.ui.port,
            path=lambda: _landing(cfg),
            claimed=True,
            on_shown=prefetch.start,
        )
    except BaseException as e:                    # noqa: BLE001 — otherwise silence on screen
        report_startup_failure(e)
        return 1


def leave(code: int) -> None:
    """Exit at once, without waiting for the interpreter and .NET to dismantle the remains.

    Everything needed is already closed by this point: the pipeline is stopped (the recording is
    flushed, the audio and recognition sessions are closed), the window and the hotkeys are
    removed. And a normal Python exit still waits for the executor threads and unloads the native
    libraries (PortAudio, .NET with WebView2) — and if even one of them is stuck, the process
    stays alive and holds the single-instance lock: the next launch waits for it, and the person
    sees "Argus won't open". We flush the log ourselves so that the last lines are not lost.

    **Not `os._exit` but `TerminateProcess`, and this was measured.** While the .NET runtime
    (pythonnet, WebView2) is loaded in the process, `ExitProcess` — and after it `os._exit` —
    waits for it to stop for exactly two seconds (measured 2026-10-07: 2.03 s at any number of
    launches, against 3 ms without it). These are the very "two seconds after closing the
    window" during which the process still holds the single-instance lock. `TerminateProcess` on
    itself finishes in 10 ms; everything that needed to be written we wrote above, and the system
    closes the handles and locks itself.
    """
    import logging

    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:                          # noqa: BLE001 — we exit in any case
            pass
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code & 0xFFFFFFFF)
    except Exception:                              # noqa: BLE001 — the fallback path is below
        pass
    os._exit(code)         # TerminateProcess did not return — at least this


if __name__ == "__main__":
    leave(main())
