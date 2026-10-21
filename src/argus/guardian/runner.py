"""One guardian cycle: look, diagnose, report, and (when allowed) prepare fixes.

The order matters. Checks run first and only record facts. Diagnosis is the only step that spends
model money, so it is capped per day and skipped for anything diagnosed recently. Fixes are the only
step that writes anywhere, so they run last, only in `ask` and `fix` modes, only for problems a
person approved (or, in `fix` mode, that the model marked fixable), and only a few per day.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from argus.guardian import diagnose as diagnose_mod
from argus.guardian import fixer
from argus.guardian.audit import Audit
from argus.guardian.brain import Brain, ClaudeBrain
from argus.guardian.checks import all_checks
from argus.guardian.config import Config, Mode
from argus.guardian.deps import Deps
from argus.guardian.github import GitHub
from argus.guardian.notify import build_notifier
from argus.guardian.redact import redact
from argus.guardian.state import ACTIVE, Finding, State

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
LOCK_STALE_S = 3 * 3600


@dataclass
class CycleReport:
    mode: str
    skipped: str = ""
    checked: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    diagnosed: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    prs: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class CycleLock:
    """A file lock so that two cycles (a scheduled one and a manual one) never overlap."""

    def __init__(self, home: Path) -> None:
        self.path = home / "cycle.lock"

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.held = False
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)     # atomic: only one process wins
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime < LOCK_STALE_S:
                        return False
                    self.path.unlink(missing_ok=True)                              # a crashed cycle left it behind
                except FileNotFoundError:
                    pass
                continue
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
            return True
        return False

    def __exit__(self, *exc) -> None:
        if getattr(self, "held", False):
            self.path.unlink(missing_ok=True)


def build_deps(cfg: Config) -> Deps:
    return Deps(brain=ClaudeBrain(cfg), notifier=build_notifier(cfg),
                github=GitHub(cfg.github_repo) if cfg.github_repo else None)


def _order(f: Finding) -> tuple[int, float]:
    return SEVERITY_ORDER.get(f.severity, 9), -f.last_seen


def _line(f: Finding, mode: Mode) -> str:
    d = f.diagnosis or {}
    out = [f"[{f.severity}] {f.id}: {f.title}"]
    if d:
        out.append(f"  why: {d.get('summary', '')}")
        out.append(f"  confidence {d.get('confidence', '?')}, fixable: {'yes' if d.get('fixable') else 'no'}")
        for note in d.get("notes", []):
            out.append(f"  note: {note}")
    elif f.note:
        out.append(f"  note: {f.note}")
    if f.status == "proposed":
        out.append(f"  approve the fix: python -m argus.guardian approve {f.id}   (or reject {f.id})")
    if f.pr_url:
        out.append(f"  pull request: {f.pr_url}")
    return "\n".join(out)


def _budget_left(cfg: Config, state: State, now: float) -> bool:
    return state.today("cost_usd", now) < cfg.budget_per_day_usd and state.today("tokens", now) < cfg.max_tokens_per_day


def approve(state: State, audit: Audit, ident: str, who: str = "human") -> Finding:
    f = state.get(ident)
    if f is None:
        raise KeyError(f"no such problem: {ident}")
    d = f.diagnosis or {}
    if f.status not in ("proposed", "diagnosed", "fix_failed") or not d.get("fixable"):
        raise ValueError(f"{f.id} is {f.status} and not fixable; there is nothing to approve")
    state.kv_set(f"approved_by:{f.id}", who)
    audit.log("approved", finding=f.id, by=who)
    return state.update(f.id, status="approved")


def reject(state: State, audit: Audit, ident: str) -> Finding:
    f = state.get(ident)
    if f is None:
        raise KeyError(f"no such problem: {ident}")
    audit.log("rejected", finding=f.id)
    return state.update(f.id, status="dismissed", note="dismissed by a person")


async def execute_approved(cfg: Config, deps: Deps, state: State, audit: Audit, report: CycleReport, dry_run: bool) -> None:
    brain: Brain = deps.brain  # type: ignore[assignment]
    for f in sorted(state.list(("approved",)), key=_order):
        now = deps.now()
        if state.today("fixes", now) >= cfg.max_fixes_per_day or not _budget_left(cfg, state, now):
            audit.log("fix_skipped", finding=f.id, reason="daily limit reached")
            report.failed.append(f"{f.id}: daily limit reached, will try again tomorrow")
            break
        by = state.kv_get(f"approved_by:{f.id}", "human")
        state.add("fixes", 1, now)
        result = await fixer.fix(cfg, deps, state, audit, f, brain=brain, github=deps.github,  # type: ignore[arg-type]
                                 auto=(by == "auto"), dry_run=dry_run)
        if result.ok:
            link = result.pr_url or result.compare_url or result.branch
            state.update(f.id, status="pr_open", pr_url=link, branch=result.branch, note=result.reason)
            report.prs.append(f"{f.id}: {link}")
        else:
            why = result.reason + ("; " + "; ".join(result.violations) if result.violations else "")
            state.update(f.id, status="fix_failed", note=why[:500], branch=result.branch)
            report.failed.append(f"{f.id}: {why}")
        audit.log("fix_result", finding=f.id, ok=result.ok, reason=result.reason, violations=result.violations,
                  flags=result.flags, cost=result.cost)


async def run_cycle(cfg: Config, deps: Deps | None = None, *, dry_run: bool = False) -> CycleReport:
    mode = cfg.effective_mode
    report = CycleReport(mode=mode.value)
    if mode is Mode.OFF:
        report.skipped = "the guardian is switched off"
        return report
    deps = deps or build_deps(cfg)
    state = State(cfg.home)
    audit = Audit(cfg.home, run_id=time.strftime("%Y%m%d-%H%M%S"))
    try:
        with CycleLock(cfg.home) as acquired:
            if not acquired:
                report.skipped = "another cycle is already running"
                return report
            audit.log("cycle_start", mode=mode.value, dry_run=dry_run)
            await _cycle(cfg, deps, state, audit, report, mode, dry_run)
            audit.log("cycle_end", new=report.new, resolved=report.resolved, diagnosed=report.diagnosed,
                      proposed=report.proposed, prs=report.prs, failed=report.failed)
    finally:
        state.close()
    return report


async def _cycle(cfg: Config, deps: Deps, state: State, audit: Audit, report: CycleReport, mode: Mode, dry_run: bool) -> None:
    now = deps.now()

    # 1. look
    for name, check in all_checks().items():
        try:
            result = check(cfg, deps, state)
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            audit.log("check_error", check=name, error=repr(e))
            continue
        if result is None:
            continue
        report.checked.append(name)
        seen = {o.key for o in result.observations} | result.still_open
        for o in result.observations:
            finding, is_new = state.upsert(key=o.key, check_name=name, title=o.title, severity=o.severity,
                                           evidence=o.evidence, now=now)
            if is_new:
                report.new.append(finding.id)
                audit.log("finding", finding=finding.id, key=o.key, severity=o.severity)
        if result.complete:
            for gone in state.resolve_missing(name, seen):
                report.resolved.append(gone.id)
                audit.log("resolved", finding=gone.id)

    # 2. diagnose (read-only model)
    blocked_until = float(state.kv_get("model_blocked_until", "0") or 0)
    if blocked_until > now:
        audit.log("model_blocked", until=blocked_until)
    elif deps.brain is not None:
        todo = [f for f in state.list(("open",))
                if f.last_diagnosed is None or now - f.last_diagnosed > cfg.diagnose_cooldown_h * 3600]
        for f in sorted(todo, key=_order):
            if state.today("diagnoses", now) >= cfg.max_diagnoses_per_day or not _budget_left(cfg, state, now):
                audit.log("diagnose_skipped", finding=f.id, reason="daily limit reached")
                break
            if await diagnose_mod.diagnose(cfg, deps, state, audit, f, deps.brain) is not None:  # type: ignore[arg-type]
                report.diagnosed.append(f.id)
            elif float(state.kv_get("model_blocked_until", "0") or 0) > now:
                break                       # the model is unavailable: leave the rest for later

    # 3. propose or approve fixes
    if mode in (Mode.ASK, Mode.FIX):
        for f in state.list(("diagnosed",)):
            if not (f.diagnosis or {}).get("fixable"):
                continue
            if not cfg.can_run_model_code:
                # A fix means running model-written code. Say so once, and leave the problem for a person.
                note = "fixable, but a fix needs a sandbox (or `allow_unsandboxed = true`), so none was prepared"
                if f.note != note:
                    state.update(f.id, note=note)
                continue
            if mode is Mode.FIX:
                approve(state, audit, f.id, who="auto")
            else:
                state.update(f.id, status="proposed")
                report.proposed.append(f.id)
        # 4. prepare approved fixes on branches
        if deps.brain is not None:
            await execute_approved(cfg, deps, state, audit, report, dry_run)

    # 5. tell a person
    told = _notify(cfg, deps, state, audit, report, mode, now)
    if not told:
        _heartbeat(cfg, deps, state, audit, report, now)


def _notify(cfg: Config, deps: Deps, state: State, audit: Audit, report: CycleReport, mode: Mode, now: float) -> bool:
    """Send what is new or worth repeating. True when a message went out."""
    if deps.notifier is None:
        return False
    cooldown = cfg.notify_cooldown_h * 3600
    show: list[Finding] = []
    for f in sorted(state.list(ACTIVE + ("pr_open",)), key=_order):
        fresh = f.id in report.new or f.id in report.diagnosed or f.id in report.proposed
        pr_update = any(p.startswith(f.id + ":") for p in report.prs + report.failed)
        remind = f.last_notified is not None and now - f.last_notified > cooldown and (
            f.severity in ("critical", "high") or f.status == "proposed")
        if fresh or pr_update or remind or (f.last_notified is None and f.status != "resolved"):
            show.append(f)
    parts = [_line(f, mode) for f in show]
    if report.resolved:
        parts.append("Resolved: " + ", ".join(report.resolved))
    if report.failed:
        parts.append("Fix attempts that did not work:\n" + "\n".join(f"  {x}" for x in report.failed))
    if not parts:
        return False
    title = f"Argus guardian: {len(show)} problem(s)" if show else "Argus guardian: update"
    deps.notifier.send(title, redact("\n\n".join(parts)))  # type: ignore[attr-defined]
    for f in show:
        state.update(f.id, last_notified=now)
    audit.log("notified", findings=[f.id for f in show])
    state.kv_set("heartbeat_day", time.strftime("%Y-%m-%d", time.localtime(now)))     # a message just proved it is alive
    return True


def _heartbeat(cfg: Config, deps: Deps, state: State, audit: Audit, report: CycleReport, now: float) -> None:
    """One short mail a day saying the guardian is alive and what it looked at, so that silence means "all is well"."""
    if cfg.heartbeat_hour < 0 or deps.notifier is None:
        return
    local = time.localtime(now)
    today = time.strftime("%Y-%m-%d", local)
    if local.tm_hour < cfg.heartbeat_hour or state.kv_get("heartbeat_day") == today:
        return
    open_now = sorted(state.list(ACTIVE + ("pr_open",)), key=_order)
    lines = ["No new problems." if not open_now else f"{len(open_now)} problem(s) still open:"]
    lines += [f"  [{f.severity}] {f.id}: {f.title}" for f in open_now]
    lines.append("Looked at: " + (", ".join(report.checked) or "nothing (no check is configured)"))
    lines.append(f"Spent today: {int(state.today('diagnoses', now))} diagnoses, {int(state.today('tokens', now)):,} tokens.")
    deps.notifier.send("Argus guardian: all quiet" if not open_now else "Argus guardian: daily summary",  # type: ignore[attr-defined]
                       redact("\n".join(lines)))
    state.kv_set("heartbeat_day", today)
    audit.log("heartbeat", open=[f.id for f in open_now])


def summary(cfg: Config, state: State) -> str:
    spent = (f"today: {int(state.today('diagnoses'))} diagnoses, {int(state.today('fixes'))} fix attempts, "
             f"${state.today('cost_usd'):.2f} and {int(state.today('tokens')):,} tokens spent")
    lines = [f"mode: {cfg.effective_mode.value}" + (" (kill switch is on)" if cfg.killed else ""), f"home: {cfg.home}", spent]
    active = [f for f in state.list(ACTIVE + ("pr_open",))]
    if not active:
        lines.append("no open problems")
    for f in sorted(active, key=_order):
        lines.append(f"  {f.id}  {f.severity:<8} {f.status:<10} {f.title}")
    return "\n".join(lines)
