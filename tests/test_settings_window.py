"""Settings window: the tabs in `admin.js` and the sections on the server must not drift
apart.

The window (`chrome.js`, `openWindow`) builds its tabs from the schema sections following
the `TABS` list in `admin.js`. The list lives on the client and the schema on the server
(`admin.py`), and the first time they drift apart it will be silent: rename a section,
and a tab without sections is not drawn, while the section itself pops up as a separate
tab with a fallback icon. The test catches this where it is still a name in a list, not
an empty spot on the screen.
"""

from __future__ import annotations

import re
from pathlib import Path

from argus.tts.settings import TTSSettings
from argus.ui.admin import sections

STATIC = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static"

# How many characters fit in the caption of a settings-window row: one line under the title
# (`max-width:56ch` in `admin.css`; in practice slightly more than 60 fit).
CAPTION_MAX = 58


def captions() -> dict[str, str]:
    """Row captions of the window: the `ROW_CAPTION` table in `admin.js`."""
    js = (STATIC / "admin.js").read_text(encoding="utf-8")
    block = re.search(r"const ROW_CAPTION = \{(.*?)\n\};", js, re.DOTALL)
    assert block, "admin.js has no ROW_CAPTION table"
    return dict(re.findall(r'"([\w.]+)":\s*"([^"]+)"', block.group(1)))


def tabs_block() -> str:
    js = (STATIC / "admin.js").read_text(encoding="utf-8")
    block = re.search(r"const TABS = \[(.*?)\n\];", js, re.DOTALL)
    assert block, "admin.js has no TABS list"
    return block.group(1)


def test_every_section_named_by_a_tab_exists_on_the_server():
    """A tab that refers to a nonexistent section is a typo or a rename, not a design."""
    declared = {s.id for s in sections(TTSSettings())}
    named = {
        name
        for group in re.findall(r"sections: \[([^\]]*)\]", tabs_block())
        for name in re.findall(r'"([^"]+)"', group)
    }
    assert named, "TABS names no section at all"
    assert named <= declared, f"tabs refer to sections that do not exist: {sorted(named - declared)}"


def test_tab_ids_are_unique_and_leave_account_alone():
    """A tab identifier is part of the panel's address and a selection key; "account" belongs to
    the account tab that `chrome.js` adds."""
    ids = re.findall(r'\{ id: "(\w+)"', tabs_block())
    assert len(ids) == len(set(ids)), f"tab identifiers repeat: {ids}"
    assert "account" not in ids


def test_hint_links_open_the_account_tab_on_the_same_page():
    """The "settings" link in the sign-in hint opens the window on the "Account"
    tab on the same page where the person pressed it. Through `/admin#account` they
    were taken to another page, and the server lost the mark (the redirect leads to
    `/#admin`)."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/admin#account" not in index
    assert 'href="#account"' in index

    chrome = (STATIC / "chrome.js").read_text(encoding="utf-8")
    assert '"#account"' in chrome and '"#admin"' in chrome
    assert "hashchange" in chrome


def test_account_has_no_button_of_its_own_in_the_header():
    """Requirement of 2026-10-07: the person button is removed from the header, since settings
    already cover it. Sign-in and balance live on the "Account" tab of the settings
    window. Several editing sessions work on the pages at once, and a stale copy of
    `index.html` would silently bring the button back; this test will not let that through."""
    for name in ("home.html", "index.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert 'id="account-btn"' not in text, f"{name}: the account button is back in the header"
        # Without it, sign-in relies on the settings button: with the admin page off, the button
        # stays as long as there is something to open the account with.
        # On the home screen the condition is moved into `syncGear` (the sign-in screen has no
        # gear at all, 2026-10-08).
        condition = "lastAdmin || window.argusAccount" if name == "home.html" else "s.admin || window.argusAccount"
        assert condition in text, f"{name}: the gear will vanish without the admin page"
    assert "account-btn" not in (STATIC / "chrome.js").read_text(encoding="utf-8")


def test_every_row_icon_has_a_picture():
    """A window row draws its icon through `data-ico` (`ROW_ICON` and the widgets in
    `admin.js`), and the picture itself is the rule `.f[data-ico="…"]` in `admin.css`. A name
    without a picture would give an empty tile; the test catches this before it reaches the
    screen (2026-10-07, a window modeled on Cluely)."""
    js = (STATIC / "admin.js").read_text(encoding="utf-8")
    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    table = re.search(r"const ROW_ICON = \{(.*?)\n\};", js, re.DOTALL)
    assert table, "admin.js has no ROW_ICON table"
    names = set(re.findall(r':\s*"([\w-]+)"', table.group(1)))
    names |= set(re.findall(r'data-ico="([\w-]+)"', js))   # widgets draw their rows themselves
    names.add("sliders")                                   # fallback icon for a row without an entry in the table
    assert len(names) > 5
    for name in sorted(names):
        assert f'.f[data-ico="{name}"]' in css, f"admin.css has no picture for the icon '{name}'"


def first_phrase(hint: str) -> str:
    """The same as `hintParts` in `admin.js`: the first phrase of the explanation, if there is no
    caption.
    """
    if not hint or "<" in hint:
        return hint
    # The character class is copied from `admin.js` as is, including its Cyrillic capitals.
    m = re.match(r"^(.+?[.!?…])\s+(?=[«\"“A-ZА-ЯЁ])", hint, re.S)
    return m.group(1) if m else hint


def test_a_row_with_an_icon_has_two_lines_of_text():
    """Requirement of 2026-10-08: if an icon is used, there must be only two lines of text next
    to it: the name and the caption, each on one line. The caption is an entry in
    `ROW_CAPTION` or, if there is none, the first phrase of the explanation; longer than
    `CAPTION_MAX` it does not fit (this is what happened with "Session
    name": an 81-character caption, and after truncation a stub like "nam…" in place of the
    caption)."""
    wide = {"text", "chips"}                      # such rows go without an icon and without a caption
    table = captions()
    too_long = []
    for section in sections(TTSSettings()):
        for f in section.settings:
            if f.hidden or not f.label or f.kind in wide:
                continue
            shown = table.get(f.path) or first_phrase(f.hint)
            if len(shown) > CAPTION_MAX:
                too_long.append(f"{f.path}: {len(shown)} characters - {shown}")
    assert not too_long, "a row caption does not fit on one line:\n" + "\n".join(too_long)


def test_the_icon_row_that_admin_js_builds_itself_follows_the_same_rule():
    """"Invisible on recording" is drawn by `admin.js` without the schema,
    so its caption is in the script itself.
    """
    js = (STATIC / "admin.js").read_text(encoding="utf-8")
    m = re.search(r'hintParts\(wcHint, "([^"]+)"\)', js)
    assert m, "'Invisible on recording' has no caption of its own"
    assert len(m.group(1)) <= CAPTION_MAX


def test_the_window_keeps_name_and_caption_on_one_line_each():
    """A safeguard for whatever does not fit (a narrow window, a long translation): not a wrap
    onto a third line but an ellipsis. Without these rules the row with an icon would turn
    into three lines again."""
    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    name = re.search(r"\.win \.admin-root \.f\[data-ico\] \.lab \.name \{([^}]*)\}", css)
    desc = re.search(r"\.win \.admin-root \.f\[data-ico\] \.lab \.desc \{([^}]*)\}", css)
    assert name and "white-space:nowrap" in name.group(1)
    assert desc and "white-space:nowrap" in desc.group(1) and "text-overflow:ellipsis" in desc.group(1)


def test_every_caption_belongs_to_a_setting_and_fits_its_line():
    """An entry in `ROW_CAPTION` for a setting that does not exist is a typo in the path or an
    edit forgotten after a rename: the caption silently will not show."""
    declared = {f.path for s in sections(TTSSettings()) for f in s.settings}
    table = captions()
    assert table, "ROW_CAPTION is empty"
    assert set(table) <= declared, f"captions without a setting: {sorted(set(table) - declared)}"
    for path, text in table.items():
        assert len(text) <= CAPTION_MAX, f"{path}: a caption of {len(text)} characters - {text}"


def test_every_control_in_a_window_row_is_35px_tall():
    """The list, the field, the key combination, and "Check" all have the same height
    (requirement of 2026-10-08: select fields at the same height everywhere): `.hk` came out
    33.4px and "Check" 35.7px, because the font set their height. Measured in a real
    window in headless Edge; the test holds the rule in CSS."""
    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    # The height of fields and lists is the shared token `--field-h` (chrome.css, 35px); for
    # the key combination it is a number.
    assert "--field-h:35px" in (STATIC / "chrome.css").read_text(encoding="utf-8")
    for selector, height in ((r"\.win \.admin-root select", "height:var(--field-h)"), (r"\.win \.admin-root \.hk", "height:35px")):
        block = re.search(selector + r" \{([^}]*)\}", css)
        assert block and height in block.group(1), f"'{selector}' has no {height}"
    inputs = re.findall(r"\.win \.admin-root \.f \.ctl input\[type=text\], [^{]*\{([^}]*)\}", css)
    assert any("height:var(--field-h)" in body for body in inputs), "single-line fields have no height:var(--field-h)"


def test_the_microphone_list_never_shows_a_blank_when_the_pinned_device_is_gone():
    """A pinned microphone (a wireless headset) may disappear, and then the app listens to the
    system one (`find_microphone`). The list used to set a value it does not contain and show
    a blank (requirement of 2026-10-08: it must show the system default instead of just an
    empty spot)."""
    admin = (STATIC / "admin.js").read_text(encoding="utf-8")
    assert "if (mic.selectedIndex < 0) mic.selectedIndex = 0;" in admin
    assert "mic.value = chosen;" in admin and "mic.value = r.pinned" not in admin
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "if (micDev.selectedIndex < 0) micDev.value = r.current" in index


def test_only_one_hotkey_is_captured_at_a_time():
    """Bug report of 2026-10-08: in the hotkeys, all of them can be selected at once. Each button
    has its own state, and pressing the second did not release the first: all the "capturing"
    ones listened to the keyboard at once, and one combination was recorded into all of them.
    Checked in the browser: press three buttons, and only the last one captures."""
    js = (STATIC / "admin.js").read_text(encoding="utf-8")
    assert "let activeHotkey = null;" in js
    assert "if (activeHotkey) activeHotkey.finish(true);" in js      # a new button ends the previous one
    assert "if (activeHotkey === self) activeHotkey = null;" in js
    # a click away from the buttons cancels an assignment in progress
    assert re.search(r'document\.addEventListener\("mousedown", \(e\) => \{\s*if \(activeHotkey && ', js)
