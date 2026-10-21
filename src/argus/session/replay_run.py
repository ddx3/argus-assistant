"""Running a replay from the command line: acceptance for M2.

It shows what the replay was made for: the recording went through the whole pipeline, the
events arrived in the same order and with the same timestamps, and all of it took seconds
instead of minutes.
"""

from __future__ import annotations

import asyncio

import numpy as np

from argus.console import console
from argus.context.render import estimate_tokens, render_context
from argus.context.timeline import Timeline
from argus.core.bus import Bus
from argus.core.config import Config
from argus.core.events import SAMPLE_RATE, AudioChunk, Frame, ScreenEvent, Source, SpeechEvent
from argus.session.layout import find_session
from argus.session.replayer import Replayer


def _rms_db(total_sq: float, n: int) -> float:
    if n == 0:
        return -99.0
    rms = (total_sq / n) ** 0.5
    return -99.0 if rms <= 1e-9 else 20 * np.log10(rms)


async def run_replay(
    cfg: Config,
    ident: str,
    speed: float,
    with_frames: bool,
    with_stt: bool = False,
    with_ocr: bool = False,
) -> None:
    path = find_session(cfg.sessions_dir, ident)
    loop = asyncio.get_running_loop()
    bus = Bus(loop)
    player = Replayer(
        path, bus, chunk_ms=cfg.audio.chunk_ms, speed=speed, with_frames=with_frames
    )
    meta = player.meta

    console.print(f"\n  record   : [bold]{path.name}[/bold]")
    console.print(f"  captured : {meta.started_at:%Y-%m-%d %H:%M:%S} UTC, {meta.duration_s:.1f} s")
    if meta.mic:
        console.print(f"  mic      : {meta.mic.name} ({meta.mic.rate} Hz, {meta.mic.channels} ch.)")
    if meta.loopback:
        console.print(f"  system   : {meta.loopback.name}")
    console.print(f"  speed    : {'maximum' if speed <= 0 else f'x{speed:g}'}\n")

    a_sub = bus.subscribe(AudioChunk, maxsize=1024, name="audio")
    f_sub = bus.subscribe(Frame, maxsize=8, name="frames")

    seen = {src: {"n": 0, "sq": 0.0, "chunks": 0} for src in (Source.ME, Source.THEM)}
    frames_seen = 0
    order_ok = True
    last_t = -1

    async def pump_audio() -> None:
        nonlocal order_ok, last_t
        async for chunk in a_sub:
            s = seen[chunk.source]
            x = chunk.samples.astype(np.float64) / 32768.0
            s["sq"] += float(np.sum(x * x))
            s["n"] += len(x)
            s["chunks"] += 1
            if chunk.t < last_t:
                order_ok = False
            last_t = chunk.t

    async def pump_frames() -> None:
        nonlocal frames_seen
        async for _ in f_sub:
            frames_seen += 1

    tasks = [asyncio.create_task(pump_audio()), asyncio.create_task(pump_frames())]

    timeline = Timeline()
    vis = None
    seen_screens: list[ScreenEvent] = []
    if with_ocr:
        from argus.perception.vision_pipeline import VisionPipeline

        scr_sub = bus.subscribe(ScreenEvent, maxsize=64, name="screen-out")

        async def pump_screen() -> None:
            async for ev in scr_sub:
                seen_screens.append(ev)
                timeline.add_screen(ev)

        vis = VisionPipeline(bus, cfg)
        with console.status("  warming up OCR…"):
            await vis.warmup()
        tasks += [asyncio.create_task(vis.run()), asyncio.create_task(pump_screen())]

    pipe = None
    heard: list[SpeechEvent] = []
    if with_stt:
        from argus.perception.speech import SpeechPipeline

        s_sub = bus.subscribe(SpeechEvent, maxsize=256, name="speech-out")

        async def pump_speech() -> None:
            async for ev in s_sub:
                heard.append(ev)
                # Only the main engine goes into the feed. The shadow one recognizes the same audio for
                # comparison, and its text would double every utterance in the prompt.
                if not ev.engine:
                    timeline.add_speech(ev)

        pipe = SpeechPipeline(bus, cfg)
        with console.status("  warming up the model…"):
            await pipe.warmup()
        tasks += [asyncio.create_task(pipe.run()), asyncio.create_task(pump_speech())]

    stats = await player.run()
    if vis is not None:
        # Screen reading runs at one frame per second: the replay ended before it did.
        await asyncio.sleep(2.0)
    if pipe is not None:
        # The replay is over, but the recognition queue may still hold utterances.
        await asyncio.sleep(0.2)
        await pipe.drain()
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.05)  # let the subscribers process the tail
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if vis is not None:
        vis.close()

    if with_stt:
        shadow_name = pipe.shadow.name if pipe is not None and pipe.shadow else ""
        if shadow_name:
            _print_comparison(console, heard, cfg.stt.provider, shadow_name)
        else:
            console.print("  [bold]transcription[/bold]")
            if not heard:
                console.print("    [dim]no speech found[/dim]")
            for ev in sorted(heard, key=lambda e: e.t):
                who = "[cyan]Me  [/cyan]" if ev.source is Source.ME else "[magenta]Them[/magenta]"
                conf = f"{ev.confidence:.2f}" if ev.confidence is not None else "—"
                console.print(f"    {ev.t / 1e9:6.1f}s  {who}  {ev.text}")
                console.print(f"    {'':>6}   [dim]confidence {conf}[/dim]")
            console.print()

    if with_ocr:
        console.print("  [bold]what was on the screen[/bold]")
        if not seen_screens:
            console.print("    [dim]no text found[/dim]")
        for ev in seen_screens[:6]:
            ordered = sorted(ev.blocks, key=lambda b: (b.box[1], b.box[0]))
            head = " · ".join(b.text for b in ordered[:5])
            console.print(f"    {ev.t / 1e9:6.1f}s  [dim]{len(ev.blocks)} blocks[/dim]  {head[:90]}")
        if len(seen_screens) > 6:
            console.print(f"    [dim]… {len(seen_screens) - 6} more screen states[/dim]")
        g = vis.gate
        console.print(
            f"    [dim]the filter passed {g.passed} frames out of {g.seen} ({g.pass_rate:.0%}), "
            f"reading at {vis.avg_ms:.0f} ms each[/dim]"
        )
        console.print()

    if with_stt or with_ocr:
        context = render_context(
            timeline,
            screen_now="",
        )
        if context.strip():
            console.print("  [bold]the timeline as the model will see it[/bold]")
            for line in context.splitlines():
                console.print(f"    [dim]|[/dim] {line}")
            console.print(
                f"    [dim]{len(timeline)} entries, about "
                f"{estimate_tokens(context)} tokens[/dim]"
            )
            console.print()

    console.print("  [bold]what went through the pipeline[/bold]")
    for src, label in ((Source.ME, "mic     "), (Source.THEM, "system  ")):
        s = seen[src]
        console.print(
            f"    {label} : {s['chunks']:5d} chunks, {s['n'] / SAMPLE_RATE:6.1f} s, "
            f"{_rms_db(s['sq'], s['n']):6.1f} dB"
        )
    console.print(f"    screen   : {frames_seen:5d} frames")

    console.print()
    console.print(f"  event order by time: {'preserved' if order_ok else '[red]VIOLATED[/red]'}")
    dropped = {k: v["dropped"] for k, v in bus.stats().items() if v["dropped"]}
    console.print(f"  bus losses: {dropped or 'none'}")
    console.print(
        f"  the run took {stats.wall_seconds:.1f} s for a recording length of "
        f"{meta.duration_s:.1f} s — [bold]x{stats.speedup:.0f}[/bold] relative to real time"
    )
    console.print()


def _print_comparison(
    console,
    heard: list[SpeechEvent],
    primary: str,
    shadow: str,
) -> None:
    """Show both engines on the same audio, utterance by utterance.

    Utterances are joined by start time: both engines get **the same** slicing from the VAD,
    so `t` matches exactly and works as a key. That is by design: comparing engines on
    different slicing is meaningless, since the differences would come from phrase boundaries
    and not from recognition.
    """
    console.print(f"  [bold]transcription — {primary} vs {shadow}[/bold]")
    if not heard:
        console.print("    [dim]no speech found[/dim]")
        console.print()
        return

    by_start: dict[int, dict[str, SpeechEvent]] = {}
    for ev in heard:
        by_start.setdefault(ev.t, {})[ev.engine or primary] = ev

    same = diff = only_a = only_b = 0
    lat_a: list[float] = []
    lat_b: list[float] = []

    for start in sorted(by_start):
        pair = by_start[start]
        a = pair.get(primary)
        b = next((v for k, v in pair.items() if k != primary), None)

        ref = a or b
        assert ref is not None
        who = "[cyan]Me  [/cyan]" if ref.source is Source.ME else "[magenta]Them[/magenta]"
        console.print(f"    {start / 1e9:6.1f}s  {who}")

        for label, ev in ((primary, a), (shadow, b)):
            if ev is None:
                console.print(f"      [dim]{label:<18} — did not recognize[/dim]")
                continue
            ms = ev.latency_ms
            if ms is not None:
                (lat_a if label == primary else lat_b).append(ms)
            tail = f"[dim]{ms:>6.0f} ms[/dim]" if ms is not None else ""
            console.print(f"      [dim]{label:<18}[/dim] {ev.text}  {tail}")

        if a is None and b is not None:
            only_b += 1
        elif b is None and a is not None:
            only_a += 1
        elif a is not None and b is not None:
            if _normalize(a.text) == _normalize(b.text):
                same += 1
            else:
                diff += 1
                console.print("      [yellow]↑ differ[/yellow]")

    total = same + diff + only_a + only_b
    console.print()
    console.print(
        f"    [bold]matched[/bold] {same} of {total}, "
        f"[yellow]differed[/yellow] {diff}, "
        f"only {primary}: {only_a}, only {shadow}: {only_b}"
    )
    if lat_a or lat_b:
        avg_a = sum(lat_a) / len(lat_a) if lat_a else 0.0
        avg_b = sum(lat_b) / len(lat_b) if lat_b else 0.0
        console.print(
            f"    [dim]latency: {primary} {avg_a:.0f} ms, {shadow} {avg_b:.0f} ms[/dim]"
        )
    console.print(
        "    [dim]the disagreements are the very thing being measured: what matters is not their"
        " number,[/dim]"
    )
    console.print(
        "    [dim]but whose variant is correct where the engines disagreed.[/dim]"
    )
    console.print()


def _normalize(text: str) -> str:
    """Compare by words, not by punctuation.

    The engines place commas and capital letters differently, and counting that as a
    difference would drown the real errors in formatting noise.
    """
    import re

    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
