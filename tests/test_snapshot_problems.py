"""Complaints in the state snapshot and the `audio_problem` flag.

The page (`index.html`) shows "Reconnect audio" only next to a
complaint about audio: a busy hotkey, a dead screen capture, a recognition complaint, a
recording failure and invalid settings are not cured by reconnecting audio. Before, the
snapshot did not send the flag, and the button appeared next to any complaint. Invalid
assistant settings did not get into the list at all: one wrong field was silently
replaced by the ordinary value, and the person did not know why the answer came out
different from what they had chosen.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from argus.app import Argus
from argus.assist import settings as assist_settings
from argus.core.config import Config
from argus.ui.server import STATIC, snapshot

BAD_SETTING = {"profile": {"years": 61}}          # experience out of range: the field will be replaced by the ordinary value


class FakeAudio:
    """Audio capture without hardware: exactly what `Argus.problems()` and the snapshot ask it
    for.
    """

    def __init__(self, *, healthy=True, error=None, complaints=()):
        self.healthy = healthy
        self._error = error
        self._complaints = list(complaints)

    def first_error(self):
        return self._error

    def complaints(self):
        return list(self._complaints)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """An app where everything is fine: the sound is intact and quiet, and there are no key
    combinations.
    """
    cfg = Config()
    cfg.root = tmp_path                       # chats and materials go into a temporary folder
    app = Argus(cfg, with_screen=False, with_brain=False)
    app.audio = FakeAudio()
    monkeypatch.setattr("argus.desktop.hotkeys", lambda: None)
    return app


# --------------------------------------------------------------------------
# The `audio_problem` flag
# --------------------------------------------------------------------------


def test_a_healthy_pipeline_has_no_problems_and_no_audio_flag(app):
    s = json.loads(json.dumps(snapshot(app)))              # the snapshot goes into the websocket as JSON
    assert s["problems"] == []
    assert s["audio_problem"] is False


@pytest.mark.parametrize(
    "audio",
    [
        FakeAudio(healthy=False, error=RuntimeError("stream closed")),
        FakeAudio(healthy=False),
        FakeAudio(complaints=["the pinned device (mic) “Headset” wasn't found — listening to “Microphone”"]),
        FakeAudio(complaints=["microphone “Headset” has been silent for 40 s — check that it's on"]),
        FakeAudio(complaints=["system audio is going elsewhere: listening to “A”, but Windows plays to “B” — reconnect the audio"]),
        FakeAudio(healthy=False, complaints=["the audio of window “Browser” is gone — was the app closed?"]),
    ],
    ids=["stream-died", "no-stream", "pinned-mic-missing", "mic-silent", "loopback-elsewhere",
         "stream-died-and-window-gone"],
)
def test_a_problem_of_the_sound_itself_raises_the_audio_flag(app, audio):
    app.audio = audio
    s = snapshot(app)
    assert s["problems"], "the complaint about the sound must be in the list too"
    assert s["audio_problem"] is True


def _hotkey_is_busy(app, monkeypatch):
    keys = SimpleNamespace(problems=["“ctrl+alt+a” — busy with another program"])
    monkeypatch.setattr("argus.desktop.hotkeys", lambda: keys)
    return "“ctrl+alt+a”"


def _screen_is_dead(app, monkeypatch):
    app.screen = SimpleNamespace(alive=False, error="window closed")
    return "screen:"


def _recording_failed(app, monkeypatch):
    app.record_error = "disk full"
    return "recording:"


def _recognition_failed(app, monkeypatch):
    app.speech.warm_error = "no connection"
    return "recognition:"


def _a_setting_is_wrong(app, monkeypatch):
    app.cfg.channel[assist_settings.KEY] = BAD_SETTING
    return assist_settings.problems(app.cfg)[0]


@pytest.mark.parametrize(
    "break_it",
    [_hotkey_is_busy, _screen_is_dead, _recording_failed, _recognition_failed, _a_setting_is_wrong],
    ids=lambda f: f.__name__.removeprefix("_"),
)
def test_a_problem_that_is_not_about_sound_does_not_raise_the_audio_flag(app, monkeypatch, break_it):
    """The sound is intact and the complaint is a different one: the "Reconnect audio"
    button is not needed here.
    """
    start = break_it(app, monkeypatch)
    s = snapshot(app)
    assert len(s["problems"]) == 1 and s["problems"][0].startswith(start), s["problems"]
    assert s["audio_problem"] is False


def test_sound_and_other_problems_together_keep_the_flag_and_every_line(app, monkeypatch):
    app.audio = FakeAudio(complaints=["the pinned device (mic) “Headset” wasn't found — listening to “Microphone”"])
    _hotkey_is_busy(app, monkeypatch)
    _a_setting_is_wrong(app, monkeypatch)
    s = snapshot(app)
    assert s["audio_problem"] is True
    assert [p.split(" — ")[0] for p in s["problems"]] == [
        "the pinned device (mic) “Headset” wasn't found", "“ctrl+alt+a”",
        assist_settings.problems(app.cfg)[0].split(" — ")[0],
    ]


def test_the_real_capture_that_is_not_opened_yet_counts_as_a_sound_problem(tmp_path, monkeypatch):
    """Without a fake audio source: the real capture is not alive before startup, and the list
    says so.
    """
    cfg = Config()
    cfg.root = tmp_path
    monkeypatch.setattr("argus.desktop.hotkeys", lambda: None)
    s = snapshot(Argus(cfg, with_screen=False, with_brain=False))
    assert any(line.startswith("audio:") for line in s["problems"])
    assert s["audio_problem"] is True


def test_a_pinned_microphone_that_went_missing_on_the_real_capture_raises_the_flag(tmp_path, monkeypatch):
    """The real capture's `complaints()`, not a fake one: the streams are alive, but the chosen
    microphone is missing.
    """
    cfg = Config()
    cfg.root = tmp_path
    monkeypatch.setattr("argus.desktop.hotkeys", lambda: None)
    app = Argus(cfg, with_screen=False, with_brain=False)

    def channel(missed=""):
        device = SimpleNamespace(name="Microphone", missed=missed, index=1)
        return SimpleNamespace(alive=True, error=None, silent_s=0.0, device=device)

    app.audio.mic, app.audio.loop = channel(missed="Headset"), channel()
    s = snapshot(app)
    assert len(s["problems"]) == 1 and "Headset" in s["problems"][0], s["problems"]
    assert s["audio_problem"] is True

    app.audio.mic = channel()                                   # the microphone was found, so no complaints
    s = snapshot(app)
    assert s["problems"] == [] and s["audio_problem"] is False


def test_the_page_reads_the_very_key_the_snapshot_sends(app):
    """The joint between two sides: rename the key on one side, and the button will again show up
    next to whatever.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "audio_problem" in html
    assert "audio_problem" in snapshot(app)


# --------------------------------------------------------------------------
# A line about invalid settings
# --------------------------------------------------------------------------


def test_a_wrong_setting_shows_up_among_the_problems(app):
    app.cfg.channel[assist_settings.KEY] = BAD_SETTING
    s = snapshot(app)
    assert s["problems"] == assist_settings.problems(app.cfg) and len(s["problems"]) == 1


def test_good_settings_add_no_line(app):
    app.cfg.channel[assist_settings.KEY] = {"profile": {"name": "Anton"}}
    assert snapshot(app)["problems"] == []


def test_the_line_about_settings_goes_away_when_the_setting_is_fixed(app):
    app.cfg.channel[assist_settings.KEY] = BAD_SETTING
    assert snapshot(app)["problems"]
    app.cfg.channel[assist_settings.KEY] = {"profile": {"years": 6}}
    assert snapshot(app)["problems"] == []
