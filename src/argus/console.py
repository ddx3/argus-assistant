"""Console output setup.

By default Windows gives the console the cp1252 encoding, and any Cyrillic crashes the
process with UnicodeEncodeError. The project works with Russian transcripts and on-screen
text, so UTF-8 is forced right at the entry point — before anything has time to print.
"""

from __future__ import annotations

import sys


class _LazyConsole:
    """A `rich.console.Console` that is created on first access.

    `import rich.console` takes fifty milliseconds, and the app window waits for them on the main
    thread: `argus.desktop` takes `console` at module level but prints no earlier than the window
    is already on screen. Any access (`print`, `status`, …) builds the real `Console` and from
    then on works as it does.

    It must also be usable where a `Console` is passed to other `rich` objects:
    `Live(console=console)` and `FileProxy` use `with console:` (protocol methods are looked up
    on the type, not through `__getattr__`, so they are explicit here) and single service names
    such as `_lock`.
    """

    def __init__(self) -> None:
        self._real = None

    def _build(self):  # type: ignore[no-untyped-def]
        if self._real is None:
            from rich.console import Console

            self._real = Console()
        return self._real

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        # Dunder names (`__wrapped__`, `__deepcopy__`, …) are asked for by `copy`, `inspect` and
        # `pickle`: there is no reason to build a `Console` for them, and the real one does not have
        # them. `_real` is our own field: before `__init__` (a copy, unpickling) it would recurse.
        if name == "_real" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._build(), name)

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self._build().__enter__()

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        return self._build().__exit__(*exc)


console = _LazyConsole()


def setup_stdio() -> None:
    """Switch stdout/stderr to UTF-8. Call this first thing."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # the stream is redirected and does not support it
                pass
