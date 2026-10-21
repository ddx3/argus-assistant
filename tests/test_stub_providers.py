"""Stubs for STT and the model: the pipeline must assemble and work without a network.

Not about quality (there is nothing to be wrong here, the text is fixed), but about the
stub honestly implementing the protocol: `build_stt`/`build_provider` find it by name,
`transcribe`/`ask` return what is set in the settings, and empty text still does not turn
into a phantom utterance.
"""

from __future__ import annotations

import numpy as np
import pytest

from argus.core.config import BrainConfig, Config, StubSTTConfig
from argus.core.events import SAMPLE_RATE, Source, Utterance

NS = 1_000_000_000


def utterance() -> Utterance:
    n = SAMPLE_RATE
    return Utterance(
        t=0, t_end=NS, source=Source.THEM,
        samples=(np.sin(np.arange(n) * 0.05) * 8000).astype(np.int16),
    )


# --------------------------------------------------------------------------
# STT
# --------------------------------------------------------------------------


def test_build_stt_finds_the_stub_by_name():
    from argus.perception.speech import build_stt
    from argus.perception.stt.stub import StubSTT

    cfg = Config()
    cfg.stt.provider = "stub"
    assert isinstance(build_stt(cfg), StubSTT)


def test_stub_stt_returns_the_configured_text():
    from argus.perception.stt.stub import StubSTT

    stt = StubSTT(StubSTTConfig(text="hello"))
    ev = stt.transcribe(utterance())
    assert ev is not None
    assert ev.text == "hello"
    assert ev.source is Source.THEM


def test_stub_stt_fills_in_the_call_count_and_source():
    from argus.perception.stt.stub import StubSTT

    stt = StubSTT(StubSTTConfig(text="line {n} from {source}"))
    first = stt.transcribe(utterance())
    second = stt.transcribe(utterance())
    assert first.text == "line 1 from them"
    assert second.text == "line 2 from them"


def test_stub_stt_empty_text_is_dropped_like_a_real_engine():
    from argus.perception.stt.stub import StubSTT

    stt = StubSTT(StubSTTConfig(text="   "))
    assert stt.transcribe(utterance()) is None


def test_stub_stt_needs_no_warmup_or_key():
    from argus.perception.stt.stub import StubSTT

    stt = StubSTT(StubSTTConfig())
    stt.warmup()          # must not throw: neither a key nor a network is required
    stt.close()


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


def test_build_provider_finds_the_stub_by_name():
    from argus.brain.assistant import build_provider
    from argus.brain.stub import StubProvider

    cfg = Config()
    cfg.brain.provider = "stub"
    assert isinstance(build_provider(cfg), StubProvider)


async def test_stub_brain_returns_the_configured_text():
    """`stub_text` only when `stub_answers` is empty: it takes priority over the default."""
    from argus.brain.stub import StubProvider

    cfg = BrainConfig(stub_text="test answer", stub_answers=[], stub_delay_ms=0)
    provider = StubProvider(cfg)
    answer = await provider.ask("never mind what is here")
    assert answer.text == "test answer"
    assert answer.ok


async def test_stub_brain_cycles_through_configured_answers():
    """By default `stub_answers` is not empty, and the answers do not repeat each other."""
    from argus.brain.stub import StubProvider

    cfg = BrainConfig(stub_answers=["one", "two", "three"], stub_delay_ms=0)
    provider = StubProvider(cfg)
    texts = [(await provider.ask("question")).text for _ in range(4)]
    assert texts == ["one", "two", "three", "one"]      # in a cycle


async def test_stub_brain_waits_the_configured_delay():
    import time

    from argus.brain.stub import StubProvider

    provider = StubProvider(BrainConfig(stub_delay_ms=120))
    started = time.perf_counter()
    await provider.ask("question")
    assert time.perf_counter() - started >= 0.1


async def test_stub_brain_needs_no_start_or_key():
    from argus.brain.stub import StubProvider

    provider = StubProvider(BrainConfig(stub_delay_ms=0))
    await provider.start()      # must not throw
    await provider.warm()
    await provider.close()


@pytest.mark.parametrize("bad", ["whisper-local", "not-a-provider"])
def test_unknown_stt_provider_still_fails_loudly(bad: str):
    """The stub must not quietly swallow other configuration errors."""
    from argus.perception.speech import build_stt

    cfg = Config()
    cfg.stt.provider = bad
    with pytest.raises(ValueError):
        build_stt(cfg)
