"""Speech recognition interface.

The point of the interface is not abstraction for its own sake. Local Whisper has zero
cost and gives privacy, but it runs into the GPU limit; the cloud Deepgram and AssemblyAI
give lower latency, but cost on the order of 0.15-0.40 dollars per hour of continuous
recognition. What to choose will become clear only from measurements, so switching the
provider must be a line in the config, not a rewrite of the pipeline.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from argus.core.events import SpeechEvent, Utterance


@runtime_checkable
class STTProvider(Protocol):
    """Turns an utterance into text.

    Returns `None` if there is nothing to recognize: the speech detector sometimes fires on
    key clatter or a slammed door, and it is better to return nothing than to let an invented
    phrase into the timeline.
    """

    name: str

    # Whether the "me" and "other party" channels need their own instance. False for local
    # models: they hold video memory and compute one job at a time, so a second instance only
    # doubles the usage. True for session-based ones: a session accumulates the context of the
    # conversation and serves utterances one at a time, so a session shared by two channels
    # both mixes the context and makes the channels wait for each other.
    per_channel: bool = False

    def transcribe(self, utterance: Utterance) -> SpeechEvent | None: ...

    def warmup(self) -> None:
        """Warm up the model so that the first utterance does not wait for the weights to load."""
        ...

    def close(self) -> None: ...
