"""Tests for the control panel: the on/off switch and the per-source checkboxes.

The panel touches the running pipeline on the fly, and the error here is silent: the
checkbox is cleared but the channel is still recognized, or the other way round, it is
switched on but there is no data. Neither produces an error, so it is tested separately.
"""

from __future__ import annotations

import numpy as np

from argus.app import Argus
from argus.core.config import Config
from argus.core.events import AudioChunk, Source


def make(with_screen: bool = True) -> Argus:
    return Argus(Config(), with_screen=with_screen, with_brain=False)


def chunk(source: Source, n: int = 512) -> AudioChunk:
    return AudioChunk(t=0, source=source, samples=np.zeros(n, dtype=np.int16))


# --------------------------------------------------------------------------
# Master switch
# --------------------------------------------------------------------------


def test_all_sources_are_enabled_at_start():
    """The sources are on, and recognition itself waits for the button."""
    app = make()
    c = app.controls()
    assert c["sources"] == {"mic": True, "loopback": True, "screen": True}
    assert c["running"] is False


def test_power_off_stops_all_recognition():
    """The master switch cuts off both speech and screen at once."""
    app = make()
    app.set_running(False)
    assert not app.controls()["running"]
    assert app.speech.paused
    assert app.vision.paused


def test_power_on_resumes():
    app = make()
    app.set_running(False)
    app.set_running(True)
    assert not app.speech.paused
    assert not app.vision.paused


def test_capture_keeps_running_while_paused():
    """We do not touch the hardware itself: a repeated start-up costs seconds, and the headset
    may not come back after reopening. We cut the stream higher up."""
    app = make()
    app.set_running(False)
    # The capture stays in place as an object; only the processing stops.
    assert app.audio is not None
    assert app.screen is not None


# --------------------------------------------------------------------------
# Per-source checkboxes
# --------------------------------------------------------------------------


def test_microphone_can_be_switched_off_alone():
    """A common case: listen to the interlocutor only, do not recognize yourself."""
    app = make()
    app.set_source("mic", False)
    assert app.controls()["sources"]["mic"] is False
    assert Source.ME not in app.speech.enabled
    assert Source.THEM in app.speech.enabled


def test_system_audio_can_be_switched_off_alone():
    app = make()
    app.set_source("loopback", False)
    assert Source.THEM not in app.speech.enabled
    assert Source.ME in app.speech.enabled


def test_screen_can_be_switched_off():
    """Screen reading is the heaviest part, it is switched off first."""
    app = make()
    app.set_source("screen", False)
    assert not app.vision.enabled


def test_switching_a_source_off_forgets_unfinished_speech():
    """Otherwise after switching on, the detector would finish a phrase from the past.

    It would land in the timeline as a fresh one, and the model would answer a question
    asked half an hour ago.
    """
    app = make()
    seg = app.speech.segmenters[Source.ME]
    seg.feed(chunk(Source.ME))
    seg._in_speech = True
    seg._speech = [np.zeros(512, dtype=np.int16)]

    app.set_source("mic", False)
    assert not seg._in_speech
    assert seg._speech == []
    assert seg.flush() is None


def test_unknown_source_is_rejected_loudly():
    app = make()
    try:
        app.set_source("telepathy", True)
    except KeyError:
        return
    raise AssertionError("an unknown source must be rejected")


def test_screen_toggle_is_safe_without_screen_capture():
    """The screen could have been disabled at launch — the toggle must not crash."""
    app = make(with_screen=False)
    app.set_source("screen", True)
    assert app.vision is None


def test_controls_reflect_every_change():
    app = make()
    app.set_source("mic", False)
    app.set_source("screen", False)
    app.set_running(False)
    c = app.controls()
    assert c["running"] is False
    assert c["sources"] == {"mic": False, "loopback": True, "screen": False}


# --------------------------------------------------------------------------
# Window choice
# --------------------------------------------------------------------------


def test_no_window_is_pinned_at_start():
    """By default we watch the active window."""
    assert make().controls()["target_window"] == ""


def test_window_can_be_pinned():
    """A pinned window is readable wherever the person looks.

    That is the whole point: you can keep the dashboard in front of your eyes while Argus
    watches a stream or a call in a neighboring window.
    """
    app = make()
    app.set_target_window("Twitch")
    assert app.controls()["target_window"] == "Twitch"
    assert app.cfg.screen.target_window == "Twitch"


def test_pinning_resets_the_frame_gate():
    """Otherwise the first frame of the new window would be compared to the fingerprint of the
    previous one.

    It might fail the filter, and the timeline would get the difference between two
    completely different windows, as if something had appeared on one screen.
    """
    app = make()
    import numpy as np

    app.vision.gate.check(np.zeros((100, 100, 3), dtype=np.uint8))
    assert app.vision.gate.seen == 1

    app.set_target_window("Twitch")
    assert app.vision.gate.seen == 0


def test_unpinning_returns_to_the_active_window():
    app = make()
    app.set_target_window("Twitch")
    app.set_target_window("")
    assert app.controls()["target_window"] == ""


def test_unpinning_means_the_whole_screen():
    """Unpinned — the whole screen is read. There is no third state."""
    app = make()
    app.set_target_window("Twitch")
    app.set_target_window("")
    assert app.cfg.screen.target_window == ""


def test_window_list_excludes_our_own_dashboard():
    """Our own dashboard must not be offered for selection — it would be a loop."""
    app = make()
    titles = [w["title"] for w in app.windows()]
    assert not any("argus" in t.lower() for t in titles)


def test_window_list_has_sizes():
    """The size helps tell the needed window from a service namesake."""
    app = make()
    for w in app.windows():
        if not w["title"]:
            continue  # the "whole screen" card, its size is taken from the frame
        assert w["width"] > 0
        assert w["height"] > 0


def test_window_list_starts_with_the_whole_screen():
    """"Whole screen" is a choice like a window, and stands first in the list."""
    app = make()
    first = app.windows()[0]
    assert first["title"] == ""
    assert first["label"] == "Whole screen"


# --------------------------------------------------------------------------
# The control panel in the dashboard snapshot
# --------------------------------------------------------------------------


def test_snapshot_carries_the_control_state():
    """The dashboard draws checkboxes from this field: without it they would lie."""
    from argus.ui.server import snapshot

    app = make()
    app.set_source("mic", False)
    app.set_running(True)
    s = snapshot(app)
    assert s["controls"]["sources"]["mic"] is False
    assert s["controls"]["running"] is True


def test_the_prompt_is_not_pushed_into_every_snapshot_but_can_be_fetched(tmp_path):
    """"The model is talking nonsense" and "nonsense went into the prompt" are different
    breakages, and the prompt must be visible. But up to twelve thousand characters per
    answer, twice a second, into every window is too expensive for a field the page does
    not read: a separate route serves it, on request."""
    from fastapi.testclient import TestClient

    from argus.brain.base import Answer
    from argus.ui.server import _answer_json, build

    cfg = Config()
    cfg.root = tmp_path                    # the chats folder is in a temporary one, not in the working copy
    app = Argus(cfg, with_screen=False, with_brain=False)
    answer = Answer(text="answer", provider="fake")
    answer.meta["prompt"] = "# What happened\n[00:01] Them: question"
    assert "prompt" not in _answer_json(answer)

    app.state.answers.append(answer)
    client = TestClient(build(app))
    served = client.get("/answer/0/prompt")
    assert served.status_code == 200
    assert "Them: question" in served.text
    assert client.get("/answer/1/prompt").status_code == 404
    assert client.get("/answer/-1/prompt").status_code == 404


# --------------------------------------------------------------------------
# Launch without recognition
# --------------------------------------------------------------------------


def test_recognition_is_off_at_startup():
    """The dashboard comes up, but nothing goes into the timeline until the button is pressed.

    This way the person has time to choose the window and the sources before anything random
    flows into the timeline, and extra junk cannot be removed from it afterwards.
    """
    assert make().controls()["running"] is False


def test_autostart_can_be_requested():
    """For the scenario "started it and walked away" the flag remains."""
    from argus.app import Argus

    app = Argus(Config(), with_screen=True, with_brain=False, autostart=True)
    assert app.controls()["running"] is True


def test_capture_is_ready_before_recognition_starts():
    """Capture is up and waiting: switching on by the button must be instant, not require
    reopening the devices."""
    app = make()
    assert app.audio is not None
    assert app.screen is not None
    assert app.speech is not None


def test_screen_toggle_controls_both_picture_and_reading():
    """"Screen" is one notion for the person: the model sees the screen or it does not.

    Inside there are two different things behind it, a picture in the request and text
    recognition into the timeline, but there is no reason to separate them in the interface.
    Before, there were two switches, and it was unclear how they differed.
    """
    app = make()
    assert app.cfg.brain.send_screenshot is True
    assert app.vision.enabled is True

    app.set_source("screen", False)
    assert app.cfg.brain.send_screenshot is False
    assert app.vision.enabled is False

    app.set_source("screen", True)
    assert app.cfg.brain.send_screenshot is True
    assert app.vision.enabled is True
