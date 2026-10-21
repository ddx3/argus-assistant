"""Checks for speech detection and cutting into utterances.

The cutting logic is checked on a fake detector with a "speech/silence" script: this way
the tests do not depend on the model, they are fast, and they check exactly what was
written by hand: hysteresis, the lead-in before the start, the length ceiling, the
unfinished tail.

Separately there are checks on the real model and real speech. They are marked
`needs_model` and skipped if the weights are missing: there is no point downloading 2 MB
on every test run, but the error in calling the model must be caught, because that is
exactly where the pipeline once silently returned zero speech on a loud recording.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from argus.core.events import SAMPLE_RATE, AudioChunk, Source
from argus.perception.vad import (
    CONTEXT,
    FRAME,
    SileroVAD,
    UtteranceSegmenter,
    VADSettings,
)

NS = 1_000_000_000
SPEECH_WAV = Path(__file__).parent / "data" / "speech_ru.wav"
VAD_MODEL = Path(__file__).parents[1] / "models" / "silero_vad.onnx"

needs_model = pytest.mark.skipif(not VAD_MODEL.exists(), reason="no Silero VAD weights")


class ScriptedVAD:
    """A fake detector: returns a predefined sequence."""

    def __init__(self, script: list[bool]) -> None:
        self.script = script
        self.i = 0

    def probability(self, frame: np.ndarray) -> float:
        value = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return 0.9 if value else 0.05

    def reset(self) -> None:
        self.i = 0


def frames_of(pattern: str) -> list[bool]:
    """"--##--" -> [False, False, True, True, False, False]. One window = 32 ms."""
    return [c == "#" for c in pattern]


def feed_pattern(seg: UtteranceSegmenter, n_frames: int):
    """Feed n_frames windows of silence as a stream of chunks."""
    out = []
    samples = np.zeros(FRAME, dtype=np.int16)
    for i in range(n_frames):
        out += seg.feed(
            AudioChunk(t=i * FRAME * NS // SAMPLE_RATE, source=Source.ME, samples=samples)
        )
    return out


def make(script: str, **overrides):
    settings = VADSettings(
        min_speech_ms=overrides.pop("min_speech_ms", 64),    # 2 windows
        min_silence_ms=overrides.pop("min_silence_ms", 128),  # 4 windows
        speech_pad_ms=overrides.pop("speech_pad_ms", 0),
        **overrides,
    )
    seg = UtteranceSegmenter(ScriptedVAD(frames_of(script)), Source.ME, settings)
    return seg, feed_pattern(seg, len(script))


# --------------------------------------------------------------------------
# The cutting logic
# --------------------------------------------------------------------------


def test_silence_produces_nothing():
    _, out = make("-" * 20)
    assert out == []


def test_speech_becomes_one_utterance():
    _, out = make("----########----------")
    assert len(out) == 1
    assert out[0].source is Source.ME


def test_short_blip_is_ignored():
    """A single click shorter than min_speech_ms must not produce an utterance."""
    _, out = make("-----#------------")
    assert out == []


def test_pause_between_words_does_not_split_phrase():
    """The main reason hysteresis is needed: a pause inside a phrase does not cut it.

    Without it "Tell me ... about your experience" would turn
    into two utterances, and Whisper would recognize scraps instead of a sentence.
    """
    _, out = make("---#####--#####------------")
    assert len(out) == 1


def test_real_pause_splits_into_two():
    """But a long pause is already the end of a phrase."""
    _, out = make("---#####--------#####------------")
    assert len(out) == 2


def test_preroll_captures_start_of_speech():
    """The lead-in before the start must get into the utterance, otherwise the first word is
    lost.

    The detector fires on the very first sound, and `min_speech_ms` adds a delay on top.
    Without the lead-in the utterance would start in the middle of the first word, and
    Whisper would lose it entirely.
    """
    pattern = "-----########--------"
    _, plain = make(pattern, speech_pad_ms=0)
    _, padded = make(pattern, speech_pad_ms=192)  # 6 windows of lead-in

    assert len(padded[0].samples) > len(plain[0].samples)
    assert padded[0].t < plain[0].t


def test_long_speech_is_cut_at_the_limit():
    """An utterance must not outgrow Whisper's 30-second window."""
    _, out = make("#" * 400, max_utterance_s=2.0)
    assert len(out) >= 2
    assert all(u.duration_s <= 2.5 for u in out)


def test_flush_closes_unfinished_speech():
    """The last phrase of the session must not be lost just because there was no silence."""
    seg, out = make("----########")
    assert out == []          # there was no silence after the speech
    tail = seg.flush()
    assert tail is not None
    assert tail.duration_s > 0


def test_flush_on_silence_returns_nothing():
    seg, _ = make("--------")
    assert seg.flush() is None


def test_chunks_are_reassembled_across_boundaries():
    """Stream chunks are not a multiple of the window, so the detector must glue the tails
    together.

    Otherwise the last samples would be lost on every chunk, and a noticeable time shift
    would build up over a long session.
    """
    seg = UtteranceSegmenter(
        ScriptedVAD([True] * 100), Source.ME,
        VADSettings(min_speech_ms=64, min_silence_ms=128, speech_pad_ms=0),
    )
    odd = 700  # not a multiple of 512
    for i in range(10):
        seg.feed(AudioChunk(t=i * odd * NS // SAMPLE_RATE, source=Source.ME,
                            samples=np.zeros(odd, dtype=np.int16)))
    # 10 * 700 = 7000 samples -> 13 full windows, nothing lost
    assert seg.frames_seen == 7000 // FRAME


def test_other_channel_is_ignored():
    """The segmenter of one channel must not react to the other channel's sound."""
    seg = UtteranceSegmenter(
        ScriptedVAD([True] * 50), Source.ME, VADSettings()
    )
    out = seg.feed(
        AudioChunk(t=0, source=Source.THEM, samples=np.zeros(FRAME, dtype=np.int16))
    )
    assert out == []
    assert seg.frames_seen == 0


# --------------------------------------------------------------------------
# The lead-in before speech and gaps in the stream
# --------------------------------------------------------------------------

FRAME_NS = FRAME * NS // SAMPLE_RATE


class LoudVAD:
    """A fake detector by content: any nonzero sample in the window is speech.

    The "by window count" script (`ScriptedVAD`) does not fit here: a gap in the stream
    resets the model's state, and on a reset the script starts over.
    """

    def probability(self, frame: np.ndarray) -> float:
        return 0.9 if frame.any() else 0.05

    def reset(self) -> None:
        pass


def short_phrases() -> VADSettings:
    return VADSettings(min_speech_ms=64, min_silence_ms=128, speech_pad_ms=0)


def feed_run(seg, start_ns: int, chunks: int, loud: bool, n: int = 700, jitter=lambda k: 0):
    """Feed `chunks` chunks of `n` samples (not a multiple of the window, so a tail remains).

    The time of a chunk is by sample count plus `jitter(k)`; returns (utterances, end time).
    """
    out = []
    step_ns = n * NS // SAMPLE_RATE
    for k in range(chunks):
        samples = np.full(n, 1000, dtype=np.int16) if loud else np.zeros(n, dtype=np.int16)
        out += seg.feed(AudioChunk(
            t=start_ns + k * step_ns + jitter(k), source=Source.ME, samples=samples))
    return out, start_ns + chunks * step_ns


def test_default_pad_keeps_the_first_speech_frame_and_the_lead_in():
    """Defaults: speech confirmation is 8 windows (250 ms), the lead-in before it is 7 more.

    The lead-in queue held only 7 windows: by the time of confirmation the first speech
    window was already leaving it, and the utterance started from the second window with no
    lead-in at all, so the first consonant of "yes" and "no" was cut off.
    """
    s = VADSettings()
    pad = s.speech_pad_ms * SAMPLE_RATE // 1000 // FRAME
    assert pad == 7
    first_speech = 12
    seg = UtteranceSegmenter(
        ScriptedVAD(frames_of("-" * first_speech + "#" * 12 + "-" * 30)), Source.ME, s)
    out = []
    for i in range(first_speech + 12 + 30):
        # Each window holds its own number, which shows which windows went into the utterance.
        samples = np.full(FRAME, i + 1, dtype=np.int16)
        out += seg.feed(AudioChunk(t=i * FRAME_NS, source=Source.ME, samples=samples))
    assert len(out) == 1

    numbers = [int(out[0].samples[k * FRAME]) - 1 for k in range(len(out[0].samples) // FRAME)]
    assert first_speech in numbers, "the first window of speech is lost"
    assert numbers[0] == first_speech - pad, "the lead-in before the speech did not fill up"
    assert out[0].t == (first_speech - pad) * FRAME_NS


def test_audio_after_a_pause_gets_its_own_time_not_the_stale_one():
    """A window tail remained, a pause passed: new audio must not get the old time.

    Recognition was stopped and turned on an hour later: the samples were counted from a mark
    an hour old, and the utterances of the first seconds landed in the feed before the start
    of the conversation (and were filtered out there).
    """
    seg = UtteranceSegmenter(LoudVAD(), Source.ME, short_phrases())
    _, end = feed_run(seg, 0, 10, loud=False)
    assert len(seg._tail), "the precondition of the bug: something is left in the window tail"

    hour = 3600 * NS
    out, end2 = feed_run(seg, end + hour, 40, loud=True)
    more, _ = feed_run(seg, end2, 20, loud=False)
    out += more

    assert len(out) == 1
    assert out[0].t >= end + hour, "the utterance got a time from before the pause"
    assert out[0].t_end <= end2 + 250_000_000      # the speech + the 128 ms of silence that closed it


def test_an_open_utterance_is_closed_at_the_gap_not_glued_to_what_follows():
    """An utterance begun before the gap ends where the sound ended."""
    seg = UtteranceSegmenter(LoudVAD(), Source.ME, short_phrases())
    out, end = feed_run(seg, 0, 40, loud=True)
    assert out == [], "precondition: the utterance is still open, there was no silence after the speech"

    later = end + 10 * NS
    more, end2 = feed_run(seg, later, 40, loud=True)
    more += feed_run(seg, end2, 20, loud=False)[0]

    assert len(more) == 2, "the utterance was glued together across the gap"
    before, after = more
    assert before.t_end <= end
    assert after.t >= later


def test_a_stall_in_the_callback_does_not_cut_a_phrase():
    """A stall in the callback: the audio is intact but the chunk times slipped by 400 ms; that
    does not cut the utterance.
    """
    seg = UtteranceSegmenter(LoudVAD(), Source.ME, short_phrases())
    out, end = feed_run(
        seg, 0, 40, loud=True, jitter=lambda k: 400_000_000 if 15 <= k < 27 else 0)
    out += feed_run(seg, end, 20, loud=False)[0]
    assert len(out) == 1


def test_the_first_chunk_is_never_a_gap():
    """Before the first chunk there is nothing to compare the time with: a large timestamp at the
    start is not a gap.
    """
    seg = UtteranceSegmenter(LoudVAD(), Source.ME, short_phrases())
    start = 5000 * NS
    out, end = feed_run(seg, start, 40, loud=True)
    out += feed_run(seg, end, 20, loud=False)[0]
    assert len(out) == 1
    assert out[0].t >= start


# --------------------------------------------------------------------------
# Downloading the weights
# --------------------------------------------------------------------------


class FakeDownload:
    """A fake `urlopen`: remembers what timeout it was called with and returns the given bytes."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.timeout = None

    def __call__(self, url, timeout=None):
        import io

        self.timeout = timeout
        return io.BytesIO(self.body)        # as a response: `with` and `read(n)`


def test_download_has_a_timeout(tmp_path, monkeypatch):
    """Without a timeout, a broken connection hung app startup forever."""
    import urllib.request

    from argus.perception import vad

    import hashlib

    fake = FakeDownload(b"x" * (vad.MIN_MODEL_BYTES + 10))
    monkeypatch.setattr(vad, "VAD_MODEL_SHA256", hashlib.sha256(fake.body).hexdigest())     # fake "verified" content
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    path = vad.ensure_model(tmp_path)
    assert fake.timeout is not None and 0 < fake.timeout <= 60
    assert path.read_bytes() == fake.body
    assert not list(tmp_path.glob("*.part"))


def test_a_tiny_download_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    """A provider's page instead of the weights must not sit there as a ready model."""
    import urllib.request

    from argus.perception import vad

    monkeypatch.setattr(urllib.request, "urlopen", FakeDownload(b"<html>blocked</html>"))
    with pytest.raises(RuntimeError, match="downloaded damaged"):
        vad.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_pinned_checksum_rejects_a_changed_file(tmp_path, monkeypatch):
    import hashlib
    import urllib.request

    from argus.perception import vad

    good = b"a" * (vad.MIN_MODEL_BYTES + 1)
    monkeypatch.setattr(vad, "VAD_MODEL_SHA256", hashlib.sha256(good).hexdigest())

    monkeypatch.setattr(urllib.request, "urlopen", FakeDownload(b"b" * (vad.MIN_MODEL_BYTES + 1)))
    with pytest.raises(RuntimeError, match="did not match"):
        vad.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(urllib.request, "urlopen", FakeDownload(good))
    assert vad.ensure_model(tmp_path).read_bytes() == good


def test_the_download_is_pinned_to_a_commit_and_to_the_checksum_the_mac_build_checks():
    """The moving `master` branch of someone else's repository is not a source for weights that
    we later run on people's machines.
    """
    import re

    from argus.perception import vad

    assert re.fullmatch(r"[0-9a-f]{40}", vad.VAD_MODEL_COMMIT)
    assert vad.VAD_MODEL_COMMIT in vad.VAD_MODEL_URL and "/master/" not in vad.VAD_MODEL_URL
    assert re.fullmatch(r"[0-9a-f]{64}", vad.VAD_MODEL_SHA256)
    root = Path(__file__).parents[1]
    pinned = (root / "packaging" / "silero_vad.sha256").read_text(encoding="utf-8")
    assert pinned == f"{vad.VAD_MODEL_SHA256}  models/{vad.VAD_MODEL_NAME}\n", "`shasum -c` parses exactly such a line, and without \\r"
    workflow = (root / ".github" / "workflows" / "macos.yml").read_text(encoding="utf-8")
    assert vad.VAD_MODEL_URL in workflow, "the Mac build downloads the same weights from the same commit"
    assert "raw/master/" not in workflow


@needs_model
def test_the_weights_in_the_project_are_the_pinned_ones():
    """The file that `stage.py` puts into the Windows build is the very one whose checksum is
    pinned.
    """
    import hashlib

    from argus.perception import vad

    assert hashlib.sha256(VAD_MODEL.read_bytes()).hexdigest() == vad.VAD_MODEL_SHA256


def test_an_existing_model_is_not_downloaded_again(tmp_path, monkeypatch):
    import urllib.request

    from argus.perception import vad

    (tmp_path / vad.VAD_MODEL_NAME).write_bytes(b"mine")

    def boom(*a, **kw):
        raise AssertionError("a ready file must not be downloaded")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert vad.ensure_model(tmp_path).read_bytes() == b"mine"


# --------------------------------------------------------------------------
# The real model
# --------------------------------------------------------------------------


@needs_model
def test_model_needs_context_to_work():
    """Regression: without 64 samples of context the model silently returns almost zero.

    That is how it was in the first version: ONNX Runtime accepted an array of the wrong
    length without a single complaint, and the detector found no speech anywhere at all. The
    error was discovered only from the result, so it is pinned down here by a test.
    """
    vad = SileroVAD(VAD_MODEL)
    assert vad._context.shape == (CONTEXT,)

    with wave.open(str(SPEECH_WAV), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    probs = [vad.probability(audio[i : i + FRAME]) for i in range(0, len(audio) - FRAME, FRAME)]
    assert max(probs) > 0.9, "the model found no speech in a recording where there certainly is some"
    assert 0.2 < sum(p > 0.5 for p in probs) / len(probs) < 0.9


@needs_model
@pytest.mark.skipif(not SPEECH_WAV.exists(), reason="no recording with speech")
def test_real_speech_splits_into_three_phrases():
    """A recording of three phrases with pauses must give exactly three utterances."""
    from argus.perception.vad import SileroVAD

    with wave.open(str(SPEECH_WAV), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    seg = UtteranceSegmenter(SileroVAD(VAD_MODEL), Source.THEM, VADSettings())
    step = SAMPLE_RATE * 32 // 1000
    out = []
    for i in range(0, len(audio), step):
        out += seg.feed(
            AudioChunk(t=i * NS // SAMPLE_RATE, source=Source.THEM, samples=audio[i : i + step])
        )
    tail = seg.flush()
    if tail is not None:
        out.append(tail)

    assert len(out) == 3
    assert all(3.0 < u.duration_s < 6.0 for u in out)
