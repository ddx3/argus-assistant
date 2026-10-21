"""Voiceover: text cleanup, cancelling paid synthesis, visible failures, voice trial
(core-session-2, -6, -7, -13, -14 and the notes from the client review).

Voiceover is paid per character, so mistakes here are money: code read aloud, synthesis
that keeps finishing after voiceover is turned off or the chat is switched, a voice trial
that can be triggered over and over. And an error without words for the person is "I
turned it on and there is no voice", and so forever.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from argus.brain.base import Answer
from argus.core import friendly
from argus.core.bus import Bus
from argus.tts import client, reply
from argus.tts.settings import TTSSettings, VoiceProfile
from argus.tts.text import speakable, take_ready_sentences


class App:
    def __init__(self, *, epoch: int | None = None, account=None) -> None:
        self.bus = Bus()
        if epoch is not None:
            self.assistant = SimpleNamespace(epoch=epoch)
        if account is not None:
            self._account = account


def settings(**over) -> TTSSettings:
    return TTSSettings(
        enabled=True, active_character="demo", characters={"demo": VoiceProfile(voice_id="v1")},
        **over,
    )


def said(text: str, t: int = 1, **meta) -> Answer:
    answer = Answer(text=text, provider="stub")
    answer.meta.update({"t": t, **meta})
    return answer


async def settle(ticks: int = 30) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Text: links, names, quotes, tables (core-session-13)
# --------------------------------------------------------------------------


def test_a_link_is_read_as_its_words_and_a_bare_address_as_its_site():
    out = speakable(
        "See [the documentation](https://example.com/a/very/long/path?id=12345&x=y) or "
        "https://example.com/other/page?q=1 and www.site.org/x.", 1000,
    )

    assert out == "See the documentation or example.com and site.org."
    assert "http" not in out and "?" not in out


def test_an_image_is_read_as_its_caption_not_its_address():
    assert speakable("Here ![network diagram](https://cdn.example.com/n.png) above", 1000) == (
        "Here network diagram above"
    )


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("the field my_config_value is set", "the field my_config_value is set"),   # a name, not italics
        ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),                             # multiplication, not italics
        ("a * b and c * d", "a * b and c * d"),
        ("this is _important_ and __very__", "this is important and very"),
        ("this is *important* and **very** and ***absolutely***", "this is important and very and absolutely"),
    ],
)
def test_emphasis_is_stripped_but_names_and_products_are_left_alone(source, spoken):
    assert speakable(source, 1000) == spoken


def test_quotes_rules_and_tables_are_not_read_as_symbols():
    out = speakable("> quote\n\n---\n\n| Who | What |\n|---|---|\n| Me | plan |\n| Ann | docs |", 1000)

    assert out == "quote\n\nWho, What\nMe, plan\nAnn, docs"
    for symbol in (">", "|", "---"):
        assert symbol not in out


def test_code_that_was_cut_off_before_its_closing_fence_is_dropped_too():
    # "код опущен" ("code omitted") is the Russian marker that voice-over inserts (tts/text.py), so it stays Russian.
    assert speakable("Here is the code ```python\nx = 1. 2.", 1000) == "Here is the code код опущен"


# --------------------------------------------------------------------------
# Streaming path: numbering and code (core-session-13, -7)
# --------------------------------------------------------------------------


def test_numbered_steps_are_not_cut_after_their_numbers():
    text = "Steps:\n1. Open the window\n2. Press the button"

    assert take_ready_sentences(text, final=False) == ([], 0)         # we wait, rather than cutting at "1."
    pieces, used = take_ready_sentences(text, final=True)
    assert pieces == [text] and used == len(text)
    assert speakable(pieces[0], 1000) == "Steps:\nOpen the window\nPress the button"


def test_steps_that_are_whole_sentences_still_split_between_items():
    pieces, _ = take_ready_sentences("1. Open the window. 2. Press the button. 3. Wait", final=False)

    assert pieces == ["1. Open the window.", "2. Press the button."]


def test_punctuation_inside_a_code_block_does_not_end_a_sentence():
    text = "Here is the code:\n```py\nprint('a. b? c')\n```\nDone. Next"

    pieces, used = take_ready_sentences(text, final=False)

    assert pieces == ["Here is the code:\n```py\nprint('a. b? c')\n```\nDone."]
    assert text[used:] == "Next"
    assert speakable(pieces[0], 1000) == "Here is the code:\n код опущен \nDone."


def test_text_after_an_unclosed_fence_waits_for_the_closing_one():
    head = "Here is code. More:\n```py\nprint('a. b? c')\nx = 2. "

    pieces, used = take_ready_sentences(head, final=False)
    assert pieces == ["Here is code."] and head[used:].startswith("More:")      # the block has not arrived in full yet

    pieces, _ = take_ready_sentences(head + "\n```\nThe end. ", final=False)
    assert pieces == ["Here is code.", "More:\n```py\nprint('a. b? c')\nx = 2. \n```\nThe end."]


async def test_the_streaming_path_never_speaks_the_code(monkeypatch):
    spoken: list[str] = []

    async def fake(text, profile, settings, api_key):
        spoken.append(text)
        return b"mp3"

    monkeypatch.setattr(reply.client, "synthesize", fake)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    monkeypatch.setattr(reply, "POLL_S", 0)
    app = App()
    watcher = reply.install(app, settings())
    answer = Answer(text="", provider="stub", done=False)
    answer.meta["t"] = 5
    app.bus.publish(answer)

    for part in ("Here is a code sample. ", "Look:\n```py\nprint('a. ", "b? c')\nx = 1. ", "\n```\nDone."):
        answer.text += part
        await settle()
    answer.done = True
    await settle(60)

    assert not any("print" in s or "```" in s for s in spoken), spoken
    assert sum("код опущен" in s for s in spoken) == 1      # the Russian "code omitted" marker, see tts/text.py
    watcher.stop()
    await settle()


# --------------------------------------------------------------------------
# Paid synthesis is not finished for nothing (core-session-6, notes from the client review)
# --------------------------------------------------------------------------


async def test_stopping_cancels_the_synthesis_that_is_in_flight(monkeypatch):
    started, cancelled = asyncio.Event(), []

    async def slow(text, profile, settings, api_key):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(text)
            raise
        return b"mp3"

    monkeypatch.setattr(reply.client, "synthesize", slow)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    app = App()
    watcher = reply.install(app, settings())
    app.bus.publish(said("This sentence went into synthesis."))
    await asyncio.wait_for(started.wait(), 2)

    reply.install(app, TTSSettings(enabled=False))         # voiceover was turned off
    await settle()

    assert cancelled == ["This sentence went into synthesis."]
    assert not watcher._tasks, "stopped voiceover still holds tasks"


async def test_the_answer_of_a_closed_conversation_is_not_spoken(monkeypatch):
    calls = []

    async def fake(text, profile, settings, api_key):
        calls.append(text)
        return b"mp3"

    monkeypatch.setattr(reply.client, "synthesize", fake)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    app = App(epoch=2)
    watcher = reply.install(app, settings())

    app.bus.publish(said("The answer of the previous chat.", epoch=1))      # the chat was switched while it was running
    app.bus.publish(said("The answer of this chat.", t=2, epoch=2))
    await settle()

    assert calls == ["The answer of this chat."]
    watcher.stop()
    await settle()


async def test_the_rest_of_an_answer_is_dropped_when_the_chat_changes_midway(monkeypatch):
    calls = []
    app = App(epoch=1)

    async def fake(text, profile, settings, api_key):
        calls.append(text)
        app.assistant.epoch += 1                    # the person opened another chat
        return b"mp3"

    monkeypatch.setattr(reply.client, "synthesize", fake)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    watcher = reply.install(app, settings())
    app.bus.publish(said(
        "This first sentence is made long on purpose. "
        "This second sentence is also made long on purpose. "
        "This third sentence is also made long on purpose.", epoch=1,
    ))
    await settle(60)

    assert calls == ["This first sentence is made long on purpose."]
    watcher.stop()
    await settle()


async def test_the_relay_call_runs_in_a_daemon_thread_and_a_late_answer_is_dropped():
    """A pool thread holds the process at exit until the request ends (up to ≈160 s)."""
    seen: dict = {}
    release = threading.Event()

    class Account:
        def _call(self, method, path, *, auth, json):
            seen["daemon"] = threading.current_thread().daemon
            seen["path"] = path
            release.wait(5)
            return SimpleNamespace(content=b"audio")

    profile = VoiceProfile(voice_id="v1")
    task = asyncio.create_task(client.synthesize_via_relay("text", profile, settings(), Account()))
    await asyncio.sleep(0.1)
    task.cancel()                                           # voiceover was stopped, the request is still running
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()                                           # a late result goes to nobody, without noise
    await asyncio.sleep(0.1)

    assert seen == {"daemon": True, "path": "/relay/tts/synthesize"}


async def test_the_relay_call_still_returns_the_audio():
    class Account:
        def _call(self, method, path, *, auth, json):
            return SimpleNamespace(content=b"audio")

    got = await client.synthesize_via_relay("text", VoiceProfile(voice_id="v1"), settings(), Account())

    assert got == b"audio"


# --------------------------------------------------------------------------
# A voiceover failure is visible to the person (core-session-14)
# --------------------------------------------------------------------------


class Refused(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("")
        self.response = SimpleNamespace(status_code=status)


async def test_a_refused_voice_is_told_to_the_person_and_not_retried_until_something_changes(
    monkeypatch,
):
    calls = []

    async def refuse(text, profile, settings, api_key):
        calls.append(text)
        raise Refused(401)

    monkeypatch.setattr(reply.client, "synthesize", refuse)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    app = App()
    watcher = reply.install(app, settings())

    app.bus.publish(said("First answer.", t=1))
    await settle()
    assert watcher.problem() == f"voice replies: {friendly.ACCESS}"
    assert reply.problems(app) == [f"voice replies: {friendly.ACCESS}"]

    app.bus.publish(said("Second answer.", t=2))
    app.bus.publish(said("Third answer.", t=3))
    await settle()
    assert calls == ["First answer."], "the refusal is repeated on every answer"

    reply.install(app, settings(max_chars=400))          # the settings were changed, so it is fine to try again
    assert watcher.problem() == ""
    app.bus.publish(said("Fourth answer.", t=4))
    await settle()
    assert calls == ["First answer.", "Fourth answer."]
    watcher.stop()
    await settle()


async def test_a_refusal_is_retried_after_the_pause_even_when_nothing_changed(monkeypatch):
    """The balance could have been topped up while the settings stayed the same."""
    calls = []

    async def refuse(text, profile, settings, api_key):
        calls.append(text)
        raise Refused(402)

    monkeypatch.setattr(reply.client, "synthesize", refuse)
    monkeypatch.setattr(reply, "HALT_S", 0.4)          # real time, not a patched clock
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    app = App()
    watcher = reply.install(app, settings())
    app.bus.publish(said("One.", t=1))
    await settle()
    app.bus.publish(said("At once.", t=5))                # the pause has not passed yet
    await settle()
    assert calls == ["One."]
    await asyncio.sleep(0.5)
    app.bus.publish(said("Two.", t=2))
    await settle()

    assert calls == ["One.", "Two."]
    watcher.stop()
    await settle()


async def test_a_network_failure_is_reported_but_does_not_stop_the_next_try(monkeypatch):
    calls = []

    async def flaky(text, profile, settings, api_key):
        calls.append(text)
        if len(calls) == 1:
            raise ConnectionError("connection refused")
        return b"mp3"

    monkeypatch.setattr(reply.client, "synthesize", flaky)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    app = App()
    watcher = reply.install(app, settings())
    app.bus.publish(said("First.", t=1))
    await settle()
    assert watcher.problem() == f"voice replies: {friendly.NETWORK}"

    app.bus.publish(said("Second.", t=2))
    await settle()
    assert calls == ["First.", "Second."]
    assert watcher.problem() == "" and reply.problems(app) == []      # it works again, so the complaint is cleared
    watcher.stop()
    await settle()


async def test_voice_on_without_a_key_or_a_login_says_so_in_plain_words(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    app = App()
    watcher = reply.install(app, settings())
    app.bus.publish(said("Hello."))
    await settle()
    assert watcher.problem() == f"voice replies: {reply.NOT_SET_UP}"
    watcher.stop()

    relay_app = App(account=SimpleNamespace(signed_in=False))
    relay = reply.install(relay_app, settings(relay=True))
    relay_app.bus.publish(said("Hello."))
    await settle()
    assert relay.problem() == f"voice replies: {reply.SIGN_IN}"
    relay.stop()
    await settle()

    for phrase in (reply.NOT_SET_UP, reply.SIGN_IN):
        assert friendly.plain(phrase), phrase                # for the person: no keys and no services
        assert not any(w in phrase.lower() for w in ("key", "elevenlabs", "api", "relay", "token"))


def test_a_watcher_that_never_failed_has_no_complaint():
    class Nothing:
        pass

    assert reply.problems(Nothing()) == []


# --------------------------------------------------------------------------
# The voice trial is not endless (core-session-2)
# --------------------------------------------------------------------------


def preview_client(monkeypatch, *, delay: float = 0.0):
    calls = []

    async def fake(text, profile, settings, api_key):
        calls.append(text)
        await asyncio.sleep(delay)
        return b"clip"

    monkeypatch.setattr(reply.client, "synthesize", fake)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    cfg = SimpleNamespace(channel={"tts": {"characters": {"demo": {"voice_id": "v1"}}}})
    web = FastAPI()
    reply.routes(SimpleNamespace(cfg=cfg))(web)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test")
    return http, calls


async def test_the_preview_is_not_more_often_than_once_in_a_couple_of_seconds(monkeypatch):
    http, calls = preview_client(monkeypatch)
    async with http:
        first = await http.post("/tts/preview", json={"character": "demo"})
        second = await http.post("/tts/preview", json={"character": "demo"})

    assert first.status_code == 200 and first.content == b"clip"       # without voiceover turned on
    assert second.status_code == 429 and second.json()["detail"] == reply.PREVIEW_BUSY
    assert len(calls) == 1, "the second trial still went into paid synthesis"
    assert friendly.plain(reply.PREVIEW_BUSY)


async def test_a_preview_goes_again_after_the_pause(monkeypatch):
    http, calls = preview_client(monkeypatch)
    monkeypatch.setattr(reply, "PREVIEW_GAP_S", 0.05)   # real time, not a patched clock
    async with http:
        await http.post("/tts/preview", json={"character": "demo"})
        await asyncio.sleep(0.1)
        again = await http.post("/tts/preview", json={"character": "demo"})

    assert again.status_code == 200 and len(calls) == 2


async def test_two_previews_at_once_make_one_synthesis(monkeypatch):
    http, calls = preview_client(monkeypatch, delay=0.2)
    async with http:
        a, b = await asyncio.gather(
            http.post("/tts/preview", json={"character": "demo"}),
            http.post("/tts/preview", json={"character": "demo"}),
        )

    assert sorted([a.status_code, b.status_code]) == [200, 429]
    assert len(calls) == 1


async def test_a_wrong_request_is_still_refused_with_its_own_reason_not_the_pause(monkeypatch):
    http, _ = preview_client(monkeypatch)
    async with http:
        first = await http.post("/tts/preview", json={"character": "no such voice"})
        second = await http.post("/tts/preview", json={"character": "no such voice"})

    assert first.status_code == second.status_code == 404
