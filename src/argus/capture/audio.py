"""Audio capture: microphone and system audio as two independent streams.

Two streams, not one mixed stream, is the main decision of the whole project. The
microphone is "me", the loopback is "I am being spoken to". While they are separate, the
question "who is speaking now" is solved for free. Mix them and you will have to drag in
speaker recognition, which is heavy and inaccurate.

Devices are chosen **by name, not by number**. Numbers in Windows drift: verified on this
machine, as soon as a wireless headset was plugged in, the microphone moved from 15 to 20
and the loopback from 16 to 23. A program with hardcoded numbers would not crash but
would silently listen to someone else's device, and finding that would take a long time.

Input and output are chosen **independently**. Also verified: Windows switched the output
to the headset but left the input on the built-in microphone. A single "current device"
for both channels would give silence in half of the cases.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import numpy as np

# WASAPI capture is Windows only. On macOS this module is needed for the shared types
# (`DeviceInfo`, `DeviceNotFound`, constants), and the audio comes from `audio_mac`: the
# choice by system is in `argus.capture.backend`.
try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover — the package is present on Windows
    pyaudio = None  # type: ignore[assignment]

from argus.capture.resample import Resampler
from argus.core import friendly
from argus.core.events import SAMPLE_RATE, AudioChunk, Source

# How much flat silence on the microphone counts as a fault. A live microphone always
# produces noise, so zero for longer than a few seconds is not "quiet in the room" but "the
# device is silent". Fifteen seconds leave a margin for pauses at the start of a session.
MIC_SILENCE_S = 15.0

# How often to ask the system which devices are the defaults now.
#
# We have to ask **the system itself** (`capture/mmdevice.py`), not PortAudio: it takes the
# device list once, at first initialization, and a new `PyAudio()` during a live capture
# only adds one to the reference count and returns the same old picture. Because of this,
# the microphone choice showed a single headset, a webcam plugged in later did not appear
# at all, and the "we are listening to the wrong thing" check stayed
# silent: it was comparing the state at launch with itself.
#
# The poll costs a few milliseconds, but there is no need to trigger it twice a second, as
# often as the state is broadcast. So it runs once every five seconds and in a separate
# thread.
DEVICE_POLL_S = 5.0

# PortAudio instances, one at a time.
#
# `Pa_Initialize` and `Pa_Terminate` count references, but do so with no protection against
# races: two threads bringing the library up and down at the same time crash the process
# with an access violation. Caught by the tests: it was not our test that failed but a
# neighboring one, simply because it happened to be close in time.
_PA_LOCK = threading.Lock()

if TYPE_CHECKING:
    from argus.core.bus import Bus
    from argus.core.clock import Clock
    from argus.core.config import AudioConfig

log = logging.getLogger(__name__)

NS_PER_SEC = 1_000_000_000


class DeviceNotFound(RuntimeError):
    """The needed device was not found: better to say so at once than to record silence."""


def _norm(name: str) -> str:
    """Device name for comparison: the one we opened comes from PortAudio, the current one from
    the system.

    They match letter for letter, but a false "we are listening to the wrong thing"
    complaint on a healthy machine is worse than a missed one, so spaces and case are
    left out of the comparison.
    """
    return " ".join(name.split()).casefold()


@dataclass(slots=True)
class DeviceInfo:
    """What we need to know about a device, without the rest of the PortAudio dictionary."""

    index: int
    name: str
    rate: int
    channels: int

    # What was asked for but not found. Filled in when the pinned device disappeared and the
    # system one had to be taken: without this mark the substitution would pass silently, and
    # the person would be left sure that their choice is being listened to.
    missed: str | None = None

    @classmethod
    def from_pa(cls, d: dict) -> DeviceInfo:
        return cls(
            index=int(d["index"]),
            name=str(d["name"]),
            rate=int(d["defaultSampleRate"]),
            channels=int(d["maxInputChannels"]),
        )


def _wasapi_devices(p: pyaudio.PyAudio) -> list[dict]:
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    out = []
    for idx in range(p.get_device_count()):
        d = p.get_device_info_by_index(idx)
        if d["hostApi"] == api["index"]:
            out.append(d)
    return out


def find_microphone(p: pyaudio.PyAudio, spec: int | str | None) -> DeviceInfo:
    """Find the microphone: by index, by part of the name, or the system default."""
    devices = [
        d for d in _wasapi_devices(p) if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice")
    ]
    if isinstance(spec, int):
        return DeviceInfo.from_pa(p.get_device_info_by_index(spec))
    if isinstance(spec, str):
        match = next((d for d in devices if spec.lower() in d["name"].lower()), None)
        if match is None:
            # We do not die: we take the system one and say so.
            #
            # Before, this was a refusal to start, and for a name typed by hand into the config that
            # was right. But now the device is chosen with the mouse in the dashboard, and what gets
            # chosen on this machine is a wireless headset that falls asleep by itself. A refusal would
            # mean: pinned the headphones, in the morning they did not wake up, and the program does
            # not come up at all, along with recognition and recording.
            #
            # Staying silent is not an option either: listening to something other than what was asked
            # for, and not saying so, is exactly the quiet failure this whole thing was started for.
            # So we take the system one, and the complaint goes to the top of the page.
            log.warning("pinned microphone '%s' not found - using the system one", spec)
            return _default_input(p, missed=spec)
        return DeviceInfo.from_pa(match)

    return _default_input(p)


def _default_input(p: pyaudio.PyAudio, missed: str | None = None) -> DeviceInfo:
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    index = api["defaultInputDevice"]
    # No microphone at all (a PC without one, or one disabled or forbidden in the sound
    # settings): PortAudio returns the number -1, and `get_device_info_by_index(-1)` raises a
    # bare `OSError` "[Errno -9996]": the person would see a code instead of the reason.
    if index < 0:
        raise DeviceNotFound(
            "No microphone found. Plug one in or enable it in the Windows sound settings "
            "and start Argus again."
        )
    info = DeviceInfo.from_pa(p.get_device_info_by_index(index))
    info.missed = missed
    return info


def find_loopback(p: pyaudio.PyAudio, spec: int | str | None) -> DeviceInfo:
    """Find a loopback: by index, by name, or the one paired with the current output.

    By default we take exactly the loopback that corresponds to the default output device;
    otherwise we would listen to headphones that are silent.
    """
    loopbacks = [d for d in _wasapi_devices(p) if d.get("isLoopbackDevice")]
    if isinstance(spec, int):
        return DeviceInfo.from_pa(p.get_device_info_by_index(spec))
    if isinstance(spec, str):
        match = next((d for d in loopbacks if spec.lower() in d["name"].lower()), None)
        if match is None:
            log.warning("pinned loopback '%s' not found - using the system one", spec)
            return _default_loopback(p, loopbacks, missed=spec)
        return DeviceInfo.from_pa(match)

    return _default_loopback(p, loopbacks)


def _default_loopback(
    p: pyaudio.PyAudio, loopbacks: list[dict], missed: str | None = None
) -> DeviceInfo:
    """Loopback paired with the current output device.

    A refusal remains here: without a pair for the current output, system audio cannot be
    captured at all, and substituting just anything would mean listening to someone else's
    speakers. This is not "took the wrong device" but "there is no such device".
    """
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    index = api["defaultOutputDevice"]
    # The same as with the microphone: there is no output device (happens in a remote
    # session): the number is -1.
    if index < 0:
        raise DeviceNotFound(
            "No speakers or headphones found, and system audio can't be captured without them. "
            "Plug them in or enable them in Windows sound settings and start Argus again."
        )
    speaker = p.get_device_info_by_index(index)
    match = next((d for d in loopbacks if speaker["name"] in d["name"]), None)
    if match is None:
        raise DeviceNotFound(
            f"no loopback for the output device '{speaker['name']}' — "
            "system audio cannot be captured"
        )
    info = DeviceInfo.from_pa(match)
    info.missed = missed
    return info


class _Channel:
    """One capture stream: device -> resampler -> bus.

    Lives in the thread that PortAudio creates. One rule: leave the callback as fast as
    possible. Get stuck and there will be a hole in the audio that no processing can restore
    later.
    """

    def __init__(
        self,
        pa: pyaudio.PyAudio,
        device: DeviceInfo,
        source: Source,
        bus: Bus,
        clock: Clock,
        chunk_ms: int,
        average: bool,
    ) -> None:
        self.device = device
        self.source = source
        self._bus = bus
        self._clock = clock
        self._resampler = Resampler(device.rate, device.channels, average)

        self.chunks = 0
        self.samples_out = 0
        self.error: BaseException | None = None
        self.overflows = 0

        # When the channel last had at least one nonzero sample.
        #
        # A live stream and the sound going through it are different things, and the second breaks
        # more often than the first. The headset falls asleep, Windows moves the output to another
        # device, and the stream stays open and dutifully delivers digital silence. From outside
        # this looks like "the program does not hear", and finding the cause from `alive` alone is
        # impossible: it says "alive".
        self.opened_at = time.monotonic()
        self.last_sound = 0.0

        frames = max(1, int(device.rate * chunk_ms / 1000))
        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=device.channels,
            rate=device.rate,
            input=True,
            input_device_index=device.index,
            frames_per_buffer=frames,
            stream_callback=self._on_audio,
            start=False,
        )

    def _on_audio(self, in_data, frame_count, time_info, status):
        # PortAudio simply swallows an exception inside the callback: the stream goes quiet, and
        # from outside this looks like "the microphone does not work". So we catch everything and
        # bring it outside, so that the watchdog sees it.
        try:
            if status & pyaudio.paInputOverflow:
                self.overflows += 1

            raw = np.frombuffer(in_data, dtype=np.int16)
            samples = self._resampler.process(raw)
            if len(samples):
                # We take the time from the shared clock at the moment the chunk arrives and wind it back
                # by its duration. Counting time by the number of accumulated samples is not possible: a
                # loopback can stop delivering data during silence, and then the whole scale after the
                # pause would shift.
                duration = len(samples) * NS_PER_SEC // SAMPLE_RATE
                self._bus.publish_threadsafe(
                    AudioChunk(
                        t=max(0, self._clock.now() - duration),
                        source=self.source,
                        samples=samples,
                    )
                )
                self.chunks += 1
                self.samples_out += len(samples)
                if samples.any():
                    self.last_sound = time.monotonic()
        except BaseException as e:  # noqa: BLE001 — otherwise the error disappears without a trace
            self.error = e
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    @property
    def alive(self) -> bool:
        try:
            return bool(self._stream.is_active())
        except OSError:
            return False

    @property
    def silent_s(self) -> float:
        """How many seconds in a row the channel has been exactly zero.

        Counted from the last sound, and before the first one from the opening of the stream:
        otherwise a just-opened channel would immediately look as if it had been silent for an
        eternity.
        """
        return time.monotonic() - (self.last_sound or self.opened_at)

    def start(self) -> None:
        self._stream.start_stream()

    def stop(self) -> None:
        try:
            if self._stream.is_active():
                self._stream.stop_stream()
            self._stream.close()
        except OSError as e:
            log.debug("stream %s: closed with an error: %s", self.source.value, e)


class _AppReader:
    """Audio of one application, in its own thread.

    It differs from `_Channel` only in the data source: there PortAudio calls us from its own
    thread, here we read what has accumulated ourselves. Everything else (clock, resampler,
    bus, counters) is shared.

    The own thread is needed not for speed but because of COM: it is bound to the thread
    where it was opened, and reading from the same one is a requirement, not a convenience.
    """

    def __init__(
        self,
        window: str,
        pid: int,
        source: Source,
        bus: Bus,
        clock: Clock,
        average: bool,
    ) -> None:
        from argus.capture.app_loopback import CHANNELS, RATE, ProcessLoopback

        self.window = window
        self._resampler = Resampler(RATE, CHANNELS, average)
        self._loopback = ProcessLoopback(pid)
        self.source = source
        self._bus = bus
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.chunks = 0
        self.samples_out = 0
        self.error: BaseException | None = None
        self.overflows = 0
        self.opened_at = time.monotonic()
        self.last_sound = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"app-audio-{self.source.value}-{self.window[:20]}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self._loopback.open()
        except BaseException as e:  # noqa: BLE001 — otherwise the stream dies silently
            self.error = e
            self._loopback.close()
            return

        try:
            while not self._stop.is_set():
                raw = self._loopback.read()
                if len(raw):
                    self._publish(raw)
                else:
                    # The application is silent: Windows simply gives no packets in that case. We sleep for
                    # less than a chunk lasts, so as not to accumulate delay on the very first sound.
                    self._stop.wait(0.01)
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            self._loopback.close()

    def _publish(self, raw) -> None:
        samples = self._resampler.process(raw)
        if not len(samples):
            return
        duration = len(samples) * NS_PER_SEC // SAMPLE_RATE
        self._bus.publish_threadsafe(
            AudioChunk(
                t=max(0, self._clock.now() - duration),
                source=self.source,
                samples=samples,
            )
        )
        self.chunks += 1
        self.samples_out += len(samples)
        if samples.any():
            self.last_sound = time.monotonic()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.error is None)

    @property
    def silent_s(self) -> float:
        return time.monotonic() - (self.last_sound or self.opened_at)

    def signal_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self.signal_stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class _AppChannel:
    """Audio of one or several applications instead of the audio of the whole device.

    One window is one `_AppReader`. Several windows at once are several independent
    `_AppReader`s, each on its own process and its own thread (COM is bound to the thread,
    see the `_AppReader` docstring), publishing to the same bus under the same `source`.
    There is no sample-by-sample mixing between them: chunks are published as they arrive,
    just as is already done for several simultaneous speakers in Discord
    (`discord_bot/voice.py: MixSink`). With one active source no difference is visible; with
    several people speaking at once, recognition on the overlap may degrade but does not
    break completely. More exact mixing is not for this scale.

    From outside the channel is indistinguishable from a single one: the same set of
    attributes as `_Channel`/`_AppReader`: `device`, `start`/`stop`, `alive`, `error`,
    `silent_s`.
    """

    def __init__(
        self,
        windows: list[tuple[str, int]],
        source: Source,
        bus: Bus,
        clock: Clock,
        average: bool,
    ) -> None:
        from argus.capture.app_loopback import CHANNELS, RATE

        label = windows[0][0] if len(windows) == 1 else f"{len(windows)} windows"
        self.device = DeviceInfo(index=-1, name=label, rate=RATE, channels=CHANNELS)
        self.source = source
        self.opened_at = time.monotonic()
        self._readers = [
            _AppReader(title, pid, source, bus, clock, average) for title, pid in windows
        ]

    def start(self) -> None:
        for r in self._readers:
            r.start()

    def stop(self) -> None:
        # Signal everyone at once, then join everyone: otherwise a stuck read in one window (2 s
        # timeout) holds up closing the others, and the total stop time grows with the number of
        # pinned windows instead of a fixed 2 s.
        for r in self._readers:
            r.signal_stop()
        for r in self._readers:
            r.stop()

    @property
    def alive(self) -> bool:
        # Alive while at least one is alive: one application that closed must not silence the
        # audio from the other pinned windows.
        return any(r.alive for r in self._readers)

    @property
    def error(self) -> BaseException | None:
        # As with a device failure: an error of the whole channel is when no live reader is left,
        # not the first one that fell out of many.
        if self.alive:
            return None
        return next((r.error for r in self._readers if r.error is not None), None)

    @property
    def silent_s(self) -> float:
        # Silence of the channel is when all are silent at once; if even one window has just
        # sounded, "no audio" would be untrue.
        if not self._readers:
            return time.monotonic() - self.opened_at
        return min(r.silent_s for r in self._readers)

    @property
    def chunks(self) -> int:
        return sum(r.chunks for r in self._readers)

    @property
    def samples_out(self) -> int:
        return sum(r.samples_out for r in self._readers)

    def failed_windows(self) -> list[str]:
        """Which of the pinned windows are not giving audio right now, for a complaint.

        This is not about the whole channel failing (that is what `alive`/`error` are for): while
        at least one reader is alive, the dead ones do not break the audio, but staying silent
        about them would be a quiet failure of the same kind as the others in this file.
        """
        return [r.window for r in self._readers if not r.alive]


class AudioCapture:
    """Both audio channels at once."""

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        cfg: AudioConfig,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._cfg = cfg
        self._pa: pyaudio.PyAudio | None = None
        self._lock = threading.Lock()
        self.mic: _Channel | None = None
        self.loop: _Channel | None = None

        # The last fresh answer from the system about the default devices. Empty until the first
        # poll returns: we don't know, so we don't complain.
        self._defaults: dict[str, str] = {}
        self._defaults_at = 0.0
        self._probing = False

    def open(self) -> tuple[DeviceInfo, DeviceInfo]:
        """Find the devices and open the streams. Returns what exactly was taken."""
        if pyaudio is None:
            raise DeviceNotFound("Windows audio capture is unavailable on this system")
        with self._lock:
            # The same lock as the device poll, and this is not over-caution. `Pa_Initialize` and
            # `Pa_Terminate` are not protected against races: while the background poll brings up its
            # instance, a second one cannot be brought up here. With different locks they would not see
            # each other, and that crashes the process. Our own `_lock` is not enough here: it is about
            # our channels, while the race is about the library itself.
            try:
                with _PA_LOCK:
                    self._pa = pyaudio.PyAudio()
                mic_dev = find_microphone(self._pa, self._cfg.mic_device)
                loop_dev = find_loopback(self._pa, self._cfg.loopback_device)

                self.mic = _Channel(
                    self._pa, mic_dev, Source.ME, self._bus, self._clock,
                    self._cfg.chunk_ms, self._cfg.downmix_average,
                )
                self.loop = self._open_loopback(loop_dev)
                loop_dev = self.loop.device
                self.mic.start()
                self.loop.start()
            except BaseException:
                # Failed in the middle (no microphone, no speakers, the device is busy): the microphone may
                # have already opened and PortAudio stayed up. After that the application would not start,
                # or the audio would be reopened anew, and all of this would hang around untouched.
                self._release()
                raise

        log.info("microphone: '%s', Hz: %d, channels: %d", mic_dev.name, mic_dev.rate, mic_dev.channels)
        log.info("loopback: '%s', Hz: %d, channels: %d", loop_dev.name, loop_dev.rate, loop_dev.channels)
        return mic_dev, loop_dev

    def _open_loopback(self, device: DeviceInfo):
        """Audio of the pinned windows if there are any; otherwise the whole device.

        A failure here must not leave the channel empty: the person is in the middle of a
        conversation, and "no audio at all" is worse than "audio from the whole device". So we
        fall back to the whole device only when none of the pinned windows could be resolved. If
        at least some were, we listen to them and **say so** about the rest: a silently
        substituted source is later hunted for hours, wondering why music is getting into the
        timeline.
        """
        windows = [w.strip() for w in self._cfg.loopback_windows if w.strip()]
        if not windows:
            self._app_audio_failed = ""
            return _Channel(
                self._pa, device, Source.THEM, self._bus, self._clock,
                self._cfg.chunk_ms, self._cfg.downmix_average,
            )

        from argus.capture.app_loopback import ProcessAudioUnavailable, available, window_pid
        from argus.capture.window import find_window

        def to_device() -> _Channel:
            return _Channel(
                self._pa, device, Source.THEM, self._bus, self._clock,
                self._cfg.chunk_ms, self._cfg.downmix_average,
            )

        ok, why = available()
        if not ok:
            # Failing to resolve any window at once means an old Windows, not a fault of a particular
            # window. Say so once, not once for each pinned window.
            log.warning("app audio is unavailable: %s", why)
            self._app_audio_failed = f"the audio of individual windows is unavailable: {why}"
            return to_device()

        resolved: list[tuple[str, int]] = []
        failed: list[str] = []
        for window in windows:
            try:
                found = find_window(window)
                if found is None:
                    raise ProcessAudioUnavailable(f"window '{window}' not found")
                pid = window_pid(found.hwnd)
                if not pid:
                    raise ProcessAudioUnavailable(f"window '{found.title}' has no process")
                resolved.append((found.title, pid))
            except (ProcessAudioUnavailable, OSError) as e:
                log.warning("app audio '%s' is unavailable: %s", window, e)
                failed.append(friendly.plain_or(e, f"the audio of window '{window}' is unavailable"))

        if not resolved:
            self._app_audio_failed = "; ".join(failed)
            return to_device()

        self._app_audio_failed = (
            f"{'; '.join(failed)} — listening to the other pinned windows" if failed else ""
        )
        return _AppChannel(resolved, Source.THEM, self._bus, self._clock, self._cfg.downmix_average)

    def close(self) -> None:
        with self._lock:
            self._release()

    def _release(self) -> None:
        """Close the channels and shut down PortAudio. The caller holds the `_lock` lock."""
        for ch in (self.mic, self.loop):
            if ch is not None:
                try:
                    ch.stop()
                except Exception:  # one stubborn channel must not leave the second one open
                    log.debug("channel closed with an error", exc_info=True)
        self.mic = self.loop = None
        if self._pa is not None:
            pa, self._pa = self._pa, None
            with _PA_LOCK:
                pa.terminate()

    @property
    def healthy(self) -> bool:
        """Both streams are alive and without errors.

        A wireless headset can drop in the middle of a session; then the stream dies, and that
        must be noticed rather than silently recording silence to the end.
        """
        return all(ch is not None and ch.alive and ch.error is None for ch in (self.mic, self.loop))

    def first_error(self) -> BaseException | None:
        for ch in (self.mic, self.loop):
            if ch is not None and ch.error is not None:
                return ch.error
        return None

    def complaints(self) -> list[str]:
        """What is wrong with the audio, apart from a dead stream.

        The most expensive failure of this project is not a crash but silence on a live stream.
        A wireless headset falls asleep, Windows moves the output to the built-in one, and we
        keep listening to the sleeping device: `alive` says "alive", there are no errors, the
        levels are exactly zero. From outside this is "the program does not hear", and without a
        hint there is nowhere to look for the cause.

        So we compare what we hold with what the system considers current, and say so plainly.
        Devices set by hand in the config are left alone: since the person chose them, "not the
        default" is their decision, not a fault.
        """
        out: list[str] = []
        if getattr(self, "_app_audio_failed", ""):
            out.append(self._app_audio_failed)

        # A window may have closed in the middle of a conversation, after the channel was already
        # open. This is not caught at startup, and `_app_audio_failed` has nothing to do with it.
        # While at least one reader is alive, the channel's `alive` says "alive", and the closed
        # window simply stops giving sound, quietly. Since the person pinned it themselves, we
        # cannot fall back to the device in its place, but we cannot stay silent about the loss
        # either.
        if isinstance(self.loop, _AppChannel):
            dead = self.loop.failed_windows()
            if dead and self.loop.alive:
                out.append(
                    "the audio of window " + ", ".join(f"“{w}”" for w in dead)
                    + " is gone — was the app closed?"
                )

        # We compare with what the system considers current **right now**, not with what it was at
        # startup. The first answer arrives a second after startup; until then we stay silent: we
        # don't know, so we don't complain.
        for ch, what in ((self.mic, "mic"), (self.loop, "system audio")):
            if ch is not None and ch.device.missed:
                out.append(
                    f"the pinned device ({what}) “{ch.device.missed}” wasn't found — "
                    f"listening to “{ch.device.name}”"
                )

        current = self.system_defaults()
        if current:
            # We don't know the device (the system cannot name it: no speakers, no microphone), so we
            # don't complain about it: a message like "Windows plays to ''" (an empty name) tells the person
            # nothing.
            speaker, listening = current.get("out", ""), current.get("in", "")
            # When an application is pinned, there is nothing to compare with: the "device" of such a
            # channel is a window title. Without this caveat a person who chose Chrome would get "system
            # audio is going elsewhere, reconnect" out of nowhere, and
            # would reconnect, losing their choice.
            if (
                speaker
                and self._cfg.loopback_device is None
                and self.loop is not None
                and self.loop.device.index >= 0
                and _norm(speaker) not in _norm(self.loop.device.name)
            ):
                out.append(
                    f"system audio is going elsewhere: listening to “{self.loop.device.name}”, "
                    f"but Windows plays to “{speaker}” — reconnect the audio"
                )
            if (
                listening
                and self._cfg.mic_device is None
                and self.mic is not None
                and _norm(listening) != _norm(self.mic.device.name)
            ):
                out.append(
                    f"listening to the microphone “{self.mic.device.name}”, "
                    f"but the system one is now “{listening}” — pick it or reconnect the audio"
                )

        # An open microphone always makes noise: a flat zero means it is switched off, disconnected,
        # or muted. For the loopback, zero is legitimate: when nothing is playing, WASAPI delivers
        # digital silence, and complaining about it would mean shouting "fault" in the middle of
        # normal operation.
        if self.mic is not None and self.mic.alive and self.mic.silent_s > MIC_SILENCE_S:
            out.append(
                f"microphone “{self.mic.device.name}” has been silent for "
                f"{self.mic.silent_s:.0f} s — check that it's on"
            )
        return out

    def system_defaults(self) -> dict[str, str]:
        """What the system considers the current devices.

        Returns the last known answer and, if it has gone stale, starts a poll in the background.
        Never waits: this method is called from the state broadcast, twice a second, and hanging
        it for fifty milliseconds would make the picture jerk in every open tab.
        """
        # While capture is not open, there is nothing to compare: we hold no devices, and there is
        # nothing to say "we are listening to the wrong thing" about. This also
        # turns off the poll where audio was not brought up at all, for example in tests.
        if self._pa is None:
            return {}

        now = time.monotonic()
        if not self._probing and now - self._defaults_at > DEVICE_POLL_S:
            self._probing = True
            threading.Thread(
                target=self._probe_defaults, name="audio-probe", daemon=True
            ).start()
        return self._defaults

    def _fresh_snapshot(self):
        """A fresh answer from the system about devices; `None` means asking failed.

        The PortAudio of a live capture never has a fresh answer (see `DEVICE_POLL_S`), so we ask
        the system directly. If that fails, we do not guess from the old picture: "we don't know,
        we don't complain" is better than comparing the state at startup with itself.
        """
        try:
            from argus.capture import mmdevice

            return mmdevice.snapshot()
        except Exception:  # devices could have disappeared right during the poll
            log.debug("querying devices from the system failed", exc_info=True)
            return None

    def _probe_defaults(self) -> None:
        try:
            snap = self._fresh_snapshot()
            if snap is not None:
                # What the system cannot name (no speakers, no microphone) is absent from the answer.
                self._defaults = {
                    key: endpoint.name
                    for key, endpoint in (("in", snap.default_in), ("out", snap.default_out))
                    if endpoint is not None
                }
        finally:
            self._defaults_at = time.monotonic()
            self._probing = False

    def inputs(self) -> list[dict]:
        """Microphones there is something to choose from.

        Loopback devices are excluded from here on purpose: they are not microphones but a
        reflection of what plays in the speakers, and listening to them through the "me" channel
        would break the separation of the participants, exactly what this project has two streams
        instead of one for.

        The name matters more than the number. Numbers in Windows drift: plug in a headset and
        the microphone moves from 15 to 20. So we hand out the name, and later search by it.
        """
        # Capture is open, so we ask the system: PortAudio of a live capture would show the devices
        # as they were at startup, and a new instance does not refresh it (reference counting), so a
        # microphone plugged in later would never appear in the list. Such a list has no number or
        # sample rate (nor does the list on Mac): the name and the "system" flag are all that is
        # needed for a choice.
        if self._pa is not None:
            snap = self._fresh_snapshot()
            if snap is not None:
                default = snap.default_in.id if snap.default_in is not None else None
                return [
                    {"name": e.name, "index": -1, "default": e.id == default, "rate": 0}
                    for e in snap.inputs
                ]

        # There is no capture (it did not come up or is closed): a new instance will count the
        # devices afresh by itself.
        with _PA_LOCK:
            pa = pyaudio.PyAudio()
        try:
            api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default = api["defaultInputDevice"]
            out = []
            for d in _wasapi_devices(pa):
                if d["maxInputChannels"] <= 0 or d.get("isLoopbackDevice"):
                    continue
                out.append({
                    "name": d["name"],
                    "index": d["index"],
                    "default": d["index"] == default,
                    "rate": int(d["defaultSampleRate"]),
                })
            return out
        except OSError as e:
            log.warning("could not list microphones: %s", e)
            return []
        finally:
            with _PA_LOCK:
                pa.terminate()

    def reopen(self) -> tuple[DeviceInfo, DeviceInfo]:
        """Choose the devices again and bring up the streams.

        It is needed for exactly the reason the complaints above exist: when the audio has moved
        to another device, fixing that by restarting the whole program means reloading Whisper
        and losing the conversation timeline. Here only the audio is reopened; the bus and
        everything subscribed to it stay in place.
        """
        self.close()
        return self.open()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
