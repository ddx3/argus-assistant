"""The `argus-syscap` helper: audio of the whole system (or of several programs) through
ScreenCaptureKit.

macOS has no analog of WASAPI loopback: the system does not hand out "what plays on the
speakers". The only way without installing a driver is ScreenCaptureKit (macOS 13+), and
it lives in Swift/Objective-C. It can be called from Python through PyObjC, but
assembling a flat array from a `CMSampleBuffer` there is a scatter of pointers and
structures, where a mistake does not crash but corrupts the audio. So the helper is a
separate small Swift program (`macos/helper/`), and here we only launch it.

The contract. The helper writes **raw audio** to stdout: int16, little-endian, two
channels interleaved, 48 kHz. This is the same format as `capture/app_loopback.py` on
Windows, so the same `Resampler` works further on. On stderr it writes JSON lines with
events (`ready`, `error`, `stopped`). Closing its stdin means "the parent died, exit":
this way the helper does not wander around the system if the app crashes.

The screen-recording permission is granted to the app, not to the helper: it is started as
a child process, and the system attributes the request to the "responsible" process,
`Argus.app`. So in the settings the user sees one row, "Argus", and not a nameless file.

Why startup does not block: on first use the system shows a prompt and waits for the
user's answer. If `open()` waited for it, the app would not open until the user pressed
the button. The helper starts in the background; whether it is ready or has failed can
be seen from `HelperProcess.state`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

HELPER_NAME = "argus-syscap"

# The stream format. Kept in one place with `capture/app_loopback.py` (48000 and 2 there):
# further down the chain both go through the same resampler.
RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2

# The helper's exit codes.
EXIT_OK = 0
EXIT_PERMISSION = 2
EXIT_UNSUPPORTED = 3
EXIT_NO_DISPLAY = 4

MIN_MACOS = (13, 0)

STARTING = "starting"
READY = "ready"
FAILED = "failed"
STOPPED = "stopped"


class SystemAudioUnavailable(RuntimeError):
    """System audio cannot be taken: `str(e)` explains what to do."""


def macos_version() -> tuple[int, int]:
    """The real macOS version. `(0, 0)` means not macOS or could not be determined.

    We ask `sw_vers`, not `platform.mac_ver()`: an app built with an old SDK gets a
    "compatible" version 10.16 from the system instead of 11-15, and a "macOS 13 or newer"
    check would wrongly pass on every system.
    """
    if sys.platform != "darwin":
        return (0, 0)
    try:
        out = subprocess.run(
            ["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5,
            env={**os.environ, "SYSTEM_VERSION_COMPAT": "0"}, check=True,
        ).stdout.strip()
        major, _, rest = out.partition(".")
        minor = rest.partition(".")[0] or "0"
        return (int(major), int(minor))
    except (OSError, ValueError, subprocess.SubprocessError):
        return (0, 0)


def find_helper() -> Path | None:
    """Where `argus-syscap` lives. `None` means it was not found."""
    candidates: list[Path] = []
    asked = os.environ.get("ARGUS_SYSCAP")
    if asked:
        candidates.append(Path(asked))

    exe = Path(sys.executable).resolve()
    candidates.append(exe.parent / HELPER_NAME)                      # Argus.app/Contents/MacOS
    candidates.append(exe.parent.parent / "Helpers" / HELPER_NAME)   # Argus.app/Contents/Helpers

    # Working copy: built with `swift build -c release` in macos/helper.
    helper_root = Path(__file__).resolve().parents[3] / "macos" / "helper" / ".build"
    candidates += [
        helper_root / "release" / HELPER_NAME,
        helper_root / "arm64-apple-macosx" / "release" / HELPER_NAME,
        helper_root / "x86_64-apple-macosx" / "release" / HELPER_NAME,
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def check_available() -> None:
    """Raises `SystemAudioUnavailable` if system audio cannot be obtained at all."""
    if sys.platform != "darwin":
        raise SystemAudioUnavailable("system audio is available this way only on macOS")
    version = macos_version()
    if version != (0, 0) and version < MIN_MACOS:
        raise SystemAudioUnavailable(
            f"system audio needs macOS {MIN_MACOS[0]} or newer, this is {version[0]}.{version[1]}"
        )
    if find_helper() is None:
        # The user gets no file name; the developer gets a log line saying what to build.
        log.warning("not found: %s (from source: `swift build -c release --package-path macos/helper`)", HELPER_NAME)
        raise SystemAudioUnavailable("the system audio capture module was not found — reinstall the app")


def explain(code: str, message: str = "") -> str:
    """Words for the user, based on the helper's error code.

    We do not show the raw system message to the user: it is in English and about internals.
    It goes to the log (`_on_event` writes it). So `message` is not used here, and an
    unknown code gets a generic phrase.
    """
    from argus.mac import permissions

    if code == "permission":
        return permissions.advice("screen", permissions.DENIED)
    if code == "unsupported":
        return f"system audio needs macOS {MIN_MACOS[0]} or newer"
    if code == "no_display":
        return "there is no display to capture system audio from"
    if code == "no_applications":
        return "couldn't find the pinned programs — are they closed?"
    return "the system audio capture module stopped"


class HelperProcess:
    """A running `argus-syscap` and its audio.

    Two threads: one reads events from stderr, the other hands the audio out through
    `read()`, which the channel thread calls. All state fields change under a lock: events
    arrive from another thread, and the state broadcast reads them.
    """

    def __init__(
        self,
        pids: list[int] | None = None,
        exclude_pids: list[int] | None = None,
        command: list[str] | None = None,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.pids = list(pids or [])
        self.exclude_pids = list(exclude_pids or [])
        self._command = command
        self._spawn = spawn
        self._proc: subprocess.Popen | None = None
        self._events: threading.Thread | None = None
        self._lock = threading.Lock()
        self.state = STARTING
        self.error_code = ""
        self.error_text = ""

    # -- launch -------------------------------------------------------------

    def command(self) -> list[str]:
        if self._command is not None:
            return list(self._command)
        helper = find_helper()
        if helper is None:
            raise SystemAudioUnavailable("the system audio capture module was not found")
        cmd = [str(helper), "--rate", str(RATE), "--channels", str(CHANNELS)]
        for pid in self.pids:
            cmd += ["--pid", str(pid)]
        for pid in self.exclude_pids:
            cmd += ["--exclude-pid", str(pid)]
        return cmd

    def start(self) -> None:
        cmd = self.command()
        try:
            self._proc = self._spawn(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as e:
            self._fail("start", f"the system audio capture module failed to start: {e}")
            raise SystemAudioUnavailable(self.error_text) from e
        self._events = threading.Thread(target=self._read_events, name="syscap-events", daemon=True)
        self._events.start()

    def _read_events(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in iter(proc.stderr.readline, b""):
                self._on_event(raw.decode("utf-8", "replace").strip())
        except (OSError, ValueError):
            pass
        finally:
            code = proc.wait()
            with self._lock:
                if self.state not in (FAILED, STOPPED):
                    if code == EXIT_OK:
                        self.state = STOPPED
                    else:
                        self.state = FAILED
                        self.error_code = self.error_code or _code_of_exit(code)
                        self.error_text = self.error_text or explain(self.error_code)

    def _on_event(self, line: str) -> None:
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            log.debug("syscap: %s", line)
            return
        kind = event.get("event")
        with self._lock:
            if kind == "ready":
                self.state = READY
            elif kind == "error":
                self.state = FAILED
                self.error_code = str(event.get("code") or "")
                self.error_text = explain(self.error_code, str(event.get("message") or ""))
                log.warning("syscap: %s (%s)", self.error_text, event.get("message", ""))
            elif kind == "stopped":
                self.state = STOPPED
            elif kind == "warning":
                log.warning("syscap: %s", event.get("message", ""))   # not the end: the audio continues
            else:
                log.debug("syscap: %s", event)

    def _fail(self, code: str, text: str) -> None:
        with self._lock:
            self.state = FAILED
            self.error_code = code
            self.error_text = text

    # -- audio --------------------------------------------------------------

    def read(self, size: int = 65536) -> bytes:
        """Some bytes of audio; empty means the helper has finished (the stream is closed).

        Blocks. It is called from the channel thread, so stopping is done not with a flag but by
        closing the process: `stop()` kills the helper, and `read` returns empty.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return proc.stdout.read(size)
        except (OSError, ValueError):
            return b""

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with self._lock:
            self.state = STOPPED
        # Politely first: a closed stdin is a request to exit. The helper ends the
        # ScreenCaptureKit stream by itself; killing it right away would leave the system with a
        # "hanging" screen recording (the icon stays in the menu bar).
        for stream in (proc.stdin,):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def _code_of_exit(code: int) -> str:
    return {
        EXIT_PERMISSION: "permission",
        EXIT_UNSUPPORTED: "unsupported",
        EXIT_NO_DISPLAY: "no_display",
    }.get(code, "failed")


def probe(timeout: float = 15.0) -> tuple[bool, str]:
    """A check for `argus doctor`: will the capture start and is there permission. Returns
    `(ok, words)`.
    """
    try:
        check_available()
    except SystemAudioUnavailable as e:
        return False, str(e)
    helper = find_helper()
    assert helper is not None
    try:
        run = subprocess.run(
            [str(helper), "--probe"], capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"the capture module is not responding: {e}"
    if run.returncode == EXIT_OK:
        return True, "system audio responds, permission granted"
    return False, explain(_code_of_exit(run.returncode))
