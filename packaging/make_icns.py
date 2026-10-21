"""The app icon for macOS — the same drawing as `argus.ico`, but in the macOS layout.

    uv run python packaging/make_icns.py

It is drawn by `make_icon.draw` (all the work on the mark is there, including how it reads at
small sizes). There is one difference: on macOS the icon in the Dock and in Finder is not
cropped by the system, and expects the drawing to already stand inside a canvas with margins
— the rounded square takes about 80% of the canvas (824 out of 1024 points per the Apple
template), and the rest is transparent. Without the margins the icon would look noticeably
larger than its neighbors.

The `.icns` file is assembled by Pillow itself, and `iconutil` (macOS only) is not needed:
this way the icon can be updated on any system.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import make_icon  # noqa: E402 — the path is added by the line above

# Sizes that Pillow puts into the `.icns` (32, 64, 128, 256, 512, 1024 — the system takes the
# rest from these).
SIZES = (32, 64, 128, 256, 512, 1024)

# The share of the canvas for the drawing: per the macOS icon template — 824 out of 1024.
ART = 824 / 1024


def framed(size: int) -> Image.Image:
    """Icon of `size` by `size` points: the picture is centered, with transparent margins around it."""
    art = max(1, round(size * ART))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = (size - art) // 2
    canvas.paste(make_icon.draw(art), (offset, offset))
    return canvas


def main() -> int:
    out = Path(__file__).parent / "argus.icns"
    frames = {n: framed(n) for n in SIZES}
    base = frames[max(SIZES)]
    base.save(out, format="ICNS", append_images=[frames[n] for n in SIZES if n != max(SIZES)])
    print(f"  {out} - {', '.join(str(n) for n in SIZES)} px, margins {round((1 - ART) * 100)}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
