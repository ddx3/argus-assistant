"""Bring up only the web part: settings without hardware.

A normal launch warms up Whisper, opens the microphone and the system-audio loopback. For
configuring that is excessive, and sometimes impossible: three working copies of the
project share one sound card and eight gigabytes of video memory, of which Whisper takes
one and a half. Three full pipelines will not come up at once, yet three settings pages
are needed exactly at once, when you sit down to lay out the profile, character and voice
in one go.

So here is the same pipeline, assembled but not started. The difference is exactly one
call: `Argus.start()` is not called, and therefore no devices are opened and no weights
are loaded. All settings are read and written as usual, because they live in the config,
not in the hardware.

What this mode does not have and cannot have is stated on the page directly: live audio
levels, recognized utterances, the screen preview and the headphone-leak measurement. All
of it requires a working capture. Silent zeros in their place would look like a broken
pipeline, not like a pipeline that is switched off.
"""

from __future__ import annotations

import asyncio
import threading
import webbrowser

from argus.app import Argus
from argus.console import console
from argus.core.config import Config


async def run_settings(
    cfg: Config,
    port: int = 0,
    open_browser: bool = True,
    stop_signal: threading.Event | None = None,
) -> None:
    from argus.ui.server import has_admin, serve

    port = port or cfg.ui.port

    # We do not connect the screen: it opens a DXGI stream. We do connect the brain: it is just
    # an object until the first call, but without it the admin page has nothing to configure in
    # the filter and nothing to show the assembled prompt with.
    app = Argus(cfg, with_screen=False, with_brain=True, record=False)

    url = cfg.ui.base_url(port)
    console.print()
    # With the config: with `[ui] admin = false` the settings page does not come up at all, and
    # a link to it would lead to a 404 (`runner.py` asks the same way).
    admin = has_admin(cfg)
    if admin:
        console.print(f"  settings: [bold cyan]{url}/admin[/bold cyan]")
    else:
        console.print(f"  [yellow]this copy has no admin page[/yellow] - only the instrument panel: {url}")
    console.print(
        "  [dim]settings mode: no audio, screen or recognition. "
        "There will be no live numbers - for those you need a regular argus run.[/dim]\n"
    )
    if open_browser:
        webbrowser.open(f"{url}/admin" if admin else url)

    from argus.runner import _stop_on_signal

    bye = asyncio.Event()
    server = asyncio.create_task(serve(app, port=port, stop=bye))
    watcher = (
        asyncio.create_task(_stop_on_signal(stop_signal, server, bye))
        if stop_signal is not None else None
    )
    try:
        await server
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if watcher is not None:
            watcher.cancel()
        console.print("\n  stopped\n")
