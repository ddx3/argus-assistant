"""Read the server log and turn it into a short list of distinct problems.

A log is mostly noise and a few repeated shapes. The job here is to collapse it: every Python
traceback becomes one problem named by its exception type and the innermost frame (so the same bug
is one finding no matter how many people hit it), and plain `ERROR` lines are grouped by their text
with the numbers and ids blanked out.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from argus.guardian.checks import CheckResult, Observation
from argus.guardian.config import Config
from argus.guardian.deps import Deps
from argus.guardian.redact import redact
from argus.guardian.state import State

TRACE_START = "Traceback (most recent call last):"
FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s*(.*))?$")
ERROR_LINE = re.compile(r"\b(ERROR|CRITICAL)\b[:\s-]+(.*)$")
# The server's access log: `INFO:     203.0.113.7:51234 - "POST /meter/stt HTTP/1.1" 500 Internal Server Error`.
# Only the method, the path (without the query) and the status are kept: the address of the caller never leaves the log.
ACCESS_5XX = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD) (\S+) HTTP/[\d.]+" (5\d\d)\b')
NOISE = re.compile(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{8,}|\d+")
SERIOUS = {"OperationalError", "DatabaseError", "IntegrityError", "MemoryError", "DiskError", "PermissionError"}
MAX_EVIDENCE = 3500
TAIL_BYTES = 400_000


@dataclass
class Cluster:
    key: str
    title: str
    severity: str
    sample: str
    count: int = 1
    kinds: set[str] = field(default_factory=set)


def _short_path(path: str) -> str:
    path = path.replace("\\", "/")
    for marker in ("/argus/", "/site-packages/"):
        if marker in path:
            return path.split(marker, 1)[1] if marker == "/site-packages/" else "argus/" + path.split(marker, 1)[1]
    return path.rsplit("/", 1)[-1]


def parse(text: str) -> list[Cluster]:
    lines = text.splitlines()
    clusters: dict[str, Cluster] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().endswith(TRACE_START) or line.strip() == TRACE_START:
            block = [line]
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i])
                i += 1
            exc = EXC_LINE.match(lines[i].strip()) if i < len(lines) else None
            if i < len(lines):
                block.append(lines[i])
                i += 1
            frames = [m for m in (FRAME.match(b) for b in block) if m]
            exc_type = (exc.group(1).rsplit(".", 1)[-1] if exc else "UnknownError")
            where = f"{_short_path(frames[-1].group(1))} in {frames[-1].group(3)}" if frames else "unknown location"
            key = f"log:{exc_type}@{where}"
            severity = "high" if exc_type in SERIOUS else "medium"
            sample = "\n".join(block)
            if key in clusters:
                clusters[key].count += 1
            else:
                clusters[key] = Cluster(key, f"{exc_type} in {where}", severity, sample)
            continue
        a = ACCESS_5XX.search(line)
        if a:
            method, path, status = a.group(1), NOISE.sub("#", a.group(2).split("?", 1)[0])[:80], a.group(3)
            key = f"log5xx:{method} {path} {status}"
            if key in clusters:
                clusters[key].count += 1
            else:
                clusters[key] = Cluster(key, f"{method} {path} answers {status}", "medium", f"{method} {path} -> {status}")
            i += 1
            continue
        m = ERROR_LINE.search(line)
        if m:
            level, message = m.group(1), m.group(2).strip()
            shape = NOISE.sub("#", message)[:160]
            key = "logerr:" + hashlib.sha1(shape.encode("utf-8")).hexdigest()[:8]
            context = "\n".join(lines[max(0, i - 2): i + 3])
            if key in clusters:
                clusters[key].count += 1
            else:
                clusters[key] = Cluster(key, f"Repeated error: {shape[:90]}", "critical" if level == "CRITICAL" else "medium", context)
            clusters[key].kinds.add(level)
        i += 1
    return list(clusters.values())


def _read(cfg: Config, deps: Deps, state: State) -> str | None:
    if cfg.log_file:
        path = Path(cfg.log_file)
        if not path.is_file():
            return None
        stat = path.stat()
        size, ident = stat.st_size, f"{stat.st_dev}:{stat.st_ino}"
        offset = int(state.kv_get("log_offset", "0"))
        if size < offset or ident != state.kv_get("log_ident"):     # truncated, or replaced by a new file
            offset = 0
        state.kv_set("log_ident", ident)
        with path.open("rb") as fh:
            fh.seek(max(offset, size - TAIL_BYTES))
            data = fh.read()
        state.kv_set("log_offset", str(size))
        return data.decode("utf-8", "replace")
    if cfg.log_command:
        since = state.kv_get("log_since") or datetime.fromtimestamp(deps.now() - cfg.log_window_min * 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        argv = [part.replace("{since}", since) for part in cfg.log_command]
        result = deps.run(argv, timeout=90)
        if not result.ok and not (result.out or result.err):
            return None
        state.kv_set("log_since", datetime.fromtimestamp(deps.now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return result.out + "\n" + result.err
    return None


def run(cfg: Config, deps: Deps, state: State) -> CheckResult | None:
    text = _read(cfg, deps, state)
    if text is None:
        return None
    result = CheckResult(complete=False)    # a quiet scan does not prove the bug is fixed
    for c in parse(text):
        if c.key.startswith(("logerr:", "log5xx:")) and c.count < cfg.log_min_count and c.severity != "critical":
            continue
        evidence = redact(c.sample)[:MAX_EVIDENCE] + f"\n\n(seen {c.count}x in this scan)"
        result.observations.append(Observation(key=c.key, title=c.title, severity=c.severity, evidence=evidence))
    return result
