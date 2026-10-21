"""The pipeline's common language: the event types that travel between modules.

There are two kinds of events, and they are deliberately built differently.

*Hot* ones (`AudioChunk`, `Frame`) are raw data from the hardware: tens and hundreds
per second, each carrying megabytes. They are `dataclass(slots=True)`: no validation, no
copying, minimal overhead. The payload is a reference to a numpy array with a single
owner, and we make no copies.

*Semantic* ones (`SpeechEvent`, `ScreenEvent`) are the result of recognition: rare and
small, but they are written to disk, sent to the dashboard and inserted into the prompt.
Here pydantic is justified: validation, serialization, clear errors.

Everything that goes onto the bus must carry `t`, the time from the common clock
(`core.clock`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
from pydantic import BaseModel, Field

# The audio format, the same for the whole pipeline.
SAMPLE_RATE = 16_000
SAMPLE_DTYPE = np.int16
CHANNELS = 1


class Source(str, Enum):
    """Who is the source of the event.

    The names are semantic, not "microphone/loopback": further along the pipeline what
    matters is not which device the audio came from but who is speaking. This is the very
    split that replaces diarization for us at no cost.
    """

    ME = "me"          # the microphone: I am speaking
    THEM = "them"      # system audio: others are speaking to me
    SCREEN = "screen"  # what is visible on the screen
    # The assistant itself. There is no audio from it, only timeline entries about what it
    # has already answered (`Kind.REPLY`), so that "more", "harder" and "repeat" have
    # something to refer to.
    ASSISTANT = "assistant"


# --------------------------------------------------------------------------
# Hot path
# --------------------------------------------------------------------------

@dataclass(slots=True)
class AudioChunk:
    """A piece of audio from one device, already converted to the common format.

    The format is fixed across the whole pipeline: 16 kHz, mono, int16. We convert right at
    the input, because devices record differently (here the headphones run at 48 kHz and
    the microphone at 44.1 kHz), and without a common format the streams cannot be aligned
    in time.
    """

    t: int                  # start time of the chunk, ns from the session start
    source: Source          # ME or THEM
    samples: np.ndarray     # int16, mono, 16 kHz

    @property
    def duration_ns(self) -> int:
        return len(self.samples) * 1_000_000_000 // SAMPLE_RATE

    @property
    def t_end(self) -> int:
        return self.t + self.duration_ns


@dataclass(slots=True)
class Utterance:
    """A piece of audio in which the detector found speech, from the start to the end of a
    phrase.

    An intermediate link between the raw stream and text. Whisper is run on exactly this
    kind of segmentation: running it on a continuous stream makes no sense, since it is
    built for meaningful segments and invents words on silence.
    """

    t: int
    t_end: int
    source: Source
    samples: np.ndarray     # int16, mono, 16 kHz

    @property
    def duration_s(self) -> float:
        return len(self.samples) / SAMPLE_RATE


@dataclass(slots=True)
class Frame:
    """A screen frame.

    `image` is the raw array from DXGI. We do not copy it: 2560x1440 is 14 MB, and at 10
    frames per second copying would cost 140 MB/s for nothing.
    """

    t: int
    image: np.ndarray       # BGR, uint8, (H, W, 3)
    seq: int = 0            # sequence number, so that gaps are visible

    # The active window at the moment of capture. Asking for it later, during processing, is
    # not allowed: seconds pass between capture and recognition, and the user manages to
    # switch in that time. Then a frame of one window gets the title of another, and the
    # timeline ends up saying that text from the editor was read "in the browser".
    window: str | None = None
    window_rect: tuple[int, int, int, int] | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return self.image.shape[0], self.image.shape[1]


# --------------------------------------------------------------------------
# Semantic path
# --------------------------------------------------------------------------

class SpeechEvent(BaseModel):
    """A recognized utterance."""

    t: int                              # start of the line
    t_end: int                          # end of the line
    source: Source                      # ME or THEM
    text: str
    error: str = ""                    # a recognition error, without text
    confidence: float | None = None
    language: str | None = None

    # Which engine did the recognition. Empty means the main one, configured in
    # `stt.provider`, whose text goes into the timeline and to the model.
    #
    # A non-empty value means a shadow run: the very same piece of audio was recognized by a
    # second engine **for comparison**. Such events reach the dashboard and nowhere else, not
    # the timeline and not the prompt. Otherwise turning comparison on would change what the
    # model sees, and two things would be measured at once instead of one.
    engine: str = ""

    # Not empty means the main engine produced nothing and a backup engine picked up the
    # line. The text is real and goes into the timeline like any other, but the substitution
    # should be visible: if it happens often, the main engine was chosen wrongly.
    rescued_by: str = ""

    # How long the recognition itself took. For comparing engines this is half the answer:
    # the text may be more accurate but arrive twice as late.
    latency_ms: float | None = None

    @property
    def duration_ms(self) -> float:
        return (self.t_end - self.t) / 1_000_000


class ScreenBlock(BaseModel):
    """One recognized block of text on the screen, with its place in the picture."""

    text: str
    box: tuple[int, int, int, int]      # x, y, w, h in pixels
    confidence: float | None = None


class ScreenEvent(BaseModel):
    """The state of the screen at moment t: what could be read from it."""

    t: int
    blocks: list[ScreenBlock] = Field(default_factory=list)
    frame_hash: str | None = None       # dhash of the frame, to catch repeats

    # The title of the window it was read from. This is important context for the model: the
    # same line means different things in a browser and in a terminal.
    window: str | None = None

    @property
    def text(self) -> str:
        """The whole screen text as one string, for insertion into the prompt."""
        return "\n".join(b.text for b in self.blocks)


class LevelEvent(BaseModel):
    """Channel loudness level. Needed only for the dashboard and the M1 acceptance."""

    t: int
    source: Source
    rms: float                          # 0.0 .. 1.0
    peak: float


