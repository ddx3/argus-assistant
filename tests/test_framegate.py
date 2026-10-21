"""Tests for frame filtering.

The filter is the only thing between the camera and recognition, which costs more than a
second per frame. A mistake here does not crash the program: it quietly eats CPU or, the
other way round, misses a screen change, so it is tested in detail.
"""

from __future__ import annotations

import numpy as np
import pytest

from argus.perception.framegate import (
    BITS,
    FrameGate,
    GateSettings,
    dhash,
    distance,
)


def blank(value: int = 40, size: tuple[int, int] = (200, 320)) -> np.ndarray:
    return np.full((*size, 3), value, dtype=np.uint8)


def with_text_block(base: np.ndarray, x: int, y: int, w: int = 80, h: int = 20) -> np.ndarray:
    img = base.copy()
    img[y : y + h, x : x + w] = 230
    return img


def with_cursor(base: np.ndarray) -> np.ndarray:
    """One blinking cursor — what must be filtered out."""
    img = base.copy()
    img[100:112, 50:52] = 255
    return img


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


def test_hash_is_stable_for_same_image():
    img = with_text_block(blank(), 30, 40)
    assert dhash(img) == dhash(img.copy())


def test_hash_changes_when_content_moves():
    base = blank()
    a = with_text_block(base, 30, 40)
    b = with_text_block(base, 150, 120)
    assert distance(dhash(a), dhash(b)) >= 3


def test_hash_survives_brightness_shift():
    """dhash compares ratios of brightness, not the brightness itself.

    So a change of backlight or a smooth background gradient must not look like a screen
    change; otherwise recognition would run on every breath of an animation.
    """
    a = with_text_block(blank(40), 30, 40)
    b = with_text_block(blank(80), 30, 40)
    b = np.clip(b.astype(np.int16) + 25, 0, 255).astype(np.uint8)
    assert distance(dhash(a), dhash(b)) <= 2


def test_distance_is_bounded_by_bit_count():
    a, b = dhash(blank(0)), dhash(blank(255))
    assert 0 <= distance(a, b) <= BITS


# --------------------------------------------------------------------------
# Pass logic
# --------------------------------------------------------------------------


def test_first_frame_always_passes():
    gate = FrameGate()
    passed, _ = gate.check(blank(), now=0.0)
    assert passed
    assert gate.passed == 1


def test_identical_frame_is_skipped():
    gate = FrameGate()
    img = with_text_block(blank(), 30, 40)
    gate.check(img, now=0.0)
    passed, _ = gate.check(img, now=1.0)
    assert not passed
    assert gate.skipped_same == 1


def test_blinking_cursor_does_not_trigger_ocr():
    """The main case the filter exists for.

    The cursor blinks every half second. Without the filter, every blink would start
    recognition for a second or more, and the CPU would be busy continuously and to no
    purpose.
    """
    gate = FrameGate()
    base = with_text_block(blank(), 30, 40)
    gate.check(base, now=0.0)

    for i in range(20):
        img = with_cursor(base) if i % 2 else base
        passed, _ = gate.check(img, now=5.0 + i * 0.5)
        assert not passed, f"the cursor at step {i} got through the filter"


def test_window_switch_passes():
    gate = FrameGate()
    gate.check(with_text_block(blank(), 10, 10), now=0.0)
    passed, _ = gate.check(with_text_block(blank(200), 150, 120, w=200, h=100), now=5.0)
    assert passed


def test_rate_limit_holds_back_fast_changes():
    """Full-screen video changes every frame — it cannot be recognized."""
    gate = FrameGate(GateSettings(min_interval_s=2.0))
    gate.check(with_text_block(blank(), 10, 10), now=0.0)

    passed, _ = gate.check(with_text_block(blank(), 100, 100), now=0.5)
    assert not passed
    assert gate.skipped_rate == 1

    passed, _ = gate.check(with_text_block(blank(), 100, 100), now=2.5)
    assert passed


def test_rate_limited_frame_does_not_become_the_new_reference():
    """A held-back frame must not be remembered as the last one seen.

    Otherwise a change would be "smeared" into a series of small steps, each below the
    threshold, and a screen that changed gradually would never pass the filter at all.
    """
    gate = FrameGate(GateSettings(min_interval_s=2.0, min_distance=3))
    start = with_text_block(blank(), 10, 10)
    gate.check(start, now=0.0)

    moved = with_text_block(blank(), 150, 120)
    gate.check(moved, now=0.5)          # held back by rate
    passed, _ = gate.check(moved, now=3.0)
    assert passed, "the frame got stuck: the held-back fingerprint became the reference"


def test_heartbeat_refreshes_static_screen():
    """On a static screen the feed must not freeze forever."""
    gate = FrameGate(GateSettings(heartbeat_s=10.0))
    img = with_text_block(blank(), 30, 40)
    gate.check(img, now=0.0)

    assert not gate.check(img, now=5.0)[0]
    assert gate.check(img, now=11.0)[0]


def test_pass_rate_reflects_work_saved():
    gate = FrameGate()
    img = with_text_block(blank(), 30, 40)
    for i in range(10):
        gate.check(img, now=float(i))
    assert gate.seen == 10
    assert gate.passed == 1
    assert gate.pass_rate == pytest.approx(0.1)


def test_empty_gate_has_no_pass_rate():
    assert FrameGate().pass_rate == 0.0


# --------------------------------------------------------------------------
# Local changes on a big screen: tiles next to dhash
# --------------------------------------------------------------------------


def big_screen() -> np.ndarray:
    """2560x1440, dark theme, three ready code lines, the left panel slightly lighter."""
    img = np.full((1440, 2560, 3), 30, dtype=np.uint8)
    img[:, :300] = 45
    for y in (200, 500, 900):
        img = with_code_line(img, 400, y)
    return img


def with_code_line(base: np.ndarray, x: int, y: int, width: int = 480) -> np.ndarray:
    """A code line: strokes of 3 px every 12, height 16 px — roughly 60 characters in a small
    font.
    """
    img = base.copy()
    for k in range(x, x + width, 12):
        img[y : y + 16, k : k + 3] = 220
    return img


@pytest.mark.parametrize("y", [700, 77, 1432])
def test_one_new_text_line_passes_though_dhash_does_not_see_it(y):
    """Audit measurement: on a 2560x1440 screen one line changes 0-1 bits of 64 in the dhash.

    The 3-bit threshold does not see it, and the line was read only after another change or
    after the heartbeat deadline. A line paints over six 32x18 tiles by several levels.
    """
    before = big_screen()
    after = with_code_line(before, 400, y)
    assert distance(dhash(before), dhash(after)) < 3, "precondition: dhash does not catch this line"

    gate = FrameGate()
    gate.check(before, now=0.0)
    passed, _ = gate.check(after, now=5.0)
    assert passed, "the new line of text did not pass the filter"


def test_the_same_screen_with_a_cursor_or_a_clock_digit_does_not_pass():
    before = big_screen()
    cursor = before.copy()
    cursor[300:318, 700:702] = 255                     # cursor 2x18
    digit = before.copy()
    digit[1410:1422, 2480:2488] = 240                  # clock digit in the tray 8x12

    gate = FrameGate()
    gate.check(before, now=0.0)
    for name, img in (("cursor", cursor), ("clock", digit)):
        assert not gate.check(img, now=5.0)[0], f"the {name} got through the filter"


def test_a_screen_wide_change_of_light_does_not_pass():
    """dhash does not see a change of backlight on purpose; tiles without a correction would
    see it in full.

    A background that "breathes" by a dozen levels would otherwise start a read every
    `min_interval_s`.
    """
    before = big_screen()
    brighter = np.clip(before.astype(np.int16) + 25, 0, 255).astype(np.uint8)
    fade = np.linspace(0, 30, before.shape[0], dtype=np.float32)[:, None, None]
    graded = np.clip(before.astype(np.float32) + fade, 0, 255).astype(np.uint8)

    gate = FrameGate()
    gate.check(before, now=0.0)
    assert not gate.check(brighter, now=5.0)[0]
    assert not gate.check(graded, now=6.0)[0]


def test_a_dialog_opening_on_a_big_screen_passes():
    before = big_screen()
    after = before.copy()
    after[400:900, 900:1700] = 200
    gate = FrameGate()
    gate.check(before, now=0.0)
    assert gate.check(after, now=5.0)[0]


# --------------------------------------------------------------------------
# Recheck deadline: a held-back frame and a heartbeat without new frames
# --------------------------------------------------------------------------


def test_nothing_to_recheck_before_the_first_read():
    assert FrameGate().wait_s(now=0.0) is None


def test_a_frame_held_back_by_the_rate_limit_asks_to_be_checked_when_the_interval_ends():
    """DXGI sends no frames on a frozen screen: a held-back frame without a reminder would be
    lost forever.
    """
    gate = FrameGate(GateSettings(min_interval_s=3.0, heartbeat_s=30.0))
    gate.check(with_text_block(blank(), 10, 10), now=0.0)
    moved = with_text_block(blank(), 150, 120)

    assert not gate.check(moved, now=1.0)[0]
    assert gate.wait_s(now=1.0) == pytest.approx(2.0)

    assert gate.check(moved, now=3.01)[0], "by the deadline the same frame must pass"


def test_a_static_screen_asks_for_the_heartbeat():
    gate = FrameGate(GateSettings(heartbeat_s=30.0))
    img = with_text_block(blank(), 30, 40)
    gate.check(img, now=0.0)

    assert gate.wait_s(now=0.0) == pytest.approx(30.0)
    assert not gate.check(img, now=12.0)[0]
    assert gate.wait_s(now=12.0) == pytest.approx(18.0)
    assert gate.check(img, now=30.0)[0]
    assert gate.wait_s(now=30.0) == pytest.approx(30.0)


def test_when_the_held_frame_changed_back_the_wait_returns_to_the_heartbeat():
    gate = FrameGate(GateSettings(min_interval_s=3.0, heartbeat_s=30.0))
    a = with_text_block(blank(), 10, 10)
    gate.check(a, now=0.0)
    gate.check(with_text_block(blank(), 150, 120), now=1.0)          # held back
    assert gate.wait_s(now=1.0) == pytest.approx(2.0)

    gate.check(a, now=2.0)                                           # the screen came back, nothing to hold
    assert gate.wait_s(now=2.0) == pytest.approx(28.0)
