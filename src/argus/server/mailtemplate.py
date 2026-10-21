"""Design of Argus emails: one shared design, in the style of the app.

Colors and shape are the same tokens as in `ui/static/chrome.css`: an even dark background,
a `--win` card, a button that is a flat `.cta` pill without an edge (in the app's windows
`--btn-edge` is transparent), a dim label, and the Argus mark on top. No outlines and no
lines: the button must match the app's style, and the app has no outlines anywhere. Text in
moderation: a heading, a couple of clear phrases, and the needed details (neither too much
text nor too plain).

Mail programs do not understand `rgba()` and CSS variables, so the app's transparent fills
are recomputed into solid colors (`PALETTE`; a test checks them against `chrome.css`), and
the layout uses tables with styles right in the tags: this is how both Gmail and Outlook
read it.

Each email has two parts: text (read by clients without HTML) and markup. The link is in
both. The mark is a picture attached to the email itself (`cid:`), not a link to the
website: the website on the domain is closed, and mailboxes block pictures by link by
default.

A new email is a new function in `mailevents.py` that calls `render`; an email must not
have its own layout. The bottom line (the email is automatic, where to write) is in the
email's language, from `mail_texts/<language>.json`.
"""
from __future__ import annotations

import html
import struct
from dataclasses import dataclass, field
from pathlib import Path

from argus.server import mailtexts

# Where to write. The sender address (`noreply@`) does not accept mail, and `support@` is
# forwarded to the owner.
SUPPORT = "support@arguspilot.com"

# The app's tokens recomputed into solid colors (an email has no transparency).
PALETTE = {
    "page": "#0a0c10",        # --bg: background
    "card": "#101318",        # --win: card
    "text": "#f2f4f7",        # --text
    "dim": "#9aa1ad",         # --dim: button label, captions
    "faint": "#5b6270",       # --faint: bottom line
    "btn": "#1d2025",         # --btn in windows: white .055 over the card, no edge
}

FONT = "'Segoe UI',system-ui,-apple-system,'Helvetica Neue',Helvetica,Arial,sans-serif"

LOGO_CID = "argus-logo"
LOGO_SHOWN_SCALE = 3      # how many times larger the picture is than its displayed size (`deploy/server/make_mail_logo.py`)
_LOGO_PATH = Path(__file__).with_name("mail_logo.png")


def _logo() -> tuple[bytes, int, int]:
    """The Argus mark: PNG bytes and the size to show it at (the picture is `LOGO_SHOWN_SCALE` times
    larger).
    """
    data = _LOGO_PATH.read_bytes()
    width, height = struct.unpack(">II", data[16:24])
    return data, round(width / LOGO_SHOWN_SCALE), round(height / LOGO_SHOWN_SCALE)


@dataclass(frozen=True)
class Message:
    """A ready email: subject, text, markup and the pictures attached to it (`cid` -> PNG)."""

    subject: str
    text: str
    html: str
    images: dict[str, bytes] = field(default_factory=dict)


def render(*, subject: str, heading: str, paragraphs: list[str] | None = None,
           rows: list[tuple[str, str]] | None = None, button: tuple[str, str] | None = None,
           note: str = "", lang: str = "ru", footer: str | None = None) -> Message:
    """Build an email: a heading, lines of text, optionally "name - value" rows and a button, and
    a small note.

    All text is escaped: only what is passed here gets into the email, but escaping is not
    "on trust". `button` is (label, link); in the text part the link goes on a separate line so
    that it can be copied.
    """
    paragraphs = [p for p in (paragraphs or []) if p.strip()]
    footer = (footer if footer is not None else mailtexts.footer(lang)).format(support=SUPPORT)
    logo, logo_w, logo_h = _logo()
    c = PALETTE
    esc = html.escape

    body = [f'<h1 style="margin:0 0 12px;font-size:17px;line-height:1.4;font-weight:600;color:{c["text"]};">'
            f'{esc(heading)}</h1>']
    for paragraph in paragraphs:
        body.append(f'<p style="margin:0 0 12px;font-size:15px;line-height:1.6;color:{c["text"]};">'
                    f'{esc(paragraph)}</p>')
    if rows:
        cells = "".join(
            f'<tr><td style="padding:3px 0;font-size:15px;line-height:1.6;color:{c["dim"]};">{esc(name)}</td>'
            f'<td align="right" style="padding:3px 0 3px 16px;font-size:15px;line-height:1.6;font-weight:600;'
            f'color:{c["text"]};">{esc(value)}</td></tr>'
            for name, value in rows)
        body.append(f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
                    f'style="margin:0 0 12px;">{cells}</table>')
    if button:
        label, url = button
        # The flat `.cta` pill: height 35, padding 14, radius 6, `--btn` fill, no edge, dim label.
        body.append(
            f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:18px 0 6px;">'
            f'<tr><td bgcolor="{c["btn"]}" style="background:{c["btn"]};border-radius:6px;">'
            f'<a href="{esc(url, quote=True)}" style="display:inline-block;padding:8px 14px;font-family:{FONT};'
            f'font-size:15px;line-height:19px;color:{c["dim"]};text-decoration:none;">{esc(label)}</a>'
            f'</td></tr></table>')
    if note:
        body.append(f'<p style="margin:12px 0 0;font-size:13px;line-height:1.6;color:{c["dim"]};">{esc(note)}</p>')

    page = f"""<!doctype html>
<html lang="{esc(lang)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:{c["page"]};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(paragraphs[0] if paragraphs else heading)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="{c["page"]}" \
style="background:{c["page"]};">
<tr><td align="center" style="padding:32px 16px;font-family:{FONT};">
<table role="presentation" width="440" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:440px;">
<tr><td style="padding:0 2px 18px;"><img src="cid:{LOGO_CID}" width="{logo_w}" height="{logo_h}" alt="Argus" \
style="display:block;border:0;width:{logo_w}px;height:{logo_h}px;color:{c["text"]};font-size:22px;font-weight:700;"></td></tr>
<tr><td bgcolor="{c["card"]}" style="background:{c["card"]};border-radius:12px;padding:24px 26px;font-family:{FONT};">
{chr(10).join(body)}
</td></tr>
<tr><td style="padding:14px 2px 0;font-family:{FONT};font-size:12px;line-height:1.6;color:{c["faint"]};">{esc(footer)}</td></tr>
</table></td></tr></table></body></html>"""

    lines = ["Argus", "", heading, ""]
    for paragraph in paragraphs:
        lines += [paragraph, ""]
    for name, value in rows or []:
        lines.append(f"{name}: {value}")
    if rows:
        lines.append("")
    if button:
        lines += [button[1], ""]
    if note:
        lines += [note, ""]
    lines += ["--", footer]
    return Message(subject=subject, text="\n".join(lines), html=page, images={LOGO_CID: logo})
