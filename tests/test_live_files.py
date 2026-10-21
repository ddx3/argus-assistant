"""One version of the page for the build and for the sources.

The built `.exe` carries a copy of `ui/static` inside, taken at build time, and listens
on the same port 8420 as `argus run`. On the development machine this gave two pages at
one address: the build showed week-old markup, the sources showed fresh markup, and
there was nothing to tell them apart. A day went on "the versions rolled back".

Here the cure itself is checked: a build with the sources next to it reads the page from
them. And, no less important, that nothing like this happens for a person who was given
the build.
"""

from __future__ import annotations

from pathlib import Path

import argus.live as live_mod
from argus.live import live, source_root


def _pretend_frozen(monkeypatch, exe: str, package: Path) -> None:
    """Pretend to be a built app with an exe at this path."""
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", exe)
    monkeypatch.setattr(live_mod, "PACKAGE", package)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A working copy: `src/argus/...` and `dist/Argus/_internal/argus/...`."""
    # The marker of a working copy root. Without it, the walk up from the exe took anything for
    # a copy, up to the disk root.
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    src = tmp_path / "src" / "argus"
    (src / "ui" / "static").mkdir(parents=True)
    (src / "ui" / "static" / "index.html").write_text("fresh", encoding="utf-8")
    (src / "design").mkdir()

    packaged = tmp_path / "dist" / "Argus" / "_internal" / "argus"
    (packaged / "ui" / "static").mkdir(parents=True)
    (packaged / "ui" / "static" / "index.html").write_text("frozen", encoding="utf-8")
    (packaged / "design").mkdir()
    return src, packaged


def test_from_sources_nothing_is_substituted():
    """`argus run` reads the sources anyway, so there is nothing to substitute."""
    assert source_root() is None
    here = Path(live_mod.__file__).parent / "ui" / "static"
    assert live(here) == here


def test_a_build_next_to_its_sources_reads_the_sources(tmp_path, monkeypatch):
    """This is what it was all done for: one version for the app and for the browser."""
    src, packaged = _make_repo(tmp_path)
    _pretend_frozen(monkeypatch, str(tmp_path / "dist" / "Argus" / "Argus.exe"), packaged)

    assert source_root() == tmp_path / "src"
    page = live(packaged / "ui" / "static") / "index.html"
    assert page.read_text(encoding="utf-8") == "fresh"
    # The shared stylesheet too: if it lagged behind the pages, the mismatch would be visible
    # not as an old file but as broken layout.
    assert live(packaged / "design") == src / "design"


def test_a_build_on_a_stranger_machine_reads_itself(tmp_path, monkeypatch):
    """Whoever was handed the build has no sources next to it, so there is nothing to reach for."""
    _, packaged = _make_repo(tmp_path)
    # Not just "another folder" but another branch of the tree: the check looks exactly two
    # folders up from the exe, and a neighbor of the same depth would pass for a working copy.
    elsewhere = tmp_path / "stranger machine" / "Program Files" / "Argus"
    elsewhere.mkdir(parents=True)
    _pretend_frozen(monkeypatch, str(elsewhere / "Argus.exe"), packaged)

    assert source_root() is None
    page = live(packaged / "ui" / "static") / "index.html"
    assert page.read_text(encoding="utf-8") == "frozen"


def test_a_folder_without_the_page_is_not_taken_for_sources(tmp_path, monkeypatch):
    """A stranger's `src` folder nearby is no reason to read just anything from it."""
    _, packaged = _make_repo(tmp_path)
    # There is a `src`, but it has no dashboard page.
    (tmp_path / "src" / "argus" / "ui" / "static" / "index.html").unlink()
    _pretend_frozen(monkeypatch, str(tmp_path / "dist" / "Argus" / "Argus.exe"), packaged)

    assert source_root() is None


def test_the_substitution_can_be_switched_off(tmp_path, monkeypatch):
    """To look at the build the way a stranger will see it."""
    _, packaged = _make_repo(tmp_path)
    _pretend_frozen(monkeypatch, str(tmp_path / "dist" / "Argus" / "Argus.exe"), packaged)
    monkeypatch.setenv("ARGUS_NO_LIVE", "1")

    assert source_root() is None
    assert live(packaged / "ui" / "static") == packaged / "ui" / "static"


def test_an_explicit_path_wins(tmp_path, monkeypatch):
    """`ARGUS_SRC` is for when the copy lies somewhere other than where it is looked for by
    default.
    """
    src, packaged = _make_repo(tmp_path)
    elsewhere = tmp_path / "stranger machine" / "Program Files" / "Argus"
    elsewhere.mkdir(parents=True)
    _pretend_frozen(monkeypatch, str(elsewhere / "Argus.exe"), packaged)
    monkeypatch.setenv("ARGUS_SRC", str(tmp_path / "src"))

    assert source_root() == tmp_path / "src"
    assert live(packaged / "ui" / "static") == src / "ui" / "static"


def test_an_installed_build_cannot_be_pointed_at_a_planted_folder(tmp_path, monkeypatch):
    """The hole that was being closed: walking up "three folders" runs into the volume root.

    In `pathlib` the parent of the root is the root itself, so the walk up never fails:
    `C:\\Program Files\\Argus\\Argus.exe` turned into `C:\\src`, a flash drive into `E:\\src`,
    a share into `\\\\server\\share\\src`. Any unprivileged user can create a folder in the disk
    root, and a page planted there would get the same origin as the screen stream, the
    websocket with speech and `/admin/apply`. That is, a foreign file would read the user's
    screen and conversation.

    We check the most ordinary install layouts, not invented ones.
    """
    _, packaged = _make_repo(tmp_path)
    for where in ("Program Files/Argus", "Program Files (x86)/Argus", "Argus",
                  "Users/u/Downloads/Argus", "apps/Argus"):
        # We plant the bait exactly where the old walk up ended.
        exe = tmp_path / where / "Argus.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        bait = exe.parent.parent.parent / "src" / "argus" / "ui" / "static"
        bait.mkdir(parents=True, exist_ok=True)
        (bait / "index.html").write_text("planted", encoding="utf-8")

        _pretend_frozen(monkeypatch, str(exe), packaged)
        assert source_root() is None, where
        page = live(packaged / "ui" / "static") / "index.html"
        assert page.read_text(encoding="utf-8") == "frozen", where


def test_a_onefile_build_does_not_reach_for_sources(tmp_path, monkeypatch):
    """With onefile, `sys.executable` is one level higher, so the arithmetic of the walk up is
    different.

    Silently substituting the wrong folder is worse than substituting nothing: live mode
    just does not switch on, and this is visible from the version marker.
    """
    _, packaged = _make_repo(tmp_path)
    _pretend_frozen(monkeypatch, str(tmp_path / "dist" / "Argus.exe"), packaged)
    assert source_root() is None


def test_a_path_from_outside_the_package_is_left_alone(tmp_path, monkeypatch):
    """We substitute only our own files. A path from outside is none of our business."""
    _, packaged = _make_repo(tmp_path)
    _pretend_frozen(monkeypatch, str(tmp_path / "dist" / "Argus" / "Argus.exe"), packaged)

    outside = tmp_path / "somewhere" / "else"
    assert live(outside) == outside
