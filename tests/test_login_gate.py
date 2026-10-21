"""The sign-in screen instead of the session list (2026-10-08). Requirement: when we are not
signed in, the home page shows the sign-in screen full-screen, because the app cannot be
used without it anyway.

The form is drawn by the same `account.js` as the "Account" tab; the home page
only decides when to show it."""

from __future__ import annotations

import re
from pathlib import Path

HOME = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static" / "home.html"


def home() -> str:
    return HOME.read_text(encoding="utf-8")


def test_the_gate_locks_only_when_the_server_says_not_signed_in():
    """A build on its own keys (the route answers with a refusal without the field), a disabled
    admin page and a dropped connection lock nothing: only an explicit `signed_in: false` locks."""
    html = home()
    assert "setGate(!!st && st.signed_in === false)" in html
    assert "catch (e) { /* no connection — we do not lock */ }" in html
    assert "if (!window.argusAccount) return;" in html


def test_the_gate_reuses_the_account_form_and_hides_the_list():
    html = home()
    assert 'id="gate"' in html and 'id="gate-slot"' in html
    assert "window.argusAccount.mount(box" in html           # the same form as in the "Account" tab
    assert 'document.querySelector("main").hidden = on' in html
    assert re.search(r"main\[hidden\] \{ display:none; \}", html)


def test_signing_in_or_out_re_checks_the_gate():
    """Signing in through the screen and signing out from the "Account" tab both go
    through `onChange`.
    """
    html = home()
    hooks = html[html.index("window.argusAccountHooks = {"):html.index("// --- sign-in instead of the list ---")]
    assert "checkGate()" in hooks
    gate = html[html.index("function setGate(on)"):html.index("checkGate();\n", html.index("function setGate(on)"))]
    assert "checkGate()" in gate                          # the sign-in screen's own form has it too
    assert "window.argusWin.close()" in gate             # an open settings window does not get in the way of the sign-in screen


def test_the_sign_in_hint_stays_quiet_while_the_gate_is_on():
    """"you need to sign in — it's in settings" is redundant on the sign-in screen (requirement:
    do not write this until we are signed in): the form is already in front of the eyes."""
    html = home()
    notice = html[html.index("function setNotice(text)"):html.index("let ws;")]
    assert "|| gateOn" in notice
    start = html.index("function setGate(on)")
    gate = html[start:html.index("checkGate();\n", start)]
    assert '$("notice").hidden = !noticeText || gateOn;' in gate


def test_the_name_on_the_gate_is_one_lockup_and_the_top_bar_keeps_only_window_buttons():
    """Requirement: the logo is set into the name, in the same font (the mark replaces the first
    letter, not "A Argus"), and the top bar has only three buttons: minimize, window, close.
    There is no gear on the sign-in screen."""
    html = home()
    brand = html[html.index('<div class="gate-brand">'):html.index('<div id="gate-slot">') if '<div id="gate-slot">' in html else html.index('id="gate-slot"')]
    assert 'class="wm"' in brand and ">rgus</span>" in brand and "<b>Argus</b>" not in brand
    assert "aspect-ratio:691/699" in html and "height:.7em" in html
    assert "function syncGear() { $(\"settings-btn\").hidden = gateOn || !(lastAdmin || window.argusAccount); }" in html
    # the state stream no longer shows the gear icon, bypassing the sign-in screen
    assert '$("settings-btn").hidden = !(s.admin' not in html


def test_on_the_gate_the_three_window_buttons_stay_where_they_always_are():
    """Requirement: only three buttons at the top. On the sign-in screen the window buttons stay,
    and they stand on the right, as everywhere else. An earlier change moved them to the left by
    taking "top left" literally, and it was reverted. Only the gear icon can be on the left, and
    the sign-in screen has none."""
    bar = (HOME.parent / "winbar.js").read_text(encoding="utf-8")
    assert "gate-on" not in bar and "order:-1" not in bar
    assert "gate-on" not in home()


def test_the_winbar_stylesheet_string_has_no_stray_backticks():
    """The strip's CSS sits in a JS string in backticks: a stray backtick in a comment breaks the
    string, `winbar.js` does not load, and in a window without a system frame all three window
    buttons disappear (this is what happened when the file was edited on 2026-10-08)."""
    js = (HOME.parent / "winbar.js").read_text(encoding="utf-8")
    start = js.index("css.textContent = `")
    end = js.index("`;", start + 20)
    css = js[start + len("css.textContent = `"):end]
    assert "`" not in css
