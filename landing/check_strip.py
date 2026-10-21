"""Checks the stripping of comments (`strip_comments.py`): the code is intact, and the look and
behavior are unchanged.

    python check_strip.py

1. The scripts of the stripped page are parsed by node (`--check`): the syntax is not broken.
2. The demo is built in several states, with comments and without; in Chromium both give the
   same screenshots, the same page text and the same list of console errors.
3. The stripped page contains no developer notes about how the app works inside.
"""
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

import build_demo
import build_landing

HERE = Path(__file__).parent
CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"

STATES = [
    dict(freeze=54),
    dict(freeze=33),
    dict(freeze=16),
    dict(variant="solo", freeze=33),
    dict(variant="solo", freeze=54),
]

LEFTOVERS = re.compile(r"pywebview|webview2|chatgpt|cluely|whisper|openai|anthropic|claude|elevenlabs", re.I)


def build(strip: bool, **kw) -> str:
    build_demo.STRIP = strip
    return build_demo.build(lang="en", **kw)


def scripts(doc: str) -> list[str]:
    out = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", doc, re.S | re.I):
        if "src=" in m.group(1) or not m.group(2).strip():
            continue
        if 'type="application/json"' in m.group(1):
            continue
        out.append(m.group(2))
    return out


def node_check(js: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.js"
        f.write_text(js, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        return done.stderr.strip()


def look(browser, html: str, tag: str):
    f = HERE / "out" / f"_strip_{tag}.html"
    f.parent.mkdir(exist_ok=True)
    f.write_text(html, encoding="utf-8")
    try:
        ctx = browser.new_context(viewport={"width": 1200, "height": 700})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f.as_uri())
        page.wait_for_timeout(1500)
        # the running dots of "thinking" and the blinking cursor change from one screenshot to the
        # next by themselves
        page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important;"
                                   "caret-color:transparent!important}")
        page.wait_for_timeout(300)
        shot = page.screenshot()
        text = page.evaluate("document.body.innerText")
        count = page.evaluate("document.querySelectorAll('*').length")
        ctx.close()
        return shot, text, count, errors
    finally:
        f.unlink(missing_ok=True)


# Screenshots differ by a couple of pixels by themselves (verified: two screenshots of the same
# build differ in the same way), so we compare with a tolerance.
NOISE_PIXELS = 50


def differing(a: bytes, b: bytes) -> int:
    d = ImageChops.difference(Image.open(io.BytesIO(a)).convert("RGB"), Image.open(io.BytesIO(b)).convert("RGB"))
    return sum(1 for ch in d.convert("L").point(lambda v: 255 if v else 0).tobytes() if ch)


def main() -> int:
    bad = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        for i, kw in enumerate(STATES):
            keep, cut = build(False, **kw), build(True, **kw)
            for n, js in enumerate(scripts(cut)):
                err = node_check(js)
                if err:
                    bad += 1
                    print(f"state {kw}: script {n} cannot be parsed:\n{err[:600]}")
            a, b = look(browser, keep, f"keep{i}"), look(browser, cut, f"cut{i}")
            pixels = differing(a[0], b[0])
            same = pixels <= NOISE_PIXELS and a[1] == b[1] and a[2] == b[2] and a[3] == b[3]
            bad += not same
            print(f"{kw}: {'the same' if same else 'DIFFERENT'}; differing pixels: {pixels}; "
                  f"{len(keep)} -> {len(cut)} bytes; console errors: {a[3][:2]} / {b[3][:2]}")
            if not same:
                print("   text", a[1] == b[1], "nodes", a[2], b[2])
        browser.close()

    build_demo.STRIP = True
    page = build_landing.build(["en"])
    for n, js in enumerate(scripts(page)):
        err = node_check(js)
        if err:
            bad += 1
            print(f"page: script {n} cannot be parsed:\n{err[:600]}")
    left = sorted({m.group(0).lower() for m in LEFTOVERS.finditer(page)})
    print("page:", len(page), "bytes; traces of the app internals in the source:", left or "none")
    bad += bool(left)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
