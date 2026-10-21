"""Audio capture on macOS: the microphone and system audio as two independent streams.

The main decision is the same as on Windows (`capture/audio.py`): two streams, not one
mixed stream. The microphone is "me", system audio is "being spoken to"; while they are
separate, the question "who is speaking" is answered for free. Devices are chosen by
name, not by number, and the input is chosen independently of the output.

What differs.

* **The microphone** is opened by PortAudio (`sounddevice`), while the device list and
  "what is the default now" come from CoreAudio (`mac/coreaudio.py`): PortAudio takes
  the list once and does not see a headset plugged in later.
* **System audio** is the `argus-syscap` helper (`mac/syscap.py`): on macOS "what plays
  in the speakers" is not handed out by the system, and ScreenCaptureKit is needed. The
  audio of pinned windows is the same helper with a filter by programs, in one stream
  (Windows runs a process per window; here the system is asked to mix it itself).
* **A virtual device.** If `loopback_device` is set in the settings (BlackHole,
  Loopback and the like), system audio is taken from it as from an ordinary input. This
  is a fallback path for macOS without ScreenCaptureKit.
* **A failure does not crash the launch.** No screen recording permission or an old
  macOS: the app still opens and listens to the microphone, and the reason goes to the
  page's complaints. On Windows with no loopback there is no point in launching; on a
  Mac the first launch almost always has no permission, and refusing to open would be
  the worst first impression.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import numpy as np

from argus.capture.audio import MIC_SILENCE_S, NS_PER_SEC, DeviceInfo, DeviceNotFound
from argus.capture.resample import Resampler
from argus.core import friendly
from argus.core.events import SAMPLE_RATE, AudioChunk, Source
from argus.mac import coreaudio, permissions, syscap

if TYPE_CHECKING:
    from argus.core.bus import Bus
    from argus.core.clock import Clock
    from argus.core.config import AudioConfig

log = logging.getLogger(__name__)

# How often to ask the system which devices are the defaults now. A CoreAudio call is
# cheap but not free, and it is asked twice a second.
DEVICE_POLL_S = 5.0

# Programs that install a virtual loopback device: they must not be used as a microphone.
# Choosing such a device as "Me" would mean listening to system audio with both channels
# and breaking the separation of the interlocutors, which is exactly why there are two
# channels.
LOOPBACK_DRIVERS = ("blackhole", "soundflower", "loopback audio", "ground control")

# A microphone does not need more than two channels: audio interfaces can have 16, while
# mono goes into recognition.
MAX_MIC_CHANNELS = 2

# Temporary PortAudio instances, one at a time: its initialization and shutdown are not
# protected against races (on Windows this crashed the process).
_PA_LOCK = threading.Lock()


def _load_sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as e:
        raise DeviceNotFound("the audio subsystem is unavailable — reinstall the app") from e
    return sounddevice


# --------------------------------------------------------------------------
# Finding devices
# --------------------------------------------------------------------------


def _port_inputs(sd: Any) -> list[dict]:
    """PortAudio inputs together with their numbers (the number is the place in its list)."""
    out = []
    for index, d in enumerate(sd.query_devices()):
        if int(d.get("max_input_channels", 0)) > 0:
            out.append({**dict(d), "index": index})
    return out


def _info(port: dict, missed: str | None = None) -> DeviceInfo:
    rate = int(port.get("default_samplerate") or 0) or 48_000
    channels = max(1, min(int(port["max_input_channels"]), MAX_MIC_CHANNELS))
    return DeviceInfo(
        index=int(port["index"]), name=str(port["name"]), rate=rate, channels=channels,
        missed=missed,
    )


def _is_loopback_driver(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in LOOPBACK_DRIVERS)


def find_microphone(sd: Any, spec: int | str | None) -> DeviceInfo:
    """Microphone: by PortAudio number, by part of the name, or the system default."""
    ports = [p for p in _port_inputs(sd) if not _is_loopback_driver(str(p["name"]))]
    if not ports:
        raise DeviceNotFound("no microphone found")

    if isinstance(spec, int):
        match = next((p for p in ports if p["index"] == spec), None)
        if match is not None:
            return _info(match)
        spec = str(spec)
        return _default_input(ports, missed=spec)
    if isinstance(spec, str) and spec.strip():
        low = spec.strip().lower()
        match = next((p for p in ports if low in str(p["name"]).lower()), None)
        if match is None:
            # We do not die; we take the system one and say so, as on Windows: you pinned headphones,
            # in the morning they did not wake up, and the program must not refuse to come up together
            # with recognition and recording.
            log.warning("pinned microphone '%s' not found - using the system one", spec)
            return _default_input(ports, missed=spec.strip())
        return _info(match)
    return _default_input(ports)


def _default_input(ports: list[dict], missed: str | None = None) -> DeviceInfo:
    live = coreaudio.default_names().get("in", "")
    match = next((p for p in ports if live and str(p["name"]) == live), None)
    return _info(match or ports[0], missed=missed)


def find_loopback_device(sd: Any, spec: int | str) -> DeviceInfo | None:
    """The virtual device given in the settings. `None` means there is none."""
    ports = _port_inputs(sd)
    if isinstance(spec, int):
        match = next((p for p in ports if p["index"] == spec), None)
    else:
        low = str(spec).strip().lower()
        match = next((p for p in ports if low in str(p["name"]).lower()), None)
    return _info(match) if match is not None else None


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


class _Channel:
    """One PortAudio input -> resampler -> bus.

    We leave the callback as fast as possible: stall and the audio gets a hole, and no
    amount of processing can restore it afterwards.
    """

    def __init__(
        self, sd: Any, device: DeviceInfo, source: Source, bus: Bus, clock: Clock,
        chunk_ms: int, average: bool,
    ) -> None:
        self.device = device
        self.source = source
        self._sd = sd
        self._bus = bus
        self._clock = clock
        self._resampler = Resampler(device.rate, device.channels, average)

        self.chunks = 0
        self.samples_out = 0
        self.error: BaseException | None = None
        self.overflows = 0
        # When the channel last had at least one non-zero sample. A live stream and sound actually
        # coming through it are different things, and the second breaks more often (on macOS
        # without microphone permission the stream is alive and delivers pure zeros).
        self.opened_at = time.monotonic()
        self.last_sound = 0.0

        frames = max(1, int(device.rate * chunk_ms / 1000))
        try:
            self._stream = sd.InputStream(
                device=device.index, channels=device.channels, samplerate=device.rate,
                dtype="int16", blocksize=frames, callback=self._on_audio,
            )
        except Exception as e:  # noqa: BLE001 - PortAudio raises its own exceptions
            raise DeviceNotFound(f"microphone '{device.name}' failed to open") from e

    def _on_audio(self, indata, frames, time_info, status) -> None:
        # PortAudio does not show an exception inside the callback: the stream just goes silent.
        # We catch everything and carry it outward so that the watchdog can see it.
        try:
            if getattr(status, "input_overflow", False):
                self.overflows += 1
            samples = self._resampler.process(np.asarray(indata).reshape(-1))
            if len(samples):
                # The time comes from the common clock at the moment the chunk arrives, wound back by its
                # duration; it cannot be computed from the number of samples.
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
        except BaseException as e:  # noqa: BLE001 - otherwise the error would vanish without a trace
            self.error = e
            abort = getattr(self._sd, "CallbackAbort", None)
            if abort is not None:
                raise abort from e

    @property
    def alive(self) -> bool:
        try:
            return bool(self._stream.active)
        except Exception:  # noqa: BLE001
            return False

    @property
    def silent_s(self) -> float:
        """How many seconds in a row the channel is exactly zero (until the first sound, counted from
        opening).
        """
        return time.monotonic() - (self.last_sound or self.opened_at)

    def start(self) -> None:
        try:
            self._stream.start()
        except Exception as e:  # noqa: BLE001 - PortAudio raises its own exceptions
            raise DeviceNotFound(f"microphone '{self.device.name}' failed to start") from e

    def stop(self) -> None:
        try:
            if self._stream.active:
                self._stream.stop()
            self._stream.close()
        except Exception as e:  # noqa: BLE001
            log.debug("stream %s: closed with an error: %s", self.source.value, e)


class _HelperChannel:
    """System audio (or the audio of pinned programs) from the `argus-syscap` helper."""

    def __init__(
        self, label: str, pids: list[int], source: Source, bus: Bus, clock: Clock, average: bool,
        helper: syscap.HelperProcess | None = None,
    ) -> None:
        self.device = DeviceInfo(index=-1, name=label, rate=syscap.RATE, channels=syscap.CHANNELS)
        self.source = source
        self._bus = bus
        self._clock = clock
        self._resampler = Resampler(syscap.RATE, syscap.CHANNELS, average)
        # Our own sound (voicing of answers) must not get into the "system sound": otherwise
        # recognition would hear its own voice and answer it.
        self._helper = helper or syscap.HelperProcess(pids=pids, exclude_pids=[os.getpid()])
        self.pids = list(pids)
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

        self.chunks = 0
        self.samples_out = 0
        self.overflows = 0
        self.opened_at = time.monotonic()
        self.last_sound = 0.0
        self._ended: BaseException | None = None

    def start(self) -> None:
        self._helper.start()
        self._thread = threading.Thread(target=self._run, name="syscap-audio", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        pending = b""
        frame = syscap.BYTES_PER_FRAME
        try:
            while not self._stopped.is_set():
                data = self._helper.read()
                if not data:
                    break
                pending += data
                usable = len(pending) - len(pending) % frame
                if usable:
                    raw = np.frombuffer(pending[:usable], dtype="<i2")
                    pending = pending[usable:]
                    self._publish(raw)
        except BaseException as e:  # noqa: BLE001
            self._ended = e
            return
        if not self._stopped.is_set():
            self._ended = syscap.SystemAudioUnavailable(
                self._helper.error_text or "system audio stopped"
            )

    def _publish(self, raw: np.ndarray) -> None:
        samples = self._resampler.process(raw)
        if not len(samples):
            return
        duration = len(samples) * NS_PER_SEC // SAMPLE_RATE
        self._bus.publish_threadsafe(
            AudioChunk(
                t=max(0, self._clock.now() - duration), source=self.source, samples=samples
            )
        )
        self.chunks += 1
        self.samples_out += len(samples)
        if samples.any():
            self.last_sound = time.monotonic()

    @property
    def error(self) -> BaseException | None:
        if self._ended is not None:
            return self._ended
        if self._helper.state == syscap.FAILED:
            return syscap.SystemAudioUnavailable(self._helper.error_text or "system audio stopped")
        return None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive()) and self.error is None

    @property
    def silent_s(self) -> float:
        return time.monotonic() - (self.last_sound or self.opened_at)

    @property
    def helper_state(self) -> str:
        return self._helper.state

    def dead_pids(self) -> list[int]:
        """Pinned programs that no longer exist. The helper says nothing about this: the stream remains,
        but there is no sound.
        """
        # macOS only: `os.kill(pid, 0)` on Windows does not check the process, it terminates it.
        if sys.platform == "win32":
            return []
        gone = []
        for pid in self.pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone.append(pid)
            except OSError:
                pass            # someone else's process that we have no right to check, so it exists
        return gone

    def stop(self) -> None:
        self._stopped.set()
        self._helper.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


class _DeadChannel:
    """There is no system audio, but the app must stay alive: a stub channel with a reason.

    From outside it is indistinguishable from a real one: `alive` is false, `error` carries
    words for a person, and the page shows them in the complaints.
    """

    def __init__(self, label: str, reason: str, source: Source) -> None:
        self.device = DeviceInfo(index=-1, name=label, rate=syscap.RATE, channels=syscap.CHANNELS)
        self.source = source
        self.error: BaseException | None = syscap.SystemAudioUnavailable(reason)
        self.chunks = 0
        self.samples_out = 0
        self.overflows = 0
        self.opened_at = time.monotonic()
        self.last_sound = 0.0
        self.alive = False

    @property
    def silent_s(self) -> float:
        return time.monotonic() - self.opened_at

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


# --------------------------------------------------------------------------
# The capture as a whole
# --------------------------------------------------------------------------


class MacAudioCapture:
    """Both audio channels at once: the same set of methods as `capture.audio.AudioCapture`."""

    def __init__(
        self, bus: Bus, clock: Clock, cfg: AudioConfig, sd: Any | None = None
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._cfg = cfg
        self._sd_override = sd
        self._lock = threading.Lock()
        self.mic: _Channel | None = None
        self.loop: _Channel | _HelperChannel | _DeadChannel | None = None
        self._opened = False
        self._app_audio_failed = ""

        self._defaults: dict[str, str] = {}
        self._defaults_at = 0.0
        self._probing = False
        self._perm_at = 0.0
        self._perm: dict[str, str] = {}

    # -- open / close --------------------------------------------------------

    def _sd(self) -> Any:
        return self._sd_override if self._sd_override is not None else _load_sounddevice()

    def open(self) -> tuple[DeviceInfo, DeviceInfo]:
        """Find the devices and open the streams. Returns what exactly was taken."""
        with self._lock:
            sd = self._sd()
            if permissions.microphone() == permissions.NOT_DETERMINED:
                permissions.request_microphone()

            mic_dev = find_microphone(sd, self._cfg.mic_device)
            self.mic = _Channel(
                sd, mic_dev, Source.ME, self._bus, self._clock,
                self._cfg.chunk_ms, self._cfg.downmix_average,
            )
            self.loop = self._open_loop(sd)
            self.mic.start()
            try:
                self.loop.start()
            except syscap.SystemAudioUnavailable as e:
                # The helper did not start (the file is missing or not executable): the app lives on
                # without system audio, and the reason goes to the complaints, as with the other failures.
                log.warning("system audio did not start: %s", e)
                self.loop = _DeadChannel("System audio (unavailable)", str(e), Source.THEM)
            self._opened = True

        loop_dev = self.loop.device
        log.info("microphone: '%s', Hz: %d, channels: %d", mic_dev.name, mic_dev.rate, mic_dev.channels)
        log.info("system audio: '%s', Hz: %d, channels: %d", loop_dev.name, loop_dev.rate, loop_dev.channels)
        return mic_dev, loop_dev

    def _open_loop(self, sd: Any):
        """System audio: a virtual device, the audio of pinned windows, or the whole system."""
        self._app_audio_failed = ""
        spec = self._cfg.loopback_device
        missed: str | None = None
        if spec not in (None, ""):
            device = find_loopback_device(sd, spec)
            if device is not None:
                return _Channel(
                    sd, device, Source.THEM, self._bus, self._clock,
                    self._cfg.chunk_ms, self._cfg.downmix_average,
                )
            missed = str(spec)
            log.warning("pinned device '%s' not found - using system audio", spec)

        try:
            syscap.check_available()
        except syscap.SystemAudioUnavailable as e:
            log.warning("system audio is unavailable: %s", e)
            channel = _DeadChannel("System audio (unavailable)", str(e), Source.THEM)
            channel.device.missed = missed
            return channel

        pids, label = self._pinned_pids()
        channel = _HelperChannel(
            label, pids, Source.THEM, self._bus, self._clock, self._cfg.downmix_average
        )
        channel.device.missed = missed
        return channel

    def _pinned_pids(self) -> tuple[list[int], str]:
        """Programs whose audio counts as "system audio". Empty means the whole system."""
        titles = [t.strip() for t in self._cfg.loopback_windows if t.strip()]
        if not titles:
            return [], "System audio"

        from argus.mac import windows

        resolved: list[tuple[str, int]] = []
        failed: list[str] = []
        for title in titles:
            found = windows.find_window(title)
            if found is None or not found.pid:
                log.warning("app audio '%s' is unavailable: window not found", title)
                failed.append(f"window '{title}' not found")
                continue
            resolved.append((found.title, found.pid))

        if not resolved:
            self._app_audio_failed = "; ".join(failed)
            return [], "System audio"

        self._app_audio_failed = (
            f"{'; '.join(failed)} — listening to the other pinned windows" if failed else ""
        )
        label = resolved[0][0] if len(resolved) == 1 else f"{len(resolved)} windows"
        pids = sorted({pid for _title, pid in resolved})
        return pids, label

    def close(self) -> None:
        with self._lock:
            for channel in (self.mic, self.loop):
                if channel is not None:
                    channel.stop()
            self.mic = self.loop = None
            self._opened = False

    def reopen(self) -> tuple[DeviceInfo, DeviceInfo]:
        """Choose the devices again and bring the streams up.

        We refresh the PortAudio list here too: while there are no streams it is safe, and
        otherwise it would never see a headset plugged in later.
        """
        self.close()
        self._reload_portaudio()
        return self.open()

    def _reload_portaudio(self) -> None:
        if self._sd_override is not None:
            return
        try:
            sd = _load_sounddevice()
            with _PA_LOCK:
                sd._terminate()
                sd._initialize()
        except Exception as e:  # noqa: BLE001 - an old list is better than a crash
            log.debug("PortAudio did not re-read the devices: %s", e)

    # -- state ----------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        """Both streams are alive and without errors."""
        return all(ch is not None and ch.alive and ch.error is None for ch in (self.mic, self.loop))

    def first_error(self) -> BaseException | None:
        for channel in (self.mic, self.loop):
            if channel is not None and channel.error is not None:
                return channel.error
        return None

    def _permissions(self) -> dict[str, str]:
        """Permissions, no more than once per `DEVICE_POLL_S`: the call is a system one, and it is asked
        twice a second.
        """
        now = time.monotonic()
        if not self._perm or now - self._perm_at > DEVICE_POLL_S:
            self._perm = {
                "microphone": permissions.microphone(),
                "screen": permissions.screen_recording(),
            }
            self._perm_at = now
        return self._perm

    def complaints(self) -> list[str]:
        """What is wrong with the audio, other than a crashed stream.

        The most expensive failure in the project is not a crash but silence on a live stream.
        On macOS its main cause is a permission that was not granted: the stream is open, there
        are no errors, the levels are exactly zero. So we check the permission directly and say
        it in words.
        """
        out: list[str] = []
        if self._app_audio_failed:
            out.append(self._app_audio_failed)

        for channel, what in ((self.mic, "mic"), (self.loop, "system sound")):
            if channel is not None and channel.device.missed:
                out.append(
                    f"the pinned device ({what}) “{channel.device.missed}” wasn't found — "
                    f"listening to “{channel.device.name}”"
                )

        if not self._opened:
            return out

        perms = self._permissions()
        mic_advice = permissions.advice("microphone", perms.get("microphone", permissions.UNKNOWN))
        if mic_advice:
            out.append(mic_advice)

        # The pinned program closed in the middle of a conversation: the stream is alive, there is
        # no sound, a silent failure of the same kind as the others. Going back to the audio of
        # the whole system instead of it is not allowed (the person chose it themselves), but
        # keeping quiet about the loss is not allowed either.
        if isinstance(self.loop, _HelperChannel) and self.loop.pids and self.loop.dead_pids():
            out.append("the pinned program's audio is gone — was it closed?")

        # The screen recording permission is needed by both the system audio and the picture:
        # without it the screenshot silently consists of wallpaper alone. We always mention it; if
        # the helper has already failed precisely because of it, the same words stand in the
        # "audio: …" line, and we do not repeat them.
        screen_advice = permissions.advice("screen", perms.get("screen", permissions.UNKNOWN))
        if screen_advice and (self.loop is None or self.loop.error is None):
            out.append(screen_advice)

        current = self.system_defaults()
        if (
            current
            and self._cfg.mic_device in (None, "")
            and self.mic is not None
            and current.get("in")
            and current["in"] != self.mic.device.name
        ):
            out.append(
                f"listening to the microphone “{self.mic.device.name}”, "
                f"but the system one is now “{current['in']}” — pick it or reconnect the audio"
            )

        # An open microphone is always noisy: an even zero means it is switched off, disconnected
        # or without permission (the permission was mentioned above, we do not repeat it).
        if (
            self.mic is not None and self.mic.alive and not mic_advice
            and self.mic.silent_s > MIC_SILENCE_S
        ):
            out.append(
                f"microphone “{self.mic.device.name}” has been silent for "
                f"{self.mic.silent_s:.0f} s — check that it's on"
            )
        return out

    def system_defaults(self) -> dict[str, str]:
        """What the system considers the current devices. Never waits.

        It returns the last known answer and, if it has gone stale, starts a poll in the
        background: this method is called by the state broadcast twice a second, and hanging it
        would make the picture jerk in every open tab.
        """
        if not self._opened:
            return {}
        now = time.monotonic()
        if not self._probing and now - self._defaults_at > DEVICE_POLL_S:
            self._probing = True
            threading.Thread(target=self._probe_defaults, name="audio-probe", daemon=True).start()
        return self._defaults

    def _probe_defaults(self) -> None:
        try:
            self._defaults = coreaudio.default_names()
        except Exception:  # noqa: BLE001 - the devices could have disappeared right during the poll
            log.debug("querying devices failed", exc_info=True)
        finally:
            self._defaults_at = time.monotonic()
            self._probing = False

    def inputs(self) -> list[dict]:
        """The microphones there is something to choose from, asked again every time.

        Loopback devices are excluded on purpose: listening to them through the "me" channel
        would break the separation of the interlocutors. The name matters more than the number:
        we hand the name outward, and look the device up by it later.
        """
        try:
            found = [d for d in coreaudio.inputs() if not _is_loopback_driver(d.name)]
        except Exception as e:  # noqa: BLE001
            log.warning("could not list microphones: %s", e)
            return []

        index_of: dict[str, int] = {}
        if self._opened or self._sd_override is not None:
            try:
                with _PA_LOCK:        # while `reopen` restarts PortAudio, the list must not be requested
                    ports = _port_inputs(self._sd())
                for port in ports:
                    index_of.setdefault(str(port["name"]), int(port["index"]))
            except Exception:  # noqa: BLE001 - the number is needed only for display
                log.debug("cannot get PortAudio device numbers", exc_info=True)

        return [
            {
                "name": d.name,
                "index": index_of.get(d.name, -1),
                "default": d.default_input,
                "rate": d.rate,
            }
            for d in found
        ]

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def describe_error(error: BaseException | None) -> str:
    """Words for a person about an audio error (for `doctor` and for the console)."""
    return friendly.plain_or(error, "stream stopped")
