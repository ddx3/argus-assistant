"""Launch of the full pipeline together with the dashboard."""

from __future__ import annotations

import asyncio
import threading
import webbrowser

from argus.app import Argus
from argus.console import console
from argus.core.config import Config


def _import_server():  # type: ignore[no-untyped-def]
    """`(has_admin, serve)` from the server module, for the sake of loading the module itself."""
    from argus.ui.server import has_admin, serve

    return has_admin, serve


async def run_app(
    cfg: Config,
    port: int = 0,
    with_screen: bool = True,
    with_brain: bool = True,
    record: bool = False,
    open_browser: bool = True,
    open_path: str = "",
    autostart: bool = False,
    seconds: float = 0,
    stop_signal: threading.Event | None = None,
) -> None:
    # The default port is taken from the settings: three working copies start the same command,
    # and each has its own port. The flag remains a way to override the setting for one launch.
    port = port or cfg.ui.port

    # The window was closed while the modules were loading: bringing up the whole pipeline only
    # to stop it at once means wasted seconds during which the process still holds the
    # single-instance lock and keeps a new launch from starting.
    if stop_signal is not None and stop_signal.is_set():
        return

    # The server module is heavy (FastAPI, uvicorn, the whole interface), and it is needed only
    # after the pipeline is assembled. We load it in a thread while the assembly runs: on a cold
    # disk, reading files and the antivirus check are waiting, not CPU work, and they overlap.
    server_module = asyncio.get_running_loop().run_in_executor(None, _import_server)

    app = Argus(cfg, with_screen=with_screen, with_brain=with_brain,
                record=record, autostart=autostart)

    console.print()
    with console.status("  starting the pipeline: models, audio, screen…"):
        await app.start()

    has_admin, serve = await server_module

    if stop_signal is not None and stop_signal.is_set():
        await app.stop()
        return

    console.print(f"  microphone : [bold]{app.mic.name}[/bold]")
    console.print(f"  system     : [bold]{app.loopback.name}[/bold]")
    if with_screen:
        # Reading the screen and merely capturing it are different modes, and they must not be
        # confused: an OCR line when reading is off sends people looking for a fault in recognition
        # where there is none.
        console.print(
            f"  screen     : OCR {cfg.ocr.rec_lang}, downscaled to {cfg.ocr.max_width} px"
            if cfg.ocr.enabled
            else "  screen     : snapshot for the model only, no text reading"
        )
    if with_brain:
        console.print(f"  model      : [bold]{cfg.brain.provider}[/bold] / {cfg.brain.model}")
        console.print(
            f"  filter     : pause {cfg.gate_brain.cooldown_s:.0f} s, "
            f"limit of {cfg.gate_brain.max_calls} calls"
        )
    if app._record:
        frames = "audio and frames" if app.record_frames else "audio only"
        console.print(
            f"  recording  : [bold]on button press[/bold]  [dim]{frames}, "
            f"~220 MB/hour, limit {cfg.record.keep_gb:.0f} GB[/dim]"
        )

    watcher: asyncio.Task | None = None
    url = cfg.ui.base_url(port)
    console.print(f"\n  dashboard  : [bold cyan]{url}[/bold cyan]")
    if has_admin(cfg):
        console.print(f"  settings   : [bold cyan]{url}/admin[/bold cyan]")

    # A recording was explicitly requested but did not open (disk full, folder unavailable):
    # recognition is paid, and a run without a recording is not the one it was started for, so
    # we say so plainly and do not work further. (If it was turned on with the button,
    # `set_running` has already refused recognition, and the person sees the reason on the
    # page.)
    if app.record_required and autostart and not app.running:
        from rich.markup import escape

        reason = escape(app.record_error or "check the disk space")
        said = f"recording {reason}" if reason.startswith("didn't start") else f"recording didn't start: {reason}"
        console.print(
            f"\n  [red]{said}.[/red]"
            "\n  [red]Recognition is not enabled - this run is pointless without recording. "
            "Free up some space or change the sessions folder, then start again.[/red]\n"
        )
        await app.stop()
        return

    if seconds > 0:
        console.print(
            f"  [dim]will stop {seconds / 60:.0f} min AFTER "
            f"{'recording starts' if app._record else 'recognition is turned on'}; "
            f"Ctrl+C to stop sooner[/dim]\n"
        )
    else:
        console.print("  [dim]Ctrl+C to stop[/dim]\n")
    if open_browser:
        webbrowser.open(url + open_path)

    try:
        bye = asyncio.Event()
        server = asyncio.create_task(serve(app, port=port, stop=bye))
        watcher = (
            asyncio.create_task(_stop_on_signal(stop_signal, server, bye))
            if stop_signal is not None else None
        )
        if seconds > 0:
            await _stop_after(app, server, seconds)
        await server
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if watcher is not None:
            watcher.cancel()
        console.print("\n  stopping...")
        await app.stop()
        _summary(app)


async def _stop_on_signal(
    signal: threading.Event, server: asyncio.Task, bye: asyncio.Event | None = None
) -> None:
    """Stop when the window is closed.

    The event comes from a foreign thread: the window must live on the main one, so the
    pipeline runs in the background. We poll instead of waiting: `Event.wait` in a pool thread
    would occupy it until the process ends and hang the exit exactly when the user has already
    closed the window and expects everything to be over.
    """
    while not signal.is_set():
        if server.done():
            return
        # Fifty milliseconds, not two hundred: closing the window reaches the pipeline in exactly
        # this poll, and two hundred was the visible part of the shutdown.
        await asyncio.sleep(0.05)
    # We ask it to finish gracefully: cancelling the task breaks out as a traceback from
    # uvicorn, and a normal window close would look like a crash.
    if bye is not None:
        bye.set()
    else:
        server.cancel()


async def _stop_after(app: Argus, server: asyncio.Task, seconds: float) -> None:
    """Stop `seconds` after the recording started, not after launch.

    Counting from launch would be wrong: minutes go to choosing the window and sources, and
    "ten minutes" would turn into eight. The count starts at the button press, so ten minutes
    is ten minutes of recording. Without a recording (`--seconds` without `--record`) it
    starts from enabling recognition: the "recording started" event will never come there, and
    the deadline would never arrive.

    If the requested recording did not open, recognition is not enabled (`set_running`) and
    there is nothing to wait for: without it the run will not start by itself. We say so and
    finish instead of hanging.
    """
    recording = app._record

    def started() -> bool:
        return app.recording_began.is_set() if recording else app.running

    while not started():
        if server.done():                   # the server went down before the start
            return
        if recording and app.record_required and app.record_error and not app.running:
            from rich.markup import escape

            console.print(
                f"  [red]recording did not start ({escape(app.record_error)}) - without it the "
                f"{seconds / 60:.0f}-min run was not started, stopping[/red]"
            )
            server.cancel()
            return
        await asyncio.wait({server}, timeout=0.25)

    if recording:
        console.print(f"  [dim]recording started: {app.session_dir}[/dim]")
    else:
        console.print("  [dim]recognition enabled - the countdown has started[/dim]")
    if app.pruned:
        freed = sum(size for _, size in app.pruned) / 2**30
        names = ", ".join(p.name for p, _ in app.pruned[:3])
        more = f" and {len(app.pruned) - 3} more" if len(app.pruned) > 3 else ""
        console.print(
            f"  [yellow]old recordings deleted to free up space: {names}{more} "
            f"- freed {freed:.1f} GB[/yellow]"
        )

    # We wait for the deadline OR for the server to crash. A plain `sleep` will not do: if the
    # server died in the first second, we would silently stand there for ten minutes and think
    # we had recorded the session.
    done, _ = await asyncio.wait({server}, timeout=seconds)
    if not done:
        console.print(f"  [dim]time is up: {seconds / 60:.0f} min of recording[/dim]")
        server.cancel()


def _summary(app: Argus) -> None:
    st = app.state
    console.print()
    console.print(f"  utterances transcribed: {app.speech.transcribed} of {app.speech.utterances}")
    if app.vision is not None:
        g = app.vision.gate
        console.print(
            f"  frames                : {g.seen}, {g.passed} passed on to reading ({g.pass_rate:.0%})"
        )
    if app.assistant is not None:
        stats = app.assistant.gate.stats()
        console.print(f"  model calls           : {stats['requests']}, filtered out {stats['filtered out']}")
    for stage in st.stages.values():
        if stage.count:
            console.print(f"  {stage.name:<16}: {stage.avg_ms:.0f} ms on average, {stage.count} times")
    if app.session_dir is not None:
        console.print(f"  recording             : {app.session_dir}")
    console.print()
