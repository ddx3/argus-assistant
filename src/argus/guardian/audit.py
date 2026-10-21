"""The audit log: one JSON line per thing the guardian did or was stopped from doing.

It is append-only by construction (the file is only ever opened for appending) and every string
goes through `redact`, so the log can be read, shared, and kept without a second thought. Tool
calls are recorded whether they succeeded or were blocked: a blocked call is the interesting
kind, because it is what a manipulated model looks like from outside.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from argus.guardian.redact import redact

MAX_FIELD = 600
_lock = threading.Lock()


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + f"… (+{len(text) - MAX_FIELD})"
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value][:40]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean(str(value))


class Audit:
    def __init__(self, home: Path, run_id: str = "") -> None:
        self.path = home / "audit.jsonl"
        self.run_id = run_id
        home.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **fields: Any) -> None:
        record = {"t": round(time.time(), 3), "run": self.run_id, "kind": kind, **_clean(fields)}
        line = json.dumps(record, ensure_ascii=False)
        with _lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def tail(self, n: int = 40) -> list[dict]:
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines if line.strip()]
