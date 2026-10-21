"""Finding speech in a stream and cutting it into utterances.

Why this is needed at all. Whisper cannot simply be fed continuous audio: it is designed
for meaningful segments and on silence starts to invent text, usually scraps of credits
such as "Subtitles made by DimaTorzok" (in Russian), learned from the
training data. Also, recognition costs computation, and spending it on silence is a
pointless GPU expense. So first comes a cheap speech detector, then the expensive
recognition.

The model is Silero VAD v5 in ONNX: 2.3 MB, runs on the CPU hundreds of times faster than
real time, and does not require PyTorch. It computes the speech probability for a window
of 512 samples (32 ms at 16 kHz). One `AudioChunk` carries exactly that much with the
default settings, so usually this is one call per chunk.

The cutting itself is a state machine with hysteresis. Plain "probability > threshold"
does not work: the detector flickers on pauses between words, and a phrase would fall
apart into a dozen scraps. So speech must last `min_speech_ms` to start, and silence must
last `min_silence_ms` for the phrase to count as finished.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from argus.core.events import SAMPLE_RATE, AudioChunk, Source, Utterance

log = logging.getLogger(__name__)

NS_PER_SEC = 1_000_000_000
# The window size the model is designed for. It must not be changed: the model was trained
# on it.
FRAME = 512
# The tail of the previous window that the model requires to see together with the new one.
CONTEXT = 64
FRAME_NS = FRAME * NS_PER_SEC // SAMPLE_RATE

# How far a chunk's time may differ from the place where we expected it while the stream
# is still considered continuous. A chunk's time is taken from the clock at the moment it
# arrives, so a callback delayed by a jam gives a spread of hundreds of milliseconds with
# intact audio: such an error in a timestamp is not worth breaking an utterance. A second
# or more is a recognition pause, an audio reopen, or a silent loopback: there the audio is
# really missing, and the time must be taken from the chunk, not from the sample counter.
GAP_NS = 1_000_000_000

VAD_MODEL_NAME = "silero_vad.onnx"
# The address is pinned to a commit (`add v6.2 model`), not to the `master` branch: the
# branch moves, and a file swapped in someone else's repository would immediately reach
# everyone who needs to download the weights. It is the same file, by hash, as the one in
# `models/` on the developer's machine (verified by downloading on 2026-10-14: the commit
# and the branch give the same sum, `VAD_MODEL_SHA256`).
VAD_MODEL_COMMIT = "bfdc0193023f121ea5b3cc7b176dbed570a68a59"
VAD_MODEL_URL = f"https://github.com/snakers4/silero-vad/raw/{VAD_MODEL_COMMIT}/src/silero_vad/data/silero_vad.onnx"

# Download: without a timeout, a broken connection would hang startup forever, and a file
# of any size (a provider's page instead of the weights) would stay on disk and break
# startup every time. The timeout applies to each socket wait, not to the whole file.
DOWNLOAD_TIMEOUT_S = 30
MIN_MODEL_BYTES = 1_000_000

# Checksum of the verified weights (SHA-256, 64 characters): a download that does not match
# it is not accepted and does not stay on disk. The same sum is written to
# `packaging/silero_vad.sha256`, which the Mac build checks (`.github/workflows/macos.yml`),
# and a test makes sure the two records do not drift apart. Empty means no check (this
# happens only in tests).
VAD_MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"


def ensure_model(models_dir: Path) -> Path:
    """Return the path to the model, downloading it on the first launch. What was downloaded is
    checked against `VAD_MODEL_SHA256`; if it does not match, it is refused.
    """
    path = models_dir / VAD_MODEL_NAME
    if path.exists():
        return path

    import hashlib
    import urllib.request

    models_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading the VAD model to %s", path)
    tmp = path.with_suffix(".part")
    try:
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(VAD_MODEL_URL, timeout=DOWNLOAD_TIMEOUT_S) as resp, \
                open(tmp, "wb") as out:
            while block := resp.read(1 << 16):
                out.write(block)
                digest.update(block)
                size += len(block)
        if size < MIN_MODEL_BYTES:
            raise RuntimeError(
                "The file for speech recognition was downloaded damaged. "
                "Check your internet connection and start Argus again."
            )
        if VAD_MODEL_SHA256 and digest.hexdigest() != VAD_MODEL_SHA256.lower():
            # A wrong file, not a broken transfer: the size is fine, the content is foreign (swapped on
            # the way, or a provider's placeholder page).
            log.error("VAD weights: checksum mismatch (got %s, expected %s) — file rejected", digest.hexdigest(), VAD_MODEL_SHA256)
            raise RuntimeError(
                "The downloaded speech recognition file did not match the verified one and was rejected. "
                "Start Argus again, and if it repeats, check your connection or update the app."
            )
        tmp.replace(path)
    finally:
        # We do not leave a partial or rejected file: otherwise on the next start `path.exists()`
        # would take it for a ready one.
        tmp.unlink(missing_ok=True)
    return path


class SileroVAD:
    """A wrapper over the model: an audio window -> the probability that it is speech.

    Two subtleties, each of which, if violated, gives a silently wrong result rather than an
    error.

    The first: the input is not 512 samples but **576: 64 from the tail of the previous
    window plus 512 new ones**. The model needs context to hear the start of a sound at the
    window boundary. The model's dimensions are dynamic, so ONNX Runtime happily accepts an
    array of the wrong length and returns garbage. We measured that without the context the
    speech probability stays at 0.001 even on loud speech, while with the context it reaches
    1.0 on the same file.

    The second: the model is recurrent and remembers state between windows, so each channel
    must have its own instance. One shared by the microphone and the system audio would mix
    up the state, and the detector would react to the other channel's speech.
    """

    __slots__ = ("_context", "_session", "_sr", "_state")

    def __init__(self, model_path: Path) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        # The model is tiny; extra threads only add overhead to it, and the cores are needed more
        # by OCR and the rest of the pipeline.
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        """Probability of speech in a window of exactly FRAME samples (int16)."""
        x = np.concatenate([
            self._context,
            frame.astype(np.float32) / 32768.0,
        ]).reshape(1, -1)
        out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = x[0, -CONTEXT:]
        return float(out[0][0])


@dataclass(slots=True)
class VADSettings:
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 600
    speech_pad_ms: int = 250
    max_utterance_s: float = 25.0


class UtteranceSegmenter:
    """Cuts the stream of one channel into utterances.

    It keeps a small reserve of audio from *before* the start of speech (`speech_pad_ms`):
    the detector fires already on the first sound, and without the reserve the start of a
    word ends up cut off. On a chopped start Whisper loses the whole first word.
    """

    def __init__(self, vad: SileroVAD, source: Source, settings: VADSettings) -> None:
        self.vad = vad
        self.source = source
        self.s = settings

        self._tail = np.zeros(0, dtype=np.int16)   # incomplete window left from last time
        self._tail_t = 0                           # start time of this window
        self._fed = False                          # whether any chunk has arrived yet: before that there is nothing to compare the time with
        self._speech: list[np.ndarray] = []        # the current utterance accumulates here
        self._speech_t = 0
        self._in_speech = False
        self._speech_ms = 0.0                      # how much "speech" in a row
        self._silence_ms = 0.0                     # how much "silence" in a row

        # The reserve before speech accumulates while speech is still being confirmed, so the queue
        # must hold the confirmation windows plus the reserve itself. With the reserve alone (as it
        # used to be), the first speech windows left the queue before confirmation: `speech_pad_ms`
        # did not work, and the utterance began at the second speech window, so the first consonant
        # of a short Russian "da" (yes) was cut off.
        pad_frames = max(1, self.s.speech_pad_ms * SAMPLE_RATE // 1000 // FRAME)
        confirm_frames = max(1, math.ceil(self.s.min_speech_ms / (FRAME / SAMPLE_RATE * 1000)))
        self._preroll: deque[tuple[int, np.ndarray]] = deque(maxlen=pad_frames + confirm_frames)

        self.utterances = 0
        self.frames_seen = 0
        self.speech_frames = 0

    def feed(self, chunk: AudioChunk) -> list[Utterance]:
        """Feed a piece of the stream, get ready utterances (usually zero)."""
        if chunk.source is not self.source:
            return []

        done: list[Utterance] = []
        # Time advances by the sample counter from `_tail_t` while there is something in the tail,
        # and from the chunk itself when the tail is empty. If a tail was left over, a recognition
        # pause went by, or the audio was reopened, the new audio got the old audio's time, and
        # utterances landed in the timeline earlier than the start of the conversation. So we
        # compare where we expected the start of this chunk and where the chunk itself says it
        # started.
        if self._fed:
            expected = self._tail_t + len(self._tail) * NS_PER_SEC // SAMPLE_RATE
            if abs(chunk.t - expected) > GAP_NS:
                finished = self._break()
                if finished is not None:
                    done.append(finished)
        self._fed = True

        if len(self._tail):
            samples = np.concatenate([self._tail, chunk.samples])
            start_t = self._tail_t
        else:
            samples, start_t = chunk.samples, chunk.t

        n_frames = len(samples) // FRAME
        for i in range(n_frames):
            frame = samples[i * FRAME : (i + 1) * FRAME]
            out = self._step(frame, start_t + i * FRAME_NS)
            if out is not None:
                done.append(out)

        used = n_frames * FRAME
        self._tail = samples[used:]
        self._tail_t = start_t + n_frames * FRAME_NS
        return done

    def _step(self, frame: np.ndarray, t: int) -> Utterance | None:
        self.frames_seen += 1
        is_speech = self.vad.probability(frame) >= self.s.threshold
        if is_speech:
            self.speech_frames += 1
        frame_ms = FRAME / SAMPLE_RATE * 1000

        if not self._in_speech:
            self._preroll.append((t, frame))
            if is_speech:
                self._speech_ms += frame_ms
                if self._speech_ms >= self.s.min_speech_ms:
                    # We start the utterance not from the current window but from the reserve before it.
                    self._speech = [f for _, f in self._preroll]
                    self._speech_t = self._preroll[0][0]
                    self._preroll.clear()
                    self._in_speech = True
                    self._silence_ms = 0.0
            else:
                self._speech_ms = 0.0
            return None

        self._speech.append(frame)
        self._silence_ms = 0.0 if is_speech else self._silence_ms + frame_ms

        too_long = len(self._speech) * FRAME / SAMPLE_RATE >= self.s.max_utterance_s
        if self._silence_ms >= self.s.min_silence_ms or too_long:
            return self._finish(t + FRAME_NS)
        return None

    def _finish(self, t_end: int) -> Utterance | None:
        samples = np.concatenate(self._speech) if self._speech else np.zeros(0, np.int16)
        self._speech = []
        self._in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._preroll.clear()
        if len(samples) < FRAME:
            return None
        self.utterances += 1
        return Utterance(
            t=self._speech_t,
            t_end=t_end,
            source=self.source,
            samples=samples,
        )

    def _break(self) -> Utterance | None:
        """The stream broke: close the utterance in progress where the audio ended, and forget
        everything else.

        Close it, do not drop it: what was said before the break is real, and with its own, old,
        time it will take its place in the timeline (it will also be filtered out as "before the
        conversation began", if that is so). Dropping it would mean discarding what was already
        said, and gluing it to what comes after would produce an utterance spanning the whole gap.
        The window tail, the reserve before speech and the confirmation counters belong to the
        audio on the other side of the break, and so does the model state.
        """
        finished = self.flush()
        self._tail = np.zeros(0, dtype=np.int16)
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._preroll.clear()
        self.vad.reset()
        return finished

    def reset(self) -> None:
        """Forget what was left unfinished. Needed when a channel is turned off on the fly.

        Without it, after turning it on again, the detector would finish a phrase started half an
        hour ago, and it would take its place in the timeline as a fresh one.
        """
        self._tail = np.zeros(0, dtype=np.int16)
        self._fed = False
        self._speech = []
        self._in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._preroll.clear()
        self.vad.reset()

    def flush(self) -> Utterance | None:
        """Close the unfinished utterance when the stream stops.

        Otherwise the last phrase of a session, after which `min_silence_ms` of silence had no
        time to accumulate, would simply be lost, and in a recording that is often the most
        interesting place.
        """
        if not self._in_speech:
            return None
        n = sum(len(f) for f in self._speech)
        return self._finish(self._speech_t + n * NS_PER_SEC // SAMPLE_RATE)
