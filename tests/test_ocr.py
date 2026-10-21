"""Screen-reading checks.

The junk filter and the coordinate remapping are checked without a model; this is pure
logic. Real recognition is a separate test: it pulls in weights and runs for a second or
more, so it is skipped when the models are absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from argus.core.config import OCRConfig
from argus.perception.vision.ocr_rapid import RapidOCRProvider, _is_noise, _to_xywh


def _ocr_weights_present() -> bool:
    """RapidOCR keeps its weights inside its own package, not in the project's models/."""
    import rapidocr

    models = Path(rapidocr.__file__).parent / "models"
    return models.exists() and any(models.glob("*PP-OCRv5*"))


needs_ocr = pytest.mark.skipif(not _ocr_weights_present(), reason="no OCR weights")


# --------------------------------------------------------------------------
# Junk filter
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    [
        "*F",       # a fragment of an icon
        "ю",        # a single letter (Cyrillic on purpose: the filter must not depend on the script)
        "=====",    # a separator
        "...",
        "|||",
        "  ",
        "a",
    ],
)
def test_interface_debris_is_filtered(junk: str):
    """A measurement on a real screen: 13 of 64 blocks were such debris.

    Each of them costs as many tokens as a meaningful line and only confuses the model.

    Purely numeric strings like "000" are deliberately not included here: the rule "numbers
    pass" is worth more. A timer in an exam and a score in a game are exactly what the screen
    is read for, while a cropped icon of all zeros is rare.
    """
    assert _is_noise(junk, min_chars=3)


@pytest.mark.parametrize(
    "good",
    [
        "Explorer",
        "Question 3 of 10",
        "def solve(x):",
        "File",
        "Расскажите про ваш опыт",  # Cyrillic on purpose: Russian text must survive the filter
        "12:45",
        "Comments 63",
    ],
)
def test_real_text_survives_the_filter(good: str):
    assert not _is_noise(good, min_chars=3)


# --------------------------------------------------------------------------
# Coordinates
# --------------------------------------------------------------------------


def test_box_is_converted_to_rectangle():
    """The detector returns four corners, while the rest of the pipeline needs a rectangle."""
    corners = [[10, 20], [110, 22], [108, 60], [12, 58]]
    assert _to_xywh(corners, scale=1.0) == (10, 20, 100, 40)


def test_box_is_mapped_back_to_original_resolution():
    """The frame was shrunk before recognition, so the coordinates must be mapped back.

    Otherwise the box around the found text would drift by a third of the screen, and
    everything that relies on a block's position would be wrong.
    """
    corners = [[50, 100], [150, 100], [150, 140], [50, 140]]
    x, y, w, h = _to_xywh(corners, scale=0.5)
    assert (x, y, w, h) == (100, 200, 200, 80)


def test_missing_box_does_not_crash():
    assert _to_xywh(None, scale=1.0) == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# The real model
# --------------------------------------------------------------------------


@needs_ocr
def test_reads_russian_and_english_from_an_image():
    """An end-to-end check on a drawn picture with known text."""
    from PIL import Image, ImageDraw, ImageFont

    lines = [
        "Расскажите про ваш опыт работы с базами данных",   # Russian on purpose: Russian is read too
        "What is the time complexity of quicksort?",
    ]
    font_path = Path(r"C:\Windows\Fonts\segoeui.ttf")
    if not font_path.exists():
        pytest.skip("no font to draw with")

    font = ImageFont.truetype(str(font_path), 34)
    img = Image.new("RGB", (1200, 200), (24, 24, 28))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((30, 30 + i * 80), line, font=font, fill=(228, 228, 232))

    ocr = RapidOCRProvider(OCRConfig())
    blocks = ocr.read(np.array(img)[:, :, ::-1])  # PIL returns RGB, the engine needs BGR
    text = " ".join(b.text for b in blocks).lower()

    assert "опыт работы" in text
    assert "quicksort" in text
    assert ocr.avg_ms > 0


@needs_ocr
def test_blank_screen_gives_nothing():
    ocr = RapidOCRProvider(OCRConfig())
    assert ocr.read(np.full((400, 800, 3), 30, dtype=np.uint8)) == []


@pytest.mark.parametrize("value", ["22%", "3/5", "12:45", "1/10", "99"])
def test_numbers_and_timers_survive(value: str):
    """A score, a timer and progress are short but meaningful.

    "Time left 12:45" in an exam or "3/5" in a game is exactly what the
    screen is read for, and they do not pass the general length threshold.
    """
    assert not _is_noise(value, min_chars=4)


@pytest.mark.parametrize("junk", ["Ma×", "Sms", "ю", "X", "*F", "..."])
def test_letter_debris_is_filtered(junk: str):
    """Debris without three letters in a row is not words."""
    assert _is_noise(junk, min_chars=4)
