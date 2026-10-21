"""Live speech recognition, acceptance step M3.

Shows the same as the capture monitor, plus a feed of utterances as they appear. It is the
first screen where sound turns into meaning.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Group
from rich.live import Live
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from argus.capture.backend import AudioCapture
from argus.capture.screen import ScreenCapture
from argus.console import console
from argus.core import friendly
from argus.core.bus import Bus
from argus.core.clock import LiveClock
from argus.core.config import Config
from argus.core.events import AudioChunk, Frame, ScreenEvent, Source, SpeechEvent
from argus.perception.speech import SpeechPipeline
from argus.perception.vision_pipeline import VisionPipeline
from argus.preview import _bar, _db, _Meter

LABELS = {Source.ME: ("Me", "cyan"), Source.THEM: ("Them", "magenta")}


@dataclass
class _View:
    meters: dict[Source, _Meter] = field(default_factory=lambda: {
        Source.ME: _Meter(),
        Source.THEM: _Meter(),
    })
    lines: deque[SpeechEvent] = field(default_factory=lambda: deque(maxlen=12))
    frames: int = 0
    latency_ms: float = 0.0
    screen: list[str] = field(default_factory=list)
    screen_t: float = 0.0


def _meters(view: _View) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(width=4)
    t.add_column(width=24)
    t.add_column(justify="right", width=10)
    for src in (Source.ME, Source.THEM):
        m = view.meters[src]
        label, color = LABELS[src]
        t.add_row(f"[{color}]{label}[/{color}]", _bar(m.level, m.peak), f"{_db(m.last_rms):6.1f} dB")
    return t


def _transcript(view: _View) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column(width=7, justify="right")
    t.add_column(width=4)
    t.add_column(overflow="fold")
    if not view.lines:
        t.add_row("", "", Text("waiting for speech…", style="dim"))
    for ev in view.lines:
        label, color = LABELS[ev.source]
        conf = f"{ev.confidence:.2f}" if ev.confidence is not None else "  — "
        t.add_row(
            Text(f"{ev.t / 1e9:6.1f}s", style="dim"),
            f"[{color}]{label}[/{color}]",
            Text(ev.text, style="bold" if ev.source is Source.THEM else ""),
        )
        t.add_row("", "", Text(f"confidence {conf}", style="dim"))
    return t


def _screen_panel(view: _View) -> Table:
    """What is visible on the screen right now: the top lines of the recognized text."""
    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column(width=7, justify="right")
    t.add_column(overflow="ellipsis", no_wrap=True)
    if not view.screen:
        t.add_row(Text("SCREEN", style="dim"), Text("no text found yet", style="dim"))
        return t
    t.add_row(
        Text("SCREEN", style="bold yellow"),
        Text(f"{view.screen_t:.1f}s · {len(view.screen)} blocks", style="dim"),
    )
    for line in view.screen[:6]:
        t.add_row("", Text(line, style="yellow"))
    return t


def _status(view: _View, pipe: SpeechPipeline, vis: VisionPipeline | None) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column()
    rtf = getattr(pipe.stt, "realtime_factor", 0.0)
    t.add_row(Text(
        f"speech: utterances {pipe.utterances} · transcribed {pipe.transcribed} · "
        f"dropped {pipe.dropped} · x{rtf:.0f} · latency {view.latency_ms:.0f} ms",
        style="dim"))
    if vis is not None:
        g = vis.gate
        t.add_row(Text(
            f"screen: frames {view.frames} · filter passed {g.passed} of {g.seen} "
            f"({g.pass_rate:.0%}) · reads {vis.events}, {vis.avg_ms:.0f} ms each",
            style="dim"))
    return t


async def run_listen(cfg: Config, seconds: float, with_screen: bool = True) -> int:
    loop = asyncio.get_running_loop()
    clock = LiveClock()
    bus = Bus(loop)

    audio = AudioCapture(bus, clock, cfg.audio)
    screen = ScreenCapture(bus, clock, cfg.screen) if with_screen else None

    mic_dev, loop_dev = audio.open()
    # A device name is foreign text: without escaping, markup would swallow the brackets in
    # "Microphone [USB]".
    console.print(f"\n  microphone : [bold]{escape(mic_dev.name)}[/bold]")
    console.print(f"  system     : [bold]{escape(loop_dev.name)}[/bold]")
    console.print(f"  engine     : [bold]{cfg.stt.provider}[/bold]")

    pipe = SpeechPipeline(bus, cfg)
    vis = VisionPipeline(bus, cfg) if with_screen else None
    with console.status("  warming up the models…"):
        await pipe.warmup()
        if vis is not None:
            await vis.warmup()
    if vis is not None:
        console.print(
            f"  screen     : OCR [bold]{cfg.ocr.rec_lang}[/bold], "
            f"downscaling to {cfg.ocr.max_width} px"
        )
    console.print("  [dim]Ctrl+C to stop[/dim]\n")

    if screen is not None:
        screen.open()

    view = _View()
    a_sub = bus.subscribe(AudioChunk, maxsize=256, name="levels")
    s_sub = bus.subscribe(SpeechEvent, maxsize=64, name="speech-out")
    f_sub = bus.subscribe(Frame, maxsize=3, name="frames") if screen else None
    scr_sub = bus.subscribe(ScreenEvent, maxsize=8, name="screen-out") if vis else None

    async def pump_levels() -> None:
        async for chunk in a_sub:
            view.meters[chunk.source].feed(chunk.samples)

    async def pump_speech() -> None:
        async for ev in s_sub:
            view.lines.append(ev)
            # The delay from the end of the phrase to the text appearing is what the whole pipeline was
            # built the way it is for.
            view.latency_ms = (clock.now() - ev.t_end) / 1e6

    async def pump_frames() -> None:
        async for _ in f_sub:
            view.frames += 1

    async def pump_screen() -> None:
        async for ev in scr_sub:
            # Top to bottom, as on screen: the order of blocks from the detector is arbitrary.
            ordered = sorted(ev.blocks, key=lambda b: (b.box[1], b.box[0]))
            view.screen = [b.text for b in ordered]
            view.screen_t = ev.t / 1e9

    tasks = [
        asyncio.create_task(pipe.run()),
        asyncio.create_task(pump_levels()),
        asyncio.create_task(pump_speech()),
    ]
    if f_sub is not None:
        tasks.append(asyncio.create_task(pump_frames()))
    if vis is not None:
        tasks += [asyncio.create_task(vis.run()), asyncio.create_task(pump_screen())]

    t0 = time.perf_counter()
    last = t0
    audio_failure: str | None = None        # why the audio broke off; None means we stopped it ourselves
    try:
        with Live(console=console, refresh_per_second=10) as live:
            while True:
                await asyncio.sleep(0.1)
                now = time.perf_counter()
                for m in view.meters.values():
                    m.decay(now - last)
                last = now
                live.update(Group(
                    _meters(view), Text(""),
                    _screen_panel(view), Text(""),
                    _transcript(view), Text(""),
                    _status(view, pipe, vis),
                ))
                if not audio.healthy:
                    # We take the cause now: after `audio.close()` the channels are gone.
                    audio_failure = friendly.plain_or(audio.first_error(), "stream stopped")
                    break
                if seconds and now - t0 >= seconds:
                    break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await pipe.drain()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if screen is not None:
            screen.close()
        audio.close()
        pipe.close()
        if vis is not None:
            vis.close()

    console.print()
    console.print(f"  utterances found : {pipe.utterances}")
    console.print(f"  transcribed      : {pipe.transcribed}")
    console.print(f"  dropped          : {pipe.dropped} [dim](noise, too quiet, model hallucinations)[/dim]")
    rtf = getattr(pipe.stt, "realtime_factor", 0.0)
    console.print(f"  STT speed        : x{rtf:.0f} real time")
    if vis is not None:
        g = vis.gate
        console.print(f"  frames seen      : {g.seen}, filter passed {g.passed} ({g.pass_rate:.0%})")
        console.print(f"  screen reads     : {vis.events} at {vis.avg_ms:.0f} ms on average")
    console.print()
    if audio_failure is not None:
        # As in `argus capture`: broken audio is not a normal stop, and a script should see that.
        console.print(f"[red]FAILURE: audio: {escape(audio_failure)}[/red]\n")
        return 1
    return 0


async def run_transcribe(cfg: Config, wav: Path) -> int:
    """Recognize a finished WAV: a quick way to check the model without a microphone."""
    from argus.perception.speech import transcribe_file

    console.print(f"\n  file : [bold]{escape(str(wav))}[/bold]\n")
    events = await transcribe_file(wav, cfg)
    for ev in events:
        # Recognition text is foreign text: tags like "[laughs]" and "[music]" are not markup.
        console.print(f"  [{ev.t / 1e9:5.1f}-{ev.t_end / 1e9:5.1f} s] {escape(ev.text)}")
        confidence = f"{ev.confidence:.2f}" if ev.confidence is not None else "—"
        console.print(
            f"  {'':>16}[dim]confidence {confidence}, language {escape(str(ev.language))}[/dim]")
    console.print(f"\n  utterances: {len(events)}\n")
    return 0
