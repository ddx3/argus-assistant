"""Bringing the audio to the single pipeline format: 16 kHz, mono, int16.

Devices deliver whatever they like. Measurements on this machine:

    built-in microphone  44100 Hz, 2 channels
    HyperX microphone    48000 Hz, 1 channel
    headphone loopback   48000 Hz, 2 channels

Further down the pipeline all of this must look the same, otherwise the streams can be
neither aligned in time nor fed to Whisper. So all the variety ends here, right at the
input.

Why `soxr` and not "take every Nth sample": 48000 divides by 16000 evenly, and decimation
would work for the headphones. But 44100/16000 = 2.75625 is not an integer, and decimation
gives aliasing: high frequencies "fold" into the audible range and turn into a screech.
Whisper starts inventing words on that.
"""

from __future__ import annotations

import numpy as np
import soxr

from argus.core.events import SAMPLE_RATE

# int16 occupies [-32768, 32767]; for conversion from float we take 32767, so that +1.0
# does not overflow into -32768.
_INT16_PEAK = 32767.0


def to_mono(samples: np.ndarray, channels: int, average: bool = True) -> np.ndarray:
    """Fold multichannel audio into one channel without changing the scale of values.

    `samples` is a flat array with interleaved channels (L, R, L, R, ...), which is how
    PortAudio hands it out. Call **after** `to_float32`: averaging must be done in normalized
    form, otherwise it is easy to lose track of the scale.

    Mono devices (a headset microphone) arrive here with channels=1 and pass straight
    through; that is not a rare case but half of real microphones.
    """
    if channels <= 1:
        return samples

    frames = len(samples) // channels
    matrix = samples[: frames * channels].reshape(frames, channels)

    if not average:
        # Fallback path: if the channels are in antiphase, averaging gives silence.
        return np.ascontiguousarray(matrix[:, 0])

    return matrix.mean(axis=1, dtype=np.float32)


def to_float32(samples: np.ndarray) -> np.ndarray:
    """Convert int16 to float32 in the range [-1, 1], which is what soxr works in.

    Careful: float32 input is returned as is, because it is impossible to tell from one array
    whether it is "already normalized or not yet". So the order in the pipeline is strict:
    first normalization, then everything else.
    """
    if samples.dtype == np.float32:
        return samples
    if samples.dtype == np.int16:
        return samples.astype(np.float32) / _INT16_PEAK
    return samples.astype(np.float32)


def to_int16(samples: np.ndarray) -> np.ndarray:
    """Return to int16 with clipping at the edges, without wrap-around on overflow."""
    if samples.dtype == np.int16:
        return samples
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * _INT16_PEAK).astype(np.int16)


class Resampler:
    """Converts the stream of one device into the pipeline format.

    State is kept between calls: `soxr.ResampleStream` remembers the tail of the previous
    chunk. Without that there would be a click at every chunk boundary, and on a long
    recording a scatter of clicks that Whisper hears as fragments of words.
    """

    __slots__ = ("_stream", "average", "channels", "in_rate", "passthrough")

    def __init__(
        self,
        in_rate: int,
        channels: int,
        average: bool = True,
        out_rate: int = SAMPLE_RATE,
    ) -> None:
        self.in_rate = in_rate
        self.channels = channels
        self.average = average
        # The rate is already what is needed: no resampler required, and no need to pay for one.
        self.passthrough = in_rate == out_rate
        self._stream = (
            None
            if self.passthrough
            else soxr.ResampleStream(in_rate, out_rate, 1, dtype="float32", quality="HQ")
        )

    def process(self, raw: np.ndarray) -> np.ndarray:
        """A chunk from the device -> 16 kHz, mono, int16.

        The order of the steps matters and must not be changed: normalization, then downmix to
        mono, then resampling. If the channels are downmixed first, the result will be float32
        with values on the int16 scale, `to_float32` will not touch it, and the output will hit
        the maximum everywhere: solid overload.
        """
        normalized = to_float32(raw)
        mono = to_mono(normalized, self.channels, self.average)
        if self.passthrough:
            return to_int16(mono)
        return to_int16(self._stream.resample_chunk(mono))

    def flush(self) -> np.ndarray:
        """Flush the remaining tail when the stream stops."""
        if self.passthrough or self._stream is None:
            return np.zeros(0, dtype=np.int16)
        return to_int16(self._stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
