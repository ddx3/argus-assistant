"""The console without an extra import, and `argus listen` / `argus transcribe`, which
crashed and stayed silent.

`console` creates the real `rich.Console` lazily, but it could not be passed to `Live`
(no `with`), and recognition text with "[laughs]" turned into markup.
"""

from __future__ import annotations

import asyncio
import copy
import io
import subprocess
import sys

import pytest
from rich.console import Console
from rich.live import Live
from rich.text import Text

from argus import listen
from argus.console import _LazyConsole
from argus.core.config import Config
from argus.core.events import Source, SpeechEvent

NS = 1_000_000_000


def lazy_over(buffer: io.StringIO) -> _LazyConsole:
    console = _LazyConsole()
    console._real = Console(file=buffer, force_terminal=False, width=100)
    return console


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------


def test_live_accepts_the_lazy_console():
    """Previously: `TypeError` on `with console:` — `argus listen` could not show a single
    frame.
    """
    out = io.StringIO()
    with Live(console=lazy_over(out), refresh_per_second=4) as live:
        live.update(Text("hello"))
    assert "hello" in out.getvalue()


def test_the_lazy_console_is_a_context_manager_and_forwards_private_names():
    console = lazy_over(io.StringIO())
    with console:
        console.print("ok")
    assert console._lock is console._real._lock            # a single underscore goes to the real one


def test_dunder_probes_do_not_build_the_real_console():
    console = _LazyConsole()
    with pytest.raises(AttributeError):
        console.__wrapped__                                  # noqa: B018 -- this is how inspect asks
    copy.copy(console)
    copy.deepcopy(console)
    assert console._real is None


def test_importing_and_copying_the_console_does_not_import_rich():
    code = (
        "import copy, sys\n"
        "from argus.console import console\n"
        "before = 'rich.console' in sys.modules\n"
        "copy.copy(console)\n"
        "print(before, 'rich.console' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "False"]


# --------------------------------------------------------------------------
# argus transcribe
# --------------------------------------------------------------------------


async def test_transcribe_prints_bracketed_words_and_does_not_crash(monkeypatch):
    """"[laughs]" used to disappear, "[/x]" crashed — already after the paid recognition."""
    out = io.StringIO()
    monkeypatch.setattr(listen, "console", lazy_over(out))

    async def fake(wav, cfg):
        return [SpeechEvent(t=0, t_end=2 * NS, source=Source.ME,
                            text="he said [laughs] and [/x] then [work]", language="en")]

    monkeypatch.setattr("argus.perception.speech.transcribe_file", fake)
    from pathlib import Path

    code = await listen.run_transcribe(Config(), Path("C:/folder [1]/sound.wav"))

    text = out.getvalue()
    assert code == 0
    assert "[laughs]" in text and "[/x]" in text and "[work]" in text
    assert "folder [1]" in text


# --------------------------------------------------------------------------
# argus listen
# --------------------------------------------------------------------------


class _Device:
    def __init__(self, name):
        self.name = name


class _Audio:
    """Audio that dies on the very first step — like a pulled-out headset."""

    healthy = False
    closed = False

    def __init__(self, bus, clock, cfg):
        pass

    def open(self):
        return _Device("Microphone [USB]"), _Device("Speakers")

    def first_error(self):
        return None if self.closed else OSError("[Errno -9999] Unanticipated host error")

    def close(self):
        self.closed = True


class _Pipeline:
    utterances = transcribed = dropped = 0
    stt = object()

    def __init__(self, bus, cfg):
        pass

    async def warmup(self):
        pass

    async def run(self):
        await asyncio.sleep(3600)

    async def drain(self):
        pass

    def close(self):
        pass


async def test_listen_reports_a_dead_audio_stream_and_fails(monkeypatch):
    """Previously: a silent end and code 0 — a failure could not be told apart from an ordinary
    stop.
    """
    out = io.StringIO()
    monkeypatch.setattr(listen, "console", lazy_over(out))
    monkeypatch.setattr(listen, "AudioCapture", _Audio)
    monkeypatch.setattr(listen, "SpeechPipeline", _Pipeline)

    code = await listen.run_listen(Config(), seconds=5, with_screen=False)

    text = out.getvalue()
    assert code == 1
    assert "FAILURE: audio:" in text
    assert "Errno" not in text                      # the raw error text does not go to the screen
    assert "Microphone [USB]" in text                 # the brackets in the device name are intact
