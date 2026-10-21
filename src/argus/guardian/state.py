"""What the guardian remembers between cycles: findings, daily counters, and small notes.

SQLite, because a cycle can be killed at any moment (a closed laptop, a reboot) and a half-written
JSON file would lose the state. Every change is a single statement in a transaction.

A *finding* is one problem, identified by a stable `key` (for example `probe:api` or
`test:tests/test_x.py::test_y`). The same problem seen again updates the same row instead of
creating a new one, which is what keeps the reports quiet.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# open -> diagnosed -> proposed -> approved -> fixing -> pr_open
#                          \-> dismissed        \-> fix_failed
# `resolved` is reached from any state when the problem stops being seen.
STATUSES = ("open", "diagnosed", "proposed", "approved", "fixing", "pr_open", "fix_failed", "resolved", "dismissed")
ACTIVE = ("open", "diagnosed", "proposed", "approved", "fixing", "fix_failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, check_name TEXT NOT NULL, title TEXT NOT NULL,
    severity TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_notified REAL, last_diagnosed REAL,
    diagnosis TEXT, pr_url TEXT, branch TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS counters (day TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY (day, name));
CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class Finding:
    id: str
    key: str
    check_name: str
    title: str
    severity: str
    evidence: str
    status: str
    count: int
    first_seen: float
    last_seen: float
    last_notified: float | None = None
    last_diagnosed: float | None = None
    diagnosis: dict | None = None
    pr_url: str = ""
    branch: str = ""
    note: str = ""


def finding_id(key: str) -> str:
    return "F-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]


class State:
    def __init__(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(home / "state.db", isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- findings ----------------------------------------------------------------------

    def _row(self, row: sqlite3.Row | None) -> Finding | None:
        if row is None:
            return None
        data = dict(row)
        data["key"] = data["key"]
        data["diagnosis"] = json.loads(data["diagnosis"]) if data["diagnosis"] else None
        data["pr_url"] = data["pr_url"] or ""
        data["branch"] = data["branch"] or ""
        data["note"] = data["note"] or ""
        return Finding(**data)

    def get(self, ident: str) -> Finding | None:
        return self._row(self.db.execute("SELECT * FROM findings WHERE id=? OR key=?", (ident, ident)).fetchone())

    def list(self, statuses: tuple[str, ...] | None = None) -> list[Finding]:
        rows = self.db.execute("SELECT * FROM findings ORDER BY last_seen DESC").fetchall()
        found = [self._row(r) for r in rows]
        return [f for f in found if f and (statuses is None or f.status in statuses)]

    def upsert(self, *, key: str, check_name: str, title: str, severity: str, evidence: str, now: float) -> tuple[Finding, bool]:
        """Record a sighting. Returns the finding and whether it is new (or came back after being resolved)."""
        existing = self.get(key)
        if existing is None:
            self.db.execute(
                "INSERT INTO findings (id, key, check_name, title, severity, evidence, status, count, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?, 'open', 1, ?, ?)",
                (self._free_id(key), key, check_name, title, severity, evidence, now, now))
            return self.get(key), True  # type: ignore[return-value]
        came_back = existing.status in ("resolved",)
        status = "open" if came_back else existing.status
        extra = ", diagnosis=NULL, last_diagnosed=NULL, pr_url='', branch='', note='', last_notified=NULL, first_seen=?" if came_back else ""
        args: list[Any] = [title, severity, evidence, status, now]
        sql = "UPDATE findings SET title=?, severity=?, evidence=?, status=?, count=count+1, last_seen=?" + extra
        if came_back:
            args.append(now)
        self.db.execute(sql + " WHERE key=?", (*args, key))
        return self.get(key), came_back  # type: ignore[return-value]

    def _free_id(self, key: str) -> str:
        """The short id of a key, made longer if another problem already holds it (a log can carry chosen text)."""
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        for length in range(6, len(digest) + 1):
            candidate = "F-" + digest[:length]
            if self.db.execute("SELECT 1 FROM findings WHERE id=?", (candidate,)).fetchone() is None:
                return candidate
        raise RuntimeError("no free id for a problem key")

    def update(self, ident: str, **fields: Any) -> Finding:
        if "diagnosis" in fields and fields["diagnosis"] is not None:
            fields["diagnosis"] = json.dumps(fields["diagnosis"], ensure_ascii=False)
        names = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE findings SET {names} WHERE id=?", (*fields.values(), ident))
        return self.get(ident)  # type: ignore[return-value]

    def resolve_missing(self, check_name: str, seen_keys: set[str]) -> list[Finding]:
        """Mark findings of one check as resolved when their problem was not seen this time."""
        gone = []
        for f in self.list(ACTIVE + ("pr_open",)):
            if f.check_name == check_name and f.key not in seen_keys:
                gone.append(self.update(f.id, status="resolved", note="no longer seen"))
        return gone

    # --- counters and notes ------------------------------------------------------------

    @staticmethod
    def _today(now: float | None = None) -> str:
        return datetime.fromtimestamp(now or time.time(), UTC).strftime("%Y-%m-%d")

    def add(self, name: str, amount: float = 1.0, now: float | None = None) -> float:
        day = self._today(now)
        self.db.execute(
            "INSERT INTO counters (day, name, value) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET value=value+?",
            (day, name, amount, amount))
        return self.today(name, now)

    def today(self, name: str, now: float | None = None) -> float:
        row = self.db.execute("SELECT value FROM counters WHERE day=? AND name=?", (self._today(now), name)).fetchone()
        return float(row["value"]) if row else 0.0

    def kv_get(self, name: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, name: str, value: str) -> None:
        self.db.execute("INSERT INTO kv (name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
