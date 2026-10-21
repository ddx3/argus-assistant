"""Detached panels: the page, the routes, the window.

The window itself is not opened here: a WebView cannot be brought up in tests. Everything
else is checked, first of all two things that break silently: that the standalone page is
the very same page (otherwise a second layout drifts away from the first and shows
different data), and that in a browser the button does not pretend to work.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.app import Argus
from argus.core.config import Config
from argus.desktop import PANEL_TITLES, Panels
from argus.ui.server import PANELS, build


def _no_capture_config() -> Config:
    """A config for tests that are not about capture protection. The protection is on by default,
    and opening a panel with it means hiding the window with a real system call; here the window
    is a stub (the order and the failure of protection are in `test_panel_capture_order.py`)."""
    cfg = Config()
    cfg.ui.exclude_from_capture = False
    return cfg


def _fake_events():
    """A stub for `window.events`: `+=` stores nothing and does nothing. It fits where `open()` is
    needed as a whole (it hangs `closed` and `resized` on any opened window), while the events
    themselves do not matter in the particular test."""
    noop = type("S", (), {"__iadd__": lambda s, f: s})()
    return type("E", (), {"closed": noop, "resized": noop})()


@pytest.fixture
def http(monkeypatch):
    # No windows: as in a browser tab. We clean up on purpose: `_PANELS` lives in the module,
    # and a neighbor in the run that touched window startup would leave an app instance behind
    # where there is none.
    monkeypatch.setattr("argus.desktop._PANELS", None)
    app = Argus(Config(), with_screen=False, with_brain=False)
    with TestClient(build(app)) as client:
        yield client


@pytest.fixture
def http_with_window(monkeypatch):
    """The same client, but with an already open "all at once" window: with a handle
    and a window object without a real Windows behind them. `_resize_one` and `_set_min_size`
    are replaced: the real calls would fail without the handle and the form of a live window."""
    from argus import desktop

    panels = Panels("http://127.0.0.1:0", Config())
    panels.hwnds["live"] = 55
    panels.windows["live"] = object()
    monkeypatch.setattr(desktop, "_PANELS", panels)
    monkeypatch.setattr(desktop, "_resize_one", lambda hwnd, w, h: True)
    monkeypatch.setattr(desktop, "_set_min_size", lambda window, w, h: True)
    app = Argus(Config(), with_screen=False, with_brain=False)
    with TestClient(build(app)) as client:
        yield client, panels


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", PANELS)
def test_panel_page_is_the_same_page(http, name):
    """One layout for both views. A copy would drift from the original silently."""
    page = http.get(f"/panel/{name}").text
    # Not from `/`: that is now the home page with the chat list, while the page under test is
    # the chat work screen. It is the same file that `/chat/<id>` serves.
    from argus.ui.server import STATIC

    # The translation layer (`ui/i18n.py`) is injected into any page: we compare against the
    # file without it.
    from argus.ui.i18n import strip

    assert strip(page) == (STATIC / "index.html").read_text(encoding="utf-8")


def test_unknown_panel_is_not_served(http):
    assert http.get("/panel/nosuch").status_code == 404


def test_page_knows_how_to_show_itself_alone(http):
    """The standalone view rests on the address: it both hides the extras and asks the window."""
    page = http.get("/panel/chat").text
    assert "dataset.solo" in page and "data-solo" in page
    assert "/window/open" in page
    # The opacity slider is gone from the window: the page does not ask the app for the
    # window's overall alpha and does not remember it in the browser.
    assert "/window/opacity" not in page and "win-opacity" not in page


def test_the_ask_strip_is_always_shown_and_transparent(http):
    """The ask strip in a detached window is always shown and fully transparent.

    Requirement: this block is always shown, and everything is transparent, both the input
    field and the send button. Before that, the strip slid out when the mouse was over the
    window (the window grew downward in steps, with a spare overlay over the bottom); all of
    that is removed. This test checks what would come back silently: that the strip is a
    plain grid row again (not `position:fixed`, not hidden by transparency and an offset),
    that there are no slide-out attributes, no reserve under the overlay, no handle pinned to
    the growing window, and that the bar, the field and the button have no fill at rest.
    """
    page = http.get("/panel/chat").text
    # the strip is a grid row under the answers, not a slide-out bar
    assert 'grid-template-areas:"chat" "ask";' in page
    assert "position:fixed; left:0; right:0; bottom:0;" not in page
    assert "translateY(101%)" not in page
    for gone in ("data-ask-open", "data-ask-drawer", "data-ask-still", "--ask-base", "--ask-h",
                 "askBox", "pauseAsk", "openAsk", "const move = async", "const STEP = 12;",
                 "ask-toggle"):
        assert gone not in page, gone
    # the answers have no reserve under the strip: it does not cover their bottom
    assert "#p-chat .body { padding-bottom:64px; }" not in page
    # everything in the window is transparent: the bar, the field, the button at rest (on hover
    # the button gets the usual fill)
    assert "background:transparent; border:0; border-radius:0;" in page
    assert "body[data-solo] .ask > .composer { background:transparent; }" in page
    assert "body[data-solo] .ask > .composer > button.go:not(:hover) { background:transparent; }" in page
    # open screen panel: the line on the left continues the column divider, and the strip sits
    # in the answers column
    assert 'body[data-solo="live"][data-screen-open] .ask { border-left:1px solid var(--line); }' in page
    assert "left:380px" not in page


def test_every_panel_has_a_window_title():
    """A nameless window on the taskbar cannot be found among other such windows."""
    assert set(PANEL_TITLES) == set(PANELS)


# --------------------------------------------------------------------------
# There are no windows in a browser, and the page says so
# --------------------------------------------------------------------------


def test_detaching_in_a_browser_says_why_it_cannot(http):
    """`reason: "browser"` is what the page uses to open a popover instead of a refusal
    (`openPanel`, index.html). The same page makes the message string readable for the person,
    but JS switches on this field, not on that string."""
    out = http.post("/window/open", json={"panel": "chat"}).json()
    assert out["ok"] is False
    assert out["reason"] == "browser"
    assert "in the app" in out["error"]


def test_the_page_opens_a_popup_when_there_is_no_app_window(http):
    """Not silence in answer to a click, but its own popover at the same panel address."""
    js = http.get("/panel/live").text
    assert 'r.reason === "browser"' in js
    assert "openPopup(name)" in js
    assert 'window.open(`/panel/${name}`' in js


def test_the_popup_gets_its_own_drag_and_resize(http):
    """The browser popover is not an app window: pywebview does not inject its script there,
    and without its own replacement there is nothing to drag and resize within limits with.

    Every place that used to do this only through `/window/*` (unreachable without `Panels`)
    must branch on `IN_APP`; otherwise nothing happens in the popover on a press or a drag.
    """
    js = http.get("/panel/live").text
    assert "let IN_APP = !!window.pywebview" in js

    # Dragging goes through `window.moveBy`, in the same class as in the app.
    assert "window.moveBy(" in js
    assert '.closest(".pywebview-drag-region")' in js

    # There is no opacity slider either in the app or in the popover: the web has no overall
    # window alpha by any means, and in the app it dimmed the window together with the text (by
    # default down to 85%), so the window came out lighter than the page paints it.
    assert 'type="range"' not in js and "Window transparency" not in js

    # The resize handle exists only in the app (the popover already has the ordinary window
    # edge); in the browser, instead of it, the limit is corrected after the fact.
    assert "if (IN_APP) {" in js
    assert "window.resizeTo(" in js

    # Widening the screen panel and fitting to the frame also do not stay silent in the
    # popover, each with its own `resizeTo`.
    assert "window.resizeTo(window.outerWidth + delta, window.outerHeight)" in js
    assert "window.resizeTo(window.outerWidth, window.outerHeight + (height - window.innerHeight))" in js


def test_the_window_has_no_opacity_route_any_more(http):
    """The window opacity handle is gone: there is nobody to ask for it and no reason to."""
    assert http.post("/window/opacity", json={"panel": "chat", "value": 0.5}).status_code in (404, 405)


def test_resize_in_a_browser_says_why_it_cannot(http):
    out = http.post("/window/resize", json={"panel": "screen", "width": 460,
                                            "height": 259}).json()
    assert out["ok"] is False


def test_absurd_window_size_is_fitted_not_refused():
    """The size comes from the page as a frame proportion: any proportion, not only 16:9.

    The page once treated a refusal based on absolute pixels as "the server has no such
    route" and stopped asking altogether. So here it is not a refusal but a fit into the
    allowed box, keeping the proportion.
    """
    from argus.ui.server import WINDOW_BOX, fit_window_size

    min_w, min_h, max_w, max_h = WINDOW_BOX

    # Too small: it grows, the proportion stays.
    w, h = fit_window_size(20, 12)
    assert min_w <= w <= max_w and min_h <= h <= max_h
    assert round(w / h, 2) == round(20 / 12, 2)

    # Too big: it shrinks, the proportion stays.
    w, h = fit_window_size(4000, 3000)
    assert min_w <= w <= max_w and min_h <= h <= max_h
    assert round(w / h, 2) == round(4000 / 3000, 2)

    # Zero height must not divide by zero and must not pass through as is.
    w, h = fit_window_size(460, 0)
    assert min_w <= w <= max_w and min_h <= h <= max_h

    # Extreme proportions (a narrow column and a wide strip) do not break the box.
    w, h = fit_window_size(300, 900)
    assert min_w <= w <= max_w and min_h <= h <= max_h
    w, h = fit_window_size(1920, 300)
    assert min_w <= w <= max_w and min_h <= h <= max_h


def test_clamp_window_size_does_not_grow_the_other_side():
    """The corner grip is not a screen frame: it is dragged as is, with no shape behind it.

    `fit_window_size` does not fit here: once one side hits the limit, the same proportion
    starts growing the other side by itself, with no press on it. From the outside it reads
    as "the window suddenly spread sideways in the middle of a corner drag", which is exactly
    what happened before the split into two paths. `clamp_window_size` clips the sides
    separately: one at the limit, the other as requested (if it is also within the limit).
    """
    from argus.ui.server import WINDOW_BOX, clamp_window_size

    min_w, min_h, max_w, max_h = WINDOW_BOX

    # The height went below the limit; the width is within the allowed bounds and is not moved.
    w, h = clamp_window_size(500, 50)
    assert w == 500
    assert h == min_h

    # And the other way round: by width.
    w, h = clamp_window_size(50, 500)
    assert w == min_w
    assert h == 500

    # Both sides are past the limit, each in its own way: both are clamped independently.
    w, h = clamp_window_size(10, 5000)
    assert w == min_w
    assert h == max_h


def test_clamp_window_size_respects_a_raised_floor():
    """The "everything at once" window with the screen panel open has its own, higher floor.

    The screen panel is wider than the shared `WINDOW_BOX` on its own: the shared limit will
    not squeeze it, but it would let the window go below what the panel and the ask row need
    together (see `LIVE_DRAWER_MIN_WIDTH`). `min_width` raises the floor only above the base
    one and only while it is passed; it never lowers it below the base.
    """
    from argus.ui.server import WINDOW_BOX, clamp_window_size

    min_w, min_h, _, _ = WINDOW_BOX

    # Below the base limit, but the passed floor is below the base: the base one holds.
    w, h = clamp_window_size(50, 300, min_width=100)
    assert w == min_w

    # The passed floor is above the base: it pulls toward itself, not toward the base.
    w, h = clamp_window_size(400, 300, min_width=580)
    assert w == 580

    # Without a floor: as before, only the base one.
    w, h = clamp_window_size(400, 300)
    assert w == 400


def test_live_drawer_min_width_gives_the_ask_row_its_own_proven_floor():
    """The ask row in an open panel gets the same floor as without the panel.

    `.ask` (the field, the screen icon, the "Ask" button) is one and the same
    element whether the screen panel is open or not: it shares a grid column with `#p-chat`
    (`grid-template-areas` in index.html), while the panel sits in the neighboring column of
    its own. Its own need for width does not change because of that. `WINDOW_BOX[0]` was
    raised from 200 to 360 precisely because 200 was no longer enough for this row (see the
    comment above `WINDOW_BOX` in server.py). So here too, for the "everything at once"
    window with the panel open, the floor for it cannot be 200 again: that is exactly the
    number the file itself documents as insufficient for this row.
    """
    from argus.ui.server import LIVE_DRAWER_MIN_WIDTH, WINDOW_BOX

    min_w, _, _, _ = WINDOW_BOX
    assert LIVE_DRAWER_MIN_WIDTH == 380 + min_w
    assert LIVE_DRAWER_MIN_WIDTH - 380 >= min_w      # not less than the already proven floor




def test_the_page_says_where_it_came_from(http):
    """The build and the sources listen on one port and look the same.

    The built `.exe` has its own copy of `static/` inside, taken at build time: the page in
    it is frozen, while the files in `src/` keep living. An open tab showed a week-old build
    next to fresh sources, and it looked like "the versions rolled back". There was nothing to
    tell them apart; now there is.
    """
    from argus.ui.server import STATIC

    build = http.get("/build").json()
    assert build["frozen"] is False              # tests run from the sources
    assert build["root"] == str(STATIC)
    assert build["page"] > 0                     # the time of the page itself, not of the build

    head = http.get("/").headers["X-Argus-Build"]
    assert head.startswith("src ")               # with a single `curl -I`, no investigation needed


def test_unknown_panel_is_refused_before_any_window(http):
    out = http.post("/window/open", json={"panel": "nosuch"}).json()
    assert out["ok"] is False and "nosuch" in out["error"]


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


def test_the_panel_object_has_no_opacity_any_more():
    """The window's overall alpha (a Windows layer) was removed along with the slider: the window
    stays the way the page paints it."""
    from argus import desktop

    assert not hasattr(desktop, "_set_opacity")
    assert not hasattr(Panels, "opacity")


def test_second_detach_raises_the_window_instead_of_cloning_it():
    """Two windows of one panel would show the same thing and confuse people by looking different."""
    shown = []

    class FakeWindow:
        events = _fake_events()

        def show(self):
            shown.append(True)

    panels = Panels("http://127.0.0.1:0", Config())
    panels.ready.set()
    panels.windows["chat"] = FakeWindow()
    panels.open("chat")
    assert shown == [True]
    assert len(panels.windows) == 1


def test_resize_while_hidden_is_deferred_not_applied(monkeypatch):
    """"Panic" hides the panel; a resize must not bring it back on screen.

    Fitting the window to the frame shape also comes from a hidden panel: the page inside it
    keeps living and seeing new frames. Applying the size now would show the panel exactly
    during the screen sharing it was hidden for, so the size is applied not at once but when
    the panel is brought back.
    """
    calls = []
    monkeypatch.setattr("argus.desktop._resize_one",
                        lambda hwnd, w, h: calls.append((hwnd, w, h)) or True)

    class FakeWindow:
        events = type("E", (), {"closed": type("S", (), {"__iadd__": lambda s, f: s})()})()

        def show(self):
            pass

    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["screen"] = FakeWindow()
    panels.hwnds["screen"] = 4242
    panels.hidden = True

    applied = panels.resize("screen", 500, 300)
    assert applied is False
    assert calls == []                          # the window is untouched while hidden

    panels.show_all()
    assert calls == [(4242, 500, 300)]           # applied on return
    assert panels.pending_size == {}             # and will not repeat again


def test_resize_while_visible_is_applied_at_once(monkeypatch):
    calls = []
    monkeypatch.setattr("argus.desktop._resize_one",
                        lambda hwnd, w, h: calls.append((hwnd, w, h)) or True)
    panels = Panels("http://127.0.0.1:0", Config())
    panels.hwnds["screen"] = 99
    assert panels.resize("screen", 460, 259) is True
    assert calls == [(99, 460, 259)]


def test_resize_without_a_seq_keeps_applying_every_request(monkeypatch):
    """Old behavior: with no stamp there is nothing to compare to, so we always apply.

    Direct calls (these very tests) and a hypothetical old page without the `seq` fix must
    not suddenly start losing anything.
    """
    calls = []
    monkeypatch.setattr("argus.desktop._resize_one",
                        lambda hwnd, w, h: calls.append((w, h)) or True)
    panels = Panels("http://127.0.0.1:0", Config())
    panels.hwnds["screen"] = 99
    assert panels.resize("screen", 100, 100) is True
    assert panels.resize("screen", 50, 50) is True          # also applied, though smaller
    assert calls == [(100, 100), (50, 50)]


def test_resize_ignores_a_stale_request_that_arrives_after_a_newer_one(monkeypatch):
    """The real race behind the complaint "the size cannot be changed" / "the size breaks".

    The corner grip and the "show screen" arrow send `/window/resize` independently of each
    other, and each call goes into `asyncio.to_thread` on the server's shared thread pool: a
    thread dispatched earlier may well finish later than the one dispatched after it. Here
    this is modeled with no threads and no HTTP at all: `seq` is the stamp of the moment of
    sending, and the requests are simply *applied* in a different order than they were sent
    (2 comes before 1 by the time of the `resize` call, but that is exactly how the server
    thread would find them if the race finished in reverse order). Without checking `seq`,
    the second call would silently roll back the size that the first one set.
    """
    calls = []
    monkeypatch.setattr("argus.desktop._resize_one",
                        lambda hwnd, w, h: calls.append((w, h)) or True)
    panels = Panels("http://127.0.0.1:0", Config())
    panels.hwnds["live"] = 7

    # A request that is newer by send time (seq=2) reached the thread first, as if its HTTP
    # request had overtaken an earlier one on the network.
    assert panels.resize("live", 1000, 340, seq=2) is True
    # An older one (seq=1), itself sent earlier, reached the thread later and must not override
    # the newer size that has already been applied.
    assert panels.resize("live", 620, 340, seq=1) is False
    assert calls == [(1000, 340)]                            # the second call did not touch the window

    # And a truly new request (seq=3), arriving after both, is applied as usual: the check does
    # not block resizing forever, only what is older than what has already happened.
    assert panels.resize("live", 800, 340, seq=3) is True
    assert calls == [(1000, 340), (800, 340)]


def test_window_resize_endpoint_forwards_seq_and_ignores_the_stale_one(http_with_window):
    """The same, but through the real route: `seq` goes in the request body and must reach
    `Panels.resize`, not get lost on the way in `server.py`."""
    client, panels = http_with_window

    newer = client.post("/window/resize", json={
        "panel": "live", "width": 1000, "height": 340, "free": True, "seq": 2,
    }).json()
    assert newer["ok"] is True
    assert newer["width"] == 1000

    older = client.post("/window/resize", json={
        "panel": "live", "width": 620, "height": 340, "free": True, "seq": 1,
    }).json()
    # A stale request is not applied, but that is not an error: it is a lost race, named
    # honestly rather than hidden under the same text as "the panel is hidden" (a different
    # reason for the same `ok: False`).
    assert older["ok"] is False
    assert "outdated" in older["error"]


def test_closing_forgets_the_window():
    """A forgotten handle is opacity that went to someone else's window."""
    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["chat"] = object()
    panels.hwnds["chat"] = 42
    panels._forget("chat")
    assert not panels.windows and not panels.hwnds


def test_closing_forgets_the_raised_floor_too():
    """A forgotten width floor is a foreign, narrower panel that inherited someone else's."""
    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["live"] = object()
    panels.hwnds["live"] = 42
    panels._min_widths["live"] = 580
    panels._forget("live")
    assert panels.get_min_width("live") is None


def test_scroll_targets_the_named_body_by_default():
    calls = []

    class FakeWindow:
        def evaluate_js(self, js):
            calls.append(js)

    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["chat"] = FakeWindow()
    panels.scroll("chat", 240)
    assert "#p-chat .body" in calls[0]


def test_scroll_can_target_a_different_section():
    """"All at once" is not a `chat` panel but a window with two sections inside it;
    the key must scroll the conversation section, not the window by its own name
    (`#p-live .body` does not exist at all)."""
    calls = []

    class FakeWindow:
        def evaluate_js(self, js):
            calls.append(js)

    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["live"] = FakeWindow()
    panels.scroll("live", 240, target="chat")
    assert "#p-chat .body" in calls[0]


@pytest.mark.windows_only
def test_the_panel_window_is_found_by_its_own_name(monkeypatch):
    """The main window and a panel are born together; "new window" used to catch the wrong one."""
    from argus import desktop

    titles = {11: "Argus", 22: "Argus · answers"}
    monkeypatch.setattr(desktop, "own_windows", lambda: list(titles))
    monkeypatch.setattr(desktop, "_window_title", titles.get)
    assert Panels._await_titled("Argus · answers") == 22
    assert Panels._await_titled("Argus") == 11


def test_a_window_that_never_appears_is_not_mistaken_for_another(monkeypatch):
    from argus import desktop

    monkeypatch.setattr(desktop, "own_windows", lambda: [11])
    monkeypatch.setattr(desktop, "_window_title", lambda h: "Argus")
    assert Panels._await_titled("Argus · screen", timeout=0.2) is None


def test_the_close_button_goes_through_the_app(http):
    """A frameless window has no cross: Python has to close it."""
    page = http.get("/panel/chat").text
    assert "/window/close" in page
    out = http.post("/window/close", json={"panel": "chat"}).json()
    assert out["ok"] is False and "in the app" in out["error"]


def test_closing_destroys_the_window_and_forgets_it():
    destroyed = []

    class FakeWindow:
        def destroy(self):
            destroyed.append(True)

    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["chat"] = FakeWindow()
    panels.hwnds["chat"] = 7
    panels.close("chat")
    assert destroyed == [True]
    assert not panels.windows and not panels.hwnds


def test_closing_what_is_not_open_is_an_honest_error():
    panels = Panels("http://127.0.0.1:0", Config())
    with pytest.raises(RuntimeError, match="is not open"):
        panels.close("chat")


def test_the_window_is_dragged_only_by_its_strip(monkeypatch):
    """With "easy dragging" the window moves from a press anywhere.

    Including on the strip's buttons: then they cannot be pressed without moving the window at
    all, and it looks like broken buttons, not like a window setting.
    """
    import sys
    import types

    made = {}

    class FakeWindow:
        events = _fake_events()

        def show(self):
            pass            # on Windows the window is born hidden, and `open` shows the opaque one

    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **kw: (made.update(kw), FakeWindow())[1]
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(Panels, "_await_titled", staticmethod(lambda *a, **kw: 7))

    panels = Panels("http://127.0.0.1:0", _no_capture_config())
    panels.ready.set()
    panels.open("chat")
    assert made["easy_drag"] is False
    assert made["frameless"] is True and made["on_top"] is True


def test_opening_a_window_passes_min_size_and_sets_the_max(monkeypatch):
    """`resizable=False` was already tried once, and it broke dragging along the way.

    A custom WndProc answering `WM_GETMINMAXINFO` was tried next: it held the limit, but on
    a live window dragging disappeared entirely with it, the same symptom. The limit is now
    held by a real `min_size=` in `webview.create_window` (the lower one; WinForms answers it
    itself) and by `_set_max_size` right after the window is found (the upper one; pywebview
    has no parameter of its own for it). Neither touches WndProc, so this path does not
    affect dragging at all.
    """
    import sys
    import types

    class FakeWindow:
        events = _fake_events()

        def show(self):
            pass

    made = {}

    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **kw: (made.update(kw), FakeWindow())[1]
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(Panels, "_await_titled", staticmethod(lambda *a, **kw: 7))
    monkeypatch.setattr("argus.desktop._resize_one", lambda hwnd, w, h: True)

    maxed = []
    monkeypatch.setattr(
        "argus.desktop._set_max_size", lambda window, w, h: maxed.append((w, h))
    )

    from argus.ui.server import WINDOW_BOX

    min_w, min_h, max_w, max_h = WINDOW_BOX

    panels = Panels("http://127.0.0.1:0", _no_capture_config())
    panels.ready.set()
    panels.open("chat")
    assert made["min_size"] == (min_w, min_h)
    assert maxed == [(max_w, max_h)]


def test_set_min_width_is_read_back_and_can_be_cleared(monkeypatch):
    """`get_min_width` is what `/window/resize` uses to check the floor at the corner handle."""
    calls = []
    monkeypatch.setattr(
        "argus.desktop._set_min_size", lambda window, w, h: calls.append((w, h))
    )
    from argus.ui.server import WINDOW_BOX

    base_min_w, base_min_h, _, _ = WINDOW_BOX

    panels = Panels("http://127.0.0.1:0", Config())
    panels.windows["live"] = object()

    assert panels.get_min_width("live") is None            # not raised yet

    panels.set_min_width("live", 580)
    assert panels.get_min_width("live") == 580
    assert calls[-1] == (580, base_min_h)

    panels.set_min_width("live", None)                     # the screen panel closed
    assert panels.get_min_width("live") is None
    assert calls[-1] == (base_min_w, base_min_h)            # back to the shared floor


def test_set_min_width_of_an_unopened_window_is_a_no_op():
    """The panel could have been closed between the page deciding to raise the floor and the
    request.
    """
    panels = Panels("http://127.0.0.1:0", Config())
    panels.set_min_width("live", 580)                       # does not crash
    assert panels.get_min_width("live") is None


@pytest.mark.windows_only
def test_min_and_max_size_are_applied_on_the_forms_own_thread(monkeypatch):
    """The real bug behind the complaint "I pressed the arrow and the window just broke".

    The "show screen" arrow sends `screenOpen` over the network, and the server runs the
    request in `asyncio.to_thread`, that is, on a pool thread foreign to the form, not on the
    one where the window's event loop lives (see the header of `desktop.py`: "the window must
    live on the main thread"). `Form.MinimumSize`/`MaximumSize` are managed WinForms
    properties, not a bare `SetWindowPos`: pywebview itself in `winforms.py` wraps any other
    change to the form in `Invoke` if it is called from a thread other than the form's own;
    otherwise the change silently corrupts the form's internal state, not with an exception
    but with that very "everything breaks" and not a single line in the log.
    `_set_min_size`/`_set_max_size` must do the same.
    """
    import sys
    import types

    from argus.desktop import _set_max_size, _set_min_size

    class FakeSize:
        def __init__(self, w, h):
            self.w, self.h = w, h

    class FakeFunc:
        def __getitem__(self, item):
            return lambda fn: fn      # the delegate here is the function itself, which is enough

    system_mod = types.ModuleType("System")
    system_mod.Func = FakeFunc()
    system_mod.Type = object
    drawing_mod = types.ModuleType("System.Drawing")
    drawing_mod.Size = FakeSize
    system_mod.Drawing = drawing_mod
    monkeypatch.setitem(sys.modules, "System", system_mod)
    monkeypatch.setitem(sys.modules, "System.Drawing", drawing_mod)

    class FakeForm:
        def __init__(self, invoke_required):
            self.InvokeRequired = invoke_required
            self._scale = 1
            self.invoked = 0
            self.MinimumSize = None
            self.MaximumSize = None

        def Invoke(self, delegate):
            self.invoked += 1
            delegate()

    class FakeBrowserView:
        instances: dict = {}

    winforms_mod = types.ModuleType("webview.platforms.winforms")
    winforms_mod.BrowserView = FakeBrowserView
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", winforms_mod)

    window = type("W", (), {"uid": "w1"})()

    # A foreign thread is exactly the case of a network request from the arrow: the change must
    # go through `Invoke`, not land on the form directly.
    form = FakeForm(invoke_required=True)
    FakeBrowserView.instances["w1"] = form
    assert _set_min_size(window, 580, 240) is True
    assert form.invoked == 1
    assert (form.MinimumSize.w, form.MinimumSize.h) == (580, 240)

    assert _set_max_size(window, 1600, 1200) is True
    assert form.invoked == 2
    assert (form.MaximumSize.w, form.MaximumSize.h) == (1600, 1200)

    # The form's own thread, as when the window is first created from inside the event loop:
    # applied at once, `Invoke` is simply not needed here.
    form2 = FakeForm(invoke_required=False)
    FakeBrowserView.instances["w1"] = form2
    assert _set_min_size(window, 580, 240) is True
    assert form2.invoked == 0
    assert (form2.MinimumSize.w, form2.MinimumSize.h) == (580, 240)


def test_opening_the_drawer_raises_the_floor_over_the_network(http_with_window):
    """The arrow asks for `screenOpen: true`, and the server raises the floor on the window itself."""
    from argus.ui.server import LIVE_DRAWER_MIN_WIDTH

    client, panels = http_with_window
    out = client.post("/window/resize", json={
        "panel": "live", "width": 840, "height": 340, "free": True, "screenOpen": True,
    }).json()
    assert out["ok"] is True
    assert panels.get_min_width("live") == LIVE_DRAWER_MIN_WIDTH


def test_the_grip_cannot_undercut_the_raised_floor(http_with_window):
    """The corner grip does not send `screenOpen` at all, but the floor raised by the arrow must
    hold for it too, not only for the request that raised it.
    """
    from argus.ui.server import LIVE_DRAWER_MIN_WIDTH

    client, panels = http_with_window
    panels.set_min_width("live", LIVE_DRAWER_MIN_WIDTH)      # the screen panel is already open

    out = client.post("/window/resize", json={
        "panel": "live", "width": 400, "height": 300, "free": True,
    }).json()
    assert out["ok"] is True
    assert out["width"] == LIVE_DRAWER_MIN_WIDTH             # 400 is below the floor: clamped


def test_closing_the_drawer_lowers_the_floor_back(http_with_window):
    """Once the panel is closed, the arrow sends `screenOpen: false` and the floor is removed."""
    client, panels = http_with_window
    panels.set_min_width("live", 580)

    out = client.post("/window/resize", json={
        "panel": "live", "width": 400, "height": 300, "free": True, "screenOpen": False,
    }).json()
    assert out["ok"] is True
    assert out["width"] == 400                               # the shared limit (360) is below this
    assert panels.get_min_width("live") is None


def test_a_screen_that_is_not_tracked_is_a_small_transparent_strip_in_the_live_window(http):
    """Requirement (2026-10-08): when the screen is not tracked, show a small transparent block
    instead of a huge black one. This applies only to the auxiliary window, not to the main one.

    The frame block is a 16:9 frame with a solid background; while the screen is off, it holds
    one caption, yet it takes up space like a frame. The page marks the block (`.off`), and in
    "all at once" it shrinks to a line with no background (the caption in the same
    font as "Recognition is off", the line under it stays); in the
    main window and in the screen window there is no such rule: everything is as it was."""
    page = http.get("/panel/live").text
    flat = " ".join(page.split())
    assert 'document.querySelector("#p-screen .shotbox").classList.toggle("off", !c.sources.screen);' in page
    rule = ('body[data-solo="live"] .shotbox.off, html[data-glass] body[data-solo="live"] .shotbox.off { '
            'aspect-ratio:auto; background:transparent; place-items:center start; padding:10px 13px; }')
    assert rule in flat
    # the label has no font of its own, and the line under the block is not removed
    assert "#shot-none { font-size" not in page and "border-bottom:0" not in rule
    # only "everything at once": neither the main window, nor the screen window, nor the
    # answers are affected by this rule
    for other in ('body[data-solo="screen"] .shotbox.off', 'body[data-solo="chat"] .shotbox.off'):
        assert other not in page, other


def test_the_strip_is_marked_as_the_drag_region(http):
    """Otherwise a frameless window cannot be moved at all."""
    assert "pywebview-drag-region" in http.get("/panel/chat").text


def test_detaching_before_the_windows_exist_says_so(monkeypatch):
    """The server answers before the WebView event loop appears.

    A request in this gap used to return "done" and do nothing.
    """
    from argus import desktop

    monkeypatch.setattr(desktop, "PANEL_READY_S", 0.05)
    panels = Panels("http://127.0.0.1:0", Config())
    with pytest.raises(RuntimeError, match="still starting"):
        panels.open("chat")
    assert not panels.windows


def test_a_window_we_cannot_find_is_closed_not_left_on_screen(monkeypatch):
    """A panel without a handle cannot be hidden from capture, while the person is sure it is
    hidden.
    """
    import sys
    import types

    destroyed = []

    class FakeWindow:
        events = _fake_events()

        def show(self):
            pass

        def destroy(self):
            destroyed.append(True)

    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **kw: FakeWindow()
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(Panels, "_await_titled", staticmethod(lambda *a, **kw: None))

    panels = Panels("http://127.0.0.1:0", Config())
    panels.ready.set()
    with pytest.raises(RuntimeError, match="didn't appear"):
        panels.open("chat")
    assert destroyed == [True]
    assert not panels.windows and not panels.hwnds

# --------------------------------------------------------------------------
# Transparent panel background (Windows 11 acrylic)
# --------------------------------------------------------------------------


def _open_with_fake_webview(monkeypatch, cfg, glass_supported):
    """Open a panel on a stub webview; return (the window parameters, the call log)."""
    import sys
    import types

    from argus import desktop

    made, calls = {}, []

    class FakeWindow:
        events = _fake_events()

        def show(self):
            pass

    def create_window(*a, **kw):
        made["url"] = a[1]
        made.update(kw)
        return FakeWindow()

    fake = types.ModuleType("webview")
    fake.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "glass_supported", lambda: glass_supported)
    monkeypatch.setattr(desktop, "_glass", lambda window: calls.append("glass") or True)
    monkeypatch.setattr(
        Panels, "_await_titled", staticmethod(lambda *a, **kw: calls.append("find") or 7)
    )
    monkeypatch.setattr(desktop, "_resize_one", lambda hwnd, w, h: True)
    monkeypatch.setattr(desktop, "_set_max_size", lambda window, w, h: True)

    panels = Panels("http://127.0.0.1:0", cfg)
    panels.ready.set()
    panels.open("chat")
    return made, calls


def test_the_panel_window_asks_for_glass_before_it_is_shown(monkeypatch):
    """WebView2 makes its background transparent only when the window is born, and the form must be
    painted before it is shown: otherwise gray flashes in the first frame. The page learns about
    the glass from the address; the window has not yet had time to ask it."""
    made, calls = _open_with_fake_webview(monkeypatch, _no_capture_config(), glass_supported=True)

    assert made["transparent"] is True
    assert made["url"].endswith("/panel/chat?glass=1")
    assert calls == ["glass", "find"], "the glass is painted before the search for the (visible) window"


@pytest.mark.parametrize(
    ("switch_off", "supported"),
    [(True, True), (False, False)],
    ids=["off in the config", "Windows without acrylic"],
)
def test_no_glass_means_the_old_opaque_window(monkeypatch, switch_off, supported):
    cfg = _no_capture_config()
    cfg.ui.panel_glass = not switch_off

    made, calls = _open_with_fake_webview(monkeypatch, cfg, glass_supported=supported)

    assert made["transparent"] is False
    assert "glass" not in made["url"]
    assert calls == ["find"]
    assert made["background_color"] == "#1c1f25"


def test_glass_needs_windows_11_22h2(monkeypatch):
    import sys
    from types import SimpleNamespace

    from argus import desktop

    monkeypatch.setattr(sys, "platform", "win32")
    for build, want in ((19045, False), (22000, False), (22621, True), (26100, True)):
        monkeypatch.setattr(
            sys, "getwindowsversion", lambda b=build: SimpleNamespace(build=b), raising=False
        )
        assert desktop.glass_supported() is want, build

    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop.glass_supported() is False

    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop.glass_supported() is True, "on macOS every supported version can do a transparent window"


def test_a_window_without_a_form_just_stays_opaque():
    """pywebview internals may have changed: a failure must not break opening the window."""
    from argus.desktop import _glass

    class NoForm:
        uid = "nonexistent"

    assert _glass(NoForm(), timeout=0.05) is False
    assert _glass(object(), timeout=0.05) is False


def test_the_page_turns_glass_on_from_the_address(http):
    """Without this the window would be transparent, and the page would paint it solid."""
    page = http.get("/panel/chat?glass=1").text

    assert 'glass=1(&|$)/.test(location.search)' in page
    assert 'documentElement.dataset.glass = "1"' in page
    assert "html[data-glass] body[data-solo] { background:rgba(" in page
