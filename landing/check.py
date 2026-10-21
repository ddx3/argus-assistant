"""Looks at the built page in Chromium: errors, horizontal scrolling, screenshots.
python check.py [language ...]
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
f = Path(__file__).parent / "out" / "landing.html"
langs = sys.argv[1:] or ["en"]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
    for lang in langs:
        for name, vp in (("desk", (1280, 900)), ("phone", (390, 844))):
            ctx = b.new_context(viewport={"width": vp[0], "height": vp[1]}, locale="ru-RU")
            # the default language is English and the person's choice is stored: set it in advance
            ctx.add_init_script(f"try{{localStorage.setItem('argus.landing.lang','{lang}')}}catch(e){{}}")
            pg = ctx.new_page()
            msgs = []
            pg.on("console", lambda m: msgs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: msgs.append(str(e)))
            pg.goto(f.as_uri())
            pg.wait_for_timeout(6500)
            ov = pg.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth, document.documentElement.scrollHeight, document.documentElement.lang, (document.getElementById('lang-btn') || {textContent: '-'}).textContent.trim()]")
            frames = pg.evaluate("[...document.querySelectorAll('iframe')].map(i=>i.className)")
            broken = pg.evaluate("[...document.images].filter(i=>i.getAttribute('data-img') && !i.complete || (i.complete && i.naturalWidth===0 && i.getAttribute('data-img'))).map(i=>i.getAttribute('data-img'))")
            print(lang, name, ov, frames, "broken:", broken, "errors:", msgs[:4])
            pg.screenshot(path=str(f.parent / f"page_{lang}_{name}.png"), full_page=True)
            ctx.close()
    b.close()
