"""Flat look of windows (2026-10-07): a guard so that it does not creep back.

Requirement: remove the outlines from buttons, the same "in the settings and in all
similar windows", plus darker windows. This is done by overriding tokens in `chrome.css`
(the "windows: the flat look" block) and by one shared main button
`.cta`. The test checks only what can be checked by reading the sources: the list of
windows, the consistency of the dark `--win` in two stylesheets, and that the pages' main
buttons sit on the shared `.cta` and not on their own copies.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "argus"
STATIC = ROOT / "ui" / "static"
DESIGN = ROOT / "design"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tag(html: str, ident: str) -> str:
    found = re.search(rf'<button[^>]*\bid="{ident}"[^>]*>', html)
    assert found, f"no button #{ident}"
    return found.group(0)


def _classes(tag: str) -> set[str]:
    found = re.search(r'\bclass="([^"]*)"', tag)
    return set(found.group(1).split()) if found else set()


def test_flat_scope_covers_every_window():
    """The button and field tokens are overridden in all windows at once — by one rule."""
    css = re.sub(r"/\*.*?\*/", "", _text(STATIC / "chrome.css"), flags=re.DOTALL)
    found = re.search(r"([^{}]+)\{([^{}]*--btn-edge:\s*transparent[^{}]*)\}", css)
    assert found, "chrome.css has no \"windows: the flat look\" block"
    scope = {part.strip() for part in found.group(1).split(",")}
    # settings and account, the home dialog, window choice and notice, drop-down lists
    assert {".win", ".dialog", ".sheet", ".combo-menu"} <= scope
    for token in ("--btn", "--btn-edge-hover", "--input-fill", "--input-edge"):
        assert re.search(rf"{token}\s*:", found.group(2)), f"windows do not override {token}"


def test_window_color_is_dark_and_the_two_tables_agree():
    """`--win` is darker than the former `#181b21` and is the same in the app and in the design
    system.
    """
    chrome = re.search(r"--win:\s*(#[0-9a-fA-F]{6})", _text(STATIC / "chrome.css"))
    design = re.search(r"--win:\s*(#[0-9a-fA-F]{6})", _text(DESIGN / "argus.css"))
    assert chrome and design
    assert chrome.group(1).lower() == design.group(1).lower(), "chrome.css and design/argus.css disagree"
    channels = [int(chrome.group(1)[i:i + 2], 16) for i in (1, 3, 5)]
    was = (0x18, 0x1B, 0x21)
    darker = all(now < before for now, before in zip(channels, was))
    assert darker, "the window is no longer darker than the former #181b21, as intended"


def test_primary_buttons_share_one_cta():
    """The main button of a page is the shared `.cta`; secondary ones in a dialog have no edge."""
    css = _text(STATIC / "chrome.css")
    assert re.search(r"^\.cta\s*\{", css, flags=re.MULTILINE), "chrome.css has no .cta"

    home = _text(STATIC / "home.html")
    for ident in ("new-chat", "empty-new", "dialog-ok"):
        assert "cta" in _classes(_tag(home, ident)), f"#{ident} is not on the shared .cta"
    for ident in ("dialog-cancel", "dialog-skip"):
        assert "ghost" in _classes(_tag(home, ident)), f"#{ident} must be bare (.ghost)"

    index = _text(STATIC / "index.html")
    assert "cta" in _classes(_tag(index, "notice-ok"))
    assert "ghost" in _classes(_tag(index, "notice-cancel"))

    account = _text(STATIC / "account.js")
    for act in ('data-act="go"',):
        buttons = [m for m in re.findall(r"<button[^>]*>", account) if act in m]
        assert buttons, f"account.js has no button {act}"
        assert all("cta" in _classes(b) for b in buttons), f"{act}: not on the shared .cta"


def test_every_single_line_field_uses_the_shared_field_tokens():
    """The input field is one for the whole app (requirement, 2026-10-08: input fields are not
    standardized): size and look come from the `--field-*` tokens (chrome.css), not from each
    page's own numbers. Before this, the settings fields had 35px/6px/border, the sign-in a
    slab without a border, the dialog 38px/8px/window color, and search had no border."""
    chrome = re.sub(r"/\*.*?\*/", "", _text(STATIC / "chrome.css"), flags=re.DOTALL)
    assert re.search(r"--field-h:35px;\s*--field-r:6px;\s*--field-edge:var\(--line2\);\s*--field-fill:transparent;", chrome)
    home = re.sub(r"/\*.*?\*/", "", _text(STATIC / "home.html"), flags=re.DOTALL)
    admin = re.sub(r"/\*.*?\*/", "", _text(STATIC / "admin.css"), flags=re.DOTALL)
    rules = {
        "sign-in and registration (.ac-input)": re.search(r"\.ac-input\s*\{([^{}]*)\}", chrome).group(1),
        "top-up amount": re.search(r"\.win \.pay-custom \.ac-pay\s*\{([^{}]*)\}", chrome).group(1),
        "dialog": re.search(r"\.dialog input\s*\{([^{}]*)\}", home).group(1),
        "search": re.search(r"#filter\s*\{([^{}]*)\}", home).group(1),
        "settings field": next(m.group(1) for m in re.finditer(
            r"\.win \.admin-root \.f \.ctl input\[type=text\][^{]*\{([^{}]*)\}", admin) if "height" in m.group(1)),
        "settings list": re.search(r"\.win \.admin-root select\s*\{([^{}]*)\}", admin).group(1),
    }
    for name, body in rules.items():
        for token in ("--field-h", "--field-r", "--field-edge", "--field-fill"):
            assert f"var({token})" in body, f"the field \"{name}\" has no {token}"


def test_voice_list_fills_the_window_height():
    """The voice list in the settings window stretches to the bottom of the window (requirement,
    2026-10-08), not 215px.
    """
    css = re.sub(r"/\*.*?\*/", "", _text(STATIC / "admin.css"), flags=re.DOTALL)
    rule = re.search(r"\.win \.admin-root #wpanel-voice \.vp-list\s*\{([^{}]*)\}", css)
    assert rule, "no rule for the height of the voice list in the window"
    assert "flex:1 1 0" in rule.group(1) and "max-height:none" in rule.group(1)
    # The shell stretches only while this tab is open: otherwise it takes space from
    # "Account".
    assert re.search(r"\.win \.admin-root:has\(> #main > #wpanel-voice:not\(\[hidden\]\)\)\s*\{[^}]*flex:1", css)


def test_account_forms_are_visible_in_the_window():
    """Sign-in and registration (2026-10-08): the field in a window is window-colored and
    without an edge, and there is no slab under the form — without its own backing the
    fields were not visible. The gift line uses a number, not the nonexistent `--t-small`."""
    css = re.sub(r"/\*.*?\*/", "", _text(STATIC / "chrome.css"), flags=re.DOTALL)
    # The form field and button have the same height and corner, like all window controls
    # (35px, 6px).
    field = re.search(r"\.ac-input\s*\{([^{}]*)\}", css).group(1)
    assert "height:var(--field-h)" in field and "border-radius:var(--field-r)" in field
    assert "height" not in re.search(r"\.ac-submit\s*\{([^{}]*)\}", css).group(1)
    assert re.search(r"\.cta\s*\{[^}]*height:35px", css)
    # The line "On registration — $0.30 to your balance…" is
    # not in the form (requirement, 2026-10-08: do not write that line).
    assert "ac-gift" not in css and "ac-gift" not in _text(STATIC / "account.js")
    # The form buttons are "variant A" (requirement, 2026-10-08): a gray slab without an
    # outline, dim text. Transitions between forms ("Sign up" and others)
    # remain links: the slab was requested for "Create account", not for
    # them.
    plate = re.search(r"\.ac-submit\s*\{([^{}]*background:rgba[^{}]*)\}", css).group(1)
    assert "background:rgba(255,255,255,.055)" in plate and "border:1px solid transparent" in plate
    js = _text(STATIC / "account.js")
    assert js.count('class="linklike" data-act="swap"') == 3 and "ac-alt" not in js and "ac-alt" not in css
    assert re.search(r"\.ac-top \+ \.note\s*\{[^}]*margin-top", css), "the \"no connection\" strip stuck to the email slab"


def test_material_rows_show_no_technical_numbers():
    """A file row in "Materials" is an icon, a name, a cross (requirement,
    2026-10-08: do not write technical info at all): no size in KB, no count of characters
    and pages, no price in tokens."""
    js = _text(STATIC / "admin.js")
    widget = js[js.index('window.adminWidgets["materials"]'):js.index('window.adminWidgets["audio-check"]')]
    for tech in ("KB", "chars", "tokens each", "pages as images", "i.kb", "i.chars", "r.tokens"):
        assert tech not in widget, f"the material row has a technical caption again: {tech}"
    assert 'class="iconbtn act x"' in widget and "mt-row" in widget
    # Files are one list in one slab with lines between rows, not a slab per file.
    css = re.sub(r"/\*.*?\*/", "", _text(STATIC / "admin.css"), flags=re.DOTALL)
    assert re.search(r"\.admin-root #mt-list:not\(:empty\)\s*\{[^}]*overflow:hidden", css)
    # The background of the file list is the same as that of the drop area above it
    # (requirement, 2026-10-08), both inside the window and outside it.
    drop = re.search(r"\.win \.admin-root \.mt-drop\s*\{[^}]*background:(var\(--btn\))", css).group(1)
    assert re.search(rf"\.win \.admin-root #mt-list:not\(:empty\)\s*\{{[^}}]*background:{re.escape(drop)}", css)
    assert re.search(r"\.admin-root \.mt-drop\s*\{[^}]*background:var\(--slab\)", css)
    assert re.search(r"\.admin-root #mt-list:not\(:empty\)\s*\{[^}]*background:var\(--slab\)", css)
