"""Server pages (password recovery, redirect to the payment page, "return to Argus") are
flat, like the app windows.

Requirement: no outlines anywhere. The emails are guarded by
`test_server_mailtemplate.py`; the pages that the emails and the payment provider link
to are guarded by this file: no borders around the card, the field and the button, and
no shadows; an edge is allowed only on field focus.
"""

from __future__ import annotations

import re

import pytest

from argus.server.app import DONE_PAGE, LIQPAY_PAGE, RESET_PAGE

PAGES = {"reset": RESET_PAGE, "done": DONE_PAGE, "liqpay": LIQPAY_PAGE}


def style_of(page: str) -> str:
    return re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)


@pytest.mark.parametrize("name", PAGES)
def test_no_borders_or_shadows(name):
    css = re.sub(r"/\*.*?\*/", "", style_of(PAGES[name]), flags=re.DOTALL)      # comments do not count
    for rule in re.findall(r"[^{}]+\{[^}]*\}", css):
        selector = rule.split("{")[0].strip()
        body = rule.split("{")[1]
        if ":focus" in selector:
            continue                        # an edge on focus, like the fields in the app windows
        assert not re.search(r"border(?!-radius)\s*:\s*(?!0\b)", body), (name, selector)
        assert "box-shadow" not in body and "outline:" not in body.replace("outline:none", ""), (name, selector)


def test_the_reset_button_is_the_flat_plate():
    css = style_of(RESET_PAGE)
    button = re.search(r"\n button \{([^}]*)\}", css).group(1)
    assert "background:#1d2025" in button and "border:0" in button and "border-radius:6px" in button
    assert "color:#9aa1ad" in button


def test_the_done_page_tells_where_to_write():
    assert "support@arguspilot.com" in DONE_PAGE
