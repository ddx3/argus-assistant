"""The main window's bar: our own, in the page, instead of the system one.

The Windows system bar was removed (`desktop.use_page_caption`), and without a page with
buttons there is nothing to close the window with. So here we guard the joint: which
pages get the bar, how it controls the window, and that it draws only where it is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

from argus.ui.server import MAIN_WINDOW_ACTIONS

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "argus" / "ui" / "static"


def bar_js() -> str:
    return (STATIC / "winbar.js").read_text(encoding="utf-8")


def test_every_main_page_draws_the_bar():
    """A main-window page without `winbar.js` would be left without close buttons."""
    for name in ("home.html", "index.html", "record.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert '<script src="/winbar.js" defer></script>' in html, name


def test_the_bar_asks_only_for_actions_the_server_knows():
    js = bar_js()
    asked = set(re.findall(r'call\("([a-z_]+)"\)', js)) | set(re.findall(r'data-act="([a-z_]+)"', js))
    assert asked, "the bar asks the window for nothing"
    assert asked <= set(MAIN_WINDOW_ACTIONS), sorted(asked - set(MAIN_WINDOW_ACTIONS))


def test_the_bar_is_drawn_only_in_the_app_main_window():
    """In a browser tab, in the landing demo's iframe, and in detached windows, it is absent."""
    js = bar_js()
    assert "desk=1" in js
    assert "window.top !== window" in js
    assert 'hasAttribute("data-solo")' in js
    desktop = (ROOT / "src" / "argus" / "desktop.py").read_text(encoding="utf-8")
    assert 'desk=1' in desktop, "the app does not mark the main window's address"
    assert "restore_native_caption" in desktop, "no way back to the system strip when the page fails"


def test_the_bar_has_no_logo_but_the_letter_is_the_same_everywhere():
    """Requirement of 2026-10-07: take the logo out of the strip; everywhere else the outline of
    the A is the same.
    """
    assert "winbar-logo" not in bar_js() and "winbar-brand" not in bar_js()
    path = "M252.5 0H438.5L691 699H517L345.5 127L174 699H0Z"
    for name in ("legal/template.html", "landing/template.html"):
        assert path in (ROOT / name).read_text(encoding="utf-8"), name
    icon = (ROOT / "packaging" / "make_icon.py").read_text(encoding="utf-8")
    assert "(252.5, 0), (438.5, 0), (691, 699), (517, 699)," in icon
    assert "(345.5, 127), (174, 699), (0, 699)" in icon


def test_the_bar_is_taller_than_the_system_one_but_not_huge():
    """The bar was replaced for the sake of height: the Windows 11 system bar is 31 pixels, VS
    Code's is 35.

    At 40 it was found too big ("like VS Code's"), so the ceiling is kept too.
    """
    height = int(re.search(r"const BAR_H = (\d+);", bar_js()).group(1))
    assert 33 <= height <= 40


def test_the_server_serves_the_script_and_the_route():
    from argus.ui import server

    src = Path(server.__file__).read_text(encoding="utf-8")
    assert '@web.get("/winbar.js")' in src
    assert '@web.post("/window/main")' in src


def test_the_maximized_window_stays_off_the_taskbar():
    """Without `WS_CAPTION`, Windows maximizes the window over the whole monitor, on top of the
    taskbar.
    """
    desktop = (ROOT / "src" / "argus" / "desktop.py").read_text(encoding="utf-8")
    assert "form.MaximizedBounds = bounds" in desktop
    assert "_fit_maximized(hwnd)" in desktop


def test_the_bar_and_the_header_are_one_color():
    """The window strip is drawn with the same recipe as the page header: one block, not two
    bands.
    """
    js = bar_js()
    assert "background:var(--slab" in js
    assert "backdrop-filter:blur(var(--glass-blur" in js
    assert "html.desk header { top:var(--winbar); }" in js
    css = (STATIC / "chrome.css").read_text(encoding="utf-8")
    header = re.search(r"\nheader \{[^}]*\}", css).group(0)
    assert "background:var(--slab)" in header
    desktop = (ROOT / "src" / "argus" / "desktop.py").read_text(encoding="utf-8")
    assert "use_page_caption(cfg.ui.window_title, CAPTION_COLOR)" in desktop


def test_the_bar_script_parses():
    """An extra backtick in a CSS string quietly killed the whole strip: a page with no buttons."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    done = subprocess.run([node, "--check", str(STATIC / "winbar.js")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_whole_page_header_lives_in_the_bar():
    """Requirement of 2026-10-07: put everything into the top block. The whole page header (back,
    settings, the shared window, "RECOGNITION", sources, levels, "Work with the
    recording") moves into the strip, with a divider line below it.
    These are the same nodes that are found by the page's id and by chrome.js, so the handlers
    are not lost."""
    js = bar_js()
    assert 'body > header > .bar' in js
    assert ".winbar-icons > .iconbtn" in js and ".winbar-main" in js
    assert "border-bottom:1px solid var(--slab-edge" in js, "there is no divider line under the strip"
    pages = {
        "home.html": ("settings-btn",),
        "index.html": ("home-btn", "settings-btn", "live-btn", "power", "sources", "record-btn"),
        "record.html": ("home", "settings-btn"),
    }
    for name, ids in pages.items():
        page = (STATIC / name).read_text(encoding="utf-8")
        for ident in ids:
            assert f'id="{ident}"' in page, (name, ident)


def test_the_bar_does_not_overflow_the_narrowest_window():
    """The app window is no narrower than 880 px: on a narrow one, the level bars go first, then
    the caption by the link.
    """
    js = bar_js()
    assert "@media (max-width:1000px) { .winbar-main .cta" in js
    assert "overflow:hidden" in js.split(".winbar-main {", 1)[1].split("}", 1)[0]


def test_the_header_items_are_adapted_not_just_inserted():
    """Requirement of 2026-10-07: adapt the header items to the top block, do not just stuff them
    in: items at the full height of the strip, with no border and no pill, with dividers
    between groups."""
    js = bar_js()
    block = js.split(".winbar-main .power, .winbar-main .src, .winbar-main .cta {", 1)[1].split("}", 1)[0]
    assert "border:0" in block and "background:transparent" in block and "align-self:stretch" in block
    assert "::before" not in js.split(".winbar-main .meters", 1)[1].split("@media", 1)[0], "there must be no dividers between groups"
    assert ".winbar-main .src > .track" in js, "the level bars must sit under the words \"Microphone\" and \"System audio\""
    assert "font-weight:400" in js and "letter-spacing:0" in js, "one font in the strip"
    assert ".winbar-main .src:focus-within" in js and "outline:0" in js, "pressing the checkbox must not outline the item"


def test_the_winbar_draws_itself_in_the_app_window_even_without_the_desk_mark():
    """2026-10-08: launching twice removed the system strip but did not pass the `desk=1` mark,
    and the window was left with no buttons at all. Now, without the mark, a page in the app
    window (`chrome.webview`) compares the top of its content with the top of the window by
    itself: if there is no system strip, it draws its own. Checked on a live window: without
    the mark and without stored state, the buttons appeared."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static" / "winbar.js").read_text(encoding="utf-8")
    assert "window.chrome && window.chrome.webview" in js            # only in the app window, not in a browser tab
    assert 'JSON.stringify({ action: "state" })' in js and "st.rect[1]" in js
    assert "if (gap < 19 * dpr)" in js
    assert "function build()" in js and "build();" in js


def test_on_a_mac_the_window_buttons_are_a_traffic_light_on_the_left():
    """Requirement of 2026-10-13: the interface is not adapted to the Mac; it should look the same
    on the Mac as on the PC. The header is the same, in one strip from the very top of the
    window, but the window buttons are a traffic light on the left, like any Mac program, not
    three squares on the right. The app hides the system buttons (`mac/desktop.py:
    use_page_caption`)."""
    js = bar_js()
    assert "/Mac/i.test(navigator.platform" in js
    for act in ("close", "minimize", "toggle"):
        assert f'class="tl' in js and f'data-act="{act}"' in js
    assert ".winbar-lights" in js and ".winbar .tl.close { background:#ff5f57; }" in js
    assert ".winbar.idle .tl" in js, "window out of focus — gray traffic light"
    assert ".winbar.fs .winbar-lights { display:none; }" in js, "in full screen the menu bar covers the header: the standard buttons are there"
    # on the right it is empty on Mac, and on Windows it is the same three buttons as before
    assert 'bar.innerHTML = MAC' in js and 'class="winbar-btns"' in js


def test_the_traffic_light_only_asks_for_actions_the_mac_window_knows():
    from argus.ui.server import MAIN_WINDOW_ACTIONS

    assert {"close", "minimize", "toggle", "drag_start", "drag_move", "state"} <= set(MAIN_WINDOW_ACTIONS)


def test_the_app_marks_the_main_window_for_the_page_on_a_mac_too():
    """Without the `desk=1` mark the page does not draw the strip, and the system one on the Mac
    has already been removed.
    """
    desktop = (ROOT / "src" / "argus" / "desktop.py").read_text(encoding="utf-8")
    start = desktop.index("def use_page_caption")
    body = desktop[start:desktop.index("def caption_removed")]
    assert 'sys.platform == "darwin"' in body and "mac.use_page_caption()" in body
    assert 'caption_swapped = use_page_caption(cfg.ui.window_title, CAPTION_COLOR)' in desktop


def test_the_green_light_goes_full_screen_and_option_makes_it_zoom():
    """Requirement of 2026-10-13: the window must be able to open full screen. On
    the Mac the green circle is "full screen" (with Option, "zoom", as in the system one); on
    Windows the middle button still maximizes."""
    js = bar_js()
    assert 'data-act="fullscreen"' in js and "e.altKey" in js
    assert 'btn.dataset.act === "fullscreen" && e.altKey ? "toggle"' in js
    assert "fullscreen" in MAIN_WINDOW_ACTIONS


def test_the_traffic_light_does_not_flash_on_a_new_page_in_full_screen():
    """Bug report of 2026-10-13: in full screen, when pages change, the three buttons appeared for
    a fraction of a second near the gear. The `fs` class used to be set only by the app's
    response (after the first frame); now the page knows the state immediately: it remembers
    it from the previous page and guesses it from the window size."""
    js = bar_js()
    assert 'sessionStorage.getItem("argus-fs") === "1"' in js
    assert 'sessionStorage.setItem("argus-fs"' in js
    assert "window.innerWidth >= screen.width - 1 && window.innerHeight >= screen.height - 1" in js
    # the state is taken into account when the header is built, before the app's first response
    assert js.index('getItem("argus-fs")') < js.index("const sync = ")
