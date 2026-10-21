"""Run something on the macOS main thread and wait for the result.

Windows, menus and Carbon handlers live on the main thread, in the event loop that
pywebview runs. Calls from other threads (the server, hotkeys) do not work for them:
AppKit is not thread-safe, and the error shows up not as an exception but as a rare
process crash. `AppHelper.callAfter` (pywebview itself uses it) puts work in the loop's
queue; here it is extended with a wait for the result, with a limit, because the loop may
not be running yet and waiting forever is not acceptable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

DEFAULT_TIMEOUT_S = 3.0


def run_in_main_thread(func: Callable[[], Any], timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    """Run `func()` on the main thread and return its result.

    An exception inside `func` is re-raised here. If we do not get an answer in time,
    `TimeoutError` is raised: the event loop is not running (the app is still starting or is
    already closing), and the caller decides what to do about it.

    From the main thread itself the work runs immediately: waiting on the loop's queue makes
    sense only from another thread, and waiting on it from the main thread would mean standing
    in line behind our own call and sitting out the whole timeout.
    """
    if threading.current_thread() is threading.main_thread():
        return func()

    from PyObjCTools import AppHelper

    done = threading.Event()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = func()
        except BaseException as e:  # noqa: BLE001 — we will pass it on to the caller
            box["error"] = e
        finally:
            done.set()

    AppHelper.callAfter(work)
    if not done.wait(timeout):
        raise TimeoutError("the main thread did not respond")
    if "error" in box:
        raise box["error"]
    return box.get("value")
