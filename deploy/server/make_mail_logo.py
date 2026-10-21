"""Draw the logo for mails: `src/argus/server/mail_logo.png`.

Mail clients do not show SVG, so the Argus mark in a mail is an image attached to the mail
itself (not a link to the site: the site on the domain may be closed, and an image loaded by
link is blocked in the mailbox).

The same outline as in the app (`ui/static/home.html`): the letter A without a crossbar and a
dot, then "rgus" in Segoe UI Bold, white on transparent. It is drawn 4 times larger and then
downscaled, which makes the edges smooth. The image is three times larger than shown (the
mail displays it at `LOGO_SHOWN_W` pixels), so that it is not blurry on high-density screens.

Run (needs Pillow and a font from Windows): `python deploy/server/make_mail_logo.py`.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[2] / "src" / "argus" / "server" / "mail_logo.png"
FONT = Path("C:/Windows/Fonts/segoeuib.ttf")

SHOWN_FONT_PX = 26      # like "Argus" in the landing header (`.brand{font-size:26px}`)
DENSITY = 3             # image pixels per displayed pixel
SUPER = 4               # how many times larger we draw than we save

MARK_W, MARK_H = 691, 699
MARK_PATH = [(252.5, 0), (438.5, 0), (691, 699), (517, 699), (345.5, 127), (174, 699), (0, 699)]
DOT = (345.5, 525, 60)


def main() -> None:
    size = SHOWN_FONT_PX * DENSITY * SUPER
    font = ImageFont.truetype(str(FONT), size)
    mark_h = 0.7 * size                      # like `.wm svg{height:.7em}` in the app
    scale = mark_h / MARK_H
    mark_w = MARK_W * scale
    pad = size // 4
    baseline = size + pad
    canvas = Image.new("RGBA", (int(mark_w + size * 3) + pad * 2, int(size * 1.6) + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    top = baseline - mark_h
    draw.polygon([(pad + x * scale, top + y * scale) for x, y in MARK_PATH], fill=(255, 255, 255, 255))
    cx, cy, r = DOT
    # The dot lies inside the opening of the letter and is white too (in the app both are
    # `fill:currentColor`).
    draw.ellipse([pad + (cx - r) * scale, top + (cy - r) * scale,
                  pad + (cx + r) * scale, top + (cy + r) * scale], fill=(255, 255, 255, 255))
    draw.text((pad + mark_w - 0.015 * size, baseline), "rgus", font=font, fill=(255, 255, 255, 255), anchor="ls")
    canvas = canvas.crop(canvas.getbbox())
    width = round(canvas.width / SUPER)
    height = round(canvas.height / SUPER)
    canvas = canvas.resize((width, height), Image.LANCZOS)
    canvas.save(OUT, optimize=True)
    print(f"{OUT}: {width}x{height}px, show as {width / DENSITY:.0f}x{height / DENSITY:.0f}")


if __name__ == "__main__":
    main()
