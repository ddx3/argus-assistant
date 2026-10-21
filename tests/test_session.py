"""Checks for recording and replay.

The main one here is `test_round_trip_audio_is_identical`. The whole point of M2 is that
the pipeline cannot tell a replay from live capture; if recording and playback diverge
even slightly, reproducible debugging stops working, and a test has to catch that, not
us noticing it a week later on some unclear bug.
"""

from __future__ import annotations

import asyncio
import wave
from datetime import UTC, datetime

import numpy as np
import pytest

from argus.core.bus import Bus
from argus.core.config import RecordConfig
from argus.core.events import SAMPLE_RATE, AudioChunk, Frame, Source
from argus.session.layout import WAV_NAMES, find_session, list_sessions, session_id
from argus.session.recorder import Recorder
from argus.session.replayer import Replayer

NS = 1_000_000_000
STARTED = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def tone(n: int, amplitude: float = 0.3, freq: float = 200.0) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype(np.int16)


def chunk(t_ns: int, source: Source, n: int) -> AudioChunk:
    return AudioChunk(t=t_ns, source=source, samples=tone(n))


def frame(t_ns: int, seq: int, value: int = 128) -> Frame:
    return Frame(t=t_ns, image=np.full((16, 24, 3), value, dtype=np.uint8), seq=seq)


def wav_samples(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_position_in_file_equals_time(tmp_path):
    """A sample recorded at second 1 must sit at position 16000."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(1 * NS, Source.ME, 1600))
    rec.close()

    data = wav_samples(tmp_path / "s" / WAV_NAMES[Source.ME])
    assert len(data) == SAMPLE_RATE + 1600
    assert np.all(data[:SAMPLE_RATE] == 0)  # silence until the first chunk
    assert np.any(data[SAMPLE_RATE:] != 0)


def test_gap_is_filled_with_silence(tmp_path):
    """A gap in the stream must not collapse the chronology.

    Without filling, two chunks with a pause between them would stick together, and
    everything recorded after them would end up earlier than it happened.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, Source.THEM, 1600))            # 0.0 - 0.1 s
    rec.feed_audio(chunk(int(0.6 * NS), Source.THEM, 1600))  # after a 0.5 s pause
    meta = rec.close()

    data = wav_samples(tmp_path / "s" / WAV_NAMES[Source.THEM])
    assert len(data) == int(0.6 * SAMPLE_RATE) + 1600
    assert meta.silence_filled_ms["them"] == pytest.approx(500, abs=1)
    # The middle of the gap is exactly silence.
    assert data[int(0.35 * SAMPLE_RATE)] == 0


def test_small_jitter_is_not_treated_as_gap(tmp_path):
    """A couple of milliseconds of mismatch is the scheduler, not lost data."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, Source.ME, 1600))
    rec.feed_audio(chunk(1600 * NS // SAMPLE_RATE + 1_000_000, Source.ME, 1600))  # +1 ms
    meta = rec.close()
    assert meta.silence_filled_ms["me"] == 0


def test_silent_channel_is_padded_to_full_length(tmp_path):
    """A channel that stayed silent for the whole session must still have the full length.

    This is not made up: it was measured that WASAPI loopback calls no callback at all when
    there is no sound, and in 12 seconds of recording zero chunks arrived. Without padding
    the tail, the track would have come out with zero length, and on replay the "system"
    channel would end before the "microphone".
    """
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(10):
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, Source.ME, 1600))
    meta = rec.close()  # not a single chunk arrived for THEM

    me = wav_samples(tmp_path / "s" / WAV_NAMES[Source.ME])
    them = wav_samples(tmp_path / "s" / WAV_NAMES[Source.THEM])
    assert len(them) == len(me)
    assert np.all(them == 0)
    assert meta.silence_filled_ms["them"] == pytest.approx(1000, abs=5)


async def test_silent_channel_replays_as_silence(tmp_path):
    """On replay too, a channel that was silent must run as long as the one that was speaking."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(10):
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, Source.ME, 1600))
    rec.close()

    got, _, _ = await _replay_collect(tmp_path / "s", with_frames=False)
    me_len = sum(len(c.samples) for c in got["me"])
    them_len = sum(len(c.samples) for c in got["them"])
    assert them_len == me_len


def test_channels_are_written_separately(tmp_path):
    """The microphone and the system audio must not end up in one file."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(AudioChunk(t=0, source=Source.ME, samples=tone(1600, 0.5)))
    rec.feed_audio(AudioChunk(t=0, source=Source.THEM, samples=np.zeros(1600, np.int16)))
    rec.close()

    me = wav_samples(tmp_path / "s" / WAV_NAMES[Source.ME])
    them = wav_samples(tmp_path / "s" / WAV_NAMES[Source.THEM])
    assert np.any(me != 0)
    assert np.all(them == 0)


def test_meta_records_devices_and_counts(tmp_path):
    from argus.session.layout import DeviceMeta

    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, Source.ME, 1600))
    rec.feed_frame(frame(0, 0))
    meta = rec.close(
        mic=DeviceMeta(name="Microphone", rate=44100, channels=2),
        loopback=DeviceMeta(name="Headphones [Loopback]", rate=48000, channels=2),
    )
    assert meta.mic.rate == 44100
    assert meta.loopback.channels == 2
    assert meta.chunks["me"] == 1
    assert meta.frames == 1
    assert meta.screen_size == (24, 16)


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


async def _replay_collect(path, chunk_ms=32, with_frames=True):
    bus = Bus(asyncio.get_running_loop())
    got: dict[str, list] = {"me": [], "them": [], "frames": []}

    a_sub = bus.subscribe(AudioChunk, maxsize=16)
    f_sub = bus.subscribe(Frame, maxsize=4)

    async def pump_a():
        async for c in a_sub:
            got[c.source.value].append(c)

    async def pump_f():
        async for f in f_sub:
            got["frames"].append(f)

    tasks = [asyncio.create_task(pump_a()), asyncio.create_task(pump_f())]
    player = Replayer(path, bus, chunk_ms=chunk_ms, speed=0, with_frames=with_frames)
    stats = await player.run()
    await asyncio.sleep(0.05)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return got, stats, bus


async def test_round_trip_audio_is_identical(tmp_path):
    """Audio after replay must match what was recorded, down to the last sample."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(10):
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, Source.ME, 1600))
    rec.close()

    original = wav_samples(tmp_path / "s" / WAV_NAMES[Source.ME])
    got, _, _ = await _replay_collect(tmp_path / "s", with_frames=False)
    replayed = np.concatenate([c.samples for c in got["me"]])

    assert np.array_equal(original, replayed)


async def test_round_trip_loses_no_frames(tmp_path):
    """Replay must deliver everything that was recorded, even if the subscriber is slow.

    The live bus drops old items when congested, and that is right: hardware cannot be
    waited for. But replay publishes through `publish_all` and waits for room; otherwise
    playback stops being playback.

    We compare with what actually landed on disk: if the recorder is flooded with frames
    faster than the disk can keep up, it drops some on purpose. That is a separate behavior,
    and it is tested below.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(40):
        rec.feed_frame(frame(i * NS // 10, i, value=i * 3))
    meta = rec.close()

    got, _, bus = await _replay_collect(tmp_path / "s")
    assert len(got["frames"]) == meta.frames
    assert all(v["dropped"] == 0 for v in bus.stats().values())


async def test_frames_survive_a_realistic_rate(tmp_path):
    """At the usual 10 frames per second the recorder must lose nothing.

    The recording rate is set explicitly: the test is about the transport, not about
    thinning, and the config default must not affect it here.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=30), STARTED)
    for i in range(12):
        rec.feed_frame(frame(i * NS // 10, i, value=i * 3))
        await asyncio.sleep(0.01)  # let the writer clear the queue
    meta = rec.close()

    assert rec.dropped_frames == 0
    assert meta.frames == 12
    got, _, _ = await _replay_collect(tmp_path / "s")
    assert [f.seq for f in got["frames"]] == list(range(12))


def test_dropped_frames_are_counted_not_hidden(tmp_path):
    """If the disk cannot keep up, the loss must land in meta.json and not disappear."""
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=30), STARTED)
    # The timestamps run at the capture rate so that thinning does not eat anything: it is a
    # tight loop without pauses that overflows the queue, not close timestamps.
    for i in range(200):  # certainly faster than the writer can keep up
        rec.feed_frame(frame(i * NS // 30, i))
    meta = rec.close()

    assert rec.dropped_frames > 0
    assert meta.frames == 200 - rec.dropped_frames


async def test_replay_preserves_time_order(tmp_path):
    """Events must arrive in the same time order as they were recorded."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(20):
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, Source.ME, 1600))
        if i % 4 == 0:
            rec.feed_frame(frame(i * 1600 * NS // SAMPLE_RATE, i))
    rec.close()

    got, _, _ = await _replay_collect(tmp_path / "s")
    times = [c.t for c in got["me"]]
    assert times == sorted(times)
    assert [f.seq for f in got["frames"]] == sorted(f.seq for f in got["frames"])


async def test_replay_clock_follows_the_recording(tmp_path):
    """The replay clock must show the recording time, not the current time."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, Source.ME, SAMPLE_RATE))  # exactly one second
    rec.close()

    bus = Bus(asyncio.get_running_loop())
    player = Replayer(tmp_path / "s", bus, speed=0, with_frames=False)
    assert player.clock.now() == 0
    await player.run()
    assert player.clock.now() >= int(0.9 * NS)
    assert player.clock.to_wall(0).hour == 12


async def test_replay_is_much_faster_than_realtime(tmp_path):
    """This is what it was all for: minutes of recording must pass in seconds."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(200):  # 20 seconds of audio
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, Source.ME, 1600))
    rec.close()

    _, stats, _ = await _replay_collect(tmp_path / "s", with_frames=False)
    assert stats.speedup > 20


# --------------------------------------------------------------------------
# Finding recordings
# --------------------------------------------------------------------------


def test_session_id_is_sortable_and_readable():
    assert session_id(STARTED) == "2026-08-28_1200-00"


def test_find_session_by_tail(tmp_path):
    for stamp in ("2026-08-28_1200-00", "2026-08-28_1345-00"):
        Recorder(tmp_path / stamp, RecordConfig(), STARTED).close()
    assert find_session(tmp_path, "1345").name == "2026-08-28_1345-00"


def test_find_session_reports_ambiguity(tmp_path):
    for stamp in ("2026-08-28_1200-00", "2026-08-29_1200-00"):
        Recorder(tmp_path / stamp, RecordConfig(), STARTED).close()
    with pytest.raises(ValueError, match="several"):
        find_session(tmp_path, "1200")


def test_find_session_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_session(tmp_path, "no-such")


def test_list_sessions_ignores_broken_dirs(tmp_path):
    Recorder(tmp_path / "2026-08-28_1200-00", RecordConfig(), STARTED).close()
    (tmp_path / "junk").mkdir()
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "meta.json").write_text("{not json", encoding="utf-8")
    assert [p.name for p, _ in list_sessions(tmp_path)] == ["2026-08-28_1200-00"]


# --------------------------------------------------------------------------
# Constant recording
# --------------------------------------------------------------------------


def test_recording_is_off_by_default():
    """The default is not to record: recording without asking puts somebody's sound on disk."""
    from argus.app import Argus
    from argus.core.config import Config

    app = Argus(Config(), with_screen=False, with_brain=False)
    assert app._record is False


def test_always_records_without_the_flag():
    """`record.always` removes the need to remember the flag.

    A recording cannot be made after the fact: a session turns out to be interesting only
    after it has passed. A flag that has to be remembered in advance does not solve this,
    because people remember it exactly when nothing is happening.
    """
    from argus.app import Argus
    from argus.core.config import Config

    cfg = Config()
    cfg.record.always = True
    app = Argus(cfg, with_screen=False, with_brain=False)
    assert app._record is True


def test_constant_recording_keeps_frames_but_throttled():
    """We record frames, but not thirty per second.

    What is expensive is not "recording the screen" but recording it at the capture rate: a
    gigabyte per ten minutes goes to saving the same picture thirty times. Once per second
    is enough to ask the model later what was there.
    """
    from argus.app import Argus
    from argus.core.config import Config

    cfg = Config()
    cfg.record.always = True
    app = Argus(cfg, with_screen=True, with_brain=False)
    assert app._record is True
    assert app.record_frames is True
    assert cfg.record.frames_fps == 1.0


def test_frames_can_be_turned_off():
    """Audio only: for when the screen is not needed and disk space is worth more."""
    from argus.app import Argus
    from argus.core.config import Config

    cfg = Config()
    cfg.record.always = True
    cfg.record.always_screen = False
    app = Argus(cfg, with_screen=True, with_brain=False)
    assert app._record is True
    assert app.record_frames is False


def test_explicit_flag_records_frames_too():
    """`--record` was asked for by hand, so a full recording is needed, with the screen."""
    from argus.app import Argus
    from argus.core.config import Config

    app = Argus(Config(), with_screen=True, with_brain=False, record=True)
    assert app.record_frames is True


def test_always_screen_turns_frames_back_on():
    from argus.app import Argus
    from argus.core.config import Config

    cfg = Config()
    cfg.record.always = True
    cfg.record.always_screen = True
    app = Argus(cfg, with_screen=True, with_brain=False)
    assert app.record_frames is True


def _make_session(root, stamp: str, mb: float):
    """A recording of a given size with the start time taken from its own name.

    The time matters: deletion goes from old to new, and the order comes from `meta.json`,
    not from the folder name.
    """
    started = datetime(
        int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10]),
        int(stamp[11:13]), int(stamp[13:15]), tzinfo=UTC,
    )
    d = root / stamp
    Recorder(d, RecordConfig(), started).close()
    (d / "bulk.bin").write_bytes(b"\0" * int(mb * 2**20))
    return d


def test_prune_keeps_everything_under_budget(tmp_path):
    from argus.session.layout import prune_sessions

    _make_session(tmp_path, "2026-08-28_1200-00", 1)
    _make_session(tmp_path, "2026-08-28_1300-00", 1)
    assert prune_sessions(tmp_path, keep_gb=1.0) == []
    assert len(list(tmp_path.glob("*"))) == 2


def test_prune_removes_oldest_first(tmp_path):
    """A fresh recording matters more: it is the one that gets replayed when the engine made a
    mistake.
    """
    from argus.session.layout import prune_sessions

    _make_session(tmp_path, "2026-08-28_1200-00", 6)
    _make_session(tmp_path, "2026-08-28_1300-00", 6)
    _make_session(tmp_path, "2026-08-28_1400-00", 6)

    removed = prune_sessions(tmp_path, keep_gb=14 / 1024)     # ~14 MB
    assert [p.name for p, _ in removed] == ["2026-08-28_1200-00"]
    assert sorted(p.name for p in tmp_path.glob("*")) == [
        "2026-08-28_1300-00", "2026-08-28_1400-00",
    ]


def test_prune_reports_what_it_deleted(tmp_path):
    """Silently erasing someone's recordings is not acceptable; it has to be said out loud."""
    from argus.session.layout import prune_sessions

    _make_session(tmp_path, "2026-08-28_1200-00", 5)
    _make_session(tmp_path, "2026-08-28_1300-00", 5)

    removed = prune_sessions(tmp_path, keep_gb=6 / 1024)
    assert removed, "the deletion went through silently"
    path, size = removed[0]
    assert path.name == "2026-08-28_1200-00"
    assert size > 4 * 2**20


def test_zero_budget_disables_pruning(tmp_path):
    from argus.session.layout import prune_sessions

    _make_session(tmp_path, "2026-08-28_1200-00", 5)
    assert prune_sessions(tmp_path, keep_gb=0) == []
    assert len(list(tmp_path.glob("*"))) == 1


def test_prune_on_missing_dir_is_not_an_error(tmp_path):
    from argus.session.layout import prune_sessions

    assert prune_sessions(tmp_path / "no-such", keep_gb=1.0) == []


# --------------------------------------------------------------------------
# Frame recording rate
# --------------------------------------------------------------------------


def test_recorder_thins_frames_to_the_configured_rate(tmp_path):
    """Capture runs at 30 frames per second; there is no point in writing all of them.

    A gigabyte per ten minutes goes to saving the same picture thirty times. The recording
    exists for the question "what was on the screen", not for smoothness.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=1.0), STARTED)
    for i in range(30):                       # one second of capture
        rec.feed_frame(frame(int(i * NS / 30), i))
    meta = rec.close()

    assert meta.frames == 1, f"one frame must go to disk, {meta.frames} went"


def test_recorder_honours_a_higher_rate(tmp_path):
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=10.0), STARTED)
    for i in range(30):
        rec.feed_frame(frame(int(i * NS / 30), i))
    meta = rec.close()
    assert meta.frames == 10


def test_thinning_lives_in_the_recorder_not_the_caller(tmp_path):
    """One rule, one home.

    Thinning first lived in the dashboard, and `argus record` did not know about it: the
    setting worked in one place and silently did not work in another. The test keeps it where
    both paths can see it.
    """
    import inspect

    from argus.session.recorder import Recorder as R

    assert "frames_fps" not in inspect.getsource(__import__(
        "argus.app", fromlist=["Argus"]).Argus._pump_frames)
    assert "_next_frame_t" in inspect.getsource(R.feed_frame)


def test_skipped_frames_are_not_reported_as_losses(tmp_path):
    """Skipped by the setting is not the same as the disk not keeping up.

    The first is a decision, the second is a loss, and the report must not confuse them.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=1.0), STARTED)
    for i in range(30):
        rec.feed_frame(frame(int(i * NS / 30), i))
    meta = rec.close()

    assert rec._skipped_frames == 29
    assert rec.dropped_frames == 0
    assert meta.frames == 1


# --------------------------------------------------------------------------
# Recording starts on the button
# --------------------------------------------------------------------------


def _app(**record_cfg):
    from argus.app import Argus
    from argus.core.config import Config

    cfg = Config()
    cfg.record.always = True
    for k, v in record_cfg.items():
        setattr(cfg.record, k, v)
    return Argus(cfg, with_screen=False, with_brain=False)


def test_no_session_until_the_button(tmp_path):
    """Until the button is pressed, there must be no session folder.

    Before, recording started when the app started, and the minutes spent choosing the window
    and the sources got into it. In a measurement that is extra silence at the start, which
    then has to be found and cut off by hand.
    """
    app = _app()
    app.cfg.paths.sessions = tmp_path
    assert app.session_dir is None
    assert app.recorder is None
    assert list(tmp_path.glob("*")) == []


def test_button_opens_a_session(tmp_path):
    app = _app()
    app.cfg.paths.sessions = tmp_path
    app.set_running(True)

    assert app.recorder is not None
    assert app.session_dir is not None and app.session_dir.exists()
    assert app.recording_started_at is not None
    assert app.recording_began.is_set()


def test_button_off_closes_the_session(tmp_path):
    """Button released: the session is closed and readable."""
    from argus.session.layout import list_sessions

    app = _app()
    app.cfg.paths.sessions = tmp_path
    app.set_running(True)
    app.set_running(False)

    assert app.recorder is None
    assert len(list_sessions(tmp_path)) == 1, "meta.json is not written, the session is unreadable"


def test_each_press_gets_its_own_session(tmp_path):
    """A pause between presses must not end up in the file as silence.

    An hour of silence weighs the same 220 MB as an hour of conversation, so the previous
    session must not be appended to after a pause; we start a new one.
    """
    from argus.session.layout import list_sessions

    app = _app()
    app.cfg.paths.sessions = tmp_path

    app.set_running(True)
    first = app.session_dir
    app.set_running(False)

    app.set_running(True)
    second = app.session_dir
    app.set_running(False)

    assert first != second
    assert len(list_sessions(tmp_path)) == 2


def test_recording_starts_at_zero_not_at_app_start(tmp_path):
    """File zero is the moment of the press, not the start of the app.

    Without a reference point, a press in the second minute would give two minutes of
    silence at the start of the track: about 26 MB of emptiness.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED, t0_ns=120 * NS)
    rec.feed_audio(chunk(120 * NS, Source.ME, 1600))       # the first chunk after the press
    rec.close()

    data = wav_samples(tmp_path / "s" / WAV_NAMES[Source.ME])
    assert len(data) == 1600, f"{len(data) - 1600} samples of silence were added to the start"


def test_origin_keeps_relative_timing(tmp_path):
    """The shift must not break the property "position in file = time"."""
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED, t0_ns=120 * NS)
    rec.feed_audio(chunk(120 * NS, Source.ME, 1600))
    rec.feed_audio(chunk(121 * NS, Source.ME, 1600))       # exactly one second later
    rec.close()

    data = wav_samples(tmp_path / "s" / WAV_NAMES[Source.ME])
    assert len(data) == SAMPLE_RATE + 1600
