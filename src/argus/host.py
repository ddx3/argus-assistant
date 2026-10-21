"""Where the app is running and where its files live: Windows or macOS.

Almost all the code does not care about the platform; it differs in four places: audio
and screen capture, windows, hotkeys, and updating. This module is not an abstraction
layer over them but the little that everyone needs to know: which system this is and
where to put data. It can be imported anywhere and pulls in nothing heavy.

**Where the data lives.** On Windows the app lives in an ordinary folder: settings,
recordings, chats and `.env` sit next to `Argus.exe` (`packaging/argus_app.py`). On macOS
that is not possible: an `.app` is a signed bundle, any file added inside it breaks the
signature, and an app launched from "Downloads" is mounted by the system read-only (App
Translocation). So the mutable data lives in `~/Library/Application Support/Argus`, and
the bundle itself holds only immutable seeds (`seed/`), from which this folder is filled
on the first launch and on every version change (`seed_user_data`).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "Argus"

# What in the bundle's seeds is not copied to the data folder as is: `argus.local.json` is
# the person's personal edits, `.env` is their own keys (distributed builds have no `.env`
# in the seeds, but a local build on a machine with a `.env` could bring one in, and it
# would overwrite the person's file on update); a seed is placed once, only if the file does
# not exist yet; everything else is updated together with the version.
SEED_ONCE = ("argus.local.json", ".env")
SEED_VERSION_MARK = ".seed-version"


def is_mac() -> bool:
    """macOS. Computed on every call, not at import, so the platform can be patched in a test."""
    return sys.platform == "darwin"


def is_frozen() -> bool:
    """The built app (PyInstaller) is running, not the sources."""
    return bool(getattr(sys, "frozen", False))


def app_bundle() -> Path | None:
    """The `Argus.app` from which the process was launched. `None` means not macOS or not a
    bundle.

    The executable is in `Argus.app/Contents/MacOS/`, and the bundle is three levels up. We
    check by name, not by the bare fact of "frozen": that way a build sitting in an ordinary
    folder is not considered a bundle.
    """
    if not (is_mac() and is_frozen()):
        return None
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def user_data_dir() -> Path:
    """Folder for the mutable data of the built app (does not create it)."""
    if is_mac():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def logs_dir() -> Path:
    """Where to write the log of the built app on macOS (does not create it)."""
    if is_mac():
        return Path.home() / "Library" / "Logs" / APP_NAME
    return user_data_dir()


def seed_dir() -> Path | None:
    """The seed folder inside the macOS bundle. `None` means there is none (not a bundle, or not
    built).

    PyInstaller puts data sometimes in `Contents/Resources` and sometimes in
    `Contents/Frameworks` (depending on the version and settings), and `sys._MEIPASS` points
    to the latter. We look in both: what we need is the seed, not its address.
    """
    bundle = app_bundle()
    if bundle is None:
        return None
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "seed")
    candidates += [bundle / "Contents" / "Resources" / "seed",
                   bundle / "Contents" / "Frameworks" / "seed"]
    return next((c for c in candidates if c.is_dir()), None)


def seed_user_data(seed: Path, data: Path) -> list[str]:
    """Fill the data folder from the bundle's seeds. Returns what was done.

    The rule is the same as for the update on Windows (`packaging/stage.py` puts the new
    `argus.toml` over the old one and leaves `argus.local.json` and the sign-in alone):

    * seed files are updated when the bundle version is different, as marked by
      `.seed-version` in the data folder;
    * personal files (`SEED_ONCE`) are put in only if they do not exist yet;
    * whatever is not in the seeds (recordings, chats, sign-in, `argus.account.json`) we do
      not touch at all.

    If copying fails, we do not crash: the app comes up on what is there.
    """
    done: list[str] = []
    data.mkdir(parents=True, exist_ok=True)

    wanted = _read(seed / "VERSION")
    current = _read(data / SEED_VERSION_MARK)
    refresh = wanted != current or not (data / "argus.toml").exists()

    for source in sorted(seed.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(seed)
        target = data / relative
        if relative.name in SEED_ONCE:
            if target.exists():
                continue
        elif not refresh and target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            done.append(str(relative))
        except OSError as e:
            log.warning("could not unpack %s: %s", relative, e)

    if refresh and wanted:
        try:
            (data / SEED_VERSION_MARK).write_text(wanted, encoding="utf-8")
        except OSError as e:
            log.warning("could not write the version marker: %s", e)
    return done


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
