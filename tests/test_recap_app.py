"""The recap in the running app: words in the ask line -> a card in the timeline.

The recap engine is tested separately (`test_recap.py`). Here is the glue: that the line
"recap" does not go to the model as a question, and an ordinary question is not
swallowed as a command; that the card lands in the same timeline as the answers; that the
recap takes the conversation from the chat's journal, not from the live timeline of
fifteen minutes; that switching the chat in the middle of the count does not put the recap
into someone else's chat.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from argus.app import Argus
from argus.brain.base import Answer
from argus.chats import manager_of
from argus.core.config import Config
from argus.core.events import Source, SpeechEvent
from argus.recap import chat, intercept, recap_of, service
from argus.recap.service import LIVE_NOTE
from argus.ui.server import STATIC, build

DIALOGUE = [
    ("them", "Let's discuss the release: when can we realistically ship the new version?"),
    ("me", "If the tests pass this week, then on the twentieth of October."),
    ("them", "Good, the twentieth. The testing budget stays the same, one hundred thousand."),
    ("me", "Agreed. I will send the testing plan by Friday."),
]

JPEG = b"\xff\xd8\xff" + b"x" * 64


class Recorder:
    """A model that records requests. `gate` delays the answer until a signal."""

    name = "recorder"

    def __init__(self, text: str = "RECAP", *, error: str | None = None, on_ask=None) -> None:
        self.prompts: list[str] = []
        self.text = text
        self.error = error
        self.on_ask = on_ask
        self.gate: asyncio.Event | None = None

    async def start(self, system=None) -> None: ...

    async def warm(self, system=None, pinned=()) -> None: ...

    async def close(self) -> None: ...

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False):
        self.prompts.append(prompt)
        if self.on_ask is not None:
            self.on_ask()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            return Answer(text="", provider=self.name, error=self.error)
        return Answer(text=self.text, provider=self.name, model="m")


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def folder(self, chat_id: str) -> Path:
        return self.root / chat_id

    def history(self, chat_id: str) -> list[dict]:
        path = self.folder(chat_id) / "history.jsonl"
        if not path.is_file():
            return []
        return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines() if row]


def make_app(root: Path, *, chats: bool = False, **model) -> tuple[Argus, Recorder]:
    cfg = Config()
    cfg.root = root
    cfg.brain.provider = "stub"
    cfg.ui.admin = True
    if chats:
        cfg.chats_dir.mkdir()
    app = Argus(cfg, with_screen=False, with_brain=True)
    recorder = Recorder(**model)
    app.assistant.provider = recorder
    return app, recorder


def speak(folder: Path, lines=DIALOGUE) -> None:
    """Append utterances to the chat journal in the form the chats write them.

    Time grows like in a real journal: each batch is later than the previous one.
    """
    journal = folder / "history.jsonl"
    last = max((r["ts"] for r in FakeStore(folder.parent).history(folder.name)), default=0.0)
    start = max(time.time() - 600, last + 5)
    with journal.open("a", encoding="utf-8") as f:
        for i, (source, text) in enumerate(lines):
            record = {"k": "s", "ts": start + i * 5, "src": source, "text": text}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


@pytest.fixture
def open_chat(tmp_path, monkeypatch) -> Path:
    """An open chat `abc` with a ready conversation; returns its folder."""
    folder = tmp_path / "abc"
    folder.mkdir()
    speak(folder)
    monkeypatch.setattr(chat, "active", lambda app: ("abc", FakeStore(tmp_path)))
    return folder


# --------------------------------------------------------------------------
# Command -> card
# --------------------------------------------------------------------------


async def test_the_word_recap_becomes_a_card_made_from_the_chat_journal(tmp_path, open_chat):
    app, model = make_app(tmp_path)
    cards = app.bus.subscribe(Answer, name="test")

    answer = await app.ask("recap")

    assert answer.ok and answer.text == "RECAP"
    assert answer.meta["cause"] == "Summarize the conversation"
    assert answer.meta["epoch"] == app.assistant.epoch
    # The conversation is taken from the journal whole, with timestamps and with who was
    # speaking.
    assert "twentieth of October" in model.prompts[0]
    assert "Them: Let's discuss the release" in model.prompts[0]
    assert "Me: Agreed" in model.prompts[0]
    # The card went over the bus (from there the page's timeline and the chat journal will pick
    # it up).
    assert cards.qsize() == 1 and await cards.get() is answer
    assert app.assistant.answers[-1] is answer


async def test_an_ordinary_question_still_goes_to_the_model(tmp_path, open_chat):
    app, model = make_app(tmp_path)

    answer = await app.ask("what is the GIL?")

    assert answer.ok
    assert "# Request\nwhat is the GIL?" in model.prompts[0]
    assert "Transcript" not in model.prompts[0]


async def test_a_photo_with_the_word_is_a_question_about_the_photo_not_a_command(tmp_path, open_chat):
    app, model = make_app(tmp_path)

    await app.ask("recap", image=JPEG)

    assert "# Request\nrecap" in model.prompts[0]


async def test_a_watched_topic_is_remembered_and_comes_back_in_the_recap(tmp_path, open_chat):
    app, model = make_app(tmp_path)

    ack = await app.ask("watch for deadlines and budget")

    assert ack.ok and "deadlines and budget" in ack.text
    assert model.prompts == [], "remembering a topic is free, the model is not needed here"
    assert json.loads((open_chat / "recap.json").read_text(encoding="utf-8")) == {
        "topics": ["deadlines and budget"],
    }

    await app.ask("recap")

    assert "The user asked you to watch these topics" in model.prompts[0]
    assert "- deadlines and budget" in model.prompts[0]


async def test_a_recap_of_one_topic_ignores_the_watched_ones(tmp_path, open_chat):
    app, model = make_app(tmp_path)
    await app.ask("watch for deadlines")

    answer = await app.ask("summary of the call about budget")

    assert answer.meta["cause"] == "Summary of the call about budget"
    assert 'only one topic: "budget"' in model.prompts[0]
    assert "The user asked you to watch" not in model.prompts[0]


async def test_the_card_label_is_itself_a_command_so_that_retry_works(tmp_path, open_chat):
    """The "once more" button on the card sends its "question" as is."""
    app, model = make_app(tmp_path)
    first = await app.ask("summary of the call about budget")

    again = await app.ask(first.meta["cause"])

    assert again.meta["cause"] == first.meta["cause"]
    assert len(model.prompts) == 2 and 'only one topic: "budget"' in model.prompts[1]


async def test_topics_can_be_listed_and_removed_with_words(tmp_path, open_chat):
    app, _ = make_app(tmp_path)
    assert "Not watching anything" in (await app.ask("what are you watching?")).text
    await app.ask("watch for price, deadlines")

    listed = await app.ask("what are you watching?")
    gone = await app.ask("stop watching price")
    cleared = await app.ask("stop watching")

    assert "**price**" in listed.text and "**deadlines**" in listed.text
    assert "No longer watching: **price**" in gone.text and "Remaining: **deadlines**" in gone.text
    assert cleared.text == "Topics cleared."


async def test_nothing_said_means_no_request_and_a_polite_card(tmp_path, monkeypatch):
    (tmp_path / "abc").mkdir()
    monkeypatch.setattr(chat, "active", lambda app: ("abc", FakeStore(tmp_path)))
    app, model = make_app(tmp_path)

    answer = await app.ask("recap")

    assert answer.ok and "Too little has been said" in answer.text
    assert model.prompts == []


async def test_a_failed_recap_is_still_a_card_so_the_page_stops_waiting(tmp_path, open_chat):
    """The page draws "thinking" itself and removes it when the feed gains cards. A recap that went
    out silently would leave it waiting forever."""
    app, _ = make_app(tmp_path, error="request limit")
    cards = app.bus.subscribe(Answer, name="test")

    answer = await app.ask("recap")

    assert not answer.ok
    assert answer.error == "The summary didn't work out: request limit"
    assert cards.qsize() == 1


async def test_a_second_recap_is_refused_while_the_first_is_running(tmp_path, open_chat):
    app, model = make_app(tmp_path)
    model.gate = asyncio.Event()
    first = asyncio.create_task(app.ask("recap"))
    await asyncio.sleep(0.01)

    second = await app.ask("recap")
    model.gate.set()
    done = await first

    assert second.error == "a summary is already being prepared"
    assert done.ok and len(model.prompts) == 1


async def test_thinking_is_shown_while_the_recap_runs_and_cleared_after(tmp_path, open_chat):
    seen = {}
    app, model = make_app(tmp_path)
    model.on_ask = lambda: seen.update(thinking=dict(app.assistant.pending or {}))

    await app.ask("recap")

    assert seen["thinking"]["cause"] == "Summarize the conversation"
    assert seen["thinking"]["kind"] == "recap"
    assert app.assistant.pending is None


async def test_the_live_answer_in_flight_keeps_its_own_thinking_mark(tmp_path, open_chat):
    app, _ = make_app(tmp_path)
    mine = {"t": 1, "cause": "another question"}
    app.assistant.pending = mine

    await app.ask("recap")

    assert app.assistant.pending is mine


async def test_a_recap_that_finished_in_another_chat_is_not_shown_here(tmp_path, open_chat):
    app, model = make_app(tmp_path)
    model.on_ask = lambda: setattr(app.assistant, "epoch", app.assistant.epoch + 1)
    cards = app.bus.subscribe(Answer, name="test")

    await app.ask("recap")

    assert cards.qsize() == 0
    assert app.assistant.answers == []


async def test_without_a_chat_the_recap_reads_the_live_timeline_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "active", lambda app: None)
    app, model = make_app(tmp_path)
    for i, (source, text) in enumerate(DIALOGUE):
        app.assistant.timeline.add_speech(SpeechEvent(
            t=i * 5_000_000_000, t_end=i * 5_000_000_000 + 1, source=Source(source), text=text,
        ))

    answer = await app.ask("recap")

    assert answer.ok
    assert answer.text.startswith(LIVE_NOTE)
    assert "twentieth of October" in model.prompts[0]


async def test_topics_without_a_chat_live_until_the_program_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "active", lambda app: None)
    app, _ = make_app(tmp_path)

    ack = await app.ask("watch for deadlines")

    assert "The session is not open" in ack.text
    assert recap_of(app).topics.get() == ["deadlines"]


async def test_recap_cards_are_not_read_aloud_even_when_the_voice_is_on(
    tmp_path, open_chat, monkeypatch,
):
    """Speech output is paid per character, and the recap is long and was asked for to be read by
    eye.
    """
    from argus.tts import reply
    from argus.tts.settings import TTSSettings, VoiceProfile

    spoken = []

    async def fake_synthesize(text, profile, settings, api_key):
        spoken.append(text)
        return b"mp3"

    monkeypatch.setattr(reply.client, "synthesize", fake_synthesize)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    app, _ = make_app(tmp_path)
    watcher = reply.install(app, TTSSettings(
        enabled=True, active_character="demo", characters={"demo": VoiceProfile(voice_id="v1")},
    ))

    recap = await app.ask("recap")
    ack = await app.ask("watch for deadlines")
    # An ordinary answer alongside, to show that the voice-over works at all.
    ordinary = Answer(text="Ordinary answer", provider="stub", meta={"t": 777})
    app.bus.publish(ordinary)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if spoken:
            break

    assert recap.ok and ack.ok
    assert spoken == ["Ordinary answer"]
    watcher.stop()


async def test_intercept_leaves_ordinary_text_alone(tmp_path, open_chat):
    app, model = make_app(tmp_path)

    assert await intercept(app, "explain what an index is") is None
    assert await intercept(app, None) is None
    assert model.prompts == []


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


async def _client(app: Argus) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build(app)), base_url="http://test")


async def test_the_ask_endpoint_carries_the_command_through_the_server(tmp_path, open_chat):
    app, _ = make_app(tmp_path)
    async with await _client(app) as client:
        r = await client.post("/ask", json={"question": "recap"})

    assert r.status_code == 200
    assert r.json()["text"] == "RECAP" and r.json()["cause"] == "Summarize the conversation"


async def test_topics_endpoint_reads_and_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "active", lambda app: None)
    app, _ = make_app(tmp_path)
    async with await _client(app) as client:
        empty = (await client.get("/recap/topics")).json()
        put = (await client.put("/recap/topics", json={"topics": "deadlines, budget, Deadlines"})).json()
        now = (await client.get("/recap/topics")).json()
        bad = await client.put("/recap/topics", json={"topics": 5})

    assert empty == {"topics": [], "chat": None, "max": 10, "busy": False}
    assert put == {"ok": True, "topics": ["deadlines", "budget"]}
    assert now["topics"] == ["deadlines", "budget"]
    assert bad.status_code == 400


async def test_the_button_endpoint_runs_a_recap_and_reports_failure(tmp_path, open_chat):
    app, model = make_app(tmp_path)
    cards = app.bus.subscribe(Answer, name="test")
    async with await _client(app) as client:
        ok = (await client.post("/recap")).json()
        topic = (await client.post("/recap", json={"topic": "budget"})).json()
        model.error = "request limit"
        failed = (await client.post("/recap")).json()

    assert ok == {"ok": True, "error": "", "shown": True}
    assert topic == {"ok": True, "error": "", "shown": True}
    assert 'only one topic: "budget"' in model.prompts[1]
    # The error also lands in the timeline as a card, so the page does not repeat it.
    assert failed == {"ok": False, "error": "The summary didn't work out: request limit", "shown": True}
    assert cards.qsize() == 3


async def test_the_button_is_told_when_there_will_be_no_card(tmp_path, open_chat):
    """"A summary is already being prepared" does not get into the feed: the
    button must be told about it.
    """
    app, model = make_app(tmp_path)
    model.gate = asyncio.Event()
    async with await _client(app) as client:
        first = asyncio.create_task(client.post("/recap"))
        await asyncio.sleep(0.05)
        second = (await client.post("/recap")).json()
        model.gate.set()
        await first

    assert second == {"ok": False, "error": "a summary is already being prepared", "shown": False}


def test_the_page_carries_the_recap_controls():
    """Several people work in `index.html`, and a piece of someone else's block is easily carried
    away together with a neighboring edit. A button without a handler is dead, and a handler
    without a button brings down the whole page script (see `test_page_wiring`): here we check
    that both are in place and look at the right addresses.
    """
    page = (STATIC / "index.html").read_text(encoding="utf-8")

    for element in ("recap-btn", "recap-pop", "recap-run", "recap-topics", "recap-err"):
        assert f'id="{element}"' in page, element
    assert 'fetch("/recap/topics"' in page
    # The local server does not accept a POST without a body (a foreign page must not press this
    # for the person), so there is always a body.
    assert 'await (await fetch("/recap", {\n      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",\n    })).json()' in page
    # The window closes at once and comes back only on a refusal without a card.
    assert "!out.ok && !out.shown" in page
    # The ask line in a detached window no longer slides out (since 2026-10-07 it is always
    # shown), and the recap popup does not need to keep it open.
    assert "inUse" not in page
    # While the recap is being prepared, the "thinking" card says "preparing", not
    # "replying to".
    assert 'wait.kind === "recap" ? "preparing:" : "replying to:"' in page


# --------------------------------------------------------------------------
# End of the conversation: recognition switched off
# --------------------------------------------------------------------------


async def _stop_recognition(app: Argus) -> None:
    """The person pressed the switch, the way the page does it."""
    app.running = True
    async with await _client(app) as client:
        r = await client.post("/control", json={"running": False})
    assert r.status_code == 200


async def _wait_for_cards(cards, count: int, *, limit: float = 1.0) -> None:
    for _ in range(int(limit / 0.01)):
        if cards.qsize() >= count:
            return
        await asyncio.sleep(0.01)


@pytest.fixture
def quick_stop(monkeypatch):
    monkeypatch.setattr(service, "AUTO_DELAY_S", 0)


async def test_switching_recognition_off_sends_the_recap_when_topics_were_set(
    tmp_path, open_chat, quick_stop,
):
    app, model = make_app(tmp_path)
    speak(open_chat)                                     # enough utterances have accumulated
    await app.ask("watch for deadlines")
    cards = app.bus.subscribe(Answer, name="test")

    await _stop_recognition(app)
    await _wait_for_cards(cards, 1)

    card = await cards.get()
    assert card.ok and card.text == "RECAP"
    assert card.meta["cause"] == "Summarize the conversation"
    assert card.meta["reason"] == "recognition is off"
    assert "- deadlines" in model.prompts[0]


async def test_without_topics_switching_off_costs_nothing(tmp_path, open_chat, quick_stop):
    app, model = make_app(tmp_path)
    speak(open_chat)

    await _stop_recognition(app)
    await asyncio.sleep(0.1)

    assert model.prompts == []


async def test_a_pause_with_almost_nothing_new_does_not_ask_for_a_recap(
    tmp_path, open_chat, quick_stop,
):
    app, model = make_app(tmp_path)                      # the journal has only four utterances
    await app.ask("watch for deadlines")

    await _stop_recognition(app)
    await asyncio.sleep(0.1)

    assert model.prompts == []


async def test_the_same_recap_is_not_repeated_until_there_is_something_new(
    tmp_path, open_chat, quick_stop,
):
    app, model = make_app(tmp_path)
    speak(open_chat)
    await app.ask("watch for deadlines")
    cards = app.bus.subscribe(Answer, name="test")

    await _stop_recognition(app)
    await _wait_for_cards(cards, 1)
    await _stop_recognition(app)                         # a break with no new words
    await asyncio.sleep(0.1)
    assert len(model.prompts) == 1

    speak(open_chat)
    speak(open_chat)                                     # we talked some more: new utterances have accumulated
    await _stop_recognition(app)
    await _wait_for_cards(cards, 2)

    assert len(model.prompts) == 2


async def test_a_manual_recap_counts_as_the_last_one(tmp_path, open_chat, quick_stop):
    app, model = make_app(tmp_path)
    speak(open_chat)
    await app.ask("watch for deadlines")
    await app.ask("recap")

    await _stop_recognition(app)
    await asyncio.sleep(0.1)

    assert len(model.prompts) == 1


async def test_stopping_by_any_other_way_does_not_start_a_recap(tmp_path, open_chat, quick_stop):
    """A chat change and a sound failure also turn recognition off, but not from the remote: the
    recap of the departing conversation would have nowhere to go and would cost money for
    nothing."""
    app, model = make_app(tmp_path)
    speak(open_chat)
    await app.ask("watch for deadlines")
    app.running = True

    app.set_running(False)
    await asyncio.sleep(0.1)

    assert model.prompts == []


async def test_a_recap_after_stop_is_dropped_when_another_chat_was_opened_meanwhile(
    tmp_path, open_chat, monkeypatch,
):
    monkeypatch.setattr(service, "AUTO_DELAY_S", 0.05)
    app, model = make_app(tmp_path)
    speak(open_chat)
    await app.ask("watch for deadlines")
    cards = app.bus.subscribe(Answer, name="test")

    await _stop_recognition(app)
    app.assistant.epoch += 1                             # another chat was opened
    await asyncio.sleep(0.2)

    assert model.prompts == [] and cards.qsize() == 0


# --------------------------------------------------------------------------
# Real chats
# --------------------------------------------------------------------------


async def test_what_the_chats_write_is_what_the_recap_reads(tmp_path):
    """The only test at the joint with real chats: the journal is written by the chat manager and
    read by the recap. If the format of an utterance record changes, it will fail here."""
    app, model = make_app(tmp_path, chats=True)
    manager = manager_of(app)
    created = manager.create("standup call")
    await manager.activate(created.id)
    recorder = asyncio.create_task(manager.run())
    await asyncio.sleep(0)
    try:
        # The time of the utterances is by the app's clock and after the chat was opened: an
        # utterance that ended before it counts as belonging to a past conversation and does not get
        # into the journal of the new one (`ChatManager._watch_speech`).
        now = app.clock.now() + 1_000_000
        for i, (source, text) in enumerate(DIALOGUE):
            app.bus.publish(SpeechEvent(t=now + i, t_end=now + i + 1,
                                        source=Source(source), text=text))
        await asyncio.sleep(0.1)

        await app.ask("watch for deadlines")
        answer = await app.ask("recap")
    finally:
        recorder.cancel()

    assert answer.ok
    assert "Them: Let's discuss the release" in model.prompts[0]
    assert "- deadlines" in model.prompts[0]
    # The topic lies in this very chat's folder.
    assert (manager.store.folder(created.id) / "recap.json").is_file()
