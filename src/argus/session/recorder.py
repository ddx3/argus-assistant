"""Recording a session to disk.

The task is not just to save the data, but to save it so that the replay sees exactly the
same thing the live pipeline saw. Hence the one non-trivial decision here: **gaps in the
stream are filled with silence**.

Why. A device can stop delivering data, for example a loopback during complete silence,
or a headset that dropped off for a second. If we simply append what arrives, the file
"collapses": two chunks with a second of pause between them end up next to each other, and
the whole timeline after that spot shifts. Speech said in the fifth minute would sound in
the fourth during replay, and the match with the screen frames would fall apart.

By filling the gap with silence we get the property "position in the file = time", after
which replay becomes ordinary linear reading. A side benefit: the counter of inserted
silence immediately shows whether gaps actually happen.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from argus.core import atomic
from argus.core.events import SAMPLE_RATE, AudioChunk, Frame, Source
from argus.session.layout import (
    FRAMES_DIR,
    FRAMES_INDEX,
    META_NAME,
    WAV_NAMES,
    DeviceMeta,
    FrameRecord,
    SessionMeta,
)

if TYPE_CHECKING:
    from datetime import datetime

    from argus.core.config import RecordConfig

log = logging.getLogger(__name__)

NS_PER_SEC = 1_000_000_000
# A gap smaller than this is not counted as a gap: scheduler jitter of a couple of
# milliseconds is normal, not a loss of data.
GAP_TOLERANCE_SAMPLES = SAMPLE_RATE // 200  # 5 ms

# Silence is written in blocks of a minute, not as one piece for the whole gap: after the
# computer sleeps the gap is hours, and an array of zeros for eight hours is 1.8 GiB of
# memory at once and seconds of a stopped event loop. There is one block for everyone,
# prepared in advance.
SILENCE_BLOCK_SAMPLES = SAMPLE_RATE * 60
_SILENCE = bytes(SILENCE_BLOCK_SAMPLES * 2)

# Limit of one track. The size of the WAV data lies in the header as 32 bits, and at 4 GiB
# (37 hours on one track) `wave` fails with `struct.error` right from the recording. Slightly
# below the limit, leaving room for the header; beyond that the track is not written, and
# the session stays readable.
WAV_MAX_SAMPLES = 2**31 - 1024

# Idle time of the whole recording, with no sound and no frames, longer than this counts as
# computer sleep or a device that went away. In the file such idle time lies as a short
# stretch of silence, not in full: eight hours of sleep would otherwise become about 0.9 GB
# of zeros per track and push real sessions out of `keep_gb`. The file time is shifted at
# once for audio and frames (a shared reference point), so "position in the file = time"
# stays true between them too, and the metadata records how much was cut out.
MAX_IDLE_NS = 300 * NS_PER_SEC
KEEP_IDLE_NS = 2 * NS_PER_SEC

# How often to update the metadata (`meta.json`) of a live recording. It used to be written
# only on close, and a session cut off by a crash or a power loss stayed a folder without
# metadata: not visible in the list, not openable by replay, not counted in the `keep_gb`
# ceiling.
META_REFRESH_S = 30.0


class _Track:
    """One audio track: a WAV in which the position equals the time."""

    def __init__(self, path: Path) -> None:
        # The file lives as long as the object and is closed in close().
        self._wav = wave.open(str(path), "wb")  # noqa: SIM115
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)
        self.name = path.name
        self.written = 0          # how many samples are already in the file
        self.chunks = 0
        self.silence = 0          # how many of them are appended silence
        self.overlaps = 0         # chunks that arrived "into the past"
        self.full = False         # the WAV limit was reached: we write no more

    def _fits(self, n: int) -> bool:
        """Whether `n` more samples still fit in the WAV; if not, the track is closed for writing."""
        if self.written + n <= WAV_MAX_SAMPLES:
            return True
        if not self.full:
            self.full = True
            log.warning("WAV size limit reached on track %s — not recording any more", self.name)
        return False

    def _write_silence(self, n: int) -> None:
        """Append `n` samples of silence in blocks, so memory does not grow with the length of the gap."""
        view = memoryview(_SILENCE)
        while n > 0:
            part = min(n, SILENCE_BLOCK_SAMPLES)
            self._wav.writeframes(view[: part * 2])
            self.written += part
            self.silence += part
            n -= part

    def write(self, chunk: AudioChunk, origin_ns: int = 0) -> None:
        want = (chunk.t - origin_ns) * SAMPLE_RATE // NS_PER_SEC
        gap = want - self.written

        silence = gap if gap > GAP_TOLERANCE_SAMPLES else 0
        if gap < -GAP_TOLERANCE_SAMPLES:
            # The chunk belongs to an already recorded stretch. We cannot rewrite the past (the file is
            # written sequentially), so we append to the end and mark it, so that a parser can see an
            # overlap here.
            self.overlaps += 1
        if not self._fits(silence + len(chunk.samples)):
            return
        if silence:
            self._write_silence(silence)

        self._wav.writeframes(chunk.samples.tobytes())
        self.written += len(chunk.samples)
        self.chunks += 1

    def pad_to(self, t_ns: int) -> None:
        """Extend a track with silence up to a given moment.

        This is needed for the tail. Measured on this machine: if nothing plays through the output
        device, the WASAPI loopback does not call the callback **even once**: in 12 seconds zero
        chunks arrived. Gap filling fires only when the next chunk arrives, so without this method
        a track that went silent until the end of the session would stay shorter than the others,
        and the property "position in the file = time" would stop holding.
        """
        want = t_ns * SAMPLE_RATE // NS_PER_SEC
        gap = want - self.written
        if gap <= GAP_TOLERANCE_SAMPLES or not self._fits(gap):
            return
        self._write_silence(gap)

    def close(self) -> None:
        self._wav.close()

    @property
    def silence_ms(self) -> float:
        return self.silence / SAMPLE_RATE * 1000


class Recorder:
    """Writes everything that travels on the bus into the session folder.

    Frames are encoded to JPEG in a separate thread: encoding costs about 5 ms, and doing it
    in the shared loop would slow down everything else.
    """

    def __init__(
        self,
        path: Path,
        cfg: RecordConfig,
        started_at: datetime,
        t0_ns: int = 0,
    ) -> None:
        """`t0_ns` is the pipeline clock time that becomes the zero of the file.

        Zero by default: the recording from the app launch. But a recording starts on a button
        press, and it is pressed a minute or two after launch, so without a reference point the
        track would get those minutes as silence at the start. An hour of silence weighs the same
        220 MB as an hour of conversation.
        """
        self.path = path
        self.cfg = cfg
        self._t0_ns = t0_ns
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / FRAMES_DIR).mkdir(exist_ok=True)

        self._tracks = {src: _Track(self.path / name) for src, name in WAV_NAMES.items()}
        self._index = (self.path / FRAMES_INDEX).open("w", encoding="utf-8")
        self._frames = 0
        self._screen_size: tuple[int, int] | None = None
        self._last_t = 0

        self.meta = SessionMeta(started_at=started_at)

        self._queue: queue.Queue[tuple[int, int, np.ndarray] | None] = queue.Queue(maxsize=16)
        self._dropped_frames = 0

        # Frame thinning. `_skipped` are deliberately not recorded, unlike `_dropped`, which the
        # disk could not keep up with. They are different things: the first is a setting, the
        # second is a loss.
        self._skipped_frames = 0
        self._frame_step_ns = int(1e9 / cfg.frames_fps)
        self._next_frame_t = 0

        # How much idle time was cut out (`_rebase`), which tracks dropped off, and what was said
        # about it.
        self._idle_skipped_ns = 0
        self._dead: set[Source] = set()
        self._problems: list[str] = []
        self._meta_failed = False
        self._meta_at = time.monotonic()
        self._write_meta(finished=False)       # metadata from the first second, not only on close

        self._writer = threading.Thread(target=self._write_frames, name="rec-frames", daemon=True)
        self._writer.start()

    # -- receiving events ------------------------------------------------------

    def feed_audio(self, chunk: AudioChunk) -> None:
        """Accept a chunk of audio. Never raises: the audio pump sits on this call, and a track
        that cannot be written (disk full) must not break the other tracks or the whole intake."""
        self._rebase(chunk.t)
        source = chunk.source
        if source not in self._dead:
            try:
                self._tracks[source].write(chunk, self._t0_ns)
            except Exception as e:  # ENOSPC, MemoryError, WAV limit
                self._dead.add(source)
                self._problems.append(f"track {source.value} stopped: {type(e).__name__}")
                log.exception("track recording stopped: %s", source.value)
        self._last_t = max(self._last_t, chunk.t_end - self._t0_ns)
        self._refresh_meta()

    def _rebase(self, t_ns: int) -> None:
        """Cut out an idle stretch longer than `MAX_IDLE_NS` (sleep, a device that dropped off),
        leaving `KEEP_IDLE_NS` of silence. Idle means that neither audio nor frames came on any
        track: a silent loopback with a live microphone does not count as idle, because that
        silence is real and is needed to keep the tracks the same length.

        The reference point is shared by audio and frames, so the shift moves that same point:
        after it, both land on the new time identically."""
        idle = t_ns - self._t0_ns - self._last_t
        if idle <= MAX_IDLE_NS:
            return
        skip = idle - KEEP_IDLE_NS
        self._t0_ns += skip
        self._idle_skipped_ns += skip
        log.info("the recording had no data for %.0f min — %.0f s kept in the file",
                 idle / NS_PER_SEC / 60, KEEP_IDLE_NS / NS_PER_SEC)

    def feed_frame(self, frame: Frame) -> None:
        self._rebase(frame.t)
        # Thinning lives here, not at the caller, and this matters. Frames arrive at the capture
        # rate, thirty per second, and there is no need to write them all: a gigabyte per ten
        # minutes goes to thirty copies of one picture. At first thinning was in the dashboard, and
        # `argus record` did not know about it: the same setting worked in one place and silently
        # did not work in the other. A rule should have one home.
        t = frame.t - self._t0_ns
        if t < self._next_frame_t:
            self._skipped_frames += 1
            return
        self._next_frame_t = t + self._frame_step_ns

        if self._screen_size is None:
            self._screen_size = (frame.image.shape[1], frame.image.shape[0])
        try:
            # We do not copy the frame: bettercam gives a fresh array on every grab().
            self._queue.put_nowait((t, self._frames, frame.image))
        except queue.Full:
            # The disk cannot keep up. Skipping a frame is more honest than slowing down capture; the
            # counter goes into meta.json, so the loss does not stay unnoticed.
            self._dropped_frames += 1
            return
        self._frames += 1
        self._last_t = max(self._last_t, t)
        self._refresh_meta()

    # -- background frame writing ---------------------------------------------

    def _write_frames(self) -> None:
        import cv2

        params = [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality]
        while True:
            item = self._queue.get()
            if item is None:
                break
            t, seq, image = item
            try:
                if self.cfg.max_frame_width and image.shape[1] > self.cfg.max_frame_width:
                    scale = self.cfg.max_frame_width / image.shape[1]
                    image = cv2.resize(
                        image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                    )
                name = f"{seq:06d}.jpg"
                cv2.imwrite(str(self.path / FRAMES_DIR / name), image, params)
                line = FrameRecord(t=t, seq=seq, file=name).model_dump_json()
                self._index.write(line + "\n")
            except Exception:
                log.exception("could not write frame %d", seq)

    # -- finishing ---------------------------------------------------------

    def close(self, mic: DeviceMeta | None = None, loopback: DeviceMeta | None = None) -> SessionMeta:
        """Close the recording and write the metadata file. Never raises: the owner
        (`Argus._close_session`) resets `recorder` after closing, and an exception from here
        would leave it hanging and the session without a metadata file, that is, invisible to
        the list and to cleanup."""
        try:
            self._queue.put(None, timeout=5.0)
            self._writer.join(timeout=10.0)
        except Exception:  # the frame writer is dead, the queue is full
            log.exception("the frame writer did not stop")
        try:
            self._index.close()
        except OSError:
            log.exception("the frame index was not closed")
        for track in self._tracks.values():
            try:
                # All tracks have the same length; otherwise a track that went silent would be shorter, and
                # during replay its time would not match the others.
                track.pad_to(self._last_t)
            except Exception:  # disk full, WAV limit
                self._problems.append(f"track {track.name} was not written to the end")
                log.exception("track not written to the end: %s", track.name)
            finally:
                try:
                    track.close()
                except Exception:  # the same: the WAV header was not finished
                    log.exception("track closed with an error: %s", track.name)

        self.meta.mic = mic
        self.meta.loopback = loopback
        self._write_meta(finished=True)
        return self.meta

    # -- metadata ------------------------------------------------------------

    def _fill_meta(self) -> None:
        self.meta.duration_ns = self._last_t
        self.meta.screen_size = self._screen_size
        # `_frames` grows only on a successful enqueue, so losses must not be subtracted again: the
        # counter would go negative.
        self.meta.frames = self._frames
        self.meta.chunks = {src.value: t.chunks for src, t in self._tracks.items()}
        self.meta.silence_filled_ms = {
            src.value: round(t.silence_ms, 1) for src, t in self._tracks.items()
        }
        self.meta.idle_skipped_ms = round(self._idle_skipped_ns / 1_000_000, 1)
        self.meta.problems = list(self._problems)

    def _write_meta(self, *, finished: bool) -> None:
        """Write `meta.json`. For a live recording it is preliminary (`finished=False`): if the
        process is cut off, the session is still found, opens in replay and counts toward
        `keep_gb`."""
        self._fill_meta()
        self.meta.finished = finished
        try:
            atomic.write_text(
                self.path / META_NAME,
                json.dumps(json.loads(self.meta.model_dump_json()), ensure_ascii=False, indent=2),
            )
        except OSError:
            if not self._meta_failed:       # one line, not once every half a minute
                log.exception("the recording manifest was not written")
            self._meta_failed = True

    def _refresh_meta(self) -> None:
        now = time.monotonic()
        if now - self._meta_at >= META_REFRESH_S:
            self._meta_at = now
            self._write_meta(finished=False)

    # -- observation ---------------------------------------------------------

    @property
    def frames_written(self) -> int:
        return self._frames

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def silence_ms(self, source: Source) -> float:
        return self._tracks[source].silence_ms

    def overlaps(self, source: Source) -> int:
        return self._tracks[source].overlaps
