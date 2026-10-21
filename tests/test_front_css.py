"""The shared window stylesheets (`chrome.css`, `admin.css`) and the strip (`winbar.js`):
what cannot be checked by eye without a screen.

Audit of 2026-10-14: the "Close" button of the terms lay under the window
buttons (css-design-2, fe-account-6); lists in the settings window had no focus indicator
(css-design-4), and for fields it was barely visible (-6); small text fell short of
contrast (-5); the selected amount tile lost its edge under the mouse (-7); `--t-small`
is declared nowhere (-8); in a narrow window the "Session name" label
slipped under the field (-11); the tabs of a narrow window had no scrollbar (-12);
`user-select` was without `-webkit-` (-16). The tests hold the rules in the CSS text;
the behavior of the scripts is in `test_front_admin_node.py` and
`test_front_chrome_node.py`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from argus.ui.server import STATIC as APP_STATIC

# `ARGUS_FRONT_STATIC` is where to look instead of the working static files (this is how
# we checked that the tests fail on a copy before the fixes).
STATIC = Path(os.environ.get("ARGUS_FRONT_STATIC") or APP_STATIC)

CHROME = (STATIC / "chrome.css").read_text(encoding="utf-8")
ADMIN = (STATIC / "admin.css").read_text(encoding="utf-8")
WINBAR = (STATIC / "winbar.js").read_text(encoding="utf-8")


def rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) of every simple rule; comments are dropped, nested @media blocks are
    expanded into their own rules.
    """
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body)]


def rule(css: str, selector: str) -> str:
    """The body of the rule with exactly this selector (the last one in the file)."""
    found = [body for sel, body in rules(css) if " ".join(sel.split()) == selector]
    assert found, f"no rule for '{selector}'"
    return found[-1]


def index_of(css: str, selector: str) -> int:
    return css.rindex(selector + " {") if (selector + " {") in css else css.rindex(selector)


# ---------------------------------------------------------------- terms window


def test_the_terms_viewer_starts_below_the_window_bar():
    """css-design-2, fe-account-6: the window bar (z-index 1000, height 38) sits above the document
    window (z-index 100), so its "Close" ended up under the app's own maximize/close
    buttons."""
    body = rule(CHROME, ".legal-view")
    assert "inset:var(--winbar, 0px) 0 0 0" in body
    assert "html.desk { --winbar:${BAR_H}px; }" in WINBAR, "the bar does not declare its own height"


# ---------------------------------------------------------------- focus


def test_a_select_in_the_settings_window_shows_a_focus_ring():
    """css-design-4: `.admin-root select:focus` removed the outline, and the window rule
    (`.win .admin-root select`, same specificity and later in the file) also reset the border
    color, so no sign of keyboard focus was left at all."""
    ring = rule(ADMIN, ".admin-root select:focus-visible")
    assert "outline:2px solid var(--focus)" in ring and "outline-offset:-1px" in ring
    # with equal specificity the order decides: the ring stands AFTER the rule that removes
    # the outline
    removed = ".admin-root input:focus, .admin-root textarea:focus, .admin-root select:focus {"
    assert ADMIN.index(removed) < ADMIN.index(".admin-root select:focus-visible {")
    # and no rule after it gives `outline:none` back to the list
    later = ADMIN[ADMIN.index(".admin-root select:focus-visible {"):]
    for selector, body in rules(later):
        if "select" in selector and "focus" in selector:
            assert "outline:none" not in body, selector


def test_text_fields_light_up_their_border_clearly_on_focus():
    """css-design-6: the border of the fields on focus lightened from .14 to .20 (a 1.2:1 step,
    1.85:1 against the window), so "where is the cursor" could not be caught by eye. Now it is
    `--focus-edge` (white .5): at least 3:1 against the window, without a bright ring (the app
    keeps to calm gray tones)."""
    m = re.search(r"--focus-edge\s*:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", CHROME)
    assert m, "chrome.css has no --focus-edge"
    alpha = float(m.group(4))
    win = _hex(CHROME, "--win")
    blended = tuple(win[i] + (int(m.group(i + 1)) - win[i]) * alpha for i in range(3))
    assert _contrast(blended, win) >= 3.0, f"focus edge against the window: {_contrast(blended, win):.2f}:1"
    for selector in (".ac-input:focus", ".composer:focus-within", ".ac-pay:focus-within",
                     ".win .pay-custom .ac-pay:focus-within"):
        assert "border-color:var(--focus-edge)" in rule(CHROME, selector), selector
    assert "border-color:var(--focus-edge" in rule(
        ADMIN, ".admin-root input:focus, .admin-root textarea:focus, .admin-root select:focus")
    # the window rules override the shared one with the same specificity and come later: each
    # has its own focus edge
    assert "border-color:var(--focus-edge)" in rule(ADMIN, ".win .admin-root .f.wide textarea:focus")
    assert "border-color:var(--focus-edge)" in rule(
        ADMIN, ".win .admin-root .f .ctl input[type=text]:focus, .win .admin-root .f .ctl input[type=number]:focus")


# ---------------------------------------------------------------- text color


def _hex(css: str, name: str) -> tuple[int, int, int]:
    value = re.search(rf"{re.escape(name)}\s*:\s*#([0-9a-fA-F]{{6}})", css).group(1)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _luma(rgb: tuple[float, float, float]) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    hi, lo = sorted((_luma(a), _luma(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_read_text_uses_a_token_that_passes_contrast_and_faint_stays_decorative():
    """css-design-5: `--faint` (#5b6270) gave about 3:1 on dark surfaces, while small text needs
    4.5:1: the hints in the sign-in fields, the line about the terms, the version, the table
    captions. Readable text uses `--meta`; `--faint` is only for what does not need to be read."""
    meta = _hex(CHROME, "--meta")
    win = _hex(CHROME, "--win")
    bg = _hex(ADMIN, "--bg")
    tile = tuple(c + (255 - c) * 0.035 for c in win)          # the account slab in the window: `rgba(255,255,255,.035)` over `--win`
    for name, surface in (("--win", win), ("--bg", bg), ("slab in the window", tile)):
        assert _contrast(meta, surface) >= 4.5, f"--meta on {name}: {_contrast(meta, surface):.2f}:1"
    assert _contrast(_hex(ADMIN, "--faint"), win) < 4.5, "if --faint became readable, this test can go"
    allowed = {"button.go:disabled"}                          # the icon of a disabled send button is not text
    for css in (CHROME, ADMIN):
        for selector, body in rules(css):
            if re.search(r"(?<![-\w])color\s*:\s*var\(--faint", body):
                assert " ".join(selector.split()) in allowed, f"readable text on --faint: {selector}"


def test_every_variable_chrome_css_needs_is_declared_on_every_page_that_loads_it():
    """css-design-8: `--t-small` lived only in design/argus.css, which no page of the app loads,
    so `var(--t-small)` was invalid: the font took the inherited 14px instead of 12.5. The test
    catches this for any variable that has no fallback value."""

    def declared(text: str) -> set[str]:
        return set(re.findall(r"(--[\w-]+)\s*:", re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)))

    def used_without_fallback(css: str) -> set[str]:
        return set(re.findall(r"var\((--[\w-]+)\s*\)", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)))

    own = declared(CHROME)
    for name in ("index.html", "home.html", "record.html", "admin.html"):
        page = (STATIC / name).read_text(encoding="utf-8")
        assert "/chrome.css" in page, name
        # on the real /admin `.admin-root` sits on <body>, and the palette (`--text`, `--dim`...)
        # comes from admin.css
        known = own | declared(page) | (declared(ADMIN) if name == "admin.html" else set())
        missing = used_without_fallback(CHROME) - known
        assert not missing, f"{name}: chrome.css variables without a declaration: {sorted(missing)}"
    # admin.css lives in two pages: the real /admin and the settings window on the instrument;
    # it takes its declarations from chrome.css
    for name in ("index.html", "admin.html"):
        page = (STATIC / name).read_text(encoding="utf-8")
        missing = used_without_fallback(ADMIN) - own - declared(ADMIN) - declared(page)
        assert not missing, f"{name}: admin.css variables without a declaration: {sorted(missing)}"


# ---------------------------------------------------------------- tiles, narrow window, tabs


def test_the_selected_amount_tile_keeps_its_edge_under_the_mouse():
    """css-design-7: `button:hover:not(:disabled)` (0,2,1) is stronger than
    `.pay-tile[aria-pressed]` (0,2,0), so in the window the edge of the selected tile
    disappeared while the mouse was over it after the click."""
    body = rule(CHROME, '.pay-tile[aria-pressed="true"], .pay-tile[aria-pressed="true"]:hover:not(:disabled)')
    assert "border-color:var(--glass-edge-top)" in body


def test_fixed_width_controls_shrink_in_a_narrow_window():
    """css-design-11: the session name field (330px) and the microphone list (360px) took the
    label's space in an 832px window (the app's minimum is 880): "Session name"
    with the "i" button (about 137px) shared 108px, and the button slid under the field."""
    narrow = ADMIN[ADMIN.index("@media (max-width:960px)"):]
    name = re.search(r'\.f\[data-path="chat\.name"\] \.ctl input\[type=text\] \{ width:(\d+)px', narrow)
    mic = re.search(r"#ac-mic-row \.ctl select, [^{]*\{ width:(\d+)px", narrow)
    assert name and mic
    # window 832 - left column 236 - margins 76 - scrollbar 10 - icon 40 - gaps 32: at least
    # 137 must be left for the label
    assert 832 - 236 - 76 - 10 - 40 - 32 - int(name.group(1)) >= 137
    # narrower still: a browser tab at 761-880px (below 760 the window is rearranged: tabs on
    # top)
    tiny = ADMIN[ADMIN.index("@media (min-width:761px) and (max-width:879px)"):]
    small = int(re.search(r'\.f\[data-path="chat\.name"\] \.ctl input\[type=text\] \{ width:(\d+)px', tiny).group(1))
    assert (761 - 48) - 236 - 76 - 10 - 40 - 32 - small >= 137


def test_the_narrow_tab_strip_has_a_visible_scrollbar():
    """css-design-12: the tabs of a narrow window scroll sideways, but the scrollbar had been
    removed (`height:0`). Without it you cannot see that "Payment" and "Account"
    exist on the right, and the wheel does not scroll sideways (`chrome.js` translates
    it)."""
    narrow = CHROME[CHROME.index("@media (max-width:760px)"):]
    bar = re.search(r"\.win-tabs::-webkit-scrollbar \{ height:(\d+)px", narrow)
    assert bar and int(bar.group(1)) >= 6


# ---------------------------------------------------------------- -webkit-user-select


def test_user_select_is_always_prefixed_for_safari():
    """css-design-16: Safari/WKWebView before 17 knows only `-webkit-user-select`; without it the
    button labels were selected by a double click, while the payment address was not selected
    by a click. The page files (index.html and others) are fixed by their owners."""
    for name, text in (("chrome.css", CHROME), ("admin.css", ADMIN), ("winbar.js", WINBAR)):
        for m in re.finditer(r"(?<![-\w])user-select\s*:\s*([\w-]+)", text):
            prefixed = re.compile(rf"-webkit-user-select\s*:\s*{re.escape(m.group(1))}")
            block = text[text.rfind("{", 0, m.start()) : text.find("}", m.end())]      # the whole rule, the order of declarations is any
            assert prefixed.search(block), f"{name}: {m.group(0)} without -webkit-user-select"


# ---------------------------------------------------------------- the "not saved" bar


def test_the_unsaved_bar_is_styled_like_the_restart_bar_but_red():
    body = rule(ADMIN, ".admin-root .restart.unsaved")
    assert "rgba(240,113,107" in body, "the 'not saved' bar is not red"
    assert ".win .admin-root .restart.unsaved" in ADMIN, "in the window the bar kept the color of the 'restart needed' bar"


def test_the_capture_mark_is_red_and_never_shrinks():
    css = WINBAR[WINBAR.index(".winbar-alert {"):]
    body = css[: css.index("}")]
    assert "flex:none" in body and "var(--bad" in body
    assert ".winbar-alert[hidden] { display:none; }" in WINBAR, "`[hidden]` would override the mark's `display`"
