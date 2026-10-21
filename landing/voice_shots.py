"""Frames of the "Voice-over" step for the "how it works" scheme: the real voice picker from the
app settings (`voice-picker`, admin.js) with made-up voices. The page is served by the real
`argus.ui.i18n.inject`, and we add the voices to the settings schema (`out/_schema.json`,
taken by `dump_schema.py`). The app is not started.

    python voice_shots.py          English
    python voice_shots.py en ru

Result: img/<lang>/voice_idle.png (a voice is selected) and voice_play.png (a sample is
playing on the selected one: "▶" became "■").
"""
import copy
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from argus.ui import i18n  # noqa: E402

HERE = Path(__file__).parent
STATIC = ROOT / "src" / "argus" / "ui" / "static"
CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
OUT = HERE / "img"

# made-up voices: (slug, name, language, in favorites). The first one is selected.
VOICES = [
    ("mira", "Mira", "en", True),
    ("leo", "Leo", "en", True),
    ("ada", "Ada", "en", False),
    ("lena", "Lena", "de", False),
    ("milo", "Milo", "en", False),
    ("nora", "Nora", "en", False),
    ("sofia", "Sofia", "es", False),
    ("theo", "Theo", "fr", False),
]

LANG = {"value": "en"}
SCHEMA = {}


def schema_with_voices() -> dict:
    s = copy.deepcopy(json.loads((HERE / "out" / "_schema.json").read_text(encoding="utf-8")))
    v = s["values"]
    v["channel.tts.enabled"] = True
    v["channel.tts.active_character"] = VOICES[0][0]
    for slug, name, lang, fav in VOICES:
        base = f"channel.tts.characters.{slug}."
        v[base + "label"] = name
        v[base + "language"] = lang
        v[base + "favorite"] = fav
        v[base + "voice_id"] = "id-" + slug
        v[base + "preview_url"] = "https://example.invalid/" + slug + ".mp3"
    return s


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send(b'{"ok":true}', "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/admin":
            html = (STATIC / "admin.html").read_text(encoding="utf-8")
            self.send(i18n.inject(html, LANG["value"]).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/chrome.css":
            self.send((STATIC / "chrome.css").read_bytes(), "text/css")
        elif path == "/chrome.js":
            self.send((STATIC / "chrome.js").read_bytes(), "application/javascript")
        elif path == "/admin/admin.css":
            self.send((STATIC / "admin.css").read_bytes(), "text/css")
        elif path == "/admin/admin.js":
            self.send((STATIC / "admin.js").read_bytes(), "application/javascript")
        elif path == "/admin/schema":
            self.send(json.dumps(SCHEMA, ensure_ascii=False).encode("utf-8"), "application/json")
        else:
            self.send(b"{}", "application/json")


def main(langs):
    global SCHEMA
    SCHEMA = schema_with_voices()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        for lang in langs:
            LANG["value"] = lang
            ctx = browser.new_context(viewport={"width": 480, "height": 900}, device_scale_factor=2)
            page = ctx.new_page()
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)[:120]))
            page.goto(base + "/admin")
            page.wait_for_timeout(1500)
            box = page.locator('[data-widget="voice-picker"]').first
            box.wait_for(state="visible", timeout=5000)
            for name, playing in (("voice_idle", False), ("voice_play", True)):
                page.evaluate("""(on) => {
                    document.querySelectorAll('.vp-play').forEach((b) => { b.classList.remove('playing'); b.textContent = '▶'; });
                    if (on) { const b = document.querySelector('.vp-item.active .vp-play'); b.classList.add('playing'); b.textContent = '■'; }
                }""", playing)
                page.mouse.move(0, 0)
                page.wait_for_timeout(250)
                data = box.screenshot()
                img = Image.open(io.BytesIO(data))
                d = OUT / lang
                d.mkdir(parents=True, exist_ok=True)
                img.save(d / f"{name}.png", optimize=True)
                print(lang, name, img.size)
            if errs:
                print("page errors:", errs[:3])
            ctx.close()
        browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:] or ["en"])
