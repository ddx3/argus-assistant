"""Separate pages of the landing site: pricing, terms of use, privacy policy.

    python build_landing.py   -> out/landing.html (main) + out/pricing.html, out/terms.html,
                                 out/privacy.html

The pages sit next to the main one in a single artifact and link to each other by file name
(when published, the main page is called `index.html`). The header, footer and styles come
from the template of the main page so that they do not diverge; the language is chosen the
same way (the same key in localStorage), so the choice carries over between pages.

The terms and the policy are the same texts as in the app (`legal/text/*.html`), only in
English (a product decision); the operator fields are filled in from `legal/operator.json`,
and an empty field stays as a yellow marker (as in `legal/build.py`).
"""
import html
import json
import os
import re
import sys
from pathlib import Path

import price_copy

HERE = Path(__file__).parent
ROOT = HERE.parent
# The shared part of building the terms and the policy (languages, dates, articles): the
# standard library, like this builder itself.
sys.path.insert(0, str(ROOT / "legal"))
import legal_render  # noqa: E402
# The "Download" button: the server itself picks the file for the visitor's system
# (deploy/landing/nginx/default.conf)
DOWNLOAD = "/download"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
HINT = {"name": "operator's name", "email": "contact email", "country": "operator's country", "law": "governing law"}
TITLE = {"terms": "Terms of Use", "privacy": "Privacy Policy"}

CHECK = '<svg viewBox="0 0 24 24" aria-hidden="true">%s</svg>'
ICONS = {
    "gift": '<path d="M20 12v9H4v-9"/><rect x="2" y="7" width="20" height="5" rx="1"/><path d="M12 21V7M12 7H8.5a2.5 2.5 0 1 1 0-5C11 2 12 7 12 7Zm0 0h3.5a2.5 2.5 0 1 0 0-5C13 2 12 7 12 7Z"/>',
    "card": '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20M6 15h4"/>',
    "bars": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
}

PAGE_CSS = """
  /* pages for pricing, terms and policy */
  /* Short page (seller details): the footer sits at the bottom of the window instead of in
     the middle of a black field; the scrollbar always takes up space, so the content does
     not jump by 7 pixels when moving between a short and a long page (reported: everything
     on this page looked shifted). */
  html{scrollbar-gutter:stable;}
  body{min-height:100vh;display:flex;flex-direction:column;}
  body>main{flex:1 0 auto;}
  body>footer.site{margin-top:auto;}
  main .sect-head{max-width:none;}
  .page-head{padding-block:clamp(2.2rem,5vw,3.6rem) clamp(1.2rem,3vw,2rem);}
  .page-head h1{font-size:clamp(2rem,4.3vw,3.1rem);letter-spacing:-.012em;font-weight:700;margin-bottom:1rem;}
  .page-head .st-lede{max-width:none;}
  .fi-row.three{grid-template-columns:repeat(3,minmax(0,1fr));}
  @media (max-width:1000px){.fi-row.three{grid-template-columns:1fr;row-gap:0;}
    .fi-row.three .fi,.fi-row.three .fi:nth-child(n){padding:22px 0;border-left:0;border-top:1px solid var(--line);}
    .fi-row.three .fi:first-child{padding-top:0;border-top:0;}.fi-row.three .fi:last-child{padding-bottom:0;}}
  .pay-tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;max-width:860px;}
  @media (max-width:640px){.pay-tiles{grid-template-columns:repeat(2,minmax(0,1fr));}}
  .pay-tile{display:flex;flex-direction:column;gap:7px;padding:18px 20px;border-radius:14px;background:#121417;box-shadow:0 0 0 1px var(--slab-edge);}
  .pay-tile b{font-size:1.75rem;font-weight:700;letter-spacing:-.02em;line-height:1;}
  .pay-tile span{color:var(--dim);font-size:14px;}
  .rate-scroll{overflow-x:auto;border-radius:14px;background:#121417;box-shadow:0 0 0 1px var(--slab-edge);}
  .rates{width:100%;min-width:780px;border-collapse:collapse;}
  .rates th{padding:13px 18px;text-align:left;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);white-space:nowrap;}
  .rates tr.grp th{padding-bottom:0;text-align:right;}
  .rates thead tr:last-child th{border-bottom:1px solid var(--line);}
  .rates td{padding:16px 18px;border-top:1px solid var(--line);vertical-align:top;}
  .rates tbody tr:first-child td{border-top:0;}
  .rates .r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
  .rates td b{display:block;font-size:1.05rem;font-weight:650;letter-spacing:-.008em;}
  .rates td .pv{font-size:1.2rem;font-weight:700;}
  .rates td small{display:block;margin-top:3px;color:var(--dim);font-size:13px;font-weight:400;letter-spacing:0;}
  .fi.num b{display:block;font-size:clamp(1.8rem,3.4vw,2.6rem);font-weight:700;letter-spacing:-.025em;line-height:1.1;}
  .fi.num p{margin-top:0;}
  .notes{display:flex;flex-direction:column;gap:6px;margin-top:2rem;color:var(--faint);font-size:13.5px;line-height:1.55;}
  .notes a{color:var(--dim);text-decoration:underline;text-underline-offset:2px;}.notes a:hover{color:var(--text);}
  .price-cta{display:flex;flex-direction:column;align-items:flex-start;gap:1.1rem;}
  .price-cta p{color:var(--dim);font-size:clamp(1rem,1.6vw,1.2rem);}
  mark.todo{background:rgba(224,164,88,.18);color:#f0d19f;border-radius:4px;padding:0 5px;}
  .help-p{color:var(--dim);font-size:clamp(1rem,1.5vw,1.15rem);line-height:1.6;max-width:none;}
  .help-p a{color:var(--text);text-decoration:underline;text-underline-offset:2px;text-decoration-color:var(--faint);}
  .help-p a:hover{text-decoration-color:var(--text);}
  .help-mail{margin-top:.9rem;font-size:1.25rem;font-weight:650;letter-spacing:-.01em;}
  .help-mail a{color:var(--text);}
  /* document */
  .doc{padding-block:clamp(2rem,5vw,3.4rem);}
  .doc article{max-width:none;}
  .doc h1{font-size:clamp(1.8rem,4vw,2.5rem);letter-spacing:-.012em;line-height:1.2;margin-bottom:.4rem;}
  .doc h2{font-size:1.2rem;margin:2.2rem 0 .55rem;line-height:1.3;}
  .doc h3{font-size:1rem;margin:1.4rem 0 .4rem;}
  .doc p{margin:0 0 .8rem;color:#d6dae1;line-height:1.65;}
  .doc ul{margin:0 0 1rem;padding-left:1.4rem;color:#d6dae1;}
  .doc li{margin:0 0 .4rem;line-height:1.6;}
  .doc li::marker{color:var(--faint);}
  .doc a{color:var(--text);text-decoration:underline;text-underline-offset:2px;text-decoration-color:var(--faint);}
  .doc a:hover{text-decoration-color:var(--text);}
  .doc .meta{color:var(--faint);font-size:14px;margin-bottom:1.2rem;}
  .doc .lead{background:var(--slab);border:1px solid var(--slab-edge);border-radius:12px;padding:14px 16px;color:var(--dim);}
  .doc b{font-weight:600;color:var(--text);}
  .doc mark.todo{background:rgba(224,164,88,.18);color:#f0d19f;border-radius:4px;padding:0 4px;}

  .seller{display:grid;grid-template-columns:minmax(0,15rem) minmax(0,1fr);gap:.9rem 2rem;margin:1.6rem 0 0;}
  .seller dt{color:var(--dim);}.seller dd{margin:0;color:var(--text);overflow-wrap:anywhere;}
  @media (max-width:640px){.seller{grid-template-columns:minmax(0,1fr);gap:.2rem;}.seller dd{margin-bottom:.9rem;}}
  .doc-other{margin-top:2.4rem;padding-top:1.1rem;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px 22px;font-size:14px;}
  .doc-other a{color:var(--dim);}.doc-other a:hover{color:var(--text);}
"""

SUB_JS = """(function () {
  "use strict";
  var COPY = {{COPY}};
  var LANGS = {{LANGS}};
  var STORE = "argus.landing.lang";
  var lang = "en";
  try {
    var saved = localStorage.getItem(STORE);
    if (saved && LANGS.indexOf(saved) >= 0) lang = saved;
  } catch (e) { /* without storage: English */ }
  // Visitors from the primary target region open the site in the local language (a language
  // law and a payment-provider requirement): detected by browser language and time zone.
  // Everyone else gets English, as before; the person's own choice (in `localStorage`)
  // beats everything.
  function guessPrimaryRegion() {
    try {
      var langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""];
      for (var i = 0; i < langs.length; i++) if (/^uk(\\b|-)/i.test(String(langs[i]))) return true;
      var zone = (Intl.DateTimeFormat().resolvedOptions().timeZone || "");
      return /^Europe\\/(Kyiv|Kiev|Uzhgorod|Zaporozhye)$/.test(zone);
    } catch (e) { return false; }
  }
  if (lang === "en" && !saved && LANGS.indexOf("uk") >= 0 && guessPrimaryRegion()) lang = "uk";

  function t(id) {
    var own = COPY[lang], en = COPY.en, ru = COPY.ru;
    if (own && own[id] != null) return own[id];
    if (en && en[id] != null) return en[id];
    return ru ? ru[id] : null;
  }
  var btn = document.getElementById("lang-btn");
  var menu = document.getElementById("lang-menu");
  var opts = menu ? [].slice.call(menu.querySelectorAll(".opt")) : [];

  function syncPicker() {
    if (!menu) return;
    opts.forEach(function (o) {
      var on = o.getAttribute("data-v") === lang;
      o.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        document.getElementById("lang-name").textContent = o.textContent;
        document.getElementById("lang-code").textContent = o.getAttribute("data-code");
      }
    });
  }
  function applyCopy() {
    document.documentElement.lang = lang;
    // Terms and policy carry all languages at once (`legal/legal_render.py`): the article in
    // the page language is visible, and if there is none, the English one.
    var arts = [].slice.call(document.querySelectorAll("article[data-doc-lang]"));
    if (arts.length) {
      var have = arts.some(function (a) { return a.getAttribute("data-doc-lang") === lang; });
      arts.forEach(function (a) { a.hidden = a.getAttribute("data-doc-lang") !== (have ? lang : "en"); });
    }
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      var s = t(el.getAttribute("data-i18n"));
      if (s != null) el.innerHTML = s;
    });
    document.querySelectorAll("[data-i18n-attr]").forEach(function (el) {
      el.getAttribute("data-i18n-attr").split(";").forEach(function (pair) {
        var p = pair.split(":"), s = t(p[1]);
        if (s != null) el.setAttribute(p[0], s);
      });
    });
    var loc = lang === "pt" ? "pt-BR" : lang;
    function nf(n, d) { try { return new Intl.NumberFormat(loc, { maximumFractionDigits: d }).format(n); } catch (e) { return String(n); } }
    function hoursText(h) {
      if (h < 1) return Math.max(1, Math.round(h * 60)) + " " + (t("price.n.min") || "min");
      return nf(Math.round(h * 10) / 10, 1) + " " + (t("price.n.h") || "h");
    }
    function countText(n) {
      var k = t("price.n.k") || "k";
      if (n >= 100000) return nf(Math.round(n / 1000), 0) + " " + k;
      if (n >= 10000) return nf(Math.round(n / 100) / 10, 1) + " " + k;
      return nf(Math.round(n), 0);
    }
    document.querySelectorAll("[data-hours]").forEach(function (el) {
      var s = hoursText(parseFloat(el.getAttribute("data-hours")));
      if (el.hasAttribute("data-i18n")) el.innerHTML = el.innerHTML.replace("{n}", s); else el.textContent = s;
    });
    document.querySelectorAll("[data-count]").forEach(function (el) { el.textContent = countText(+el.getAttribute("data-count")); });
    document.querySelectorAll("[data-usd]").forEach(function (el) {
      var v = parseFloat(el.getAttribute("data-usd")), d = +el.getAttribute("data-digits"), s;
      try { s = new Intl.NumberFormat(loc, { style: "currency", currency: "USD", minimumFractionDigits: d, maximumFractionDigits: d }).format(v); }
      catch (e) { s = "$" + v.toFixed(d); }
      el.textContent = (el.hasAttribute("data-approx") ? "≈ " : "") + s;
    });
    var ti = document.documentElement.getAttribute("data-title");
    if (ti) { var s = t(ti); if (s != null) document.title = s; }
    syncPicker();
  }
  function setLanguage(l) {
    if (LANGS.indexOf(l) < 0) return;
    lang = l;
    try { localStorage.setItem(STORE, l); } catch (e) { /* harmless */ }
    applyCopy();
  }
  function openMenu() { menu.hidden = false; btn.setAttribute("aria-expanded", "true"); (menu.querySelector('[aria-selected="true"]') || opts[0]).focus(); }
  function closeMenu(focusButton) {
    if (menu.hidden) return;
    menu.hidden = true; btn.setAttribute("aria-expanded", "false");
    if (focusButton) btn.focus();
  }
  if (btn && menu) {
    btn.addEventListener("click", function () { if (menu.hidden) openMenu(); else closeMenu(false); });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (menu.hidden) openMenu(); }
    });
    menu.addEventListener("click", function (e) {
      var o = e.target.closest ? e.target.closest(".opt") : null;
      if (!o) return;
      setLanguage(o.getAttribute("data-v"));
      closeMenu(true);
    });
    menu.addEventListener("keydown", function (e) {
      var i = opts.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); opts[Math.min(opts.length - 1, i + 1)].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); opts[Math.max(0, i - 1)].focus(); }
      else if (e.key === "Home") { e.preventDefault(); opts[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); opts[opts.length - 1].focus(); }
      else if (e.key === "Escape") { e.preventDefault(); closeMenu(true); }
      else if (e.key === "Tab") { closeMenu(false); }
    });
    document.addEventListener("click", function (e) {
      if (!menu.hidden && !document.getElementById("langpick").contains(e.target)) closeMenu(false);
    });
  }
  applyCopy();

  // page-to-page transitions always start from the top: the artifact is shown in a frame of
  // full height, so the scroll position of the outer page stays the same on navigation and
  // would pin the person to the footer of the new, shorter page; `scrollIntoView` scrolls
  // the outer page too. A link to the same page does not reload it but scrolls smoothly up.
  var PAGE = {{PAGE}};
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  function toTop() {
    try { document.body.scrollIntoView({ block: "start", behavior: "instant" }); } catch (e) { window.scrollTo({ top: 0, left: 0, behavior: "instant" }); }
  }
  // Smooth return to the top with our own animation (`behavior:"smooth"` stutters on a
  // heavy page, and an instant jump looks abrupt): we find where the viewport is through
  // IntersectionObserver, move an invisible marker frame by frame and show it through
  // `scrollIntoView` (it also moves the outer page if the frame has full height).
  var smoothRun = 0;
  function smoothTop() {
    if (reduce) { toTop(); return; }
    var id = ++smoothRun;
    var root = document.documentElement;
    var own = root.scrollHeight > window.innerHeight + 2;      // the page scrolls by itself (a regular site); otherwise the frame has full height and the outer page has to be moved
    var pad = parseFloat(getComputedStyle(root).scrollPaddingTop) || 0;
    var probe = null;
    function run(start) {
      if (!(start > 2)) { if (probe) probe.remove(); toTop(); return; }
      var dur = Math.min(1100, 380 + start * 0.16), t0 = null;
      function cancel() { smoothRun++; }
      ["wheel", "touchstart", "keydown"].forEach(function (n) { window.addEventListener(n, cancel, { once: true, passive: true }); });
      function step(ts) {
        if (id !== smoothRun) { if (probe) probe.remove(); return; }
        if (t0 === null) t0 = ts;
        var k = Math.min(1, (ts - t0) / dur), e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
        var y = Math.max(0, Math.round(start * (1 - e)));
        if (own) window.scrollTo({ top: y, left: 0, behavior: "instant" });
        else {
          probe.style.top = (y + pad) + "px";      // the page's `scroll-padding-top` shifts the marker down: without a correction the first frame jerked upward
          try { probe.scrollIntoView({ block: "start", behavior: "instant" }); } catch (x) { window.scrollTo({ top: y, left: 0, behavior: "instant" }); }
        }
        if (k < 1) requestAnimationFrame(step); else { if (probe) probe.remove(); toTop(); }
      }
      requestAnimationFrame(step);
    }
    if (own) { run(window.scrollY); return; }
    if (!("IntersectionObserver" in window)) { toTop(); return; }
    probe = document.createElement("i");
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText = "position:absolute;left:0;top:0;width:1px;height:1px;pointer-events:none;visibility:hidden;";
    document.body.appendChild(probe);
    var io = new IntersectionObserver(function (es) {
      io.disconnect();
      var en = es[es.length - 1], r = en.intersectionRect;
      run(r && r.height ? r.top - en.boundingClientRect.top : 0);
    }, { threshold: [0] });
    io.observe(document.body);
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || a.target === "_blank" || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var h = a.getAttribute("href") || "";
    // a link to a page of the site: to the same page it scrolls smoothly up, to another page
    // it uses the same soft style: the content fades out (the header stays), the page changes,
    // and on the new page the content fades in (CSS `page-in`).
    // Requirement: a link to the same page scrolls smoothly, but a link to another page
    // switches abruptly; everything must use one style. We do not use the browser's
    // cross-page transitions: they are not available everywhere
    if (!/\\.html(#.*)?$/.test(h)) return;
    if (h.indexOf("#") < 0 && h.replace(/\\.html$/, "") === PAGE) { e.preventDefault(); smoothTop(); return; }
    e.preventDefault(); leave(h);
  });
  function leave(href) {
    if (reduce) { location.href = href; return; }
    var root = document.documentElement;
    root.classList.add("leaving");
    setTimeout(function () { location.href = href; }, 240);
    setTimeout(function () { root.classList.remove("leaving"); }, 4000);
  }
  window.addEventListener("pageshow", function (e) { if (e.persisted) document.documentElement.classList.remove("leaving"); });
  if (!location.hash) { toTop(); window.addEventListener("load", function () { toTop(); setTimeout(toTop, 200); }); }
})();
"""


def human_date(version: str) -> str:
    y, m, d = (int(x) for x in version.split("-"))
    return f"{MONTHS[m - 1]} {d}, {y}"


def terms_version() -> str:
    src = (ROOT / "src" / "argus" / "account" / "legal.py").read_text(encoding="utf-8")
    return re.search(r'TERMS_VERSION\s*=\s*"([^"]+)"', src).group(1)


def render_doc(doc: str) -> tuple[str, set]:
    """The document in all languages at once (`legal/legal_render.py`, as in `legal/build.py`): one
    `<article>` per language, the article for the page language is visible (`SUB_JS` switches it).
    An empty operator field is a yellow marker in the English text."""
    operator = json.loads((ROOT / "legal" / "operator.json").read_text(encoding="utf-8"))
    missing: set = set()

    def footer(lang: str, d: str) -> str:
        other = "privacy" if d == "terms" else "terms"
        return (f'<div class="doc-other"><a href="{other}.html">{legal_render.UI[lang][other]}</a>'
                f'<a href="index.html">Argus</a></div>\n')

    body, absent = legal_render.articles(doc, operator, terms_version(), missing, footer)
    if absent:
        raise SystemExit(f"no translation of document {doc}: {', '.join(absent)} - not building the landing page")
    return body, missing


def chrome(template: str, active: str = "") -> tuple[str, str, str]:
    """Styles, header and footer of the main page (already cleaned of disabled parts) for the side
    pages.
    """
    style = re.search(r"<style>(.*?)</style>", template, re.S).group(1)
    header = re.search(r'<header class="top">.*?</header>', template, re.S).group(0)
    footer = re.search(r'<footer class="site">.*?</footer>', template, re.S).group(0)
    # to the main page and its sections — by file name
    header = header.replace('<div class="brand">', '<a class="brand" href="index.html">', 1)
    header = re.sub(r'(<a class="brand" href="index.html">.*?)</div>', r"\1</a>", header, count=1, flags=re.S)
    for part in ("header", "footer"):
        pass
    header = header.replace('href="#', 'href="index.html#')
    footer = footer.replace('href="#', 'href="index.html#')
    header = header.replace(' class="on"', "")
    if active:
        header = header.replace(f'href="{active}"', f'href="{active}" class="on"', 1)
    return style, header, footer


def page(title_key: str, title: str, style: str, header: str, body: str, footer: str, copy: dict, langs: list,
         js_dumps, page_id: str = "") -> str:
    ids = set(re.findall(r'data-i18n="([^"]+)"', header + body + footer))
    for m in re.finditer(r'data-i18n-attr="([^"]+)"', header + body + footer):
        ids.update(pair.split(":")[1] for pair in m.group(1).split(";"))
    if title_key:
        ids.add(title_key)
    if title_key == "price.title":
        ids.update(("price.n.h", "price.n.min", "price.n.k"))   # the units of the numbers are filled in by the script; the markup has no markers for them
    sub ={lang: {k: v for k, v in d.items() if k in ids} for lang, d in copy.items()}
    js = SUB_JS.replace("{{COPY}}", js_dumps(sub)).replace("{{LANGS}}", js_dumps(langs)).replace("{{PAGE}}", js_dumps(page_id))
    title_attr = f' data-title="{title_key}"' if title_key else ""
    return (
        f'<!doctype html>\n<html lang="en"{title_attr}>\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        f"<title>{html.escape(title)}</title>\n"
        "<script>try{if(!location.hash)document.documentElement.scrollIntoView({behavior:'instant'})}catch(e){}</script>\n"
        '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 64 64\'%3E%3Crect width=\'64\' height=\'64\' rx=\'14\' fill=\'%23181a20\'/%3E%3Cg transform=\'translate(5 4.5) scale(.0787)\' fill=\'%23fff\'%3E%3Cpath d=\'M252.5 0H438.5L691 699H517L345.5 127L174 699H0Z\'/%3E%3Ccircle cx=\'345.5\' cy=\'525\' r=\'60\'/%3E%3C/g%3E%3C/svg%3E">\n'
        '<link rel="shortcut icon" href="/favicon.ico">\n'
        f"<style>{style}{PAGE_CSS}</style>\n</head>\n<body>\n{header}\n{body}\n{footer}\n<script>\n{js}\n</script>\n</body>\n</html>\n"
    )


def price_data() -> dict:
    """Prices and "what the amount will cover" — from `src/argus/server/pricing.py`, exactly as the
    app's "Account" window calculates them (`account.js`, `ratesBlock`).

    The file is executed separately (it has only `math` and `dataclasses`), so the app package and
    its dependencies are not needed to build the landing page.
    """
    src = (ROOT / "src" / "argus" / "server" / "pricing.py").read_text(encoding="utf-8")
    import sys
    import types
    mod = types.ModuleType("argus_pricing")
    sys.modules["argus_pricing"] = mod   # `@dataclass` looks up its module in `sys.modules`
    ns = mod.__dict__
    exec(compile(src, "pricing.py", "exec"), ns)
    sheet = ns["price_sheet"]()
    micro = ns["MICRO"]
    amounts = list(sheet["topups"])
    stt = sheet["stt_hour_micro"]
    stt_hi = sheet["stt_hour_high_micro"]
    tts = sheet["tts_1000_micro"]
    ans = [a["answer_micro"] for a in sheet["answers"]]
    return {
        "amounts": amounts,
        "min": sheet["min_topup"],
        "max": sheet["max_topup"],
        "stt_hour": stt / micro,
        "stt_min": ns["charged"](ns["stt_cost"](60)) / micro,
        "stt_hour_hi": stt_hi / micro,
        "stt_min_hi": ns["charged"](ns["stt_cost"](60, "high")) / micro,
        "tts_1000": tts / micro,
        "ans": [a / micro for a in ans],
        # how long each amount lasts: hours of speech, hours of smart recognition, voice-over
        # characters, number of answers
        "hours": [d * micro / stt for d in amounts],
        "hours_hi": [d * micro / stt_hi for d in amounts],
        "chars": [d * micro / tts * 1000 for d in amounts],
        "answers": [[int(d * micro // a) for d in amounts] for a in ans],
    }


def fmt_hours(h: float) -> str:
    """Like `hoursText` in the app: under an hour, minutes; otherwise hours with tenths (English
    format; the browser reformats it per language).
    """
    if h < 1:
        return f"{max(1, round(h * 60))} {price_copy.EN['price.n.min']}"
    v = round(h * 10) / 10
    return f"{v} {price_copy.EN['price.n.h']}"


def fmt_count(n: float) -> str:
    """Like `count` in the app: thousands get "k"."""
    k = price_copy.EN["price.n.k"]
    if n >= 100000:
        return f"{round(n / 1000):,} {k}"
    if n >= 10000:
        v = round(n / 100) / 10
        return f"{v} {k}"
    return f"{round(n):,}"


def fmt_money(v: float, digits: int) -> str:
    return f"${v:.{digits}f}"


def pricing_body(values: dict) -> str:
    d = price_data()
    en = {k: fill_text(v, values) if k in FILLED else v for k, v in price_copy.EN.items()}
    amounts = d["amounts"]

    def step(icon, col, key):
        return (f'        <div class="fi" style="--c:{col}">\n'
                f'          <div class="fi-h"><div class="fi-ico" aria-hidden="true">{CHECK % ICONS[icon]}</div>'
                f'<h3 data-i18n="price.{key}.h">{en[f"price.{key}.h"]}</h3></div>\n'
                f'          <p data-i18n="price.{key}.p">{en[f"price.{key}.p"]}</p>\n        </div>\n')

    def tile(dollars, hours):
        return (f'        <div class="pay-tile"><b>${dollars}</b><span data-i18n="price.amt.t" data-hours="{hours:.4f}">'
                f'{en["price.amt.t"].replace("{n}", fmt_hours(hours))}</span></div>\n')

    def usd(v, digits, approx=False):
        mark = " data-approx" if approx else ""
        shown = ("≈ " if approx else "") + fmt_money(v, digits)
        return f'<b class="pv" data-usd="{v:.{digits}f}" data-digits="{digits}"{mark}>{shown}</b>'

    def row(name_key, kind_key, cost_html, cells):
        tds = "".join(cells)
        return (f'            <tr><td><b data-i18n="price.{name_key}">{en["price." + name_key]}</b>'
                f'<small data-i18n="price.k.{kind_key}">{en["price.k." + kind_key]}</small></td>'
                f'<td class="r">{cost_html}</td>{tds}</tr>\n')

    def hours_cells(values):
        return [f'<td class="r" data-hours="{h:.4f}">{fmt_hours(h)}</td>' for h in values]

    def count_cells(values):
        return [f'<td class="r" data-count="{int(n)}">{fmt_count(n)}</td>' for n in values]

    def per(unit_key, usd_html):
        return usd_html + f'<small data-i18n="price.u.{unit_key}">{en["price.u." + unit_key]}</small>'

    def per_min(v):
        return f'<small><span data-usd="{v:.3f}" data-digits="3">{fmt_money(v, 3)}</span> <span data-i18n="price.u.min">{en["price.u.min"]}</span></small>'

    rows = ""
    rows += row("r1", "speech", per("hour", usd(d["stt_hour"], 2)) + per_min(d["stt_min"]), hours_cells(d["hours"]))
    rows += row("r2", "speech", per("hour", usd(d["stt_hour_hi"], 2)) + per_min(d["stt_min_hi"]), hours_cells(d["hours_hi"]))
    rows += row("r5", "chars", per("chars", usd(d["tts_1000"], 2)), count_cells(d["chars"]))
    rows += row("r3", "answers", per("answer", usd(d["ans"][0], 3, True)), count_cells(d["answers"][0]))
    rows += row("r4", "answers", per("answer", usd(d["ans"][1], 3, True)), count_cells(d["answers"][1]))
    heads = "".join(f'<th class="r">${a}</th>' for a in amounts)
    tiles = "".join(tile(a, h) for a, h in zip(amounts, d["hours"]))

    return f"""<main>
  <section class="page-head">
    <div class="wrap">
      <h1 data-i18n="price.h1">{en["price.h1"]}</h1>
      <p class="st-lede" data-i18n="price.lede">{en["price.lede"]}</p>
    </div>
  </section>
  <section class="sect" style="border-top:0;padding-top:0">
    <div class="wrap">
      <div class="fi-row three">
{step("gift", "var(--screen)", "s1")}{step("card", "var(--me)", "s2")}{step("bars", "var(--ok)", "s3")}      </div>
    </div>
  </section>
  <section class="sect">
    <div class="wrap">
      <div class="sect-head"><h2 data-i18n="price.amt.h">{en["price.amt.h"]}</h2><p data-i18n="price.amt.p">{en["price.amt.p"]}</p></div>
      <div class="pay-tiles">
{tiles}      </div>
    </div>
  </section>
  <section class="sect">
    <div class="wrap">
      <div class="sect-head"><h2 data-i18n="price.t.h">{en["price.t.h"]}</h2><p data-i18n="price.t.sub">{en["price.t.sub"]}</p></div>
      <div class="rate-scroll">
        <table class="rates">
          <thead>
            <tr class="grp"><th colspan="2"></th><th class="r" colspan="{len(amounts)}" data-i18n="price.t.fits">{en["price.t.fits"]}</th></tr>
            <tr><th data-i18n="price.t.svc">{en["price.t.svc"]}</th><th class="r" data-i18n="price.t.cost">{en["price.t.cost"]}</th>{heads}</tr>
          </thead>
          <tbody>
{rows}          </tbody>
        </table>
      </div>
      <div class="notes">
        <p data-i18n="price.t.note">{en["price.t.note"]}</p>
        <p data-i18n="price.n1">{en["price.n1"]}</p>
        <p data-i18n="price.n2">{en["price.n2"]}</p>
      </div>
    </div>
  </section>
  <section class="sect">
    <div class="wrap">
      <div class="sect-head"><h2 data-i18n="price.pay.h">{en["price.pay.h"]}</h2></div>
      <p class="help-p" data-i18n="price.pay.p">{en["price.pay.p"]}</p>
    </div>
  </section>
  <section class="sect">
    <div class="wrap">
      <div class="sect-head"><h2 data-i18n="price.ref.h">{en["price.ref.h"]}</h2></div>
      <p class="help-p" data-i18n="price.ref.p">{en["price.ref.p"]}</p>
    </div>
  </section>
  <section class="sect">
    <div class="wrap price-cta">
      <p data-i18n="price.cta">{en["price.cta"]}</p>
      <a class="btn lg" href="{DOWNLOAD}"><span class="dot" aria-hidden="true"></span><span data-i18n="btn.get">DOWNLOAD</span></a>
    </div>
  </section>
</main>"""


# Seller: the data lives in a personal file OUTSIDE the project (it does not get into the
# repository or the git history). No file — no "Seller information" page
# and no link to it in the footer: to take the data off the site, it is enough to remove the
# file and publish the landing page again (`deploy/landing/README.md`, the section of the same
# name).
SELLER_FILE = Path(os.environ.get("ARGUS_SELLER_FILE") or Path.home() / ".argus" / "seller.json")
SELLER_FIELDS = ("name", "tin", "address", "email")
# Strings with substitutions made at build time (`{email}`, `{u5}`…): in the markup and in the
# page text they are already filled in.
FILLED = ("price.ref.p",)


def seller_data() -> dict | None:
    try:
        data = json.loads(SELLER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not all(str(data.get(k, "")).strip() for k in SELLER_FIELDS):
        raise SystemExit(f"{SELLER_FILE}: non-empty fields required: {', '.join(SELLER_FIELDS)}")
    return {k: str(data[k]).strip() for k in SELLER_FIELDS}


def fill_text(text: str, values: dict) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def seller_body(d: dict) -> str:
    en = price_copy.EN
    e = html.escape
    nl = chr(10)
    cells = []
    for key in SELLER_FIELDS:
        shown = f'<a href="mailto:{e(d[key], quote=True)}">{e(d[key], quote=False)}</a>' if key == "email" else e(d[key], quote=False)
        cells.append(f'<dt data-i18n="seller.l.{key}">{en["seller.l." + key]}</dt><dd>{shown}</dd>')
    return nl.join([
        '<main class="doc"><div class="wrap">',
        '<article>',
        f'<h1 data-i18n="seller.h1">{en["seller.h1"]}</h1>',
        f'<p class="meta" data-i18n="seller.lede">{en["seller.lede"]}</p>',
        '<dl class="seller">', nl.join(cells), '</dl>',
        '</article>',
        '</div></main>',
    ])


def operator_email() -> str:
    """The operator's email for the "Help" page: an empty field gives a yellow marker (as in
    the documents).
    """
    operator = json.loads((ROOT / "legal" / "operator.json").read_text(encoding="utf-8"))
    mail = str(operator.get("email", "")).strip()
    if mail:
        m = html.escape(mail, quote=False)
        return f'<a href="mailto:{m}">{m}</a>'
    return '<mark class="todo">[contact email]</mark>'


def help_body(template: str) -> str:
    """Questions and answers (a section from the main page template; they are not on the main page),
    contact and refund.
    """
    faq = re.search(r"<!--faq-->(.*?)<!--/faq-->", template, re.S).group(1)
    faq = re.sub(r'<h2 data-i18n="faq\.h2">[^<]*</h2>', '<h2 data-i18n="faq.tag">Questions</h2>', faq, count=1)
    faq = faq.replace('<section class="sect" id="faq">', '<section class="sect" id="faq" style="border-top:0;padding-top:0">', 1)
    nl = chr(10)

    def block(key, extra=""):
        h = price_copy.EN["help." + key + ".h"]
        t = price_copy.EN["help." + key + ".p"]
        return nl.join([
            '  <section class="sect">',
            '    <div class="wrap">',
            '      <div class="sect-head"><h2 data-i18n="help.' + key + '.h">' + h + '</h2></div>',
            '      <p class="help-p" data-i18n="help.' + key + '.p">' + t + '</p>',
            extra + '    </div>',
            '  </section>',
        ]) + nl

    mail = '      <p class="help-mail">' + operator_email() + '</p>' + nl
    head = nl.join([
        '<main>',
        '  <section class="page-head">',
        '    <div class="wrap">',
        '      <h1 data-i18n="help.h1">' + price_copy.EN["help.h1"] + '</h1>',
        '      <p class="st-lede" data-i18n="help.lede">' + price_copy.EN["help.lede"] + '</p>',
        '    </div>',
        '  </section>',
        '  ' + faq,
    ]) + nl
    return head + block("contact", mail) + block("refund") + "</main>"


def doc_body(doc: str) -> tuple[str, set]:
    articles, missing = render_doc(doc)
    body = f'<main class="doc"><div class="wrap">\n{articles}\n</div></main>'
    return body, missing


def build_all(template: str, copy: dict, langs: list, js_dumps, out: Path) -> dict:
    """Build the three pages into `out/`. Returns {name: size} and the list of unfilled operator
    fields.
    """
    from strip_comments import strip_html
    import build_demo

    result = {}
    # Substitutions in the price strings: the seller's email.
    values = {"email": operator_email()}
    copy = {lang: {k: fill_text(v, values) if k in FILLED else v for k, v in d.items()} for lang, d in copy.items()}
    style, header, footer = chrome(template, active="pricing.html")
    html_p = page("price.title", "Pricing — Argus", style, header, pricing_body(values), footer, copy, langs, js_dumps, "pricing")
    (out / "pricing.html").write_text(strip_html(html_p) if build_demo.STRIP else html_p, encoding="utf-8")
    result["pricing.html"] = len(html_p)
    style, header, footer = chrome(template, active="help.html")
    html_h = page("help.title", "Help — Argus", style, header, help_body(template), footer, copy, langs, js_dumps, "help")
    (out / "help.html").write_text(strip_html(html_h) if build_demo.STRIP else html_h, encoding="utf-8")
    result["help.html"] = len(html_h)
    missing_all = set()
    style, header, footer = chrome(template)
    for doc in ("terms", "privacy"):
        body, missing = doc_body(doc)
        missing_all |= missing
        h = page("", f"{TITLE[doc]} — Argus", style, header, body, footer, copy, langs, js_dumps, doc)
        (out / f"{doc}.html").write_text(strip_html(h) if build_demo.STRIP else h, encoding="utf-8")
        result[f"{doc}.html"] = len(h)
    seller = seller_data()
    if seller:
        h = page("seller.title", "Seller information — Argus", style, header, seller_body(seller), footer, copy, langs, js_dumps, "seller")
        (out / "seller.html").write_text(strip_html(h) if build_demo.STRIP else h, encoding="utf-8")
        result["seller.html"] = len(h)
    elif (out / "seller.html").exists():
        (out / "seller.html").unlink()          # no data any more — no pages either
    result["_missing"] = sorted(missing_all)
    return result
