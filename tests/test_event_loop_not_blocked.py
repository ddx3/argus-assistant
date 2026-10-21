"""Network calls to the account server do not hold the event loop.

`Account._call` is an ordinary synchronous `httpx.request`. Called directly from an
`async` function, it freezes the whole loop for the duration of the request: while the
model is thinking, the dashboard server serves neither "thinking" nor new frames nor
utterances; everything appears at once when the answer has already arrived, and the
person does not know what is going on. Here we check not the result of the request but
that while it runs, the rest of the loop lives.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from argus.account.client import Account
from argus.brain.relay_api import RelayProvider
from argus.core.config import BrainConfig, Config
from argus.tts.client import synthesize_via_relay


class SlowAccount:
    """A server that does not answer at once: `_call` sleeps like a real network."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def _call(self, method, path, **kw):
        time.sleep(self.delay)
        return SimpleNamespace(
            json=lambda: {"text": "answer", "input_tokens": 1, "output_tokens": 1},
            content=b"mp3",
        )


async def _ticks_while(coro) -> tuple[object, int]:
    """The result of `coro` and how many times a neighboring loop, which wakes up every 10 ms,
    managed to fire in that time. A frozen loop: 0-1 times.
    """
    stop = asyncio.Event()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await task
    return result, ticks


async def test_model_call_through_the_relay_does_not_freeze_the_event_loop():
    cfg = BrainConfig(provider="relay", relay_upstream="openai-api", api_model="m")
    provider = RelayProvider(cfg, SlowAccount(0.4))

    answer, ticks = await _ticks_while(provider.ask("question"))

    assert answer.ok and answer.text == "answer"
    # 0.4 s at 10 ms is about forty; a frozen loop would give zero.
    assert ticks >= 15, f"the event loop stood still during the model request (ticks: {ticks})"


async def test_speech_through_the_relay_does_not_freeze_the_event_loop():
    profile = SimpleNamespace(voice_id="v", stability=0.5, similarity_boost=0.5)
    settings = SimpleNamespace(model_id="m", output_format="mp3_44100_128")

    audio, ticks = await _ticks_while(
        synthesize_via_relay("hello", profile, settings, SlowAccount(0.4))
    )

    assert audio == b"mp3"
    assert ticks >= 15, f"the event loop stood still during the synthesis (ticks: {ticks})"


def test_the_dashboard_snapshot_does_not_wait_for_the_account_server(tmp_path, monkeypatch):
    """The snapshot is built twice a second on the event loop. Polling the account server right
    in it (about 100 ms, up to ten seconds on a bad connection) would hold the whole server
    every half second.
    """
    from argus.app import Argus
    from argus.ui.server import snapshot

    (tmp_path / "argus.account.json").write_text(
        json.dumps({"refresh": "r", "email": "user@example.com"}), encoding="utf-8"
    )
    calls = []

    def slow_call(self, method, path, **kw):
        calls.append(path)
        time.sleep(0.5)
        return SimpleNamespace(json=lambda: {
            "email": "user@example.com", "spent_seconds": 0,
            "right": {"allowed": False, "reason": "the minutes have run out"},
        })

    monkeypatch.setattr(Account, "_call", slow_call)
    cfg = Config()
    cfg.root = tmp_path
    cfg.account.server = "http://example.invalid"
    app = Argus(cfg, with_screen=False, with_brain=False)

    t0 = time.perf_counter()
    first = snapshot(app)
    waited = time.perf_counter() - t0

    assert waited < 0.2, f"the snapshot waited {waited:.2f} s for the account server"
    # No answer yet means no hint: a false refusal is worse than a late hint.
    assert first["account_hint"] == ""

    deadline = time.monotonic() + 3
    hint = ""
    while not hint and time.monotonic() < deadline:
        time.sleep(0.05)
        hint = snapshot(app)["account_hint"]
    assert hint == "the minutes have run out"

    # And after that the server is not poked on every snapshot: the answer lives for a dozen
    # seconds.
    before = len(calls)
    for _ in range(20):
        snapshot(app)
    assert len(calls) == before
