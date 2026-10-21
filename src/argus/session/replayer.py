"""Playing a recorded session back into the pipeline.

The point of the whole idea: the pipeline must not notice the difference between live
capture and a replay. Downstream stand the same subscribers to the same events with the
same timestamps, and the clock is replaced with `ReplayClock`, so any code that asks "what
time is it" gets the recording time, not the current time.

The main benefit follows: a replay can run **faster than real time**. A five-minute
recording at `speed=0` goes through in seconds, and tuning the recognition thresholds
turns from "talk into the microphone and wait" into an ordinary edit loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from argus.core.clock import ReplayClock
from argus.core.events import SAMPLE_RATE, AudioChunk, Frame, Source
from argus.session.layout import (
    FRAMES_DIR,
    FRAMES_INDEX,
    META_NAME,
    WAV_NAMES,
    FrameRecord,
    SessionMeta,
)

if TYPE_CHECKING:
    from argus.core.bus import Bus

log = logging.getLogger(__name__)

NS_PER_SEC = 1_000_000_000


@dataclass(slots=True)
class ReplayStats:
    audio_chunks: int = 0
    frames: int = 0
    missing_frames: int = 0
    duration_ns: int = 0
    wall_seconds: float = 0.0

    @property
    def speedup(self) -> float:
        """How many times faster than real time the replay ran."""
        if self.wall_seconds <= 0:
            return 0.0
        return (self.duration_ns / NS_PER_SEC) / self.wall_seconds


def _read_audio(path: Path, source: Source, chunk_ms: int) -> Iterator[AudioChunk]:
    """Cut the WAV into chunks of the same size that live capture gives.

    The position in the file unambiguously defines the time; the writer guarantees this by
    filling gaps with silence.
    """
    if not path.exists():
        return
    with wave.open(str(path), "rb") as w:
        # Position in the file is time only for the format that the recorder writes. Stereo or
        # 48 kHz (a file brought from somewhere else) would play as garbage with wrong timestamps.
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(
                f"{path.name}: expected mono 16-bit audio at {SAMPLE_RATE} Hz, but the file has "
                f"{w.getnchannels()} channels, {8 * w.getsampwidth()} bits, {w.getframerate()} Hz"
            )
        step = max(1, SAMPLE_RATE * chunk_ms // 1000)
        offset = 0
        while True:
            raw = w.readframes(step)
            if not raw:
                return
            samples = np.frombuffer(raw, dtype=np.int16)
            yield AudioChunk(
                t=offset * NS_PER_SEC // SAMPLE_RATE,
                source=source,
                samples=samples,
            )
            offset += len(samples)


def _read_frames(path: Path, load: bool) -> Iterator[Frame]:
    index = path / FRAMES_INDEX
    if not index.exists():
        return
    import cv2

    with index.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = FrameRecord.model_validate_json(line)
            except ValueError:      # `ValidationError` is also a `ValueError`
                # The last line is cut off: the frame writer did not manage to finish (closing on a timeout,
                # or the folder was copied from a live recording). The audio and the frames before it are
                # intact.
                log.warning("%s: line truncated — not reading further frames", FRAMES_INDEX)
                return
            image = None
            if load:
                image = cv2.imread(str(path / FRAMES_DIR / rec.file))
                if image is None:
                    log.warning("unreadable frame: %s", rec.file)
                    continue
            yield Frame(t=rec.t, image=image, seq=rec.seq)


class Replayer:
    """Recording player.

    `speed`: 1.0 is real time, 2.0 is twice as fast, 0 is no pauses at all.
    """

    def __init__(
        self,
        path: Path,
        bus: Bus,
        chunk_ms: int = 32,
        speed: float = 1.0,
        with_frames: bool = True,
    ) -> None:
        self.path = path
        self.bus = bus
        self.chunk_ms = chunk_ms
        self.speed = speed
        self.with_frames = with_frames
        self.meta = SessionMeta.model_validate_json((path / META_NAME).read_text("utf-8"))
        self.clock = ReplayClock(self.meta.started_at)
        self.stats = ReplayStats(duration_ns=self.meta.duration_ns)

    def _timeline(self) -> Iterator[AudioChunk | Frame]:
        """Merge three sources into one stream ordered by time."""
        streams = [
            _read_audio(self.path / WAV_NAMES[Source.ME], Source.ME, self.chunk_ms),
            _read_audio(self.path / WAV_NAMES[Source.THEM], Source.THEM, self.chunk_ms),
        ]
        if self.with_frames:
            streams.append(_read_frames(self.path, load=True))

        heads: list[AudioChunk | Frame | None] = [next(s, None) for s in streams]
        while True:
            live = [(e.t, i) for i, e in enumerate(heads) if e is not None]
            if not live:
                return
            _, idx = min(live)
            event = heads[idx]
            heads[idx] = next(streams[idx], None)
            yield event

    async def run(self) -> ReplayStats:
        loop = asyncio.get_running_loop()
        started = loop.time()
        # A separate clock for timing: on Windows `loop.time()` ticks once per ~15 ms, and a fast run
        # on it is measured as zero seconds.
        started_precise = time.perf_counter()

        for event in self._timeline():
            self.clock.advance_to(event.t)

            if self.speed > 0:
                # We keep the recording's pace: we wait until real time catches up with the recording.
                target = started + (event.t / NS_PER_SEC) / self.speed
                delay = target - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            elif self.stats.audio_chunks % 64 == 0:
                # Otherwise the loop never yields control and the subscribers cannot process their queues:
                # waiting for space would turn into a deadlock.
                await asyncio.sleep(0)

            # Exactly `publish_all`, not `publish`: a replay must deliver everything.
            await self.bus.publish_all(event)
            if isinstance(event, Frame):
                self.stats.frames += 1
            else:
                self.stats.audio_chunks += 1

        # We let the subscribers read the tails of their queues.
        await asyncio.sleep(0)
        self.stats.wall_seconds = time.perf_counter() - started_precise
        return self.stats
