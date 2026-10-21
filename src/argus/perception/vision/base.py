"""Screen reading interface.

There is one implementation now, OCR. The second, a vision model (VLM), is deliberately
postponed: for screens with text (a question, code, a chat, correspondence) OCR plus a
text model beats a VLM on price, on latency, and on accuracy with small fonts. A VLM will
be needed when it becomes important to understand the picture as a whole: what is
happening in a game, what is drawn on a diagram. Then it will go here, next to OCR, and
switching will be a line in the config.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from argus.core.events import ScreenBlock


@runtime_checkable
class VisionProvider(Protocol):
    """Turns a frame into a set of text blocks with their place on the screen."""

    name: str

    def read(self, image: np.ndarray) -> list[ScreenBlock]: ...

    def warmup(self) -> None: ...

    def close(self) -> None: ...
