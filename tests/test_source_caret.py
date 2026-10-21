"""The caret next to "System audio" and "Screen" flips while the
picker window is open (requirement of 2026-10-08: the caret should flip on press). The
window is closed in five places, so the `open` class is set by an observer of the
`hidden` attribute, not by each of them."""

from __future__ import annotations

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static" / "index.html"


def test_the_caret_flips_while_the_source_picker_is_open():
    html = INDEX.read_text(encoding="utf-8")
    assert re.search(r"\.src\.open \.caret \{ transform:rotate\(180deg\); \}", html)
    # the flipped caret does not twitch for those who ask for less motion
    assert "prefers-reduced-motion: reduce) { .src .caret { transition:none; } }" in html
    # the class follows the window itself, not every one of the places that close it
    assert 'new MutationObserver(syncCarets).observe($("picker"), { attributes: true, attributeFilter: ["hidden"] })' in html
    # switching from one source to another does not touch `hidden`, so `openPicker` syncs by
    # itself
    opening = html[html.index("async function openPicker("):html.index("setText($(\"pick-title\")")]
    assert "syncCarets();" in opening
    sync = html[html.index("function syncCarets()"):html.index("new MutationObserver(syncCarets)")]
    assert '"loopback"' in sync and '"screen"' in sync and 'open === "audio"' in sync and 'open === "screen"' in sync
