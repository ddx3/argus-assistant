"""Frames that went to the model: what is remembered and how it gets into the feed.

The point of the whole thing is one: to show **the very** frame that the model saw.
Taking a second capture for display is cheap, but in those same milliseconds the screen
can change, and the person would discuss one picture with the model while looking at
another. Such a mismatch is then hunted for hours and not found.
"""

from __future__ import annotations

import pytest

from argus.app import Argus, Shot
from argus.core.config import Config
from argus.ui.server import snapshot


def app() -> Argus:
    return Argus(Config(), with_screen=False, with_brain=False)


def test_shots_reach_the_snapshot_without_the_bytes():
    """Marks fly over the websocket, not pictures.

    The snapshot goes out twice a second; putting a hundred kilobytes per frame into it would
    clog the channel with the same bytes. The picture is pulled by link and cached by the
    browser.
    """
    a = app()
    a.state.shots.append(Shot(t=5_000_000_000, jpeg=b"x" * 4096))

    shots = snapshot(a)["shots"]
    assert shots == [{"t": 5.0, "id": 5_000_000_000, "kb": 4, "kind": "screen"}]
    assert "jpeg" not in shots[0]


def test_memory_is_short():
    """A dozen frames is about a megabyte. There is no reason to hold more."""
    a = app()
    for i in range(30):
        a.state.shots.append(Shot(t=i, jpeg=b"x"))
    assert len(a.state.shots) == 10
    assert a.state.shots[0].t == 20


def test_every_frame_given_to_the_model_is_remembered(monkeypatch):
    """We remember in one place: where the frame is encoded.

    Not where the button was pressed: every frame that goes to the model passes through this
    function, whether from the button, from the interlocutor's question, or from the timer.
    Otherwise the feed would show only what was shown by hand.
    """
    import numpy as np

    a = app()
    monkeypatch.setattr(a, "preview_image", lambda: np.zeros((40, 60, 3), dtype=np.uint8))

    jpeg = a.screenshot_jpeg()
    assert jpeg
    assert len(a.state.shots) == 1
    # What gets into the feed is exactly what was given to the model, byte for byte.
    assert a.state.shots[0].jpeg == jpeg


def test_nothing_is_remembered_when_there_is_no_frame(monkeypatch):
    a = app()
    monkeypatch.setattr(a, "preview_image", lambda: None)
    assert a.screenshot_jpeg() is None
    assert not a.state.shots


async def test_asking_about_the_screen_forces_the_picture(monkeypatch):
    """The "ask about the screen" button without a picture does not do what was asked.

    Constant sending is turned off because of limits: a frame costs about fifteen hundred
    tokens against a hundred for text. But a press is itself a request to look.
    """
    a = app()
    a.cfg.brain.send_screenshot = False

    seen: list[bool] = []
    forced: list[bool] = []

    async def fake_ask(question=None, deep=False, *, force_image=False):
        seen.append(a.cfg.brain.send_screenshot)
        forced.append(force_image)
        return "ok"

    monkeypatch.setattr(a, "ask", fake_ask)
    await a.ask_about_screen()

    # The shared switch is not touched even for the duration of the question: the picture is
    # requested by an explicit flag, while a temporary config edit would overwrite the
    # "Screen off" pressed while the question is running (see
    # `test_a_switch_pressed_during_the_question_is_kept`).
    assert seen == [False]
    # Not by a coinciding `send_screenshot`, but by an explicit flag: the ordinary "ask" hotkey
    # may also find `send_screenshot=True` (a permanent setting), and that alone is not enough
    # to tell it from "ask about the screen"; see `Assistant.ask_now`.
    assert forced == [True]
    # The button asks once and does not change the mode.
    assert a.cfg.brain.send_screenshot is False


@pytest.mark.parametrize("before, pressed", [(True, False), (False, True)])
async def test_a_switch_pressed_during_the_question_is_kept(monkeypatch, before, pressed):
    """Pressing "Screen off" in the middle of a question about the screen must not
    be rolled back.

    Before, `finally` restored the value read before the question: the page showed "off", and
    the following questions with `screenshot_mode = always` kept going out with a screenshot.
    In the opposite direction, "on" was lost.
    """
    import asyncio

    a = app()
    a.cfg.brain.send_screenshot = before
    gate = asyncio.Event()

    async def slow_ask(question=None, deep=False, *, force_image=False):
        await gate.wait()
        return "ok"

    monkeypatch.setattr(a, "ask", slow_ask)
    pending = asyncio.create_task(a.ask_about_screen())
    await asyncio.sleep(0)                      # the question went out and is waiting for an answer
    a.set_source("screen", pressed)             # the person pressed the switch
    gate.set()
    await pending

    assert a.cfg.brain.send_screenshot is pressed
    assert a.controls()["send_screenshot"] is pressed


async def test_the_setting_is_not_touched_even_after_a_failure(monkeypatch):
    """A failed call must not silently turn sending on for good."""
    a = app()
    a.cfg.brain.send_screenshot = False

    async def boom(question=None, deep=False, *, force_image=False):
        raise RuntimeError("the network went down")

    monkeypatch.setattr(a, "ask", boom)
    with pytest.raises(RuntimeError):
        await a.ask_about_screen()
    assert a.cfg.brain.send_screenshot is False
