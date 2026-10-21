"""Email design: in the app's style, safe, and with no external dependencies.

Requirements: all emails match the style of our app; the button matches it too; there are
no outlines anywhere; less text, simpler. The email is assembled in one place
(`mailtemplate.render`), so the style is guarded here too: the colors and the button must
not drift from the app (we check against the real `chrome.css`), there must be no
outlines, foreign text must not become markup, and the email must not pull anything from
the network.
"""

from __future__ import annotations

import html as htmllib
import re
import struct
from pathlib import Path

from argus.server import mailevents, mailtemplate

STATIC = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static"
LINK = "https://api.arguspilot.com/auth/reset#token=abc_DEF-123"


def reset(lang: str = "en") -> mailtemplate.Message:
    return mailevents.reset_password("user@example.com", LINK, 30, lang)[1]


def css() -> str:
    return (STATIC / "admin.css").read_text(encoding="utf-8") + (STATIC / "chrome.css").read_text(encoding="utf-8")


def blend(alpha: float, base_hex: str) -> tuple[int, ...]:
    """White at opacity `alpha` over `base_hex`, as the eye sees it in the app."""
    base = [int(base_hex[i:i + 2], 16) for i in (1, 3, 5)]
    return tuple(round(b + (255 - b) * alpha) for b in base)


def rgb(hex_: str) -> tuple[int, ...]:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def visible_text(page: str) -> str:
    """What a person reads in the markup: without tags and without the hidden preview line."""
    body = page.split("<body", 1)[1].split(">", 1)[1]
    body = re.sub(r"<div style=\"display:none[^>]*>.*?</div>", "", body, flags=re.DOTALL)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


def test_the_reset_letter_has_text_and_html_with_the_same_link_and_ttl():
    m = reset()
    assert m.subject == "Argus password reset"
    # The link in the text goes on its own line: it is copied whole, with nothing extra around
    # it.
    assert LINK in m.text.splitlines()
    assert f'href="{LINK}"' in m.html
    assert "30 minutes" in m.text and "30 minutes" in m.html


def test_the_html_uses_only_the_apps_colours():
    """Every color in the markup comes from the `PALETTE` table, and that table matches the app's
    tokens.
    """
    used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", reset().html)}
    assert used, "the email has no colors at all - the layout is lost"
    assert used <= {v.lower() for v in mailtemplate.PALETTE.values()}, \
        f"colors outside the palette: {used - {v.lower() for v in mailtemplate.PALETTE.values()}}"
    assert mailtemplate.PALETTE["page"] in reset().html and mailtemplate.PALETTE["card"] in reset().html


def test_the_palette_matches_the_app_tokens():
    """If the app's tokens change, the letter must change together with them, not silently fall
    behind.
    """
    styles = css().lower()
    for name in ("page", "card", "text", "dim", "faint"):
        assert mailtemplate.PALETTE[name].lower() in styles, \
            f"email color '{name}' {mailtemplate.PALETTE[name]} is not found in the app styles"


def test_there_are_no_outlines_anywhere():
    """We use no outlines at all: no borders, no divider lines, no shadows, neither on the card
    nor on the button nor on the rows.
    """
    full = mailtemplate.render(subject="Subject", heading="Heading", paragraphs=["Paragraph"], rows=[("Name", "Value")],
                               button=("Button", LINK), note="Footnote")
    for page in (reset().html, full.html):
        assert not re.search(r"border(?!-radius|:0\b|=)", page), "the email has an outline"
        for bad in ("<hr", "box-shadow", "outline", "text-shadow"):
            assert bad not in page, bad


def test_the_button_is_the_apps_flat_plate():
    """The email button is the `.cta` from the app's windows: a `--btn` fill with no edge,
    rounding 6, height 35, a dim label.

    The values are taken from the real `chrome.css`, not written here from memory: the first
    version of the email took a dense plate with a bright edge, the second the recovery page's
    button with a thin edge, and both were off-style.
    """
    styles = css()
    flat = re.search(r"--btn:rgba\(255,255,255,([\d.]+)\);\s*--btn-edge:transparent", styles)
    assert flat, "chrome.css has no flat window tokens"
    want = blend(float(flat.group(1)), mailtemplate.PALETTE["card"])
    got = rgb(mailtemplate.PALETTE["btn"])
    assert all(abs(a - b) <= 1 for a, b in zip(want, got)), f"button fill {got} != {want}"

    cta = re.search(r"^\.cta \{([^}]*)\}", styles, re.MULTILINE).group(1)
    radius = re.search(r"border-radius:\s*(\d+)px", cta).group(1)
    height = int(re.search(r"height:\s*(\d+)px", cta).group(1))
    pad = re.search(r"padding:\s*0 (\d+)px", cta).group(1)
    assert "color:var(--dim)" in cta.replace(" ", "")

    page = reset().html
    assert f"border-radius:{radius}px" in page
    assert f"padding:8px {pad}px" in page
    assert f"line-height:{height - 16}px" in page, "the button height (8+8 padding and the line) must give the height of `.cta`"
    assert f"color:{mailtemplate.PALETTE['dim']};text-decoration:none" in page, "the button label must be dim"
    assert "display:inline-block" in page, "`.cta` is as wide as its label, not the whole card"


def test_the_button_is_a_quiet_grey_plate_not_an_accent():
    for hex_ in set(re.findall(r"#([0-9a-fA-F]{6})\b", reset().html)):
        r, g, b = (int(hex_[i:i + 2], 16) for i in (0, 2, 4))
        # The app's grays are slightly cool (`--faint:#5b6270`, spread 21); a real accent
        # (`#4f8cff`) gives about 170.
        assert max(r, g, b) - min(r, g, b) <= 30, f"#{hex_} is a colored accent"


def test_the_letter_pulls_nothing_from_the_network():
    """The image is attached to the letter, and there are no fonts or styles loaded by link: the
    mailbox neither blocks nor tracks.
    """
    m = reset()
    assert 'src="cid:argus-logo"' in m.html
    for bad in ("<script", "<link", "@import", "url(", "<iframe", 'src="http', "src='http"):
        assert bad not in m.html, bad
    assert list(m.images) == ["argus-logo"]
    assert m.images["argus-logo"].startswith(b"\x89PNG\r\n\x1a\n")


def test_the_logo_is_shown_at_a_third_of_its_pixels():
    m = reset()
    width, height = struct.unpack(">II", m.images["argus-logo"][16:24])
    shown_w = round(width / mailtemplate.LOGO_SHOWN_SCALE)
    shown_h = round(height / mailtemplate.LOGO_SHOWN_SCALE)
    assert f'width="{shown_w}" height="{shown_h}"' in m.html


def test_text_that_looks_like_markup_stays_text():
    hostile = '<script>alert(1)</script> "&\''
    m = mailtemplate.render(subject=hostile, heading=hostile, paragraphs=[hostile],
                            rows=[(hostile, hostile)],
                            button=(hostile, 'https://x.example/?a="onmouseover="x'), note=hostile)
    assert "<script>" not in m.html
    assert 'href="https://x.example/?a=&quot;onmouseover=&quot;x"' in m.html
    assert "&lt;script&gt;" in m.html
    # The text part is plain text: there is no need to escape markup in it, and it must not
    # change the text.
    assert hostile in m.text


def test_a_letter_without_a_button_has_no_link_block():
    m = mailtemplate.render(subject="Subject", heading="Heading", paragraphs=["Paragraph"])
    assert "href=" not in m.html
    assert "http" not in m.text
    assert m.text.startswith("Argus\n\nHeading\n\nParagraph")


def test_a_letter_with_only_a_heading_is_fine():
    m = mailtemplate.render(subject="Subject", heading="Heading")
    assert "Heading" in m.html and m.text.startswith("Argus\n\nHeading")


def test_every_letter_says_where_to_write_in_the_footer():
    for lang in ("ru", "en"):
        m = reset(lang)
        assert mailtemplate.SUPPORT in m.text and mailtemplate.SUPPORT in m.html


def test_the_reset_letter_is_neither_bare_nor_a_wall_of_text():
    """Feedback of 2026-10-15: first there was too much text everywhere, then (after it was cut
    down to one line) it became too plain and the normal look had to come back. We keep the
    middle: a couple of clear phrases and a button, but not a wall of text."""
    text = visible_text(reset().html)
    assert 120 <= len(text) <= 300, f"{len(text)} characters: {text!r}"
