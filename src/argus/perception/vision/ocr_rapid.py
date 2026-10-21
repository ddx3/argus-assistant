"""Screen reading through RapidOCR.

The combination was chosen by measurement, not by description: we drew a picture with
known text (Russian, English and code) and compared the variants.

    detector multi v4  + ESLAV v5   100% of words, but 31 blocks: each word separately
    detector ch v5     + ESLAV V5   100% of words and 5 blocks: whole lines

The second one won, and what decided it was not the number of recognized words but the
structure. Further down the pipeline the text goes into the prompt, and the line
"Tell us about your experience with databases" carries meaning, while a scatter
of "Tell", "us", "about", "your" does not.

It runs on the CPU: we have 12 cores, while the video memory is given entirely to
Whisper.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import numpy as np

from argus.core.events import ScreenBlock

if TYPE_CHECKING:
    from argus.core.config import OCRConfig

log = logging.getLogger(__name__)

# Three letters in a row are a sign of a real word. Recognition debris («Ма×», «D Смс»,
# «© НоE X») almost never produces them.
_RUN_OF_LETTERS = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


class RapidOCRProvider:
    """Screen OCR."""

    name = "rapidocr"

    def __init__(self, cfg: OCRConfig) -> None:
        self.cfg = cfg
        self._engine = None
        self.calls = 0
        self.total_s = 0.0
        self.blocks_found = 0

    def _load(self):
        if self._engine is not None:
            return self._engine
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        t0 = time.perf_counter()
        self._engine = RapidOCR(params={
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.lang_type": LangDet[self.cfg.det_lang],
            "Det.ocr_version": OCRVersion[self.cfg.det_version],
            "Det.model_type": ModelType[self.cfg.det_model],
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.lang_type": LangRec[self.cfg.rec_lang],
            "Rec.ocr_version": OCRVersion[self.cfg.rec_version],
            "Rec.model_type": ModelType[self.cfg.rec_model],
            "Rec.rec_batch_num": self.cfg.rec_batch,
        })
        log.info("OCR loaded in %.1f s", time.perf_counter() - t0)
        return self._engine

    def warmup(self) -> None:
        engine = self._load()
        engine(np.full((64, 256, 3), 255, dtype=np.uint8))

    def read(self, image: np.ndarray) -> list[ScreenBlock]:
        engine = self._load()

        # We shrink a large screen: at 2560 wide recognition takes noticeably longer, while small
        # text runs into the detector anyway, not into the size.
        scale = 1.0
        if self.cfg.max_width and image.shape[1] > self.cfg.max_width:
            import cv2

            scale = self.cfg.max_width / image.shape[1]
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        t0 = time.perf_counter()
        result = engine(image)
        self.total_s += time.perf_counter() - t0
        self.calls += 1

        if result is None or not result.txts:
            return []

        blocks: list[ScreenBlock] = []
        scores = result.scores if result.scores is not None else [None] * len(result.txts)
        boxes = result.boxes if result.boxes is not None else [None] * len(result.txts)

        for text, score, box in zip(result.txts, scores, boxes, strict=False):
            text = (text or "").strip()
            if not text or _is_noise(text, self.cfg.min_chars):
                continue
            if score is not None and score < self.cfg.min_confidence:
                continue
            blocks.append(
                ScreenBlock(
                    text=text,
                    box=_to_xywh(box, scale),
                    confidence=float(score) if score is not None else None,
                )
            )
        self.blocks_found += len(blocks)
        return blocks

    @property
    def avg_ms(self) -> float:
        return self.total_s / self.calls * 1000 if self.calls else 0.0

    def close(self) -> None:
        self._engine = None


def _is_noise(text: str, min_chars: int) -> bool:
    """Filter out what is clearly not text but interface debris.

    The rules had to be tightened when the model began to receive the picture itself. Before,
    the recognized text was the only source, and garbage in it was the lesser evil. Now the
    model parses the picture itself, and debris in the timeline only gets in its way: on a
    live screen the browser tab titles produced «Ма×», «NIX X», «© НоE X», and there turned
    out to be more such lines than content. On such a prompt the model answered "the text is
    unreadable".

    The rule is simple: a line must contain three letters in a row. That is enough for any
    real word and not enough for debris. Separately, we let numbers and times through:
    «22%», «12:45», «3/5» are short but meaningful.
    """
    text = text.strip()
    if not text:
        return True

    letters = sum(c.isalpha() for c in text)
    digits = sum(c.isdigit() for c in text)

    # «=====», «...», «|||» are separators and frames. We check only lines with no letters and
    # no digits: «99» and «111» are also one repeated character, but they are a score, not a
    # frame.
    if letters == 0 and digits == 0 and len(set(text)) <= 1:
        return True

    # We check numbers, times and counters before the length: «22%», «3/5», «12:45» are shorter
    # than the threshold but meaningful: a score, a timer, progress.
    if letters == 0 and digits >= 1 and all(c.isdigit() or c in ":/%.,-+ " for c in text):
        return False

    if len(text) < min_chars:
        return True
    if letters == 0:
        return True
    # Any word has three letters in a row, and debris like «Ма×» and «D Смс» does not.
    if not _RUN_OF_LETTERS.search(text):
        return True
    # Letters and digits must make up at least half: «*Ф» and «[0.61]» are dropped.
    return letters + digits < len(text) * 0.5


def _to_xywh(box, scale: float) -> tuple[int, int, int, int]:
    """Four corners from the detector -> a rectangle in the coordinates of the original frame."""
    if box is None:
        return (0, 0, 0, 0)
    pts = np.asarray(box, dtype=np.float64).reshape(-1, 2) / max(scale, 1e-6)
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    return (int(x0), int(y0), int(x1 - x0), int(y1 - y0))
