"""One version of the page for the build and for the sources.

A built `.exe` carries inside it a copy of `ui/static` and `design/`, taken by PyInstaller
at build time. That copy freezes forever, while the files in `src/` live on, and on a
development machine this gives **two** pages on the same port 8420: one in the build, the
other in the sources. In a browser they are indistinguishable, and a day goes on "versions
rolled back" when nothing rolled back.

Here this is cured at the root: if the sources the build was made from sit next to it,
the page is read **from them**. Then there is one version, the freshest, and an edit shows
both in the app and in a browser tab, without a rebuild.

A person who was handed the build has no sources next to it: the check does not find them
and returns what is inside the build. So the substitution works exactly where it is needed
and cannot happen where it is not.

What is searched for: `<exe folder>/../../src/argus/...`, which is how `dist` sits in a
working copy. The path can be set directly with `ARGUS_SRC`; it can also be forbidden with
`ARGUS_NO_LIVE=1`, if you need to see the build as a stranger would see it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# The package root: `.../src/argus` in the sources, `..._internal/argus` in the build.
PACKAGE = Path(__file__).parent


def _has_page(root: Path) -> bool:
    return (root / "argus" / "ui" / "static" / "index.html").exists()


def source_root() -> Path | None:
    """The `src` folder of the working copy, or None if there is none nearby.

    There are more checks here than seems necessary, and each closes a hole.

    Climbing "three folders up" by itself **cannot fail to find something**: in `pathlib` the
    parent of the root is the root itself, so the path silently runs into the volume root.
    `C:\\Program Files\\Argus\\Argus.exe` thus turns into `C:\\src`, a flash drive into `E:\\src`,
    a network share into `\\\\server\\share\\src`. And any unprivileged user can create a folder
    in a drive root. After that a planted page gets the same origin as the screen stream, the
    websocket with speech transcripts and `/admin/apply`, so a foreign file reads the screen
    and the conversation of the machine's owner.

    So finding a `src` with the right file is not enough. The climb must pass **through the
    build folder**: `<copy>/dist/Argus/Argus.exe`, where above `Argus` stands exactly `dist`,
    and the root of the copy holds `pyproject.toml`. An installed app never has such a layout,
    and a working copy has exactly this one. The case where the climb ran into the volume root
    is cut off separately: there is nothing left to check.
    """
    if os.environ.get("ARGUS_NO_LIVE"):
        return None

    # An explicit setting is the only way to turn on the substitution anywhere. It is a
    # deliberate manual request, not a guess from where the files sit.
    asked = os.environ.get("ARGUS_SRC")
    if asked:
        root = Path(asked)
        return root if _has_page(root) else None

    if not getattr(sys, "frozen", False):
        # We are in the sources already: nothing to substitute.
        return None

    exe = Path(sys.executable).resolve()
    # `dist/Argus/Argus.exe` → `dist/Argus` → `dist` → root of the working copy.
    build_dir = exe.parent
    if build_dir.parent.name != "dist":
        return None
    repo = build_dir.parent.parent
    if repo == repo.parent:                  # ran into the volume root
        return None
    if not (repo / "pyproject.toml").exists():
        return None
    root = repo / "src"
    return root if _has_page(root) else None


def live(packaged: Path) -> Path:
    """A live folder instead of the packaged one, if a live one exists.

    The input is a path computed from the module (`Path(__file__).parent / "static"`); the
    output is the same path or its twin in the sources. The match is computed by the place
    inside the package, not by a list: there are several folders with page files
    (`ui/static`, `design`), and a list would have to be kept in two places.
    """
    root = source_root()
    if root is None:
        return packaged
    try:
        inside = packaged.relative_to(PACKAGE)
    except ValueError:
        # A path not from our package is none of our business.
        return packaged
    twin = root / "argus" / inside
    return twin if twin.exists() else packaged


def identity() -> dict:
    """Where the page you are looking at right now came from.

    It appeared after a day wasted: the build and `argus run` listen on the same port 8420 and
    are indistinguishable in a browser, while the pages they showed were different.

    Freshness is computed from the **newest** file among those actually served, not from
    `index.html` alone. Otherwise an edit in `chrome.js` or on the settings page would not
    move the marker, and it would answer "all the same" exactly when asked.
    """
    frozen = bool(getattr(sys, "frozen", False))
    newest = 0.0
    for folder in (live(PACKAGE / "ui" / "static"), live(PACKAGE / "design")):
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            if item.is_file():
                newest = max(newest, item.stat().st_mtime)
    return {
        "version": version(),
        # What launched it: a built `.exe` or `argus run` from the sources.
        "frozen": frozen,
        # Whether live files are read in this case. For a build on a development machine this is
        # true: it takes the page from `src/`, and there is one version for the app and for the
        # browser tab. For a build handed to a person it is false: there are no sources nearby, and
        # what is inside is read.
        "live": frozen and source_root() is not None,
        "page": newest,
        "root": str(live(PACKAGE / "ui" / "static")),
    }


def header() -> str:
    """The same idea in one line, for `curl -I`."""
    it = identity()
    what = "exe-live" if it["live"] else "exe" if it["frozen"] else "src"
    when = time.strftime("%d.%m %H:%M", time.localtime(it["page"])) if it["page"] else "?"
    return f"{what} {when}"


def version() -> str:
    # Import inside the function: `argus/__init__.py` pulls in the whole package, and this
    # module has to come up before everything else, since it is called while resolving paths.
    from argus import __version__

    return __version__
