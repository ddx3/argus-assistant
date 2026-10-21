"""Checks: each one looks at something and reports what is wrong, in a uniform shape.

A check returns `Observation`s (problems seen right now) inside a `CheckResult`. When `complete` is
true, a problem of that check that is *not* in the result is considered gone and its finding is
resolved. A check that could not look (no log source configured, tests not due yet) returns `None`
and changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from argus.guardian.config import Config
from argus.guardian.deps import Deps
from argus.guardian.state import State


@dataclass
class Observation:
    key: str            # stable: the same problem always has the same key
    title: str
    severity: str       # low | medium | high | critical
    evidence: str       # already redacted


@dataclass
class CheckResult:
    observations: list[Observation] = field(default_factory=list)
    complete: bool = True
    # Keys that are not failing loudly enough to report yet, but must not be resolved either.
    still_open: set[str] = field(default_factory=set)


Check = Callable[[Config, Deps, State], CheckResult | None]


def all_checks() -> dict[str, Check]:
    from argus.guardian.checks import data, host, logs, probes, repo

    return {"probes": probes.run, "host": host.run, "data": data.run, "logs": logs.run, "repo_tests": repo.run}
