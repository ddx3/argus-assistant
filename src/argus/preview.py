"""Live capture monitor, acceptance step M1.

Shows in the terminal that both audio channels and the screen really work, and that they
work independently of each other. This is not the product's UI but an instrument: the
real dashboard will come at M7.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from rich.live import Live
from rich.table import Table

from argus.capture.backend import AudioCapture
from argus.capture.screen import ScreenCapture
from argus.console import console
from argus.core.bus import Bus
from argus.core.clock import LiveClock
from argus.core.config import Config
from argus.core.events import AudioChunk, Frame, Source

BAR_WIDTH = 24
# Below -60 dB we count it as silence: conversational speech runs from about -35 to -10 dB,
# and a linear scale would show it as a barely twitching zero.
FLOOR_DB = -60.0
# How long the peak mark takes to slide back down to the current level.
PEAK_DECAY_PER_SEC = 1.2


def _db(rms: float) -> float:
    return FLOOR_DB if rms <= 1e-9 else max(FLOOR_DB, 20.0 * math.log10(rms))


def _bar(level: float, peak: float) -> str:
    filled = round(level * BAR_WIDTH)
    peak_pos = min(BAR_WIDTH - 1, round(peak * BAR_WIDTH))
    cells = []
    for i in range(BAR_WIDTH):
        if i == peak_pos and peak > 0.01:
            cells.append("[bold yellow]|[/bold yellow]")
        elif i < filled:
            cells.append("[green]█[/green]" if i < BAR_WIDTH * 0.8 else "[red]█[/red]")
        else:
            cells.append("[dim]·[/dim]")
    return "".join(cells)


@dataclass
class _Meter:
    level: float = 0.0
    peak: float = 0.0
    chunks: int = 0
    samples: int = 0
    last_rms: float = 0.0

    def feed(self, samples: np.ndarray) -> None:
        x = samples.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        self.last_rms = rms
        self.level = (_db(rms) - FLOOR_DB) / -FLOOR_DB
        self.peak = max(self.peak, self.level)
        self.chunks += 1
        self.samples += len(samples)

    def decay(self, dt: float) -> None:
        self.peak = max(self.level, self.peak - PEAK_DECAY_PER_SEC * dt)

    @property
    def seconds(self) -> float:
        from argus.core.events import SAMPLE_RATE

        return self.samples / SAMPLE_RATE


@dataclass
class _State:
    meters: dict[Source, _Meter] = field(default_factory=lambda: {
        Source.ME: _Meter(),
        Source.THEM: _Meter(),
    })
    frames: int = 0
    fps: float = 0.0
    last_frame: np.ndarray | None = None
    size: tuple[int, int] | None = None
    recorder: object | None = None
    session_dir: Path | None = None


def _render(
    state: _State, audio: AudioCapture, screen: ScreenCapture, elapsed: float,
    with_screen: bool = True,
) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(width=7)
    table.add_column(width=BAR_WIDTH)
    table.add_column(justify="right", width=10)
    table.add_column(justify="right", width=22)

    labels = {Source.ME: "MICROPHONE", Source.THEM: "SYSTEM"}
    for src in (Source.ME, Source.THEM):
        m = state.meters[src]
        table.add_row(
            f"[bold]{labels[src]}[/bold]",
            _bar(m.level, m.peak),
            f"{_db(m.last_rms):6.1f} dB",
            f"{m.seconds:6.1f} s of audio",
        )

    hint = ""
    if screen.polls and not state.frames:
        hint = "screen unchanged"
    if with_screen:
        table.add_row(
            "[bold]SCREEN[/bold]",
            f"{state.size[0]}x{state.size[1]}" if state.size else "[dim]waiting for a frame[/dim]",
            f"{state.fps:5.1f} fps",
            f"{state.frames} frames {hint}",
        )

    if state.recorder is not None and state.session_dir is not None:
        from argus.session.layout import dir_size

        rec = state.recorder
        size_mb = dir_size(state.session_dir) / 2**20
        note = f"{rec.frames_written} frames"
        if rec.dropped_frames:
            note += f", [yellow]{rec.dropped_frames} dropped[/yellow]"
        table.add_row("[bold]RECORDING[/bold]", state.session_dir.name, f"{size_mb:6.1f} MB", note)

    problems = []
    if not audio.healthy:
        problems.append(f"audio: {audio.first_error() or 'stream stopped'}")
    if with_screen and not screen.alive:
        problems.append(f"screen: {screen.error or 'stream stopped'}")
    table.add_row("", "", "", f"[dim]{elapsed:5.1f} s[/dim]")
    if problems:
        table.add_row("[red]FAILURE[/red]", "[red]" + "; ".join(problems) + "[/red]", "", "")
    return table


def _live_console():
    """A real `rich.console.Console` for `Live`.

    `argus.console.console` creates it lazily and exposes neither `with` nor the internal
    fields, and `Live` does not work without them: `argus record` crashed on the very first
    redraw. `rich` is already imported here (at the top of the file), so laziness saves
    nothing.
    """
    from rich.console import Console

    return Console()


async def run_capture(
    cfg: Config,
    seconds: float,
    snapshot: Path | None,
    record: bool = False,
    with_screen: bool = True,
) -> int:
    """Run the capture and show the levels. Returns the exit code.

    `with_screen=False` records audio only. Frames are the main cost of a recording: on a
    changing picture ten minutes give more than a gigabyte, and when the recording is needed
    for the speech, there is no reason to pay for it with disk space.
    """
    loop = asyncio.get_running_loop()
    clock = LiveClock()
    bus = Bus(loop)

    audio = AudioCapture(bus, clock, cfg.audio)
    screen = ScreenCapture(bus, clock, cfg.screen)

    mic_dev, loop_dev = audio.open()
    console.print(f"  microphone : [bold]{mic_dev.name}[/bold]  {mic_dev.rate} Hz, {mic_dev.channels} ch.")
    console.print(f"  system     : [bold]{loop_dev.name}[/bold]  {loop_dev.rate} Hz, {loop_dev.channels} ch.")
    if snapshot:
        console.print(f"  snapshot   : {snapshot} [dim](updated once a second)[/dim]")
    console.print("\n  [dim]Ctrl+C to stop[/dim]\n")
    if with_screen:
        screen.open()

    state = _State()
    recorder = None
    if record:
        from argus.session.layout import session_id
        from argus.session.recorder import Recorder

        state.session_dir = cfg.sessions_dir / session_id(clock.started_at)
        recorder = Recorder(state.session_dir, cfg.record, clock.started_at)
        state.recorder = recorder
        console.print(f"  recording  : [bold]{state.session_dir}[/bold]")

    a_sub = bus.subscribe(AudioChunk, maxsize=256, name="audio")
    f_sub = bus.subscribe(Frame, maxsize=3, name="frames") if with_screen else None

    stop = asyncio.Event()

    async def pump_audio() -> None:
        async for chunk in a_sub:
            state.meters[chunk.source].feed(chunk.samples)
            if recorder is not None:
                recorder.feed_audio(chunk)

    async def pump_frames() -> None:
        if f_sub is None:
            return
        async for frame in f_sub:
            state.frames += 1
            state.last_frame = frame.image
            state.size = frame.shape[1], frame.shape[0]
            if recorder is not None:
                recorder.feed_frame(frame)

    async def snap() -> None:
        if snapshot is None:
            return
        import cv2

        snapshot.parent.mkdir(parents=True, exist_ok=True)
        while not stop.is_set():
            await asyncio.sleep(1.0)
            if state.last_frame is not None:
                cv2.imwrite(str(snapshot), state.last_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

    tasks = [
        asyncio.create_task(pump_audio()),
        asyncio.create_task(pump_frames()),
        asyncio.create_task(snap()),
    ]

    t0 = loop.time()
    last = t0
    frames_at_last = 0
    exit_code = 0

    try:
        with Live(console=_live_console(), refresh_per_second=12, transient=False) as live:
            while not stop.is_set():
                await asyncio.sleep(1 / 12)
                now = loop.time()
                dt = now - last
                last = now

                if dt > 0:
                    state.fps = 0.7 * state.fps + 0.3 * ((state.frames - frames_at_last) / dt)
                frames_at_last = state.frames
                for m in state.meters.values():
                    m.decay(dt)

                live.update(_render(state, audio, screen, now - t0, with_screen))

                # We check the screen only if it was opened: with `--no-screen` there is no stream, and
                # "not alive" would be a false alarm; the recording would break off on the very first step.
                if not audio.healthy or (with_screen and not screen.alive):
                    exit_code = 1
                    break
                if seconds and now - t0 >= seconds:
                    break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        screen.close()
        audio.close()

    console.print()
    _summary(state, screen, bus, with_screen)

    if recorder is not None:
        from argus.capture.audio import DeviceInfo
        from argus.session.layout import dir_size

        def meta_of(d: DeviceInfo):
            from argus.session.layout import DeviceMeta

            return DeviceMeta(name=d.name, rate=d.rate, channels=d.channels)

        meta = recorder.close(mic=meta_of(mic_dev), loopback=meta_of(loop_dev))
        size_mb = dir_size(recorder.path) / 2**20
        console.print(f"  recording  : {recorder.path}")
        console.print(f"               {meta.duration_s:.1f} s, {meta.frames} frames, {size_mb:.1f} MB")
        silence = {k: v for k, v in meta.silence_filled_ms.items() if v > 0}
        if silence:
            filled = ", ".join(f"{k}: {v:.0f} ms" for k, v in silence.items())
            console.print(f"               silence added to fill gaps - {filled}")
        else:
            console.print("               no gaps in the stream")
        console.print()

    return exit_code


def _summary(state: _State, screen: ScreenCapture, bus: Bus, with_screen: bool = True) -> None:
    me, them = state.meters[Source.ME], state.meters[Source.THEM]
    console.print(f"  microphone : {me.chunks} chunks, {me.seconds:.1f} s of audio")
    console.print(f"  system     : {them.chunks} chunks, {them.seconds:.1f} s of audio")
    if with_screen:
        console.print(f"  screen     : {state.frames} frames from {screen.polls} polls")
    dropped = {k: v["dropped"] for k, v in bus.stats().items() if v["dropped"]}
    if dropped:
        console.print(f"  [yellow]losses on the bus: {dropped}[/yellow]")
    else:
        console.print("  no losses on the bus")
    console.print()
