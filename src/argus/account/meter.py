"""Recognition accounting: how many seconds of audio went to the cloud.

It is needed only when audio goes to recognition straight from the person's computer,
bypassing our server (`realtime_stt.relay` is off or there is no `[account] server`); then
the server does not know about it. The app counts the seconds of utterances itself and
reports once every `FLUSH_S` (`POST /meter/stt`); the server charges the balance and
answers whether to continue. Through the relay (`server/relay_stt.py`) the server counts
itself, and the engine is marked `metered_by_server`, and `SpeechPipeline` puts no seconds
here.

This is accounting, not a lock: a dishonest client may skip reporting while the
recognition key sits on its machine. The distributed build gets no key at all.

Reports go in a thread of their own: the network must not hold the pipeline, and a lost
connection must not stop the conversation. What did not get through accumulates and goes
with the next report; it does not accumulate forever (`MAX_PENDING_S`): beyond the ceiling
we drop what did not get through ourselves, rather than charge the person for hours they
do not remember.

A report must not be charged twice. The connection also breaks after the server has
already charged: the answer is lost, the client thinks it did not get through, and sends
again. So each report has its own number (`report_id`), and it is fixed together with the
seconds and the tier at the moment the report is composed: a retry goes out with the same
number and the same figures, and the seconds that have accumulated in the meantime go as a
separate report with a new number. The server, seeing a number a second time, charges
nothing (`POST /meter/stt`).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass

from argus.account.client import Account

log = logging.getLogger(__name__)

FLUSH_S = 10.0

# The server does not accept a report larger than this at once (`MAX_STT_REPORT_S` in
# `server/app.py`); what accumulated during a connection loss is cut into chunks of this
# size.
CHUNK_S = 120.0

# How many seconds we keep undelivered. An hour of conversation without a connection is the
# limit beyond which this is no longer accounting but guesswork.
MAX_PENDING_S = 3600.0

# The default accuracy tier is the normal one; "high" ("more accurate") has a different
# price.
DEFAULT_TIER = "normal"


@dataclass(frozen=True)
class _Report:
    """One composed report: the number, tier and seconds do not change until it has been delivered."""

    id: str
    tier: str
    seconds: float


def _new_id() -> str:
    """Report number: 32 characters of [0-9a-f]; the server accepts 8-64 of [A-Za-z0-9_-]."""
    return uuid.uuid4().hex


class Meter:
    """Accumulates seconds and reports them in the background."""

    def __init__(self, account: Account, interval: float = FLUSH_S) -> None:
        self.account = account
        self.interval = interval
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()      # one send at a time: a retry does not overtake the first
        # Seconds per accuracy tier: "more accurate" costs more, and merging them into one figure
        # would charge for it at the normal price.
        self._owed: dict[str, float] = {}
        # Composed reports not yet accepted by the server, in order. The report number and figures
        # are fixed: a retry after a break goes out exactly the same.
        self._queue: list[_Report] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def exhausted(self) -> bool:
        """The server has already said there is no money.

        It is read from the last known entitlement, not from a flag of its own: the person topped
        up the balance and pressed "turn on", `Account.state()` refreshed the entitlement on that
        press, and the flag went out by itself. A flag of our own would stay up, and there would be
        no way to reset it without the audio that it itself stops.
        """
        right = self.account.known_right
        return bool(right) and right.get("allowed") is False

    @property
    def pending(self) -> float:
        """How many seconds are still not handed in, across all accuracy tiers together."""
        with self._lock:
            return self._total()

    def _total(self) -> float:
        """Everything not yet handed in: both what has accumulated and what is already composed into
        reports. Call under the lock.
        """
        return sum(self._owed.values()) + sum(r.seconds for r in self._queue)

    def add(self, seconds: float, tier: str = DEFAULT_TIER) -> None:
        """Record the seconds of audio that went into recognition and the accuracy tier they were
        processed at."""
        if seconds <= 0 or not self.account.signed_in:
            return
        with self._lock:
            self._keep(tier, seconds)
        self._ensure_thread()

    def _keep(self, tier: str, seconds: float) -> None:
        """Add to the undelivered amount without exceeding the overall cap. Call under the lock."""
        room = MAX_PENDING_S - self._total()
        if room > 0:
            self._owed[tier] = self._owed.get(tier, 0.0) + min(seconds, room)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="meter", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 — accounting must not fail silently into nowhere
                log.exception("could not report recognition usage")

    def _seal(self) -> None:
        """Compose reports from what has accumulated, in chunks the server accepts. Call under the
        lock.

        A remainder shorter than 0.05 s does not go into a report: there is nothing to charge for
        it, and the report would cost a request.
        """
        owed, self._owed = self._owed, {}
        for tier, seconds in owed.items():
            while seconds > 0.05:
                chunk = min(seconds, CHUNK_S)
                self._queue.append(_Report(_new_id(), tier, chunk))
                seconds -= chunk

    def flush(self) -> None:
        """Send what has accumulated. What was not delivered stays in the queue, with the same number
        and the same seconds.
        """
        with self._flush_lock:
            with self._lock:
                self._seal()
                reports = list(self._queue)
            for report in reports:
                try:
                    self.account.report_stt(report.seconds, report.tier, report_id=report.id)
                except Exception as e:  # noqa: BLE001 — both a server refusal and a broken network
                    # (network failures arrive as `AccountError`; the wide `except` is insurance against the
                    # unexpected). No connection, or the sign-in dropped: we do not lose the data, but we do not
                    # hammer the server either, so we wait until the next visit by the timer. The report and
                    # everything after it stay in the queue as they are; beyond the ceiling `MAX_PENDING_S` new
                    # data does not accumulate, and we drop it ourselves.
                    log.warning("recognition report did not arrive: %s", e)
                    return
                with self._lock:
                    self._queue.remove(report)        # accepted, so it leaves; the others follow

    def close(self) -> None:
        """Stop the thread and hand in the remainder, otherwise the last seconds of the conversation
        would go unpaid."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            log.exception("could not submit the remainder on close")
