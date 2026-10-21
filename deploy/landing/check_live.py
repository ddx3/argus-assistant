"""Check of the deployed landing page in a real browser (headless Chromium): nine languages,
desktop and phone, all pages, the explanations for the "Download" button, the Mac
question in "Help", and clicks on the buttons (Mac gets the file, Windows and phone
get an explanation).

    python deploy/landing/check_live.py http://203.0.113.10 <screenshot folder>

Needs playwright (an environment outside the repository, see landing/README.md). Takes about
2.5 minutes; the file is not downloaded to the end on clicks (it is cancelled).
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
OUT = Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)
LANGS = ["ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl"]
WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
bad = 0


def note(ok, msg):
    global bad
    if not ok:
        bad += 1
    print(("ok   " if ok else "FAIL ") + msg)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        for lang in LANGS:
            for label, vp in (("desk", {"width": 1360, "height": 900}), ("phone", {"width": 390, "height": 844})):
                ctx = browser.new_context(user_agent=WIN_UA, viewport=vp)
                ctx.add_init_script(f"try{{localStorage.setItem('argus.landing.lang','{lang}')}}catch(e){{}}")
                page = ctx.new_page()
                log = []
                page.on("pageerror", lambda e: log.append("pageerror " + str(e)[:200]))
                page.on("console", lambda m: log.append("console." + m.type + " " + m.text[:160]) if m.type == "error" else None)
                page.on("requestfailed", lambda r: log.append("failed " + r.url[-80:]))
                page.on("response", lambda r: log.append(f"http{r.status} " + r.url[-80:]) if r.status >= 400 else None)
                # main page + the "soon" explanation
                page.goto(f"{BASE}/?dl=soon", wait_until="load")
                page.wait_for_timeout(1800)
                toast = page.inner_text("#dl-note").strip() if page.is_visible("#dl-note") else ""
                fits = page.evaluate("(()=>{const b=document.getElementById('dl-note').getBoundingClientRect();return b.left>=0&&b.right<=innerWidth})()")
                over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                note(bool(toast) and fits and over <= 0 and page.evaluate("document.documentElement.lang") == lang,
                     f"{lang}/{label} main page: toast={toast[:60]!r} fits={fits} overflow={over} lang={page.evaluate('document.documentElement.lang')}")
                if label == "phone":
                    page.screenshot(path=str(OUT / f"{lang}_phone_toast.png"))
                page.goto(f"{BASE}/?dl=other", wait_until="load")
                page.wait_for_timeout(900)
                t2 = page.inner_text("#dl-note").strip() if page.is_visible("#dl-note") else ""
                note(bool(t2) and t2 != toast, f"{lang}/{label} dl=other: {t2[:70]!r}")
                # help page: the Mac question
                page.goto(f"{BASE}/help.html", wait_until="load")
                page.wait_for_timeout(900)
                q = page.query_selector('[data-i18n="faq.13.q"]')
                code_ok = False
                if q:
                    q.click()
                    page.wait_for_timeout(250)
                    code_ok = page.evaluate("(()=>{const c=document.querySelector('[data-i18n=\"faq.13.a\"] code');return !!c&&c.textContent.trim().startsWith('xattr -dr com.apple.quarantine')&&c.getBoundingClientRect().right<=innerWidth})()")
                over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                note(bool(q) and code_ok and over <= 0, f"{lang}/{label} help: faq.13={bool(q)} code_fits={code_ok} overflow={over}")
                if label == "desk" and lang in ("de", "ru"):
                    page.screenshot(path=str(OUT / f"{lang}_help_faq13.png"))
                # the remaining pages
                for path in ("/pricing.html", "/terms.html", "/privacy.html"):
                    page.goto(BASE + path, wait_until="load")
                    page.wait_for_timeout(400)
                    ov = page.evaluate("document.documentElement.scrollWidth - innerWidth")
                    note(ov <= 0, f"{lang}/{label} {path}: overflow={ov}")
                errs = [l for l in log if not l.startswith("failed") or "net::ERR_ABORTED" not in l]
                note(not errs, f"{lang}/{label} log: {errs[:4] if errs else 'clean'}")
                ctx.close()

        # buttons: Windows -> explanation, Mac -> file (in different languages; the button on the
        # pricing page and in the footer)
        for lang in ("en", "ru", "de"):
            ctx = browser.new_context(user_agent=MAC_UA, viewport={"width": 1360, "height": 900}, accept_downloads=True)
            ctx.add_init_script(f"try{{localStorage.setItem('argus.landing.lang','{lang}')}}catch(e){{}}")
            page = ctx.new_page()
            for where, sel in (("/", ".cta-row a.btn"), ("/", ".final a.btn"), ("/pricing.html", ".price-cta a.btn"), ("/", 'footer a[href="/download/mac"]')):
                page.goto(BASE + where, wait_until="load")
                page.wait_for_timeout(600)
                with page.expect_download(timeout=60000) as dl:
                    page.locator(sel).first.click()
                d = dl.value
                note(d.suggested_filename == "Argus-mac.dmg" and page.url.rstrip("/").endswith(where.rstrip("/")),
                     f"Mac {lang} {where} {sel}: {d.suggested_filename}, page stayed at {page.url}")
                d.cancel()
            ctx.close()
            ctx = browser.new_context(user_agent=WIN_UA, viewport={"width": 1360, "height": 900}, accept_downloads=True)
            ctx.add_init_script(f"try{{localStorage.setItem('argus.landing.lang','{lang}')}}catch(e){{}}")
            page = ctx.new_page()
            # Windows gets Argus-win.zip; if the file is not on the server, the button sends the visitor
            # back to the landing page with an explanation (?dl=soon)
            for where, sel in (("/", ".cta-row a.btn"), ("/pricing.html", ".price-cta a.btn"), ("/", 'footer a[href="/download/windows"]')):
                page.goto(BASE + where, wait_until="load")
                page.wait_for_timeout(500)
                try:
                    with page.expect_download(timeout=20000) as dl:
                        page.locator(sel).first.click()
                    d = dl.value
                    note(d.suggested_filename == "Argus-win.zip" and page.url.rstrip("/").endswith(where.rstrip("/")),
                         f"Windows {lang} {where} {sel}: {d.suggested_filename}, page stayed at {page.url}")
                    d.cancel()
                except Exception:
                    page.wait_for_timeout(900)
                    note(False, f"Windows {lang} {where} {sel}: no file, note={page.is_visible('#dl-note')} url={page.url}")
            ctx.close()
    finally:
        browser.close()
print("FAILURES:", bad)
# Exit code is for the caller: a check that always "succeeds" guards nothing.
sys.exit(1 if bad else 0)
