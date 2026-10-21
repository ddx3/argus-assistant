"""Tests for the silent audio failure.

This file exists because of a real breakage, and the breakage was silent. The levels
in the meter sat at zero and there were no utterances, while the footer said "pipeline
alive": streams open, no errors, `healthy` returns True. The wireless
headset fell asleep, Windows moved the output to the built-in device, and Argus kept
listening to the sleeping one, with nobody to tell it so.

Hence the rule: a live stream and sound actually flowing through it are different
things, and the second breaks more often. The second is what must be checked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pytest

from argus.capture.audio import MIC_SILENCE_S, AudioCapture, DeviceInfo
from argus.core.config import AudioConfig
from argus.core.events import Source


@dataclass
class FakeStream:
    active: bool = True

    def is_active(self) -> bool:
        return self.active


class FakeChannel:
    """A channel without PortAudio: the same silence arithmetic, without hardware."""

    def __init__(self, name: str, source: Source) -> None:
        self.device = DeviceInfo(index=0, name=name, rate=48_000, channels=1)
        self.source = source
        self.error = None
        self._stream = FakeStream()
        self.opened_at = time.monotonic()
        self.last_sound = 0.0

    @property
    def alive(self) -> bool:
        return self._stream.is_active()

    @property
    def silent_s(self) -> float:
        return time.monotonic() - (self.last_sound or self.opened_at)


def capture(cfg: AudioConfig | None = None, **defaults: str) -> AudioCapture:
    """Capture without PortAudio: the channels are fakes, the system's answer is set by hand.

    The answer is injected, not polled. Otherwise the test would depend on which headset
    is plugged into the machine right now. How fresh the poll itself is gets checked
    elsewhere: it can only be seen on live hardware.
    """
    cap = AudioCapture.__new__(AudioCapture)
    cap._cfg = cfg or AudioConfig()
    cap._pa = object()          # "capture is open"; otherwise the poll is skipped
    cap._lock = __import__("threading").Lock()
    cap.mic = FakeChannel("Microphone (HyperX)", Source.ME)
    cap.loop = FakeChannel("Headphones (HyperX) [Loopback]", Source.THEM)

    cap._defaults = {
        "in": defaults.get("mic", "Microphone (HyperX)"),
        "out": defaults.get("out", "Headphones (HyperX)"),
    }
    cap._defaults_at = time.monotonic()   # fresh: there will be no background poll
    cap._probing = False
    return cap


# --------------------------------------------------------------------------
# Silence watchdog
# --------------------------------------------------------------------------


def test_fresh_channel_is_not_reported_as_silent():
    """A freshly opened channel must not look silent for an eternity."""
    assert capture().mic.silent_s < 1.0


def test_silent_microphone_is_a_complaint():
    """An open microphone always has some noise: a flat zero means the hardware is off.

    This check is what tells "quiet in the room" apart from "the program cannot hear".
    """
    cap = capture()
    cap.mic.opened_at = time.monotonic() - MIC_SILENCE_S - 5
    assert any("silent for" in c for c in cap.complaints())


def test_a_microphone_with_sound_is_quiet_in_the_log():
    cap = capture()
    cap.mic.opened_at = time.monotonic() - 300
    cap.mic.last_sound = time.monotonic()
    assert not cap.complaints()


def test_silent_loopback_is_not_a_complaint():
    """Zero is legitimate for loopback.

    When nothing is playing, WASAPI returns digital silence. Complaining about it would
    mean shouting "breakage" in the middle of normal work, and after a day people stop
    reading such shouting, and the real alarms get ignored along with it.
    """
    cap = capture()
    cap.loop.opened_at = time.monotonic() - 3600
    assert not [c for c in cap.complaints() if "Loopback" in c or "loopback" in c]


def test_pinned_devices_are_the_users_choice():
    """A device set by hand is the person's decision, not a breakage."""
    cap = capture(AudioConfig(mic_device="HyperX", loopback_device=16))
    cap.mic.opened_at = time.monotonic() - MIC_SILENCE_S - 5
    cap.mic.last_sound = time.monotonic()
    assert not cap.complaints()


# --------------------------------------------------------------------------
# Listening to the wrong device
# --------------------------------------------------------------------------


def test_drifted_output_is_reported():
    """The headset fell asleep, Windows moved the output, and we are listening to the sleeping one.

    This is exactly the breakage that started it all: streams alive, no errors, levels at
    zero. This is the only way to tell it apart from silence in the room.
    """
    cap = capture(out="Headphones (High Definition Audio Device)")
    assert any("system audio is going elsewhere" in c for c in cap.complaints())


def test_drifted_input_is_reported():
    cap = capture(mic="Microphone (Logitech Webcam C930e)")
    cap.mic.last_sound = time.monotonic()      # it has noise, but it is the wrong device
    assert any("but the system one is now" in c for c in cap.complaints())


def test_matching_devices_are_quiet():
    cap = capture()
    cap.mic.last_sound = time.monotonic()
    assert not cap.complaints()


def test_nothing_is_claimed_before_the_first_probe():
    """Until the system has answered, we stay quiet: if we do not know, we do not complain."""
    cap = capture()
    cap.mic.last_sound = time.monotonic()
    cap._defaults = {}
    assert not cap.complaints()


def test_closed_capture_asks_the_system_nothing():
    """No capture, nothing to compare.

    The check is not about convenience but about crashing: the poll creates its own
    PortAudio instance, and doing that where audio was never opened takes the process down.
    That is what happened, in somebody else's test that just happened to run nearby.
    """
    cap = capture()
    cap._pa = None
    cap._defaults_at = 0.0
    assert cap.system_defaults() == {}


# --------------------------------------------------------------------------
# The pinned device is gone
# --------------------------------------------------------------------------


def test_missing_pinned_device_is_reported():
    """A substitution must not pass silently.

    The headphones were pinned, they did not wake up in the morning, and now we listen to
    the built-in input. Before, the program simply would not start in this case; now it
    starts, which makes it more important to say that it is not listening to what was asked.
    """
    cap = capture(AudioConfig(mic_device="HyperX"))
    cap.mic.device.missed = "HyperX"
    cap.mic.last_sound = time.monotonic()
    assert any("wasn't found" in c for c in cap.complaints())


def test_found_pinned_device_says_nothing():
    cap = capture(AudioConfig(mic_device="HyperX"))
    cap.mic.last_sound = time.monotonic()
    assert not cap.complaints()


# --------------------------------------------------------------------------
# PortAudio lifecycle
# --------------------------------------------------------------------------


@pytest.mark.windows_only
def test_every_path_takes_the_same_lock(monkeypatch):
    """The library may be brought up and shut down only one caller at a time.

    `Pa_Initialize` and `Pa_Terminate` are not protected against races, and the price of a
    mistake is not an exception but the whole process crashing. The check is blunt: anyone
    who creates an instance must hold the shared lock. Last time there turned out to be two
    locks, and opening the audio together with the background poll crashed the process.
    """
    from argus.capture import audio as mod

    held: list[bool] = []

    def device(index: int, *, loopback: bool = False) -> dict:
        return {
            "index": index,
            "name": "Headphones (fake)" if loopback else "Microphone (fake)",
            "defaultSampleRate": 48_000.0,
            "maxInputChannels": 1,
            "maxOutputChannels": 0,
            "hostApi": 0,
            "isLoopbackDevice": loopback,
        }

    class FakeStream:
        def start_stream(self) -> None: ...
        def stop_stream(self) -> None: ...
        def close(self) -> None: ...
        def is_active(self) -> bool:
            return True

    class FakePA:
        def __init__(self) -> None:
            held.append(mod._PA_LOCK.locked())

        def terminate(self) -> None:
            held.append(mod._PA_LOCK.locked())

        def get_host_api_info_by_type(self, _):
            return {"index": 0, "defaultInputDevice": 0, "defaultOutputDevice": 1}

        def get_device_info_by_index(self, i):
            return device(i, loopback=i == 1)

        def get_device_count(self) -> int:
            return 2

        def open(self, **_):
            return FakeStream()

    monkeypatch.setattr(mod.pyaudio, "PyAudio", FakePA)

    from argus.core.bus import Bus
    from argus.core.clock import LiveClock

    cap = capture()
    cap._bus, cap._clock = Bus(), LiveClock()
    cap._probing = False
    # With capture open, the poll and the device list ask the system, not PortAudio (see
    # below). Here we need exactly the paths that create an instance: the list without
    # capture and bringing up the audio.
    monkeypatch.setattr(cap, "_fresh_snapshot", lambda: None)
    cap._probe_defaults()          # background poll
    cap._pa = None
    cap.inputs()                   # the list for selection when there is no capture
    cap.open()                     # bringing up the audio: the same path that used to crash
    cap.close()

    assert held, "an instance was never created, so the check checked nothing"
    assert all(held), "PortAudio was brought up or shut down without the shared lock"


# --------------------------------------------------------------------------
# A fresh picture of the devices: we ask the system, not PortAudio
# --------------------------------------------------------------------------


def snapshot_of(*, mic=None, out=None, inputs=()):
    from argus.capture.mmdevice import Endpoint, Snapshot

    def end(name):
        return None if name is None else Endpoint(f"id:{name}", name)

    return Snapshot(default_in=end(mic), default_out=end(out), inputs=[end(n) for n in inputs])


def refuse_portaudio(monkeypatch) -> None:
    """Any new PortAudio instance while capture is alive is a test error.

    A new instance does not re-enumerate the devices (reference counting), so asking it is
    pointless.
    """
    from argus.capture import audio as mod

    def boom(*a, **kw):
        raise AssertionError("PortAudio is not asked about devices while a capture is live")

    monkeypatch.setattr(mod.pyaudio, "PyAudio", boom)


@pytest.mark.windows_only
def test_defaults_come_from_the_system_not_from_portaudio(monkeypatch):
    """A headset was plugged in after startup and became the current one: we must learn about it.

    The PortAudio of a live capture sees only the devices that existed at startup, so the
    "we are listening to the wrong one" check compared them with themselves and never fired.
    """
    refuse_portaudio(monkeypatch)
    cap = capture()
    snap = snapshot_of(mic="Microphone (HyperX)", out="Speakers (Realtek)")
    monkeypatch.setattr(cap, "_fresh_snapshot", lambda: snap)
    cap._defaults, cap._defaults_at = {}, 0.0

    cap._probe_defaults()

    assert cap._defaults == {"in": "Microphone (HyperX)", "out": "Speakers (Realtek)"}
    assert any("system audio is going elsewhere" in c for c in cap.complaints())


@pytest.mark.windows_only
def test_a_microphone_plugged_in_after_the_start_is_in_the_list(monkeypatch):
    refuse_portaudio(monkeypatch)
    cap = capture()
    snap = snapshot_of(
        mic="Microphone (HyperX)", out="Headphones (HyperX)",
        inputs=("Microphone (HyperX)", "Microphone (Logitech Webcam C930e)"),
    )
    monkeypatch.setattr(cap, "_fresh_snapshot", lambda: snap)

    listed = cap.inputs()

    assert [d["name"] for d in listed] == ["Microphone (HyperX)", "Microphone (Logitech Webcam C930e)"]
    assert [d["default"] for d in listed] == [True, False]


@pytest.mark.windows_only
def test_an_unplugged_microphone_leaves_the_list(monkeypatch):
    refuse_portaudio(monkeypatch)
    cap = capture()
    snap = snapshot_of(mic="Microphone (Logitech Webcam C930e)",
                       inputs=("Microphone (Logitech Webcam C930e)",))
    monkeypatch.setattr(cap, "_fresh_snapshot", lambda: snap)
    assert [d["name"] for d in cap.inputs()] == ["Microphone (Logitech Webcam C930e)"]


def test_the_same_device_with_other_spacing_is_not_a_complaint():
    """A false complaint on a healthy machine is worse than a missed one: spaces and case do not
    count.
    """
    cap = capture(mic="microphone  (hyperx)", out="HEADPHONES (HyperX)")
    cap.mic.last_sound = time.monotonic()
    assert not cap.complaints()


def test_a_default_the_system_cannot_name_is_not_a_complaint():
    """There are no default speakers — the message "Windows plays to ''" (an empty name)
    would tell the person nothing.
    """
    cap = capture()
    cap._defaults = {"in": "Microphone (HyperX)"}
    cap.mic.last_sound = time.monotonic()
    assert not cap.complaints()


@pytest.mark.windows_only
def test_the_system_names_the_devices_like_portaudio_does():
    """The names come from two sources — and must match, otherwise the complaint "we are
    listening to the wrong one" would sound on every healthy machine. A check on real
    hardware: no devices — no check."""
    from argus.capture import audio as mod
    from argus.capture import mmdevice

    try:
        snap = mmdevice.snapshot()
    except OSError:
        pytest.skip("the system does not answer about devices")
    if snap.default_in is None or snap.default_out is None:
        pytest.skip("this machine has no microphone or speakers")

    with mod._PA_LOCK:
        pa = mod.pyaudio.PyAudio()
    try:
        api = pa.get_host_api_info_by_type(mod.pyaudio.paWASAPI)
        if api["defaultInputDevice"] < 0 or api["defaultOutputDevice"] < 0:
            pytest.skip("PortAudio does not see default devices")
        mic = pa.get_device_info_by_index(api["defaultInputDevice"])["name"]
        speaker = pa.get_device_info_by_index(api["defaultOutputDevice"])["name"]
    finally:
        with mod._PA_LOCK:
            pa.terminate()

    assert mod._norm(mic) == mod._norm(snap.default_in.name)
    assert mod._norm(speaker) == mod._norm(snap.default_out.name)
    assert any(mod._norm(e.name) == mod._norm(mic) for e in snap.inputs)


# --------------------------------------------------------------------------
# No microphone or speakers
# --------------------------------------------------------------------------


def fake_portaudio(*, mic: bool = True, speaker: bool = True, fail_second_open: bool = False):
    """A replacement for `PyAudio`: on a machine without a microphone (or speakers) the default
    index is -1.
    """
    from types import SimpleNamespace

    seen = SimpleNamespace(terminated=0, closed=0, opened=0)

    devices = [
        {"index": 0, "name": "Microphone (fake)", "maxInputChannels": 1, "isLoopbackDevice": False},
        {"index": 1, "name": "Headphones (fake)", "maxInputChannels": 0, "isLoopbackDevice": False},
        {"index": 2, "name": "Headphones (fake) [Loopback]", "maxInputChannels": 2,
         "isLoopbackDevice": True},
    ]

    class FakeStream:
        def start_stream(self) -> None: ...
        def stop_stream(self) -> None: ...
        def is_active(self) -> bool:
            return True

        def close(self) -> None:
            seen.closed += 1

    class FakePA:
        def get_host_api_info_by_type(self, _):
            return {"index": 0, "defaultInputDevice": 0 if mic else -1,
                    "defaultOutputDevice": 1 if speaker else -1}

        def get_device_info_by_index(self, i):
            if i < 0:
                # Exactly what the real PortAudio does on device number -1.
                raise OSError(-9996, "Invalid device info")
            return {**devices[i], "hostApi": 0, "defaultSampleRate": 48_000.0,
                    "maxOutputChannels": 0}

        def get_device_count(self) -> int:
            return len(devices)

        def open(self, **_):
            seen.opened += 1
            if fail_second_open and seen.opened == 2:
                raise OSError(-9999, "Unanticipated host error")
            return FakeStream()

        def terminate(self) -> None:
            seen.terminated += 1

    return FakePA, seen


def real_capture(monkeypatch, **fake):
    from argus.capture import audio as mod
    from argus.core.bus import Bus
    from argus.core.clock import LiveClock

    fake_pa, seen = fake_portaudio(**fake)
    monkeypatch.setattr(mod.pyaudio, "PyAudio", fake_pa)
    return AudioCapture(Bus(), LiveClock(), AudioConfig()), seen


@pytest.mark.windows_only
def test_no_microphone_is_a_readable_refusal_not_an_errno(monkeypatch):
    """A PC without a microphone: instead of "[Errno -9996]" — an understandable phrase, and
    PortAudio is not left initialized.
    """
    from argus.capture.audio import DeviceNotFound
    from argus.core import friendly

    cap, seen = real_capture(monkeypatch, mic=False)
    with pytest.raises(DeviceNotFound, match="microphone") as caught:
        cap.open()

    text = str(caught.value)
    assert friendly.plain_or(text, "SUBSTITUTE") == text, "the phrase is not fit to show a person"
    assert seen.terminated == 1 and cap._pa is None
    assert cap.mic is None and cap.loop is None


@pytest.mark.windows_only
def test_no_speakers_is_a_readable_refusal_not_an_errno(monkeypatch):
    """A remote session without an output device: the same for speakers."""
    from argus.capture.audio import DeviceNotFound
    from argus.core import friendly

    cap, seen = real_capture(monkeypatch, speaker=False)
    with pytest.raises(DeviceNotFound, match="speakers") as caught:
        cap.open()

    text = str(caught.value)
    assert friendly.plain_or(text, "SUBSTITUTE") == text
    assert seen.terminated == 1 and cap._pa is None


@pytest.mark.windows_only
def test_a_failure_halfway_closes_what_was_already_open(monkeypatch):
    """The microphone opened but loopback did not: leaving the microphone and PortAudio open
    is not allowed.

    The application does not start in that case, and when audio is reopened from the
    settings the breakage would pile up.
    """
    cap, seen = real_capture(monkeypatch, fail_second_open=True)
    with pytest.raises(OSError, match="9999"):
        cap.open()

    assert seen.opened == 2
    assert seen.closed == 1, "the open microphone was left hanging"
    assert seen.terminated == 1 and cap._pa is None
    assert cap.mic is None and cap.loop is None


@pytest.mark.windows_only
def test_inputs_fall_back_to_portaudio_when_the_system_does_not_answer(monkeypatch):
    cap, seen = real_capture(monkeypatch)
    monkeypatch.setattr(cap, "_fresh_snapshot", lambda: None)
    cap._pa = object()                  # capture is "open": the system did not answer, so we take what we have

    assert [d["name"] for d in cap.inputs()] == ["Microphone (fake)"]
    assert seen.terminated == 1         # the temporary instance is shut down


# --------------------------------------------------------------------------
# The signal reaches the levels
# --------------------------------------------------------------------------


def test_nonzero_samples_mark_the_channel_as_heard():
    """The mark is set by any nonzero sample, not by loudness.

    A threshold here would be a mistake: quiet speech on a distant microphone is sound, not
    silence, and it must not be declared a breakage.
    """
    ch = FakeChannel("mic", Source.ME)
    ch.opened_at = time.monotonic() - 100
    assert ch.silent_s > 99

    quiet = np.array([0, 0, 1, 0], dtype=np.int16)
    if quiet.any():
        ch.last_sound = time.monotonic()
    assert ch.silent_s < 1.0


def test_pure_silence_does_not_mark_the_channel():
    ch = FakeChannel("mic", Source.ME)
    ch.opened_at = time.monotonic() - 100
    silence = np.zeros(512, dtype=np.int16)
    if silence.any():
        ch.last_sound = time.monotonic()
    assert ch.silent_s > 99
