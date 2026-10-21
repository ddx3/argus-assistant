"""Checks for the settings layer that the admin page edits.

The errors here are silent and expensive. A setting was saved but did not reach the
pipeline: the person turns a threshold and cannot understand why nothing changes. Or the
other way around: it did reach it, but a different value landed in the file, and after a
restart everything came back. Neither raises an exception, so they are checked
separately.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field, ValidationError

from argus.app import Argus
from argus.core.config import Config
from argus.core.settings_store import (
    LOCAL_NAME,
    SettingsStore,
    deep_merge,
    drop_path,
    flatten,
)
from argus.ui.admin_base import AdminPanel, Section, Setting, adopt

TOML = """
[stt]
language = "ru"
min_confidence = 0.35

[gate_brain]
cooldown_s = 8.0
"""


@pytest.fixture
def project(tmp_path):
    """A working copy on disk: its own argus.toml and its own place for edits."""
    (tmp_path / "argus.toml").write_text(TOML, encoding="utf-8")
    return tmp_path


def load(project, **kw) -> Config:
    return Config.load(project / "argus.toml", **kw)


# --------------------------------------------------------------------------
# The settings dictionary
# --------------------------------------------------------------------------


def test_deep_merge_keeps_untouched_branches():
    base = {"stt": {"model": "a", "language": "ru"}, "vad": {"threshold": 0.5}}
    out = deep_merge(base, {"stt": {"model": "b"}})
    assert out == {"stt": {"model": "b", "language": "ru"}, "vad": {"threshold": 0.5}}
    assert base["stt"]["model"] == "a"      # the source is untouched


def test_list_replaces_whole():
    """A list is one value, not a set to append to.

    Otherwise "remove a window from skip_windows" would never work: the old item would stay
    in the file, and the person would decide that the setting does not work.
    """
    out = deep_merge({"screen": {"skip": ["Argus", "Chrome"]}}, {"screen": {"skip": ["Argus"]}})
    assert out["screen"]["skip"] == ["Argus"]


def test_drop_path_removes_empty_tables():
    data = {"stt": {"model": "x"}}
    drop_path(data, "stt.model")
    assert data == {}


def test_flatten_keeps_empty_dict_as_leaf():
    assert flatten({"channel": {}}) == {"channel": {}}


# --------------------------------------------------------------------------
# Three layers
# --------------------------------------------------------------------------


def test_overrides_win_over_file(project):
    (project / LOCAL_NAME).write_text(json.dumps({"stt": {"language": "en"}}), encoding="utf-8")
    assert load(project).stt.language == "en"
    assert load(project, overrides=False).stt.language == "ru"


def test_broken_overrides_do_not_break_startup(project):
    """The file is machine-written, but it sits next to the person, so it can get damaged."""
    (project / LOCAL_NAME).write_text("{ not json", encoding="utf-8")
    assert load(project).stt.language == "ru"


def test_defaults_survive_missing_sections(project):
    """argus.toml has no ui section; the port must still be there."""
    assert load(project).ui.port == 8420


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_apply_writes_only_the_difference(project):
    store = SettingsStore(load(project))
    store.apply({"gate_brain.cooldown_s": 20.0, "stt.language": "ru"})

    saved = json.loads((project / LOCAL_NAME).read_text(encoding="utf-8"))
    # What was changed is written, and **only** that: a value that matches the project file
    # does not go into the local file, otherwise within a month it becomes a copy of the
    # config, and it is no longer visible what the person actually turned.
    assert saved == {"gate_brain": {"cooldown_s": 20.0}}


def test_returning_value_back_clears_the_override(project):
    store = SettingsStore(load(project))
    store.apply({"gate_brain.cooldown_s": 20.0})
    store.apply({"gate_brain.cooldown_s": 8.0})
    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {}


def test_apply_reports_what_really_changed(project):
    store = SettingsStore(load(project))
    _, changed = store.apply({"gate_brain.cooldown_s": 20.0, "gate_brain.max_calls": 200})
    # `max_calls` was 200 and stayed 200, so there is nothing to rebuild because of it.
    assert changed == {"gate_brain.cooldown_s"}


def test_bad_value_is_rejected_before_writing(project):
    """Validation happens before the write, not after.

    Saving an unusable value and crashing on the next launch is the worst outcome: the
    pipeline will not come up, and the fix has to be made in the file by hand and blindly.
    """
    store = SettingsStore(load(project))
    with pytest.raises(ValidationError):
        store.build({"vad.threshold": 9.0})
    assert not (project / LOCAL_NAME).exists()


def test_reset_returns_to_the_project_file(project):
    store = SettingsStore(load(project))
    store.apply({"stt.language": "en", "ui.port": 8422})
    cfg, changed = store.reset(None)
    assert cfg.stt.language == "ru"
    assert cfg.ui.port == 8420
    assert {"stt.language", "ui.port"} <= changed


def test_frozen_paths_are_not_writable(project):
    store = SettingsStore(load(project))
    store.apply({"root": "C:/nope"})
    assert json.loads((project / LOCAL_NAME).read_text(encoding="utf-8")) == {}


def test_channel_section_is_free_form(project):
    """The core carries the channel settings over without looking inside."""
    store = SettingsStore(load(project))
    cfg, _ = store.apply({"channel.assist.profile.stack": "Python, asyncio"})
    assert cfg.channel["assist"]["profile"]["stack"] == "Python, asyncio"
    assert load(project).channel["assist"]["profile"]["stack"] == "Python, asyncio"


# --------------------------------------------------------------------------
# Applying on a live pipeline
# --------------------------------------------------------------------------


def test_adopt_updates_objects_in_place():
    """References handed out at build time must see the change.

    The pipeline hands out `cfg.brain`, `cfg.ocr` and the rest to its nodes at startup.
    Replacing `app.cfg` wholesale would leave all of them on the old object, and the setting
    would silently reach nowhere, without a single error in the log.
    """
    cfg = Config()
    brain = cfg.brain                      # the way the Assistant holds it
    fresh = Config.model_validate({"brain": {"max_context_chars": 4321}})
    adopt(cfg, fresh)
    assert brain.max_context_chars == 4321
    assert cfg.brain is brain


def test_adopt_keeps_project_root():
    cfg = Config()
    cfg.root = cfg.root / "somewhere"
    adopt(cfg, Config())
    assert cfg.root.name == "somewhere"


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------


class ChannelSettings(BaseModel):
    persona: str = "neutral"
    heat: float = Field(default=0.5, ge=0.0, le=1.0)


def panel(project, **kw) -> AdminPanel:
    app = Argus(load(project), with_screen=False, with_brain=False)
    sections = [
        Section("main", "Main", settings=[
            Setting("vad.threshold", "Speech threshold", kind="float"),
            Setting("stt.language", "Language", kind="line", hot=False),
            Setting("channel.test.heat", "Heat", kind="float"),
        ])
    ]
    return AdminPanel(
        app, title="Test", subtitle="", channel="test", accent="#fff",
        sections=sections, page=project / "admin.html",
        channel_model=ChannelSettings, **kw,
    )


def test_limits_come_from_the_config_models(project):
    """A range is not rewritten by hand a second time.

    Written twice, it would drift apart on the very first config change, and the admin page
    would start accepting values that the pipeline later rejects.
    """
    schema = panel(project).schema()
    field = schema["sections"][0]["settings"][0]
    assert (field["min"], field["max"]) == (0.1, 0.95)


def test_channel_limits_come_from_the_channel_model(project):
    field = panel(project).schema()["sections"][0]["settings"][2]
    assert (field["min"], field["max"]) == (0.0, 1.0)


def test_version_falls_back_to_source_copy_label(project):
    """A working copy from source does not carry VERSION (only packaging/stage.py puts it next
    to a built .exe); for the version there you ask git log, and the settings page honestly
    says that this is not a build.
    """
    assert panel(project).schema()["version"] == {"number": "source", "build": ""}


def test_version_splits_number_and_build(project):
    (project / "VERSION").write_text("1.0+abc123", encoding="utf-8")
    assert panel(project).schema()["version"] == {"number": "1.0", "build": "abc123"}


def test_channel_defaults_show_up_on_first_visit(project):
    """The channel section is not in the config yet, but the fields must not be empty."""
    assert panel(project).schema()["values"]["channel.test.persona"] == "neutral"


def test_panel_ignores_unknown_paths(project):
    p = panel(project)
    p.apply({"stt.beam_size": 1})          # there is no such field on the page
    assert not p.store.changed()


def test_panel_reports_bad_value_by_field(project):
    p = panel(project)
    out = p.apply({"vad.threshold": 42.0})
    assert out["ok"] is False
    assert "vad.threshold" in out["errors"]


def test_panel_validates_channel_settings(project):
    p = panel(project)
    out = p.apply({"channel.test.heat": 5.0})
    assert out["ok"] is False


def test_cold_setting_asks_for_restart(project):
    p = panel(project)
    out = p.apply({"stt.language": "en"})
    assert out["restart"] == ["stt.language"]

    # The badge stays until a restart: if it vanished on a page reload, the person would go
    # off to work on the old settings, thinking they were new.
    assert p.schema()["restart_pending"] == ["stt.language"]


def test_hot_setting_reaches_the_pipeline(project):
    p = panel(project)
    p.sections[0].settings.append(Setting("gate_brain.cooldown_s", "Pause", kind="float"))
    p._index["gate_brain.cooldown_s"] = p.sections[0].settings[-1]
    p.app.assistant = None
    p.apply({"gate_brain.cooldown_s": 33.0})
    assert p.app.cfg.gate_brain.cooldown_s == 33.0
    assert p.apply({"gate_brain.cooldown_s": 33.0})["restart"] == []
