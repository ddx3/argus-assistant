"""The app survives recording failures and counts the model's latency honestly.

The errors here are silent: the audio pump used to die with not a word in the log or on the
page; the "model" stage showed 0 ms on every streamed answer.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from argus.app import Argus
from argus.brain.base import Answer
from argus.core.config import Config
from argus.core.events import AudioChunk, Source
from argus.session.layout import META_NAME


def make_app(tmp_path, **record) -> Argus:
    cfg = Config()
    cfg.root = tmp_path
    cfg.paths.sessions = tmp_path / "sessions"
    cfg.record.always = True
    for k, v in record.items():
        setattr(cfg.record, k, v)
    return Argus(cfg, with_screen=False, with_brain=False)


def chunk(t_ms: int, source: Source = Source.ME) -> AudioChunk:
    samples = np.full(512, 1000, dtype=np.int16)
    return AudioChunk(t=t_ms * 1_000_000, source=source, samples=samples)


class _FullDisk:
    """A recording with nowhere to write: every call fails, and so does closing."""

    def __init__(self) -> None:
        self.closed_calls = 0

    def feed_audio(self, chunk):
        raise OSError(28, "No space left on device")

    def feed_frame(self, frame):
        raise OSError(28, "No space left on device")

    def close(self, mic=None, loopback=None):
        self.closed_calls += 1
        raise OSError(28, "No space left on device")


# --------------------------------------------------------------------------
# Recording and a full disk
# --------------------------------------------------------------------------


async def test_a_full_disk_does_not_kill_the_audio_pump(tmp_path):
    app = make_app(tmp_path)
    app.bus.bind(asyncio.get_running_loop())
    app.recorder = _FullDisk()
    sub = app.bus.subscribe(AudioChunk, name="t")
    pump = asyncio.create_task(app._pump_audio(sub))

    app.bus.publish(chunk(0))
    app.bus.publish(chunk(32))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if app.state.levels[Source.ME].rms > 0 and app.recorder is None:
            break
    try:
        assert not pump.done(), "the audio pump died because of the recording"
        assert app.state.levels[Source.ME].rms > 0           # levels are alive
        assert app.recorder is None                          # a dead recording does not hang around
        assert "recording:" in " ".join(app.problems())          # and the reason is visible on the page
        assert "Errno" not in " ".join(app.problems())
    finally:
        pump.cancel()


async def test_a_dead_recorder_is_dropped_even_when_closing_it_fails(tmp_path, caplog):
    """Previously an exception from `close()` left a dead recording behind: the next start did
    not open a new session, and `stop()` aborted before closing the rest."""
    app = make_app(tmp_path)
    rec = _FullDisk()
    app.recorder = rec

    with caplog.at_level("ERROR"):
        app._close_session()

    assert rec.closed_calls == 1
    assert app.recorder is None
    assert "recording closed with an error" in caplog.text


async def test_stop_closes_everything_even_if_the_recording_will_not_close(tmp_path):
    app = make_app(tmp_path)
    app.recorder = _FullDisk()
    closed: list[str] = []
    app.audio.close = lambda: closed.append("audio")
    app.speech.close = lambda: closed.append("speech")

    await app.stop()

    assert closed == ["audio", "speech"]


def test_recognition_starts_without_a_recording_when_the_disk_is_unavailable(tmp_path, monkeypatch):
    """Turning on recognition matters more than recording it."""
    app = make_app(tmp_path)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("argus.app.Recorder", boom)
    app.set_running(True)

    assert app.running is True
    assert app.recorder is None
    assert app.record_error.startswith("didn't start")


def test_a_new_session_opens_after_a_failed_one(tmp_path):
    app = make_app(tmp_path)
    app.recorder = _FullDisk()
    app.set_running(False)                     # closing fails, but the recording is dropped
    assert app.recorder is None

    app.set_running(True)                      # the next switch-on opens a new one
    assert app.recorder is not None
    assert app.record_error == ""
    app.set_running(False)
    assert (app.session_dir / META_NAME).exists()
    assert json.loads((app.session_dir / META_NAME).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Timeline database
# --------------------------------------------------------------------------


def test_the_timeline_database_is_created_only_when_something_fills_it(tmp_path):
    """With the assistant, it keeps the timeline, and `timeline.db` stayed an empty file in every
    session.
    """
    plain = make_app(tmp_path / "a")
    plain.set_running(True)
    assert (plain.session_dir / "timeline.db").exists()
    plain.set_running(False)

    cfg = Config()
    cfg.root = tmp_path / "b"
    cfg.paths.sessions = tmp_path / "b" / "sessions"
    cfg.record.always = True
    with_brain = Argus(cfg, with_screen=False, with_brain=True)
    with_brain.set_running(True)
    assert not (with_brain.session_dir / "timeline.db").exists()
    with_brain.set_running(False)


# --------------------------------------------------------------------------
# Model latency
# --------------------------------------------------------------------------


async def test_a_streamed_answer_records_its_latency_when_it_is_complete(tmp_path):
    """A streamed answer is published empty, and `latency_ms` is set later — the stage showed 0."""
    app = make_app(tmp_path)
    app.STAGE_POLL_S = 0.01
    app.bus.bind(asyncio.get_running_loop())
    sub = app.bus.subscribe(Answer, name="t")
    pump = asyncio.create_task(app._pump_answers(sub))

    answer = Answer(text="", provider="x", done=False)
    app.bus.publish(answer)
    await asyncio.sleep(0.05)
    assert app.state.stages["brain"].count == 0              # still in progress, we do not record 0

    answer.text = "done"
    answer.latency_ms = 1800.0
    answer.done = True
    for _ in range(300):
        await asyncio.sleep(0.01)
        if app.state.stages["brain"].count:
            break
    try:
        assert app.state.stages["brain"].count == 1
        assert app.state.stages["brain"].last_ms == 1800.0
    finally:
        pump.cancel()


async def test_a_finished_answer_records_its_latency_at_once(tmp_path):
    app = make_app(tmp_path)
    app.bus.bind(asyncio.get_running_loop())
    sub = app.bus.subscribe(Answer, name="t")
    pump = asyncio.create_task(app._pump_answers(sub))

    app.bus.publish(Answer(text="answer", latency_ms=700.0, provider="x"))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if app.state.stages["brain"].count:
            break
    try:
        assert app.state.stages["brain"].last_ms == 700.0
        assert not app._stage_tasks
    finally:
        pump.cancel()


@pytest.mark.parametrize("force_image", [True, False])
async def test_an_explicit_screen_question_sends_the_picture_with_the_switch_off(force_image):
    """"Ask about the screen" overrides the "Screen" switch, and an
    ordinary question does not.
    """
    from test_brain import FakeProvider, make_assistant

    provider = FakeProvider()
    a = make_assistant(provider)
    a.cfg.brain.send_screenshot = False
    a.screenshot = lambda: b"jpeg-bytes"

    await a.ask_now("what is on the screen", force_image=force_image)

    assert provider.images == ([b"jpeg-bytes"] if force_image else [None])
    assert a.cfg.brain.send_screenshot is False
