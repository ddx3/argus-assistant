"""Speech pipeline: audio stream -> utterances -> text.

It brings together the speech detector and recognition, and solves one non-obvious
problem: **Whisper blocks the thread for hundreds of milliseconds**. Measured on this
machine, it takes 230 ms for a four-second phrase. Calling it directly in the event loop
would freeze the whole pipeline for that time: capture would stall, frames would pile up,
and the dashboard would freeze.

So recognition goes to separate threads, and how many depends on the engine. Local
Whisper gets **exactly one** for both channels: the video card computes one job at a time
anyway, while memory use would double, and with 8 GB that is a direct path to running out
of memory for no reason. A network engine gets one thread per channel: it occupies
nothing on this machine and waits for an answer instead of computing, so a queue shared
by two channels would only mean that the other party's utterance waits until a long reply
from the user is finished.

The speech detector, on the contrary, lives right in the loop: it costs microseconds per
window (227 times faster than real time), and there is no reason to move it elsewhere.

The loop only cuts the audio into utterances and puts them in the queue of its channel;
a separate consumer per channel waits for transcription. Otherwise one slow utterance
(a fifteen-second timeout) would stop audio reading on both channels, and the
one-thread-per-channel pools would not work in parallel as intended above.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from argus.core import friendly
from argus.core.events import AudioChunk, Source, SpeechEvent, Utterance
from argus.perception.stt.base import STTProvider
from argus.perception.vad import SileroVAD, UtteranceSegmenter, VADSettings, ensure_model

if TYPE_CHECKING:
    from collections.abc import Callable

    from argus.account.client import Account
    from argus.account.meter import Meter
    from argus.core.bus import Bus
    from argus.core.config import Config

log = logging.getLogger(__name__)


def build_stt(cfg: Config, provider: str | None = None, account: Account | None = None) -> STTProvider:
    """Build a recognizer by name; with no name, the primary one from the config.

    The choice point has been needed from the start: cloud providers give lower latency for
    money, and they can be compared only on the same recording. The `provider` parameter
    takes this idea to its end: now two engines can be brought up at once and fed the same
    audio.

    `account` is the link to the account server. If it exists and `realtime_stt.relay` is
    on, the audio goes through the server (there is no key on this computer). Otherwise, as
    before, it goes straight to the cloud.
    """
    name = provider or cfg.stt.provider
    if name == "whisper-local":
        # Removed from the product: one and a half gigabytes of weights at install time and as much
        # video memory at run time. We catch the name separately so that an old config gets a clear
        # answer instead of "unknown provider".
        raise ValueError(
            "whisper-local has been removed: the product has no local recognition. "
            "Set stt.provider = \"realtime-openai\""
        )
    if name == "realtime-openai":
        from argus.perception.stt.realtime_openai import RealtimeOpenAI

        return RealtimeOpenAI(cfg.realtime_stt, account=account if cfg.realtime_stt.relay else None)
    if name == "gpt-live-transcribe":
        from argus.perception.stt.gpt_live_transcribe import GPTLiveTranscribe

        return GPTLiveTranscribe(cfg.gpt_live_transcribe)
    if name == "stub":
        from argus.perception.stt.stub import StubSTT

        return StubSTT(cfg.stub_stt)
    raise ValueError(f"unknown recognition provider: {name}")


def build_engines(
    cfg: Config, provider: str | None = None, account: Account | None = None
) -> dict[Source, STTProvider]:
    """Build a recognizer for each channel.

    Whisper is one for both. It holds one and a half gigabytes of video memory and computes
    one job at a time anyway: a second instance would double the usage and speed up nothing.

    Realtime, on the contrary, must have its own per channel, for two reasons. First, all its
    value is that the open session accumulates the context of the conversation. One session
    for two channels would mix both sides in that context and bring back exactly the problem
    that separate streams solve for free. Second, a session serves utterances strictly one
    at a time. A shared one would mean that a long reply from the user delays the other
    party's question, which is exactly the utterance the whole thing exists for.
    """
    first = build_stt(cfg, provider, account)
    if not getattr(first, "per_channel", False):
        return {Source.ME: first, Source.THEM: first}
    return {Source.ME: first, Source.THEM: build_stt(cfg, provider, account)}


def build_shadow(cfg: Config) -> STTProvider | None:
    """Build the second engine for comparison, if the config defines it.

    Returns `None` when the comparison is off. This is the normal state, not an error.
    """
    name = cfg.stt.compare_with
    if not name:
        return None
    if name == cfg.stt.provider:
        raise ValueError(
            f"comparing {name} with itself is pointless: "
            f"stt.compare_with must differ from stt.provider"
        )
    return build_stt(cfg, name)


class BadWavError(ValueError):
    """The WAV is in the wrong format or unreadable; the text is written for a person."""


class SpeechPipeline:
    """Audio -> text, one pipeline per channel."""

    # How many utterances of one channel may wait in the transcription queue. If the engine
    # gets stuck, the queue must not grow without limit. On overflow an old utterance is dropped
    # in favor of a fresh one (so that a returning connection does not dump a pile of things
    # said long ago, which would also have to be paid for).
    QUEUE_LIMIT = 8

    # How many utterances in a row must be lost through the engine's fault (connection,
    # timeout, dead session) before the page says that there is no recognition. One loss is an
    # accident, not a reason.
    LOST_BEFORE_COMPLAINT = 2
    _lost_in_a_row = 0

    def __init__(
        self,
        bus: Bus,
        cfg: Config,
        stt: STTProvider | None = None,
        shadow: STTProvider | None = None,
        meter: Meter | None = None,
        on_broke: Callable[[], None] | None = None,
        account: Account | None = None,
    ) -> None:
        self.bus = bus
        self.cfg = cfg
        # Usage metering: the audio of utterances sent to cloud recognition is paid
        # (`account/meter.py`). Without a meter, it is a build on its own keys.
        self.meter = meter
        # Called on the event loop when the server said there is no money:
        # the control panel turns recognition off instead of silently going deaf.
        self.on_broke = on_broke
        # A recognizer per channel; see build_engines for why it is done this way.
        # An engine passed in by hand is set for both: tests and replay compare engines with each
        # other, and there is no reason to separate the channels there too.
        self.engines: dict[Source, STTProvider] = (
            {Source.ME: stt, Source.THEM: stt} if stt is not None else build_engines(cfg, account=account)
        )

        # A second engine on the same audio. Its text reaches the dashboard and nowhere else: not
        # the timeline, not the prompt. Otherwise turning on the comparison would change what the
        # model sees, and two things would be measured at once.
        self.shadow = shadow if shadow is not None else build_shadow(cfg)

        model = ensure_model(cfg.models_dir)
        settings = VADSettings(**cfg.vad.model_dump())
        # Its own model instance per channel: the model is recurrent and remembers state, and a
        # shared detector would react to speech from the other channel.
        self.segmenters = {
            src: UtteranceSegmenter(SileroVAD(model), src, settings)
            for src in (Source.ME, Source.THEM)
        }

        # A pool per channel, but only when the engines are different. A shared instance (this is
        # always local Whisper) must not be called from two threads: there is one model, and
        # parallel calls to it do not speed it up but break it.
        if self.engines[Source.ME] is self.engines[Source.THEM]:
            shared = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
            self._pools = {Source.ME: shared, Source.THEM: shared}
        else:
            self._pools = {
                src: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"stt-{src.value}")
                for src in (Source.ME, Source.THEM)
            }

        # A separate thread for the shadow engine, and this is not a detail. In a shared thread it
        # would queue up behind Whisper and add Whisper's delay to its own, so the speed
        # measurement would show the sum of two engines instead of each one separately.
        self._shadow_pool = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-shadow")
            if self.shadow is not None else None
        )
        self._shadow_tasks: set[asyncio.Task] = set()

        self.utterances = 0
        self.transcribed = 0
        self.dropped = 0
        self.rescued = 0        # utterances picked up by the backup engine

        # Why recognition did not come up at startup (`warm_in_background`).
        # Empty means everything is fine or it is still coming up. It is cleared by the first
        # recognized utterance: once one has got through, there is nothing left to complain about.
        self.warm_error = ""
        self._warm_task: asyncio.Task | None = None

        # Queues of utterances waiting for transcription (per channel) while `run` is active; see
        # the module docstring.
        self._queues: dict[Source, asyncio.Queue[Utterance]] = {}

        # How many utterances in a row were lost through the engine's fault: reset to zero by the
        # first successful one.
        self._lost_in_a_row = 0

        # Which channels to listen to right now. It changes on the fly from the interface: you
        # often want to turn off the microphone and leave only the other party.
        self.enabled: set[Source] = {Source.ME, Source.THEM}
        self.paused = False

    @property
    def stt(self) -> STTProvider:
        """The primary recognizer.

        There are two channels, but from the outside (in the dashboard, in replay, in statistics)
        only one matters: they have the same engine, and only the sessions differ.
        """
        return self.engines[Source.THEM]

    def _unique_engines(self) -> list[tuple[Source, STTProvider]]:
        """Engines without repeats. Whisper stands on both channels as one instance, and it must not be
        warmed up or closed twice."""
        out: list[tuple[Source, STTProvider]] = []
        for src in (Source.ME, Source.THEM):
            engine = self.engines[src]
            if not any(engine is e for _, e in out):
                out.append((src, engine))
        return out

    def reopen_realtime(self, reason: str) -> None:
        """Reconnect the engines that hold a long session (realtime-openai, gpt-live-transcribe).
        This is how settings such as the session language reach a conversation that is already
        running, without restarting the whole app.

        Each engine goes through its own pool, the same one as ordinary transcription: if the
        pool is busy with an utterance right then, the reconnect simply queues up behind it
        instead of colliding with it in the middle of a session.
        """
        loop = asyncio.get_running_loop()
        for src, engine in self._unique_engines():
            reconnect = getattr(engine, "reconnect", None)
            if reconnect is None:
                continue

            def _go(reconnect=reconnect) -> None:
                try:
                    reconnect(reason)
                except Exception:
                    log.exception("could not reconnect speech recognition")

            loop.run_in_executor(self._pools[src], _go)

    async def warmup(self) -> None:
        """Warm up the engines. With separate threads, in parallel rather than one by one: ME and
        THEM have different pools exactly so that they can be loaded at once (a shared pool
        always means the same local Whisper, see `__init__`, and there is nothing to parallelize
        there; `_unique_engines` returns it only once).
        """
        loop = asyncio.get_running_loop()

        # The shadow engine starts in the same salvo. It has its own pool, and it has no reason to
        # wait in line after the primary ones.
        shadow_task = (
            loop.run_in_executor(self._shadow_pool, self.shadow.warmup)
            if self.shadow is not None and self._shadow_pool is not None
            else None
        )

        tasks = [
            loop.run_in_executor(self._pools[src], engine.warmup)
            for src, engine in self._unique_engines()
        ]
        try:
            if tasks:
                await asyncio.gather(*tasks)
        finally:
            # In `finally`, not after it. Otherwise a failed primary engine interrupts the warm-up
            # before the already started shadow task is collected, and it is left hanging unclaimed in
            # its pool.
            if shadow_task is not None:
                try:
                    await shadow_task
                except Exception as e:                   # noqa: BLE001
                    # The shadow engine has no right to bring down startup: it stands beside the primary one
                    # for comparison, and the main pipeline must work without it.
                    log.warning("comparison disabled, startup failed for %s: %s", self.shadow.name, e)
                    self.shadow = None

    def warm_in_background(self) -> None:
        """Bring up the recognition sessions without making startup wait for them.

        A session is a network handshake with the cloud: a second or two in good weather and up
        to fifteen in bad. Before, startup waited for all of it: the window and the page appeared
        only afterwards, and without a network or without a key the app did not open at all. The
        user saw "could not start" where it was enough to say
        "recognition is not available yet".

        Now the sessions come up on their own while the user looks at the window. An utterance
        that arrives earlier is not lost: transcription runs in the same thread as the warm-up
        (a one-thread pool per channel, see `__init__`) and simply queues up behind it. A failure
        is not a startup crash: the reason goes to `warm_error` (the page shows it among the
        complaints), and the next utterance will try to bring the session up again itself
        (`RealtimeOpenAI._speak`).
        """

        async def go() -> None:
            try:
                await self.warmup()
                self.warm_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                if getattr(e, "quiet", False):
                    # Not signed in to an account: that is how it should be on a fresh install. The account
                    # hint (`account_hint`) is already on the page, and a complaint would sit on top of it.
                    log.info("recognition is waiting for account sign-in: %s", e)
                    return
                # Details go to the log (below); the user gets one understandable phrase: environment
                # variable names and the texts of network libraries mean nothing to them. The exception is
                # a refusal from our own server (`RelayRefused`): its text is written for people, and a
                # generic phrase "no access" in place of "your balance is empty" would be a
                # lie.
                self.warm_error = friendly.explain(
                    e, trust_text=bool(getattr(e, "for_people", False)))
                log.warning(
                    "recognition did not start at launch: %s — the next utterance will try again", e
                )

        self._warm_task = asyncio.get_running_loop().create_task(go())

    def complaints(self) -> list[str]:
        """What is wrong with recognition besides a failed utterance: for the complaint list on the page."""
        if self.warm_error:
            return [f"recognition: {self.warm_error}"]
        if self._lost_in_a_row >= self.LOST_BEFORE_COMPLAINT:
            # Recognition was working, and now utterances are lost one after another with no
            # human-readable reason from the engine (a dropped connection, a timeout, a dead session).
            # Otherwise the page would show "enabled" while nothing is heard. It is cleared by the first
            # utterance that gets through.
            return [f"recognition: {friendly.NETWORK}"]
        return []

    def set_enabled(self, source: Source, on: bool) -> None:
        """Turn a channel on or off on the fly.

        On turning off, the detector state is reset. Otherwise, after turning on, it would
        finish a phrase left unfinished half an hour ago, and a piece from the past would appear
        in the timeline.
        """
        if on:
            self.enabled.add(source)
        else:
            self.enabled.discard(source)
            self.segmenters[source].reset()

    async def run(self, source_filter: Source | None = None) -> None:
        """Listen to the bus and publish the recognized utterances."""
        if self.cfg.stt.provider == "stub" and self.cfg.stub_stt.dialogue_enabled:
            # A scenario instead of microphone audio: recognition has nothing to do with it,
            # `_run_dialogue` publishes the ready utterances itself.
            await self._run_dialogue()
            return
        loop = asyncio.get_running_loop()
        sub = self.bus.subscribe(AudioChunk, maxsize=512, name="speech")

        # The loop only cuts the audio. Transcription is awaited by its own consumer per channel,
        # in order: the other party's utterance does not wait behind a long reply from the user,
        # and a stuck engine does not stall audio reading (which would overflow the bus queue and
        # erase the audio of both channels).
        queues = {src: asyncio.Queue(maxsize=self.QUEUE_LIMIT) for src in (Source.ME, Source.THEM)}
        self._queues = queues
        consumers = [loop.create_task(self._consume(loop, q)) for q in queues.values()]
        was_paused = False
        try:
            async for chunk in sub:
                if self.paused:
                    if not was_paused:
                        was_paused = True
                        self._cut_open()
                    continue
                was_paused = False
                if chunk.source not in self.enabled:
                    continue
                if source_filter is not None and chunk.source is not source_filter:
                    continue
                for utterance in self.segmenters[chunk.source].feed(chunk):
                    self._enqueue(utterance)
            # The subscription has ended (the bus is closed): finish what is already in the queues.
            await asyncio.gather(*(q.join() for q in queues.values()))
        except asyncio.CancelledError:
            # Finish what was left unfinished: the last phrase of a session is often the most needed.
            for seg in self.segmenters.values():
                tail = seg.flush()
                if tail is not None:
                    await self._handle(loop, tail)
            raise
        finally:
            sub.close()
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            self._queues = {}

    async def _consume(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Utterance]) -> None:
        """Transcribe the utterances of one channel in turn. The failure of one utterance must not stop
        the channel: without this the pipeline would silently die at the first exception."""
        while True:
            utterance = await queue.get()
            try:
                await self._handle(loop, utterance)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("utterance not processed")
            finally:
                queue.task_done()

    def _enqueue(self, utterance: Utterance) -> None:
        """Put an utterance in its channel's queue; when it is full, the oldest is dropped."""
        queue = self._queues[utterance.source]
        if queue.full():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
            log.warning("transcription queue overflowed — the old utterance was dropped")
        queue.put_nowait(utterance)

    def _cut_open(self) -> None:
        """Pause: unfinished utterances go to transcription, and the detectors forget everything
        else.

        The last words before "stop" are real. If left in the detector, they would be glued to
        the sound after "start" and transcribed (and billed) in the new session.
        """
        for seg in self.segmenters.values():
            tail = seg.flush()
            seg.reset()
            if tail is not None:
                self._enqueue(tail)

    async def _run_dialogue(self) -> None:
        """A conversation scenario instead of audio; see `StubSTTConfig.dialogue`.

        The scenario's utterances already carry ready text, so STT (`self.engines`) does not
        take part in this path at all: the pipeline is emulated starting from `Utterance`, not by
        replacing only `transcribe`. A pause (`self.paused`) is honored by the same button as for
        real audio: the scenario must not keep talking while the conversation is stopped by hand.
        """
        turns = self.cfg.stub_stt.dialogue
        if not turns:
            return
        interval = self.cfg.stub_stt.dialogue_interval_s
        silence = np.zeros(1, dtype=np.int16)
        i = 0
        while True:
            if self.paused:
                await asyncio.sleep(interval)
                continue
            turn = turns[i % len(turns)]
            source = Source(turn.source)
            t = time.monotonic_ns()
            t_end = t + int(interval * 1_000_000_000)
            self.utterances += 1
            self.bus.publish(Utterance(t=t, t_end=t_end, source=source, samples=silence))
            self.transcribed += 1
            self.bus.publish(SpeechEvent(t=t, t_end=t_end, source=source, text=turn.text))
            i += 1
            await asyncio.sleep(interval)

    async def _handle(self, loop: asyncio.AbstractEventLoop, utterance: Utterance) -> None:
        self.utterances += 1
        self.bus.publish(utterance)

        source = utterance.source
        engine = self.engines[source]
        paid = self.meter is not None and engine.name != "stub"
        if paid and self.meter.exhausted:
            # When the money runs out, no audio goes to the cloud: otherwise we would be the ones
            # paying. We turn off the control panel here, on the event loop, and not from the meter
            # thread: it touches recording and windows, and those are not called from foreign threads.
            if self.on_broke is not None:
                self.on_broke()
            return

        # The shadow engine is started BEFORE waiting for the primary one and is not awaited: it
        # runs at its own pace and adds not a millisecond to the delay of the primary path. The
        # point of the comparison is to observe the engines, not to slow down the working pipeline
        # for the sake of observation.
        shadow = self._spawn_shadow(loop, utterance)

        event, sent, failed = await loop.run_in_executor(
            self._pools[source], self._run_engine, engine, utterance
        )

        # We charge the seconds when the audio actually left (if the session did not come up or
        # there is no key, it did not leave, and there is nothing to pay for), not just because an
        # utterance existed. If it left and came back empty, that is paid too: the cloud accepted
        # it. Audio that goes through the account server is counted by that server itself
        # (`server/relay_stt.py`): a report from the app would charge the same seconds a second
        # time.
        if paid and sent and not getattr(engine, "metered_by_server", False):
            # The price depends on which accuracy tier is listening: engines without a choice ("stub",
            # the backup one) have no tier, so they are "normal".
            self.meter.add(utterance.duration_s, getattr(engine, "tier", "normal"))

        if event is None:
            self.dropped += 1
            event = await self._rescue(shadow)
            if event is None:
                # The utterance is not lost silently: if the engine knows a reason that can be told to
                # people ("the balance is empty", "you need to sign in"), it goes among the page's
                # complaints and is cleared by the first recognized utterance.
                reason = getattr(engine, "last_error", "")
                if reason:
                    self.warm_error = reason
                # Empty text (key clatter) is not a loss; a loss is a failure of the engine itself.
                self._lost_in_a_row = self._lost_in_a_row + 1 if failed else 0
                return
        self._lost_in_a_row = 0
        self.transcribed += 1
        self.warm_error = ""
        if event.engine and not event.error:
            event = event.model_copy(update={"engine": ""})
        self.bus.publish(event)

    @staticmethod
    def _run_engine(engine: STTProvider, utterance: Utterance) -> tuple[SpeechEvent | None, bool, bool]:
        """Transcribe an utterance in a pool thread and read two flags of this call from the engine.

        `sent` tells whether the audio went to the network (paid). `failed` tells whether the
        engine itself failed: its `failures` counter grew. We read them here, in the same thread
        right after the call: the pool per engine has a single thread, so another utterance
        cannot reset them between the call and the read. An engine without these flags (a stub,
        a fallback) is considered to have sent the audio and not to have failed.
        """
        before = getattr(engine, "failures", None)
        event = engine.transcribe(utterance)
        after = getattr(engine, "failures", None)
        failed = isinstance(before, int) and isinstance(after, int) and after > before
        return event, bool(getattr(engine, "audio_sent", True)), failed

    async def _rescue(self, shadow: asyncio.Task | None) -> SpeechEvent | None:
        """Take the text from the second engine when the primary one produced nothing.

        The primary stays silent for three reasons, and all three are network-related: the
        connection went down, the limit ran out, or the conversational model broke into a dialogue
        and its answer was thrown away by the filter. In all three cases the other party's
        utterance is lost, which is exactly the one the pipeline exists for, while a second
        engine is already running next to it on the very same audio.

        It is fine to wait for it here: this path has already failed anyway, and an extra second
        is cheaper than a lost question. On a healthy path there is no waiting, because we
        simply do not go there.
        """
        if shadow is None:
            return None
        event = await shadow
        if event is None or not event.text.strip():
            return None
        self.rescued += 1
        log.warning("the primary engine returned nothing, the utterance was picked up by %s", event.engine)
        return event.model_copy(update={"engine": "", "rescued_by": event.engine})

    def _spawn_shadow(
        self, loop: asyncio.AbstractEventLoop, utterance: Utterance
    ) -> asyncio.Task | None:
        if self.shadow is None or self._shadow_pool is None:
            return None

        shadow = self.shadow

        async def run() -> SpeechEvent | None:
            try:
                event = await loop.run_in_executor(
                    self._shadow_pool, shadow.transcribe, utterance
                )
            except Exception as e:                   # noqa: BLE001
                log.debug("the shadow engine stumbled: %s", e)
                return None
            if event is not None:
                self.bus.publish(event)
            return event

        task = loop.create_task(run())
        # We keep a reference: without it the garbage collector may remove the task on the fly,
        # and some shadow utterances would vanish silently.
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)
        return task

    async def drain(self) -> None:
        """Close unfinished utterances without stopping the pipeline."""
        loop = asyncio.get_running_loop()
        for seg in self.segmenters.values():
            tail = seg.flush()
            if tail is None:
                continue
            if self._queues:
                self._enqueue(tail)
            else:
                await self._handle(loop, tail)      # `run` is not active, so there are no queues: transcribe right away
        if self._queues:
            # And what was already waiting in the queues: "close" means waiting for the text of
            # everything that was said.
            await asyncio.gather(*(q.join() for q in self._queues.values()))

    # How long to wait in total for the engines to close their sessions. Closing a session is a
    # handshake, and when the other side does not answer (and at shutdown it most often does
    # not), each engine gives up by itself after two or three seconds. They used to be closed
    # one by one, and two channels gave four seconds of shutdown (measured) during which the
    # process still held the single-instance lock.
    CLOSE_BUDGET_S = 2.5

    def close(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
        for pool in {id(p): p for p in self._pools.values()}.values():
            pool.shutdown(wait=False, cancel_futures=True)

        for task in list(self._shadow_tasks):
            task.cancel()
        if self._shadow_pool is not None:
            self._shadow_pool.shutdown(wait=False, cancel_futures=True)

        # We close all sessions at once, each in its own thread, not one after another: we wait not
        # for the sum but for the longest one, and no longer than the total limit. Whatever did not
        # finish stays on a daemon thread, which the process does not wait for.
        #
        # The usage meter goes in the same salvo: on closing it reports to the account server over
        # the network, and an unreachable server held the shutdown for up to ten seconds
        # (measured). Whatever remainder did not go out in time is lost; the cost is a fraction of
        # a second of usage, and that is cheaper than a process that does not exit.
        closers = [engine.close for _, engine in self._unique_engines()]
        if self.shadow is not None:
            closers.append(self.shadow.close)
        if self.meter is not None:
            closers.append(self.meter.close)
        threads = [
            threading.Thread(target=closer, name=f"speech-close-{i}", daemon=True)
            for i, closer in enumerate(closers)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.CLOSE_BUDGET_S
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                log.warning("recognition shutdown did not finish within %.1f s — abandoning", self.CLOSE_BUDGET_S)


async def transcribe_file(path: Path, cfg: Config, source: Source = Source.THEM) -> list[SpeechEvent]:
    """Recognize a finished 16 kHz mono WAV. Needed for tests and debugging."""
    import wave

    from argus.core.events import SAMPLE_RATE

    # Another format cannot simply be "read as is": 44.1 kHz, stereo or 8-bit would turn into
    # noise that would go to paid recognition. It is better to refuse before sending.
    try:
        with wave.open(str(path), "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            if (rate, channels, width) != (SAMPLE_RATE, 1, 2):
                raise BadWavError(
                    f"a WAV of {SAMPLE_RATE // 1000} kHz, mono, 16 bit is required; "
                    f"the file has {rate} Hz, {channels} channels, {width * 8} bits"
                )
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError) as e:
        raise BadWavError(f"the file cannot be read as WAV: {e}") from e

    model = ensure_model(cfg.models_dir)
    seg = UtteranceSegmenter(SileroVAD(model), source, VADSettings(**cfg.vad.model_dump()))
    stt = build_stt(cfg)

    step = SAMPLE_RATE * cfg.audio.chunk_ms // 1000
    found: list[Utterance] = []
    for i in range(0, len(audio), step):
        t = i * 1_000_000_000 // SAMPLE_RATE
        found += seg.feed(AudioChunk(t=t, source=source, samples=audio[i : i + step]))
    tail = seg.flush()
    if tail is not None:
        found.append(tail)

    events = [stt.transcribe(u) for u in found]
    return [e for e in events if e is not None]
