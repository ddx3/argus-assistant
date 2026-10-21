"""Running a fixed, read-only command on the server over ssh.

Everything the model or a log can say stays out of these commands: they are built here from constants and from
settings that were validated when the file was loaded. The remote side only ever reads (docker inspect, df, ls,
stat, openssl, a read-only SQLite connection).
"""

from __future__ import annotations

import base64

from argus.guardian.config import SAFE_NAME, Config
from argus.guardian.deps import Deps, Result

SSH_TIMEOUT_S = 60


def safe(value: str, what: str) -> str:
    """A setting that ends up inside a remote command must be a plain name or path."""
    if not SAFE_NAME.match(value):
        raise ValueError(f"{what} {value!r} may contain only letters, digits, and _ . - /")
    return value


def run_remote(cfg: Config, deps: Deps, command: str) -> Result | None:
    """Run `command` on the server through the configured ssh prefix. None when ssh is not configured."""
    if not cfg.ssh_command:
        return None
    return deps.run([*cfg.ssh_command, command], timeout=SSH_TIMEOUT_S)


def run_remote_python(cfg: Config, deps: Deps, script: str, container: str, argument: str = "") -> Result | None:
    """Run a constant Python script inside a container, fed through stdin (base64 keeps the quoting trivial)."""
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    arg = safe(argument, "argument") if argument else ""
    return run_remote(cfg, deps, f"echo {payload} | base64 -d | docker exec -i {safe(container, 'container')} python3 - {arg}".rstrip())
