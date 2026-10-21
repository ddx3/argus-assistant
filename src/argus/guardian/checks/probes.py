"""HTTP probes: does the server answer, with the right status and the right content?

One failed request proves little (networks blink), so a problem is reported only after two checks in
a row fail. It disappears from the report as soon as one succeeds.
"""

from __future__ import annotations

from argus.guardian.checks import CheckResult, Observation
from argus.guardian.config import Config
from argus.guardian.deps import Deps
from argus.guardian.state import State

NEEDED_FAILS = 2


def run(cfg: Config, deps: Deps, state: State) -> CheckResult | None:
    if not cfg.probes:
        return None
    result = CheckResult()
    for probe in cfg.probes:
        status, body = deps.fetch(probe.url)
        ok = status == probe.expect_status and (not probe.expect_text or probe.expect_text in body)
        counter = f"probe_fails:{probe.name}"
        fails = 0 if ok else int(state.kv_get(counter, "0")) + 1
        state.kv_set(counter, str(fails))
        key = f"probe:{probe.name}"
        if fails >= NEEDED_FAILS:
            reason = "no answer" if status == 0 else f"status {status}, expected {probe.expect_status}"
            if status == probe.expect_status and probe.expect_text:
                reason = f"the answer does not contain {probe.expect_text!r}"
            result.observations.append(Observation(
                key=key, title=f"{probe.name} is not answering correctly", severity="high",
                evidence=f"GET {probe.url}: {reason} ({fails} checks in a row)"))
        elif fails:
            result.still_open.add(key)
    return result
