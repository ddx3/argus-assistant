"""Chats: each conversation has its own settings, materials and answer history.

The errors here are silent. A chat setting written into the shared file changes all the
other chats and gives no sign of it; an answer from a previous chat that surfaces in
the timeline of a new one looks like a model answer; materials for one exam that leak
into another's prompt are noticeable only by a strange answer. So the tests do not
check that "the route exists" but that an opened chat really replaced the settings, the
prompt and the timeline, and a closed one put everything back as it was.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from argus.app import Argus, Shot
from argus.assist.settings import read
from argus.brain.base import Answer
from argus.chats import chats_of, manager_of
from argus.chats.manager import (
    CHAT_EXACT,
    FEED,
    MATERIALS_DIR_PATH,
    NAME_PATH,
    SUMMARY_PATH,
    is_chat_path,
    restored_shot_id,
)
from argus.chats.store import ChatStore, clean_name, valid_id
from argus.context.timeline import Kind
from argus.core.config import Config
from argus.core.events import Source, SpeechEvent
from argus.core.settings_store import LOCAL_NAME
from argus.ui.admin import build_panel
from argus.ui.server import STATIC, build

GENERAL = "a general story about myself"


def make_app(root: Path, local: dict | None = None, *, chats_dir: bool = False) -> Argus:
    """An application rooted in a temporary folder, with no hardware and no network.

    `local` is what would have been in `argus.local.json` before chats appeared.
    `chats_dir` creates the chats folder in advance: otherwise the very first call decides
    that this is the first launch and moves the shared settings into the "Main session"
    (`test_old_settings_become_the_first_chat_once`).
    """
    local = local or {}
    if local:
        (root / LOCAL_NAME).write_text(json.dumps(local, ensure_ascii=False), encoding="utf-8")
    cfg = Config.model_validate(local)
    cfg.root = root
    cfg.brain.provider = "stub"
    cfg.ui.admin = True
    if chats_dir:
        cfg.chats_dir.mkdir()
    return Argus(cfg, with_screen=False, with_brain=True)


def with_general_context(root: Path) -> Argus:
    return make_app(
        root, {"channel": {"assist": {"profile": {"summary": GENERAL}}}}, chats_dir=True
    )


def answer(text: str = "answer", cause: str = "question", **meta) -> Answer:
    return Answer(text=text, provider="stub", model="m", latency_ms=1200.0, done=True,
                  meta={"cause": cause, "trigger": "hotkey", "reason": "r", **meta})


def prompt(app: Argus) -> str:
    return app.cfg.brain.system_prompt


def served_from(text: str, name: str) -> bool:
    """The page is the file `name`: what the server served, without the translation layer.

    The interface language layer (`ui/i18n.py`) inserts its script into the `<head>` of
    every served page, and equality of the whole text would not hold without `strip`.
    """
    from argus.ui.i18n import strip

    return strip(text) == (STATIC / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Disk
# --------------------------------------------------------------------------


def test_chat_ids_cannot_climb_out_of_the_folder(tmp_path):
    store = ChatStore(tmp_path / "chats")
    for bad in ("../x", "..", "", "ABCDEF0123", "abc", "a" * 40, "../../argus.toml"):
        assert not valid_id(bad), bad
        assert store.get(bad) is None
        with pytest.raises(KeyError):
            store.folder(bad)
    assert valid_id("0123456789")


def test_the_list_skips_a_broken_folder_and_puts_recent_first(tmp_path):
    store = ChatStore(tmp_path / "chats")
    old = store.create("old", lambda _: {})
    time.sleep(0.05)               # the Windows clock has a step of about 16 ms, a shorter pause is not enough
    new = store.create("new", lambda _: {})
    (tmp_path / "chats" / "0123456789").mkdir()           # a folder without chat.json
    (tmp_path / "chats" / "0123456788").mkdir()
    (tmp_path / "chats" / "0123456788" / "chat.json").write_text("{not json", encoding="utf-8")
    assert [c.id for c in store.list()] == [new.id, old.id]


def test_history_survives_a_torn_last_line(tmp_path):
    """The process was cut off in the middle of a write — one line is corrupted, not the history."""
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    store.append(chat.id, {"k": "a", "text": "one"})
    store.append(chat.id, {"k": "a", "text": "two"})
    with (store.folder(chat.id) / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"k":"a","text":"torn')
    assert [r["text"] for r in store.history(chat.id)] == ["one", "two"]
    # The next record does not stick to the torn fragment and does not vanish together with it.
    store.append(chat.id, {"k": "a", "text": "after the failure"})
    assert [r["text"] for r in store.history(chat.id)] == ["one", "two", "after the failure"]


def test_the_id_comes_from_the_folder_not_from_the_file(tmp_path):
    """A chat folder can be brought from another computer. The identifier goes onto the page and
    into disk paths — the file must not control it."""
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    path = store.folder(chat.id) / "chat.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["id"] = '"><img src=x onerror=alert(1)>'
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get(chat.id).id == chat.id
    assert [c.id for c in store.list()] == [chat.id]
    assert valid_id(store.get(chat.id).id)


def test_a_late_save_does_not_bring_a_deleted_chat_back(tmp_path):
    """The answer finished arriving after the chat was deleted — the write must not resurrect
    it.
    """
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    store.delete(chat.id)
    with pytest.raises(OSError):
        store.save(chat)
    store.append(chat.id, {"k": "a"})                  # the history silently writes nowhere
    assert store.save_shot(chat.id, 5, b"jpeg") is False   # and the frame too
    assert not store.folder(chat.id).exists()
    assert store.list() == []


def test_saving_a_chat_leaves_no_temp_file(tmp_path):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("chat", lambda _: {})
    chat.name = "other"
    store.save(chat)
    # Next to it there is only the previous copy of the file (`chat.json.bak`); no temporary
    # file is left.
    assert sorted(p.name for p in store.folder(chat.id).iterdir()) == ["chat.json", "chat.json.bak"]
    assert store.get(chat.id).name == "other"


def test_names_are_tidied():
    assert clean_name("  Interview \n at   Acme ") == "Interview at Acme"
    assert len(clean_name("x" * 200)) == 80
    assert clean_name(None) == ""


def test_personal_chats_are_ignored_by_git_but_the_package_is_not():
    """The rule `chats/` without a leading slash would swallow the package `src/argus/chats/`
    too: git would silently not see the code, while the personal data would lie right next
    to it."""
    import shutil
    import subprocess

    git = shutil.which("git")
    root = Path(__file__).resolve().parents[1]
    if git is None or not (root / ".git").exists():
        pytest.skip("not a git copy — nothing to check the rule against")

    def ignored(path: str) -> bool:
        return subprocess.run([git, "check-ignore", "-q", path], cwd=root, check=False).returncode == 0

    assert ignored("chats/0123456789/chat.json")
    assert not ignored("src/argus/chats/manager.py")


def test_which_settings_are_the_chats_own():
    for own in (SUMMARY_PATH, MATERIALS_DIR_PATH, "channel.assist.prompting.tone",
                "channel.assist.profile.language", "channel.assist.answering.mic_mode",
                "channel.assist.answering.think_longer", "realtime_stt.language",
                "realtime_stt.smart"):
        assert is_chat_path(own), own
    # Hardware, keys, account, speech output and the app's own language are shared.
    for shared in ("audio.mic_device", "channel.assist.hotkeys.ask", "channel.tts.enabled",
                   "channel.assist.interface.language", "channel.assist.overlay.width",
                   "account.server", "brain.provider", "stt.language"):
        assert not is_chat_path(shared), shared
    assert "realtime_stt.language" in CHAT_EXACT
    assert "realtime_stt.smart" in CHAT_EXACT


# --------------------------------------------------------------------------
# Open a chat
# --------------------------------------------------------------------------


async def test_a_new_chat_does_not_inherit_the_general_context(tmp_path):
    """The story about oneself in the general settings is left over from the times without
    chats; a new chat must not pick it up — otherwise someone else's story would sit in
    every conversation."""
    app = with_general_context(tmp_path)
    build_panel(app)
    assert GENERAL in prompt(app)

    chats = manager_of(app)
    chat = chats.create("Exam")
    assert chat.settings[SUMMARY_PATH] == ""
    await chats.activate(chat.id)
    assert read(app.cfg).profile.summary == ""
    assert GENERAL not in prompt(app)

    await chats.deactivate()                     # returned to the home screen
    assert read(app.cfg).profile.summary == GENERAL
    assert GENERAL in prompt(app)


async def test_a_new_chat_reads_its_materials_even_if_the_general_switch_was_off(tmp_path):
    """The "Use materials" checkbox was once switched off for the general
    folder. Files that a person uploads to a new chat must be read — not lie there silently."""
    app = make_app(
        tmp_path, {"channel": {"assist": {"materials": {"enabled": False}}}}, chats_dir=True
    )
    build_panel(app)
    assert read(app.cfg).materials.enabled is False
    chats = manager_of(app)
    chat = chats.create("Lecture")
    folder = chats.store.folder(chat.id) / "materials"
    folder.mkdir()
    (folder / "topic.md").write_text("Ohm's law", encoding="utf-8")

    await chats.activate(chat.id)
    assert read(app.cfg).materials.enabled is True
    assert "Ohm's law" in prompt(app)
    await chats.deactivate()
    assert read(app.cfg).materials.enabled is False         # the shared setting is untouched


async def test_each_chat_answers_from_its_own_context(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    a.settings[SUMMARY_PATH] = "preparing for an exam on databases"
    b.settings[SUMMARY_PATH] = "a call with a client about payments"
    chats.store.save(a)
    chats.store.save(b)

    await chats.activate(a.id)
    assert "databases" in prompt(app) and "payments" not in prompt(app)
    await chats.activate(b.id)
    assert "payments" in prompt(app) and "databases" not in prompt(app)
    # The chats wrote to their own files, the shared one is untouched.
    assert not (tmp_path / LOCAL_NAME).exists()


async def test_answering_modes_follow_the_chat(tmp_path):
    """"When to answer" is also a conversation setting, and the filter is
    rebuilt.
    """
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    lecture = chats.create("Lecture")
    lecture.settings["channel.assist.answering.system_mode"] = "wake_word"
    lecture.settings["channel.assist.answering.wake_word"] = "argus"
    chats.store.save(lecture)
    call = chats.create("Call")

    await chats.activate(lecture.id)
    assert app.assistant.gate.settings.system_mode.value == "wake_word"
    assert app.assistant.gate.settings.wake_word == "argus"
    await chats.activate(call.id)
    assert app.assistant.gate.settings.system_mode.value == "by_form"


async def test_materials_are_per_chat(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    for chat, text in ((a, "normal forms"), (b, "payment idempotency")):
        folder = chats.store.folder(chat.id) / "materials"
        folder.mkdir()
        (folder / "lecture.md").write_text(text, encoding="utf-8")

    from argus.assist import materials

    await chats.activate(a.id)
    assert materials.directory(app.cfg) == chats.store.folder(a.id) / "materials"
    assert "normal forms" in prompt(app) and "payment idempotency" not in prompt(app)
    await chats.activate(b.id)
    assert "payment idempotency" in prompt(app) and "normal forms" not in prompt(app)
    await chats.deactivate()
    assert materials.directory(app.cfg) == tmp_path / "materials"


async def test_open_clears_the_previous_conversation(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    await chats.activate(a.id)

    app.state.answers.append(answer())
    app.state.shots.append(Shot(t=5, jpeg=b"x"))
    app.state.speech.append(SpeechEvent(t=1, t_end=2, source=Source.THEM, text="a remark"))
    app.timeline.add_speech(app.state.speech[0])
    app.assistant.answers.append(answer())
    app.assistant.gate.calls = 7
    epoch = app.assistant.epoch

    await chats.activate(b.id)

    assert not app.state.answers and not app.state.shots and not app.state.speech
    assert len(app.timeline) == 0
    assert app.assistant.answers == []
    assert app.assistant.gate.calls == 0
    assert app.assistant.epoch == epoch + 1


async def test_what_the_machine_chose_survives_opening_a_chat(tmp_path):
    """The pinned window, the "system sound" windows, the "Screen" switched off — the
    state of the machine, not of the chat. Opening a chat must not return them to defaults:
    otherwise the "Screen" checkbox would stay off while frames were still going to
    the model."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    app.set_target_window("Zoom Meeting")
    app.cfg.audio.loopback_windows = ["Zoom Meeting"]
    app.cfg.brain.send_screenshot = False
    app.sources["screen"] = False

    await chats.activate(chat.id)

    assert app.cfg.screen.target_window == "Zoom Meeting"
    assert app.cfg.audio.loopback_windows == ["Zoom Meeting"]
    assert app.cfg.brain.send_screenshot is False
    await chats.deactivate()
    assert app.cfg.screen.target_window == "Zoom Meeting"


async def test_editing_an_unrelated_setting_does_not_wipe_the_chat_from_the_prompt(tmp_path):
    """Editing a field outside the channel rebuilt the config and wiped the system prompt built
    from the story: the conversation context disappeared while the page kept showing it."""
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    chat.settings[SUMMARY_PATH] = "preparing for an interview about message queues"
    chats.store.save(chat)
    await chats.activate(chat.id)
    assert "message queues" in prompt(app)

    for edit in ({"realtime_stt.language": "en"}, {"channel.tts.enabled": True},
                 {"channel.assist.hotkeys.ask": "ctrl+alt+z"}):
        assert panel.apply(edit)["ok"], edit
        assert "message queues" in prompt(app), edit


async def test_a_remembered_choice_is_not_lost_by_a_later_edit(tmp_path):
    """The microphone is remembered bypassing the settings page (`Argus._remember`). The page
    and the chats share one store, and the write must go through it: otherwise its old
    `overrides` would, on the first edit, erase the choice from the file."""
    app = make_app(tmp_path, chats_dir=True)
    app.cfg.config_path = tmp_path / "argus.toml"
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")

    app.cfg.audio.mic_device = "HyperX Cloud Alpha"
    app._remember("audio.mic_device", "HyperX Cloud Alpha")
    await chats.activate(chat.id)
    assert app.cfg.audio.mic_device == "HyperX Cloud Alpha"

    assert panel.apply({"channel.assist.hotkeys.ask": "ctrl+alt+z"})["ok"]
    saved = json.loads((tmp_path / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["audio"]["mic_device"] == "HyperX Cloud Alpha"
    assert saved["channel"]["assist"]["hotkeys"]["ask"] == "ctrl+alt+z"


def test_a_key_that_vanished_is_dropped_from_the_live_config():
    """The chat key (`materials.dir`) disappears together with the chat. A diff taken only over
    what remains would not notice it, and a closed chat would keep reading its folder."""
    from argus.core.settings_store import diff_paths, flatten
    from argus.ui.admin_base import adopt_changed

    live = Config.model_validate({"channel": {"assist": {"materials": {"dir": "chats/x"}}}})
    fresh = Config()
    before = flatten(live.model_dump(mode="json"))
    after = flatten(fresh.model_dump(mode="json"))
    changed = diff_paths(before, after)
    assert "channel.assist.materials.dir" in changed

    adopt_changed(live, fresh, changed)
    assert live.channel == {}


async def test_opening_the_same_chat_again_keeps_the_conversation(tmp_path):
    """The page was refreshed (F5) — the conversation goes on, and it must not be reset."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    await chats.activate(chat.id)
    app.state.answers.append(answer())
    app.set_running(True)

    await chats.activate(chat.id)
    assert len(app.state.answers) == 1
    assert app.running is True


async def test_opening_another_chat_switches_recognition_off(tmp_path):
    """Recognition is paid and is turned on only by the button — it must not move into someone
    else's conversation."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    await chats.activate(a.id)
    app.set_running(True)
    await chats.activate(b.id)
    assert app.running is False
    assert app.speech.paused


async def test_a_chat_with_broken_settings_still_opens(tmp_path, caplog):
    """The file was fixed by hand or the model changed — the history must stay accessible, not
    lock up together with the bad field."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    chat.settings["channel.assist.answering.min_words"] = "many"      # the channel model does not accept it
    chat.settings["realtime_stt.language"] = 123                       # and the core does not accept it
    chats.store.save(chat)
    await chats.activate(chat.id)
    assert chats.active_id == chat.id
    assert app.cfg.realtime_stt.language == "ru"                        # the unfit value was not applied
    assert "invalid — opening with the shared ones" in caplog.text                                    # this is the rollback path, not "it matched anyway"


async def test_an_unknown_chat_does_not_open(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    with pytest.raises(KeyError):
        await manager_of(app).activate("0123456789")
    assert manager_of(app).active_id is None


# --------------------------------------------------------------------------
# Editing settings on the page
# --------------------------------------------------------------------------


async def test_the_page_edits_the_open_chat_not_the_general_file(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("Exam")
    await chats.activate(chat.id)

    out = panel.apply({SUMMARY_PATH: "preparing for an exam", NAME_PATH: "Database exam",
                       "channel.assist.prompting.tone": "casual"})
    assert out["ok"], out
    assert out["values"][NAME_PATH] == "Database exam"
    assert "preparing for an exam" in prompt(app)

    saved = chats.store.get(chat.id)
    assert saved.name == "Database exam"
    assert saved.settings[SUMMARY_PATH] == "preparing for an exam"
    assert saved.settings["channel.assist.prompting.tone"] == "casual"
    # Nothing went into the shared file.
    assert not (tmp_path / LOCAL_NAME).exists()

    # Closed, opened again: the edit is in place.
    await chats.activate(chats.create("another").id)
    assert "preparing for an exam" not in prompt(app)
    await chats.activate(chat.id)
    assert "preparing for an exam" in prompt(app)


async def test_a_general_edit_does_not_disturb_the_open_chat(tmp_path):
    """Editing a key rebuilds the config — and must not put the general values back in place of
    those the chat has of its own."""
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    chat.settings[SUMMARY_PATH] = "the chat's story"
    chats.store.save(chat)
    await chats.activate(chat.id)

    out = panel.apply({"channel.assist.hotkeys.ask": "ctrl+alt+z"})
    assert out["ok"], out
    assert read(app.cfg).profile.summary == "the chat's story"
    assert "the chat's story" in prompt(app)
    saved = json.loads((tmp_path / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["channel"]["assist"]["hotkeys"]["ask"] == "ctrl+alt+z"
    assert "profile" not in saved["channel"]["assist"]


async def test_one_edit_can_touch_both_the_chat_and_the_general_settings(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    await chats.activate(chat.id)

    out = panel.apply({SUMMARY_PATH: "for the chat", "channel.assist.hotkeys.ask": "ctrl+alt+y"})
    assert out["ok"], out
    assert chats.store.get(chat.id).settings[SUMMARY_PATH] == "for the chat"
    saved = json.loads((tmp_path / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["channel"]["assist"]["hotkeys"]["ask"] == "ctrl+alt+y"


async def test_a_bad_value_is_refused_and_nothing_is_written(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    await chats.activate(chat.id)
    before = dict(chats.store.get(chat.id).settings)

    out = panel.apply({"channel.assist.answering.system_mode": "no such mode"})
    assert out["ok"] is False
    assert "channel.assist.answering.system_mode" in out["errors"]
    out = panel.apply({NAME_PATH: "   "})
    assert out == {"ok": False, "errors": {NAME_PATH: "the name can't be empty"}}
    assert chats.store.get(chat.id).settings == before
    assert chats.store.get(chat.id).name == "A"


def test_without_a_chat_the_page_edits_the_general_settings_as_before(tmp_path):
    app = make_app(tmp_path)
    panel = build_panel(app)
    out = panel.apply({SUMMARY_PATH: "the general story"})
    assert out["ok"], out
    saved = json.loads((tmp_path / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["channel"]["assist"]["profile"]["summary"] == "the general story"
    # And an application with no chat has no chat name at all, neither in the values nor in
    # the fields.
    assert NAME_PATH not in panel.values()
    fields = [f["path"] for s in panel.schema()["sections"] for f in s["settings"]]
    assert NAME_PATH not in fields


async def test_the_schema_knows_whose_section_is_whose(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)

    shared = panel.schema(scope="app")
    assert {s["id"] for s in shared["sections"]} == {"hotkeys", "audio", "tts", "interface"}
    assert all(s["scope"] == "app" for s in shared["sections"])

    chat = chats.create("A")
    await chats.activate(chat.id)
    full = panel.schema()
    scopes = {s["id"]: s["scope"] for s in full["sections"]}
    assert scopes["about"] == scopes["materials"] == scopes["language"] == "chat"
    assert scopes["answering"] == "chat"
    assert scopes["hotkeys"] == "app"
    name_field = full["sections"][0]["settings"][0]
    assert name_field["path"] == NAME_PATH
    assert full["values"][NAME_PATH] == "A"


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


async def test_answers_are_written_and_come_back_when_the_chat_is_opened(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat, other = chats.create("A"), chats.create("B")
    await chats.activate(chat.id)

    await chats._record_answer(chat.id, answer("first", "What is the GIL?"), None)
    await chats._record_answer(chat.id, answer("second", "And what is it for?", deep=True), None)
    assert chats.active.answers == 2
    assert chats.store.get(chat.id).answers == 2

    await chats.activate(other.id)
    assert not app.state.answers
    await chats.activate(chat.id)
    got = list(app.state.answers)
    assert [a.text for a in got] == ["first", "second"]
    assert got[0].meta["cause"] == "What is the GIL?"
    assert got[1].meta["deep"] is True
    assert got[0].ok and got[0].done

    # The page receives restored data the same way as fresh data, through the snapshot, and
    # the heavy `prompt` is not in it.
    from argus.ui.server import snapshot

    snap = snapshot(app)
    assert [a["text"] for a in snap["answers"]] == ["first", "second"]
    assert "prompt" not in snap["answers"][0]
    assert snap["chat"] == {"id": chat.id, "name": "A", "unnamed": False}


async def test_the_spoken_lines_come_back_to_the_screen_but_not_to_the_model(tmp_path):
    """Opened a session with history: the conversation utterances are visible on the working
    screen.

    Found on 2026-10-07: the chat looked empty. The log held 51 utterances, but the screen
    was blank, because only the answers were loaded. Utterances go into the snapshot as a
    separate list (`past_speech`, wall-clock time): this is display only, and the timeline
    the prompt is built from stays empty.
    """
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat, other = chats.create("A"), chats.create("B")
    for i, (src, text) in enumerate([("them", "Hi!"), ("me", "Hello."), ("them", "")]):
        chats.store.append(chat.id, {"k": "s", "ts": 1_000_000 + i, "src": src, "text": text})
    chats.store.append(chat.id, {"k": "a", "ts": 1_000_010, "text": "answer", "cause": "question"})

    await chats.activate(chat.id)
    assert app.state.past_speech == [
        {"wall": 1_000_000.0, "source": "them", "text": "Hi!"},
        {"wall": 1_000_001.0, "source": "me", "text": "Hello."},
    ]
    assert len(app.timeline) == 0 and not app.state.speech      # the model and the live timeline do not see them
    from argus.ui.server import snapshot

    assert [r["text"] for r in snapshot(app)["past_speech"]] == ["Hi!", "Hello."]

    await chats.activate(other.id)
    assert app.state.past_speech == []                          # someone else's session, someone else's history
    assert snapshot(app)["past_speech"] == []

    # Only the last `FEED`: on a long conversation the page does not bloat.
    for i in range(FEED + 20):
        chats.store.append(other.id, {"k": "s", "ts": 2_000_000 + i, "src": "them", "text": f"r{i}"})
    await chats.deactivate()
    await chats.activate(other.id)
    assert len(app.state.past_speech) == FEED
    assert app.state.past_speech[-1]["text"] == f"r{FEED + 19}"


async def test_the_screenshot_of_an_answer_is_kept_with_it(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat, other = chats.create("A"), chats.create("B")
    await chats.activate(chat.id)

    jpeg = b"\xff\xd8\xff" + b"x" * 3000
    shot = Shot(t=987654321, jpeg=jpeg)
    await chats._record_answer(chat.id, answer("saw the screen", image_kb=shot.kb), shot)
    assert (chats.store.folder(chat.id) / "shots" / "987654321.jpg").read_bytes() == jpeg

    await chats.activate(other.id)
    await chats.activate(chat.id)
    # The restored frame has a negative time: it is from a previous visit, and it does not
    # belong in the "what is happening now" timeline (the page discards such frames).
    assert [s.t for s in app.state.shots] == [restored_shot_id(chat.id, 987654321)]
    assert app.state.shots[0].t < 0
    assert app.state.shots[0].jpeg == jpeg
    assert next(iter(app.state.answers)).meta["image_kb"] == shot.kb
    from argus.ui.server import snapshot

    assert [x["t"] < 0 for x in snapshot(app)["shots"]] == [True]


async def test_errors_and_empty_answers_stay_out_of_the_history(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    await chats.activate(chat.id)
    failed = answer("")
    failed.error = "no network"
    await chats._record_answer(chat.id, failed, None)
    await chats._record_answer(chat.id, answer("   "), None)
    assert chats.store.history(chat.id) == []


async def test_the_recorder_listens_to_the_bus(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    app.bus.bind(asyncio.get_running_loop())
    chats = manager_of(app)
    chat = chats.create("A")
    task = asyncio.create_task(chats.run())
    await asyncio.sleep(0.05)
    try:
        # The chat is not open: nowhere to write.
        app.bus.publish(answer("to nobody"))
        await asyncio.sleep(0.4)
        assert chats.store.history(chat.id) == []

        await chats.activate(chat.id)
        now = app.clock.now() + 1_000_000          # the utterances come after the chat was opened
        app.bus.publish(answer("into history", "question?"))
        app.bus.publish(SpeechEvent(t=now, t_end=now + 1, source=Source.THEM,
                                    text="the other person's line"))
        app.bus.publish(SpeechEvent(t=now + 2, t_end=now + 3, source=Source.ME, text="",
                                    error="failure"))
        app.bus.publish(SpeechEvent(t=now + 4, t_end=now + 5, source=Source.THEM,
                                    text="shadowed", engine="shadow"))
        for _ in range(40):
            await asyncio.sleep(0.1)
            kinds = [r["k"] for r in chats.store.history(chat.id)]
            if "a" in kinds and "s" in kinds:
                break
        history = chats.store.history(chat.id)
        assert [r["k"] for r in history] == ["s", "a"] or [r["k"] for r in history] == ["a", "s"]
        spoken = next(r for r in history if r["k"] == "s")
        assert spoken["src"] == "them" and spoken["text"] == "the other person's line"
        assert next(r for r in history if r["k"] == "a")["cause"] == "question?"
        assert chats.active.answers == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_streamed_answer_is_written_when_it_is_finished(tmp_path):
    """A streamed answer is published empty and grows in place."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    app.bus.bind(asyncio.get_running_loop())
    chats = manager_of(app)
    chat = chats.create("A")
    task = asyncio.create_task(chats.run())
    await asyncio.sleep(0.05)
    try:
        await chats.activate(chat.id)
        streamed = Answer(text="", provider="stub", done=False, meta={"cause": "q"})
        app.bus.publish(streamed)
        await asyncio.sleep(0.4)
        assert chats.store.history(chat.id) == []
        streamed.text = "the finished answer"
        streamed.done = True
        for _ in range(40):
            await asyncio.sleep(0.1)
            if chats.store.history(chat.id):
                break
        assert [r["text"] for r in chats.store.history(chat.id)] == ["the finished answer"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_an_answer_from_a_closed_conversation_stays_out_of_the_new_feed(tmp_path):
    """The answer arrived over the bus while another chat was being opened — it does not go into
    the new chat's feed: it would look like an answer to a question nobody asked there."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    await chats.activate(a.id)
    old = answer("from the previous chat", epoch=app.assistant.epoch)
    await chats.activate(b.id)

    async def arrives():
        yield old
        yield answer("own", epoch=app.assistant.epoch)

    await app._pump_answers(arrives())
    assert [x.text for x in app.state.answers] == ["own"]


async def test_a_phrase_that_ended_before_the_switch_stays_out_of_the_new_chat(tmp_path):
    """Recognition is not instant: an utterance the person finished in the previous chat
    arrives over the bus after the switch. It does not go into the new chat — neither the
    feed, nor the history, nor as a reason to wake the model."""
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    await chats.activate(a.id)
    old_end = app.clock.now()
    await chats.activate(b.id)
    later = app.clock.now() + 1_000_000

    def said(text, t_end):
        return SpeechEvent(t=t_end - 500, t_end=t_end, source=Source.THEM, text=text)

    async def arrives():
        yield said("a question from the previous chat?", old_end)
        yield said("a question in this chat?", later)

    await app._pump_speech(arrives())
    assert [e.text for e in app.state.speech] == ["a question in this chat?"]

    await app.assistant._watch_speech(arrives())
    # Utterances only: the assistant already answered the question in this chat, and its
    # answer lies in the timeline too (`Kind.REPLY`), but the test is not about that.
    assert [e.text for e in app.timeline if e.kind is Kind.SPEECH] == ["a question in this chat?"]

    await chats._watch_speech(arrives())
    assert [r["text"] for r in chats.store.history(b.id) if r["k"] == "s"] == ["a question in this chat?"]
    assert chats.store.history(a.id) == []


async def test_a_late_answer_is_not_published_into_the_next_chat(tmp_path, monkeypatch):
    """The request went to the model in one chat and came back in another."""
    # A chat has to be opened in the middle of a request, and `activate` waits until the
    # model finishes reading; here it never will, so we wait only briefly.
    monkeypatch.setattr("argus.chats.manager.SETTLE_S", 0.2)
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    app.bus.bind(asyncio.get_running_loop())
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    await chats.activate(a.id)
    sub = app.bus.subscribe(Answer, name="t")

    assistant = app.assistant
    real = assistant.provider.ask

    async def slow(*args, **kwargs):
        await chats.activate(b.id)           # the person managed to open another chat
        # The previous request is still in flight, but the first question in the new chat does
        # not wait for it.
        assert not assistant.busy
        return await real(*args, **kwargs)

    assistant.provider.ask = slow            # type: ignore[method-assign]
    from argus.brain.gate import Decision, Trigger

    result = await assistant._ask(Decision(True, "test", trigger=Trigger.HOTKEY), "question")
    assert result.text                       # the answer went back to the caller
    assert assistant.answers == []           # it did not land in the new chat's timeline
    assert sub.qsize() == 0                  # and it did not go to the bus
    assert not assistant.busy                # and the foreign end neither took nor freed someone else's slot


async def test_a_straggler_does_not_free_the_slot_of_the_new_chat(tmp_path, monkeypatch):
    """A request of the previous chat is still on its way, and the new chat has already asked.
    When the old one finally returns, it must not clear the busy flag of the new request:
    otherwise a third question would go to the model in parallel with the second."""
    monkeypatch.setattr("argus.chats.manager.SETTLE_S", 0.2)
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    a, b = chats.create("A"), chats.create("B")
    await chats.activate(a.id)
    assistant = app.assistant

    old_may_finish, new_may_finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def ask(prompt, system=None, image=None, pinned=(), deep=False):
        calls.append(prompt)
        await (old_may_finish if len(calls) == 1 else new_may_finish).wait()
        return Answer(text="answer", provider="stub")

    assistant.provider.ask = ask             # type: ignore[method-assign]
    from argus.brain.gate import Decision, Trigger

    decision = Decision(True, "test", trigger=Trigger.HOTKEY)
    old = asyncio.create_task(assistant._ask(decision, "old question"))
    await asyncio.sleep(0.05)
    assert assistant.busy

    await chats.activate(b.id)               # the old request is still in flight
    assert not assistant.busy
    new = asyncio.create_task(assistant._ask(decision, "new question"))
    await asyncio.sleep(0.05)
    assert assistant.busy

    old_may_finish.set()
    await old
    assert assistant.busy                    # the old request does not touch the new request's slot
    new_may_finish.set()
    await new
    assert not assistant.busy


# --------------------------------------------------------------------------
# Creating, name, copy, delete, migrating the old one
# --------------------------------------------------------------------------


def test_default_names_do_not_repeat(tmp_path):
    chats = manager_of(make_app(tmp_path, chats_dir=True))
    assert [chats.create().name for _ in range(3)] == [
        "New session", "New session 2", "New session 3"]


def test_a_chat_cannot_be_renamed_to_nothing(tmp_path):
    from argus.chats.manager import ChatEditError

    chats = manager_of(make_app(tmp_path, chats_dir=True))
    chat = chats.create("Exam")
    with pytest.raises(ChatEditError):
        chats.rename(chat.id, "  ")
    assert chats.rename(chat.id, " Database  exam ").name == "Database exam"
    assert chats.store.get(chat.id).name == "Database exam"


async def test_deleting_the_open_chat_closes_it_first(tmp_path):
    app = with_general_context(tmp_path)
    build_panel(app)
    chats = manager_of(app)
    chat = chats.create("A")
    await chats.activate(chat.id)
    await chats.delete(chat.id)
    assert chats.active_id is None
    assert not chats.store.folder(chat.id).exists()
    assert read(app.cfg).profile.summary == GENERAL         # the shared settings are back


async def test_a_copy_takes_settings_and_files_but_not_the_history(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    src = chats.create("Exam")
    src.settings[SUMMARY_PATH] = "about databases"
    src.settings["channel.assist.prompting.tone"] = "casual"
    chats.store.save(src)
    folder = chats.store.folder(src.id) / "materials"
    folder.mkdir()
    (folder / "lecture.md").write_text("indexes", encoding="utf-8")
    await chats.activate(src.id)
    await chats._record_answer(src.id, answer(), None)

    copy = chats.duplicate(src.id)
    assert copy.name == "Exam (copy)"
    assert copy.settings[SUMMARY_PATH] == "about databases"
    assert copy.settings["channel.assist.prompting.tone"] == "casual"
    # Its own materials, not shared with the original.
    assert copy.settings[MATERIALS_DIR_PATH] != src.settings[MATERIALS_DIR_PATH]
    assert (chats.store.folder(copy.id) / "materials" / "lecture.md").read_text(
        encoding="utf-8") == "indexes"
    assert chats.store.history(copy.id) == []
    assert chats.store.get(copy.id).answers == 0


def test_old_settings_become_the_first_chat_once(tmp_path):
    """Before chats, the context and the materials folder were one per application. They must
    not disappear: they become the first chat, and the lectures stay where they are."""
    (tmp_path / "materials").mkdir()
    (tmp_path / "materials" / "lecture.md").write_text("indexes", encoding="utf-8")
    app = make_app(tmp_path, {"channel": {"assist": {
        "profile": {"summary": GENERAL},
        "answering": {"system_mode": "wake_word"},
        "hotkeys": {"ask": "ctrl+alt+q"},
    }}})
    chats = manager_of(app)

    first = chats.overview()["chats"]
    assert [c["name"] for c in first] == ["Main session"]
    assert first[0]["about"] == GENERAL
    saved = chats.store.get(first[0]["id"])
    assert saved.settings[SUMMARY_PATH] == GENERAL
    assert saved.settings[MATERIALS_DIR_PATH] == "materials"
    assert saved.settings["channel.assist.answering.system_mode"] == "wake_word"
    # Keys are shared, they do not travel into the chat.
    assert not any("hotkeys" in key for key in saved.settings)

    again = chats.overview()["chats"]
    assert [c["id"] for c in again] == [first[0]["id"]]
    chats.create("Second")
    assert len(chats.overview()["chats"]) == 2


def test_nothing_is_carried_over_when_there_was_nothing(tmp_path):
    app = make_app(tmp_path)
    assert manager_of(app).overview() == {"chats": [], "active": None, "running": False}
    assert not app.cfg.chats_dir.exists()          # the disk was not touched


# --------------------------------------------------------------------------
# A name that was skipped
# --------------------------------------------------------------------------

CONTEXT = "Backend developer, five years of Python. I'm interviewing for a senior position on the payments team."


async def wait_for_name(chats, chat_id, expected_state="auto", seconds=4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chat = chats.store.get(chat_id)
        if chat.name_state == expected_state:
            return chat
        await asyncio.sleep(0.05)
    return chats.store.get(chat_id)


@pytest.fixture
def quick_naming(monkeypatch):
    """Do not wait six seconds each time: same behavior, shorter deadline."""
    monkeypatch.setattr("argus.chats.manager.NAME_DELAY_S", 0.15)
    monkeypatch.setattr("argus.chats.manager.NAME_AFTER_ANSWER_S", 0.05)


async def test_a_skipped_name_is_a_placeholder_the_page_shows_empty(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("")
    assert (chat.name, chat.name_state) == ("New session", "default")
    assert chats.card(chat)["unnamed"] is True

    await chats.activate(chat.id)
    # A placeholder in the field would read as a real name that has to be erased.
    assert panel.values()[NAME_PATH] == ""
    field = panel.schema()["sections"][0]["settings"][0]
    assert field["path"] == NAME_PATH and "made up for you" in field["placeholder"]
    assert chats.brief()["name"] == "New session"


async def test_a_typed_name_is_never_replaced(tmp_path, quick_naming):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("My name")
    assert chat.name_state == "manual"
    await chats.activate(chat.id)

    assert panel.apply({SUMMARY_PATH: CONTEXT})["ok"]
    await asyncio.sleep(0.5)
    assert chats.store.get(chat.id).name == "My name"


async def test_an_unnamed_session_is_named_from_its_context(tmp_path, quick_naming):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)

    assert panel.apply({SUMMARY_PATH: CONTEXT})["ok"]
    named = await wait_for_name(chats, chat.id)
    assert (named.name, named.name_state) == ("Backend developer, five years of Python", "auto")
    # The header and the list already show the new name, and the settings field shows it too.
    assert chats.brief()["name"] == named.name
    assert panel.values()[NAME_PATH] == named.name
    assert chats.card(chats.active)["unnamed"] is False

    # A name made up once does not change with later edits of the description.
    assert panel.apply({SUMMARY_PATH: "A completely different conversation: a physics exam next week"})["ok"]
    await asyncio.sleep(0.5)
    assert chats.store.get(chat.id).name == named.name


async def test_a_short_context_does_not_name_anything(tmp_path, quick_naming):
    """You cannot invent a name from "Interview" (too short) — wait until the description
    becomes a description.
    """
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)
    assert panel.apply({SUMMARY_PATH: "Interview"})["ok"]
    await asyncio.sleep(0.5)
    assert chats.store.get(chat.id).name_state == "default"


async def test_naming_waits_until_the_person_stops_typing(tmp_path, monkeypatch):
    monkeypatch.setattr("argus.chats.manager.NAME_DELAY_S", 0.4)
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)

    assert panel.apply({SUMMARY_PATH: "Preparing for an interview about message queues"})["ok"]
    await asyncio.sleep(0.25)
    assert chats.store.get(chat.id).name_state == "default"      # still writing
    assert panel.apply({SUMMARY_PATH: "Preparing for an exam on databases, transactions and indexes"})["ok"]
    await asyncio.sleep(0.25)
    assert chats.store.get(chat.id).name_state == "default"      # the deadline moved
    named = await wait_for_name(chats, chat.id)
    assert named.name == "Preparing for an exam on databases"       # by the latest text


async def test_a_manual_rename_beats_a_pending_automatic_one(tmp_path, quick_naming):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)
    assert panel.apply({SUMMARY_PATH: CONTEXT})["ok"]
    chats.rename(chat.id, "Mine")                       # the person was faster
    await asyncio.sleep(0.5)
    saved = chats.store.get(chat.id)
    assert (saved.name, saved.name_state) == ("Mine", "manual")


async def test_a_session_without_context_is_named_from_its_first_question(tmp_path, quick_naming):
    """"Start right away": there is no description, but there is already a
    question and an answer.
    """
    app = make_app(tmp_path, chats_dir=True)
    build_panel(app)
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)

    await chats._record_answer(chat.id, answer("The operation is idempotent…", "What is idempotency?"), None)
    named = await wait_for_name(chats, chat.id)
    assert (named.name, named.name_state) == ("What is idempotency", "auto")


class TitleProvider:
    """A model that can only name."""

    def __init__(self, name="relay", reply="«Interview at Acme»", fail=False):
        self.name, self.reply, self.fail, self.prompts = name, reply, fail, []

    async def ask(self, prompt, system=None, image=None, pinned=(), deep=False):
        self.prompts.append((prompt, system))
        if self.fail:
            raise ConnectionError("no network")
        return Answer(text=self.reply, provider=self.name)

    async def start(self, system=None):
        return None

    async def warm(self, system=None, pinned=()):
        return None

    async def close(self):
        return None


async def test_the_model_names_the_session_when_it_can(tmp_path, quick_naming):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    app.assistant.provider = TitleProvider()
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)
    assert panel.apply({SUMMARY_PATH: CONTEXT})["ok"]

    named = await wait_for_name(chats, chat.id)
    assert named.name == "Interview at Acme"                 # quotes removed
    prompt_sent, system = app.assistant.provider.prompts[0]
    assert "Backend developer" in prompt_sent and "title" in system.lower()


@pytest.mark.parametrize("provider", [
    TitleProvider(fail=True), TitleProvider(reply="..."), TitleProvider(reply=""),
])
async def test_when_the_model_cannot_the_start_of_the_description_is_used(
    tmp_path, quick_naming, provider
):
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    app.assistant.provider = provider
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)
    assert panel.apply({SUMMARY_PATH: CONTEXT})["ok"]
    named = await wait_for_name(chats, chat.id)
    assert named.name == "Backend developer, five years of Python"


@pytest.mark.parametrize("name", ["claude-code", "stub"])
async def test_some_providers_are_never_asked_to_name_a_session(tmp_path, quick_naming, name):
    """`claude-code` keeps one live conversation: an extra question would stay in the model's
    memory and get in the way of the conversation's answers. The stub answers "one"."""
    app = make_app(tmp_path, chats_dir=True)
    panel = build_panel(app)
    app.assistant.provider = TitleProvider(name=name)
    chats = manager_of(app)
    chat = chats.create("")
    await chats.activate(chat.id)
    assert panel.apply({SUMMARY_PATH: CONTEXT})["ok"]
    named = await wait_for_name(chats, chat.id)
    assert named.name == "Backend developer, five years of Python"
    assert app.assistant.provider.prompts == []


def test_titles_from_the_start_of_a_description():
    from argus.chats.naming import clean_title, derive_title

    assert derive_title(CONTEXT) == "Backend developer, five years of Python"
    assert derive_title("I'm taking a relational database exam: normal forms, indexes.") == (
        "I'm taking a relational database exam")
    assert derive_title("  \n  interview at acme  ") == "Interview at acme"
    assert derive_title("12345 67890") == "" and derive_title("") == ""
    long_one = derive_title("Preparing for the final round of interviews at a big company on system "
                            "design and algorithms next week")
    assert len(long_one) <= 48 and not long_one.endswith((" at", " a", " of", " and", " on"))

    assert clean_title("«Interview at Acme»") == "Interview at Acme"
    assert clean_title("Title: Database exam.") == "Database exam"
    # The Russian "Title:" prefix is stripped too (language data, the model may answer in Russian).
    assert clean_title("Название: Экзамен по БД.") == "Экзамен по БД"
    assert clean_title("**Networking lecture**\nExplanation: …") == "Networking lecture"
    assert clean_title("") == clean_title("...") == clean_title("ok") == ""
    assert len(clean_title("word " * 40)) <= 60


def test_files_from_before_the_automatic_names_count_as_named_by_hand(tmp_path):
    """For sessions created earlier, the person wrote the name by hand: the app will not rename
    them."""
    import json

    store = ChatStore(tmp_path / "chats")
    chat = store.create("Old name", lambda _: {})
    path = store.folder(chat.id) / "chat.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["name_state"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get(chat.id).name_state == "manual"
    data["name_state"] = "something strange"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get(chat.id).name_state == "manual"


# --------------------------------------------------------------------------
# Pages and routes
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = make_app(tmp_path, chats_dir=True)
    with TestClient(build(app)) as http:
        http.app_obj = app
        yield http


def test_the_root_is_the_list_of_chats_and_the_dashboard_is_a_chat(client):
    home = client.get("/")
    assert home.status_code == 200
    assert 'id="new-chat"' in home.text
    assert served_from(home.text, "home.html")
    assert home.headers["X-Argus-Build"]
    assert client.get("/state").json()["chat"] is None

    made = client.post("/chats", json={"name": "Interview"}).json()
    assert made["ok"] and made["chat"]["name"] == "Interview"
    page = client.get(f"/chat/{made['chat']['id']}")
    assert page.status_code == 200
    assert served_from(page.text, "index.html")
    assert 'id="power"' in page.text
    assert client.get("/state").json()["chat"] == {
        "id": made["chat"]["id"], "name": "Interview", "unnamed": False}


def test_an_unknown_chat_sends_you_home(client):
    for path in ("/chat/0123456789", "/chat/not-a-chat"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/", path
    # An address that climbs out of the folder never even reaches the route.
    assert client.get("/chat/..%2Fx", follow_redirects=False).status_code in (302, 404)
    assert client.get("/state").json()["chat"] is None


def test_reloading_the_chat_page_keeps_the_conversation(client):
    chat = client.post("/chats", json={"name": "A"}).json()["chat"]
    client.get(f"/chat/{chat['id']}")
    client.app_obj.state.answers.append(answer("conversation in progress"))
    client.get(f"/chat/{chat['id']}")                      # F5
    assert [a["text"] for a in client.get("/state").json()["answers"]] == ["conversation in progress"]


def test_a_restored_screenshot_is_served_to_the_page(client):
    """The card of an old answer pulls the frame from the same address as a fresh one."""
    chat = client.post("/chats", json={"name": "A"}).json()["chat"]
    store = manager_of(client.app_obj).store
    jpeg = bytes([0xFF, 0xD8, 0xFF]) + b"x" * 2000
    store.save_shot(chat["id"], 111, jpeg)
    store.append(chat["id"], {"k": "a", "text": "answer", "cause": "question",
                              "image_kb": len(jpeg) // 1024, "image_src": "screen", "shot": 111})
    client.get(f"/chat/{chat['id']}")
    snap = client.get("/state").json()
    assert [sh["id"] for sh in snap["shots"]] == [restored_shot_id(chat["id"], 111)]
    got = client.get(f"/shot/{snap['shots'][0]['id']}.jpg")
    assert got.status_code == 200 and got.content == jpeg


def test_the_list_renames_copies_and_deletes(client):
    first = client.post("/chats", json={"name": "First"}).json()["chat"]
    second = client.post("/chats", json={"name": ""}).json()["chat"]
    assert second["name"] == "New session"
    assert second["unnamed"] is True and first["unnamed"] is False

    listing = client.get("/chats").json()
    assert {c["name"] for c in listing["chats"]} == {"First", "New session"}
    assert listing["active"] is None and listing["running"] is False

    assert client.patch(f"/chats/{first['id']}", json={"name": "Interview"}).json()["chat"]["name"] == "Interview"
    renamed = client.patch(f"/chats/{second['id']}", json={"name": "Own"}).json()["chat"]
    assert renamed["unnamed"] is False                  # written by hand: no longer a placeholder
    client.patch(f"/chats/{second['id']}", json={"name": "New session"})
    bad = client.patch(f"/chats/{first['id']}", json={"name": " "}).json()
    assert bad["ok"] is False and "empty" in bad["error"]

    copy = client.post(f"/chats/{first['id']}/duplicate").json()["chat"]
    assert copy["name"] == "Interview (copy)"

    client.get(f"/chat/{first['id']}")
    assert client.get("/chats").json()["active"] == first["id"]
    assert next(c for c in client.get("/chats").json()["chats"] if c["id"] == first["id"])["active"]

    assert client.delete(f"/chats/{first['id']}").json() == {"ok": True}
    assert client.get("/chats").json()["active"] is None
    assert first["id"] not in {c["id"] for c in client.get("/chats").json()["chats"]}


def test_unknown_or_hostile_ids_are_not_found(client):
    assert client.patch("/chats/0123456789", json={"name": "x"}).status_code == 404
    assert client.delete("/chats/0123456789").status_code == 404
    assert client.post("/chats/0123456789/duplicate").status_code == 404
    assert client.delete("/chats/..").status_code in (404, 405)


def test_the_home_page_asks_only_for_the_general_settings(client):
    home = client.get("/").text
    assert 'document.body.dataset.settingsScope = "app"' in home
    sections = client.get("/admin/schema", params={"scope": "app"}).json()["sections"]
    assert {s["id"] for s in sections} == {"hotkeys", "audio", "tts", "interface"}
    assert chats_of(client.app_obj) is not None
