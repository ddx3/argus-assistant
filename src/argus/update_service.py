"""Update from the running app: "a new version is available" and the "Update"
button.

At launch the app already asks the server and installs a release by itself
(`update.maybe_self_update`, before the window). But the app lives for days: a new release
may have come out while it runs. Every half hour this service asks the server again and
keeps the answer for the page: while there is no release, there is no button in the
window; once one appears, the page (`winbar.js`) draws a bright button on any page, and a
press installs the release by the same path as at launch (`update.install_release`: signed
description, hash, replacement by a separate process), after which the windows close and
the replacement starts the new version.

If updating from here is impossible (launched from "Downloads", no rights to the folder,
the program is not built), `can_install` is false, and instead of installing, the button
opens the download page in an ordinary browser.

The page gets nothing technical: the state is a few flags, and the reason for a refusal
goes only to the log (`argus.log`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
import webbrowser
from collections.abc import Callable

from argus import update
from argus.core.config import Config

log = logging.getLogger(__name__)

# At launch the app has already asked the server; the first query from the running app comes
# later, then every half hour.
FIRST_CHECK_S = 120.0
CHECK_EVERY_S = 1800.0
# While the windows close and the replacement waits for the process to exit, the page must
# have time to show "Restarting".
QUIT_DELAY_S = 1.0

IDLE, DOWNLOADING, INSTALLING, FAILED = "idle", "downloading", "installing", "failed"

# Preview of the button during development: `ARGUS_UPDATE_PREVIEW=1 argus run`. The service
# pretends that a new release has come out, and the "install" only draws the download
# progress and downloads, replaces and closes nothing. A built app ignores the variable.
PREVIEW_ENV = "ARGUS_UPDATE_PREVIEW"


def _quit_app() -> None:
    """Close the app windows and quit. Without a window (web mode), the same as Ctrl+C."""
    from argus import desktop

    if not desktop.quit_app():
        signal.raise_signal(signal.SIGINT)


class UpdateService:
    def __init__(
        self,
        cfg: Config,
        running: Callable[[], bool] = lambda: False,
        quit_app: Callable[[], None] = _quit_app,
    ) -> None:
        self.server = cfg.update.server.strip()
        self.root = cfg.root
        self.running = running
        self.quit_app = quit_app
        # Updating exists only for a built app and only if a server is set (the same as at launch).
        self.enabled = bool(self.server) and update.is_frozen()
        self.release: update.Release | None = None
        self.preview = os.environ.get(PREVIEW_ENV) == "1" and not update.is_frozen()
        if self.preview:
            self.enabled = True
            self.release = update.Release(version="preview", zip="preview.zip", sha256="0" * 64, size=1, build=1)
        self.phase = IDLE
        self.percent: int | None = None
        self.failed_reason = ""
        self._failed_build = 0            # which release failed to install (to tell a failure from a new release)
        self._lock = threading.Lock()
        self._reason = ""

    # ---- what the page sees ----

    def snapshot(self) -> dict:
        """State for the page. No reasons or versions: the page draws a button, not a report."""
        return {
            "available": self.enabled and self.release is not None,
            "phase": self.phase,
            "percent": self.percent,
            # It can be installed from here; otherwise the button leads to the download page.
            "can_install": self.enabled and self.release is not None and not self._reason,
            # Recognition is running: the page will ask whether to really restart.
            "busy": bool(self.running()),
        }

    # ---- check ----

    def check_now(self) -> bool:
        """Ask the server. Blocks (network): call it from a thread. Returns whether there is a release."""
        if not self.enabled:
            return False
        if self.preview:
            return True
        release = update.check_release(self.server, self.root, timeout=10.0)
        if self.phase == FAILED and release is not None and release.build != self._failed_build:
            self.retry()          # new release: button is "Update" again, not a stuck "Failed"
        self.release = release
        self._reason = update.cannot_install_reason(self.root) if release is not None else ""
        if release is not None:
            log.info("release available: %s (%s)%s", release.version, release.build,
                     f", but it cannot be installed from here: {self._reason}" if self._reason else "")
        return release is not None

    async def run(self) -> None:
        """Background check: the first one a couple of minutes after launch, then every half hour."""
        if not self.enabled:
            return
        delay = FIRST_CHECK_S
        while True:
            await asyncio.sleep(delay)
            delay = CHECK_EVERY_S
            if self.phase in (DOWNLOADING, INSTALLING):
                continue
            try:
                await asyncio.to_thread(self.check_now)
            except Exception:  # noqa: BLE001 — the check must not crash the server
                log.exception("update check crashed")

    # ---- install ----

    def start_install(self) -> bool:
        """Start the install in a separate thread. `False` means there is nothing to install, it is not
        allowed, or one is already running.
        """
        if not self.enabled or self.release is None or self._reason:
            return False
        with self._lock:
            if self.phase in (DOWNLOADING, INSTALLING):
                return False
            self.phase = DOWNLOADING
            self.percent = 0
        worker = self._simulate if self.preview else self._install
        threading.Thread(target=worker, args=(self.release,), name="argus-update", daemon=True).start()
        return True

    def _simulate(self, release: update.Release) -> None:
        """Preview: the course of the download and restart for the eyes, with no network and no
        replacement.
        """
        for step in range(0, 101, 5):
            self._progress(step, 100)
            time.sleep(0.12)
        self.phase = INSTALLING
        time.sleep(2.0)
        self.phase = IDLE
        self.percent = None

    def _progress(self, done: int, total: int) -> None:
        self.percent = min(100, int(done * 100 / total)) if total else None
        if total and done >= total:
            self.phase = INSTALLING

    def _install(self, release: update.Release) -> None:
        try:
            started = update.install_release(self.server, release, self.root, self._progress)
        except update.UpdateError as e:
            log.warning("update was not installed: %s", e)
            self.failed_reason = str(e)
            self._failed_build = release.build
            self.phase = FAILED
            return
        except Exception:  # noqa: BLE001
            log.exception("update crashed")
            self.failed_reason = "unexpected error"
            self._failed_build = release.build
            self.phase = FAILED
            return
        if not started:
            self._failed_build = release.build
            self.phase = FAILED
            return
        self.phase = INSTALLING
        log.info("update started - closing windows, the program will restart")
        time.sleep(QUIT_DELAY_S)
        try:
            self.quit_app()
        except Exception:  # noqa: BLE001 — windows did not close: the replacement waits for the process to exit
            log.exception("could not close the windows for the update")

    def retry(self) -> None:
        """After a failure, return the button to its initial state."""
        if self.phase == FAILED:
            self.phase = IDLE
            self.percent = None

    # ---- fallback path ----

    def download_page(self) -> str:
        return f"{self.server.rstrip('/')}/download"

    def open_download_page(self) -> bool:
        """The download page in an ordinary browser, for when it cannot be installed by the app itself."""
        if not self.server:
            return False
        try:
            return bool(webbrowser.open(self.download_page()))
        except Exception:  # noqa: BLE001
            log.exception("browser did not open")
            return False


def attach(web, service: UpdateService) -> None:
    """Service routes. POST requires a JSON body: a foreign page in a browser cannot send one
    without a preflight permission, so the app will not restart because of someone else's form."""

    @web.get("/update/state")
    async def update_state() -> dict:
        return service.snapshot()

    @web.post("/update/install")
    async def update_install(payload: dict) -> dict:
        # Recognition is running: we install only with confirmation (`confirm: true`). The page
        # decides by the "busy" flag, which it learned up to a minute ago, and recording may have
        # just been turned on; closing the windows would cut it off.
        if service.running() and payload.get("confirm") is not True:
            return {"ok": False, "confirm": True, **service.snapshot()}
        if service.phase == FAILED:
            service.retry()
        started = service.start_install()
        return {"ok": started, **service.snapshot()}

    @web.post("/update/open")
    async def update_open(payload: dict) -> dict:
        return {"ok": await asyncio.to_thread(service.open_download_page)}
