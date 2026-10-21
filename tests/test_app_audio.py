"""Audio of a single app instead of audio of the whole device.

Live capture is not tested here: it needs a process that is playing right now, and the build
machine has none. What is tested is what breaks silently: the choice of source and the
behavior on failure.

Failure matters more than success. If the app's audio could not be taken, the channel must
return to the device and **say so**: the person is in a conversation, and "no sound at all"
is worse than "sound from the whole device". A silently substituted source is then hunted
for hours, guessing why music gets into the feed.
"""

from __future__ import annotations

import asyncio

import pytest

from argus.capture.audio import AudioCapture, DeviceInfo

# Per-window sound is WASAPI process loopback and COM: Windows only.
pytestmark = pytest.mark.windows_only
from argus.core.bus import Bus
from argus.core.clock import LiveClock
from argus.core.config import Config


def test_window_is_not_pinned_by_default():
    """The default is the whole device: that is how everything behaved before this change."""
    assert Config().audio.loopback_windows == []


def test_old_single_window_field_still_loads():
    """Compatibility with files already saved on other people's machines: the old
    `loopback_window` field (a single string) turns into a list of one."""
    cfg = Config.model_validate({"audio": {"loopback_window": "Zoom"}})
    assert cfg.audio.loopback_windows == ["Zoom"]


def test_windows_here_can_give_audio_of_one_process():
    """Requires Windows 10 build 20348 or newer. On older ones the result is silently silence,
    so we ask in advance."""
    from argus.capture.app_loopback import available

    ok, detail = available()
    assert isinstance(ok, bool)
    assert detail


def test_window_without_a_process_gives_zero():
    """A zero handle is not a window. It must not raise an exception."""
    from argus.capture.app_loopback import window_pid

    assert window_pid(0) == 0


class _Recorder:
    """Replaces `_Channel` so that no real device is opened."""

    def __init__(self, *args, **kwargs):
        self.device = args[1] if len(args) > 1 else DeviceInfo(0, "device", 48000, 2)
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


@pytest.fixture
def capture(monkeypatch):
    from argus.capture import audio

    monkeypatch.setattr(audio, "_Channel", _Recorder)
    cfg = Config()
    return audio, AudioCapture(Bus(asyncio.new_event_loop()), LiveClock(), cfg.audio), cfg


def test_missing_window_falls_back_to_the_device(capture):
    """The window was closed between setup and start — no reason to be left without sound."""
    _, cap, cfg = capture
    cfg.audio.loopback_windows = ["no-such-window-and-never-will-be"]
    cap._cfg = cfg.audio

    channel = cap._open_loopback(DeviceInfo(0, "headphones", 48000, 2))

    assert isinstance(channel, _Recorder), "we should have fallen back to the device"
    assert "not found" in cap._app_audio_failed


def test_the_fallback_is_said_out_loud(capture):
    """The source substitution must reach the dashboard, not stay in the log."""
    _, cap, cfg = capture
    cfg.audio.loopback_windows = ["no-such-window"]
    cap._cfg = cfg.audio
    cap._open_loopback(DeviceInfo(0, "headphones", 48000, 2))

    assert any("no-such-window" in c for c in cap.complaints())


def test_one_missing_window_does_not_lose_the_others(monkeypatch, capture):
    """Not all of several pinned windows were found — we listen to what was found, rather than
    falling back to the whole device."""
    from argus.capture import app_loopback, window
    from argus.capture.audio import _AppChannel

    _, cap, cfg = capture
    cfg.audio.loopback_windows = ["Chrome", "missing-one"]
    cap._cfg = cfg.audio

    monkeypatch.setattr(app_loopback, "available", lambda: (True, "ok"))
    monkeypatch.setattr(app_loopback, "window_pid", lambda hwnd: 1234)
    monkeypatch.setattr(
        window, "find_window",
        lambda needle, skip=(): (
            window.WindowInfo(title="Chrome", left=0, top=0, right=100, bottom=100, hwnd=1)
            if needle == "Chrome" else None
        ),
    )
    monkeypatch.setattr(app_loopback, "ProcessLoopback", lambda pid, **kw: _FakeLoopback())

    channel = cap._open_loopback(DeviceInfo(0, "headphones", 48000, 2))

    assert isinstance(channel, _AppChannel)
    assert "missing-one" in cap._app_audio_failed
    assert "Chrome" not in cap._app_audio_failed


class _FakeLoopback:
    """A stub for `ProcessLoopback` that does not touch the real COM."""

    def open(self):
        pass

    def read(self):
        import numpy as np

        return np.empty(0, dtype=np.int16)

    def close(self):
        pass


class _DeadThread:
    def is_alive(self):
        return False


class _AliveThread:
    def is_alive(self):
        return True


def test_multi_window_channel_survives_one_reader_dying(monkeypatch):
    """Several pinned windows mean several independent readers (`_AppReader`). The crash of one
    reader's process must not silence the sound from the others: the channel counts as failed
    only when all of them have died at once.
    """
    import asyncio

    from argus.capture import app_loopback
    from argus.capture.audio import _AppChannel
    from argus.core.bus import Bus
    from argus.core.clock import LiveClock
    from argus.core.events import Source

    monkeypatch.setattr(app_loopback, "ProcessLoopback", lambda pid, **kw: _FakeLoopback())

    channel = _AppChannel(
        [("Chrome", 111), ("Zoom", 222)], Source.THEM,
        Bus(asyncio.new_event_loop()), LiveClock(), True,
    )
    # We do not start real threads; we substitute the state of the readers directly, as if one of
    # the processes had closed during work.
    channel._readers[0].error = RuntimeError("the process closed")
    channel._readers[0]._thread = _DeadThread()
    channel._readers[1]._thread = _AliveThread()

    assert channel.alive, "the second reader is still alive, so the channel is alive"
    assert channel.error is None
    assert channel.failed_windows() == ["Chrome"]

    channel._readers[1]._thread = _DeadThread()
    channel._readers[1].error = RuntimeError("this one too")

    assert not channel.alive, "both readers are dead, so the channel is dead"
    assert channel.error is not None


def test_a_dead_window_among_others_is_complained_about(monkeypatch):
    """A window closed in the middle of a conversation — this cannot go unmentioned, even if the
    other pinned windows still sound and the channel as a whole is alive."""
    import asyncio

    from argus.capture import app_loopback
    from argus.capture.audio import AudioCapture, _AppChannel
    from argus.core.bus import Bus
    from argus.core.clock import LiveClock
    from argus.core.config import Config
    from argus.core.events import Source

    monkeypatch.setattr(app_loopback, "ProcessLoopback", lambda pid, **kw: _FakeLoopback())

    cfg = Config()
    cap = AudioCapture(Bus(asyncio.new_event_loop()), LiveClock(), cfg.audio)
    channel = _AppChannel(
        [("Chrome", 111), ("Zoom", 222)], Source.THEM,
        Bus(asyncio.new_event_loop()), LiveClock(), True,
    )
    channel._readers[0].error = RuntimeError("the process closed")
    channel._readers[0]._thread = _DeadThread()
    channel._readers[1]._thread = _AliveThread()
    cap.loop = channel

    complaints = cap.complaints()
    assert any("Chrome" in c for c in complaints)
    assert not any("Zoom" in c for c in complaints)


def test_without_a_window_nothing_is_complained_about(capture):
    """The normal mode is no reason for a complaint."""
    _, cap, cfg = capture
    cap._cfg = cfg.audio
    cap._open_loopback(DeviceInfo(0, "headphones", 48000, 2))

    assert not getattr(cap, "_app_audio_failed", "")


def test_pinned_app_is_not_reported_as_the_wrong_device():
    """There is nothing to compare a window title with the names of the speakers.

    Without this reservation, a person who chose Chrome would get "the sound is going past,
    reconnect" out of the blue, and would reconnect, losing their choice.
    """
    import asyncio

    from argus.capture.audio import AudioCapture, DeviceInfo
    from argus.core.bus import Bus
    from argus.core.clock import LiveClock
    from argus.core.config import Config

    cfg = Config()
    cap = AudioCapture(Bus(asyncio.new_event_loop()), LiveClock(), cfg.audio)

    class _AppLike:
        # index < 0 marks an app channel: there is no device behind it.
        device = DeviceInfo(index=-1, name="Chrome", rate=48000, channels=2)
        alive = True
        silent_s = 0.0

    cap.loop = _AppLike()
    cap._defaults = {"out": "Headphones", "in": "Microphone"}

    assert not any("going elsewhere" in c for c in cap.complaints())
