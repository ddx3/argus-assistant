"""Checks of the foundation: clock, events, bus, config."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import UTC, datetime

import numpy as np
import pytest
from pydantic import ValidationError

from argus.core.bus import Bus
from argus.core.clock import LiveClock, ReplayClock
from argus.core.config import Config
from argus.core.events import (
    SAMPLE_RATE,
    AudioChunk,
    Frame,
    ScreenBlock,
    ScreenEvent,
    Source,
    SpeechEvent,
)

# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------


def test_live_clock_monotonic():
    """Time goes only forward."""
    c = LiveClock()
    marks = [c.now() for _ in range(200)]
    assert marks == sorted(marks)
    assert marks[0] >= 0


def test_live_clock_wall_anchor():
    """Wall time is tied to the start and is converted back."""
    c = LiveClock()
    started = c.started_at
    assert started.tzinfo is UTC
    one_sec_later = c.to_wall(1_000_000_000)
    assert abs((one_sec_later - started).total_seconds() - 1.0) < 0.01


def test_replay_clock_is_driven_externally():
    """The replay clock stands still until it is moved."""
    c = ReplayClock(datetime(2026, 8, 28, tzinfo=UTC))
    assert c.now() == 0
    c.advance_to(5_000_000_000)
    assert c.now() == 5_000_000_000
    assert c.to_wall(0).year == 2026


def test_replay_clock_refuses_to_go_back():
    """Time going backward is a mixed-up event order, we catch it at once."""
    c = ReplayClock(datetime(2026, 8, 28, tzinfo=UTC))
    c.advance_to(1000)
    with pytest.raises(ValueError):
        c.advance_to(999)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def test_audio_chunk_duration():
    """The duration is computed from the number of samples, not stored separately."""
    chunk = AudioChunk(t=0, source=Source.ME, samples=np.zeros(SAMPLE_RATE, dtype=np.int16))
    assert chunk.duration_ns == 1_000_000_000
    assert chunk.t_end == 1_000_000_000


def test_frame_does_not_copy_image():
    """A frame holds a reference to the array: copying 14 MB is not allowed."""
    img = np.zeros((4, 6, 3), dtype=np.uint8)
    frame = Frame(t=0, image=img)
    assert frame.image is img
    assert frame.shape == (4, 6)


def test_screen_event_joins_text():
    ev = ScreenEvent(
        t=0,
        blocks=[
            ScreenBlock(text="Question 1", box=(0, 0, 10, 10)),
            ScreenBlock(text="Option A", box=(0, 20, 10, 10)),
        ],
    )
    assert ev.text == "Question 1\nOption A"


def test_speech_event_serialises():
    """Semantic events must survive being written to disk."""
    ev = SpeechEvent(t=0, t_end=2_000_000_000, source=Source.THEM, text="hello")
    restored = SpeechEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev
    assert restored.duration_ms == 2000
    assert restored.source is Source.THEM


# --------------------------------------------------------------------------
# Bus
# --------------------------------------------------------------------------


async def test_bus_delivers_by_type():
    """A subscriber receives only its own event type."""
    bus = Bus(asyncio.get_running_loop())
    audio = bus.subscribe(AudioChunk)
    frames = bus.subscribe(Frame)

    bus.publish(AudioChunk(t=1, source=Source.ME, samples=np.zeros(4, dtype=np.int16)))
    bus.publish(Frame(t=2, image=np.zeros((2, 2, 3), dtype=np.uint8)))

    assert (await asyncio.wait_for(audio.get(), 1)).t == 1
    assert (await asyncio.wait_for(frames.get(), 1)).t == 2
    assert audio.qsize() == 0


async def test_bus_drops_oldest_when_full():
    """Overflow drops the oldest and is counted, rather than blocking the publisher."""
    bus = Bus(asyncio.get_running_loop())
    sub = bus.subscribe(Frame, maxsize=3)

    for i in range(10):
        bus.publish(Frame(t=i, image=np.zeros((1, 1, 3), dtype=np.uint8), seq=i))

    assert sub.dropped == 7
    assert sub.qsize() == 3
    # The freshest ones are left in the queue.
    remaining = [(await sub.get()).seq for _ in range(3)]
    assert remaining == [7, 8, 9]


async def test_bus_publish_from_thread():
    """Capture lives in another thread — publishing from there must work."""
    loop = asyncio.get_running_loop()
    bus = Bus()
    bus.bind(loop)
    sub = bus.subscribe(AudioChunk)

    def capture_thread():
        for i in range(5):
            bus.publish_threadsafe(
                AudioChunk(t=i, source=Source.THEM, samples=np.zeros(2, dtype=np.int16))
            )

    t = threading.Thread(target=capture_thread)
    t.start()
    t.join()

    got = [(await asyncio.wait_for(sub.get(), 1)).t for _ in range(5)]
    assert got == [0, 1, 2, 3, 4]


async def test_bus_threadsafe_requires_bind():
    """Without binding to the loop, publishing from a thread is an error, not a silent swallow."""
    bus = Bus()
    with pytest.raises(RuntimeError):
        bus.publish_threadsafe(Frame(t=0, image=np.zeros((1, 1, 3), dtype=np.uint8)))


async def test_bus_stats_report_drops():
    bus = Bus(asyncio.get_running_loop())
    bus.subscribe(Frame, maxsize=1, name="screen")
    for i in range(4):
        bus.publish(Frame(t=i, image=np.zeros((1, 1, 3), dtype=np.uint8)))
    assert bus.stats()["screen"] == {"received": 1, "dropped": 3, "queued": 1}


async def test_unsubscribe_stops_delivery():
    bus = Bus(asyncio.get_running_loop())
    sub = bus.subscribe(Frame)
    sub.close()
    bus.publish(Frame(t=0, image=np.zeros((1, 1, 3), dtype=np.uint8)))
    assert sub.qsize() == 0


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_defaults_without_file():
    cfg = Config.load(None)
    assert cfg.audio.chunk_ms == 32
    assert cfg.screen.fps == 30


def test_config_reads_toml(tmp_path):
    (tmp_path / "argus.toml").write_text(
        '[audio]\nchunk_ms = 50\n\n[screen]\nfps = 5\noutput_idx = 1\n',
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path / "argus.toml")
    assert cfg.audio.chunk_ms == 50
    assert cfg.screen.fps == 5
    assert cfg.screen.output_idx == 1
    # What is not in the file is taken from the defaults.
    assert cfg.audio.downmix_average is True


def test_config_resolves_paths_from_project_root(tmp_path):
    (tmp_path / "argus.toml").write_text('[paths]\nsessions = "rec"\n', encoding="utf-8")
    cfg = Config.load(tmp_path / "argus.toml")
    assert cfg.sessions_dir == tmp_path / "rec"


def test_config_rejects_nonsense(tmp_path):
    """A plainly wrong value must fail at load, not surface at M1."""
    (tmp_path / "argus.toml").write_text("[screen]\nfps = 999\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        Config.load(tmp_path / "argus.toml")


# --------------------------------------------------------------------------
# Keys from .env
# --------------------------------------------------------------------------


def test_env_file_fills_missing_variables(tmp_path, monkeypatch):
    """The key must be picked up on its own, without a variable in the terminal.

    Otherwise it lives in one window, is forgotten after a reboot, and its absence looks not
    like "no key" but like "the comparison somehow does not work".
    """
    from argus.core.config import load_env

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")

    taken = load_env(tmp_path / ".env")
    assert taken == {"OPENAI_API_KEY": "sk-from-file"}
    assert os.environ["OPENAI_API_KEY"] == "sk-from-file"


def test_environment_beats_the_file(tmp_path, monkeypatch):
    """A variable that is set beats the file.

    The reverse order would mean that the file silently overrides a key the person has just
    typed by hand, and debugging "why is it the wrong key" would start from the least
    obvious place.
    """
    from argus.core.config import load_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-terminal")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")

    assert load_env(tmp_path / ".env") == {}
    assert os.environ["OPENAI_API_KEY"] == "sk-from-terminal"


def test_env_file_ignores_comments_and_junk(tmp_path, monkeypatch):
    from argus.core.config import load_env

    monkeypatch.delenv("ARGUS_TEST_A", raising=False)
    monkeypatch.delenv("ARGUS_TEST_B", raising=False)
    (tmp_path / ".env").write_text(
        "# comment\n"
        "\n"
        "line without an equals sign\n"
        'ARGUS_TEST_A="in quotes"\n'
        "  ARGUS_TEST_B = with spaces  \n",
        encoding="utf-8",
    )

    taken = load_env(tmp_path / ".env")
    assert taken == {"ARGUS_TEST_A": "in quotes", "ARGUS_TEST_B": "with spaces"}


def test_missing_env_file_is_not_an_error(tmp_path):
    """A missing file is the usual state, not a breakage."""
    from argus.core.config import load_env

    assert load_env(tmp_path / "no-such-file") == {}
