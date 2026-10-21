"""Session recording survives sleep, a full disk and a crash (core-session-9, -10, -11, -x4).

Before, any gap in time was filled with zeros in a single piece: after an eight-hour
sleep that is ≈1.8 GiB of memory at once and ≈0.9 GB of junk per track, and after 37
hours `wave` crashed with `struct.error` right from the recording. The metadata file
(`meta.json`) was written only at close: a session cut off by a crash got into neither
the list nor the cleanup, and its space was not counted anywhere.
"""

from __future__ import annotations

import json
import os
import struct
import time
import wave
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from test_session import NS, STARTED, chunk, frame, wav_samples

from argus.core.config import RecordConfig
from argus.core.events import SAMPLE_RATE, AudioChunk, Source
from argus.session import recorder as recorder_module
from argus.session.layout import WAV_NAMES, find_session, list_sessions, prune_sessions
from argus.session.recorder import Recorder
from argus.session.replayer import Replayer, _read_audio

ME, THEM = Source.ME, Source.THEM


# --------------------------------------------------------------------------
# A gap is written in blocks
# --------------------------------------------------------------------------


def test_a_long_gap_is_written_in_small_blocks_not_one_allocation(tmp_path, monkeypatch):
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    track = rec._tracks[ME]
    sizes: list[int] = []
    real = track._wav.writeframes

    def spy(data):
        sizes.append(len(data))
        return real(data)

    monkeypatch.setattr(track._wav, "writeframes", spy)
    rec.feed_audio(chunk(0, ME, 1600))
    rec.feed_audio(chunk(250 * NS, ME, 1600))       # four minutes of silence between the chunks
    meta = rec.close()

    block_bytes = recorder_module.SILENCE_BLOCK_SAMPLES * 2
    assert max(sizes) <= block_bytes, "the silence went out as one piece for the whole gap"
    assert sum(sizes) == (250 * SAMPLE_RATE + 1600) * 2
    data = wav_samples(tmp_path / "s" / WAV_NAMES[ME])
    assert len(data) == 250 * SAMPLE_RATE + 1600
    assert np.all(data[1600 : 250 * SAMPLE_RATE] == 0) and np.any(data[250 * SAMPLE_RATE :] != 0)
    assert meta.silence_filled_ms["me"] == pytest.approx((250 * SAMPLE_RATE - 1600) / 16, abs=1)


def test_the_tail_padding_is_written_in_blocks_too(tmp_path, monkeypatch):
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    sizes: list[int] = []
    track = rec._tracks[THEM]
    real = track._wav.writeframes
    monkeypatch.setattr(track._wav, "writeframes", lambda d: (sizes.append(len(d)), real(d))[1])
    rec.feed_audio(chunk(0, ME, 1600))
    rec.feed_audio(chunk(200 * NS, ME, 1600))        # THEM is silent for all 200 seconds
    rec.close()

    assert sizes and max(sizes) <= recorder_module.SILENCE_BLOCK_SAMPLES * 2
    assert len(wav_samples(tmp_path / "s" / WAV_NAMES[THEM])) == 200 * SAMPLE_RATE + 1600


# --------------------------------------------------------------------------
# Idle time of the whole recording is cut out, real silence of one channel is not
# --------------------------------------------------------------------------


def test_a_sleep_of_hours_does_not_become_hours_of_zeros(tmp_path):
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=30), STARTED)
    for src in (ME, THEM):
        rec.feed_audio(chunk(0, src, 1600))
    rec.feed_frame(frame(0, 0))
    eight_hours = 8 * 3600 * NS
    rec.feed_audio(chunk(eight_hours, ME, 1600))      # the computer woke up
    rec.feed_audio(chunk(eight_hours, THEM, 1600))
    rec.feed_frame(frame(eight_hours + NS, 1))
    meta = rec.close()

    keep = recorder_module.KEEP_IDLE_NS / NS
    for src in (ME, THEM):
        samples = wav_samples(tmp_path / "s" / WAV_NAMES[src])
        # The sleep stays as a span of a couple of seconds, not 460 MB of zeros per track; both
        # tracks reach the last frame (0.1 s of audio + 2 s in place of the sleep + one second up
        # to the frame).
        assert len(samples) == pytest.approx((0.1 + keep + 1.0) * SAMPLE_RATE, abs=SAMPLE_RATE // 20)
    assert meta.idle_skipped_ms == pytest.approx(8 * 3600 * 1000 - keep * 1000 - 100, rel=1e-3)
    # Frames live on the same scale as the audio: "position = time" holds between them too.
    times = [json.loads(row)["t"] for row in (tmp_path / "s" / "frames.jsonl").read_text("utf-8").split("\n") if row]
    assert times[0] == 0 and times[1] == pytest.approx((keep + 1) * NS, abs=NS // 5)
    assert meta.duration_ns == pytest.approx((keep + 1) * NS, abs=NS // 2)


def test_a_frame_after_the_sleep_is_enough_to_cut_it(tmp_path):
    """The first thing after sleep may be a frame rather than audio: the time offset is shifted
    in that case too.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=30), STARTED)
    rec.feed_audio(chunk(0, ME, 1600))
    rec.feed_frame(frame(3 * 3600 * NS, 0))
    rec.feed_audio(chunk(3 * 3600 * NS + NS // 2, ME, 1600))
    meta = rec.close()

    me = wav_samples(tmp_path / "s" / WAV_NAMES[ME])
    assert len(me) < 10 * SAMPLE_RATE, "three hours of sleep went into the file as silence"
    assert meta.idle_skipped_ms > 3 * 3600 * 1000 - 5000


def test_a_silent_loopback_beside_a_live_mic_is_still_filled_in_full(tmp_path):
    """A silent loopback beside a live microphone is not idle: the silence is real, and the
    tracks have the same length.
    """
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for second in range(700):
        rec.feed_audio(chunk(second * NS, ME, SAMPLE_RATE))      # the microphone runs the whole time
    rec.feed_audio(chunk(650 * NS, THEM, 1600))                  # the speakers started talking at the 650th second
    meta = rec.close()

    them = wav_samples(tmp_path / "s" / WAV_NAMES[THEM])
    me = wav_samples(tmp_path / "s" / WAV_NAMES[ME])
    assert len(them) == len(me)
    assert meta.idle_skipped_ms == 0
    assert np.all(them[: 650 * SAMPLE_RATE] == 0)


def test_a_normal_pause_is_untouched(tmp_path):
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, ME, 1600))
    rec.feed_audio(chunk(120 * NS, ME, 1600))        # two minutes is not a sleep yet
    meta = rec.close()

    assert meta.idle_skipped_ms == 0
    assert len(wav_samples(tmp_path / "s" / WAV_NAMES[ME])) == 120 * SAMPLE_RATE + 1600


# --------------------------------------------------------------------------
# Failures do not take away the metadata file and do not stop intake
# --------------------------------------------------------------------------


def test_a_dead_track_is_logged_once_and_the_pump_keeps_living(tmp_path, monkeypatch, caplog):
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)

    def full_disk(chunk, origin_ns=0):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rec._tracks[ME], "write", full_disk)
    with caplog.at_level("ERROR"):
        for i in range(30):
            rec.feed_audio(chunk(i * 100_000_000, ME, 1600))     # does not throw: the audio pump does not die
            rec.feed_audio(chunk(i * 100_000_000, THEM, 1600))
    meta = rec.close()

    assert sum("track recording stopped" in r.getMessage() for r in caplog.records) == 1
    assert meta.chunks == {"me": 0, "them": 30}
    assert any("me" in p for p in meta.problems)
    assert len(wav_samples(tmp_path / "s" / WAV_NAMES[THEM])) == 30 * 1600


def test_close_writes_the_passport_even_when_the_tail_cannot_be_written(tmp_path, monkeypatch):
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, ME, 1600))

    def broken(self, t_ns):
        raise struct.error("argument out of range")      # as at the 37th hour of recording

    monkeypatch.setattr(recorder_module._Track, "pad_to", broken)
    meta = rec.close()                                  # before, an exception, and no metadata file

    assert meta.finished
    assert [p.name for p, _ in list_sessions(tmp_path)] == ["s"]
    assert any("was not written to the end" in p for p in meta.problems)


def test_a_track_stops_at_the_wav_size_limit_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_module, "WAV_MAX_SAMPLES", 4000)
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    for i in range(4):
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, ME, 1600))
    meta = rec.close()

    assert rec._tracks[ME].full
    assert meta.chunks["me"] == 2                       # 3200 fit, the third chunk is already past the limit
    assert len(wav_samples(tmp_path / "s" / WAV_NAMES[ME])) == 3200


# --------------------------------------------------------------------------
# Metadata of a live recording: a cut-off session can be found
# --------------------------------------------------------------------------


def test_a_session_that_was_never_closed_is_still_found_and_readable(tmp_path):
    """The process was killed in the middle of recording: the manifest exists from the first
    second, and replay opens the audio.
    """
    rec = Recorder(tmp_path / "2026-08-28_1200-00", RecordConfig(), STARTED)
    for i in range(10):
        rec.feed_audio(chunk(i * 1600 * NS // SAMPLE_RATE, ME, 1600))
    try:
        found = find_session(tmp_path, "1200")
        (_, meta), = list_sessions(tmp_path)
        assert found.name == "2026-08-28_1200-00" and meta.finished is False
        replayed = sum(len(c.samples) for c in _read_audio(found / "me.wav", ME, 100))
        assert replayed == 10 * 1600
    finally:
        rec.close()


def test_the_passport_is_refreshed_while_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_module, "META_REFRESH_S", 0.0)
    rec = Recorder(tmp_path / "s", RecordConfig(), STARTED)
    rec.feed_audio(chunk(0, ME, 1600))
    rec.feed_audio(chunk(NS, ME, 1600))
    try:
        data = json.loads((tmp_path / "s" / "meta.json").read_text("utf-8"))
        assert data["finished"] is False and data["chunks"]["me"] == 2
        assert data["duration_ns"] >= NS
    finally:
        rec.close()
    assert json.loads((tmp_path / "s" / "meta.json").read_text("utf-8"))["finished"] is True


def test_an_old_passport_without_the_new_fields_reads_as_closed(tmp_path):
    folder = tmp_path / "2026-08-28_1200-00"
    folder.mkdir()
    (folder / "meta.json").write_text(
        json.dumps({"version": 1, "started_at": STARTED.isoformat(), "duration_ns": 5}), "utf-8",
    )

    (_, meta), = list_sessions(tmp_path)

    assert meta.finished is True and meta.idle_skipped_ms == 0 and meta.problems == []


# --------------------------------------------------------------------------
# Cleanup also sees abandoned recordings
# --------------------------------------------------------------------------


def _orphan(root, name: str, mb: float, age_s: float):
    """A recording folder without a manifest, as after a process crash."""
    folder = root / name
    folder.mkdir()
    (folder / "me.wav").write_bytes(b"\0" * int(mb * 2**20))
    stamp = time.time() - age_s
    os.utime(folder / "me.wav", (stamp, stamp))
    return folder


def _session(root, name: str, mb: float):
    from test_session import _make_session

    return _make_session(root, name, mb)


def test_prune_counts_and_removes_a_session_that_has_no_passport(tmp_path):
    _orphan(tmp_path, "2026-08-27_1000-00", 6, age_s=7200)       # old, cut off
    _session(tmp_path, "2026-08-28_1200-00", 6)
    _session(tmp_path, "2026-08-28_1300-00", 6)

    removed = prune_sessions(tmp_path, keep_gb=14 / 1024)

    assert [p.name for p, _ in removed] == ["2026-08-27_1000-00"]
    assert sorted(p.name for p in tmp_path.glob("*")) == ["2026-08-28_1200-00", "2026-08-28_1300-00"]


def test_an_orphan_takes_part_in_the_budget_so_real_sessions_are_not_spared_for_it(tmp_path):
    _session(tmp_path, "2026-08-28_1200-00", 6)
    _orphan(tmp_path, "2026-08-28_1230-00", 8, age_s=7200)
    _session(tmp_path, "2026-08-28_1300-00", 6)

    removed = prune_sessions(tmp_path, keep_gb=14 / 1024)        # ~14 MB of 20

    assert "2026-08-28_1200-00" in [p.name for p, _ in removed]
    assert (tmp_path / "2026-08-28_1300-00").is_dir()            # the newest real one is still in place
    total = sum(f.stat().st_size for f in tmp_path.rglob("*") if f.is_file())
    assert total <= 14 * 2**20, "the abandoned recording is not counted in the ceiling"
    assert not (tmp_path / "2026-08-28_1230-00").exists()


def test_a_fresh_folder_without_a_passport_is_not_deleted_under_a_live_writer(tmp_path):
    _session(tmp_path, "2026-08-28_1200-00", 6)
    now = datetime.now(UTC)
    live = _orphan(tmp_path, (now - timedelta(seconds=30)).strftime("%Y-%m-%d_%H%M-%S"), 6, age_s=5)

    removed = prune_sessions(tmp_path, keep_gb=1 / 1024)         # budget 1 MB: everything is excess

    assert live.is_dir(), "the folder that is still being written to was erased"
    assert [p.name for p, _ in removed] == ["2026-08-28_1200-00"]


def test_prune_ignores_the_sample_folder_and_folders_without_a_recording(tmp_path):
    _session(tmp_path, "2026-08-28_1200-00", 6)
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "me.wav").write_bytes(b"\0" * 2**20)
    (tmp_path / "foreign").mkdir()
    (tmp_path / "foreign" / "notes.txt").write_text("not a recording", "utf-8")

    removed = prune_sessions(tmp_path, keep_gb=1 / 1024)

    assert [p.name for p, _ in removed] == ["2026-08-28_1200-00"]
    assert (tmp_path / "sample" / "me.wav").exists() and (tmp_path / "foreign" / "notes.txt").exists()


# --------------------------------------------------------------------------
# Replay does not crash on a truncated tail and does not trust any format
# --------------------------------------------------------------------------


def test_replay_survives_a_truncated_last_frame_line(tmp_path):
    rec = Recorder(tmp_path / "s", RecordConfig(frames_fps=30), STARTED)
    for i in range(3):
        rec.feed_frame(frame(i * NS // 10, i))
    rec.feed_audio(chunk(0, ME, 1600))
    rec.close()
    with (tmp_path / "s" / "frames.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"t": 300000000, "se')                 # the writer did not finish the line

    events = list(Replayer(tmp_path / "s", None, speed=0)._timeline())     # type: ignore[arg-type]

    assert sum(isinstance(e, AudioChunk) for e in events) >= 1
    assert len([e for e in events if not isinstance(e, AudioChunk)]) == 3


def test_replay_refuses_a_wav_in_the_wrong_format_with_a_clear_reason(tmp_path):
    bad = tmp_path / "me.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48_000)
        w.writeframes(bytes(4000))

    with pytest.raises(ValueError, match="expected mono"):
        list(_read_audio(bad, ME, 32))
