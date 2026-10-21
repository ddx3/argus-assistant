"""Builds the landing page: the template + the real app interface (the demo) + the texts and
a sample conversation in the enabled languages.

    python build_landing.py        -> out/landing.html and out/img/<lang>/... (for publishing)
    python build_landing.py --all  -> the same for all nine languages (checking, and enabling later)
    python build_landing.py --out FOLDER  -> build into another folder instead of out/ (so a
                                             server deploy does not interfere with another build)

The landing page lives on our own server, not as a third-party hosted artifact:
`deploy/landing/publish_landing.py` publishes it.

All nine languages are enabled (`ENABLED`); the language picker is in the header (its markup
in `lang_picker.html` is baked into the template). A language without its own frames stops
the build (missing frame): the frames are taken by `shots.py`, `screens.py` and
`voice_shots.py`, and the texts are in `copy/<lang>.json`.
"""
import json
import re
import shutil
from pathlib import Path

import build_demo
from strip_comments import strip_html

HERE = Path(__file__).parent
SRC = HERE.parents[0] / "src" / "argus" / "ui" / "static" / "i18n"
LANGS = build_demo.LANGS
ENABLED = list(LANGS)

# Links to the terms of use and the privacy policy (the caption under the button, the
# "Legal" column in the footer, and a suggestion in the answer about data).
# They sit between the `<!--legal-->…<!--/legal-->` markers in the template and in
# copy/<lang>.json. While the documents are not published (the download page returns 404) and
# `legal/operator.json` is empty, they contain yellow placeholders instead of the operator's
# details, so the page is built without these places. Once the documents live at the addresses
# from the template, set True and rebuild.
LEGAL = True

LEGAL_BLOCK = re.compile(r"<!--legal-->(.*?)<!--/legal-->", re.DOTALL)
# The line "By downloading Argus, you accept the Terms of Use and the Privacy Policy" under the
# download buttons: off for now (not requested; only the
# pages were requested).
FINE = False
FINE_BLOCK = re.compile(r"<!--fine-->(.*?)<!--/fine-->", re.DOTALL)
# The "Seller information" link in the footer: it is there only while the
# seller's personal file (`subpages.SELLER_FILE`) is on the build machine.
SELLER_BLOCK = re.compile(r"<!--seller-->(.*?)<!--/seller-->", re.DOTALL)
# The questions and answers live on the "Help" page (subpages.py takes them from here); they
# are removed from the main page.
FAQ_BLOCK = re.compile(r"<!--faq-->(.*?)<!--/faq-->", re.DOTALL)
SUB_INPUT = None   # (markup of the main page after cleanup, all texts) — for the side pages (subpages.py)


def legal(text: str, on: bool = LEGAL) -> str:
    return LEGAL_BLOCK.sub(lambda m: m.group(1) if on else "", text)


def js_string(html: str) -> str:
    s = json.dumps(html, ensure_ascii=False)
    # Inside <script> we must not leave "<!--", "</" or "<script": HTML parsing would break. We
    # hide the other document tags too, so the string is not mistaken for the page's own markup.
    # We write the second letter with code: a plain backslash does not work here, because `\b` in
    # JS is the backspace character, not the "b" of "body".
    bs = chr(92)
    for tag in ("<!", "</", "<script", "<html", "<head", "<body"):
        s = s.replace(tag, "<" + bs + "u%04x" % ord(tag[1]) + tag[2:])
    return s


def dumps(o) -> str:
    return js_string(o) if isinstance(o, str) else json.dumps(o, ensure_ascii=False).replace("</", "<\\/")


def extract(t: str) -> dict:
    """The source text from the marked-up markup: {id: innerHTML | attribute value}."""
    out = {}
    for m in re.finditer(r"<([a-z0-9]+)\b[^<>]*?\sdata-i18n=\"([^\"]+)\"[^<>]*>", t):
        tag, id_ = m.group(1), m.group(2)
        start = m.end()
        depth = 1
        for mm in re.finditer(rf"<(/?){tag}\b[^<>]*>", t[start:]):
            depth += -1 if mm.group(1) else 1
            if depth == 0:
                out[id_] = t[start:start + mm.start()]
                break
    for m in re.finditer(r"<[a-z0-9]+\b[^<>]*?data-i18n-attr=\"([a-z-]+):([^\"]+)\"[^<>]*>", t):
        attr, id_ = m.group(1), m.group(2)
        v = re.search(rf'\s{re.escape(attr)}="([^"]*)"', m.group(0))
        if v:
            out[id_] = v.group(1)
    return dict(sorted(out.items()))


def load_copy(langs) -> dict:
    out = {}
    for lang in langs:
        p = HERE / "copy" / f"{lang}.json"
        if p.exists():
            out[lang] = {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    return out


def build(langs=None) -> str:
    langs = list(langs or ENABLED)
    t = (HERE / "template.html").read_text(encoding="utf-8")
    # The English text in the markup is the source: copy/en.json is rewritten from it. The other
    # languages (Russian included) are translations kept in copy/<lang>.json as they are.
    copy_en = extract(t)
    import price_copy
    copy_en.update(price_copy.EN)   # texts of the price page: the English source is in price_copy.py
    (HERE / "copy").mkdir(exist_ok=True)
    (HERE / "copy" / "en.json").write_text(json.dumps(copy_en, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    copy = {lang: {k: legal(v) for k, v in d.items()} for lang, d in load_copy(langs).items()}
    t = legal(t).replace("{{FOOT_COLS}}", "3" if LEGAL else "2")
    t = FINE_BLOCK.sub(lambda m: m.group(1) if FINE else "", t)
    import subpages
    t = SELLER_BLOCK.sub(lambda m: m.group(1) if subpages.seller_data() else "", t)
    global SUB_INPUT
    SUB_INPUT = (t, dict(copy))
    t = FAQ_BLOCK.sub("", t)
    # Only the strings that have a marker in the (already cleaned) markup go into the page:
    # otherwise, for disabled places such as the links to the documents, the text with the
    # addresses would stay in the page source.
    ids = set(re.findall(r'data-i18n="([^"]+)"', t))
    for m in re.finditer(r'data-i18n-attr="([^"]+)"', t):
        ids.update(pair.split(":")[1] for pair in m.group(1).split(";"))
    copy = {lang: {k: v for k, v in d.items() if k in ids} for lang, d in copy.items()}

    page_js = (HERE / "page.js").read_text(encoding="utf-8")
    page_js = page_js.replace("{{DEMO_SRC}}", js_string(build_demo.build(langs=langs)))
    page_js = page_js.replace("{{LANGS}}", dumps(langs))
    # the real "Record" page for the finale of the "Remembers" block and its made-up data per
    # language (if a language has no data, the landing uses the English data)
    page_js = page_js.replace("{{RECDEMO}}", js_string(build_demo.build_record()))
    page_js = page_js.replace("{{RECDATA}}", dumps({l: d for l in langs if (d := build_demo.record_lang_data(l))}))
    # where the row and the record button are in the sessions window: written by screens.py
    # together with the frames; the window is the same in all languages, so we take the first one
    mem = HERE / "img" / langs[0] / "mem.json"
    page_js = page_js.replace("{{MEMPOS}}", mem.read_text(encoding="utf-8") if mem.exists() else "null")
    page_js = page_js.replace("{{COPY}}", dumps(copy))
    page_js = page_js.replace("{{LANGDATA}}", dumps({l: build_demo.lang_data(l) for l in langs}))
    # JS sits inside <script>: lines with "</" are already escaped above
    html = re.sub(r"<script>\s*\{\{PAGE_JS\}\}\s*</script>", lambda m: "<script>\n" + page_js + "\n</script>", t)
    # developer notes are not needed in the page source (see `build_demo.STRIP`)
    return strip_html(html) if build_demo.STRIP else html


def main(argv):
    langs = LANGS if "--all" in argv else ENABLED
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else HERE / "out"
    out.mkdir(parents=True, exist_ok=True)
    html = build(langs)
    (out / "landing.html").write_text(html, encoding="utf-8")
    (out / "index.html").write_text(html, encoding="utf-8")   # this is what the main page is called in the published artifact: for checking the links between pages
    import subpages
    sub = subpages.build_all(SUB_INPUT[0], SUB_INPUT[1], langs, dumps, out)
    miss = sub.pop("_missing")
    print("pages:", ", ".join(f"{k} {v} bytes" for k, v in sub.items()))
    if miss:
        print("NOT FILLED IN in legal/operator.json (the terms and policy carry a yellow marker):", ", ".join(miss))
    img_out = out / "img"
    if img_out.exists():
        shutil.rmtree(img_out)
    used = sorted(set(re.findall(r'data-img="([a-z0-9_]+)"', (HERE / "template.html").read_text(encoding="utf-8"))))
    for lang in langs:
        (img_out / lang).mkdir(parents=True, exist_ok=True)
        for name in used:
            found = [f for f in (HERE / "img" / lang).glob(name + ".*")]
            if not found:
                raise SystemExit(f"no frame {lang}/{name}: python shots.py {lang} / python screens.py {lang}")
            shutil.copy2(found[0], img_out / lang / found[0].name)
    n = sum(1 for _ in img_out.rglob("*") if _.is_file())
    print(out / "landing.html", len(html), "bytes;", n, "images; languages:", ", ".join(langs))
    print("This is a build. To get the page onto the site: python deploy/landing/publish_landing.py (we no longer update the claude.ai artifact).")


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
