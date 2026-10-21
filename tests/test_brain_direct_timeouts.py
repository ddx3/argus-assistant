"""Direct providers (`openai-api`, `anthropic-api`) set their own timeout and one retry.

The library defaults are ten minutes and two retries: a half-open connection would hold
`Assistant._busy` ("the assistant is still answering") for half an hour, and all
auto-replies would stand still the whole time.
"""

from __future__ import annotations

import sys
import types

from argus.brain.anthropic_api import AnthropicAPIProvider
from argus.brain.openai_api import OpenAIAPIProvider
from argus.core.config import BrainConfig


def _fake_module(monkeypatch, module: str, cls: str) -> list[dict]:
    created: list[dict] = []

    class Client:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

    fake = types.ModuleType(module)
    setattr(fake, cls, Client)
    monkeypatch.setitem(sys.modules, module, fake)
    return created


async def test_the_openai_client_gets_the_configured_timeout_and_one_retry(monkeypatch):
    created = _fake_module(monkeypatch, "openai", "AsyncOpenAI")
    await OpenAIAPIProvider(BrainConfig(timeout_s=45.0)).start()
    assert created == [{"timeout": 45.0, "max_retries": 1}]


async def test_the_anthropic_client_gets_the_configured_timeout_and_one_retry(monkeypatch):
    created = _fake_module(monkeypatch, "anthropic", "AsyncAnthropic")
    await AnthropicAPIProvider(BrainConfig(timeout_s=45.0)).start()
    assert created == [{"timeout": 45.0, "max_retries": 1}]
