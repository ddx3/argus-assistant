"""Assembling the whole pipeline into one piece.

Until now every command assembled its own part itself. From here on there are two of them —
the terminal and the dashboard — and both need the same thing: capture, recognition, the
feed, the brain. Duplicating the assembly would mean diverging on the first edit, so it
lives here.

The module draws nothing. It holds the pipeline and hands the state outward; how to show it
is up to the caller.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from argus.brain.assistant import Assistant
from argus.brain.base import Answer
from argus.capture.audio import DeviceInfo
from argus.capture.backend import AudioCapture
from argus.capture.screen import ScreenCapture
from argus.context.store import TimelineStore
from argus.context.timeline import Entry, Timeline
from argus.core import friendly
from argus.core.bus import Bus
from argus.core.clock import LiveClock
from argus.core.config import Config
from argus.core.events import (
    AudioChunk,
    Frame,
    ScreenEvent,
    Source,
    SpeechEvent,
)
from argus.perception.framegate import FrameGate, GateSettings
from argus.perception.speech import SpeechPipeline
from argus.perception.vision_pipeline import VisionPipeline
from argus.session.layout import prune_sessions, session_id
from argus.session.recorder import Recorder

log = logging.getLogger(__name__)


def _free_dir(path: Path) -> Path:
    """A free name next to a taken one: `...-42`, `...-42_2`, `...-42_3`.

    Session names distinguish time only down to the second. While the recording began at app
    startup, that was enough. But now it begins on a button, and the button can be clicked
    off-on faster than in a second — and the second session would write into the same folder,
    overwriting the WAV of the first.
    """
    if not path.exists():
        return path
    for n in range(2, 100):
        candidate = path.with_name(f"{path.name}_{n}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name}_{os.getpid()}")


@dataclass
class Levels:
    """Channel loudness, for display only."""

    rms: float = 0.0
    peak: float = 0.0


@dataclass
class Stage:
    """A measurement of one pipeline stage.

    Latency is the sum of several steps, and when it grows we need to see at once which exact
    step became slow instead of guessing.
    """

    name: str
    last_ms: float = 0.0
    total_ms: float = 0.0
    count: int = 0

    def add(self, ms: float) -> None:
        self.last_ms = ms
        self.total_ms += ms
        self.count += 1

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


@dataclass
class Shot:
    """A frame that went to the model.

    What is stored is exactly what was sent, not one captured again for display. Capturing a
    second time is cheap, but in those same milliseconds the screen manages to change — and the
    person would be looking at one picture while discussing another with the model. This is
    exactly the kind of discrepancy that is later hunted for hours.
    """

    t: int
    jpeg: bytes
    # "screen" is a screenshot, "paste" is a photo the person pasted from the clipboard. The feed
    # and the answer card label them differently: a photo is not the "screen", and calling it that
    # would be lying about the source.
    kind: str = "screen"

    @property
    def kb(self) -> int:
        return len(self.jpeg) // 1024


@dataclass
class AppState:
    """Everything worth showing to the outside."""

    levels: dict[Source, Levels] = field(
        default_factory=lambda: {Source.ME: Levels(), Source.THEM: Levels()}
    )
    speech: deque[SpeechEvent] = field(default_factory=lambda: deque(maxlen=50))

    # Utterances of the shadow engine — the one that listens to the same audio for comparison. A
    # separate queue, not one shared with a mark: this way the main feed stays exactly what it
    # was, and turning comparison on changes none of the counters that were watched before.
    speech_b: deque[SpeechEvent] = field(default_factory=lambda: deque(maxlen=50))
    # Utterances from earlier visits to an open session (`chats/manager.py`, `_restore`): only to
    # show on the work screen as they lie in the journal (`wall` — wall-clock time, `source`,
    # `text`). Not `SpeechEvent`: those carry time from the start of this run, while these are
    # from the past, and they do not go into the feed the prompt is built from — the model does
    # not see them.
    past_speech: list[dict] = field(default_factory=list)
    answers: deque[Answer] = field(default_factory=lambda: deque(maxlen=20))
    # The last frames given to the model. A short memory: we keep ten so that the conversation
    # feed can show what the model was looking at without carrying away megabytes — a frame
    # weighs about a hundred kilobytes.
    shots: deque[Shot] = field(default_factory=lambda: deque(maxlen=10))

    screen_lines: list[str] = field(default_factory=list)
    screen_t: float = 0.0
    screen_window: str | None = None
    frames: int = 0
    started_at: float = 0.0

    stages: dict[str, Stage] = field(
        default_factory=lambda: {
            "speech": Stage("speech -> text"),
            "speech-b": Stage("speech -> text (2)"),
            "screen": Stage("screen -> text"),
            "brain": Stage("model"),
        }
    )


class Argus:
    """The full pipeline: hardware -> recognition -> timeline -> model."""

    def __init__(
        self,
        cfg: Config,
        with_screen: bool = True,
        with_brain: bool = True,
        record: bool = False,
        autostart: bool = False,
    ) -> None:
        self.cfg = cfg
        self.with_screen = with_screen
        self.with_brain = with_brain

        self.clock = LiveClock()
        self.bus = Bus()
        # Screen frames arrive from the capture thread thirty times a second, each up to 14 MB, and
        # subscriber queues hold three each: if the event loop has stopped, there is no point
        # accumulating more than that in the loop (`Bus.limit_pending`).
        self.bus.limit_pending(Frame, 3)
        self.state = AppState()
        self.timeline = Timeline()

        self.audio = AudioCapture(self.bus, self.clock, cfg.audio)
        self.screen = ScreenCapture(self.bus, self.clock, cfg.screen) if with_screen else None
        # The link to the account server is one per app, and it is needed not only by the relays: the
        # recognition spend is also counted through it (`Meter`).
        self._account = None
        self.meter = None
        if cfg.account.server:
            from argus.account.client import Account
            from argus.account.meter import Meter

            self._account = Account(cfg.account.server, cfg.root)
            self.meter = Meter(self._account)
        self.speech = SpeechPipeline(
            self.bus, cfg, meter=self.meter, on_broke=lambda: self.set_running(False),
            account=self._account,
        )
        # Screen capture and screen reading are turned on separately. The model needs the snapshot
        # always, but constant text recognition only where memory of earlier screen content matters.
        self.vision = (
            VisionPipeline(self.bus, cfg) if with_screen and cfg.ocr.enabled else None
        )

        self.assistant = (
            Assistant(self.bus, cfg, self.clock, self.timeline,
                     screenshot=self.screenshot_jpeg, account=self._account)
            if with_brain
            else None
        )

        self.session_dir: Path | None = None
        self.recorder: Recorder | None = None
        self.store: TimelineStore | None = None
        # Why the recording stopped on its own (disk full, folder locked), in human terms; empty means
        # all is well. It is shown among the page's complaints (`problems`).
        self.record_error = ""

        # The recording is turned on by a flag or by the "record always" setting. The
        # second answers the fact that a recording cannot be made after the fact: a session turns out
        # to be interesting only after it has passed.
        self._record = record or cfg.record.always

        # We record frames only on explicit request. Audio weighs next to nothing, but a picture on a
        # game gives more than a gigabyte in ten minutes — constant screen recording would eat the
        # disk in a couple of days.
        self.record_frames = record or cfg.record.always_screen

        # What was deleted to make room. Shown in the console: silently erasing people's recordings
        # is not allowed.
        self.pruned: list[tuple[Path, int]] = []

        # When the button was pressed. The `--seconds` deadline is counted from here too: "ten
        # minutes" means ten minutes of recording, not ten minutes since launch, half of which went
        # on choosing a window.
        self.recording_started_at: int | None = None
        self.recording_began = asyncio.Event()
        # The event loop on which `recording_began` lives: the recording is turned on from a worker
        # thread (`/control` calls `set_running` through `to_thread`), and `Event.set()` from a foreign
        # thread wakes nothing. It is created in `start()`; without a loop (synchronous tests,
        # `argus record`) the event is set directly.
        self._loop: asyncio.AbstractEventLoop | None = None
        # The recording and the on-disk feed live in two threads: the event loop feeds them pieces of
        # audio, and the control panel's worker thread opens and closes them. The lock is held by
        # `set_running` as a whole (two clicks in a row will not open two recordings) and by closing
        # the recording; the pumps take it without waiting and skip a piece if the recording is being
        # closed right then — the event loop will not stall for ten seconds because of a closing.
        self._rec_lock = threading.RLock()
        # The recording was requested explicitly (`argus run --record`), and not turned on by the
        # "record always" setting. Then a recording that failed to open does not leave
        # recognition on: the run for which it was requested (`--seconds`) is meaningless without a
        # recording, and recognition is paid.
        self.record_required = record

        # From which moment (app clock, ns) the current conversation runs. Everything that ended
        # earlier belongs to the previous chat (`reset_conversation`).
        self.conversation_since = 0

        # The last frame is kept by reference: the dashboard encodes it to JPEG on request, not
        # constantly — encoding costs 5 ms, and doing it for nothing is pointless.
        self._last_image = None
        self.mic: DeviceInfo | None = None
        self.loopback: DeviceInfo | None = None
        # Control panel. We do not stop the hardware capture — bringing it back up costs seconds, and
        # the headset may not return. Instead we block the stream at the input of recognition: it
        # turns on and off instantly.
        #
        # By default recognition is **not started**. The dashboard is up, the devices are open, the
        # models are warm — but nothing goes into the feed until the person presses the button. This
        # way they have time to choose a window and sources before something random pours into the
        # feed.
        self.running = autostart
        # Whether the hardware is up: microphone, screen, models. `argus run` calls `start()` before
        # opening the port, so the server sees this flag already correct from the first request.
        # `argus settings` does not call `start()` at all: this is a deliberate mode without a
        # dashboard (three copies share the sound card, and the full pipeline cannot be brought up
        # three times), and the flag stays false forever. The settings page uses it to decide whether
        # there is a dashboard worth returning the person to, or there is simply no dashboard.
        self.started = False
        self.sources = {"mic": True, "loopback": True, "screen": with_screen}
        self._tasks: list[asyncio.Task] = []
        self._subs: list = []
        # Short tasks "wait for the end of a streamed answer and record its latency".
        self._stage_tasks: set[asyncio.Task] = set()
        # When (monotonic seconds) the assistant loop died on its own — see `_assistant_ended`.
        self._assistant_deaths: list[float] = []
        self.on_event: Callable[[str, object], None] | None = None

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.bus.bind(loop)
        self.state.started_at = loop.time()

        self.mic, self.loopback = self.audio.open()

        # Recognition sessions — a network handshake, seconds on a good connection and up to fifteen
        # on a bad one — are brought up in the background and the startup does not wait for them: the
        # window and the page must not depend on whether the cloud answered, and without a network or
        # without a key the app must open, not refuse. An error does not bring down the startup but
        # goes into the page's complaints (`SpeechPipeline.warm_error`).
        self.speech.warm_in_background()

        # The OCR and model-session warmups do not know about each other and used to wait their turns
        # one after another, although each goes off into its own thread or onto the network anyway.
        # Run together, they take no longer than the slowest one, not the sum.
        warmups = []
        if self.vision is not None:
            warmups.append(self.vision.warmup())
        if self.assistant is not None:
            warmups.append(self.assistant.start())
        # `return_exceptions=True`, not a bare `gather`: it would raise the first exception without
        # waiting for the others — and if by then `assistant.start()` had managed to bring up a live
        # session at the provider (one provider starts a subprocess), there would be nobody left to
        # close it: `start()` fails before `self._wire()`, and without that `stop()` from inside the
        # calling code cannot reach `self.assistant`.
        results = await asyncio.gather(*warmups, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            if self.assistant is not None:
                await self.assistant.close()
            raise errors[0]

        if self.screen is not None:
            self.screen.open()

        self._loop = asyncio.get_running_loop()
        self._wire()
        # We apply the control panel state after assembly: before it there is nothing to stop.
        self.set_running(self.running)
        self.started = True

    def _wire(self) -> None:
        bus = self.bus
        audio_sub = bus.subscribe(AudioChunk, maxsize=256, name="app-audio")
        speech_sub = bus.subscribe(SpeechEvent, maxsize=64, name="app-speech")
        screen_sub = bus.subscribe(ScreenEvent, maxsize=16, name="app-screen")
        answer_sub = bus.subscribe(Answer, maxsize=16, name="app-answer")
        frame_sub = bus.subscribe(Frame, maxsize=3, name="app-frames") if self.screen else None
        self._subs = [s for s in (audio_sub, speech_sub, screen_sub, answer_sub, frame_sub) if s]

        self._tasks = [
            asyncio.create_task(self._pump_audio(audio_sub)),
            asyncio.create_task(self._pump_speech(speech_sub)),
            asyncio.create_task(self._pump_screen(screen_sub)),
            asyncio.create_task(self._pump_answers(answer_sub)),
            asyncio.create_task(self.speech.run()),
        ]
        if frame_sub is not None:
            self._tasks.append(asyncio.create_task(self._pump_frames(frame_sub)))
        if self.vision is not None:
            self._tasks.append(asyncio.create_task(self.vision.run()))
        if self.assistant is not None:
            self._start_assistant()

    # How many times per minute the assistant loop may die on its own before it stops being
    # brought up again: a failure that repeats from the very first utterance would otherwise spin
    # the loop idle.
    ASSISTANT_DEATHS_PER_MIN = 5
    ASSISTANT_RESTART_DELAY_S = 1.0

    def _start_assistant(self) -> None:
        """The assistant loop is supervised: if it dies on its own, there is a line in the log and it
        is brought up again.

        While the assistant exists, it is the one that puts utterances into the feed
        (`_pump_speech`), and a dead `run()` without a single line in the log would freeze the feed:
        hotkey answers would come from a stale context, and voice questions would not reach the
        model at all. A cancellation at app shutdown is not counted as a death.
        """
        task = asyncio.create_task(self.assistant.run())
        self._tasks.append(task)
        task.add_done_callback(self._assistant_ended)

    def _assistant_ended(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return                      # subscriptions are closed together with the bus — the usual end
        log.error("assistant loop died - restarting", exc_info=error)
        now = time.monotonic()
        self._assistant_deaths = [t for t in self._assistant_deaths if now - t < 60.0] + [now]
        if len(self._assistant_deaths) > self.ASSISTANT_DEATHS_PER_MIN:
            log.error("assistant loop dies too often - not restarting it any more")
            return
        try:
            asyncio.get_running_loop().call_later(
                self.ASSISTANT_RESTART_DELAY_S, self._restart_assistant)
        except RuntimeError:            # the loop is gone — they are shutting down
            return

    def _restart_assistant(self) -> None:
        # After `stop()` the task list is empty — then there is nothing to bring up and nobody to do
        # it. And while `stop()` is still running, the other tasks are already cancelled: a new one
        # would stay alive after the stop.
        if not self._tasks or self.assistant is None:
            return
        if any(t.cancelled() or t.cancelling() for t in self._tasks):
            return
        self._start_assistant()

    # How long to wait for unfinished utterances to reach text before stopping. The final read
    # goes over the network, one channel at a time, and when the cloud is silent each channel waits
    # its fifteen seconds (measured: the stop did not finish at all, and the process was killed at
    # the twentieth second).
    DRAIN_BUDGET_S = 2.0

    async def stop(self) -> None:
        if self.speech is not None:
            try:
                await asyncio.wait_for(self.speech.drain(), timeout=self.DRAIN_BUDGET_S)
            except TimeoutError:
                log.warning(
                    "unfinished utterances did not get their text within %.0f s - stopping without them",
                    self.DRAIN_BUDGET_S,
                )
        for task in (*self._tasks, *self._stage_tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, *self._stage_tasks, return_exceptions=True)
        self._tasks.clear()
        self._stage_tasks.clear()

        # We close the recording before anything that may get stuck (network, audio, windows): without
        # this a session closed by the app shutting down would have no meta.json — and would become
        # unreadable. By the same path as on the button.
        self._close_session()

        if self.screen is not None:
            self.screen.close()
        self.audio.close()
        self.speech.close()
        if self.vision is not None:
            self.vision.close()
        if self.assistant is not None:
            await self.assistant.close()

    # -- event streams -----------------------------------------------------

    async def _pump_audio(self, sub) -> None:
        import numpy as np

        async for chunk in sub:
            x = chunk.samples.astype(np.float32) / 32768.0
            level = self.state.levels[chunk.source]
            level.rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            level.peak = max(level.peak * 0.9, level.rms)
            self._feed_recorder("feed_audio", chunk)

    def _feed_recorder(self, method: str, item) -> None:
        """Hand over a piece of the recording. The recording must not bring down the pump: a failure
        goes to `_recording_failed`.

        It takes the recording lock without waiting: if the control panel's worker thread is closing
        the recording at that moment, the piece is skipped (the recording ends anyway), and the
        event loop does not wait for the closing.
        """
        rec = self.recorder
        if rec is None or not self._rec_lock.acquire(blocking=False):
            return
        failure: Exception | None = None
        try:
            if rec is self.recorder:
                getattr(rec, method)(item)
        except Exception as e:  # noqa: BLE001 — the recording must not bring down the audio and frame pump
            failure = e
        finally:
            self._rec_lock.release()
        if failure is not None:
            self._recording_failed(failure)

    async def _pump_frames(self, sub) -> None:
        async for frame in sub:
            self.state.frames += 1
            self._last_image = frame.image
            # Thinning out is the business of `Recorder` itself: it must work the same here and in
            # `argus record`.
            if self.record_frames:
                self._feed_recorder("feed_frame", frame)

    async def _pump_speech(self, sub) -> None:
        async for event in sub:
            if event.t_end <= self.conversation_since:
                # The utterance ended before this chat was opened, and reached us after — recognition is not
                # instant. It belongs to another conversation and does not go into the new feed.
                continue
            if event.error:
                self.state.speech.append(event)
                self._notify("speech", event)
                continue
            if event.engine:
                # Shadow engine: only to show alongside. It deliberately does not go into the feed —
                # otherwise every utterance would get into the prompt twice, and the engine comparison would
                # change what the model sees.
                self.state.speech_b.append(event)
                self.state.stages["speech-b"].add(
                    event.latency_ms
                    if event.latency_ms is not None
                    else (self.clock.now() - event.t_end) / 1e6
                )
                self._notify("speech", event)
                continue

            self.state.speech.append(event)
            # From the end of the phrase to the appearance of text — that is what the whole pipeline was
            # built this way for.
            self.state.stages["speech"].add((self.clock.now() - event.t_end) / 1e6)
            entry = self.timeline.add_speech(event) if self.assistant is None else None
            if entry is not None:
                self._store_entries([entry])
            self._notify("speech", event)

    async def _pump_screen(self, sub) -> None:
        async for event in sub:
            ordered = sorted(event.blocks, key=lambda b: (b.box[1], b.box[0]))
            self.state.screen_lines = [b.text for b in ordered]
            self.state.screen_t = event.t / 1e9
            self.state.screen_window = event.window
            self.state.stages["screen"].add(self.vision.avg_ms if self.vision else 0.0)
            if self.assistant is None:
                self._store_entries(self.timeline.add_screen(event))
            self._notify("screen", event)

    def _store_entries(self, entries: list) -> None:
        """Append the timeline to disk if one is being kept. At that moment the database may be being
        closed by the console's worker thread (recording was switched off): then a write to a closed
        database is not a failure of the pump."""
        store = self.store
        if store is None:
            return
        try:
            store.add_many(entries)
            store.commit()
        except Exception:  # noqa: BLE001 — the on-disk feed must not bring down the pump
            log.warning("the feed was not fully written to disk: the database is closed or unavailable", exc_info=True)

    async def _pump_answers(self, sub) -> None:
        async for answer in sub:
            if (
                self.assistant is not None
                and answer.meta.get("epoch", self.assistant.epoch) != self.assistant.epoch
            ):
                # An answer from a conversation that has already ended (another chat was opened while it was
                # making its way here over the bus) does not go into the new feed.
                continue
            self.state.answers.append(answer)
            self._track_brain_latency(answer)
            self._notify("answer", answer)

    # How often to check whether a streamed answer has been read to the end (for measuring the
    # "model" stage).
    STAGE_POLL_S = 0.25

    def _track_brain_latency(self, answer: Answer) -> None:
        """The model latency goes into the stage measurement once it is known.

        A streamed answer is published empty before the first token, and `latency_ms` is set after
        the read is finished (`Assistant._ask`): if read at the moment of publication, it would give
        0 ms on every streamed answer and hide exactly the slowness the stage was created for.
        """
        if answer.done:
            self.state.stages["brain"].add(answer.latency_ms)
            return
        task = asyncio.create_task(self._brain_latency_when_done(answer))
        self._stage_tasks.add(task)
        task.add_done_callback(self._stage_tasks.discard)

    async def _brain_latency_when_done(self, answer: Answer) -> None:
        deadline = time.monotonic() + 600
        while not answer.done and time.monotonic() < deadline:
            await asyncio.sleep(self.STAGE_POLL_S)
        if answer.done:
            self.state.stages["brain"].add(answer.latency_ms)

    def _notify(self, kind: str, payload: object) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:
            log.exception("event handler crashed")

    # -- control panel --------------------------------------------------------------

    def set_running(self, on: bool) -> None:
        """The general recognition switch. The recording lives together with it.

        Before, the recording began at app startup, and it included all the time while the person
        chooses a window and sources. In a measured run these were extra minutes of silence at the
        start, which then had to be found and cut off by hand.

        Now the recording is exactly what was checked: it began on the button, it ended on the
        button. Every press gives its own session instead of appending to the previous one: the
        pause between presses would otherwise lie in the file as silence, and an hour of silence
        weighs the same 220 MB as an hour of conversation.

        It is called both from the event loop and from a worker thread: `/control` sends it here
        through `to_thread`, because cleaning old recordings (walking the sessions folder) and
        closing a recording (up to ten seconds to flush frames) on the loop would stop all windows
        and answers. That is why the whole body is under the recording lock: two clicks in a row
        will not open two recordings and will not leave the flags and the recording in different
        states.
        """
        with self._rec_lock:
            self.running = on
            self.speech.paused = not on
            if self.vision is not None:
                self.vision.paused = not on

            if not self._record:
                return
            if on and self.recorder is None:
                try:
                    self._open_session()
                except Exception as e:  # noqa: BLE001 — the recording must not prevent turning recognition on
                    # Disk full or folder unavailable: recognition turns on without a recording — except when a
                    # recording was explicitly requested (`argus run --record`).
                    self._recording_failed(e, opening=True)
                    if self.record_required:
                        self.running = False
                        self.speech.paused = True
                        if self.vision is not None:
                            self.vision.paused = True
                        log.error("recognition not enabled: the requested recording did not open")
            elif not on and self.recorder is not None:
                self._close_session()

    def _recording_failed(self, error: BaseException, *, opening: bool = False) -> None:
        """The recording is not being written (disk full, file closed) — we stop it, not the pipeline.

        Before, an exception from `Recorder` killed the audio task: the levels froze, the recording
        went deaf, and there was not a word in the log or on the page. Now there is a line in the
        log, the recording is closed as best it can be, the reason stays in the page's complaints
        (`problems`), and recognition and answers keep working. A new recording will begin the next
        time recognition is turned on.
        """
        log.error(
            "recording %s: %s: %s", "did not start" if opening else "stopped",
            type(error).__name__, error, exc_info=error,
        )
        self.record_error = friendly.plain_or(
            error,
            "didn't start — check the disk space" if opening
            else "stopped — check the disk space",
        )
        self._close_session()

    def _open_session(self) -> None:
        self.record_error = ""
        # We clean the old files before the start, not after: the room is needed now, and "after" may
        # never come — sessions get closed any which way.
        self.pruned = prune_sessions(self.cfg.sessions_dir, self.cfg.record.keep_gb)

        # The file's zero is the moment of the press, not the app launch. Otherwise the minutes spent
        # choosing a window would lie at the start of the track as silence.
        started = datetime.now(UTC)
        self.recording_started_at = self.clock.now()

        # The session name distinguishes time only to the second, and the button can be clicked twice
        # in a second — and the second recording would lie over the first, destroying it.
        self.session_dir = _free_dir(self.cfg.sessions_dir / session_id(started))
        self.recorder = Recorder(
            self.session_dir, self.cfg.record, started, t0_ns=self.recording_started_at
        )
        # The on-disk feed only when the pipeline itself maintains it. With an assistant, it puts the
        # utterances and the screen into the feed (`_pump_speech`, `_pump_screen`), and the database
        # would remain an empty file that nobody reads in every session.
        if self.assistant is None:
            self.store = TimelineStore(self.session_dir / "timeline.db")
        self._signal_recording_began()
        self._notify("recording", {"dir": str(self.session_dir), "on": True})

    def _signal_recording_began(self) -> None:
        """Set `recording_began` from any thread: `asyncio.Event` is not thread-safe, and `set()` from a
        worker thread would not wake up whoever is waiting on the loop (`runner._stop_after`)."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self.recording_began.set)
                return
        self.recording_began.set()

    def _close_session(self) -> None:
        from argus.session.layout import DeviceMeta

        def meta(d: DeviceInfo | None):
            return DeviceMeta(name=d.name, rate=d.rate, channels=d.channels) if d else None

        # Closing must not break on the disk: `stop()` calls it before closing audio, screen,
        # recognition and the assistant, and an exception from here would leave them open (for one
        # provider that is a live subprocess). And a dead `Recorder` left in `self.recorder` would not
        # let the next turn-on open a new session — hence the reset in `finally`.
        #
        # Under the recording lock: the audio and frame pumps skip pieces meanwhile (`_feed_recorder`),
        # and `Recorder` itself cannot write and close from two threads at once.
        with self._rec_lock:
            try:
                if self.recorder is not None:
                    self.recorder.close(mic=meta(self.mic), loopback=meta(self.loopback))
            except Exception:  # disk full, file busy
                log.exception("recording closed with an error")
            finally:
                self.recorder = None
            store, self.store = self.store, None     # detach first: the pump will no longer pick up the one being closed
            try:
                if store is not None:
                    store.close()
            except Exception:
                log.exception("feed database did not close")
        self._notify(
            "recording", {"dir": str(self.session_dir) if self.session_dir else "", "on": False}
        )

    async def reset_conversation(self, feed: int = 20) -> None:
        """A clean slate: a different conversation begins — a different chat.

        Everything that belongs to the past is forgotten: the answers and utterances on the page,
        the frames, the feed the prompt is built from, and the assistant's memory. What is visible
        on screen right now is not touched — that is not about the conversation but about the
        moment.

        `feed` is how many answers the page keeps. The usual feed is twenty; a chat with history has
        more (`argus/chats/`): otherwise the very first new answers would push out what was restored
        from last time.
        """
        self.conversation_since = self.clock.now()
        state = self.state
        state.answers = deque(maxlen=feed)
        state.speech.clear()
        state.speech_b.clear()
        state.past_speech = []
        state.shots.clear()

        timeline = self.timeline
        timeline.entries.clear()
        timeline.dropped = 0
        timeline._window = None
        timeline.digest.reset()

        if self.assistant is not None:
            await self.assistant.reset()

    def set_source(self, name: str, on: bool) -> None:
        """Turn a single source on or off."""
        if name not in self.sources:
            raise KeyError(name)
        self.sources[name] = on
        if name == "mic":
            self.speech.set_enabled(Source.ME, on)
        elif name == "loopback":
            self.speech.set_enabled(Source.THEM, on)
        elif name == "screen":
            # "Screen" is a single concept for the person: the model sees the screen. Inside it there are
            # two different things, and both turn on together: the picture goes to the model at the moment
            # of the request, and text recognition constantly writes into the feed — so that it has a
            # memory of what was on the screen a minute ago. There is no reason to separate them in the
            # interface: the user needs one switch, not two.
            self.cfg.brain.send_screenshot = on
            if self.vision is not None:
                self.vision.enabled = on

    def set_target_window(self, title: str) -> None:
        """Pin a window by a piece of its title. An empty string — follow the active one.

        When the target changes, we reset the filtering and the accumulated screen state: otherwise
        the first frame of the new window would be compared with the fingerprint of the previous one
        and might not pass, and the difference between different windows would land in the feed.
        """
        self.cfg.screen.target_window = title.strip()
        if self.vision is not None:
            self.vision.gate = FrameGate(GateSettings(**self.cfg.gate.model_dump()))
        self.timeline.digest.reset()

    def preview_image(self):
        """A picture of what goes to recognition right now.

        Exactly the same area that the pipeline reads: showing the model one thing and reading
        another would mean getting answers about different things.
        """
        from argus.capture.window import capture_window_image, find_window

        target = self.cfg.screen.target_window
        if not target:
            return self._last_image

        win = find_window(target, tuple(self.cfg.screen.skip_windows))
        if win is None:
            return None
        # The same live stream that the pipeline reads.
        if self.vision is not None:
            own = self.vision._pinned.frame_for(win.hwnd, win.title)
            if own is not None:
                return own
        shot = capture_window_image(win)
        if shot is not None:
            return shot
        # Nothing was captured — a crop from the screen is better than an empty space.
        return win.crop(self._last_image) if self._last_image is not None else None

    def screenshot_jpeg(self) -> bytes | None:
        """A frame of the reading area as JPEG — what will go to the model together with the text.

        We take exactly the same area as recognition: showing the model one thing and reading
        another would mean getting answers about different things.
        """
        import cv2

        frame = self.preview_image()
        if frame is None:
            return None
        width = self.cfg.brain.screenshot_width
        if frame.shape[1] > width:
            scale = width / frame.shape[1]
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.brain.screenshot_quality]
        )
        if not ok:
            return None

        # We remember it here, not where the model is asked, and this is not by chance: **every**
        # frame going to the model passes through this function — on the button, on the interlocutor's
        # question, and on the timer. Recording in one place means the feed shows everything the model
        # has seen, not only what was shown by hand.
        jpeg = buf.tobytes()
        self.state.shots.append(Shot(t=self.clock.now(), jpeg=jpeg))
        return jpeg

    async def ask_about_screen(self, question: str | None = None, deep: bool = False) -> Answer:
        """Ask about what is on the screen right now.

        For the time of the question the picture is sent even if constant sending is off. It is
        turned off because of limits: a frame costs about fifteen hundred tokens against a hundred
        for text, and there is no point paying that on every utterance. But here the person pressed
        a button precisely about the screen — the request to look is the whole point of the press,
        and answering it without a picture would mean doing something other than what was asked.

        The shared config is not touched: the ban is lifted by `force_image` itself
        (`Assistant._ask`). Before, the flag was temporarily turned on and restored in `finally`, and
        if during the question the person pressed "Screen off", the rollback overwrote
        their choice: the page showed "off", while the next questions kept going out with a
        screenshot.
        """
        return await self.ask(question, deep, force_image=True)

    def windows(self, thumbs: bool = False, thumb_width: int = 280) -> list[dict]:
        """Windows that can be pinned — with thumbnails on request.

        Thumbnails are taken through `PrintWindow`, not cut out of a screenshot: an overlapped
        window would otherwise show what lies on top of it, and all the thumbnails would come out
        the same. Measured: 40-50 ms per window, so we take them only when the list has actually
        been opened.

        Empty shells are filtered out at the same time. Service overlays like "XBOX" are visible,
        full-screen and have a title — you cannot tell them apart by name, but their snapshot is
        solid black.
        """
        import base64

        import cv2

        from argus.capture.window import capture_window_image, has_content, list_windows

        out: list[dict] = []

        # First comes the card of the whole screen, with a real thumbnail: a stub among live pictures
        # looked foreign.
        whole = {"title": "", "label": "Whole screen", "width": 0, "height": 0, "thumb": None}
        if thumbs and self._last_image is not None:
            img = self._last_image
            whole["width"], whole["height"] = img.shape[1], img.shape[0]
            scale = min(1.0, thumb_width / max(img.shape[1], 1))
            small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 62])
            if ok:
                whole["thumb"] = base64.b64encode(buf.tobytes()).decode("ascii")
        out.append(whole)

        for w in list_windows(tuple(self.cfg.screen.skip_windows)):
            item = {"title": w.title, "label": w.title,
                    "width": w.width, "height": w.height, "thumb": None}
            if not thumbs:
                out.append(item)
                continue

            image = capture_window_image(w)
            if not has_content(image):
                continue  # an empty service overlay, nothing to choose
            scale = min(1.0, thumb_width / max(image.shape[1], 1))
            small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 62])
            if ok:
                item["thumb"] = base64.b64encode(buf.tobytes()).decode("ascii")
            out.append(item)
        return out

    def controls(self) -> dict:
        return {
            "running": self.running,
            "sources": dict(self.sources),
            "target_window": self.cfg.screen.target_window,
            # The windows whose sound we count as "system sound". Empty means the whole device: this is
            # how the panel tells the two modes apart.
            "loopback_windows": list(self.cfg.audio.loopback_windows),
            "send_screenshot": self.cfg.brain.send_screenshot,
            "current_window": self.vision.last_window if self.vision else None,
            "target_missing": self.vision.target_missing if self.vision else 0,
        }

    # -- outward -------------------------------------------------------------

    async def ask(
        self,
        question: str | None = None,
        deep: bool = False,
        *,
        force_image: bool = False,
        image: bytes | None = None,
    ) -> Answer:
        """`image` is a photo the person pasted from the clipboard (JPEG). It goes to the model instead
        of the screenshot and is put into the frame memory with the mark `paste`: this way it is
        visible in the feed and can be opened from the answer card.
        """
        if self.assistant is None:
            return Answer(text="", error="the assistant is turned off")
        if question and image is None and not force_image:
            # "Recap", "watch the price" — a request to the assistant, not a
            # question to the model (`argus/recap/`). A question with a photo or about the screen is never
            # a command.
            from argus.recap import intercept

            handled = await intercept(self, question)
            if handled is not None:
                return handled
        if image is not None:
            self.state.shots.append(Shot(t=self.clock.now(), jpeg=image, kind="paste"))
        return await self.assistant.ask_now(question, deep, force_image=force_image, image=image)

    @property
    def healthy(self) -> bool:
        if not self.audio.healthy:
            return False
        return not (self.screen is not None and not self.screen.alive)

    def problems(self) -> list[str]:
        out = []
        if not self.audio.healthy:
            out.append(f"audio: {friendly.plain_or(self.audio.first_error(), 'stream stopped')}")
        # A live stream and the audio going over it are different things, and the second breaks more
        # often. Without these complaints a failure looks like this: levels at zero, no utterances,
        # "pipeline alive" in the footer. There is nowhere to look for the cause in
        # such a picture.
        out.extend(self.audio.complaints())
        if self.screen is not None and not self.screen.alive:
            out.append(f"screen: {friendly.plain_or(self.screen.error, 'stream stopped')}")
        if self.record_error:
            out.append(f"recording: {self.record_error}")
        # The recognition session is brought up in the background at startup and may fail to come up
        # (no key, no network): the startup does not fail because of it, but the person must know why
        # there will be no utterances.
        out.extend(self.speech.complaints())
        return out

    def reopen_audio(self) -> dict:
        """Reconnect the audio without bringing down everything else.

        The standard repair for the channel's main failure: the headset fell asleep, Windows moved
        the output, and we are listening to a dead device. Restarting the program cures this too,
        but it costs minutes — Whisper is loaded again and the conversation feed is lost.
        """
        mic, loopback = self.audio.reopen()
        self.mic, self.loopback = mic, loopback
        log.info("audio reconnected: '%s' and '%s'", mic.name, loopback.name)
        self._notify("audio", {"mic": mic.name, "loopback": loopback.name})
        return {"mic": mic.name, "loopback": loopback.name}

    def set_loopback_windows(self, titles: list[str]) -> dict:
        """Pin the windows whose sound counts as "system sound". Empty — the whole device.

        Several windows at once mean several processes, each with its own capture thread (see
        `capture/audio.py: _AppChannel`), mixed into one "system sound" stream the same way several
        speakers in Discord are already mixed.

        We reopen the audio at once: a setting that would take effect sometime later is useless in a
        conversation. Reopening is cheap — only the capture is touched, and neither Whisper nor the
        feed suffers.
        """
        seen: list[str] = []
        for raw in titles:
            title = raw.strip()
            if title and title not in seen:
                seen.append(title)
        self.cfg.audio.loopback_windows = seen
        return self.reopen_audio()

    def set_mic_device(self, spec: str | None) -> dict:
        """Pin the microphone by name and reconnect the audio at once.

        By name, not by number: numbers float in Windows — plug in a headset, and the microphone
        moves from 15 to 20. A program with a recorded number would not crash but would silently
        listen to someone else's device.

        An empty string gives the choice back to the system. The difference is not cosmetic: a
        pinned device stops being counted as a failure when the system has switched to another one —
        since the person chose it themselves, that is their decision, and there is nothing to
        complain about.
        """
        self.cfg.audio.mic_device = spec or None
        self._remember("audio.mic_device", self.cfg.audio.mic_device)
        return self.reopen_audio()

    def _remember(self, path: str, value: object) -> None:
        """Write the choice next to the config so that it survives a restart.

        A device choice that is forgotten at the next launch is half a solution: the person fixes
        the audio, closes the laptop, and the next day fixes it again and does not understand why.

        We stay silent when there is no config at all: tests build `Config()` that way, and they
        must not write a file into the working copy.
        """
        if self.cfg.config_path is None:
            return
        from argus.core.settings_store import store_of

        # The app's shared store, not a fresh one: the settings page and the chats have the same one,
        # and a write that bypassed it left them with a stale `overrides` — the very first edit of any
        # field erased the chosen microphone from the file.
        try:
            store_of(self).apply({path: value})
        except Exception:
            # It did not reach the file — but it is already set in the live config, and the audio will
            # reconnect. There is no reason to lose the session over a failed write.
            log.exception("could not remember %s", path)

    def timeline_entries(self) -> list[Entry]:
        return list(self.timeline)
