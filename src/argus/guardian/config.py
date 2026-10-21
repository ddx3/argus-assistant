"""Guardian settings: one TOML file plus a few environment overrides.

The file is `guardian.toml` in the guardian's home folder (`~/.argus/guardian` by default). Nothing
secret lives in it: tokens come from environment variables, so the file can be shown in a bug
report.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Mode(StrEnum):
    OFF = "off"
    WATCH = "watch"
    ASK = "ask"
    FIX = "fix"


DEFAULT_HOME = Path.home() / ".argus" / "guardian"

# A setting that ends up inside a command run on the server must be a plain name or path: no spaces, quotes, `$`, `;`.
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-/]+$")


@dataclass
class Probe:
    """One HTTP address that must answer."""

    name: str
    url: str
    expect_status: int = 200
    expect_text: str = ""


@dataclass
class Config:
    mode: Mode = Mode.ASK
    home: Path = DEFAULT_HOME

    # --- what is watched ---------------------------------------------------------------
    probes: list[Probe] = field(default_factory=list)
    # How to read the server log. Either a file on this machine, or a command (for example
    # ["ssh", "host", "docker", "logs", "--since", "{since}", "argus-server"]). `{since}` becomes
    # the time of the previous scan, so a problem is counted once, not on every cycle.
    log_file: str = ""
    log_command: list[str] = field(default_factory=list)
    log_window_min: int = 15
    log_min_count: int = 3          # plain ERROR lines need this many repeats; tracebacks never do

    # --- the server itself (read-only, over ssh) ---------------------------------------
    # A command prefix that reaches the server and runs the single command given as its last argument, for example
    # ["ssh", "-i", "/home/me/.ssh/id_ed25519", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "root@server"].
    # Only fixed, read-only commands built in `checks/host.py` and `checks/data.py` are ever sent through it: nothing
    # a log or a model says reaches the server.
    ssh_command: list[str] = field(default_factory=list)
    host_containers: list[str] = field(default_factory=list)   # must be running (and healthy, if they report health)
    host_backup_dir: str = ""       # a folder of *.sqlite.gz files; the newest must be younger than 36 hours
    host_cert: str = ""             # a certificate file; it must be valid for 14 more days
    data_container: str = ""        # the container that holds the accounts database; empty = no data checks
    data_db: str = "/app/data/argus-accounts.sqlite"

    # --- the repository ----------------------------------------------------------------
    repo: Path | None = None        # a local clone; the guardian only reads it and adds worktrees
    base_branch: str = "master"
    remote: str = "origin"
    github_repo: str = ""           # "owner/name", used to open pull requests
    test_command: list[str] = field(default_factory=lambda: [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    test_env: dict[str, str] = field(default_factory=lambda: {"PYTHONPATH": "src"})
    test_timeout_s: int = 1800
    tests_every_h: int = 24
    # A command prefix that runs the tests somewhere the model-written code cannot hurt (a container with no
    # network, a throwaway VM). `{worktree}` is replaced by the folder to test. Without it the tests run directly
    # on this machine, which is acceptable only when a person approved the change: `fix` mode requires a sandbox.
    sandbox: list[str] = field(default_factory=list)
    sandbox_test_command: list[str] = field(default_factory=list)   # the test command as seen inside the sandbox
    # Running a fix means running tests, and running tests means running the code the model wrote. Without a
    # sandbox that happens only when this is set, by a person who accepts that it runs with their permissions.
    allow_unsandboxed: bool = False

    # --- the model ---------------------------------------------------------------------
    auth: str = "subscription"      # "subscription" (development) or "api" (needs ANTHROPIC_API_KEY)
    model: str = ""
    max_turns_diagnose: int = 20
    max_turns_fix: int = 30
    agent_timeout_s: int = 900

    # --- who is told -------------------------------------------------------------------
    # E-mail goes out through the Resend service (the same one the server uses). The key is never in this file: it
    # comes from GUARDIAN_RESEND_KEY or from the one-line file named here. A reply address that forwards to a person
    # (such as the project's support address) keeps their own address out of the settings.
    email_from: str = ""            # for example "Argus Guardian <noreply@example.com>"
    email_to: str = ""
    resend_key_file: str = "~/.argus/resend_api_key"

    # --- limits ------------------------------------------------------------------------
    budget_per_run_usd: float = 1.0
    budget_per_day_usd: float = 4.0     # counts real cost (API mode); a subscription reports none
    max_tokens_per_day: int = 3_000_000  # counts tokens: the cap that also works on a subscription
    max_diagnoses_per_day: int = 12
    max_fixes_per_day: int = 3
    diagnose_cooldown_h: int = 12
    notify_cooldown_h: int = 6
    # A short "all is well" mail once a day, so that silence can be told from a dead guardian. -1 = never; otherwise
    # the local hour from which the day's mail may be sent.
    heartbeat_hour: int = -1
    max_files: int = 6
    max_changed_lines: int = 250

    # --- helpers -----------------------------------------------------------------------

    @property
    def off_file(self) -> Path:
        return self.home / "OFF"

    @property
    def killed(self) -> bool:
        """The kill switch: a file in the home folder, or an environment variable."""
        return self.off_file.exists() or os.environ.get("GUARDIAN_OFF") == "1"

    @property
    def effective_mode(self) -> Mode:
        if self.killed:
            return Mode.OFF
        if self.mode is Mode.FIX and not self.sandbox:
            return Mode.ASK         # nothing may run model-written code unattended outside a sandbox
        return self.mode

    @property
    def can_run_model_code(self) -> bool:
        return bool(self.sandbox) or self.allow_unsandboxed

    def test_env_for(self, worktree: Path) -> dict[str, str]:
        """The environment of the test run. With a sandbox the launcher gets nothing pointing into the worktree: a
        launcher that honoured a PYTHONPATH inside the untrusted tree would run the model's code before the
        sandbox even starts. The sandbox command sets whatever the tests need on the inside."""
        if self.sandbox:
            return {}
        return {k: (str(worktree / v) if k == "PYTHONPATH" else v) for k, v in self.test_env.items()}

    def test_argv(self, worktree: Path, extra: list[str] | None = None) -> list[str]:
        """The command that runs tests in `worktree`, wrapped in the sandbox when one is configured."""
        extra = extra or []
        if self.sandbox:
            prefix = [part.replace("{worktree}", str(worktree)) for part in self.sandbox]
            return [*prefix, *(self.sandbox_test_command or self.test_command), *extra]
        return [*self.test_command, *extra]

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        home = Path(os.environ.get("GUARDIAN_HOME", "") or DEFAULT_HOME)
        path = path or home / "guardian.toml"
        raw: dict = {}
        if path.is_file():
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        cfg = cls(home=Path(raw.pop("home", home)))
        probes = raw.pop("probes", [])
        for key, value in raw.items():
            if not hasattr(cfg, key):
                raise ValueError(f"guardian.toml: unknown setting {key!r}")
            current = getattr(cfg, key)
            if key == "mode":
                value = Mode(value)
            elif key == "repo" or isinstance(current, Path):
                value = Path(value)
            setattr(cfg, key, value)
        cfg.probes = [Probe(**p) for p in probes]
        env_mode = os.environ.get("GUARDIAN_MODE")
        if env_mode:
            cfg.mode = Mode(env_mode)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Refuse settings that could change what the fixed server commands do."""
        names = {"host_containers": self.host_containers, "host_backup_dir": [self.host_backup_dir],
                 "host_cert": [self.host_cert], "data_container": [self.data_container], "data_db": [self.data_db]}
        for setting, values in names.items():
            for value in values:
                if value and not (isinstance(value, str) and SAFE_NAME.match(value)):
                    raise ValueError(f"guardian.toml: {setting} {value!r} may contain only letters, digits, and _ . - /")
        if not all(isinstance(part, str) for part in self.ssh_command):
            raise ValueError("guardian.toml: ssh_command must be a list of strings")
