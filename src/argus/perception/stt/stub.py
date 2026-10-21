"""A stub in place of recognition, for testing the pipeline without a bill for minutes.

Real recognition (`realtime-openai`) costs money on every phrase and needs a key and a
network. Often what has to be checked is something else: whether an utterance is written
to the timeline, whether the "Ask" button next to it works, whether the
time-based join with the screen frames breaks. For that, any text in place of the real
transcription will do.

The VAD and the cutting into utterances work as usual: the stub stands only in place of
the recognition itself, which comes after them.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from argus.core.events import SpeechEvent, Utterance

if TYPE_CHECKING:
    from argus.core.config import StubSTTConfig


class StubSTT:
    """Returns a preset text for every utterance caught by the VAD."""

    name = "stub"

    # False, as with `local`: the stub holds nothing and does not compute one job at a time,
    # so one shared instance for both channels is enough.
    per_channel = False

    def __init__(self, cfg: StubSTTConfig, label: str = "") -> None:
        self.cfg = cfg
        self.label = label or self.name
        self.calls = 0
        self.failures = 0

    def warmup(self) -> None:
        """Nothing to warm up: the stub holds neither network nor weights."""
        return

    def transcribe(self, utterance: Utterance) -> SpeechEvent | None:
        if self.cfg.delay_ms > 0:
            time.sleep(self.cfg.delay_ms / 1000)

        self.calls += 1
        # `{n}` and `{source}` are optional: a string without them is simply repeated.
        text = self.cfg.text.format(n=self.calls, source=utterance.source.value)
        if not text.strip():
            return None

        return SpeechEvent(
            t=utterance.t,
            t_end=utterance.t_end,
            source=utterance.source,
            text=text,
            engine=self.label,
        )

    def close(self) -> None:
        return

    def stats(self) -> dict[str, int]:
        return {"recognized": self.calls}
