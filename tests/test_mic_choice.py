"""Microphone choice: pinning, remembering and handing back to the system.

Two things that break silently. First: the choice did not reach the pipeline. The person
tapped the list, the level bar stayed dead, and there is no way to tell who is being
listened to now. Second: the choice did not survive a restart. The next day the person
fixes the sound again and does not understand why.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from argus.app import Argus
from argus.core.config import Config
from argus.core.settings_store import LOCAL_NAME

TOML = """
[audio]
chunk_ms = 32
"""


@pytest.fixture
def app(tmp_path):
    """An app with a real config on disk, but without hardware."""
    (tmp_path / "argus.toml").write_text(TOML, encoding="utf-8")
    cfg = Config.load(tmp_path / "argus.toml")
    argus = Argus(cfg, with_screen=False, with_brain=False)

    # We do not open sound: the test is about device choice, not about PortAudio.
    reopened: list[str | None] = []
    argus.reopen_audio = lambda: reopened.append(argus.cfg.audio.mic_device) or {}
    argus.reopened = reopened
    return argus


def saved(app: Argus) -> dict:
    path = app.cfg.root / LOCAL_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_choice_reaches_the_pipeline(app):
    """A choice without reconnecting is a choice nobody can hear."""
    app.set_mic_device("HyperX Cloud Alpha")
    assert app.cfg.audio.mic_device == "HyperX Cloud Alpha"
    assert app.reopened == ["HyperX Cloud Alpha"]


def test_choice_survives_a_restart(app):
    """Otherwise the next day the sound has to be fixed again."""
    app.set_mic_device("HyperX Cloud Alpha")
    assert saved(app)["audio"]["mic_device"] == "HyperX Cloud Alpha"
    assert Config.load(app.cfg.config_path).audio.mic_device == "HyperX Cloud Alpha"


def test_empty_choice_returns_control_to_the_system(app):
    """An empty string means "choose yourself", not a device with an empty name."""
    app.set_mic_device("HyperX Cloud Alpha")
    app.set_mic_device("")
    assert app.cfg.audio.mic_device is None
    # The value returned to the original, so it is no longer in the file either: the local
    # file is a list of what the person changed, not a second config.
    assert saved(app) == {}


def _listening_to_a_non_default_mic(app):
    """Capture is "open": we listen to the "Built-in microphone", while
    the system considers "HyperX" the current one.

    The previous version of the test did not open sound at all: `system_defaults()` without
    an open capture returns `{}`, so there were no complaints under any pinning, and the test
    could not fail. Here the device is set by hand, so the branch "we listen to something
    other than what the system does" is reachable.
    """
    audio_module = pytest.importorskip("argus.capture.audio")
    audio = audio_module.AudioCapture(app.bus, app.clock, app.cfg.audio)
    audio._pa = object()                                      # capture is up, so there is something to compare with
    audio._defaults = {"in": "HyperX Cloud Alpha", "out": "Speakers"}
    audio._defaults_at = time.monotonic()                     # the answer is fresh: polling does not start
    device = SimpleNamespace(name="Built-in microphone", index=1, missed=None)
    audio.mic = SimpleNamespace(device=device, alive=True, silent_s=0.0)
    return audio


def test_pinned_device_is_not_a_complaint(app):
    """Since the person chose it themselves, "not the system default" is their decision.

    Without this the complaint would hang over the screen permanently: the person pinned a
    headset, the system considers the built-in input current, and the indicator would nag
    about a setup that works fine.
    """
    audio = _listening_to_a_non_default_mic(app)

    app.cfg.audio.mic_device = None
    assert [c for c in audio.complaints() if "listening to the microphone" in c], "without pinning there must be a complaint"

    app.cfg.audio.mic_device = "Built-in"
    assert not [c for c in audio.complaints() if "listening to the microphone" in c]


def test_nothing_is_written_without_a_project_config(tmp_path):
    """This is how tests build `Config()`; they must not write a file into the working copy.

    The root is deliberately replaced with a temporary folder. The first version of the check
    looked for "no file" in the default root, and that is a false check: the working copy
    already has an `argus.local.json` with channel settings, so it passed or failed depending
    on someone else's state.
    """
    cfg = Config()
    cfg.root = tmp_path
    argus = Argus(cfg, with_screen=False, with_brain=False)
    argus.reopen_audio = dict
    argus.set_mic_device("HyperX")

    assert argus.cfg.audio.mic_device == "HyperX"
    assert not (tmp_path / LOCAL_NAME).exists()
