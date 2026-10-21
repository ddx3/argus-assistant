"""Markup and styles of the working screen (`index.html`) that cannot be checked by
behavior: agreements broken by the findings of the 2026-10-14 audit (`css-design-1`,
`css-design-3`, `css-design-10`, `css-design-15`).

Behavior (questions, paste, socket, keys) is in `test_index_page_node.py`; here is what
is visible only in the page text: where an element sits, and what attributes and rules
it has.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from argus.ui.server import STATIC

VOID = {"meta", "link", "img", "input", "br", "hr", "source", "area", "base", "col", "wbr"}


def _page() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _markup() -> str:
    """The page markup without the main script: its strings contain tags of their own, and the
    parser would get confused by them.
    """
    html = _page()
    return html[: html.index("<script>\nconst $ = (id) => document.getElementById(id);")]


def _styles() -> str:
    """The page styles and the shared `chrome.css`, without comments."""
    html = _page()
    own = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.DOTALL))
    css = own + "\n" + (STATIC / "chrome.css").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


class _Walk(HTMLParser):
    """For each element: who the parent is, and what lies inside `<header>`."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.parent: dict[str, str | None] = {}
        self.attrs: dict[str, dict[str, str | None]] = {}
        self.in_header: list[tuple[str, str | None, list[str]]] = []

    def _open(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if "header" in self.stack:
            self.in_header.append((tag, a.get("id"), (a.get("class") or "").split()))
        if a.get("id"):
            self.parent[a["id"]] = self.stack[-1] if self.stack else None
            self.attrs[a["id"]] = a

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass


def _walk() -> _Walk:
    walk = _Walk()
    walk.feed(_markup())
    return walk


def _fixed_tokens(css: str) -> set[str]:
    """Classes and ids to which some rule gives `position:fixed` (by the last compound selector)."""
    tokens: set[str] = set()
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if not re.search(r"position\s*:\s*fixed", body):
            continue
        for selector in selectors.split(","):
            last = re.split(r"[\s>+~]+", selector.strip())[-1]
            tokens |= set(re.findall(r"[.#][\w-]+", last))
    return tokens


# --------------------------------------------------------------------------
# css-design-1: nothing fixed lies in the header
# --------------------------------------------------------------------------


def test_no_fixed_element_sits_inside_the_header():
    """The header has `backdrop-filter` (`chrome.css`), and such an ancestor becomes the reference
    frame for `position:fixed` of everything inside it: the failure window counted `bottom` from
    the edge of the header and was drawn above the app window, so the reason "didn't turn on"
    and the "Reconnect audio" button were not visible at all."""
    inside: set[str] = set()
    for _tag, ident, classes in _walk().in_header:
        inside |= {f".{c}" for c in classes} | ({f"#{ident}"} if ident else set())
    stuck = inside & _fixed_tokens(_styles())
    assert not stuck, f"`<header>` holds what the styles make `position:fixed`: {sorted(stuck)}"


def test_the_trouble_window_is_a_direct_child_of_the_body_and_stays_out_of_detached_windows():
    walk = _walk()
    assert walk.parent["alarm"] == "body"
    assert walk.parent["reopen"] == "div"
    html = _page()
    # detached windows have no header, and the failure window used to be hidden together with
    # it; that stays as it was
    assert "body[data-solo] .alarm { display:none; }" in html
    # the failure window has a role for the screen reader, and its `z-index` is not above the
    # choice and settings windows
    assert walk.attrs["alarm"].get("role") == "alert"
    assert re.search(r"\.alarm \{ position:fixed;[^}]*z-index:20;", html)


# --------------------------------------------------------------------------
# css-design-10: the shared `.t` does not reach into the settings window
# --------------------------------------------------------------------------


def test_the_time_rule_is_scoped_to_feed_rows():
    """The settings window is mounted into this same document; the generic `.t` leaked into the
    caption of the upload zone.
    """
    html = _page()
    assert ".row .t { color:var(--dim);" in html
    assert not re.search(r"^\s*\.t \{", html, re.MULTILINE)


# --------------------------------------------------------------------------
# css-design-3: the switch and the sources are reachable from the keyboard, focus is visible
# --------------------------------------------------------------------------


def test_the_recording_switch_and_the_source_parts_are_reachable_from_the_keyboard():
    walk = _walk()
    power = walk.attrs["power"]
    assert power.get("role") == "switch" and power.get("tabindex") == "0"
    assert power.get("aria-checked") == "false" and power.get("aria-labelledby") == "power-label"
    markup = _markup()
    for key in ("mic", "loopback", "screen"):
        label = re.search(rf'<label class="src" data-k="{key}">(.*?)</label>', markup, re.DOTALL)
        assert label, key
        assert re.search(r'<span class="box" role="switch" tabindex="0" aria-checked="false" '
                         r'aria-label="[^"]+"></span>', label.group(1)), f"{key}: the box has no role"
    # the names that open the window choice are buttons; for the microphone the choice is a
    # real `<select>`
    for key in ("loopback", "screen"):
        label = re.search(rf'<label class="src" data-k="{key}">(.*?)</label>', markup, re.DOTALL).group(1)
        assert 'class="name" role="button" tabindex="0" aria-haspopup="dialog"' in label
    assert '<span class="name">Microphone</span>' in markup


def test_keyboard_focus_has_a_visible_ring_on_the_switch_and_its_parts():
    css = _styles()
    assert re.search(r"\.power:focus-visible \{ outline:2px solid var\(--focus\);", css)
    assert re.search(r"\.src \.box:focus-visible, \.src \.name\[role=\"button\"\]:focus-visible "
                     r"\{ outline:2px solid var\(--focus\);", css)
    # an invisible device list (`opacity:0`) takes its outline away with it: the ring is on its
    # wrapper
    assert re.search(r"\.src \.dev-area:has\(select\.dev:focus-visible\) \{ outline:2px solid", css)
    html = _page()
    assert "document.addEventListener(\"keydown\", (e) => {\n  if ((e.key !== \"Enter\" && e.key !== \" \")" in html


def test_new_answers_are_announced_from_a_hidden_region_and_the_feed_is_not_live():
    """The feed is redrawn as a whole, and a screen reader on it would reread all the answers on
    every change.
    """
    walk = _walk()
    live = walk.attrs["chat-live"]
    assert live.get("aria-live") == "polite" and live.get("role") == "status"
    assert "sr-only" in (live.get("class") or "")
    assert "aria-live" not in walk.attrs["chat"] and walk.attrs["chat"].get("role") != "log"
    assert ".sr-only {" in _page()


# --------------------------------------------------------------------------
# css-design-15 and relatives: there are no hover tooltips
# --------------------------------------------------------------------------


def test_the_work_screen_has_no_title_tooltips():
    """Hover tooltips were removed (2026-10-08): icons have only `aria-label`. A link from an
    answer kept its address in `title`; now the address is visible as text."""
    html = _page()
    assert not re.search(r"""[\s'"`]title=["']""", html), "index.html has a title attribute again"
    assert not re.search(r"\.title\s*=[^=]", html.replace("document.title", "")), "element.title = …"
    assert 'class="lnk" title=' not in html
