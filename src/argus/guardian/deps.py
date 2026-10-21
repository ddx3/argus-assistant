"""Everything the guardian touches outside its own process, behind one small interface.

Tests replace these pieces with fakes; real runs use the defaults below. Keeping the outside world
in one place is also what makes the safety story short: processes are started from an argument
list (never through a shell), and the only network calls are the ones in this file and `notify.py`.
"""

from __future__ import annotations

import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Result:
    returncode: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# What a test process needs from the environment to start: where the system and the user's folders are. These are
# paths, not secrets. Nothing else is passed on: no tokens, no keys.
SAFE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "ALLUSERSPROFILE")


def run_command(argv: list[str], *, cwd: Path | str | None = None, timeout: float = 120,
                env: dict[str, str] | None = None, clean_env: bool = False) -> Result:
    """Run a program from an argument list. Never a shell, never a prompt.

    `clean_env` starts from a short list of harmless variables instead of the whole environment. It is
    used for everything that runs code the model had a hand in (the tests), so that a test cannot read
    the guardian's GitHub or Telegram token, or an API key, from its own environment.
    """
    merged = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV} if clean_env else dict(os.environ)
    merged["GIT_TERMINAL_PROMPT"] = "0"
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    merged.update(env or {})
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=merged, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout:.0f}s")
    except OSError as e:
        return Result(127, "", str(e))
    return Result(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))


def http_get(url: str, timeout: float = 15) -> tuple[int, str]:
    """(status, first 200 KB of the body). Status 0 means no answer at all."""
    req = urllib.request.Request(url, headers={"User-Agent": "argus-guardian/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001 - no network, bad certificate, timeout: all mean "does not answer"
        return 0, ""


@dataclass
class Deps:
    run: Callable[..., Result] = run_command
    fetch: Callable[[str], tuple[int, str]] = http_get
    now: Callable[[], float] = time.time
    # `brain`, `notifier` and `github` are filled in by `runner.build_deps`; tests pass their own.
    brain: object | None = None
    notifier: object | None = None
    github: object | None = None
    extra: dict = field(default_factory=dict)
