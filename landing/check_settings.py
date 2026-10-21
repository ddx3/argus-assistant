"""The app's settings page in the chosen languages: a screenshot and a list of the strings that
are still in Russian. The page is served by the real `argus.ui.i18n.inject`; the settings
schema is built by the real panel (`dump_schema.py`, with the project's Python) on an empty
config; the app is not started.

    ..\\.venv\\Scripts\\python dump_schema.py
    python check_settings.py de tr
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from argus.ui import i18n  # noqa: E402

STATIC = ROOT / "src" / "argus" / "ui" / "static"
CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
OUT = Path(__file__).parent / "out"


SCHEMA = json.loads((Path(__file__).parent / "out" / "_schema.json").read_text(encoding="utf-8"))
LANG = {"value": "en"}


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


LEFT = """() => {
  const out = new Set();
  const cyr = /[А-Яа-яЁё]/;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const p = n.parentNode;
    if (p && /^(SCRIPT|STYLE|TEXTAREA)$/.test(p.tagName)) continue;
    if (cyr.test(n.nodeValue)) out.add(n.nodeValue.trim().slice(0, 90));
  }
  for (const el of document.querySelectorAll('[title],[placeholder],[aria-label],[alt]'))
    for (const a of ['title','placeholder','aria-label','alt']) {
      const v = el.getAttribute(a); if (v && cyr.test(v)) out.add(a + ': ' + v.slice(0, 80));
    }
  for (const o of document.querySelectorAll('option')) if (cyr.test(o.textContent)) out.add('option: ' + o.textContent.trim().slice(0, 60));
  return [...out];
}"""


def main(langs):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        for lang in langs:
            LANG["value"] = lang
            page = browser.new_page(viewport={"width": 1000, "height": 900})
            errs = []
            page.on("pageerror", lambda e: errs.append(str(e)[:120]))
            page.goto(base + "/admin")
            page.wait_for_timeout(1500)
            left = page.evaluate(LEFT)
            page.screenshot(path=str(OUT / f"settings_{lang}.png"), full_page=True)
            print(f"== {lang}: still in Russian: {len(left)}; page errors: {errs[:3]}")
            if lang not in ("ru", "uk"):
                for s in left:
                    print("   ", s)
            page.close()
        browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:] or ["en"])
