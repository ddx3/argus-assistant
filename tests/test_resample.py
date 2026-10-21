"""Checks for converting audio to the pipeline format.

The main one is `test_amplitude_is_preserved`. The first M1 run showed both channels at
-0.0 dB, that is, solid overload: the mono downmix returned float32 at the int16 scale,
normalization did not touch such an array, and everything hit the maximum. By level it
looked like "very loud", not like a breakage.
"""

from __future__ import annotations

import numpy as np
import pytest

from argus.capture.resample import Resampler, to_float32, to_int16, to_mono
from argus.core.events import SAMPLE_RATE


def sine(freq: float, seconds: float, rate: int, amplitude: float, channels: int = 1):
    """A sine in int16, with interleaved channels, as PortAudio delivers it."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    wave = (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16)
    if channels == 1:
        return wave
    return np.repeat(wave, channels)


def peak(samples: np.ndarray) -> float:
    """Peak amplitude as a fraction of full scale."""
    return float(np.max(np.abs(samples.astype(np.float32)))) / 32767.0


# --------------------------------------------------------------------------
# Elementary conversions
# --------------------------------------------------------------------------


def test_mono_passes_single_channel_through():
    """A headset microphone is single-channel, and downmixing must not touch it."""
    x = np.array([1, 2, 3], dtype=np.int16)
    assert to_mono(x, channels=1) is x


def test_mono_averages_stereo():
    x = np.array([0.0, 1.0, 2.0, 3.0], dtype=np.float32)  # L,R,L,R
    assert to_mono(x, channels=2).tolist() == [0.5, 2.5]


def test_mono_can_take_left_channel_instead():
    x = np.array([0.0, 1.0, 2.0, 3.0], dtype=np.float32)
    assert to_mono(x, channels=2, average=False).tolist() == [0.0, 2.0]


def test_int16_roundtrip_keeps_scale():
    x = np.array([-32767, 0, 32767], dtype=np.int16)
    assert to_int16(to_float32(x)).tolist() == [-32767, 0, 32767]


def test_int16_clips_instead_of_wrapping():
    """An overshoot must hit the edge, not wrap around to the opposite one."""
    x = np.array([-2.0, 2.0], dtype=np.float32)
    assert to_int16(x).tolist() == [-32767, 32767]


# --------------------------------------------------------------------------
# Resampler
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "channels"),
    [
        (44100, 2),  # built-in microphone
        (48000, 1),  # HyperX microphone
        (48000, 2),  # headphones loopback
        (16000, 1),  # already the needed format: passes straight through
        (32000, 1),  # webcam microphone
    ],
)
def test_amplitude_is_preserved(rate: int, channels: int):
    """Output loudness must match input loudness.

    This is the test that was missing: with a scaling error the output hits 1.0 for any input,
    and on the meter it looks like normal loud sound.
    """
    r = Resampler(rate, channels)
    out = r.process(sine(440, 0.5, rate, amplitude=0.25, channels=channels))

    # We take the middle so as not to hit the filter's ramp-up in the first samples.
    body = out[len(out) // 4 : -len(out) // 4]
    assert peak(body) == pytest.approx(0.25, abs=0.02)


def test_quiet_stays_quiet():
    """Quiet input must not turn into loud output."""
    r = Resampler(44100, 2)
    out = r.process(sine(440, 0.5, 44100, amplitude=0.01, channels=2))
    assert peak(out) < 0.05


@pytest.mark.parametrize("rate", [44100, 48000, 32000])
def test_output_rate_is_16k(rate: int):
    """The output length must correspond to 16 kHz regardless of the input.

    We count together with `flush()`: a quality resampler keeps a tail of about 340 samples
    (~21 ms) in its filter, and without a flush it stays inside. This constant has a
    negligible effect on the pipeline's total delay, but it must be accounted for in the
    length arithmetic, otherwise it seems that sound disappears.
    """
    r = Resampler(rate, 1)
    seconds = 1.0
    out = np.concatenate([r.process(sine(440, seconds, rate, amplitude=0.5)), r.flush()])
    expected = SAMPLE_RATE * seconds
    assert abs(len(out) - expected) < expected * 0.01


@pytest.mark.parametrize("rate", [44100, 48000])
def test_filter_delay_is_small(rate: int):
    """The filter delay must be a few milliseconds, not hundreds."""
    r = Resampler(rate, 1)
    r.process(sine(440, 1.0, rate, amplitude=0.5))
    delay_ms = len(r.flush()) / SAMPLE_RATE * 1000
    assert delay_ms < 30


def test_passthrough_when_rate_already_matches():
    r = Resampler(SAMPLE_RATE, 1)
    assert r.passthrough
    x = sine(440, 0.1, SAMPLE_RATE, amplitude=0.5)
    assert len(r.process(x)) == len(x)


def test_stream_has_no_seams_between_chunks():
    """Chunks must join without clicks: the resampler remembers the tail.

    Without state between calls there would be a break at every joint, and Whisper hears such
    breaks as word fragments.
    """
    rate = 44100
    whole = sine(100, 0.6, rate, amplitude=0.5)

    streamed = Resampler(rate, 1)
    step = len(whole) // 12
    chunks = [streamed.process(whole[i : i + step]) for i in range(0, len(whole), step)]
    joined = np.concatenate([c for c in chunks if len(c)])

    # Neighboring samples of a low-frequency sine change smoothly; a click at a joint would give
    # a jump many times the typical step.
    steps = np.abs(np.diff(joined.astype(np.int32)))
    assert steps.max() < 6 * float(np.median(steps[steps > 0]))


def test_flush_returns_tail():
    r = Resampler(44100, 1)
    r.process(sine(440, 0.2, 44100, amplitude=0.5))
    tail = r.flush()
    assert tail.dtype == np.int16
