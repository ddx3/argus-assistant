"""The money tables: do the numbers still add up?

The accounts database keeps a ledger (`credits`: gifts, paid top-ups, grants, refunds) and a usage log (`usage`),
and a balance that is changed by many code paths. If a path ever forgets one side, nobody notices until a person
complains. This check recomputes what each balance should be and counts the accounts where it differs.

It is read-only twice over: the SQLite connection is opened with `mode=ro`, and the script is a constant that only
runs SELECTs. It reports counts and account numbers, never e-mail addresses or amounts per person.
"""

from __future__ import annotations

import json

from argus.guardian.checks import CheckResult, Observation
from argus.guardian.checks.remote import run_remote_python
from argus.guardian.config import Config
from argus.guardian.deps import Deps
from argus.guardian.state import State

NEEDED_FAILS = 2
STALE_HOLD_S = 15 * 60          # a hold lives as long as one paid call; older ones were left by a call that died
UNCREDITED_AFTER_S = 15 * 60    # a paid order gets its credit in the same transaction; a gap this long is a lost payment
# How the server words the ledger note of a paid top-up: "order" and the number.
ORDER_NOTE = "order "

# Runs inside the server container. The database path comes from the first argument; every statement is a SELECT.
_TEMPLATE = r'''
import json, sqlite3, sys, time
path = sys.argv[1]
db = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=10)
now = time.time()
one = lambda sql, *a: db.execute(sql, a).fetchone()[0]
ids = lambda sql, *a: [r[0] for r in db.execute(sql + " LIMIT 5", a).fetchall()]
out = {}
out["negative"] = ids("SELECT id FROM accounts WHERE balance_micro < 0")
# Expected balance = ledger - charged - held. The server never lets a balance go below zero, so an account that
# spent more than it had legitimately shows 0 where the sum is negative.
drift = """
SELECT a.id FROM accounts a WHERE
  a.balance_micro != (SELECT COALESCE(SUM(micro),0) FROM credits WHERE account_id=a.id)
                   - (SELECT COALESCE(SUM(charged_micro),0) FROM usage WHERE account_id=a.id)
                   - (SELECT COALESCE(SUM(micro),0) FROM holds WHERE account_id=a.id)
  AND NOT (a.balance_micro = 0 AND (SELECT COALESCE(SUM(micro),0) FROM credits WHERE account_id=a.id)
                   - (SELECT COALESCE(SUM(charged_micro),0) FROM usage WHERE account_id=a.id)
                   - (SELECT COALESCE(SUM(micro),0) FROM holds WHERE account_id=a.id) < 0)
"""
out["drift"] = ids(drift)
out["stale_holds"] = one("SELECT COUNT(*) FROM holds WHERE at < ?", now - @STALE@)
out["uncredited_orders"] = ids(
    "SELECT o.id FROM orders o WHERE o.state='paid' AND o.paid_at < ? AND NOT EXISTS "
    "(SELECT 1 FROM credits c WHERE c.kind='topup' AND c.note = @NOTE@ || o.id)", now - @UNCREDITED@)
out["loss_making"] = one("SELECT COUNT(*) FROM usage WHERE charged_micro < cost_micro AND at > ?", now - 86400)
out["accounts"] = one("SELECT COUNT(*) FROM accounts")
print(json.dumps(out))
'''
SCRIPT = (_TEMPLATE.replace("@STALE@", str(STALE_HOLD_S)).replace("@UNCREDITED@", str(UNCREDITED_AFTER_S))
          .replace("@NOTE@", repr(ORDER_NOTE)))


def parse(output: str) -> list[Observation]:
    try:
        data = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [Observation("data:unreadable", "The database check gave no usable answer", "medium", f"output: {output[-200:]!r}")]
    found: list[Observation] = []

    def ids(key: str) -> str:
        return ", ".join(str(i) for i in data.get(key, []))

    if data.get("negative"):
        found.append(Observation("data:negative-balance", "An account has a negative balance", "critical",
                                 f"account number(s): {ids('negative')}"))
    if data.get("drift"):
        found.append(Observation(
            "data:balance-drift", "A balance does not equal the ledger minus usage minus holds", "high",
            f"account number(s): {ids('drift')} (at most 5 shown). Money was added or taken somewhere without a ledger or usage row."))
    if data.get("stale_holds"):
        found.append(Observation(
            "data:stale-holds", f"{data['stale_holds']} hold(s) older than 15 minutes", "medium",
            "A hold is released when its call ends, and at server start. An old one is money stuck behind a call that died."))
    if data.get("uncredited_orders"):
        found.append(Observation(
            "data:uncredited-order", "A paid order has no top-up in the ledger", "critical",
            f"order number(s): {ids('uncredited_orders')} (at most 5 shown). A person paid and was not credited."))
    if data.get("loss_making"):
        found.append(Observation(
            "data:loss-making", f"{data['loss_making']} usage row(s) in the last day charged less than they cost", "medium",
            "The price list may have a gap: we paid the provider more than we charged the person."))
    return found


def run(cfg: Config, deps: Deps, state: State) -> CheckResult | None:
    if not cfg.ssh_command or not cfg.data_container:
        return None
    result = run_remote_python(cfg, deps, SCRIPT, cfg.data_container, cfg.data_db)
    if result is None or not result.out.strip():
        return None            # the host check reports an unreachable server; here the answer is simply unknown
    seen = json.loads(state.kv_get("data_fails", "{}"))
    counts: dict[str, int] = {}
    report = CheckResult()
    for o in parse(result.out):
        counts[o.key] = seen.get(o.key, 0) + 1
        if counts[o.key] >= NEEDED_FAILS:
            report.observations.append(o)
        else:
            report.still_open.add(o.key)
    state.kv_set("data_fails", json.dumps(counts))
    return report
