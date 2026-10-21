"""Attempt counters: no more than N events per window per key (email, address, account).

They are needed where one person could run expensive work for free or guess someone
else's password: sign-in, registration, password change, recovery emails. They are kept in
process memory. There is one server (one worker), as with the other counters
(`relay_stt._open`), and starting from zero after a restart does not help the attacker,
because they cannot restart the server.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Sequence


class Window:
    """A sliding window of events by key.

    `tiers` are pairs (how many, in how many seconds): a key is closed as soon as any tier has
    collected that many events. Two tiers are needed because the short one does not stop an
    honest person from making a couple of mistakes, and the long one prevents slow password
    guessing, one attempt every few minutes.

    We keep no more than `max_keys` keys: otherwise a stream of different addresses would
    bloat the server's memory, exactly what such a counter is put there to prevent. The oldest
    is evicted.
    """

    def __init__(self, tiers: Sequence[tuple[int, float]], max_keys: int = 20_000) -> None:
        self.tiers = tuple(tiers)
        self.max_keys = max_keys
        self._span = max(span for _, span in self.tiers)
        self._cap = max(limit for limit, _ in self.tiers) * 2
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _trim(self, events: deque[float], now: float) -> None:
        while events and events[0] <= now - self._span:
            events.popleft()

    def wait(self, key: str, now: float | None = None) -> float:
        """How many seconds to wait until the key opens. 0 means allowed. Writes nothing."""
        now = time.time() if now is None else now
        with self._lock:
            events = self._events.get(key)
            if not events:
                return 0.0
            self._trim(events, now)
            worst = 0.0
            for limit, span in self.tiers:
                recent = [e for e in events if e > now - span]
                if len(recent) >= limit:
                    # The `limit`-th event from the end stays in the window the longest.
                    worst = max(worst, recent[-limit] + span - now)
            if not events:
                del self._events[key]
            return max(0.0, worst)

    def add(self, key: str, now: float | None = None) -> None:
        """Record an event under the key."""
        now = time.time() if now is None else now
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self.max_keys:
                    self._events.popitem(last=False)
                events = self._events[key] = deque()
            else:
                self._events.move_to_end(key)
            events.append(now)
            self._trim(events, now)
            while len(events) > self._cap:
                events.popleft()

    def clear(self, key: str) -> None:
        """Forget the key: the person proved that they are who they say they are."""
        with self._lock:
            self._events.pop(key, None)


def wait_text(seconds: float) -> str:
    """Text such as "a minute", "14 min" or "3 h": it goes after the word "wait" in the phrase
    "<what> — wait <time>".
    """
    s = math.ceil(seconds)
    if s <= 90:
        return "a minute"
    minutes = math.ceil(s / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{math.ceil(minutes / 60)} h"
