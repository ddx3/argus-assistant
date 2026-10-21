"""Heavy libraries that startup needs a little later: load them ahead of time, in the
background.

`cv2.pyd` weighs 83 MB, `onnxruntime` and OpenBLAS about twenty each, and each such file
is read from disk and checked by the antivirus on first load. While a file is being read,
the processor is idle, and these used to be loaded one by one, in a row, from the thread
that happened to need the library. Here they load in a thread of their own, in parallel
with the rest of the startup; the loaded module stays in `sys.modules`, and when it is
really needed, `import` is a dictionary lookup.

**It starts when the window is already on screen** (`run_desktop(on_shown=...)`), not
earlier. Measured on this machine (NVMe, Defender is cheap at 0.5 ms per megabyte; reading
all the startup files from a cold disk takes 107 ms): such a preload, started before the
window, shares the interpreter with it and pushes the window back by about 0.1 s, with no
gain, because the disk is not the bottleneck. But on a slow disk reading files is a real
wait, and there it should overlap with other work, so the code stays; it just does not get
in the window's way.

It is safe because it changes nothing: importing a module is an idempotent operation, and
Python's import locks are per module, so two threads that need the same module simply
wait for each other. If it fails (the library is not in this build), we stay silent: the
real `import` in the right place will report it itself.

`ARGUS_NO_PREFETCH=1` turns it off, to compare a startup with and without the preload.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# In descending order of load cost on a cold disk, not alphabetically: the heaviest must go
# first so that by the time it is needed it is already ready.
#
# `bettercam` is deliberately not here: on import it immediately brings up DXGI and
# enumerates monitors, which is work with hardware, not loading a library. Its heavy part,
# `cv2`, is here, and the import itself stays where the screen is really opened.
HEAVY = (
    "cv2",             # 83 MB; needed by screen capture (`bettercam`) and JPEG frames
    "onnxruntime",     # speech detector
    "numpy",           # OpenBLAS ~20 MB; needed by almost everything
    "soxr",
    "httpx",           # client of the account server, update
    "fastapi",
    "uvicorn",
    "websockets",
)


def start() -> threading.Thread | None:
    """Start prefetching in a daemon thread. `None` means it is switched off."""
    if os.environ.get("ARGUS_NO_PREFETCH"):
        return None
    thread = threading.Thread(target=_run, name="argus-prefetch", daemon=True)
    thread.start()
    return thread


def _run() -> None:
    began = time.perf_counter()
    done = []
    for name in HEAVY:
        try:
            importlib.import_module(name)
            done.append(name)
        except Exception as e:                       # noqa: BLE001 — the real import will report it itself
            log.debug("prefetch failed for %s: %s", name, e)
    log.info(
        "prefetch: libraries %d/%d, %.0f ms",
        len(done), len(HEAVY), (time.perf_counter() - began) * 1000,
    )
