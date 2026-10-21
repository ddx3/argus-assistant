"""Run the test suite on a clean copy of the main branch and report what fails.

This is the check that watches the *project* rather than the server. It runs in a throwaway worktree
of `origin/<base>` (never in the developer's own checkout), only when the branch has a new commit or
a day has passed, and every failing test becomes one problem that goes away when the test passes.
"""

from __future__ import annotations

import re

from argus.guardian import gitops
from argus.guardian.checks import CheckResult, Observation
from argus.guardian.config import Config
from argus.guardian.deps import Deps
from argus.guardian.redact import redact
from argus.guardian.state import State

FAILED = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$", re.MULTILINE)
HEADER = re.compile(r"^_{3,} (.+?) _{3,}$", re.MULTILINE)
MAX_EVIDENCE = 3000


def parse_failures(output: str) -> list[tuple[str, str, str]]:
    """(kind, test id, one-line reason) for every FAILED/ERROR line of pytest's short summary."""
    seen: set[str] = set()
    found = []
    for m in FAILED.finditer(output):
        kind, test, reason = m.group(1), m.group(2), (m.group(3) or "").strip()
        if test in seen:
            continue
        seen.add(test)
        found.append((kind, test, reason))
    return found


def failure_section(output: str, test: str) -> str:
    """The traceback pytest printed for one test, found by the test's own name."""
    name = test.split("::")[-1].split("[")[0]
    headers = list(HEADER.finditer(output))
    for index, h in enumerate(headers):
        if name in h.group(1):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(output)
            return output[h.start(): end].strip()[:MAX_EVIDENCE]
    return ""


def run(cfg: Config, deps: Deps, state: State) -> CheckResult | None:
    if cfg.repo is None or not cfg.test_command:
        return None
    gitops.fetch(deps, cfg.repo, cfg.remote)
    base_ref = f"{cfg.remote}/{cfg.base_branch}"
    sha = gitops.rev(deps, cfg.repo, base_ref)
    now = deps.now()
    last_sha = state.kv_get("tests_sha")
    last_at = float(state.kv_get("tests_at", "0") or 0)
    if sha == last_sha and now - last_at < cfg.tests_every_h * 3600:
        return None

    work = cfg.home / "work" / f"tests-{sha[:8]}"
    gitops.add_worktree(deps, cfg.repo, work, sha)
    try:
        result = deps.run(cfg.test_argv(work, ["-rfE"]), cwd=work, timeout=cfg.test_timeout_s,
                          env=cfg.test_env_for(work), clean_env=True)
    finally:
        gitops.remove_worktree(deps, cfg.repo, work)
    state.kv_set("tests_sha", sha)
    state.kv_set("tests_at", str(now))

    output = (result.out + "\n" + result.err).replace("\r\n", "\n")      # pytest on Windows prints CRLF
    report = CheckResult()
    if result.returncode == 124:
        report.observations.append(Observation(
            key="tests:timeout", title="The test suite timed out", severity="medium",
            evidence=redact(f"tests did not finish within {cfg.test_timeout_s}s on {base_ref} ({sha[:8]})")))
        return report
    failures = parse_failures(output)
    for kind, test, reason in failures:
        evidence = redact(f"{kind} {test} on {base_ref} ({sha[:8]})\n{reason}\n\n{failure_section(output, test)}")
        report.observations.append(Observation(
            key=f"test:{test}", title=f"Test fails on {cfg.base_branch}: {test}", severity="high", evidence=evidence[:MAX_EVIDENCE]))
    if not failures and not result.ok:
        tail = "\n".join(output.strip().splitlines()[-40:])
        report.observations.append(Observation(
            key="tests:run-failed", title="The test run failed without a readable result", severity="medium",
            evidence=redact(f"exit code {result.returncode} on {base_ref} ({sha[:8]})\n{tail}")[:MAX_EVIDENCE]))
    return report
