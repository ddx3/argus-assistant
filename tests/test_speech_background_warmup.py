"""Recognition sessions come up in the background: startup does not wait for them.

Before, `Argus.start()` waited for the network handshake with the cloud (a second or two,
up to fifteen with a bad connection), and only then did the server come up, followed by
the window. Without a network or without a key the app did not open at all.

Now startup does not depend on the cloud; if it fails, the reason sits in the page's
complaints (`problems`), and the first real utterance clears it.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from argus.app import Argus
from argus.capture.audio import DeviceInfo
from argus.core import friendly
from argus.core.bus import Bus
from argus.core.config import Config
from argus.core.events import SAMPLE_RATE, Source, SpeechEvent, Utterance
from argus.perception.speech import SpeechPipeline

NS = 1_000_000_000


class FakeEngine:
    """An engine whose warm-up can be made slow or broken."""

    name = "fake"
    per_channel = False

    def __init__(self, delay: float = 0.0, fail: str | None = None) -> None:
        self.delay = delay
        self.fail = fail
        self.warmed = False

    def warmup(self) -> None:
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)
        self.warmed = True

    def transcribe(self, utterance):
        return SpeechEvent(
            t=utterance.t, t_end=utterance.t_end, source=utterance.source, text="hello"
        )

    def close(self) -> None:
        pass


def pipeline(engine: FakeEngine) -> SpeechPipeline:
    return SpeechPipeline(Bus(), Config(), stt=engine)


def utterance() -> Utterance:
    return Utterance(
        t=NS, t_end=2 * NS, source=Source.THEM, samples=np.zeros(SAMPLE_RATE, dtype=np.int16)
    )


async def test_warming_in_the_background_returns_at_once():
    engine = FakeEngine(delay=0.6)
    speech = pipeline(engine)

    started = time.monotonic()
    speech.warm_in_background()
    assert time.monotonic() - started < 0.2, "startup must not wait for the handshake"
    assert not engine.warmed

    await speech._warm_task
    assert engine.warmed
    assert speech.warm_error == ""
    speech.close()


async def test_a_failed_warmup_is_a_complaint_not_a_crash():
    """No key, no network: the app opens and says what is wrong."""
    speech = pipeline(FakeEngine(fail="no key OPENAI_API_KEY"))

    speech.warm_in_background()
    await speech._warm_task                      # does not throw

    # The person gets one clear phrase; the name of the environment variable stays in the log.
    assert speech.warm_error == friendly.ACCESS
    assert speech.complaints() == [f"recognition: {friendly.ACCESS}"]
    assert "OPENAI" not in speech.warm_error
    speech.close()


async def test_the_first_recognised_utterance_clears_the_complaint():
    """Once an utterance has reached text, there is nothing left to complain about."""
    speech = pipeline(FakeEngine())
    speech.warm_error = "network unavailable"

    await speech._handle(asyncio.get_running_loop(), utterance())

    assert speech.warm_error == ""
    assert speech.complaints() == []
    speech.close()


async def test_a_clean_pipeline_has_no_complaint():
    speech = pipeline(FakeEngine())
    assert speech.complaints() == []
    speech.close()


def _fake_devices(app: Argus) -> None:
    """Without real audio: the test is about the startup order, not about hardware."""
    mic = DeviceInfo(index=1, name="microphone", rate=16000, channels=1)
    loop = DeviceInfo(index=2, name="system", rate=48000, channels=2)
    app.audio.open = lambda: (mic, loop)          # type: ignore[method-assign]


async def test_start_does_not_wait_for_the_recognition_sessions(monkeypatch):
    app = Argus(Config(), with_screen=False, with_brain=False)
    _fake_devices(app)

    opened = asyncio.Event()

    async def slow_warmup() -> None:
        await asyncio.sleep(0.8)
        opened.set()

    monkeypatch.setattr(app.speech, "warmup", slow_warmup)

    started = time.monotonic()
    await app.start()
    elapsed = time.monotonic() - started

    try:
        assert app.started is True
        assert elapsed < 0.5, f"startup waited for the cloud for {elapsed:.1f} s"
        assert not opened.is_set(), "the sessions must come up only after startup"
        await asyncio.wait_for(opened.wait(), timeout=3.0)
    finally:
        await app.stop()


async def test_start_survives_a_recognition_that_cannot_open(monkeypatch):
    """This used to be a startup failure ("failed to start: no key")..
    """
    app = Argus(Config(), with_screen=False, with_brain=False)
    _fake_devices(app)

    async def broken() -> None:
        raise RuntimeError("no key: the OPENAI_API_KEY environment variable is empty")

    monkeypatch.setattr(app.speech, "warmup", broken)

    await app.start()
    try:
        await app.speech._warm_task
        assert app.started is True
        complaints = [p for p in app.problems() if p.startswith("recognition:")]
        assert complaints, app.problems()
        assert not any("OPENAI" in p for p in app.problems()), app.problems()
    finally:
        await app.stop()


async def test_closing_while_the_sessions_are_still_opening_is_quiet(monkeypatch):
    """The window was closed while the handshake is in progress: the warm-up is cancelled,
    quietly.
    """
    app = Argus(Config(), with_screen=False, with_brain=False)
    _fake_devices(app)

    async def forever() -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(app.speech, "warmup", forever)

    await app.start()
    task = app.speech._warm_task
    await asyncio.wait_for(app.stop(), timeout=5.0)

    with pytest.raises(asyncio.CancelledError):
        await task
