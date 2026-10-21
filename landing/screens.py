"""Frames of the "Sessions" (main) and "Record" pages for the landing page.

The pages are real: `home.html` and `record.html` from `src/argus/ui/static/`, with their own
scripts and styles, with the real translation engine and dictionaries. The address has
`desk=1`, as in the app's main window: the page draws its own top bar with the window buttons
(`winbar.js`), so the frame looks like a desktop window, not like a browser tab. Made-up data
(`scenarios/screens/<lang>.json`) is substituted for the server: a list of sessions and one
conversation record with an answer. The app is not started; the browser is a local Chromium
(playwright), and no window is shown.

    python screens.py            all languages that have data
    python screens.py en         only this one
    python screens.py en -- sessions   only this language and this frame

Result: img/<lang>/sessions.png, img/<lang>/record.png.

The "Record" page is not translated in the app itself yet: its captions are missing from the
app's dictionaries. So that a frame does not come out with untranslated captions, the captions
are taken from `scenarios/screens/<lang>.ui.json` (source string -> translation) and are
added to the app's dictionary only here. Once the page is translated in the app, the file can
be deleted.
"""
import io
import json
import mimetypes
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
SRC = HERE.parents[0] / "src" / "argus" / "ui" / "static"
DATA = HERE / "scenarios" / "screens"
OUT = HERE / "img"
CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
ORIGIN = "http://argus.local"

# We connect to nothing: the main page keeps a websocket to the server, and there is no server.
# One state snapshot, as from a real server: from it the main page shows the settings gear.
STUB = """
window.WebSocket = function () {
  var self = this;
  this.close = function () {};
  this.send = function () {};
  setTimeout(function () {
    if (self.onmessage) self.onmessage({ data: JSON.stringify({ admin: true, controls: { running: false }, account_hint: "" }) });
  }, 50);
};
window.__ARGUS_STAND__ = true;
"""


def dictionary(lang: str, extra: dict) -> dict:
    """The app dictionary plus labels that are not in it yet (for frames only)."""
    base = {}
    p = SRC / "i18n" / f"{lang}.json"
    if lang != "en" and p.exists():
        base = {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if isinstance(v, str) and v}
    base.update(extra)
    return base


def inject(html: str, lang: str, extra: dict) -> str:
    """Same as `argus.ui.i18n.inject`: language, dictionary and engine first in `<head>`."""
    # The landing page has no window buttons (minimize, restore, close). The rest of the bar is the
    # same as on desktop.
    block = "<style>.winbar-btns{display:none!important}</style>" + f"<script>window.ARGUS_LANG={json.dumps(lang)};</script>"
    d = dictionary(lang, extra)
    if d:
        engine = (SRC / "i18n" / "engine.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
        data = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
        block += f"<script>window.ARGUS_I18N={data};</script><script>{engine}</script>"
    i = html.lower().find("<head>")
    return html[: i + 6] + block + html[i + 6:] if i >= 0 else block + html


def today_at(day_back: int, hh: int, mm: int) -> float:
    """A timestamp: today (or `day_back` days ago) at the given hours and minutes by this machine's
    clock.
    """
    now = datetime.now()
    d = datetime(now.year, now.month, now.day, hh, mm)
    return d.timestamp() - day_back * 86400


class Stand:
    """Answers whatever the pages ask of the server."""

    def __init__(self, lang: str):
        self.lang = lang
        self.mode = "final"          # "empty": the record has no answers yet ("how it works" frames); "final": with an answer
        self.hold = []               # requests the server "does not answer": the page stays in "Reading the recording…"
        self.data = json.loads((DATA / f"{lang}.json").read_text(encoding="utf-8"))
        ui = DATA / f"{lang}.ui.json"
        self.extra = json.loads(ui.read_text(encoding="utf-8")) if ui.exists() else {}

    # -- data ---------------------------------------------------------
    def chats(self) -> dict:
        items = [{"id": c["id"], "name": c["name"], "about": "", "active": False,
                  "updated": today_at(c["day"], *c["at"])} for c in self.data["sessions"]]
        return {"ok": True, "chats": items, "running": False}

    def record(self) -> dict:
        d = self.data["record"]
        lines = [{"key": f"k{i}", "t": t, "who": who, "text": text} for i, (t, who, text) in enumerate(d["lines"])]
        results = [{"id": r["id"], "ts": 0, "request": r["request"], "text": r["text"],
                    "pdf": {"name": r["pdf"]} if r.get("pdf") else None} for r in d["results"]]
        if self.mode == "empty":
            results = []
        return {"ok": True, "chat": {"id": d["id"], "name": d["name"]}, "recording": False,
                "lines": lines, "results": results,
                "stats": {"lines": len(lines), "chars": sum(len(x[2]) for x in d["lines"]), "tokens": 0}}

    # -- routes -------------------------------------------------------
    def handle(self, route):
        req = route.request
        path = req.url[len(ORIGIN):].split("?")[0] if req.url.startswith(ORIGIN) else None
        if path is None:
            return route.abort()
        if req.method == "POST" and path.endswith("/request"):
            self.hold.append(route)
            return
        if path == "/" or path == "/home":
            return self.page(route, "home.html")
        if path.startswith("/chat/") and path.endswith("/record"):
            return self.page(route, "record.html")
        if path == "/chats":
            return self.json(route, self.chats())
        if path.startswith("/record/") and path.count("/") == 2:
            return self.json(route, self.record())
        if path == "/admin/account":
            return self.json(route, {"signed_in": True, "enabled": True})
        f = SRC / path.lstrip("/")
        if f.is_file() and SRC in f.resolve().parents:
            kind = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            return route.fulfill(status=200, body=f.read_bytes(), content_type=kind)
        return self.json(route, {})

    def json(self, route, obj):
        route.fulfill(status=200, body=json.dumps(obj, ensure_ascii=False), content_type="application/json")

    def page(self, route, name: str):
        html = (SRC / name).read_text(encoding="utf-8")
        route.fulfill(status=200, body=inject(html, self.lang, self.extra), content_type="text/html; charset=utf-8")


def union(boxes):
    x0 = min(b["x"] for b in boxes); y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["width"] for b in boxes); y1 = max(b["y"] + b["height"] for b in boxes)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


# A 780-pixel window: the page column is about 740, and the frame goes onto the landing page
# at its natural size (the images are shot at double density, so in CSS pixels they are half
# as big).
VIEW = {"sessions": (780, 700), "record": (780, 560)}


# Frames of the "Remembers" block on the landing page: two windows of the same width (640px,
# like the app's window), which overlap on the page. Sessions: without hover, with hover on
# the first row, with hover on the record button (the first five rows). Record: empty (the
# app's hint), request sent ("Reading the recording…"), the finished answer,
# and the answer after a click on the time (the conversation text opened at the right turn).
# The request is "typed" on the page: the field holds slices of the input line with the text
# typed up to the fifth, ninth... letter (`mem_t_N`), and they are laid over the empty window.
# Where things lie in the frames (fractions of the width and height) is written to `mem.json`:
# the page uses it to place the slices, the mouse and the highlight.
MEM_W = 640
MEM_NAMES = ["mem_s_idle", "mem_s_hover", "mem_s_press", "mem_r_0", "mem_r_2", "mem_r_3", "mem_r_4"] + [f"mem_t_{i}" for i in range(1, 7)]
MEM_TYPED = [1, 4, 7, 11, 17, 24]    # how many letters of the request are typed on each slice
MEM_S_MAX = 581    # the height of the sessions window in "Remembers" is at most this
MEM_CITE = 1    # time the mouse presses: the second (10:32, single sign-on); its turn is hidden, the list scrolls


def mem_record_height(browser, lang: str) -> int:
    """Height of the record window in which a finished answer fits without scrolling."""
    stand = Stand(lang)
    ctx = browser.new_context(viewport={"width": MEM_W, "height": 640}, device_scale_factor=1, locale="en-US")
    ctx.add_init_script(STUB)
    ctx.route(f"{ORIGIN}/**", stand.handle)
    page = ctx.new_page()
    page.goto(f"{ORIGIN}/chat/{stand.data['record']['id']}/record?desk=1")
    page.wait_for_selector("#turns .turn")
    page.wait_for_selector(".winbar")
    page.wait_for_timeout(600)
    height = 640
    for _ in range(4):
        over = page.evaluate("() => { const s = document.getElementById('scroller'); return s.scrollHeight - s.clientHeight; }")
        if over <= 0:
            break
        height += over
        page.set_viewport_size({"width": MEM_W, "height": height})
        page.wait_for_timeout(250)
    ctx.close()
    return height


def pct(box, w, h):
    return {"x": round(box["x"] / w * 100, 3), "y": round(box["y"] / h * 100, 3),
            "w": round(box["width"] / w * 100, 3), "h": round(box["height"] / h * 100, 3)}


def center(box, w, h):
    return [round((box["x"] + box["width"] / 2) / w * 100, 3), round((box["y"] + box["height"] / 2) / h * 100, 3)]


def shoot_mem(browser, lang: str, name: str):
    stand = Stand(lang)
    errors = []
    sessions = name.startswith("mem_s_")
    height = 840 if sessions else mem_record_height(browser, lang)
    ctx = browser.new_context(viewport={"width": MEM_W, "height": height}, device_scale_factor=2, locale="en-US")
    ctx.add_init_script(STUB)
    stand.mode = "final" if name in ("mem_r_3", "mem_r_4") else "empty"
    ctx.route(f"{ORIGIN}/**", stand.handle)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pos = {}
    if sessions:
        page.goto(f"{ORIGIN}/?desk=1")
        page.wait_for_selector(".chat")
        page.wait_for_selector(".winbar")
        page.wait_for_timeout(500)
        first = page.locator(".chat").first
        rec_btn = page.locator('.chat .iconbtn[data-act="record"]').first
        if name == "mem_s_hover":
            first.hover()
        elif name == "mem_s_press":
            first.hover()
            page.wait_for_timeout(200)
            rec_btn.hover()
        else:
            page.mouse.move(2, 2)
        page.wait_for_timeout(300)
        # How many rows will fit: the sessions window has the same height as before (581), its bottom
        # level with the "Remembers" stage. With one line per session more of them fit than with two
        # floors (requirement: one line per session, not two).
        every = [page.locator(".chat").nth(i).bounding_box() for i in range(page.locator(".chat").count())]
        rows = [r for r in every if r["y"] + r["height"] + 14 <= MEM_S_MAX] or every[:5]
        clip_h = rows[-1]["y"] + rows[-1]["height"] + 14
        if name == "mem_s_hover":
            rec = rec_btn.bounding_box()
            pos["s"] = {"w": MEM_W, "h": clip_h,
                        "row": [round((rows[0]["x"] + rows[0]["width"] * 0.18) / MEM_W * 100, 3), round((rows[0]["y"] + rows[0]["height"] / 2) / clip_h * 100, 3)],
                        "band": [round(rows[0]["y"] / clip_h * 100, 3), round((rows[0]["y"] + rows[0]["height"]) / clip_h * 100, 3)],   # where the first row is in height: the mouse "hovers" when it enters it
                        "record": center(rec, MEM_W, clip_h)}
        data = page.screenshot(clip={"x": 0, "y": 0, "width": MEM_W, "height": clip_h})
    else:
        page.goto(f"{ORIGIN}/chat/{stand.data['record']['id']}/record?desk=1")
        page.wait_for_selector("#turns .turn" if name in ("mem_r_3", "mem_r_4") else "#turns .hint")
        page.wait_for_selector(".winbar")
        page.wait_for_timeout(600)
        text = stand.data["record"]["results"][0]["request"]
        clip = {"x": 0, "y": 0, "width": MEM_W, "height": height}
        if name == "mem_r_0":
            form = page.locator("#askform").bounding_box()
            pad = 8
            box = {"x": max(0, form["x"] - pad), "y": form["y"] - pad, "width": min(MEM_W, form["width"] + 2 * pad), "height": form["height"] + 2 * pad}
            pos["r"] = {"w": MEM_W, "h": height, "composer": pct(box, MEM_W, height)}
        if name.startswith("mem_t_"):
            n = MEM_TYPED[int(name.split("_")[-1]) - 1]
            page.fill("#askinput", text[:n])
            page.wait_for_timeout(200)
            form = page.locator("#askform").bounding_box()
            pad = 8
            clip = {"x": max(0, form["x"] - pad), "y": form["y"] - pad, "width": min(MEM_W, form["width"] + 2 * pad), "height": form["height"] + 2 * pad}
        if name == "mem_r_2":
            page.fill("#askinput", text)
            page.wait_for_timeout(150)
            page.press("#askinput", "Enter")
            page.wait_for_selector(".ans.wait")
            page.wait_for_timeout(300)
        if name in ("mem_r_3", "mem_r_4"):
            page.evaluate("document.getElementById('scroller').scrollTop = 0")
            page.wait_for_timeout(200)
        if name == "mem_r_3":
            cite = page.locator(".cite").nth(MEM_CITE).bounding_box()
            pdf = page.locator("button.file").first.bounding_box()
            pos["r3"] = {"cite": center(cite, MEM_W, height), "pdf": pct(pdf, MEM_W, height)}
        if name == "mem_r_4":
            page.locator(".cite").nth(MEM_CITE).click()
            page.wait_for_selector("#sourcebody:not([hidden])")
            page.wait_for_timeout(700)
            page.mouse.move(MEM_W - 3, 330)      # an empty place at the right edge: no button is highlighted
            page.wait_for_timeout(200)
        data = page.screenshot(clip=clip)
    for held in stand.hold:                  # we let a hung request fail quietly, not with a traceback on close
        try:
            held.abort()
        except Exception:
            pass
    try:
        ctx.close()
    except Exception:
        pass
    img = Image.open(io.BytesIO(data))
    d = OUT / lang
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.png"
    img.save(path, optimize=True)
    if pos:
        f = d / "mem.json"
        cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        cur.update(pos)
        f.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    return img.size, path.stat().st_size, errors


def shoot(browser, lang: str, name: str):
    if name.startswith("mem_"):
        return shoot_mem(browser, lang, name)
    stand = Stand(lang)
    vp = VIEW[name]
    ctx = browser.new_context(viewport={"width": vp[0], "height": vp[1]}, device_scale_factor=2, locale="en-US")
    ctx.add_init_script(STUB)
    ctx.route(f"{ORIGIN}/**", stand.handle)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    if name == "sessions":
        page.goto(f"{ORIGIN}/?desk=1")
        page.wait_for_selector(".chat")
        page.wait_for_selector(".winbar")
        page.wait_for_timeout(500)
        first = page.locator(".chat").first
        first.hover()                       # on hover the row shows its buttons: record, settings, rename, delete
        page.wait_for_timeout(300)
        tools = page.locator(".top").first.bounding_box()
        rows = [page.locator(".chat").nth(i).bounding_box() for i in range(page.locator(".chat").count())]
        days = [h.bounding_box() for h in page.locator("h2.day").all()]
        box = union([tools] + rows + days)
        # from the very top of the window (the bar with the window buttons) to the bottom of the last row
        data = page.screenshot(clip={"x": 0, "y": 0, "width": vp[0], "height": box["y"] + box["height"] + 14})
    elif name == "record":
        page.goto(f"{ORIGIN}/chat/{stand.data['record']['id']}/record?desk=1")
        page.wait_for_selector("#turns .turn")
        page.wait_for_selector(".winbar")
        page.wait_for_timeout(600)
        # A window of exactly the height at which the whole feed fits without scrolling: otherwise the
        # page is scrolled to the bottom and the request slides under the pinned card.
        height = vp[1]
        for _ in range(4):
            over = page.evaluate("() => { const s = document.getElementById('scroller'); return s.scrollHeight - s.clientHeight; }")
            if over <= 0:
                break
            height += over
            page.set_viewport_size({"width": vp[0], "height": height})
            page.wait_for_timeout(250)
        page.evaluate("document.getElementById('scroller').scrollTop = 0")
        page.wait_for_timeout(200)
        data = page.screenshot(clip={"x": 0, "y": 0, "width": vp[0], "height": height})
    else:
        raise SystemExit("unknown frame: " + name)
    ctx.close()
    img = Image.open(io.BytesIO(data))
    d = OUT / lang
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.png"
    img.save(path, optimize=True)
    return img.size, path.stat().st_size, errors


NAMES = ["sessions", "record"] + MEM_NAMES


def main(argv):
    only = None
    if "--" in argv:
        i = argv.index("--")
        argv, only = argv[:i], argv[i + 1:]
    langs = argv or sorted(p.stem for p in DATA.glob("*.json") if not p.stem.endswith(".ui"))
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        for lang in langs:
            for name in NAMES:
                if only and name not in only:
                    continue
                size, nbytes, errors = shoot(browser, lang, name)
                print(lang, name, size, nbytes, "errors:" if errors else "", errors[:3] if errors else "")
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
