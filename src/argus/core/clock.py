"""A single source of time for the whole pipeline.

Audio comes from two devices and frames from a third. To say what happened first, an
interlocutor's line or a screen change, you need a common clock, not three
independent ones.

Wall-clock time (`time.time`) will not do for this: it jumps on NTP synchronization
and on daylight saving changes, and then events get "shuffled" after the fact. We
count with a monotonic counter and remember the wall-clock time once at start, only so
that the session recording has a human-readable date.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

NS_PER_MS = 1_000_000
NS_PER_SEC = 1_000_000_000


@runtime_checkable
class Clock(Protocol):
    """The pipeline clock.

    There are two implementations: a live one and one for replaying a recording. All the
    rest of the code takes the clock as an argument and does not know which one it got,
    and that is exactly what lets a recorded session run through the same pipeline.
    """

    def now(self) -> int:
        """Nanoseconds since the start of the session."""
        ...

    def to_wall(self, t: int) -> datetime:
        """Convert session time to wall-clock time, for logs and file names."""
        ...


class LiveClock:
    """A real-time clock. Zero is the moment of creation."""

    __slots__ = ("_t0_mono", "_t0_wall")

    def __init__(self) -> None:
        self._t0_mono = time.perf_counter_ns()
        self._t0_wall = time.time_ns()

    def now(self) -> int:
        return time.perf_counter_ns() - self._t0_mono

    def to_wall(self, t: int) -> datetime:
        return datetime.fromtimestamp((self._t0_wall + t) / NS_PER_SEC, tz=UTC)

    @property
    def started_at(self) -> datetime:
        return self.to_wall(0)


class ReplayClock:
    """A clock driven by the recording player.

    Time does not flow on its own; whoever feeds events from the file sets it. Thanks to
    this, a replay can run faster than real time while the pipeline still sees the same
    timestamps as in the live recording.
    """

    __slots__ = ("_t", "_t0_wall")

    def __init__(self, started_at: datetime) -> None:
        self._t = 0
        self._t0_wall = int(started_at.timestamp() * NS_PER_SEC)

    def now(self) -> int:
        return self._t

    def advance_to(self, t: int) -> None:
        """Move the time forward. Backward is not allowed: it would break the event order."""
        if t < self._t:
            raise ValueError(f"time does not go backward: {t} < {self._t}")
        self._t = t

    def to_wall(self, t: int) -> datetime:
        return datetime.fromtimestamp((self._t0_wall + t) / NS_PER_SEC, tz=UTC)
