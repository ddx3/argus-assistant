"""The overrides file `argus.local.json`: reading, writing, backup, concurrent editing.

The file is personal: it holds the person's settings, and they must not be lost. A
healthy file must behave exactly as before, and one that is damaged, unreadable, or
intercepted by someone else's write must not vanish silently. The errors here are silent:
none of these places raises an exception, the settings are just gone the next time.
"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

from argus.core import settings_store
from argus.core.config import Config
from argus.core.settings_store import (
    LOCAL_NAME,
    SettingsStore,
    _backup_path,
    read_overrides,
    write_overrides,
)

TOML = """
[stt]
language = "ru"

[gate_brain]
cooldown_s = 8.0
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "argus.toml").write_text(TOML, encoding="utf-8")
    return tmp_path


def load(project) -> Config:
    return Config.load(project / "argus.toml")


def bad_files(root):
    return sorted(root.glob(LOCAL_NAME + ".bad-*"))


@pytest.fixture
def dev_build(tmp_path, monkeypatch):
    """A developer's built app: `<copy>/dist/Argus`, with `pyproject.toml` at the root of the
    copy.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    root = repo / "dist" / "Argus"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """An installed program: `Desktop/Argus`, with no sources next to it."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    root = tmp_path / "Desktop" / "Argus"
    root.mkdir(parents=True)
    return root


# --------------------------------------------------------------------------
# Reading: a healthy file as before, other encodings are not lost
# --------------------------------------------------------------------------


def test_a_healthy_file_is_read_as_before(tmp_path):
    (tmp_path / LOCAL_NAME).write_text(
        json.dumps({"stt": {"language": "en"}}), encoding="utf-8")
    assert read_overrides(tmp_path) == {"stt": {"language": "en"}}
    assert not bad_files(tmp_path)


def test_a_file_with_a_bom_is_not_lost(tmp_path):
    """Notepad and PowerShell 5.1 append a BOM; such a file used to be considered corrupt."""
    (tmp_path / LOCAL_NAME).write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"stt": {"language": "en"}}).encode("utf-8"))
    assert read_overrides(tmp_path) == {"stt": {"language": "en"}}
    assert not bad_files(tmp_path)


def test_a_utf16_file_is_read(tmp_path):
    (tmp_path / LOCAL_NAME).write_bytes(
        json.dumps({"stt": {"language": "日本語"}}).encode("utf-16"))
    assert read_overrides(tmp_path) == {"stt": {"language": "日本語"}}


# --------------------------------------------------------------------------
# A damaged file is set aside, not overwritten
# --------------------------------------------------------------------------


def test_a_damaged_file_is_set_aside_not_overwritten(project):
    broken = '{"stt": {"language": "en"},'            # cut off, as after a power loss
    (project / LOCAL_NAME).write_text(broken, encoding="utf-8")

    store = SettingsStore(load(project))
    assert store.overrides == {}

    aside = bad_files(project)
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == broken
    # The very first write from the page does not erase what was set aside.
    store.apply({"gate_brain.cooldown_s": 20.0})
    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {
        "gate_brain": {"cooldown_s": 20.0}}
    assert aside[0].read_text(encoding="utf-8") == broken


@pytest.mark.parametrize("text", ["[1, 2]", "null", '"text"', ""])
def test_a_file_that_is_not_an_object_is_set_aside(tmp_path, text):
    (tmp_path / LOCAL_NAME).write_text(text, encoding="utf-8")
    assert read_overrides(tmp_path) == {}
    assert len(bad_files(tmp_path)) == 1
    assert not (tmp_path / LOCAL_NAME).exists()


def test_an_unreadable_file_is_left_where_it_is(tmp_path, monkeypatch):
    """The file may be intact and just busy with another program: it must not be set aside."""
    path = tmp_path / LOCAL_NAME
    path.write_text('{"stt": {"language": "en"}}', encoding="utf-8")

    real = type(path).read_bytes

    def locked(self):
        if self.name == LOCAL_NAME:
            raise PermissionError(13, "busy with another process")
        return real(self)

    monkeypatch.setattr(type(path), "read_bytes", locked)
    assert read_overrides(tmp_path) == {}
    monkeypatch.undo()

    assert path.exists() and not bad_files(tmp_path)


# --------------------------------------------------------------------------
# Backup: only for the developer build
# --------------------------------------------------------------------------


def test_the_dev_build_keeps_a_backup_next_to_dist(dev_build):
    write_overrides(dev_build, {"stt": {"language": "en"}})
    backup = _backup_path(dev_build)
    assert backup.parent == dev_build.parent
    assert json.loads(backup.read_text(encoding="utf-8")) == {"stt": {"language": "en"}}


def test_the_dev_build_gets_its_file_back_after_a_rebuild(dev_build):
    write_overrides(dev_build, {"stt": {"language": "en"}})
    (dev_build / LOCAL_NAME).unlink()                  # a rebuild took the folder away
    assert read_overrides(dev_build) == {"stt": {"language": "en"}}
    assert (dev_build / LOCAL_NAME).exists()


def test_a_damaged_main_file_is_restored_from_the_backup(dev_build):
    """Scenario from the audit: the main file is corrupted, the backup is intact; everything used
    to be lost.
    """
    write_overrides(dev_build, {"stt": {"language": "en"}})
    (dev_build / LOCAL_NAME).write_text("{ not json", encoding="utf-8")

    assert read_overrides(dev_build) == {"stt": {"language": "en"}}
    # The original did not vanish: it lies alongside, and the backup still holds the old
    # content.
    assert [p.read_text(encoding="utf-8") for p in bad_files(dev_build)] == ["{ not json"]
    # The next write does not impoverish the backup: it holds both the old and the new.
    write_overrides(dev_build, {"stt": {"language": "en"}, "gate_brain": {"max_calls": 5}})
    saved = json.loads(_backup_path(dev_build).read_text(encoding="utf-8"))
    assert saved["stt"] == {"language": "en"}


def test_a_damaged_backup_does_not_break_the_start(dev_build):
    _backup_path(dev_build).write_text("not json", encoding="utf-8")
    assert read_overrides(dev_build) == {}
    # The next write will update the backup, so a damaged one is set aside, not overwritten.
    aside = sorted(dev_build.parent.glob(".*backup.bad-*"))
    assert [p.read_text(encoding="utf-8") for p in aside] == ["not json"]


def test_an_installed_program_writes_no_backup_outside_its_folder(installed):
    """A backup next to a foreign folder (Desktop, Downloads) outlived the uninstall of the
    program.
    """
    write_overrides(installed, {"stt": {"language": "en"}})
    assert (installed / LOCAL_NAME).exists()
    assert not list(installed.parent.glob(".*backup*"))
    assert not _backup_path(installed).exists()


def test_deleting_the_file_of_an_installed_program_resets_it(installed):
    write_overrides(installed, {"stt": {"language": "en"}})
    (installed / LOCAL_NAME).unlink()
    assert read_overrides(installed) == {}
    assert not (installed / LOCAL_NAME).exists()


def test_source_runs_keep_no_backup(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    write_overrides(tmp_path, {"a": 1})
    assert not _backup_path(tmp_path).exists()


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_the_temp_file_reaches_the_disk_before_the_swap(tmp_path, monkeypatch):
    """Without `fsync`, a power cut left the new name with empty contents."""
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])

    write_overrides(tmp_path, {"a": 1})
    assert events == ["fsync", "replace"]


def test_a_failed_swap_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    write_overrides(tmp_path, {"a": 1})

    def boom(src, dst):
        raise OSError(28, "no space left")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_overrides(tmp_path, {"a": 2})
    monkeypatch.undo()

    assert json.loads((tmp_path / LOCAL_NAME).read_text(encoding="utf-8")) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_busy_target_is_retried(tmp_path, monkeypatch):
    """An antivirus or an indexer holds the file for a fraction of a second: the replacement must
    not give up.
    """
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "busy")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(settings_store.time, "sleep", lambda s: None)
    write_overrides(tmp_path, {"a": 1})
    assert calls["n"] == 3
    assert json.loads((tmp_path / LOCAL_NAME).read_text(encoding="utf-8")) == {"a": 1}


def test_a_failed_save_leaves_no_ghost_for_the_next_one(project, monkeypatch):
    """A value that failed to be written must not end up in the file with the next edit."""
    store = SettingsStore(load(project))
    real = settings_store.write_overrides
    state = {"fail": True}

    def flaky(root, data):
        if state["fail"]:
            raise OSError(28, "no space left")
        return real(root, data)

    monkeypatch.setattr(settings_store, "write_overrides", flaky)
    with pytest.raises(OSError):
        store.apply({"gate_brain.cooldown_s": 21.0})
    assert store.overrides == {}
    assert store.values()["gate_brain.cooldown_s"] == 8.0

    state["fail"] = False
    store.apply({"gate_brain.max_calls": 5})
    saved = json.loads((project / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved == {"gate_brain": {"max_calls": 5}}
    assert store.values()["gate_brain.cooldown_s"] == 8.0


# --------------------------------------------------------------------------
# The running app and other processes
# --------------------------------------------------------------------------


def test_a_save_keeps_what_another_process_wrote(project):
    """`argus sync-voices` wrote while the app was running, and the app's next write erased it."""
    running = SettingsStore(load(project))
    cli = SettingsStore(load(project))
    cli.apply({"channel.tts.characters.anna.voice_id": "abc123",
               "channel.tts.characters.anna.label": "Anna"})

    _, changed = running.apply({"gate_brain.cooldown_s": 20.0})

    saved = json.loads((project / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["gate_brain"] == {"cooldown_s": 20.0}
    assert saved["channel"]["tts"]["characters"]["anna"] == {"voice_id": "abc123", "label": "Anna"}
    # The live pipeline is told too: otherwise the page would show what it does not have.
    assert "channel.tts.characters.anna.voice_id" in changed
    assert running.values()["channel.tts.characters.anna.voice_id"] == "abc123"


def test_a_save_without_outside_changes_reports_only_its_own_paths(project):
    store = SettingsStore(load(project))
    _, changed = store.apply({"gate_brain.cooldown_s": 20.0, "gate_brain.max_calls": 200})
    assert changed == {"gate_brain.cooldown_s"}


def test_reset_of_one_path_keeps_what_another_process_wrote(project):
    running = SettingsStore(load(project))
    running.apply({"gate_brain.cooldown_s": 20.0})
    SettingsStore(load(project)).apply({"channel.tts.characters.anna.voice_id": "abc123"})

    running.reset(["gate_brain.cooldown_s"])

    saved = json.loads((project / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved == {"channel": {"tts": {"characters": {"anna": {"voice_id": "abc123"}}}}}


def test_a_file_damaged_under_a_running_app_is_rewritten_from_memory(project):
    store = SettingsStore(load(project))
    store.apply({"gate_brain.cooldown_s": 20.0})
    (project / LOCAL_NAME).write_text("{ not json", encoding="utf-8")

    store.apply({"gate_brain.max_calls": 5})

    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {
        "gate_brain": {"cooldown_s": 20.0, "max_calls": 5}}
    assert [p.read_text(encoding="utf-8") for p in bad_files(project)] == ["{ not json"]


def test_a_file_removed_under_a_running_app_is_rewritten_from_memory(project):
    """The file was taken away (rebuild, antivirus), but the app keeps living with these values:
    one edit must not reset everything else on the fly."""
    store = SettingsStore(load(project))
    store.apply({"gate_brain.cooldown_s": 20.0})
    (project / LOCAL_NAME).unlink()

    _, changed = store.apply({"gate_brain.max_calls": 5})

    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {
        "gate_brain": {"cooldown_s": 20.0, "max_calls": 5}}
    assert changed == {"gate_brain.max_calls"}
    assert store.values()["gate_brain.cooldown_s"] == 20.0


def test_a_removed_main_file_does_not_empty_the_backup_of_a_running_dev_build(dev_build):
    (dev_build / "argus.toml").write_text(TOML, encoding="utf-8")
    store = SettingsStore(Config.load(dev_build / "argus.toml"))
    store.apply({"gate_brain.cooldown_s": 20.0})
    (dev_build / LOCAL_NAME).unlink()

    store.apply({"gate_brain.max_calls": 5})

    saved = json.loads(_backup_path(dev_build).read_text(encoding="utf-8"))
    assert saved == {"gate_brain": {"cooldown_s": 20.0, "max_calls": 5}}


def test_an_unreadable_file_fails_the_save_instead_of_wiping_it(project, monkeypatch):
    store = SettingsStore(load(project))
    store.apply({"gate_brain.cooldown_s": 20.0})
    path = project / LOCAL_NAME
    real = type(path).read_bytes

    def locked(self):
        if self.name == LOCAL_NAME:
            raise PermissionError(13, "busy")
        return real(self)

    monkeypatch.setattr(type(path), "read_bytes", locked)
    with pytest.raises(PermissionError):
        store.apply({"gate_brain.max_calls": 5})
    monkeypatch.undo()

    assert json.loads(path.read_text(encoding="utf-8")) == {"gate_brain": {"cooldown_s": 20.0}}


def test_parallel_saves_all_land(project):
    """The microphone choice comes from a worker thread and can overlap with the settings page."""
    store = SettingsStore(load(project))
    errors: list[BaseException] = []

    def save(i: int) -> None:
        try:
            for _ in range(5):
                store.apply({f"channel.t{i}.x": i})
        except BaseException as e:      # noqa: BLE001 — collected in order to show them
            errors.append(e)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    saved = json.loads((project / LOCAL_NAME).read_text(encoding="utf-8"))
    assert {k: v["x"] for k, v in saved["channel"].items()} == {f"t{i}": i for i in range(8)}


# --------------------------------------------------------------------------
# A value that the model does not accept
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    {"ui": {"port": 80}},
    {"brain": {"cache_ttl": "2h"}},
    {"screen": None},
    {"ui": 5},
    {"audio": {"mic_device": ["a list", "not a name"]}},
    {"audio": {"mic_device": {"name": "a dict, not a name"}}},
])
def test_a_value_the_models_reject_does_not_stop_the_start(project, bad, caplog):
    """A release narrowed the allowed values, or the person made a mistake by hand: startup does
    not fail.
    """
    good = {"gate_brain": {"cooldown_s": 20.0}}
    text = json.dumps({**good, **bad}, ensure_ascii=False)
    (project / LOCAL_NAME).write_text(text, encoding="utf-8")

    with caplog.at_level("WARNING"):
        cfg = load(project)

    assert cfg.gate_brain.cooldown_s == 20.0           # the neighboring change survived
    assert cfg.ui.port == 8420
    assert "the value is invalid and is not applied" in caplog.text
    # The file is the person's own; it is not rewritten by itself.
    assert (project / LOCAL_NAME).read_text(encoding="utf-8") == text


def test_the_store_survives_a_bad_value_in_the_file(project):
    (project / LOCAL_NAME).write_text(
        json.dumps({"ui": {"port": 80}, "gate_brain": {"cooldown_s": 20.0}}), encoding="utf-8")
    store = SettingsStore(load(project))

    store.apply({"gate_brain.max_calls": 5})            # the page is not locked by someone else's value

    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {
        "gate_brain": {"cooldown_s": 20.0, "max_calls": 5}}


def test_a_broken_project_file_still_fails_loudly(project):
    """This is no longer the person's edits but the project's own `argus.toml`: an error in it
    must be known about.
    """
    from pydantic import ValidationError

    (project / "argus.toml").write_text("[ui]\nport = 80\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load(project)


# --------------------------------------------------------------------------
# Settings page: a write failure is a response, not a server error
# --------------------------------------------------------------------------


def _panel(project):
    from argus.app import Argus
    from argus.ui.admin_base import AdminPanel, Section, Setting

    app = Argus(load(project), with_screen=False, with_brain=False)
    sections = [Section("main", "Main", settings=[
        Setting("vad.threshold", "Speech threshold", kind="float"),
        Setting("gate_brain.cooldown_s", "Pause", kind="float"),
    ])]
    return AdminPanel(app, title="Test", subtitle="", channel="test", accent="#fff",
                      sections=sections, page=project / "admin.html")


def test_the_panel_reports_a_failed_save_by_field_and_changes_nothing(project, monkeypatch):
    panel = _panel(project)

    def boom(root, data):
        raise OSError(28, "no space left")

    monkeypatch.setattr(settings_store, "write_overrides", boom)
    out = panel.apply({"vad.threshold": 0.6})
    monkeypatch.undo()

    assert out["ok"] is False
    assert "couldn't save the settings" in out["errors"]["vad.threshold"]
    assert "no space left" not in out["errors"]["vad.threshold"]       # we do not show the raw OS text
    assert panel.app.cfg.vad.threshold == 0.5                        # the pipeline did not get a phantom
    assert panel.store.overrides == {}
    # The next save goes as if the failure never happened.
    assert panel.apply({"gate_brain.cooldown_s": 30.0})["ok"] is True
    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {
        "gate_brain": {"cooldown_s": 30.0}}


def test_threads_asking_for_the_store_get_the_same_one(project):
    """Without a lock, the microphone choice (a worker thread) and the page (the event loop) each
    created their own store.
    """
    from types import SimpleNamespace

    from argus.core.settings_store import store_of

    app = SimpleNamespace(cfg=load(project))
    got: list[object] = []
    threads = [threading.Thread(target=lambda: got.append(store_of(app))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(s) for s in got}) == 1


def test_a_voice_added_from_the_terminal_survives_the_running_apps_next_save(project):
    """Scenario from the audit: `argus sync-voices` appended a voice, and the next write erased
    it.
    """
    from argus import cli

    running = SettingsStore(load(project))                       # the app is already running
    cli._save_character(load(project), "voice-aaa1", "Anna", hint=False)

    running.apply({"gate_brain.cooldown_s": 20.0})                # the person edits a different setting

    saved = json.loads((project / LOCAL_NAME).read_text(encoding="utf-8"))
    assert saved["channel"]["tts"]["characters"]["anna"]["voice_id"] == "voice-aaa1"
    assert saved["gate_brain"] == {"cooldown_s": 20.0}
