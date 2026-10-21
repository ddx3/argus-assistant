"""Draw the app icon.

We draw it instead of taking a ready picture: the icon must read at 16 pixels, where only
three or four blobs remain of the drawing. The mark is the letter A without a crossbar, and
in place of the crossbar there is a dot-pupil: Argus is the many-eyed guardian, and a letter
with a pupil reads both as an A and as an eye (chosen from three variants).

The outline of the A is taken from the real Segoe UI Bold letter — the font in which the name
is set in the interface and on the landing page: a flat apex, flat legs, the same stroke
weight. So in the word "Argus" the mark and "rgus" read as one font (requirement: the logo
font must match the font of the rest of the name). All white, with no colored pupil. The same
numbers are in the header of the landing page and of the terms pages (`WM`).

    uv run python packaging/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256)

BACK = (24, 26, 32)          # almost black: on a dark taskbar it does not glow
INK = (255, 255, 255)        # the mark is entirely white, the pupil too

# The outline of the A in font units (capital height = 699): the apex, the legs, the cutout.
GLYPH_W, GLYPH_H = 691.0, 699.0
OUTLINE = (
    (252.5, 0), (438.5, 0), (691, 699), (517, 699),
    (345.5, 127), (174, 699), (0, 699),
)
PUPIL = (345.5, 525.0, 60.0)  # center and radius: where the real A had its crossbar


def _fill(size: int) -> float:
    """What share of the tile the letter takes: the smaller the icon, the larger the mark."""
    if size <= 24:
        return 0.92
    if size <= 32:
        return 0.88
    if size <= 48:
        return 0.80
    return 0.74


def draw(size: int) -> Image.Image:
    # We draw four times larger and shrink down: PIL has no antialiasing on curves, and without it
    # at 16 pixels the edges turn into stairs.
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=BACK)

    k = s * _fill(size) / GLYPH_W                 # a font unit in pixels
    ox = (s - GLYPH_W * k) / 2
    oy = (s - GLYPH_H * k) / 2

    d.polygon([(ox + x * k, oy + y * k) for x, y in OUTLINE], fill=INK)

    cx, cy, r = PUPIL
    d.ellipse((ox + (cx - r) * k, oy + (cy - r) * k, ox + (cx + r) * k, oy + (cy + r) * k), fill=INK)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    out = Path(__file__).parent / "argus.ico"
    frames = [draw(n) for n in SIZES]
    frames[-1].save(out, format="ICO", sizes=[(n, n) for n in SIZES])
    print(f"  {out} — {', '.join(str(n) for n in SIZES)} px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
