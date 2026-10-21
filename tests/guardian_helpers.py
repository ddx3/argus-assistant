"""Shared pieces for the guardian tests: a real git repository with a bug in it, and fakes for the rest.

Nothing here talks to the network or to a model. The git repository is real (a bare "origin" and a
clone in a temporary folder) so that worktrees, the diff gate, and the push are exercised for real.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from argus.guardian.audit import Audit
from argus.guardian.brain import AgentRun
from argus.guardian.config import Config, Mode
from argus.guardian.deps import Deps
from argus.guardian.state import State

SANDBOX = [sys.executable, "-I", str(Path(__file__).with_name("guardian_sandbox.py")), "{worktree}", "--"]

CALC_BUGGY = '''def add(a, b):
    return a - b
'''

CALC_FIXED = '''def add(a, b):
    return a + b
'''

TEST_CALC = '''from app.calc import add


def test_add_positive_numbers():
    assert add(2, 3) == 5
'''

BILLING = '''def charge(balance, cost):
    return balance - cost
'''


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.repo = root / "clone"
        self.home = root / "guardian-home"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "checkout", "-q", "-b", "main")
        self.write("src/app/__init__.py", "")
        self.write("src/app/calc.py", CALC_BUGGY)
        self.write("src/argus/server/billing.py", BILLING)
        self.write("tests/test_calc.py", TEST_CALC)
        self.write(".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def remote_branches(self) -> list[str]:
        out = git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [b for b in out.split() if b != "main"]

    def config(self, mode: Mode = Mode.ASK, **overrides) -> Config:
        cfg = Config(
            mode=mode, home=self.home, repo=self.repo, base_branch="main", remote="origin", github_repo="owner/name",
            test_command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            test_env={"PYTHONPATH": "src"}, test_timeout_s=120, tests_every_h=24, agent_timeout_s=60,
            allow_unsandboxed=True)
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.sent.append((title, body))

    @property
    def text(self) -> str:
        return "\n".join(f"{t}\n{b}" for t, b in self.sent)


class FakeGitHub:
    def __init__(self, can_open: bool = True) -> None:
        self.can_open_pull_requests = can_open
        self.pull_requests: list[dict] = []

    def compare_url(self, base: str, head: str) -> str:
        return f"https://github.example/compare/{base}...{head}"

    def open_draft_pull_request(self, *, base: str, head: str, title: str, body: str) -> str:
        self.pull_requests.append({"base": base, "head": head, "title": title, "body": body})
        return f"https://github.example/pull/{len(self.pull_requests)}"


class RoleBrain:
    """Plays one script for diagnosis runs and another for fix runs, against the real tools."""

    def __init__(self, diagnose: Callable[[Callable[[str, dict], str]], None] | None = None,
                 fix: Callable[[Callable[[str, dict], str]], None] | None = None, cost: float = 0.0) -> None:
        self.diagnose_script, self.fix_script, self.cost = diagnose, fix, cost
        self.diagnoses = 0
        self.fixes = 0
        self.last_prompt = ""

    async def run(self, *, system, prompt, tools, call, max_turns, timeout_s, budget_usd) -> AgentRun:
        self.last_prompt = prompt
        names = {t.name for t in tools}
        if "submit_diagnosis" in names:
            self.diagnoses += 1
            if self.diagnose_script:
                self.diagnose_script(call)
        else:
            self.fixes += 1
            if self.fix_script:
                self.fix_script(call)
        return AgentRun(True, "scripted", 0, self.cost)


def good_diagnosis(call: Callable[[str, dict], str], files=("src/app/calc.py",), fixable: bool = True) -> None:
    call("read_evidence", {})
    call("read_file", {"path": "src/app/calc.py"})
    call("submit_diagnosis", {
        "summary": "add() subtracts instead of adding", "likely_cause": "operator typo in src/app/calc.py line 2",
        "severity": "high", "confidence": 0.9, "fixable": fixable, "files": list(files), "plan": "use + instead of -"})


def good_fix(call: Callable[[str, dict], str]) -> None:
    call("edit_file", {"path": "src/app/calc.py", "old": "a - b", "new": "a + b"})
    call("run_tests", {"selectors": ["tests/test_calc.py"]})
    call("finish", {"summary": "add() now adds"})


def make_deps(clock: Clock | None = None, brain=None, notifier=None, github=None) -> Deps:
    deps = Deps(now=clock or Clock())
    deps.brain, deps.notifier, deps.github = brain, notifier, github
    return deps


def open_state(cfg: Config) -> tuple[State, Audit]:
    return State(cfg.home), Audit(cfg.home, run_id="test")
