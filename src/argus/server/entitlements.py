"""Entitlement to use the service: whether there is money on the balance, and charging for
usage.

It is calculated only here. The client shows what it is told, but it does not decide: the
app is distributed to people, and any check inside it can be cut out. The one thing that
cannot be cut out is the server's refusal to hand over someone else's answer.

One rule for everything: while the balance is above zero, everything is allowed:
recognition, answers, speech synthesis. What costs how much is in `pricing.py`.

Charging is by fact, not by promise: money is deducted when the usage has already
happened (the audio went to the model, the answer came back). Otherwise a dropped
connection would leave the person with money for which we have already paid. And so as
not to go far into the negative on one expensive request, the ceiling of its cost is
checked before calling the model (`afford`).

But one check is not enough when a call is long: while the model thinks, other requests
arrive against the same remainder, all pass `afford`, and all pay the provider, while only
the remainder can be collected from the person. So the answer and speech relays do not
check but hold (`hold`): the ceiling is deducted from the balance by a single conditional
`UPDATE` before the call, after the call the real price is taken, and the difference is
returned (`settle`); on a failure everything is returned (`release`).

The recognition socket (`relay_stt.py`) uses the same technique for a call that lasts
hours. It holds not the whole call but a window of a minute of audio, which is closed with
the real usage (`settle_many`) and opened again. While the call is alive, the held amount
is recorded in `holds`, and "how much money the person has" is the free balance plus the
held amount (`total`), which is the figure quoted in refusals.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from argus.server import pricing

log = logging.getLogger(__name__)

# `db` is one sqlite3.Connection per process (`check_same_thread=False`, see
# `server/db.py`), shared by all handlers and threads. Without a lock, two simultaneous
# charges read the same remainder, and the money is reduced from the old value: the second
# charge overwrites the first.
_lock = threading.Lock()


@dataclass(frozen=True)
class Right:
    """What this account may do right now."""

    allowed: bool
    balance_micro: int
    reason: str = ""     # why it is not allowed: text for a human

    def as_json(self) -> dict:
        return {
            "allowed": self.allowed,
            "balance_micro": self.balance_micro,
            "balance": pricing.money(self.balance_micro),
            # Running low is a reason to remind, but not a reason to forbid: `allowed` stays true as
            # long as anything is left.
            "low": self.allowed and self.balance_micro < pricing.LOW_BALANCE_MICRO,
            "reason": self.reason,
        }


def balance(db: sqlite3.Connection, account_id: int) -> int:
    row = db.execute(
        "SELECT balance_micro FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return int(row["balance_micro"]) if row else 0


def _empty() -> str:
    return (f"your balance is empty — top up from {pricing.SYMBOL}{pricing.MIN_TOPUP}, "
            "it's in the settings")


def right(db: sqlite3.Connection, account_id: int) -> Right:
    """Whether starting is allowed. As long as there is even a microdollar: yes."""
    have = balance(db, account_id)
    if have > 0:
        return Right(allowed=True, balance_micro=have)
    return Right(allowed=False, balance_micro=0, reason=_empty())


def afford(db: sqlite3.Connection, account_id: int, need_micro: int) -> Right:
    """Is there enough money for a request that in the worst case costs `need_micro`.

    The ceiling is estimated before the request, and the real price is charged after. So a
    person who has enough left for one answer will get their answer, and a person who does
    not have enough even for the ceiling will not: otherwise we would pay for their request
    ourselves.
    """
    have = balance(db, account_id)
    if have <= 0 or have < need_micro:
        return Right(allowed=False, balance_micro=max(0, have),
                     reason=_why_not(db, account_id, need_micro))
    return Right(allowed=True, balance_micro=have)


def held_total(db: sqlite3.Connection, account_id: int) -> int:
    """How much of this account's money is currently held for ongoing calls (`hold`)."""
    row = db.execute(
        "SELECT COALESCE(SUM(micro), 0) AS s FROM holds WHERE account_id = ?", (account_id,)
    ).fetchone()
    return int(row["s"])


def total(db: sqlite3.Connection, account_id: int) -> int:
    """Everything the account has: the free balance plus what is held for its own calls.

    The held amount is still the person's money (it comes back as a remainder when the call
    ends), so "how much you have on the balance" is said by this figure and not by the free
    one.
    """
    return balance(db, account_id) + held_total(db, account_id)


def _lacks(have: int, need_micro: int) -> Right:
    """A refusal: there is no money at all, or not enough for the request ceiling."""
    if have <= 0:
        return Right(allowed=False, balance_micro=0, reason=_empty())
    return Right(
        allowed=False,
        balance_micro=have,
        reason=(f"your balance is {pricing.money(have)}, but the request may cost up to "
                f"{pricing.money(need_micro)} — top up from "
                f"{pricing.SYMBOL}{pricing.MIN_TOPUP}"),
    )


def _why_not(db: sqlite3.Connection, account_id: int, need_micro: int) -> str:
    """What to tell a person who did not have enough money for `need_micro`.

    The real balance is named: the free one together with what is held for their own running
    calls (`total`). The free remainder is reduced by those calls themselves. With $0.50 and a
    running answer that holds $0.40, "your balance is $0.10" would be untrue, and the person
    would go and top up something that has not run out. If in fact it would have been enough
    (everything is taken by their own calls), it says so, without "top up".
    """
    have = total(db, account_id)
    if have < need_micro:
        return _lacks(have, need_micro).reason
    return "part of your money is held by your other requests right now — try again when they finish"


@dataclass(frozen=True)
class Hold:
    """Money held for a single paid call (`hold`)."""

    account_id: int
    allowed: bool
    micro: int = 0        # how much was deducted from the balance for the duration of the call
    id: int = 0           # row in `holds`: the call is closed exactly once through it
    reason: str = ""      # why it was refused: text for a human


def hold(db: sqlite3.Connection, account_id: int, need_micro: int,
         now: float | None = None) -> Hold:
    """Hold `need_micro` on the balance until the end of a paid call.

    The check and the deduction are one `UPDATE ... WHERE balance_micro >= need`: nobody can
    slip in between "is there enough" and "taken", however many requests arrive at once. The
    row in `holds` is written in the same transaction: if the process dies in the middle of a
    call, the held amount comes back at startup (`recover_holds`) and is not lost.
    """
    need = max(1, int(need_micro))
    now = time.time() if now is None else now
    with _lock:
        try:
            cur = db.execute(
                "UPDATE accounts SET balance_micro = balance_micro - ? "
                "WHERE id = ? AND balance_micro >= ?",
                (need, account_id, need),
            )
            if cur.rowcount != 1:
                db.rollback()
                return Hold(account_id, allowed=False, reason=_why_not(db, account_id, need))
            hold_id = int(db.execute(
                "INSERT INTO holds (account_id, micro, at) VALUES (?, ?, ?)",
                (account_id, need, now),
            ).lastrowid)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return Hold(account_id, allowed=True, micro=need, id=hold_id)


def settle(db: sqlite3.Connection, held: Hold, kind: str, units: float,
           cost_micro: int, model: str = "", now: float | None = None) -> int:
    """Close a hold with the real usage. Returns the amount charged, in micro-dollars.

    The usage is recorded, the held amount is returned, and the real price is taken, all in
    one transaction. A price above the held amount (the ceiling underestimated the input) is
    collected from the remainder, but the balance never goes below zero, as in `charge`.
    """
    return settle_many(db, held, [(kind, units, cost_micro, model)], now)


def settle_many(db: sqlite3.Connection, held: Hold,
                rows: list[tuple[str, float, int, str]], now: float | None = None) -> int:
    """Same as `settle`, but with several usage rows: lines of `(kind, units, price, model)`.

    A long call (the recognition socket) needs this: during one hold the audio could go
    through different models, and each has its own price and its own line in the log. Closing
    the hold with the first line and charging the rest separately would return money to the
    balance a moment before the rest are charged, and in that moment someone else could hold
    it. So everything runs in one transaction, as in `settle`. An empty list is just
    `release`.
    """
    now = time.time() if now is None else now
    takes = [pricing.charged(cost) for _, _, cost, _ in rows]
    take = sum(takes)
    with _lock:
        try:
            returned = 0
            if held.id:
                gone = db.execute("DELETE FROM holds WHERE id = ?", (held.id,)).rowcount
                returned = held.micro if gone == 1 else 0   # already closed: we do not return it a second time
            for (kind, units, cost_micro, model), one in zip(rows, takes, strict=True):
                db.execute(
                    "INSERT INTO usage (account_id, kind, amount, at, model, cost_micro, "
                    "charged_micro) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (held.account_id, kind, float(units), now, model, int(cost_micro), one),
                )
            db.execute(
                "UPDATE accounts SET balance_micro = MAX(0, balance_micro + ? - ?) WHERE id = ?",
                (returned, take, held.account_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return take


def release(db: sqlite3.Connection, held: Hold) -> None:
    """Return everything held: the call did not happen and costs the person nothing."""
    if not held.id:
        return
    with _lock:
        try:
            gone = db.execute("DELETE FROM holds WHERE id = ?", (held.id,)).rowcount
            if gone == 1:
                db.execute(
                    "UPDATE accounts SET balance_micro = balance_micro + ? WHERE id = ?",
                    (held.micro, held.account_id),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise


def recover_holds(db: sqlite3.Connection) -> int:
    """Return everything left on hold by the previous process. Called at startup.

    A remainder in `holds` means the process died (a restart, out of memory) between "held"
    and "closed". There is only one server, so at startup there are no live calls and
    everything can be returned. We may have paid the provider without charging the person: a
    loss on our side, but not on theirs.
    """
    with _lock:
        try:
            rows = db.execute("SELECT account_id, micro FROM holds").fetchall()
            for row in rows:
                db.execute(
                    "UPDATE accounts SET balance_micro = balance_micro + ? WHERE id = ?",
                    (int(row["micro"]), int(row["account_id"])),
                )
            db.execute("DELETE FROM holds")
            db.commit()
        except Exception:
            db.rollback()
            raise
    total = sum(int(r["micro"]) for r in rows)
    if rows:
        log.warning("after the previous run, unclosed holds left over: %d, amounting to %s — returned",
                    len(rows), pricing.money(total))
    return total


def charge(db: sqlite3.Connection, account_id: int, kind: str, units: float,
           cost_micro: int, model: str = "", now: float | None = None,
           report_id: str | None = None) -> int:
    """Record the usage and charge the balance. Returns the amount charged, in micro-dollars.

    `cost_micro` is what we paid; `pricing.charged(cost_micro)` is what is charged. Both
    numbers are written to `usage`: the difference between them is the revenue, and it cannot
    be computed later from the price list, because the list may have changed.

    The usage is always recorded, even when there is no longer enough money to charge: the
    balance does not go below zero (`MAX(0, ...)`), but the fact that we paid for it must
    remain. Otherwise an account that burns money on credit would go unnoticed.

    `report_id` is the number of the client's report (`POST /meter/stt`). A report whose number
    is already recorded for this account is not charged a second time and returns 0: the
    client repeats a report when the response was lost, and by then the charge has already
    gone through. The check and the write run under the same lock and in the same
    transaction, and a unique index (`usage_report` in `db.py`) stands behind them in case of
    a second process: its refusal also means "already recorded".
    """
    now = time.time() if now is None else now
    take = pricing.charged(cost_micro)
    with _lock:
        try:
            if report_id is not None and db.execute(
                "SELECT 1 FROM usage WHERE account_id = ? AND report_id = ?",
                (account_id, report_id),
            ).fetchone() is not None:
                return 0
            db.execute(
                "INSERT INTO usage (account_id, kind, amount, at, model, cost_micro, "
                "charged_micro, report_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, kind, float(units), now, model, int(cost_micro), take, report_id),
            )
            db.execute(
                "UPDATE accounts SET balance_micro = MAX(0, balance_micro - ?) WHERE id = ?",
                (take, account_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            if report_id is None:
                raise
            return 0                                  # another process managed to record the same report
        except Exception:
            db.rollback()
            raise
    return take


def credit(db: sqlite3.Connection, account_id: int, micro: int, kind: str,
           note: str = "", now: float | None = None) -> int:
    """Put money on the account. Returns the new balance.

    It is written to the `credits` log in one transaction with the top-up itself: a balance
    without a line in the log is money whose origin cannot be reconstructed.
    """
    if micro <= 0:
        raise ValueError("a top-up must be greater than zero")
    now = time.time() if now is None else now
    with _lock:
        try:
            db.execute(
                "INSERT INTO credits (account_id, kind, micro, note, at) VALUES (?, ?, ?, ?, ?)",
                (account_id, kind, int(micro), note, now),
            )
            db.execute(
                "UPDATE accounts SET balance_micro = balance_micro + ? WHERE id = ?",
                (int(micro), account_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return balance(db, account_id)


def reclaim(db: sqlite3.Connection, account_id: int, micro: int, kind: str,
            note: str = "", now: float | None = None) -> int:
    """Take off the balance money that was returned to the person (a payment refund by the
    payment provider).

    Unlike usage, this can take the balance below zero: the person may have already spent
    the returned money, and this debt must not disappear. While the balance is negative, paid
    calls do not go through (there is nothing to hold), and the next top-up first pays off the
    debt. A line with a negative amount goes into the `credits` log, so the balance still
    equals the log minus usage. The commit is shared with whatever the caller did before
    (for example, marking the order "reversed"): all or nothing.
    """
    if micro <= 0:
        raise ValueError("only a positive amount can be deducted")
    now = time.time() if now is None else now
    with _lock:
        try:
            db.execute(
                "INSERT INTO credits (account_id, kind, micro, note, at) VALUES (?, ?, ?, ?, ?)",
                (account_id, kind, -int(micro), note, now),
            )
            db.execute(
                "UPDATE accounts SET balance_micro = balance_micro - ? WHERE id = ?",
                (int(micro), account_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return balance(db, account_id)


def spent(db: sqlite3.Connection, account_id: int, kind: str,
          since: float = 0.0) -> float:
    """How many units of this kind were spent (seconds, tokens, characters)."""
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM usage "
        "WHERE account_id = ? AND kind = ? AND at > ?",
        (account_id, kind, since),
    ).fetchone()
    return float(row["s"])


def spent_summary(db: sqlite3.Connection, account_id: int, days: int = 30,
                  now: float | None = None) -> dict:
    """Where the money went over the last `days` days, by kind of usage.

    A person who pays for usage needs to see what exactly they paid for: "spent $3" explains
    nothing, while "answers $1.2, recognition $1.8" does.

    `n` is how many times it was used. For answers it is the number of answers: that is what
    the app shows instead of "tokens", which the person has no need to know about.
    """
    now = time.time() if now is None else now
    rows = db.execute(
        "SELECT kind, SUM(amount) AS units, SUM(charged_micro) AS micro, COUNT(*) AS n "
        "FROM usage WHERE account_id = ? AND at > ? GROUP BY kind",
        (account_id, now - days * 86400),
    ).fetchall()
    by_kind = {
        r["kind"]: {"units": float(r["units"]), "micro": int(r["micro"]), "n": int(r["n"])}
        for r in rows
    }
    total = sum(v["micro"] for v in by_kind.values())
    return {
        "days": days,
        "micro": total,
        "total": pricing.money(total),
        "by_kind": by_kind,
    }


def usage_daily(db: sqlite3.Connection, account_id: int, days: int = 30,
                tz_minutes: int = 0, now: float | None = None) -> list[dict]:
    """Usage by day over the last `days` days, for the chart.

    The list is continuous and ends with today: days without usage appear as zeros instead of
    disappearing, otherwise the chart would stretch three active days across the whole axis.
    Day boundaries follow the person's time zone (`tz_minutes`, minutes east of UTC, as the
    browser reports it): usage at one in the morning local time must not land in "yesterday".
    """
    now = time.time() if now is None else now
    # A time zone no wider than real ones (from -12 to +14 hours): garbage in the request must
    # not shift a day by a week.
    shift = max(-14 * 60, min(14 * 60, int(tz_minutes))) * 60
    today = int((now + shift) // 86400)
    first = today - days + 1

    rows = db.execute(
        "SELECT CAST((at + ?) / 86400 AS INTEGER) AS d, kind, SUM(charged_micro) AS micro "
        "FROM usage WHERE account_id = ? AND at >= ? GROUP BY d, kind",
        (shift, account_id, first * 86400 - shift),
    ).fetchall()

    by_day: dict[int, dict[str, int]] = {}
    for r in rows:
        by_day.setdefault(int(r["d"]), {})[r["kind"]] = int(r["micro"])

    out = []
    for d in range(first, today + 1):
        kinds = by_day.get(d, {})
        out.append({
            "day": datetime.fromtimestamp(d * 86400, UTC).date().isoformat(),
            "micro": sum(kinds.values()),
            "by_kind": kinds,
        })
    return out
