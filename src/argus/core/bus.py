"""Event bus: the pipe between the pipeline modules.

Capture does not know about Whisper, Whisper does not know about the timeline: they
all throw events onto the bus and subscribe to the types they need. Without this,
within a month everything would be glued together for good and swapping a local STT
for a cloud one would be impossible.

Two rules that the whole design grows from:

1. **Publishing never waits.** Audio comes from a thread started by Windows, with a
   hard deadline: stall on the callback and the recording has a hole. So the queues
   are bounded in size, and on overflow the oldest item is dropped. Better to lose
   a frame than to tear the stream.

2. **Losses are counted, not hushed up.** Every subscription remembers how many
   events it dropped. The dashboard shows it: if the counter grows, some stage is
   falling behind, and you need to see that at once, not guess.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

# Sensible queue sizes: audio comes more often than frames, so it gets a bigger reserve.
DEFAULT_MAXSIZE = 64


class Subscription:
    """A subscription to one or more event types.

    Used as an async iterator::

        sub = bus.subscribe(AudioChunk)
        async for chunk in sub:
            ...
    """

    __slots__ = ("_bus", "_queue", "_types", "dropped", "name", "received")

    def __init__(self, bus: Bus, types: tuple[type, ...], maxsize: int, name: str):
        self._bus = bus
        self._types = types
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.received = 0
        self.name = name

    async def _deliver(self, event: Any) -> None:
        """Put an event, waiting for room. For replay only."""
        await self._queue.put(event)
        self.received += 1

    def _offer(self, event: Any) -> None:
        """Put an event. Called only from the event loop thread."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # We drop the oldest: fresh data is worth more than stale data.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                pass
        else:
            self.received += 1

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        return await self._queue.get()

    async def get(self) -> Any:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __repr__(self) -> str:
        names = "/".join(t.__name__ for t in self._types)
        return f"<Subscription {self.name} [{names}] q={self.qsize()} drop={self.dropped}>"


class Bus:
    """An event dispatcher bound to a single asyncio loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._subs: dict[type, list[Subscription]] = defaultdict(list)
        # The "in flight" ceiling for types that arrive heavy from foreign threads (screen
        # frames): see `limit_pending`. The count and the lock exist only for them.
        self._pending_limit: dict[type, int] = {}
        self._pending: dict[type, int] = defaultdict(int)
        self._pending_lock = threading.Lock()
        # How many events were rejected at the entrance because of the ceiling: losses are
        # counted (rule 2).
        self.shed = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the bus to the loop. Required before publishing from threads."""
        self._loop = loop

    def limit_pending(self, event_type: type, limit: int) -> None:
        """Keep no more than `limit` events of this type in flight from foreign threads.

        `publish_threadsafe` puts the delivery on the loop's queue, and that queue is
        unbounded: while the loop stands still (a slow disk in `Recorder.close`, session
        cleanup, a heavy handler), the capture thread keeps adding frames, thirty per second,
        and each weighs up to 14 MB. A one-second stall is hundreds of megabytes, which are
        released only when the loop comes back to life and are immediately dropped by the
        subscribers' queues. So while `limit` events are already in flight, the next ones are
        simply not queued (the first `limit` remain, and the dropped ones are counted in
        `shed`).
        """
        self._pending_limit[event_type] = limit

    def subscribe(
        self,
        *types: type,
        maxsize: int = DEFAULT_MAXSIZE,
        name: str = "",
    ) -> Subscription:
        if not types:
            raise ValueError("a subscription without event types is meaningless")
        sub = Subscription(self, types, maxsize, name or types[0].__name__.lower())
        for t in types:
            self._subs[t].append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        for subs in self._subs.values():
            if sub in subs:
                subs.remove(sub)

    def publish(self, event: Any) -> None:
        """Publish from the event loop. Does not block; under congestion it drops the old."""
        for sub in self._subs.get(type(event), ()):
            sub._offer(event)

    async def publish_all(self, event: Any) -> None:
        """Publish, waiting for room in the queues. Loses nothing.

        Live capture cannot be published this way: stop the publisher and you stop the
        hardware, and the audio gets a hole. But in a replay the publisher is a file; it can
        wait as long as it likes, and it must deliver everything. A replayed session that has
        lost frames is no longer the same session, and the whole idea of reproducible
        debugging breaks down on that.
        """
        for sub in self._subs.get(type(event), ()):
            await sub._deliver(event)

    def publish_threadsafe(self, event: Any) -> None:
        """Publish from a foreign thread, for example from a PyAudio callback.

        asyncio.Queue is not thread-safe, so we hand the delivery itself over to the event
        loop. The call itself returns instantly, and the capture thread does not wait.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError("Bus is not bound to a loop: call bus.bind(loop)")
        kind = type(event)
        if kind not in self._pending_limit:
            try:
                loop.call_soon_threadsafe(self.publish, event)
            except RuntimeError:
                # The loop is already closed and shutdown is in progress, so nobody needs the event.
                pass
            return

        with self._pending_lock:
            if self._pending[kind] >= self._pending_limit[kind]:
                self.shed += 1
                shed = self.shed
            else:
                self._pending[kind] += 1
                shed = 0
        if shed:
            if shed == 1 or shed % 100 == 0:
                log.warning(
                    "event loop is falling behind: dropped on input: %s (total dropped: %d)",
                    kind.__name__, shed,
                )
            return
        try:
            loop.call_soon_threadsafe(self._publish_counted, kind, event)
        except RuntimeError:
            with self._pending_lock:
                self._pending[kind] -= 1

    def _publish_counted(self, kind: type, event: Any) -> None:
        """Delivery of an event that was counted as "in flight" (`limit_pending`)."""
        with self._pending_lock:
            self._pending[kind] -= 1
        self.publish(event)

    def stats(self) -> dict[str, dict[str, int]]:
        """Summary of the subscriptions, for the dashboard."""
        seen: dict[str, dict[str, int]] = {}
        for subs in self._subs.values():
            for sub in subs:
                seen[sub.name] = {
                    "received": sub.received,
                    "dropped": sub.dropped,
                    "queued": sub.qsize(),
                }
        return seen
