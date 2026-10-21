"""Download buttons on the landing page: the "what the system needs" labels and the iPad
branch.

`/download` picks a file by the browser string (`deploy/landing/nginx/default.conf`), but
an iPad (iPadOS 13+) calls itself desktop Safari, and every Mac in Safari is "Intel Mac
OS X", so nginx can tell apart neither an iPad nor an Intel Mac: an iPad would get the
Apple Silicon image (audit of 2026-10-14, release-ops-16). The page tells them apart
(`navigator.maxTouchPoints`), and so do the labels on the buttons ("Windows 10 2004 or
newer", "Apple Silicon, macOS 14 or newer"), which exist in nine languages.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
LANGUAGES = ["ru", "en", "uk", "es", "pt", "de", "fr", "tr", "pl"]
CAPTIONS = ("foot.reqwin", "foot.reqmac")
NODE = shutil.which("node")


def _template() -> str:
    return (LANDING / "template.html").read_text(encoding="utf-8")


def test_each_download_link_in_the_footer_has_its_requirement_under_it():
    page = _template()
    assert re.search(r'<a href="/download/windows"[^>]*>[^<]*</a><small data-i18n="foot.reqwin">Windows 10 2004', page)
    assert re.search(r'<a href="/download/mac"[^>]*>[^<]*</a><small data-i18n="foot.reqmac">Apple Silicon, macOS 14', page)


def test_the_source_copy_is_what_the_template_gives():
    # `copy/en.json` is built by `build_landing.py` from the template; if it is edited by hand,
    # the two must not diverge.
    sys.path.insert(0, str(LANDING))
    from build_landing import extract

    en = json.loads((LANDING / "copy" / "en.json").read_text(encoding="utf-8"))
    given = extract(_template())
    for key in CAPTIONS:
        assert en[key] == given[key], key


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_captions_are_translated_into_every_language(lang):
    copy = json.loads((LANDING / "copy" / f"{lang}.json").read_text(encoding="utf-8"))
    for key in CAPTIONS:
        assert copy[key].strip(), (lang, key)
    assert "Windows 10" in copy["foot.reqwin"] and "2004" in copy["foot.reqwin"]
    assert "Apple Silicon" in copy["foot.reqmac"] and "macOS 14" in copy["foot.reqmac"]
    if lang not in ("ru", "uk"):
        # Language data: only the Russian and Ukrainian texts are written in Cyrillic.
        assert not re.search("[А-Яа-яЁё]", copy["foot.reqwin"] + copy["foot.reqmac"]), lang
    if lang != "en":
        en = json.loads((LANDING / "copy" / "en.json").read_text(encoding="utf-8"))
        assert copy["foot.reqwin"] != en["foot.reqwin"] and copy["foot.reqmac"] != en["foot.reqmac"], lang


def _ipad_script() -> str:
    js = (LANDING / "page.js").read_text(encoding="utf-8")
    start = js.index("// ----------------------- iPad")
    end = js.index("// -----------------------", start + 10)
    return js[start:end]


def _hrefs_after(platform: str, touch: int, user_agent: str = "") -> dict[str, str]:
    harness = r"""
const hrefs = ["/download", "/download/mac", "/download/windows", "terms.html"];
const links = hrefs.map((h) => ({ h, setAttribute(k, v) { this.h = v; } }));
// In Node 21+ `navigator` is a getter property of the global object: assigning to it
// silently has no effect.
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
  platform: process.env.PLATFORM, maxTouchPoints: Number(process.env.TOUCH), userAgent: process.env.UA } });
globalThis.document = {
  querySelectorAll(sel) {
    const want = sel.match(/href="([^"]+)"/g).map((s) => s.slice(6, -1));
    return links.filter((l) => want.includes(l.h));
  },
};
new Function(process.env.SCRIPT)();
console.log(JSON.stringify(Object.fromEntries(links.map((l, i) => [hrefs[i], l.h]))));
"""
    env = {**os.environ, "PLATFORM": platform, "TOUCH": str(touch), "UA": user_agent, "SCRIPT": _ipad_script()}
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, env=env, timeout=60, check=True)
    return json.loads(out.stdout)


UNCHANGED = {"/download": "/download", "/download/mac": "/download/mac",
             "/download/windows": "/download/windows", "terms.html": "terms.html"}


@pytest.mark.skipif(NODE is None, reason="node is required to run the page script")
def test_an_ipad_is_sent_to_the_computer_only_note_not_to_the_mac_image():
    # iPadOS 13+: "MacIntel" and five touches. The "Download" button and the Mac link
    # lead to an explanation; Windows and the rest are as they were.
    assert _hrefs_after("MacIntel", 5) == {
        "/download": "/download/other", "/download/mac": "/download/other",
        "/download/windows": "/download/windows", "terms.html": "terms.html"}


@pytest.mark.skipif(NODE is None, reason="node is required to run the page script")
@pytest.mark.parametrize(("platform", "touch"), [
    ("MacIntel", 0),      # a real Mac: no touch screen, a trackpad does not count as touches
    ("Win32", 10),        # a Windows laptop with a touch screen
    ("iPhone", 5),        # a phone: nginx has already sent it to "other"
    ("Linux x86_64", 0),
])
def test_everything_else_keeps_its_links(platform, touch):
    assert _hrefs_after(platform, touch) == UNCHANGED
