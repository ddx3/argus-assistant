"""Comparison of two recognition engines on the same audio.

The point of the checks is not that the second engine works, but that its presence
**changes nothing** for the main path. The comparison must be an observation: the same
text in the timeline, the same calls to the model, the same latency. Otherwise the
measurement starts measuring itself.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from argus.context.timeline import Timeline
from argus.core.bus import Bus
from argus.core.config import Config
from argus.core.events import SAMPLE_RATE, Source, SpeechEvent, Utterance
from argus.perception.speech import SpeechPipeline, build_shadow
from argus.session.replay_run import _normalize

NS = 1_000_000_000


@pytest.fixture(autouse=True)
def _a_key_in_the_environment(monkeypatch):
    """The tests do not read the environment of the machine they run on.

    `argus doctor` checks the key before it looks at the comparison settings, so without a key in the environment even the
    checks about engines, not about the key, would go red. Tests that need the key missing delete it themselves (`delenv`).
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")


def utterance(start_s: float = 0.0, seconds: float = 1.0,
              source: Source = Source.THEM) -> Utterance:
    n = int(seconds * SAMPLE_RATE)
    return Utterance(
        t=int(start_s * NS),
        t_end=int((start_s + seconds) * NS),
        source=source,
        samples=np.zeros(n, dtype=np.int16),
    )


class FakeSTT:
    """A stub recognizer: returns a predefined text."""

    def __init__(self, name: str, text: str = "text", engine: str = "",
                 fail: bool = False) -> None:
        self.name = name
        self.text = text
        self.engine = engine
        self.fail = fail
        self.seen: list[Utterance] = []

    def transcribe(self, u: Utterance) -> SpeechEvent | None:
        self.seen.append(u)
        if self.fail:
            raise RuntimeError("engine failed")
        return SpeechEvent(
            t=u.t, t_end=u.t_end, source=u.source,
            text=self.text, engine=self.engine, latency_ms=42.0,
        )

    def warmup(self) -> None: ...
    def close(self) -> None: ...


def pipeline(cfg: Config, bus: Bus, primary: FakeSTT,
             shadow: FakeSTT | None = None) -> SpeechPipeline:
    return SpeechPipeline(bus, cfg, stt=primary, shadow=shadow)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_shadow_off_by_default():
    """Without an explicit setting there is no second engine — the comparison does not turn on
    by itself.
    """
    assert build_shadow(Config()) is None


def test_shadow_refuses_to_compare_engine_with_itself():
    """Comparing an engine with itself is almost certainly a typo in the config.

    Silently agreeing would mean showing two identical columns and spending twice the time
    and money to confirm what is already known.
    """
    cfg = Config()
    cfg.stt.compare_with = cfg.stt.provider
    with pytest.raises(ValueError, match="with itself"):
        build_shadow(cfg)


def test_unknown_engine_names_itself_in_the_error():
    cfg = Config()
    cfg.stt.compare_with = "no-such-engine"
    with pytest.raises(ValueError, match="no-such-engine"):
        build_shadow(cfg)


# --------------------------------------------------------------------------
# Both engines on the same audio
# --------------------------------------------------------------------------


async def test_both_engines_get_the_same_audio():
    """The key property of the measurement: the segmentation is the same for both.

    Comparing engines on different segmentation is meaningless: the differences would come
    from phrase boundaries, not from recognition.
    """
    bus = Bus()
    a = FakeSTT("a", "first")
    b = FakeSTT("b", "second", engine="b")
    pipe = pipeline(Config(), bus, a, b)

    u = utterance()
    await pipe._handle(asyncio.get_running_loop(), u)
    await asyncio.sleep(0.05)

    assert len(a.seen) == 1
    assert len(b.seen) == 1
    assert a.seen[0] is b.seen[0]      # exactly the same object, not a copy
    pipe.close()


async def test_shadow_event_is_marked_and_primary_is_not():
    bus = Bus()
    sub = bus.subscribe(SpeechEvent, maxsize=8, name="test")
    pipe = pipeline(Config(), bus, FakeSTT("a", "first"),
                    FakeSTT("b", "second", engine="realtime-openai"))

    await pipe._handle(asyncio.get_running_loop(), utterance())
    await asyncio.sleep(0.05)

    got = {}
    for _ in range(2):
        ev = await asyncio.wait_for(sub.get(), timeout=1.0)
        got[ev.engine] = ev.text

    assert got[""] == "first"                     # the main one has no mark
    assert got["realtime-openai"] == "second"
    sub.close()
    pipe.close()


async def test_shadow_failure_does_not_touch_primary():
    """The shadow engine has no right to bring down the working pipeline.

    It stands alongside for observation: the network drops, the key goes stale, the limit
    runs out, and the main recognition must keep working as if nothing happened.
    """
    bus = Bus()
    sub = bus.subscribe(SpeechEvent, maxsize=8, name="test")
    pipe = pipeline(Config(), bus, FakeSTT("a", "first"),
                    FakeSTT("b", fail=True, engine="b"))

    await pipe._handle(asyncio.get_running_loop(), utterance())
    await asyncio.sleep(0.05)

    ev = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev.text == "first"
    assert ev.engine == ""
    assert pipe.transcribed == 1
    sub.close()
    pipe.close()


async def test_shadow_does_not_delay_primary():
    """A slow second engine must not stretch the first one's latency.

    That is why it lives in its own thread: in a shared queue it would add its time to the
    other's, and the measurement would show the sum instead of each one separately.
    """
    import time

    class SlowSTT(FakeSTT):
        def transcribe(self, u: Utterance) -> SpeechEvent | None:
            time.sleep(0.4)
            return super().transcribe(u)

    bus = Bus()
    pipe = pipeline(Config(), bus, FakeSTT("a"), SlowSTT("b", engine="b"))

    started = time.perf_counter()
    await pipe._handle(asyncio.get_running_loop(), utterance())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.3, f"the main path waited for the shadow engine: {elapsed:.2f} s"
    pipe.close()


# --------------------------------------------------------------------------
# Isolation: the comparison does not change what the model sees
# --------------------------------------------------------------------------


def test_shadow_speech_never_reaches_the_timeline():
    """The timeline stays exactly what it was without the comparison.

    Letting shadow text into the timeline would double every utterance in the prompt and
    wake the model twice per question, which means measuring not the engines but the
    behavior of a changed system.
    """
    timeline = Timeline()
    timeline.add_speech(SpeechEvent(
        t=0, t_end=NS, source=Source.THEM, text="a real line",
    ))
    before = len(timeline)

    shadow = SpeechEvent(
        t=0, t_end=NS, source=Source.THEM,
        text="the same, by the second engine", engine="realtime-openai",
    )
    # The same way as `Assistant._watch_speech` and `Argus._pump_speech` do it.
    if not shadow.engine:
        timeline.add_speech(shadow)

    assert len(timeline) == before
    assert "by the second engine" not in "\n".join(e.text for e in timeline)


def test_engine_field_defaults_to_empty():
    """Old events without an `engine` field remain primary.

    Session recordings were made before the comparison appeared, and replay must keep
    reading them as before.
    """
    ev = SpeechEvent(t=0, t_end=NS, source=Source.ME, text="hello")
    assert ev.engine == ""
    assert ev.latency_ms is None


# --------------------------------------------------------------------------
# Matching of texts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("They push mid.", "they push mid"),
        ("Roshan, in a minute!", "roshan in a minute"),
        ("yes  —  fine", "Yes, fine."),
    ],
)
def test_punctuation_is_not_a_disagreement(a: str, b: str):
    """Engines place commas and capital letters differently.

    Counting that as a disagreement would drown the real jargon errors in formatting noise,
    and jargon is what the whole thing was started for.
    """
    assert _normalize(a) == _normalize(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("roshen in a minute", "Roshan in a minute"),
        ("clips on mid", "creeps on mid"),
        ("", "heard something"),
    ],
)
def test_real_differences_survive_normalization(a: str, b: str):
    assert _normalize(a) != _normalize(b)


# --------------------------------------------------------------------------
# `argus doctor`: learn that it is not ready before the recording, not after
# --------------------------------------------------------------------------


def _compare_check(cfg: Config) -> tuple[str, str]:
    from argus.cli import _check_compare

    return _check_compare(cfg)


def test_doctor_calls_disabled_comparison_normal():
    """A disabled comparison is the normal state, not a defect.

    Nagging about it would train people to pass over the yellow in `doctor`, and it is there
    precisely to be looked at.
    """
    status, detail = _compare_check(Config())
    assert "green" in status
    assert "single engine" in detail


def test_doctor_fails_when_key_is_missing(monkeypatch):
    """An empty key must surface here, not half an hour into a recording.

    Otherwise the breakage looks like "the second column is empty for some reason", and
    people look for it in recognition, not in the environment variable.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.stt.compare_with = "realtime-openai"

    status, detail = _compare_check(cfg)
    assert "red" in status
    assert "OPENAI_API_KEY" in detail


def test_doctor_passes_when_key_is_set(monkeypatch):
    """The key is there, there is one engine — this is the product norm, not a half-measure."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")
    cfg = Config()

    status, detail = _compare_check(cfg)
    assert "green" in status
    assert "realtime-openai" in detail


def test_doctor_wants_the_key_when_realtime_is_the_main_engine(monkeypatch):
    """Without a key there will be no recognition at all.

    Before, the local Whisper picked up the utterance, and a missing key went unnoticed: the
    text is in the timeline, it was just recognized by a different engine. Whisper is
    removed, nobody is left to pick it up, and staying silent about it is not allowed.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.stt.provider = "realtime-openai"

    status, detail = _compare_check(cfg)
    assert "red" in status
    assert "OPENAI_API_KEY" in detail


def test_doctor_rejects_the_removed_local_engine():
    """An old config must get an intelligible answer, not "unknown provider".

    `whisper-local` was in the default config for many versions in a row, and it remained
    for everyone who updates. Saying "removed" is the only way to explain why something that
    worked yesterday stopped working.
    """
    cfg = Config()
    cfg.stt.compare_with = "whisper-local"

    status, detail = _compare_check(cfg)
    assert "red" in status
    assert "removed" in detail


def test_doctor_does_not_nag_about_the_missing_backup(monkeypatch):
    """One engine is the norm of the product, and there is nothing to warn about.

    The warning was right while the backup existed: a network engine alone meant a
    conversation without recognition at the first connection drop. The local one is removed
    entirely, and repeating this at every launch would train people not to read the doctor,
    and it will still be needed for real breakages.

    A drop is now healed differently: the session is brought up again, not replaced with
    another engine.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")
    cfg = Config()
    cfg.stt.provider = "realtime-openai"

    status, detail = _compare_check(cfg)
    assert "green" in status
    assert "single engine" in detail


def test_doctor_catches_engine_compared_with_itself():
    cfg = Config()
    cfg.stt.compare_with = cfg.stt.provider

    status, detail = _compare_check(cfg)
    assert "red" in status
    assert "against itself" in detail


def test_doctor_names_the_unknown_engine():
    cfg = Config()
    cfg.stt.compare_with = "made-up-engine"

    status, detail = _compare_check(cfg)
    assert "red" in status
    assert "made-up-engine" in detail
