"""The state of the server itself: are the containers up, is there disk, is the backup fresh, will the certificate last.

These are the same questions the server's own `healthcheck.py` asks from inside, asked from outside over ssh with
fixed read-only commands. Asking from outside adds the one thing the inside cannot see: that the server cannot be
reached. A problem is reported after two checks in a row, because networks blink and containers restart in a deploy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from argus.guardian.checks import CheckResult, Observation
from argus.guardian.checks.remote import run_remote, safe
from argus.guardian.config import Config
from argus.guardian.deps import Deps
from argus.guardian.state import State

NEEDED_FAILS = 2
DISK_MAX_PCT = 90
BACKUP_MAX_AGE_S = 36 * 3600
CERT_MIN_DAYS = 14


def build_command(cfg: Config) -> str:
    parts = []
    for name in cfg.host_containers:
        n = safe(name, "container")
        parts.append(
            f"echo \"C {n} $(docker inspect --format '{{{{.State.Running}}}} "
            f"{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' {n} 2>&1 | head -1)\"")
    parts.append("echo \"D $(df -P / | tail -1 | awk '{print $5}')\"")
    if cfg.host_backup_dir:
        d = safe(cfg.host_backup_dir, "backup folder")
        parts.append(f"echo \"B $(date +%s) $(stat -c %Y $(ls -1t {d}/*.sqlite.gz 2>/dev/null | head -1) 2>/dev/null)\"")
    if cfg.host_cert:
        c = safe(cfg.host_cert, "certificate")
        parts.append(f"echo \"T $(openssl x509 -enddate -noout -in {c} 2>&1 | head -1)\"")
    return "; ".join(parts)


def parse(output: str, cfg: Config, now: float) -> list[Observation]:
    found: list[Observation] = []
    answered = set()
    for line in output.splitlines():
        tag, _, rest = line.strip().partition(" ")
        if tag == "C":
            name, _, state = rest.partition(" ")
            answered.add(name)
            running, _, health = state.partition(" ")
            if running != "true":
                found.append(Observation(f"host:container:{name}", f"Container {name} is not running", "critical",
                                         f"docker inspect {name}: {state[:120] or 'no answer'}"))
            elif health not in ("healthy", "none"):
                found.append(Observation(f"host:container:{name}", f"Container {name} is {health}", "high",
                                         f"docker inspect {name}: running, health is {health}"))
        elif tag == "D" and rest.endswith("%") and rest[:-1].isdigit():
            used = int(rest[:-1])
            if used >= DISK_MAX_PCT:
                found.append(Observation("host:disk", f"The server disk is {used}% full", "high", f"df /: {used}% used"))
        elif tag == "B":
            fields = rest.split()
            if len(fields) < 2 or not all(f.isdigit() for f in fields[:2]):
                found.append(Observation("host:backup", "There is no database backup", "high",
                                         f"no *.sqlite.gz file in {cfg.host_backup_dir}"))
            else:
                hours = (int(fields[0]) - int(fields[1])) // 3600
                if hours * 3600 > BACKUP_MAX_AGE_S:
                    found.append(Observation("host:backup", f"The newest database backup is {hours} hours old", "high",
                                             f"newest file in {cfg.host_backup_dir} is {hours} hours old (limit 36)"))
        elif tag == "T":
            _, _, stamp = rest.partition("=")
            try:
                end = datetime.strptime(stamp.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC).timestamp()
            except ValueError:
                found.append(Observation("host:cert", "The certificate could not be read", "medium", f"openssl said: {rest[:120]}"))
                continue
            days = (end - now) / 86400
            if days < CERT_MIN_DAYS:
                found.append(Observation("host:cert", f"The certificate expires in {int(days)} days", "high",
                                         f"{cfg.host_cert} is valid for {int(days)} more days (limit {CERT_MIN_DAYS})"))
    for name in cfg.host_containers:
        if name not in answered:
            found.append(Observation(f"host:container:{name}", f"Container {name} gave no answer", "high", "no line in the output"))
    return found


def run(cfg: Config, deps: Deps, state: State) -> CheckResult | None:
    if not cfg.ssh_command:
        return None
    result = run_remote(cfg, deps, build_command(cfg))
    reached = result is not None and bool((result.out or "").strip())
    fails = 0 if reached else int(state.kv_get("host_ssh_fails", "0")) + 1
    state.kv_set("host_ssh_fails", str(fails))
    if not reached:
        # Blind: say nothing about the rest, and do not resolve what was open.
        report = CheckResult(complete=False)
        if fails >= NEEDED_FAILS:
            why = ((result.err if result else "") or "no output").strip()[:200]
            report.observations.append(Observation(
                "host:ssh", "The server cannot be reached over ssh", "high",
                f"{fails} checks in a row got no answer, so the server and database checks are blind meanwhile: {why}"))
        else:
            report.still_open.add("host:ssh")
        return report
    seen = json.loads(state.kv_get("host_fails", "{}"))
    counts: dict[str, int] = {}
    report = CheckResult()
    for o in parse(result.out, cfg, deps.now()):
        counts[o.key] = seen.get(o.key, 0) + 1
        if counts[o.key] >= NEEDED_FAILS:
            report.observations.append(o)
        else:
            report.still_open.add(o.key)
    state.kv_set("host_fails", json.dumps(counts))
    return report
