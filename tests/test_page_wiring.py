"""A check that the page script reaches existing elements.

The file appeared because of a breakage that no test caught and no server error showed.
The markup had lost a button, while the handler for it stayed: `$("reopen").onclick = ...`
on `null` throws an exception right while the script is parsed, and everything declared
below it never runs. No connection to `/ws`, no remote control, no rendering. The page
opens and stands dead, while the server is healthy and cheerfully serves the state.

The check is dumb on purpose: collect the `id`s from the markup, collect the ones the
script reaches, and compare. It does not replace a real browser, but it catches exactly
the case when markup and handlers are edited separately.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import argus.ui

PAGES = sorted((Path(argus.ui.__file__).parent / "static").glob("*.html"))

# `$("something")` is the only way the pages of this project get elements by name.
LOOKUP = re.compile(r'\$\(\s*"([A-Za-z0-9_-]+)"\s*\)')
DECLARED = re.compile(r'\bid="([A-Za-z0-9_-]+)"')

# Elements that the script creates itself along the way; they are not in the markup and
# should not be.
MADE_AT_RUNTIME = {"offline"}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_referenced_element_exists(page: Path):
    text = page.read_text(encoding="utf-8")
    declared = set(DECLARED.findall(text)) | MADE_AT_RUNTIME
    missing = sorted(set(LOOKUP.findall(text)) - declared)
    assert not missing, f"the script gets nonexistent elements: {', '.join(missing)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_ids_are_unique(page: Path):
    """Two elements with one `id` mean that half of the handlers silently miss."""
    found = DECLARED.findall(page.read_text(encoding="utf-8"))
    doubles = sorted({x for x in found if found.count(x) > 1})
    assert not doubles, f"duplicated: {', '.join(doubles)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_script_on_the_page_parses(page: Path, tmp_path: Path):
    """The page script must be syntactically intact.

    This catches exactly what the element check does not: a function cut off in the middle.
    On such a page the browser executes **nothing**, no websocket, no rendering, yet the page
    looks almost normal: the markup is in place, it is just empty everywhere. This has already
    cost an hour of searching once.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found — nothing to check the syntax with")

    blocks = re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.DOTALL)
    for i, block in enumerate(blocks):
        js = tmp_path / f"{page.stem}-{i}.js"
        js.write_text(block, encoding="utf-8")
        done = subprocess.run([node, "--check", str(js)],
                              capture_output=True, text=True, check=False)
        assert done.returncode == 0, f"{page.name}, block {i}:\n{done.stderr}"
