"""A provider through the subscription: the session lives long, but does not keep a stale
prompt and does not hang.

The real SDK is not needed: a stand-in module with the same structure is substituted (a
client with `connect/query/receive_response/disconnect`, messages with text blocks).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import ClassVar

import pytest

from argus.brain import claude_code
from argus.brain.claude_code import ClaudeCodeProvider
from argus.core.config import BrainConfig


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class AssistantMessage:
    def __init__(self, content) -> None:
        self.content = content


class Options:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class Client:
    instances: ClassVar[list[Client]] = []
    hang = False

    def __init__(self, options) -> None:
        self.options = options
        self.disconnected = False
        self.queries: list = []
        Client.instances.append(self)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, q) -> None:
        self.queries.append(q)

    async def receive_response(self):
        if Client.hang:
            await asyncio.sleep(3600)
        yield AssistantMessage([TextBlock(f"answer to the system \"{self.options.system_prompt}\"")])


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    Client.instances = []
    Client.hang = False
    sdk = types.ModuleType("claude_agent_sdk")
    sdk.ClaudeAgentOptions = Options
    sdk.ClaudeSDKClient = Client
    sdk.AssistantMessage = AssistantMessage
    sdk.TextBlock = TextBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)


def provider() -> ClaudeCodeProvider:
    return ClaudeCodeProvider(BrainConfig(provider="claude-code", model="sonnet"))


async def test_one_session_serves_questions_with_the_same_system_part():
    p = provider()
    await p.ask("one", system="system A")
    await p.ask("two", system="system A")
    assert len(Client.instances) == 1
    assert Client.instances[0].queries == ["one", "two"]


async def test_a_new_system_part_starts_a_new_session():
    """An edit of the profile or the materials, or the variant with a screen frame — otherwise
    they would not reach the model.
    """
    p = provider()
    first = await p.ask("one", system="system A")
    second = await p.ask("two", system="system B")

    assert len(Client.instances) == 2
    assert Client.instances[0].disconnected is True
    assert Client.instances[1].options.system_prompt == "system B"
    assert "system A" in first.text and "system B" in second.text


async def test_without_an_explicit_system_part_the_config_one_is_used():
    p = provider()
    p.cfg.system_prompt = "from the settings"
    await p.ask("one")
    p.cfg.system_prompt = "an edit"
    answer = await p.ask("two")
    assert len(Client.instances) == 2 and "an edit" in answer.text


async def test_closing_forgets_the_session_and_the_system_part():
    p = provider()
    await p.ask("one", system="system A")
    await p.close()
    assert p._client is None and p._system is None
    await p.ask("two", system="system A")
    assert len(Client.instances) == 2


async def test_a_hung_session_gives_an_error_and_is_dropped(monkeypatch):
    monkeypatch.setattr(claude_code, "CALL_TIMEOUT_S", 0.05)
    p = provider()
    Client.hang = True

    answer = await asyncio.wait_for(p.ask("one", system="system A"), 2)

    assert not answer.ok and answer.error
    assert Client.instances[0].disconnected is True
    assert p._client is None                 # an unread answer will not get mixed with the next one

    Client.hang = False
    again = await p.ask("two", system="system A")
    assert again.ok and len(Client.instances) == 2


async def test_the_subscription_session_never_gets_an_api_key_from_the_environment(monkeypatch):
    """`ANTHROPIC_API_KEY` (from `.env` or from the terminal) would switch the `claude-code`
    provider from the subscription to per-token billing — silently. The SDK overlays `env`
    on top of the inherited environment, so the variable is passed empty rather than absent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    p = provider()
    await p.ask("one", system="system A")

    env = Client.instances[0].options.env
    assert env == {"ANTHROPIC_API_KEY": ""}
    # The shared constant is not handed out itself: an edit in one session must not change the
    # others.
    assert env is not claude_code.SUBSCRIPTION_ENV
