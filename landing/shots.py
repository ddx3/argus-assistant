"""Takes pieces of the real interface (frozen at the right second of the scenario) for the
landing page: the demo poster and the "how it works" frames, in each language.

    python shots.py            all languages
    python shots.py es de      only these
    python shots.py es -- see  only these languages and only the `see` frame

The browser is a local Chromium (playwright); the app is not started.
Result: img/<lang>/<name>.png|jpg
"""
import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

import build_demo

HERE = Path(__file__).parent
OUT = HERE / "img"
CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"


def union(boxes, pad=0):
    x0 = min(b["x"] for b in boxes); y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["width"] for b in boxes); y1 = max(b["y"] + b["height"] for b in boxes)
    return {"x": max(0, x0 - pad), "y": max(0, y0 - pad), "width": x1 - x0 + 2 * pad, "height": y1 - y0 + 2 * pad}


JOBS = [
    # name, build parameters, window, scale, what to shoot, format
    ("poster_dash", dict(freeze=54), (1200, 700), 1, ("page", None), "jpg"),
    ("hear_strip", dict(freeze=16), (1200, 700), 2, ("strip", None), "png"),
    ("hear_rows", dict(freeze=33), (1200, 700), 2, ("sel", ["#raw .row.them", "#raw .row.me"]), "png"),
    ("see", dict(freeze=33), (1200, 700), 2, ("sel", [".shotbox"]), "png"),
    ("ans_text", dict(freeze=54), (1200, 700), 2, ("sel", ['#chat .turn[data-idx="0"]']), "png"),
    ("ans_code", dict(freeze=54), (1200, 700), 2, ("sel", ['#chat .turn[data-idx="1"]']), "png"),
    ("ans_math", dict(freeze=54), (1200, 700), 2, ("sel", ['#chat .turn[data-idx="2"]']), "png"),
    ("solo_poster", dict(variant="solo", freeze=33), (400, 440), 2, ("page", None), "png"),
    # a card with a formula in the narrow answers window: for the final block; at natural size it
    # fits into the column
    ("final_math", dict(variant="solo", freeze=54), (400, 440), 2, ("sel", ['#chat .turn[data-idx="2"]']), "png"),
    # "How it works": five cards (variant C), each with a small window made of these frames. The
    # picker window is real, with a made-up list of windows; there are three frames: nothing is
    # marked, one window is marked, two are marked. They are shot at natural size (1x). Not all
    # the frames below are needed by the page right now: `row_them` is needed by the archived
    # scheme (archive/variant-D).
    ("pick_0", dict(freeze=2, picker={"what": "audio", "picked": []}), (620, 600), 1, ("sheet", ["#picker .sheet"]), "png"),
    ("pick_1", dict(freeze=2, picker={"what": "audio", "picked": [0]}), (620, 600), 1, ("sheet", ["#picker .sheet"]), "png"),
    ("pick_2", dict(freeze=2, picker={"what": "audio", "picked": [0, 1]}), (620, 600), 1, ("sheet", ["#picker .sheet"]), "png"),
    ("row_them", dict(freeze=33), (1200, 700), 2, ("sel", ["#raw .row.them"]), "png"),
    ("row_me", dict(freeze=33), (1200, 700), 2, ("sel", ["#raw .row.me"]), "png"),
    ("see_idle", dict(freeze=2), (1200, 700), 1, ("sel", [".shotbox"]), "png"),
    ("see_code", dict(freeze=33), (1200, 700), 1, ("sel", [".shotbox"]), "png"),
    ("see_math", dict(freeze=54), (1200, 700), 1, ("sel", [".shotbox"]), "png"),
    # answer cards in the narrow answers window (in the column of the scheme block they come out
    # at almost natural size); the card with a formula is `final_math`
    ("card_text", dict(variant="solo", freeze=54), (400, 440), 2, ("sel", ['#chat .turn[data-idx="0"]']), "png"),
    ("card_code", dict(variant="solo", freeze=54), (400, 440), 2, ("sel", ['#chat .turn[data-idx="1"]']), "png"),
    # Cards of "How it works" (five cards, variant C): the microphone chip with the level line at
    # different heights (on the page the frames replace each other quickly and the line
    # "breathes"), and the "thinking" card in the narrow answers window. The chip is shot at triple
    # density: on the page it is shown larger than in the window.
    ("chip_mic_0", dict(freeze=2), (1200, 700), 3, ("sel", ['.src[data-k="mic"]']), "png"),
    ("chip_mic_1", dict(freeze=14.2), (1200, 700), 3, ("sel", ['.src[data-k="mic"]']), "png"),
    ("chip_mic_2", dict(freeze=15.0), (1200, 700), 3, ("sel", ['.src[data-k="mic"]']), "png"),
    ("chip_mic_3", dict(freeze=15.8), (1200, 700), 3, ("sel", ['.src[data-k="mic"]']), "png"),
    ("chip_mic_4", dict(freeze=16.6), (1200, 700), 3, ("sel", ['.src[data-k="mic"]']), "png"),
    ("chip_mic_5", dict(freeze=17.4), (1200, 700), 3, ("sel", ['.src[data-k="mic"]']), "png"),
    ("card_think", dict(variant="solo", freeze=7.5), (400, 440), 2, ("sel", ["#chat .turn.wait"]), "png"),
]


def shoot(browser, lang, name, kw, vp, scale, what, fmt):
    html = build_demo.build(lang=lang, **kw)
    f = HERE / "out" / f"_shot_{lang}_{name}.html"
    f.parent.mkdir(exist_ok=True)
    f.write_text(html, encoding="utf-8")
    ctx = browser.new_context(viewport={"width": vp[0], "height": vp[1]}, device_scale_factor=scale)
    page = ctx.new_page()
    page.goto(f.as_uri())
    page.wait_for_timeout(1400)
    kind, sels = what
    if kind == "page":
        data = page.screenshot()
    elif kind == "sheet":
        # picker window: the background around the sheet is transparent, so that no dark spots are left
        # at the rounded corners
        page.add_style_tag(content="html,body,#picker{background:transparent!important}")
        loc = page.locator(sels[0]).first
        loc.wait_for(state="visible", timeout=5000)
        page.wait_for_timeout(500)
        data = page.screenshot(clip=loc.bounding_box(), omit_background=True)
    elif kind == "strip":
        # The top bar of the window as it looks on desktop: from "Record" to the last source,
        # over the full height of the bar. The back and settings buttons on the left and the window
        # buttons on the right are not needed in the frame: without them the bar fits the column at
        # natural size.
        bar = page.locator(".winbar").first.bounding_box()
        pb = page.locator("#power").first.bounding_box()
        last = page.locator(".sources .src").last.bounding_box()
        data = page.screenshot(clip={"x": pb["x"], "y": bar["y"], "width": last["x"] + last["width"] - pb["x"], "height": bar["height"]})
    else:
        boxes = []
        for sel in sels:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.scroll_into_view_if_needed()
            page.wait_for_timeout(250)
            boxes.append(loc.bounding_box())
        data = page.screenshot(clip=union(boxes))
    ctx.close()
    f.unlink(missing_ok=True)
    img = Image.open(io.BytesIO(data))
    d = OUT / lang
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.{fmt}"
    if fmt == "jpg":
        img.convert("RGB").save(path, quality=84, optimize=True)
    else:
        img.save(path, optimize=True)
    return img.size, path.stat().st_size


def main(argv):
    only_names = None
    if "--" in argv:
        i = argv.index("--")
        argv, only_names = argv[:i], argv[i + 1:]
    langs = argv or build_demo.LANGS
    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        for lang in langs:
            for name, kw, vp, scale, what, fmt in JOBS:
                if only_names and name not in only_names:
                    continue
                size, nbytes = shoot(browser, lang, name, kw, vp, scale, what, fmt)
                total += nbytes
                print(lang, name, size, nbytes)
        browser.close()
    print("total bytes:", total)


if __name__ == "__main__":
    main(sys.argv[1:])
