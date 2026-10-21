"""The fallback engine `gpt-live-transcribe`: the same session breakages that were fixed in
`realtime-openai`.

The engine is dormant (`stt.compare_with` is empty), but the two share the code by origin,
and the defects too: a session closed by the server left the engine dead forever; an
utterance waited the whole timeout when the link broke in the middle of it; a refusal to
delete from the history brought down a live utterance. It is checked on the same fake
server as the main engine.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

import pytest
from test_realtime_stt import ask, server, shut, utterance  # noqa: F401 — server: a fixture

from argus.core.config import GPTLiveTranscribeConfig
from argus.perception.stt.gpt_live_transcribe import GPTLiveTranscribe, GPTLiveTranscribeError


@pytest.fixture
def cfg(server, monkeypatch) -> GPTLiveTranscribeConfig:  # noqa: F811
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789")
    return GPTLiveTranscribeConfig(url=server.url, timeout_s=5.0)


async def test_a_session_the_server_closed_mid_utterance_is_brought_back_at_once(server, cfg):  # noqa: F811
    """Before, the engine did not mark a session closed by the server as dead, and stayed silent
    until a restart, and the utterance on which the connection broke waited for the whole
    `timeout_s`."""
    server.close_on_commit = 1
    server.replies = ["after the drop"]
    stt = GPTLiveTranscribe(cfg)
    try:
        started = asyncio.get_running_loop().time()
        ev = await ask(stt, utterance())          # type: ignore[arg-type]
        waited = asyncio.get_running_loop().time() - started
        assert waited < 3.0, f"waited {waited:.1f} s although the session closed at once"
        assert ev is not None and ev.text == "after the drop"
        assert stt.revivals == 1
    finally:
        await shut(stt)                           # type: ignore[arg-type]


async def test_the_new_session_does_not_delete_the_old_sessions_items(server, cfg):  # noqa: F811
    cfg.keep_turns = 2
    server.replies = ["one", "two", "three", "four"]
    stt = GPTLiveTranscribe(cfg)
    try:
        for i in range(3):
            await ask(stt, utterance(start_s=i))  # type: ignore[arg-type]
        await asyncio.sleep(0.3)
        assert server.deleted == ["item_1"]
        assert all(e and e.startswith("trim-") for e in server.delete_events)

        assert await asyncio.to_thread(stt.reconnect, "check")
        assert stt._items == []

        assert (await ask(stt, utterance(start_s=4))).text == "four"   # type: ignore[arg-type,union-attr]
        await asyncio.sleep(0.3)
        assert server.deleted == ["item_1"]
    finally:
        await shut(stt)                           # type: ignore[arg-type]


async def test_a_late_answer_of_an_earlier_utterance_is_not_taken_for_the_current_one(server, cfg):  # noqa: F811
    server.ghost = True
    server.replies = ["first", "second"]
    stt = GPTLiveTranscribe(cfg)
    try:
        assert (await ask(stt, utterance())).text == "first"                    # type: ignore[arg-type,union-attr]
        assert (await ask(stt, utterance(start_s=2))).text == "second"          # type: ignore[arg-type,union-attr]
    finally:
        await shut(stt)                           # type: ignore[arg-type]


def test_a_refused_history_delete_does_not_fail_the_waiting_utterance():
    stt = GPTLiveTranscribe(GPTLiveTranscribeConfig())
    stt._pending = waiting = Future()
    stt._on_message({"type": "error", "error": {"code": "invalid_value", "event_id": "trim-1"}})
    stt._on_message({"type": "error", "error": {"code": "item_not_found"}})
    assert not waiting.done()

    stt._on_message({"type": "error", "error": {"code": "invalid_value", "message": "bad"}})
    assert waiting.done() and isinstance(waiting.exception(), GPTLiveTranscribeError)


def test_a_failed_send_ends_the_wait_and_marks_the_session_dead():
    stt = GPTLiveTranscribe(GPTLiveTranscribeConfig())
    stt._pending = waiting = Future()
    sending: Future = Future()
    sending.set_exception(ConnectionError("socket closed"))

    stt._on_sent(sending, waiting)

    assert waiting.done() and isinstance(waiting.exception(), GPTLiveTranscribeError)
    assert stt._dead.startswith("sending failed")


async def test_audio_is_marked_sent_only_once_it_left(server, cfg, monkeypatch):  # noqa: F811
    stt = GPTLiveTranscribe(cfg)
    try:
        assert stt.audio_sent is False
        assert await ask(stt, utterance()) is not None        # type: ignore[arg-type]
        assert stt.audio_sent is True
    finally:
        await shut(stt)                           # type: ignore[arg-type]

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    nokey = GPTLiveTranscribe(cfg)
    assert await ask(nokey, utterance()) is None              # type: ignore[arg-type]
    assert nokey.audio_sent is False                          # the audio never left the machine
