"""Audio on macOS: the logic against fake system layers.

There is no real CoreAudio, PortAudio or ScreenCaptureKit here: the tests run on
Windows. So everything system-level is faked: the device list, the microphone stream, and
even the helper itself (`argus-syscap` is replaced by a small Python script that speaks
the same contract: events on stderr, sound on stdout). This checks what does not depend
on the system: finding devices by name, pipes and streams, event parsing, complaints in
plain words, and refusing without crashing the startup. What the system really returns
is checked by CI on macOS and by a live run (`docs/macos.md`).
"""

from __future__ import annotations

import struct
import sys
import threading
import time

import numpy as np
import pytest

from argus.capture.audio import DeviceInfo, DeviceNotFound
from argus.core import friendly
from argus.core.config import AudioConfig
from argus.core.events import AudioChunk, Source
from argus.mac import audio as mac_audio
from argus.mac import coreaudio, permissions, syscap


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class FakeBus:
    def __init__(self) -> None:
        self.events: list = []
        self._lock = threading.Lock()

    def publish_threadsafe(self, event) -> None:
        with self._lock:
            self.events.append(event)

    def chunks(self, source: Source | None = None) -> list[AudioChunk]:
        with self._lock:
            return [e for e in self.events if isinstance(e, AudioChunk) and (source is None or e.source == source)]


class FakeClock:
    def now(self) -> int:
        return int(time.monotonic() * 1e9)


# --------------------------------------------------------------------------
# CoreAudio: parsing what the system returns
# --------------------------------------------------------------------------


def fourcc_bytes(n: int) -> bytes:
    return struct.pack("<I", n)


def buffer_list(*channels: int) -> bytes:
    """An `AudioBufferList` as a 64-bit system lays it out: the count, the alignment, structures of
    16 bytes.
    """
    raw = struct.pack("<II", len(channels), 0)
    for ch in channels:
        raw += struct.pack("<IIQ", ch, 4096, 0)
    return raw


class FakeCoreAudio:
    """Three devices: the built-in microphone, the speakers and a virtual loopback."""

    MIC, SPK, HOLE = 41, 42, 43

    def __init__(self) -> None:
        self.names = {self.MIC: "MacBook Pro Microphone", self.SPK: "MacBook Pro Speakers",
                      self.HOLE: "BlackHole 2ch"}
        self.props = {
            (coreaudio.SYSTEM_OBJECT, coreaudio.SEL_DEVICES, coreaudio.SCOPE_GLOBAL):
                struct.pack("<3I", self.MIC, self.SPK, self.HOLE),
            (coreaudio.SYSTEM_OBJECT, coreaudio.SEL_DEFAULT_INPUT, coreaudio.SCOPE_GLOBAL):
                fourcc_bytes(self.MIC),
            (coreaudio.SYSTEM_OBJECT, coreaudio.SEL_DEFAULT_OUTPUT, coreaudio.SCOPE_GLOBAL):
                fourcc_bytes(self.SPK),
        }
        layout = {
            self.MIC: (buffer_list(1), buffer_list()),
            self.SPK: (buffer_list(), buffer_list(2)),
            self.HOLE: (buffer_list(2), buffer_list(2)),
        }
        for obj, (inp, out) in layout.items():
            self.props[(obj, coreaudio.SEL_STREAMS_LAYOUT, coreaudio.SCOPE_INPUT)] = inp
            self.props[(obj, coreaudio.SEL_STREAMS_LAYOUT, coreaudio.SCOPE_OUTPUT)] = out
            self.props[(obj, coreaudio.SEL_NOMINAL_RATE, coreaudio.SCOPE_GLOBAL)] = struct.pack("<d", 48000.0)
            self.props[(obj, coreaudio.SEL_TRANSPORT, coreaudio.SCOPE_GLOBAL)] = fourcc_bytes(
                coreaudio.TRANSPORT_VIRTUAL if obj == self.HOLE else coreaudio._fourcc("bltn")
            )
            # The name "as a string": the test replaces `_cfstring`, so the bytes hold the string
            # itself.
            self.props[(obj, coreaudio.SEL_NAME, coreaudio.SCOPE_GLOBAL)] = self.names[obj].encode()
            self.props[(obj, coreaudio.SEL_UID, coreaudio.SCOPE_GLOBAL)] = f"uid-{obj}".encode()

    def get(self, obj, selector, scope=coreaudio.SCOPE_GLOBAL):
        return self.props.get((obj, selector, scope))


@pytest.fixture
def fake_coreaudio(monkeypatch):
    fake = FakeCoreAudio()
    monkeypatch.setattr(coreaudio, "_get", fake.get)
    monkeypatch.setattr(coreaudio, "_cfstring", lambda raw: raw.decode())
    return fake


def test_coreaudio_lists_devices_with_their_direction(fake_coreaudio):
    found = {d.name: d for d in coreaudio.devices()}
    assert set(found) == {"MacBook Pro Microphone", "MacBook Pro Speakers", "BlackHole 2ch"}
    assert found["MacBook Pro Microphone"].inputs == 1 and found["MacBook Pro Microphone"].outputs == 0
    assert found["MacBook Pro Speakers"].inputs == 0 and found["MacBook Pro Speakers"].outputs == 2
    assert found["BlackHole 2ch"].virtual and not found["MacBook Pro Microphone"].virtual


def test_coreaudio_marks_the_defaults_and_lists_only_inputs(fake_coreaudio):
    assert [d.name for d in coreaudio.inputs()] == ["MacBook Pro Microphone", "BlackHole 2ch"]
    assert coreaudio.default_names() == {
        "in": "MacBook Pro Microphone", "out": "MacBook Pro Speakers",
    }
    assert next(d for d in coreaudio.inputs() if d.default_input).name == "MacBook Pro Microphone"


def test_coreaudio_sees_a_device_plugged_in_later(fake_coreaudio):
    """This is why CoreAudio is queried again: the PortAudio list freezes at startup."""
    assert len(coreaudio.inputs()) == 2
    new = 44
    fake_coreaudio.props[(coreaudio.SYSTEM_OBJECT, coreaudio.SEL_DEVICES, coreaudio.SCOPE_GLOBAL)] = struct.pack(
        "<4I", 41, 42, 43, new)
    fake_coreaudio.names[new] = "Logitech Webcam"
    fake_coreaudio.props[(new, coreaudio.SEL_NAME, coreaudio.SCOPE_GLOBAL)] = b"Logitech Webcam"
    fake_coreaudio.props[(new, coreaudio.SEL_STREAMS_LAYOUT, coreaudio.SCOPE_INPUT)] = buffer_list(1)
    assert "Logitech Webcam" in [d.name for d in coreaudio.inputs()]


def test_coreaudio_survives_a_system_that_does_not_answer(monkeypatch):
    monkeypatch.setattr(coreaudio, "_get", lambda *a, **k: None)
    assert coreaudio.devices() == []
    assert coreaudio.default_names() == {}


def test_buffer_list_channels_are_summed():
    assert coreaudio._channels(buffer_list(1, 2)) == 3
    assert coreaudio._channels(buffer_list()) == 0
    assert coreaudio._channels(None) == 0
    assert coreaudio._channels(b"\x01\x00\x00\x00") == 0     # truncated data does not crash the parser


# --------------------------------------------------------------------------
# Finding the microphone
# --------------------------------------------------------------------------


class FakeStream:
    def __init__(self, **params) -> None:
        self.params = params
        self.active = False
        self.closed = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True
        self.active = False

    def feed(self, frames: np.ndarray) -> None:
        """Run a chunk through the callback the way PortAudio does it."""
        self.params["callback"](frames, len(frames), None, type("S", (), {"input_overflow": False})())


class FakeSounddevice:
    class CallbackAbort(Exception):  # noqa: N818 — that is what it is called in sounddevice
        pass

    def __init__(self, devices: list[dict] | None = None) -> None:
        self.devices = devices if devices is not None else [
            {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000.0},
            {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2,
             "default_samplerate": 48000.0},
            {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2,
             "default_samplerate": 48000.0},
            {"name": "Logitech Webcam", "max_input_channels": 2, "max_output_channels": 0,
             "default_samplerate": 16000.0},
        ]
        self.streams: list[FakeStream] = []

    def query_devices(self):
        return self.devices

    def InputStream(self, **params):  # noqa: N802 — a fake imitating sounddevice
        stream = FakeStream(**params)
        self.streams.append(stream)
        return stream


@pytest.fixture
def live_defaults(monkeypatch):
    """The system treats the built-in one as the default microphone."""
    monkeypatch.setattr(coreaudio, "default_names",
                        lambda: {"in": "MacBook Pro Microphone", "out": "MacBook Pro Speakers"})


def test_default_microphone_is_the_one_the_system_says(live_defaults):
    sd = FakeSounddevice()
    dev = mac_audio.find_microphone(sd, None)
    assert dev.name == "MacBook Pro Microphone" and dev.channels == 1 and dev.rate == 48000
    assert dev.missed is None


def test_default_follows_the_live_system_answer_not_the_start_state(monkeypatch):
    sd = FakeSounddevice()
    monkeypatch.setattr(coreaudio, "default_names", lambda: {"in": "Logitech Webcam"})
    assert mac_audio.find_microphone(sd, None).name == "Logitech Webcam"


def test_a_pinned_microphone_is_found_by_part_of_the_name(live_defaults):
    assert mac_audio.find_microphone(FakeSounddevice(), "logitech").name == "Logitech Webcam"


def test_a_missing_pinned_microphone_falls_back_and_says_so(live_defaults):
    """The person pinned the headphones, and in the morning they did not wake up: do not refuse to
    start, say so instead.
    """
    dev = mac_audio.find_microphone(FakeSounddevice(), "AirPods")
    assert dev.name == "MacBook Pro Microphone"
    assert dev.missed == "AirPods"


def test_the_virtual_loopback_is_never_offered_as_a_microphone(live_defaults):
    """Choosing the loopback as "Me" would mean listening to the system sound with both
    channels.
    """
    assert mac_audio.find_microphone(FakeSounddevice(), "blackhole").name == "MacBook Pro Microphone"


def test_two_channel_microphones_are_capped_and_wide_ones_too():
    wide = [{"name": "Audio Interface", "max_input_channels": 16, "max_output_channels": 0,
             "default_samplerate": 44100.0}]
    dev = mac_audio.find_microphone(FakeSounddevice(wide), None)
    assert dev.channels == mac_audio.MAX_MIC_CHANNELS and dev.rate == 44100


def test_no_microphone_at_all_is_an_honest_refusal(live_defaults):
    only_speakers = [{"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2,
                      "default_samplerate": 48000.0}]
    with pytest.raises(DeviceNotFound, match="microphone"):
        mac_audio.find_microphone(FakeSounddevice(only_speakers), None)


# --------------------------------------------------------------------------
# The helper: real pipes, a fake program
# --------------------------------------------------------------------------

PCM_FRAMES = 4800        # 100 ms at 48 kHz

HAPPY_HELPER = f"""
import sys, json, struct, math
sys.stderr.write(json.dumps({{"event": "ready"}}) + "\\n"); sys.stderr.flush()
data = b"".join(
    struct.pack("<hh", int(12000 * math.sin(i / 3.0)), int(12000 * math.sin(i / 3.0)))
    for i in range({PCM_FRAMES})
)
out = sys.stdout.buffer
out.write(data); out.flush()
sys.stdin.buffer.read()      # wait until the owner closes stdin
sys.stderr.write(json.dumps({{"event": "stopped"}}) + "\\n"); sys.stderr.flush()
"""

DENIED_HELPER = """
import sys, json
sys.stderr.write(json.dumps({"event": "error", "code": "permission", "message": "denied"}) + "\\n")
sys.stderr.flush()
sys.exit(2)
"""


def helper_process(script: str) -> syscap.HelperProcess:
    return syscap.HelperProcess(command=[sys.executable, "-u", "-c", script])


def test_the_helper_streams_sound_and_stops_when_stdin_closes():
    helper = helper_process(HAPPY_HELPER)
    helper.start()
    try:
        got = b""
        while len(got) < PCM_FRAMES * syscap.BYTES_PER_FRAME:
            piece = helper.read()
            assert piece, "the helper finished early"
            got += piece
        assert len(got) == PCM_FRAMES * syscap.BYTES_PER_FRAME
        assert wait_for(lambda: helper.state == syscap.READY)
    finally:
        helper.stop()
    assert helper.state == syscap.STOPPED
    assert not helper.running


def test_a_refused_permission_becomes_words_for_a_human():
    helper = helper_process(DENIED_HELPER)
    helper.start()
    try:
        assert helper.read() == b""
        assert wait_for(lambda: helper.state == syscap.FAILED)
        assert helper.error_code == "permission"
        assert helper.error_text == permissions.SCREEN_ADVICE
    finally:
        helper.stop()


def test_a_helper_that_cannot_start_is_reported_not_raised_into_the_void(tmp_path):
    helper = syscap.HelperProcess(command=[str(tmp_path / "no-such-program")])
    with pytest.raises(syscap.SystemAudioUnavailable):
        helper.start()
    assert helper.state == syscap.FAILED


def test_exit_codes_map_to_reasons():
    assert syscap._code_of_exit(syscap.EXIT_PERMISSION) == "permission"
    assert syscap._code_of_exit(syscap.EXIT_UNSUPPORTED) == "unsupported"
    assert syscap._code_of_exit(99) == "failed"


def test_every_sentence_for_a_human_passes_the_plain_text_check():
    """The page replaces an opaque phrase with the generic "stream stopped", so ours must be clean.
    """
    texts = [
        permissions.SCREEN_ADVICE, permissions.MIC_ADVICE, permissions.MIC_RESTRICTED,
        syscap.explain("permission"), syscap.explain("unsupported"), syscap.explain("no_display"),
        syscap.explain("failed"),
    ]
    for text in texts:
        assert friendly.plain(text), text


def test_the_wording_hides_how_the_app_is_built():
    """Requirement (2026-10-08): no models, no libraries and nothing about how the app is built in
    the visible text.
    """
    for text in (permissions.SCREEN_ADVICE, permissions.MIC_ADVICE, syscap.explain("unsupported")):
        for word in ("ScreenCaptureKit", "CoreAudio", "PortAudio", "PyObjC", "helper"):
            assert word.lower() not in text.lower(), (word, text)


# --------------------------------------------------------------------------
# The helper's channel
# --------------------------------------------------------------------------


def test_helper_sound_reaches_the_bus_as_sixteen_kilohertz_mono():
    bus = FakeBus()
    channel = mac_audio._HelperChannel(
        "System audio", [], Source.THEM, bus, FakeClock(), True,
        helper=helper_process(HAPPY_HELPER),
    )
    channel.start()
    try:
        assert wait_for(lambda: channel.samples_out >= 1000)
        assert channel.alive and channel.error is None
        chunks = bus.chunks(Source.THEM)
        assert chunks and all(c.samples.dtype == np.int16 and c.samples.ndim == 1 for c in chunks)
        # 100 ms at 16 kHz is about 1600 samples; the resampler keeps a small tail.
        assert 1000 <= channel.samples_out <= 1700
        assert any(c.samples.any() for c in chunks)
        assert channel.silent_s < 5
    finally:
        channel.stop()


def test_a_helper_refused_by_the_system_makes_the_channel_dead_with_a_reason():
    bus = FakeBus()
    channel = mac_audio._HelperChannel(
        "System audio", [], Source.THEM, bus, FakeClock(), True,
        helper=helper_process(DENIED_HELPER),
    )
    channel.start()
    try:
        assert wait_for(lambda: channel.error is not None)
        assert not channel.alive
        assert str(channel.error) == permissions.SCREEN_ADVICE
    finally:
        channel.stop()


# --------------------------------------------------------------------------
# Capture as a whole
# --------------------------------------------------------------------------


def capture(monkeypatch, sd=None, cfg=None, helper_script: str | None = HAPPY_HELPER):
    monkeypatch.setattr(coreaudio, "default_names",
                        lambda: {"in": "MacBook Pro Microphone", "out": "MacBook Pro Speakers"})
    monkeypatch.setattr(permissions, "microphone", lambda: permissions.GRANTED)
    monkeypatch.setattr(permissions, "screen_recording", lambda: permissions.GRANTED)
    if helper_script is None:
        def unavailable():
            raise syscap.SystemAudioUnavailable("system audio needs macOS 13 or newer")
        monkeypatch.setattr(syscap, "check_available", unavailable)
    else:
        monkeypatch.setattr(syscap, "check_available", lambda: None)
        monkeypatch.setattr(
            syscap.HelperProcess, "command",
            lambda self: [sys.executable, "-u", "-c", helper_script],
        )
    bus = FakeBus()
    sd = sd or FakeSounddevice()
    cap = mac_audio.MacAudioCapture(bus, FakeClock(), cfg or AudioConfig(), sd=sd)
    return cap, bus, sd


def test_open_takes_the_default_microphone_and_the_system_sound(monkeypatch):
    cap, bus, sd = capture(monkeypatch)
    mic, loop = cap.open()
    try:
        assert mic.name == "MacBook Pro Microphone"
        assert loop.name == "System audio" and loop.index == -1
        assert sd.streams[0].params["device"] == 0 and sd.streams[0].active
        assert wait_for(lambda: bus.chunks(Source.THEM))
        assert cap.healthy
    finally:
        cap.close()
    assert sd.streams[0].closed
    assert cap.mic is None and cap.loop is None


def test_microphone_callback_publishes_sixteen_kilohertz_chunks(monkeypatch):
    cap, bus, sd = capture(monkeypatch)
    cap.open()
    try:
        tone = (8000 * np.sin(np.arange(1536) / 5.0)).astype(np.int16).reshape(-1, 1)
        for _ in range(4):
            sd.streams[0].feed(tone)
        mine = bus.chunks(Source.ME)
        assert mine and all(c.samples.dtype == np.int16 for c in mine)
        assert cap.mic.chunks == len(mine) and cap.mic.silent_s < 2
    finally:
        cap.close()


def test_a_failing_callback_stops_the_stream_and_leaves_a_trace(monkeypatch):
    cap, bus, sd = capture(monkeypatch)
    cap.open()
    try:
        cap.mic._resampler = None            # any failure inside the callback
        with pytest.raises(FakeSounddevice.CallbackAbort):
            sd.streams[0].feed(np.zeros((16, 1), dtype=np.int16))
        assert cap.mic.error is not None
        assert not cap.healthy and cap.first_error() is cap.mic.error
    finally:
        cap.close()


def test_without_system_sound_the_app_still_opens_on_the_microphone(monkeypatch):
    """The first launch is almost always without permission: refusing to open would be the worst
    first impression.
    """
    cap, bus, sd = capture(monkeypatch, helper_script=None)
    mic, loop = cap.open()
    try:
        assert mic.name == "MacBook Pro Microphone"
        assert "unavailable" in loop.name
        assert not cap.healthy
        assert "macOS 13" in str(cap.first_error())
        assert friendly.plain(str(cap.first_error()))
    finally:
        cap.close()


def test_a_virtual_loopback_device_named_in_settings_replaces_the_helper(monkeypatch):
    cap, bus, sd = capture(monkeypatch, cfg=AudioConfig(loopback_device="blackhole"))
    mic, loop = cap.open()
    try:
        assert loop.name == "BlackHole 2ch" and loop.index == 2
        assert len(sd.streams) == 2 and sd.streams[1].params["device"] == 2
        tone = (6000 * np.sin(np.arange(1536) / 4.0)).astype(np.int16).reshape(-1, 2)
        for _ in range(4):          # the resampler holds a delay: the first chunk may come out empty
            sd.streams[1].feed(tone)
        assert bus.chunks(Source.THEM)
    finally:
        cap.close()


def test_a_missing_loopback_device_falls_back_to_the_system_sound_and_says_so(monkeypatch):
    cap, bus, sd = capture(monkeypatch, cfg=AudioConfig(loopback_device="Loopback X"))
    mic, loop = cap.open()
    try:
        assert loop.name == "System audio"
        assert any("Loopback X" in c for c in cap.complaints())
    finally:
        cap.close()


def test_pinned_windows_become_a_filter_by_program(monkeypatch):
    from argus.capture.window import WindowInfo
    from argus.mac import windows

    seen: dict = {}

    def fake_find(title, skip=()):
        if title == "Zoom":
            return WindowInfo(title="Zoom Meeting", left=0, top=0, right=900, bottom=700, hwnd=7, pid=321)
        return None

    monkeypatch.setattr(windows, "find_window", fake_find)
    real_init = syscap.HelperProcess.__init__

    def spy(self, pids=None, exclude_pids=None, **kw):
        seen["pids"], seen["exclude"] = list(pids or []), list(exclude_pids or [])
        real_init(self, pids=pids, exclude_pids=exclude_pids, **kw)

    monkeypatch.setattr(syscap.HelperProcess, "__init__", spy)
    cap, bus, sd = capture(monkeypatch, cfg=AudioConfig(loopback_windows=["Zoom", "Telegram"]))
    mic, loop = cap.open()
    try:
        assert loop.name == "Zoom Meeting"
        assert seen["pids"] == [321]
        assert seen["exclude"], "its own sound (voice-over) must not end up in the system audio"
        assert any("Telegram" in c for c in cap.complaints())
    finally:
        cap.close()


def test_when_no_pinned_window_is_found_the_whole_system_is_listened_to(monkeypatch):
    from argus.mac import windows

    monkeypatch.setattr(windows, "find_window", lambda *a, **k: None)
    cap, bus, sd = capture(monkeypatch, cfg=AudioConfig(loopback_windows=["Zoom"]))
    mic, loop = cap.open()
    try:
        assert loop.name == "System audio"
        assert any("Zoom" in c for c in cap.complaints())
    finally:
        cap.close()


# --------------------------------------------------------------------------
# Complaints in plain words
# --------------------------------------------------------------------------


def opened(monkeypatch, cfg=None):
    cap, bus, sd = capture(monkeypatch, cfg=cfg)
    cap.open()
    cap._defaults = {"in": "MacBook Pro Microphone", "out": "MacBook Pro Speakers"}
    cap._defaults_at = time.monotonic()
    return cap


def test_a_denied_microphone_is_a_complaint_not_just_silence(monkeypatch):
    cap = opened(monkeypatch)
    try:
        monkeypatch.setattr(permissions, "microphone", lambda: permissions.DENIED)
        cap._perm = {}
        said = cap.complaints()
        assert permissions.MIC_ADVICE in said
        assert not any("silent for" in c for c in said), "the same thing is not said twice"
    finally:
        cap.close()


def test_a_silent_microphone_with_permission_is_the_old_complaint(monkeypatch):
    cap = opened(monkeypatch)
    try:
        cap.mic.opened_at = time.monotonic() - mac_audio.MIC_SILENCE_S - 5
        assert any("silent for" in c for c in cap.complaints())
        cap.mic.last_sound = time.monotonic()
        assert not any("silent for" in c for c in cap.complaints())
    finally:
        cap.close()


def test_a_drifted_default_input_is_reported(monkeypatch):
    cap = opened(monkeypatch)
    try:
        cap.mic.last_sound = time.monotonic()
        cap._defaults = {"in": "AirPods Pro", "out": "AirPods Pro"}
        assert any("but the system one is now “AirPods Pro”" in c for c in cap.complaints())
    finally:
        cap.close()


def test_a_pinned_microphone_is_the_users_choice_even_if_it_is_not_the_default(monkeypatch):
    cap = opened(monkeypatch, cfg=AudioConfig(mic_device="Logitech"))
    try:
        cap.mic.last_sound = time.monotonic()
        cap._defaults = {"in": "MacBook Pro Microphone"}
        assert not any("but the system one is now" in c for c in cap.complaints())
    finally:
        cap.close()


def test_a_denied_screen_right_is_a_complaint_while_the_helper_waits(monkeypatch):
    cap = opened(monkeypatch)
    try:
        monkeypatch.setattr(permissions, "screen_recording", lambda: permissions.DENIED)
        cap._perm = {}
        assert permissions.SCREEN_ADVICE in cap.complaints()
    finally:
        cap.close()


def test_nothing_is_claimed_before_the_capture_is_open(monkeypatch):
    cap, _bus, _sd = capture(monkeypatch)
    assert cap.complaints() == []
    assert cap.system_defaults() == {}


# --------------------------------------------------------------------------
# The microphone list for the page
# --------------------------------------------------------------------------


def test_the_microphone_list_is_live_and_hides_the_loopback(fake_coreaudio, monkeypatch):
    sd = FakeSounddevice()
    cap = mac_audio.MacAudioCapture(FakeBus(), FakeClock(), AudioConfig(), sd=sd)
    names = [d["name"] for d in cap.inputs()]
    assert names == ["MacBook Pro Microphone"]
    only = cap.inputs()[0]
    assert only["default"] is True and only["index"] == 0 and only["rate"] == 48000


def test_reopen_closes_and_opens_again(monkeypatch):
    cap, bus, sd = capture(monkeypatch)
    cap.open()
    try:
        cap.reopen()
        assert len(sd.streams) == 2 and sd.streams[0].closed and sd.streams[1].active
    finally:
        cap.close()



def test_a_helper_that_cannot_start_leaves_the_microphone_working(monkeypatch):
    """The helper file is missing or not executable: the app lives on the microphone."""
    cap, bus, sd = capture(monkeypatch)

    def cannot(self):
        raise syscap.SystemAudioUnavailable("the capture module failed to start")

    monkeypatch.setattr(syscap.HelperProcess, "start", cannot)
    mic, loop = cap.open()
    try:
        assert mic.name == "MacBook Pro Microphone" and "unavailable" in loop.name
        assert not cap.healthy and "failed to start" in str(cap.first_error())
        assert sd.streams[0].active, "the microphone is listening"
    finally:
        cap.close()


def test_a_microphone_that_cannot_start_is_a_plain_refusal(monkeypatch):
    cap, bus, sd = capture(monkeypatch)
    real = FakeStream.start

    def broken(self):
        raise OSError("PortAudio: Error starting stream")

    monkeypatch.setattr(FakeStream, "start", broken)
    try:
        with pytest.raises(DeviceNotFound, match="failed to start"):
            cap.open()
    finally:
        monkeypatch.setattr(FakeStream, "start", real)
        cap.close()


def test_a_missing_helper_is_said_without_its_file_name(monkeypatch):
    """To the person it is "capture module", not a file name: a file name speaks
    of how the app is built.
    """
    monkeypatch.setattr(syscap, "find_helper", lambda: None)
    monkeypatch.setattr(syscap, "macos_version", lambda: (14, 0))
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(syscap.SystemAudioUnavailable) as raised:
        syscap.check_available()
    text = str(raised.value)
    assert "argus" not in text.lower() and friendly.plain(text), text


def test_a_raw_english_message_from_the_helper_never_reaches_a_human():
    assert syscap.explain("format", "unexpected audio format") == syscap.explain("anything-else")
    for code in ("permission", "unsupported", "no_display", "no_applications", "format", "failed"):
        assert friendly.plain(syscap.explain(code, "Some English message")), code


def test_a_pinned_program_that_quit_is_a_complaint(monkeypatch):
    """The helper's stream is alive but the program is gone: silence without an error, so it must
    be reported.
    """
    from argus.capture.window import WindowInfo
    from argus.mac import windows

    monkeypatch.setattr(
        windows, "find_window",
        lambda title, skip=(): WindowInfo(title="Zoom Meeting", left=0, top=0, right=900, bottom=700, hwnd=7, pid=4321),
    )
    cap = opened(monkeypatch, cfg=AudioConfig(loopback_windows=["Zoom"]))
    try:
        cap.mic.last_sound = time.monotonic()
        alive = {"yes": True}

        def fake_kill(pid, sig):
            if not alive["yes"]:
                raise ProcessLookupError(pid)

        monkeypatch.setattr(mac_audio.os, "kill", fake_kill)
        monkeypatch.setattr(mac_audio.sys, "platform", "darwin")
        assert not any("is gone" in c for c in cap.complaints())
        alive["yes"] = False
        assert any("pinned program's audio is gone" in c for c in cap.complaints())
    finally:
        cap.close()


def test_a_process_we_may_not_signal_is_still_considered_alive(monkeypatch):
    channel = mac_audio._HelperChannel("x", [42], Source.THEM, FakeBus(), FakeClock(), True,
                                       helper=helper_process(HAPPY_HELPER))

    def refuse(pid, sig):
        raise PermissionError(pid)

    monkeypatch.setattr(mac_audio.os, "kill", refuse)
    monkeypatch.setattr(mac_audio.sys, "platform", "darwin")
    assert channel.dead_pids() == []


def test_on_windows_nobody_is_ever_signalled(monkeypatch):
    """`os.kill(pid, 0)` on Windows does not check the process, it terminates it: this path is
    never reached there.
    """
    channel = mac_audio._HelperChannel("x", [42], Source.THEM, FakeBus(), FakeClock(), True,
                                       helper=helper_process(HAPPY_HELPER))

    def forbidden(pid, sig):
        raise AssertionError("the process must not be touched")

    monkeypatch.setattr(mac_audio.os, "kill", forbidden)
    monkeypatch.setattr(mac_audio.sys, "platform", "win32")
    assert channel.dead_pids() == []
