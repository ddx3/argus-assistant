"""`OpenAIAPIProvider.ask_stream`: the shape of the SSE events was not guessed from memory
but checked against the `openai` package (`types/responses/response_stream_event.py`),
and it is pinned here by a test, so that a package update cannot break it silently.

No network and no key: `_client.responses.create` is replaced by a fake that returns
preset events. They are `types.SimpleNamespace` objects, not real `openai` pydantic
models, but with the same fields that `ask_stream` actually reads (`.type`, `.delta`,
`.response.usage...`).
"""

from __future__ import annotations

from types import SimpleNamespace

from argus.brain.openai_api import OpenAIAPIProvider
from argus.core.config import BrainConfig


def delta_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def completed_event(
    input_tokens: int = 10, output_tokens: int = 5, cached_tokens: int = 0
) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )
    return SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=usage))


class FakeStream:
    """An async iterator over predefined events: the same thing that `AsyncStream` from `openai`
    gives, only without a network under it."""

    def __init__(self, events: list) -> None:
        self._events = list(events)

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class FakeResponses:
    def __init__(self, events: list) -> None:
        self._events = events
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._events)


class FakeClient:
    def __init__(self, events: list) -> None:
        self.responses = FakeResponses(events)


def provider(events: list) -> OpenAIAPIProvider:
    cfg = BrainConfig()
    cfg.api_model = "gpt-fake"
    p = OpenAIAPIProvider(cfg)
    p._client = FakeClient(events)
    return p


async def test_ask_stream_yields_text_deltas_in_order():
    p = provider([delta_event("Hel"), delta_event("lo"), completed_event()])
    chunks = [c async for c in p.ask_stream("question")]
    assert chunks == ["Hel", "lo"]


async def test_ask_stream_sends_stream_true_and_the_prompt():
    p = provider([completed_event()])
    async for _ in p.ask_stream("my question", system="sys"):
        pass

    call = p._client.responses.calls[0]
    assert call["stream"] is True
    assert call["model"] == "gpt-fake"
    assert call["instructions"] == "sys"
    assert call["input"][0]["content"][-1] == {"type": "input_text", "text": "my question"}


async def test_ask_stream_fills_info_from_the_completed_event():
    p = provider([
        delta_event("text"),
        completed_event(input_tokens=42, output_tokens=7, cached_tokens=3),
    ])
    info: dict = {}
    async for _ in p.ask_stream("question", info=info):
        pass

    assert info == {
        "model": "gpt-fake",
        "input_tokens": 42,
        "output_tokens": 7,
        "cached_tokens": 3,
    }


async def test_ask_stream_without_a_completed_event_still_reports_the_model():
    """A connection break before `response.completed`: the chunks that managed to arrive are not
    lost, and `info["model"]` is visible at once, not only on completion."""
    p = provider([delta_event("chunk")])
    info: dict = {}
    chunks = [c async for c in p.ask_stream("question", info=info)]

    assert chunks == ["chunk"]
    assert info["model"] == "gpt-fake"
    assert "input_tokens" not in info


async def test_ask_stream_picks_the_deep_model_when_asked():
    p = provider([completed_event()])
    p.cfg.deep_model = "gpt-fake-deep"
    p.cfg.deep_effort = "high"

    async for _ in p.ask_stream("question", deep=True):
        pass

    call = p._client.responses.calls[0]
    assert call["model"] == "gpt-fake-deep"
    assert call["reasoning"] == {"effort": "high"}


async def test_ask_stream_puts_the_screenshot_before_the_prompt():
    p = provider([completed_event()])
    async for _ in p.ask_stream("question", image=b"jpeg-bytes"):
        pass

    content = p._client.responses.calls[0]["input"][0]["content"]
    assert content[0]["type"] == "input_image"
    assert content[-1] == {"type": "input_text", "text": "question"}
