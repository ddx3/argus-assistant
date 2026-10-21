"""Speech pipeline: channels do not wait for each other, a pause leaves no tail, losses are
visible on the page.

The engines and the cutter are fakes: what is checked is the wiring of the pipeline
(`SpeechPipeline.run` and `_handle`), not recognition. Audio goes through a real bus.
"""

from __future__ import annotations

import asyncio
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from argus.core.bus import Bus
from argus.core.config import Config
from argus.core.events import SAMPLE_RATE, AudioChunk, Source, SpeechEvent, Utterance
from argus.perception.speech import BadWavError, SpeechPipeline, transcribe_file

NS = 1_000_000_000


def utterance(source: Source, t_s: float = 0.0, seconds: float = 1.0) -> Utterance:
    return Utterance(
        t=int(t_s * NS), t_end=int((t_s + seconds) * NS), source=source,
        samples=np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16),
    )


def chunk(source: Source, t_s: float = 0.0) -> AudioChunk:
    return AudioChunk(t=int(t_s * NS), source=source, samples=np.zeros(512, dtype=np.int16))


class Engine:
    """A fake engine: it thinks for `delay` seconds, then returns text (or stays silent if there
    is no text).
    """

    per_channel = True

    def __init__(self, delay: float = 0.0, text: str | None = "hello", boom_first: bool = False):
        self.name = "fake"
        self.delay = delay
        self.text = text
        self.boom_first = boom_first
        self.seen: list[Utterance] = []
        self.failures = 0

    def transcribe(self, u: Utterance) -> SpeechEvent | None:
        if self.boom_first and not self.seen:
            self.seen.append(u)
            raise RuntimeError("the engine crashed")
        if self.delay:
            time.sleep(self.delay)
        self.seen.append(u)
        if self.text is None:
            return None
        return SpeechEvent(t=u.t, t_end=u.t_end, source=u.source, text=f"{self.text}-{u.source.value}")

    def warmup(self) -> None: ...

    def close(self) -> None: ...


class FakeSegmenter:
    """A fake segmenter: every chunk of audio is a finished utterance; `open` is an unfinished
    one.
    """

    def __init__(self, source: Source, each_chunk: bool = True) -> None:
        self.source = source
        self.each_chunk = each_chunk
        self.open: Utterance | None = None
        self.resets = 0

    def feed(self, c: AudioChunk) -> list[Utterance]:
        if not self.each_chunk:
            return []
        return [utterance(self.source, c.t / NS)]

    def flush(self) -> Utterance | None:
        tail, self.open = self.open, None
        return tail

    def reset(self) -> None:
        self.resets += 1


def make(me: Engine, them: Engine, bus: Bus | None = None, each_chunk: bool = True) -> SpeechPipeline:
    sp = SpeechPipeline(bus or Bus(), Config(), stt=me)
    sp.engines = {Source.ME: me, Source.THEM: them}
    sp._pools = {src: ThreadPoolExecutor(max_workers=1) for src in (Source.ME, Source.THEM)}
    sp.segmenters = {src: FakeSegmenter(src, each_chunk) for src in (Source.ME, Source.THEM)}
    return sp


async def stop(*tasks: asyncio.Task) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------
# Channels do not wait for each other
# --------------------------------------------------------------------------


async def test_a_slow_reply_on_one_channel_does_not_hold_the_other():
    """Your own long reply takes a second to transcribe, while the interlocutor's question is
    already ready.

    Before, one loop waited for the transcription inside itself: the interlocutor's question
    stood behind the user's reply, and during the stall both channels stopped reading audio.
    """
    bus = Bus()
    sp = make(Engine(delay=1.0), Engine(), bus)
    sub = bus.subscribe(SpeechEvent, maxsize=64, name="t")
    arrived: dict[Source, float] = {}
    t0 = time.monotonic()

    async def watch() -> None:
        async for ev in sub:
            arrived[ev.source] = time.monotonic() - t0

    watcher = asyncio.create_task(watch())
    run = asyncio.create_task(sp.run())
    try:
        await asyncio.sleep(0.05)
        bus.publish(chunk(Source.ME))
        bus.publish(chunk(Source.THEM))
        await asyncio.sleep(0.5)
        assert Source.THEM in arrived, "the other party's remark is waiting for another channel's transcription"
        assert Source.ME not in arrived                      # your own is still being transcribed
        await asyncio.sleep(1.0)
        assert Source.ME in arrived
    finally:
        await stop(run, watcher)
        sp.close()


async def test_the_audio_is_still_read_while_a_reply_is_being_transcribed():
    """While the engine is busy, the audio loop does not stall: the next utterance of the same
    channel is queued.
    """
    bus = Bus()
    them = Engine(delay=0.6)
    sp = make(Engine(), them, bus)
    run = asyncio.create_task(sp.run())
    try:
        await asyncio.sleep(0.05)
        for i in range(3):
            bus.publish(chunk(Source.THEM, i))
        await asyncio.sleep(0.2)
        # All three chunks are already cut: the first is with the engine, two are in the queue.
        assert them.seen == []
        assert sp._queues[Source.THEM].qsize() == 2
        await asyncio.sleep(2.0)
        assert [u.t for u in them.seen] == [0, NS, 2 * NS]   # order within the channel is preserved
    finally:
        await stop(run)
        sp.close()


async def test_one_failing_utterance_does_not_stop_the_channel():
    bus = Bus()
    them = Engine(boom_first=True)
    sp = make(Engine(), them, bus)
    run = asyncio.create_task(sp.run())
    try:
        await asyncio.sleep(0.05)
        bus.publish(chunk(Source.THEM, 0))
        bus.publish(chunk(Source.THEM, 1))
        await asyncio.sleep(0.4)
        assert len(them.seen) == 2                            # the second utterance was handled
        assert not run.done()                                 # and the loop itself is alive
    finally:
        await stop(run)
        sp.close()


def test_a_full_queue_drops_the_oldest_utterance():
    """The engine is stuck: the queue does not grow without limit, and the oldest utterance is
    dropped.
    """
    sp = make(Engine(), Engine())
    sp._queues = {src: asyncio.Queue(maxsize=2) for src in (Source.ME, Source.THEM)}
    for i in range(3):
        sp._enqueue(utterance(Source.THEM, i))
    queue = sp._queues[Source.THEM]
    assert [queue.get_nowait().t for _ in range(2)] == [NS, 2 * NS]
    assert sp.dropped == 1
    sp.close()


async def test_drain_waits_for_what_is_already_in_the_queues():
    """Stop: what is unfinished and what is already in the queue reaches text before `drain`
    returns.
    """
    bus = Bus()
    them = Engine(delay=0.2)
    sp = make(Engine(), them, bus, each_chunk=False)
    sub = bus.subscribe(SpeechEvent, maxsize=8, name="t")
    run = asyncio.create_task(sp.run())
    try:
        await asyncio.sleep(0.05)
        sp.segmenters[Source.THEM].open = utterance(Source.THEM, 5.0)
        await asyncio.wait_for(sp.drain(), timeout=3.0)
        assert [u.t for u in them.seen] == [5 * NS]
        assert sub.qsize() == 1
    finally:
        await stop(run)
        sp.close()


async def test_drain_without_a_running_loop_transcribes_directly():
    bus = Bus()
    them = Engine()
    sp = make(Engine(), them, bus, each_chunk=False)
    sp.segmenters[Source.THEM].open = utterance(Source.THEM, 2.0)
    await sp.drain()
    assert [u.t for u in them.seen] == [2 * NS]
    sp.close()


# --------------------------------------------------------------------------
# Pause
# --------------------------------------------------------------------------


async def test_pausing_sends_the_open_utterance_to_the_engine_and_clears_the_detector():
    """The last words before "stop" are transcribed at once, not glued to the new start."""
    bus = Bus()
    them = Engine()
    sp = make(Engine(), them, bus, each_chunk=False)
    run = asyncio.create_task(sp.run())
    try:
        await asyncio.sleep(0.05)
        seg = sp.segmenters[Source.THEM]
        seg.open = utterance(Source.THEM, 7.0)               # the person was still speaking
        sp.paused = True
        bus.publish(chunk(Source.THEM, 8.0))                 # audio keeps flowing during a pause, and the loop sees it
        await asyncio.sleep(0.3)
        assert [u.t for u in them.seen] == [7 * NS]
        assert seg.resets >= 1                               # the detector forgot the rest

        # Repeated chunks during a pause forward nothing.
        bus.publish(chunk(Source.THEM, 9.0))
        await asyncio.sleep(0.2)
        assert len(them.seen) == 1
    finally:
        await stop(run)
        sp.close()


# --------------------------------------------------------------------------
# Losses reach the page
# --------------------------------------------------------------------------


class Failing(Engine):
    """An engine whose utterances are lost through its own fault (connection, timeout):
    `failures` grows. When it recovers, it returns text."""

    healthy = False

    def transcribe(self, u: Utterance) -> SpeechEvent | None:
        if self.healthy:
            return super().transcribe(u)
        self.failures += 1
        return None


async def test_lost_utterances_in_a_row_become_a_complaint_and_a_success_removes_it():
    engine = Failing()
    sp = SpeechPipeline(Bus(), Config(), stt=engine)
    loop = asyncio.get_running_loop()

    await sp._handle(loop, utterance(Source.THEM))
    assert sp.complaints() == []                              # one loss is not yet a reason

    await sp._handle(loop, utterance(Source.THEM))
    assert sp.complaints() == ["recognition: no connection"]

    engine.healthy = True
    await sp._handle(loop, utterance(Source.THEM))
    assert sp.complaints() == []
    sp.close()


async def test_an_empty_transcript_is_not_a_loss():
    """Keyboard clatter yields empty text with no engine failure, so there is nothing to
    complain about.
    """
    engine = Engine(text=None)                                # returns None, `failures` does not grow
    sp = SpeechPipeline(Bus(), Config(), stt=engine)
    for _ in range(5):
        await sp._handle(asyncio.get_running_loop(), utterance(Source.THEM))
    assert sp.complaints() == []
    sp.close()


# --------------------------------------------------------------------------
# `argus transcribe`: the file format is checked before sending
# --------------------------------------------------------------------------


def write_wav(path, rate: int, channels: int, width: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setframerate(rate)
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.writeframes(bytes(width * channels * rate // 10))


@pytest.mark.parametrize(
    "rate, channels, width",
    [(44_100, 1, 2), (SAMPLE_RATE, 2, 2), (SAMPLE_RATE, 1, 1)],
    ids=["44.1 kHz", "stereo", "8 bit"],
)
async def test_a_wav_of_another_format_is_refused_before_anything_is_sent(tmp_path, rate, channels, width):
    path = tmp_path / "x.wav"
    write_wav(path, rate, channels, width)
    with pytest.raises(BadWavError, match="16 kHz, mono, 16 bit"):
        await transcribe_file(path, Config())


async def test_a_file_that_is_not_a_wav_gets_a_plain_message(tmp_path):
    path = tmp_path / "x.wav"
    path.write_bytes("this is not sound".encode())
    with pytest.raises(BadWavError, match="cannot be read as WAV"):
        await transcribe_file(path, Config())


def test_the_transcribe_command_says_why_a_wav_is_refused(tmp_path, monkeypatch):
    """The command answers with a phrase and exit code 1, not a traceback."""
    from typer.testing import CliRunner

    from argus import cli

    monkeypatch.setattr("argus.cli.Config.load", lambda *a, **k: Config())
    path = tmp_path / "x.wav"
    write_wav(path, 44_100, 2, 2)

    result = CliRunner().invoke(cli.app, ["transcribe", str(path)])

    assert result.exit_code == 1
    assert "16 kHz, mono, 16 bit" in result.output
    assert "Traceback" not in result.output
