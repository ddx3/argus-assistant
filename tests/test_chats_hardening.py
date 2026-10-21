"""Sessions on disk survive failures, races and growth (chats-recap-4, -6, -9, -11, -12,
fe-index-b-4).

Session files are a person's personal data: settings, history, screenshots of their
screen. Before, a file zeroed by a power loss made the session invisible; deleting next
to opening left a ghost session in the window; frames piled up without limit; and a
single bad utterance killed the history writing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time

import pytest
from starlette.testclient import TestClient
from test_chats import answer, make_app

from argus.chats import manager_of
from argus.chats.manager import RESTORED_SHOT_BASE, restored_shot_id
from argus.chats.store import SHOTS_ON_DISK, SHOTS_PRUNE_SLACK, ChatStore
from argus.core import atomic
from argus.core.events import Source, SpeechEvent
from argus.recap import material
from argus.ui.admin import build_panel
from argus.ui.server import build


def opened(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    return app, manager_of(app)


# --------------------------------------------------------------------------
# Address of a restored frame (fe-index-b-4)
# --------------------------------------------------------------------------


def test_the_id_of_a_restored_shot_is_stable_unique_and_safe_for_the_page():
    a = restored_shot_id("0123456789", 5)
    assert a == restored_shot_id("0123456789", 5)                   # the same on every visit
    assert a != restored_shot_id("0123456788", 5)                   # its own for each chat
    assert a != restored_shot_id("0123456789", 6)                   # and for each frame
    assert a < 0                                                    # "from a previous visit"
    assert -a > RESTORED_SHOT_BASE > 10 * 1_000_000_000             # does not overlap with the old ones
    assert -a < 2**53                                               # the page reads the number without loss


async def test_two_chats_do_not_share_the_address_of_their_restored_shots(tmp_path):
    """A window remembers pictures by address for a year: one address for two shots — someone
    else's screen in the second session.
    """
    app, chats = opened(tmp_path)
    one, two = chats.create("A"), chats.create("B")
    for chat, pixels in ((one, b"\xff\xd8\xff" + b"A" * 2000), (two, b"\xff\xd8\xff" + b"B" * 2000)):
        chats.store.save_shot(chat.id, 777, pixels)                  # the same frame file name
        chats.store.append(chat.id, {"k": "a", "text": "answer", "image_kb": 1, "shot": 777})

    await chats.activate(one.id)
    first = [s.t for s in app.state.shots]
    await chats.activate(two.id)
    second = [s.t for s in app.state.shots]
    await chats.activate(one.id)
    again = [s.t for s in app.state.shots]

    assert len(first) == len(second) == 1
    assert first != second, "two different shots got the same address"
    assert again == first, "the shot address changes from visit to visit"


def test_each_restored_shot_is_served_under_its_own_address(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    with TestClient(build(app)) as client:
        store = ChatStore(app.cfg.chats_dir)
        ids, pictures, chat_ids = [], {}, []
        for name in ("A", "B"):
            chat = client.post("/chats", json={"name": name}).json()["chat"]
            chat_ids.append(chat["id"])
            jpeg = b"\xff\xd8\xff" + name.encode() * 2000
            store.save_shot(chat["id"], 111, jpeg)
            store.append(chat["id"], {"k": "a", "text": "answer", "image_kb": len(jpeg) // 1024,
                                      "shot": 111})
            client.get(f"/chat/{chat['id']}")
            shot = client.get("/state").json()["shots"][0]["id"]
            ids.append(shot)
            pictures[shot] = jpeg
            assert client.get(f"/shot/{shot}.jpg").content == jpeg
        assert ids[0] != ids[1]
        client.get(f"/chat/{chat_ids[0]}")                            # back to the first one
        assert client.get(f"/shot/{ids[0]}.jpg").content == pictures[ids[0]]


# --------------------------------------------------------------------------
# Deleting and opening do not diverge (chats-recap-12)
# --------------------------------------------------------------------------


async def test_deleting_a_chat_that_is_being_opened_leaves_no_ghost(tmp_path, monkeypatch):
    from argus.chats.manager import ChatManager

    _, chats = opened(tmp_path)
    target = chats.create("A")

    async def slow_settle(self):
        await asyncio.sleep(0.2)              # the model is finishing reading the answer, and the chat is already "opening"

    monkeypatch.setattr(ChatManager, "_settle", slow_settle)
    opening = asyncio.create_task(chats.activate(target.id))
    await asyncio.sleep(0.05)                 # opening holds the lock
    deleting = asyncio.create_task(chats.delete(target.id))     # a second tab
    results = await asyncio.gather(opening, deleting, return_exceptions=True)

    assert not [r for r in results if isinstance(r, BaseException)], results
    assert chats.active_id is None, "the window keeps open a session that no longer exists"
    assert not chats.store.folder(target.id).exists()


async def test_a_chat_removed_behind_the_back_while_opening_is_not_left_open(tmp_path, monkeypatch):
    from argus.chats.manager import ChatManager

    _, chats = opened(tmp_path)
    target = chats.create("A")

    async def settle_and_vanish(self):
        shutil.rmtree(chats.store.folder(target.id), ignore_errors=True)   # by Explorer or another window

    monkeypatch.setattr(ChatManager, "_settle", settle_and_vanish)
    with pytest.raises(KeyError):
        await chats.activate(target.id)

    assert chats.active_id is None


# --------------------------------------------------------------------------
# Session file: flush to disk, previous copy, unreadable file (chats-recap-11)
# --------------------------------------------------------------------------


def test_a_save_flushes_to_disk_before_the_swap(tmp_path, monkeypatch):
    synced = []
    real = atomic.os.fsync
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})

    assert synced, "chat.json was replaced without fsync: after a power loss it may end up empty"
    synced.clear()
    store.save(chat)
    assert len(synced) >= 2                      # both the file itself and its previous copy


def test_a_chat_file_emptied_by_a_power_cut_falls_back_to_the_previous_copy(tmp_path, caplog):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("Interview at a bank", lambda _: {"channel.assist.profile.summary": "I am an engineer"})
    chat.answers = 3
    store.save(chat)                              # now there is a copy of the previous version next to it
    (store.folder(chat.id) / "chat.json").write_bytes(b"")          # NTFS left the rename without data

    with caplog.at_level(logging.WARNING):
        got = store.get(chat.id)

    assert got is not None and got.name == "Interview at a bank"
    assert got.settings == {"channel.assist.profile.summary": "I am an engineer"}
    assert [c.id for c in store.list()] == [chat.id]               # the session did not vanish from the list
    assert any("cannot be read" in r.getMessage() for r in caplog.records), "the corruption is not reported anywhere"


def test_a_broken_file_does_not_overwrite_the_good_previous_copy(tmp_path):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    chat.name = "good name"
    store.save(chat)
    folder = store.folder(chat.id)
    (folder / "chat.json").write_text("{not json", encoding="utf-8")

    chat.name = "new name"
    store.save(chat)                              # the previous file is corrupt, it must not be copied

    # The copy stayed the last good one (before the "good name"), and did not become a copy of
    # the corrupt file.
    assert json.loads((folder / "chat.json.bak").read_text("utf-8"))["name"] == "chat"
    assert store.get(chat.id).name == "new name"


def test_an_unreadable_chat_is_reported_once_not_on_every_list(tmp_path, caplog):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    (store.folder(chat.id) / "chat.json").write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert store.list() == []

    assert sum("cannot be read" in r.getMessage() for r in caplog.records) <= 1


async def test_a_chat_whose_file_cannot_be_read_can_still_be_deleted(tmp_path):
    _, chats = opened(tmp_path)
    chat = chats.create("A")
    (chats.store.folder(chat.id) / "chat.json").write_text("{not json", encoding="utf-8")
    (chats.store.folder(chat.id) / "chat.json.bak").unlink(missing_ok=True)

    await chats.delete(chat.id)

    assert not chats.store.folder(chat.id).exists()
    with pytest.raises(KeyError):
        await chats.delete(chat.id)               # no folder: an honest "there is no such one"


def test_the_other_files_of_a_session_are_flushed_too(tmp_path, monkeypatch):
    from argus.recap.results import Results
    from argus.recap.topics import Topics

    synced = []
    real = atomic.os.fsync
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    synced.clear()

    Results(store, chat.id).add("request", "text")
    after_results = len(synced)
    material.Edits(store, chat.id).drop("1760000000.5")
    after_edits = len(synced)
    monkeypatch.setattr("argus.recap.chat.active", lambda app: (chat.id, store))
    Topics(None).add("deadlines")

    assert 0 < after_results < after_edits < len(synced)


# --------------------------------------------------------------------------
# The history writer does not die (chats-recap-6)
# --------------------------------------------------------------------------


def test_a_lone_surrogate_in_a_line_is_replaced_not_fatal(tmp_path):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})

    store.append(chat.id, {"k": "s", "ts": 1.0, "src": "them", "text": "before\ud800after"})
    store.append(chat.id, {"k": "s", "ts": 2.0, "src": "them", "text": "further"})

    assert [r["text"] for r in store.history(chat.id)] == ["before?after", "further"]


async def test_one_bad_line_does_not_stop_the_speech_writer(tmp_path, monkeypatch):
    app, chats = opened(tmp_path)
    chat = chats.create("A")
    await chats.activate(chat.id)
    later = app.clock.now() + 1_000_000
    real = ChatStore.append
    calls = []

    def flaky(self, chat_id, record):
        calls.append(record["text"])
        if len(calls) == 1:
            raise ValueError("strange character")          # not `OSError`
        return real(self, chat_id, record)

    monkeypatch.setattr(ChatStore, "append", flaky)

    async def arrives():
        for i, text in enumerate(("first", "second", "third")):
            yield SpeechEvent(t=later + i, t_end=later + i + 1, source=Source.THEM, text=text)

    await chats._watch_speech(arrives())                  # before, an exception out of the loop

    assert [r["text"] for r in chats.store.history(chat.id)] == ["second", "third"]


async def test_one_bad_answer_does_not_stop_the_answer_writer(tmp_path):
    app, chats = opened(tmp_path)
    chat = chats.create("A")
    await chats.activate(chat.id)

    async def arrives():
        yield object()                                    # it has no `meta`
        yield answer("a good answer", epoch=app.assistant.epoch)

    await chats._watch_answers(arrives())
    await asyncio.gather(*chats._tracking)

    assert [r["text"] for r in chats.store.history(chat.id) if r["k"] == "a"] == ["a good answer"]


async def test_an_error_that_is_not_an_oserror_does_not_escape_the_answer_task(tmp_path, monkeypatch):
    _, chats = opened(tmp_path)
    chat = chats.create("A")
    await chats.activate(chat.id)

    def broken(self, chat_id, shot_id, jpeg):
        raise ValueError("the frame cannot be encoded")

    monkeypatch.setattr(ChatStore, "save_shot", broken)
    from argus.app import Shot

    shot = Shot(t=5, jpeg=b"x" * 3000)
    await chats._record_answer(chat.id, answer("text", image_kb=shot.kb), shot)    # does not raise


# --------------------------------------------------------------------------
# Utterance marks do not repeat (chats-recap-4)
# --------------------------------------------------------------------------


async def test_two_phrases_in_one_clock_tick_get_distinct_marks(tmp_path, monkeypatch):
    app, chats = opened(tmp_path)
    chat = chats.create("A")
    await chats.activate(chat.id)
    monkeypatch.setattr("argus.chats.manager.time.time", lambda: 1_760_000_000.0)   # the clock is stopped
    later = app.clock.now() + 1_000_000

    async def arrives():
        for i, who in enumerate((Source.THEM, Source.ME)):
            yield SpeechEvent(t=later, t_end=later + 1 + i, source=who, text=f"line {i}")

    await chats._watch_speech(arrives())

    marks = [r["ts"] for r in chats.store.history(chat.id)]
    assert len(marks) == 2 and marks[0] < marks[1]
    keys = [e.key for e in material.load(chats.store, chat.id)]
    assert len(set(keys)) == 2 and all(material.valid_key(k) for k in keys)


def test_lines_that_already_share_a_mark_get_distinct_keys_and_edits_hit_one(tmp_path):
    """Files written before the fix may already carry identical marks."""
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    for who, text in (("them", "first"), ("me", "second"), ("them", "third")):
        store.append(chat.id, {"k": "s", "ts": 1_760_000_000.25, "src": who, "text": text})

    keys = [e.key for e in material.load(store, chat.id)]
    assert len(set(keys)) == 3

    edits = material.Edits(store, chat.id)
    edits.fix(keys[1], "corrected")
    edits.drop(keys[2])
    shown = material.load(store, chat.id)

    assert [e.line.text for e in shown] == ["first", "corrected"]
    assert [e.edited for e in shown] == [False, True]


# --------------------------------------------------------------------------
# Frames do not pile up (chats-recap-9)
# --------------------------------------------------------------------------


def _chat_with_shots(tmp_path, count: int):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    for i in range(1, count + 1):
        store.save_shot(chat.id, i, b"jpeg%d" % i)
        store.append(chat.id, {"k": "a", "text": f"answer {i}", "shot": i})
    return store, chat


def _on_disk(store, chat) -> set[str]:
    return {p.name for p in (store.folder(chat.id) / "shots").iterdir()}


def test_old_shots_are_removed_and_the_newest_stay(tmp_path):
    store, chat = _chat_with_shots(tmp_path, SHOTS_ON_DISK + SHOTS_PRUNE_SLACK + 5)

    removed = store.prune_shots(chat.id)

    total = SHOTS_ON_DISK + SHOTS_PRUNE_SLACK + 5
    assert removed == 15
    assert _on_disk(store, chat) == {f"{i}.jpg" for i in range(total - SHOTS_ON_DISK + 1, total + 1)}


def test_a_few_extra_shots_do_not_trigger_the_cleanup(tmp_path):
    store, chat = _chat_with_shots(tmp_path, SHOTS_ON_DISK + SHOTS_PRUNE_SLACK)

    assert store.prune_shots(chat.id) == 0
    assert len(_on_disk(store, chat)) == SHOTS_ON_DISK + SHOTS_PRUNE_SLACK


def test_a_shot_nobody_refers_to_is_removed_too(tmp_path):
    store, chat = _chat_with_shots(tmp_path, SHOTS_ON_DISK + SHOTS_PRUNE_SLACK + 1)
    store.save_shot(chat.id, 99_999, b"orphan")                  # it is not in the history

    store.prune_shots(chat.id)

    assert "99999.jpg" not in _on_disk(store, chat)


async def test_the_cleanup_runs_with_the_answers_and_the_last_ten_still_come_back(tmp_path):
    from argus.app import Shot

    app, chats = opened(tmp_path)
    chat = chats.create("A")
    await chats.activate(chat.id)
    total = SHOTS_ON_DISK + SHOTS_PRUNE_SLACK + 7
    for i in range(1, total + 1):
        jpeg = b"\xff\xd8\xff" + bytes([i % 250]) * 2000
        await chats._record_answer(chat.id, answer(f"answer {i}", image_kb=1), Shot(t=i, jpeg=jpeg))

    files = _on_disk(chats.store, chat)
    assert len(files) <= SHOTS_ON_DISK + SHOTS_PRUNE_SLACK

    await chats.deactivate()
    await chats.activate(chat.id)
    assert len(app.state.shots) == 10
    assert all(s.jpeg.startswith(b"\xff\xd8\xff") for s in app.state.shots)


# --------------------------------------------------------------------------
# The file-writing helper itself
# --------------------------------------------------------------------------


def test_the_atomic_writer_leaves_neither_a_temp_file_nor_a_half_written_one(tmp_path):
    path = tmp_path / "data.json"

    atomic.write_text(path, '{"a": 1}\n')
    atomic.write_text(path, '{"a": 2}\n', backup=True)

    assert path.read_text("utf-8") == '{"a": 2}\n'
    assert (tmp_path / "data.json.bak").read_text("utf-8") == '{"a": 1}\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.json", "data.json.bak"]


def test_a_failed_write_keeps_the_old_file_untouched(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    atomic.write_text(path, '{"a": 1}\n')
    monkeypatch.setattr(atomic.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError):
        atomic.write_text(path, '{"a": 2}\n')

    assert path.read_text("utf-8") == '{"a": 1}\n'


def test_the_clock_is_not_trusted_for_ordering():
    """An utterance's mark strictly increases, even if the clock goes backward (NTP) or stands
    still.
    """
    from argus.chats.manager import ChatManager

    stub = ChatManager.__new__(ChatManager)
    stub._last_speech_ts = 0.0
    now = time.time()
    marks = [stub._next_speech_ts() for _ in range(5)]
    assert marks == sorted(set(marks)) and marks[0] >= now - 1
    stub._last_speech_ts = now + 1000                        # the clock went back
    assert stub._next_speech_ts() > now + 1000


# --------------------------------------------------------------------------
# Audit of 2026-10-16: `chat.json` from a brought-in folder does not control the app settings
# --------------------------------------------------------------------------


async def test_a_foreign_chat_file_cannot_change_app_settings_or_point_materials_elsewhere(tmp_path):
    """A chat folder is brought from another computer or from sync. The file inside is foreign:
    it must not change the address of the account server or other general settings, and the
    materials folder must stay inside the chat itself (otherwise uploading and deleting files
    would go anywhere on the disk)."""
    app, chats = opened(tmp_path)
    chat = chats.create("A")
    elsewhere = (tmp_path / "somewhere-else").as_posix()
    path = chats.store.folder(chat.id) / "chat.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["settings"].update({
        "account.server": "http://evil.example",
        "update.server": "http://evil.example",
        "channel.assist.materials.dir": elsewhere,
        "channel.assist.profile.summary": "ordinary for the chat",
    })
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    server_before = app.cfg.account.server
    update_before = app.cfg.update.server

    await chats.activate(chat.id)

    assert app.cfg.account.server == server_before != "http://evil.example"
    assert app.cfg.update.server == update_before != "http://evil.example"
    inside = (chats.store.folder(chat.id) / "materials").resolve()
    assist = app.cfg.channel["assist"]
    assert app.cfg.resolve(type(inside)(assist["materials"]["dir"])).resolve() == inside
    assert assist["profile"]["summary"] == "ordinary for the chat"                 # the chat's own stays
