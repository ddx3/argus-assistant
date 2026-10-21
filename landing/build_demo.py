"""Builds the demo: the real Argus dashboard page (from src/), baked into one HTML file, with a
fake server (stub.js), the interface dictionaries and a conversation scenario. The app's
sources are only read.

The scenario and the texts of the fake screens come from scenarios/<language>.json; the
interface is translated by the app's engine (`ui/static/i18n/engine.js`) using the same
dictionaries as in the app itself.
"""
import base64
import json
import os
import re
import urllib.parse
from pathlib import Path

from strip_comments import strip_html

HERE = Path(__file__).parent
SRC = HERE.parents[0] / "src" / "argus" / "ui" / "static"

# The app window is baked into the page together with developer notes (about libraries,
# internals, someone's words). They have no place in the source of a published page: comments
# are stripped at build time (`strip_comments.py`). `ARGUS_LANDING_KEEP_COMMENTS=1` keeps
# them, to check that stripping does not change the look or the behavior.
STRIP = os.environ.get("ARGUS_LANDING_KEEP_COMMENTS") != "1"

LANGS = ["ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl"]

MONO = "Consolas,'Cascadia Mono',ui-monospace,monospace"
SANS = "'Segoe UI',system-ui,sans-serif"


def svg_uri(svg: str) -> str:
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg, safe="/:=;,-_.!~*'()")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------- screens

def screen_code(t: dict) -> str:
    """Screen frame: a review page with a failing test."""
    code = [
        "def has_duplicates(items):",
        "    for i in range(len(items)):",
        "        for j in range(len(items)):",
        "            if items[i] == items[j]:",
        "                return True",
        "    return False",
    ]
    lines = []
    y = 156
    for n, txt in enumerate(code, 1):
        lines.append(f'<text x="478" y="{y}" fill="#4a5568" font-size="19">{n}</text>')
        s = esc(txt)
        s = re.sub(r"\b(def|for|in|if|return)\b", r'<tspan fill="#c9a6e8">\1</tspan>', s)
        s = re.sub(r"\b(range|len)\b", r'<tspan fill="#7cc4e6">\1</tspan>', s)
        s = re.sub(r"\b(True|False)\b", r'<tspan fill="#e0b23e">\1</tspan>', s)
        lines.append(f'<text x="520" y="{y}" fill="#d7dce4" font-size="19" xml:space="preserve">{s}</text>')
        y += 34
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" font-family="{MONO}">
<rect width="1280" height="720" fill="#12151a"/>
<rect width="1280" height="48" fill="#171b21"/>
<circle cx="24" cy="24" r="6" fill="#f0716b"/><circle cx="46" cy="24" r="6" fill="#e0b23e"/><circle cx="68" cy="24" r="6" fill="#5fd68a"/>
<text x="640" y="30" fill="#9aa1ad" font-size="15" text-anchor="middle" font-family="{SANS}">{esc(t['title'])}</text>
<rect x="0" y="48" width="430" height="672" fill="#151920"/>
<line x1="430" y1="48" x2="430" y2="720" stroke="#252a33"/>
<text x="36" y="104" fill="#f2f4f7" font-size="24" font-weight="700" font-family="{SANS}">{esc(t['heading'])}</text>
<text x="36" y="144" fill="#9aa1ad" font-size="16" font-family="{SANS}">{esc(t['l1'])}</text>
<text x="36" y="168" fill="#9aa1ad" font-size="16" font-family="{SANS}">{esc(t['l2'])}</text>
<text x="36" y="192" fill="#9aa1ad" font-size="16" font-family="{SANS}">{esc(t['l3'])}</text>
<rect x="36" y="224" width="358" height="92" rx="8" fill="#0d1014" stroke="#252a33"/>
<text x="52" y="254" fill="#5b6270" font-size="14">{esc(t['input'])}</text><text x="52" y="278" fill="#d7dce4" font-size="16">[1, 2, 3]</text>
<text x="52" y="302" fill="#5b6270" font-size="14">{esc(t['expected'])}</text>
<rect x="450" y="72" width="800" height="360" rx="10" fill="#0d1014" stroke="#252a33"/>
<rect x="450" y="72" width="800" height="36" rx="10" fill="#171b21"/>
<text x="470" y="96" fill="#9aa1ad" font-size="14">solution.py</text>
{''.join(lines)}
<rect x="450" y="452" width="800" height="236" rx="10" fill="#0d1014" stroke="#252a33"/>
<text x="470" y="484" fill="#5b6270" font-size="14">{esc(t['tests'])}</text>
<text x="470" y="522" fill="#f0716b" font-size="18">✕ test_unique_list</text>
<text x="470" y="552" fill="#9aa1ad" font-size="16">  assert has_duplicates([1, 2, 3]) == False</text>
<text x="470" y="578" fill="#f0716b" font-size="16">  AssertionError: True != False</text>
<text x="470" y="622" fill="#5fd68a" font-size="18">✓ test_has_duplicate</text>
</svg>"""


def screen_math(t: dict) -> str:
    """Screen frame: a table with a deposit forecast."""
    cells = []
    y = 260
    last = len(t["rows"]) - 1
    for i, (k, v) in enumerate(t["rows"]):
        hot = i == last
        cells.append(f'<rect x="200" y="{y}" width="880" height="64" fill="{"#1c2128" if hot else "#151920"}" stroke="#252a33"/>')
        cells.append(f'<text x="232" y="{y+41}" fill="#9aa1ad" font-size="24">{esc(k)}</text>')
        cells.append(f'<text x="1048" y="{y+41}" fill="{"#e0b23e" if hot else "#f2f4f7"}" font-size="26" font-weight="700" text-anchor="end">{esc(v)}</text>')
        y += 64
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" font-family="{SANS}">
<rect width="1280" height="720" fill="#12151a"/>
<rect width="1280" height="48" fill="#171b21"/>
<circle cx="24" cy="24" r="6" fill="#f0716b"/><circle cx="46" cy="24" r="6" fill="#e0b23e"/><circle cx="68" cy="24" r="6" fill="#5fd68a"/>
<text x="640" y="30" fill="#9aa1ad" font-size="15" text-anchor="middle">{esc(t['title'])}</text>
<text x="200" y="130" fill="#5b6270" font-size="20" letter-spacing="3">{esc(t['slide'])}</text>
<text x="200" y="196" fill="#f2f4f7" font-size="46" font-weight="700">{esc(t['heading'])}</text>
{''.join(cells)}
</svg>"""


def screen_idle(t: dict) -> str:
    items = "".join(
        f'<text x="170" y="{300 + 44 * i}" fill="#9aa1ad" font-size="22">{esc(s)}</text>' for i, s in enumerate(t["items"])
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" font-family="{SANS}">
<rect width="1280" height="720" fill="#12151a"/>
<rect width="1280" height="48" fill="#171b21"/>
<circle cx="24" cy="24" r="6" fill="#f0716b"/><circle cx="46" cy="24" r="6" fill="#e0b23e"/><circle cx="68" cy="24" r="6" fill="#5fd68a"/>
<text x="640" y="30" fill="#9aa1ad" font-size="15" text-anchor="middle">{esc(t['title'])}</text>
<rect x="120" y="150" width="1040" height="420" rx="14" fill="#151920" stroke="#252a33"/>
<text x="170" y="236" fill="#f2f4f7" font-size="38" font-weight="700">{esc(t['heading'])}</text>
{items}
</svg>"""


# ----------------------------------- picker windows ("What to listen to")

def _mix(fg: str, bg: str, a: float) -> tuple:
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return tuple(round(f[i] * a + b[i] * (1 - a)) for i in range(3))


def _thumb(kind: str) -> str:
    """Thumbnail of a made-up window for the real window picker: JPEG in base64, as the app returns
    it.
    """
    import io
    from PIL import Image, ImageDraw
    W, H = 320, 180
    im = Image.new("RGB", (W, H), "#0d1015")
    d = ImageDraw.Draw(im)
    if kind == "call":
        for x, y, c in ((6, 6, "#5fd68a"), (164, 6, "#c77fd8"), (6, 94, "#d8b54f"), (164, 94, "#4fb8d8")):
            d.rounded_rectangle([x, y, x + 150, y + 80], 8, fill="#151a21")
            d.ellipse([x + 52, y + 16, x + 98, y + 62], fill=_mix(c, "#151a21", .65))
    elif kind == "lecture":
        d.rectangle([0, 0, W, H], fill="#12151a")
        d.rounded_rectangle([20, 22, 190, 44], 5, fill="#e4e7ec")
        for i, w in enumerate((150, 120, 136)):
            d.rounded_rectangle([20, 66 + i * 20, 20 + w, 72 + i * 20], 3, fill="#3a414d")
        for x, h, a in ((216, 70, .7), (244, 44, .5), (272, 96, .9)):
            d.rectangle([x, 150 - h, x + 20, 150], fill=_mix("#4fb8d8", "#12151a", a))
    elif kind == "music":
        d.rectangle([0, 0, W, H], fill="#0f1217")
        d.ellipse([34, 34, 134, 134], fill="#1b2029")
        d.ellipse([70, 70, 98, 98], fill=_mix("#c77fd8", "#1b2029", .6))
        d.rounded_rectangle([170, 62, 290, 72], 5, fill="#3a414d")
        d.rounded_rectangle([170, 86, 250, 94], 4, fill="#2a303a")
        d.rounded_rectangle([170, 116, 290, 120], 2, fill="#2a303a")
    else:   # notes
        d.rectangle([0, 0, W, H], fill="#12151a")
        d.rounded_rectangle([28, 24, 130, 40], 4, fill="#e4e7ec")
        for i, w in enumerate((250, 220, 236, 140)):
            d.rounded_rectangle([28, 62 + i * 22, 28 + w, 68 + i * 22], 3, fill="#3a414d")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def fake_windows(sc: dict) -> list:
    """What the app returns for `/windows?thumbs=true`: windows to choose from, with thumbnails. The
    titles come from the language's scenario.
    """
    wins = sc.get("windows") or load_scenario("en")["windows"]
    return [{"title": w["title"], "label": w["label"], "width": w["w"], "height": w["h"], "thumb": _thumb(w["kind"])}
            for w in wins]


# ----------------------------------------------------------- scenario

BASE = 247  # on the session clock of the timeline the conversation starts at 04:07
LENGTH = 62

# When things happen (seconds of the loop); the texts come from scenarios/<language>.json.
# The answer arrives whole after "thinking": that is how the build works through the
# relay.
SPEECH_AT = [  # (at, dur, source)
    (5.0, 3.4, "them"), (19.0, 5.6, "me"), (25.0, 3.2, "them"), (38.0, 3.0, "me"), (42.0, 3.0, "them"),
]
SHOTS = [(1, 25.5, 142), (2, 42.6, 96)]  # (id, when, KB)
QA = [  # (speech index, askAt, firstAt, latency, reason, shot, deep)
    (0, 5.9, 9.3, 3400, "they asked a question", None, False),
    (2, 25.9, 29.8, 3900, "they asked a question", 1, False),
    (4, 43.0, 48.8, 5800, "asked manually", 2, True),
]


def load_scenario(lang: str) -> dict:
    return json.loads((HERE / "scenarios" / f"{lang}.json").read_text(encoding="utf-8"))


def stub_scenario(sc: dict) -> dict:
    speech = [
        {"at": at, "dur": dur, "source": src, "conf": conf, "text": sc["speech"][i],
         "clock": BASE + at - dur}
        for i, ((at, dur, src), conf) in enumerate(zip(SPEECH_AT, (0.96, 0.93, 0.95, 0.94, 0.95)))
    ]
    shots = [{"id": i, "at": at, "showAt": at, "kb": kb, "clock": BASE + at} for i, at, kb in SHOTS]
    qa = [
        # typeFor 0: the answer lands in the timeline whole, as through the relay. "Typing" over 0.3 s
        # showed raw LaTeX cut off mid-word (`$$S = 10\,000 \cd`) and truncated markup.
        {"askAt": ask, "firstAt": first, "typeFor": 0, "latency": lat, "reason": reason,
         "cause": sc["speech"][si], "text": sc["answers"][n], **({"shot": shot} if shot else {}),
         **({"deep": True} if deep else {})}
        for n, (si, ask, first, lat, reason, shot, deep) in enumerate(QA)
    ]
    return {"length": LENGTH, "speech": speech, "shots": shots, "qa": qa}


def stub_screens(sc: dict) -> dict:
    return {
        "idle": svg_uri(screen_idle(sc["idle"])),
        "1": svg_uri(screen_code(sc["code"])),
        "2": svg_uri(screen_math(sc["math"])),
    }


def lang_data(lang: str) -> dict:
    """Everything the demo takes from a language: the scenario for the stub and the screen images."""
    sc = load_scenario(lang)
    return {"scenario": stub_scenario(sc), "screens": stub_screens(sc), "call": sc["call"]}


# ----------------------------------------------------------- build

def inline_script(src: str) -> str:
    return src.replace("</script", "<\\/script")


# The models and services under the hood are kept secret. The app dictionary has settings
# strings that name them; the demo does not show them, but the page source would, so they are
# not baked in.
HIDDEN = re.compile(r"whisper|elevenlabs|openai|anthropic|claude|gemini", re.IGNORECASE)


def dictionaries(langs=None) -> dict:
    out = {}
    for lang in (langs or LANGS):
        p = SRC / "i18n" / f"{lang}.json"
        if lang != "en" and p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            out[lang] = {k: v for k, v in data.items() if not (HIDDEN.search(k) or HIDDEN.search(str(v)))}
    return out


def demo_winbar(src: str) -> str:
    """`winbar.js` for the demo: the same file as in the app, but it always draws the strip.

    In the app the strip exists only in the main window (the `desk=1` mark) and not in an iframe,
    while the demo on the landing page shows exactly the main window and sits in an iframe. Two
    things are patched: the "I am not in an iframe" check is removed, and the "main window" mark
    is set by us. The script still skips the answers window (`data-solo`): it has no strip in the
    app either.
    """
    out = []
    for line in src.split("\n"):
        if "window.top !== window" in line:
            continue
        if 'sessionStorage.getItem("argus-desk")' in line or "desk = /[?&]desk=1" in line:
            line = "    desk = true;"
        # The window buttons (minimize, windowed mode, close) are not on the landing page, by
        # requirement. The rest of the strip is as on PC.
        if ".winbar-btns { display:flex; flex:none; }" in line:
            line = "  .winbar-btns { display:none; }"
        out.append(line)
    patched = "\n".join(out)
    if "desk = true;" not in patched or "window.top !== window" in patched or ".winbar-btns { display:none; }" not in patched:
        raise SystemExit("winbar.js changed: update demo_winbar in landing/build_demo.py")
    return patched


def build(variant="dashboard", freeze=None, glass=False, start=None, lang=None, langs=None, picker=None):
    """The demo page as one file.

    `lang`: bake in the language and the scenario at once (for screenshots). Without it the page
    waits for `window.ARGUS_LANG`, `__ARGUS_SCENARIO__` and `__ARGUS_SCREENS__` from the host
    (that is how the landing page launches it, `srcdoc` in template.html).

    `picker`: capture the picker window: `{"what": "audio", "picked": [numbers of windows from
    the scenario]}`. The page opens the real "What to listen to" window itself,
    with an invented window list, and marks the given ones (needs `lang`).
    """
    html = (SRC / "index.html").read_text(encoding="utf-8")
    chrome_css = (SRC / "chrome.css").read_text(encoding="utf-8")
    chrome_js = (SRC / "chrome.js").read_text(encoding="utf-8")
    account_js = (SRC / "account.js").read_text(encoding="utf-8")
    temml_css = (SRC / "vendor/temml/Temml-Local.min.css").read_text(encoding="utf-8")
    temml_js = (SRC / "vendor/temml/temml.min.js").read_text(encoding="utf-8")
    engine = (SRC / "i18n" / "engine.js").read_text(encoding="utf-8")
    woff = base64.b64encode((SRC / "vendor/temml/Temml.woff2").read_bytes()).decode()
    temml_css = temml_css.replace("url('Temml.woff2')", f"url('data:font/woff2;base64,{woff}')")

    stub = (HERE / "stub.js").read_text(encoding="utf-8")
    baked = ""
    if lang is not None:
        data = lang_data(lang)
        baked = (f"window.ARGUS_LANG={json.dumps(lang)};"
                 f"window.__ARGUS_SCREENS__={json.dumps(data['screens'])};"
                 f"window.__ARGUS_SCENARIO__={json.dumps(data['scenario'], ensure_ascii=False)};")
        if picker is not None:
            wins = fake_windows(load_scenario(lang))
            baked += (f"window.__ARGUS_WINDOWS__={json.dumps(wins, ensure_ascii=False)};"
                      f"window.__ARGUS_PICKER__={json.dumps({'what': picker.get('what', 'audio'), 'picked': [wins[i]['title'] for i in picker.get('picked', [])]}, ensure_ascii=False)};")
    pre = (
        # In the demo there is nothing to blur behind the strip and the header (the canvas is flat),
        # and background blur complicates the browser's layers: without it the look is the same.
        # The border of the "thinking" card in the demo is gray like the others (in the app it
        # is blue). The window in the page is scaled by a fractional factor, and when the card was
        # replaced by the answer, a blue stripe of the old border stayed at the left edge. The word
        # "thinking" stays blue, which is enough to spot the card.
        "<style>:root{color-scheme:dark} .winbar,header{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}"
        " .turn.wait{border-color:var(--slab-edge,rgba(255,255,255,.09))!important}</style>"
        "<script>" + baked
        + (f"window.__ARGUS_FREEZE__={freeze};" if freeze is not None else "")
        + (f"window.__ARGUS_START__={start};" if start is not None else "")
        + "window.ARGUS_I18N_ALL=" + json.dumps(dictionaries(langs), ensure_ascii=False).replace("</", "<\\/") + ";"
        + "</script><script>" + inline_script(stub) + "</script>"
        + "<script>" + inline_script(engine) + "</script>"
    )

    html = html.replace('<link rel="stylesheet" href="/chrome.css">', "<style>" + chrome_css + "</style>")
    html = html.replace('<link rel="stylesheet" href="/vendor/temml/Temml-Local.min.css">',
                        "<style>" + temml_css + "</style>")
    html = html.replace('<script src="/chrome.js" defer></script>', "")
    html = html.replace('<script src="/account.js"></script>', "<script>" + inline_script(account_js) + "</script>")
    html = html.replace('<script src="/vendor/temml/temml.min.js" defer></script>', "")
    # the main window strip: in the app `winbar.js` draws it, and the PC look of the window is
    # exactly with it (the page header moves into the strip, the window buttons are on the right).
    # In a browser and in the demo it does not draw itself, so we inline it with a patch
    # (`demo_winbar`)
    html = html.replace('<script src="/winbar.js" defer></script>', "")
    winbar_js = demo_winbar((SRC / "winbar.js").read_text(encoding="utf-8"))
    # chrome.js, temml and the strip go to the end, like defer: after the markup is parsed and the
    # main script has run, in the same order as in the app
    html = html.replace("</body>", "<script>" + inline_script(temml_js) + "</script>"
                        "<script>" + inline_script(chrome_js) + "</script>"
                        "<script>" + inline_script(winbar_js) + "</script></body>")
    # the stub and the translation engine come first, before any page code
    html = re.sub(r"<head>", lambda m: "<head>" + pre, html, count=1) if "<head>" in html else pre + html

    # In the app the session work screen is always opened at /chat/<id>: the "Work with the
    # recording" arrow in the window's top strip depends on it (the id is taken from
    # the page path). There is no path here, so we substitute an id; the other places where the
    # page reads the path are left alone: the landing page uses them to turn this same page into
    # the answers window (`page.js`, `srcdoc`).
    html = "\n".join('  const chatPart = "demo";' if "const chatPart = (location.pathname.match(" in ln else ln
                     for ln in html.split("\n"))
    if variant == "solo":
        # the window that shows answers and screen together: the same address /panel/live that the app
        # opens
        html = html.replace("location.pathname.match(", "'/panel/live'.match(")
        if glass:
            html = html.replace("<head>", '<head><script>document.documentElement.dataset.glass="1"</script>', 1)
    if not STRIP:
        return html
    # The window library name in the page code (`window.pywebview`, the `pywebview-drag-region`
    # class) is of no use to the demo: there is no window at all here. We replace it everywhere in
    # the same way, so the "is there an app window" checks work as before.
    return strip_html(html).replace("pywebview", "argushost")


# ------------------------- the "Record" page for the "Remembers" block

def record_lang_data(lang: str):
    """Made-up data of the "Record" page in a language (the same as in the `screens.py`
    frames): server response, session list, dictionary. If the language has no data: None, and the
    landing page takes the English one."""
    if not (HERE / "scenarios" / "screens" / f"{lang}.json").exists():
        return None
    from screens import Stand, dictionary    # frames and the demo take their recordings from one place, so they look identical
    stand = Stand(lang)
    stand.mode = "final"
    words = {k: v for k, v in dictionary(lang, stand.extra).items() if not (HIDDEN.search(k) or HIDDEN.search(str(v)))}
    return {"lang": lang, "dict": words, "record": stand.record(), "chats": stand.chats()}


def build_record() -> str:
    """The real "Record" page as one file, without data: the language and the recordings
    are put in by the landing page at launch (`page.js`, `srcdoc`).

    The "Remembers" block needs it for the finale: a click on the time in an answer
    opens a window with the conversation text, and the page does this with its own code and its
    own animation. It is built like the dashboard demo (`build`): styles and scripts are inlined,
    the server is fake (`stub_record.js`), and the window strip is drawn as on PC
    (`demo_winbar`).
    """
    html = (SRC / "record.html").read_text(encoding="utf-8")
    parts = {
        "chrome_css": (SRC / "chrome.css").read_text(encoding="utf-8"),
        "chrome_js": (SRC / "chrome.js").read_text(encoding="utf-8"),
        "account_js": (SRC / "account.js").read_text(encoding="utf-8"),
        "engine": (SRC / "i18n" / "engine.js").read_text(encoding="utf-8"),
        "winbar_js": demo_winbar((SRC / "winbar.js").read_text(encoding="utf-8")),
        "stub": (HERE / "stub_record.js").read_text(encoding="utf-8"),
    }

    def swap(old: str, new: str) -> None:
        nonlocal html
        if old not in html:
            raise SystemExit(f"record.html changed: {old!r} not found in demo_record, update landing/build_demo.py")
        html = html.replace(old, new)

    swap('<link rel="stylesheet" href="/chrome.css">', "<style>" + parts["chrome_css"] + "</style>")
    swap('<script src="/chrome.js" defer></script>', "")
    swap('<script src="/winbar.js" defer></script>', "")
    swap('<script src="/account.js"></script>', "<script>" + inline_script(parts["account_js"]) + "</script>")
    # chrome.js and the window bar go at the end of the page, like `defer`: after the markup and
    # the main script are parsed, in the same order as in the app
    swap("</body>", "<script>" + inline_script(parts["chrome_js"]) + "</script><script>" + inline_script(parts["winbar_js"]) + "</script></body>")
    # the address is /chat/<id>/record: id is the second path segment; a page in srcdoc has no path
    swap('const id = location.pathname.split("/")[2];', 'const id = "demo";')
    pre = ("<style>:root{color-scheme:dark} .winbar,header{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}</style>"
           "<script>" + inline_script(parts["stub"]) + "</script><script>" + inline_script(parts["engine"]) + "</script>")
    swap("<head>", "<head>" + pre)
    if not STRIP:
        return html
    return strip_html(html).replace("pywebview", "argushost")
