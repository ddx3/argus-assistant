"""RelayProvider: the same `ask()` as the other providers, but over the network to the
account server, not with a key from this computer.

A real server is started (`server.relay`), and `httpx` is replaced by a direct
`TestClient` call, the same way as in `test_account_client.py`. The real OpenAI is not
called: `_ask_openai` is replaced.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.account.client import Account
from argus.brain.relay_api import RelayProvider
from argus.core.config import BrainConfig
from argus.server import relay
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"


@pytest.fixture
def account(tmp_path, monkeypatch):
    db = connect(tmp_path / "accounts.sqlite")
    server = TestClient(build(db, SECRET))

    def request(method, url, **kw):
        kw.pop("timeout", None)
        return server.request(method, url.replace("http://server", ""), **kw)

    monkeypatch.setattr("argus.account.client.httpx.request", request)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda url, **kw: request("POST", url, **kw))

    acc = Account("http://server", tmp_path)
    acc.register("user@example.com", PASSWORD)
    return acc


@pytest.fixture
def cfg() -> BrainConfig:
    return BrainConfig(
        provider="relay", relay_upstream="openai-api",
        api_model="gpt-5.6-luna", deep_model="gpt-5.6-terra", effort="low", deep_effort="high",
    )


async def test_ask_reaches_upstream_through_the_relay(account, cfg, monkeypatch):
    seen = {}

    async def fake_openai(req):
        seen["model"] = req.model
        seen["effort"] = req.effort
        seen["prompt"] = req.prompt
        return relay.BrainAskResponse(text="hello from the relay", input_tokens=3, output_tokens=2)

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)

    provider = RelayProvider(cfg, account)
    answer = await provider.ask("question")

    assert answer.ok
    assert answer.text == "hello from the relay"
    assert answer.meta == {"input_tokens": 3, "output_tokens": 2}
    assert seen == {"model": "gpt-5.6-luna", "effort": "low", "prompt": "question"}


async def test_deep_picks_the_deep_model(account, cfg, monkeypatch):
    seen = {}

    async def fake_openai(req):
        seen["model"] = req.model
        seen["effort"] = req.effort
        return relay.BrainAskResponse(text="ok")

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)

    provider = RelayProvider(cfg, account)
    await provider.ask("question", deep=True)

    assert seen == {"model": "gpt-5.6-terra", "effort": "high"}


async def test_ask_without_right_returns_an_error_answer_not_a_crash(account, cfg, monkeypatch):
    async def unreachable(req):
        raise AssertionError("must not call the upstream without the right")

    monkeypatch.setattr(relay, "_ask_openai", unreachable)

    # An account without rights: the balance is empty.
    import sqlite3

    db_path = account.path.parent / "accounts.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()
    db.close()

    provider = RelayProvider(cfg, account)
    answer = await provider.ask("question")

    assert not answer.ok
    assert answer.error
    assert answer.text == ""
