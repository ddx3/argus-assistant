"""The "server is alive" check (`deploy/server/healthcheck.py`): when to write and when not.

The main thing is that the emails are to the point: a single network blip does not wake
the operator, a live problem does not flood them with emails, and when everything is
fixed exactly one "recovered" email arrives.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("healthcheck", ROOT / "deploy" / "server" / "healthcheck.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

T0 = 1_000_000.0


def step(state, problems, now):
    alerts, recovered, state = hc.decide(state, set(problems), now)
    return alerts, recovered, state


def test_a_single_failure_does_not_write():
    alerts, recovered, state = step({}, {"A"}, T0)
    assert alerts == [] and not recovered
    alerts, recovered, state = step(state, set(), T0 + 300)            # blinked and passed
    assert alerts == [] and not recovered, "no email was sent about the problem, so there is nothing to report as recovered"


def test_two_failures_in_a_row_write_once():
    _, _, state = step({}, {"A"}, T0)
    alerts, _, state = step(state, {"A"}, T0 + 300)
    assert alerts == ["A"]
    alerts, _, state = step(state, {"A"}, T0 + 600)
    assert alerts == [], "a second email about the same thing 5 minutes later is not needed"


def test_a_living_problem_is_reminded_every_six_hours():
    state = {}
    for i in range(2):
        alerts, _, state = step(state, {"A"}, T0 + 300 * i)
    assert alerts == ["A"]
    alerts, _, state = step(state, {"A"}, T0 + 300 + 5 * 3600)
    assert alerts == []
    alerts, _, state = step(state, {"A"}, T0 + 300 + 6 * 3600 + 1)
    assert alerts == ["A"]


def test_recovery_is_announced_once():
    state = {}
    for i in range(2):
        _, _, state = step(state, {"A"}, T0 + 300 * i)
    _, recovered, state = step(state, set(), T0 + 900)
    assert recovered
    _, recovered, state = step(state, set(), T0 + 1200)
    assert not recovered


def test_each_problem_is_judged_on_its_own():
    _, _, state = step({}, {"A"}, T0)
    alerts, _, state = step(state, {"A", "B"}, T0 + 300)
    assert alerts == ["A"], "B is seen for the first time, so it needs a second check"
    alerts, _, state = step(state, {"A", "B"}, T0 + 600)
    assert alerts == ["B"]


def test_a_problem_that_came_back_after_recovery_starts_over():
    state = {}
    for i in range(2):
        _, _, state = step(state, {"A"}, T0 + 300 * i)
    _, _, state = step(state, set(), T0 + 900)
    alerts, _, state = step(state, {"A"}, T0 + 1200)
    assert alerts == [], "the first check after the fix only counts again"
    alerts, _, state = step(state, {"A"}, T0 + 1500)
    assert alerts == ["A"]
