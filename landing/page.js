(function () {
  "use strict";

  // Substituted at build time (build_landing.py).
  var DEMO = {{DEMO_SRC}};       // the real app page as a single file
  var COPY = {{COPY}};           // {language: {id: text}} — the page texts
  var LANGDATA = {{LANGDATA}};   // {language: {scenario, screens, call}} — a sample conversation
  var RECDEMO = {{RECDEMO}};     // the real "Record" page as a single file: the finale of the "Remembers" block
  var RECDATA = {{RECDATA}};     // {language: {lang, dict, record, chats}} — its made-up data

  var LANGS = {{LANGS}};          // enabled languages: for now only English, the others are ready but switched off
  var STORE = "argus.landing.lang";
  var EXT = { poster_dash: "jpg" };
  // The veil of the glass window over the call is exactly the one in the app (`--glass-tint`,
  // default .9): the page takes the window's own styles and overrides nothing.

  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var lang = "en";
  var live = [];   // running demos: { frame, host, w, h, id, visible }

  // ------------------------------------------------------------------ language

  // English by default. We do not guess the language from the browser: the person's choice is
  // remembered. The exception is a visitor detected as local (below).
  // Local visitors get the site in the `uk` language (a language law and a payment-provider
  // requirement), detected by the browser language and the time zone.
  // Everyone else gets English, as before; the person's own choice (in `localStorage`) always wins.
  function guessPrimaryRegion() {
    try {
      var langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""];
      for (var i = 0; i < langs.length; i++) if (/^uk(\b|-)/i.test(String(langs[i]))) return true;
      var zone = (Intl.DateTimeFormat().resolvedOptions().timeZone || "");
      return /^Europe\/(Kyiv|Kiev|Uzhgorod|Zaporozhye)$/.test(zone);
    } catch (e) { return false; }
  }
  function detect() {
    var saved = null;
    try {
      saved = localStorage.getItem(STORE);
      // A language chosen earlier that is not on the page now (the picker is deferred) is forgotten.
      if (saved && LANGS.indexOf(saved) < 0) { localStorage.removeItem(STORE); saved = null; }
    } catch (e) { /* no storage — English */ }
    return saved || (LANGS.indexOf("uk") >= 0 && guessPrimaryRegion() ? "uk" : "en");
  }

  // Strings that a language lacks (not translated yet) are taken from English, then from Russian.
  function t(id) {
    var own = COPY[lang], en = COPY.en, ru = COPY.ru;
    if (own && own[id] != null) return own[id];
    if (en && en[id] != null) return en[id];
    return ru ? ru[id] : null;
  }

  function applyCopy() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      var s = t(el.getAttribute("data-i18n"));
      if (s == null) return;
      el.innerHTML = s;
    });
    document.querySelectorAll("[data-i18n-attr]").forEach(function (el) {
      el.getAttribute("data-i18n-attr").split(";").forEach(function (pair) {
        var p = pair.split(":");
        var s = t(p[1]);
        if (s != null) el.setAttribute(p[0], s);
      });
    });
    document.querySelectorAll("img[data-img]").forEach(function (im) {
      var name = im.getAttribute("data-img");
      im.style.visibility = "";
      var url = "img/" + lang + "/" + name + "." + (EXT[name] || "png");
      // The frames are shot at double density: `2x` tells the browser they are half the size in CSS
      // pixels, and shows them at the natural size of the window.
      if (im.hasAttribute("data-2x")) { im.removeAttribute("src"); im.srcset = url + " 2x"; }
      else im.src = url;
    });
    var call = (LANGDATA[lang] || LANGDATA.en).call;
    document.querySelectorAll("[data-av]").forEach(function (el) { el.textContent = call.initials[+el.getAttribute("data-av")]; });
    document.querySelectorAll("[data-nm]").forEach(function (el) { el.textContent = call.names[+el.getAttribute("data-nm")]; });
    syncPicker();
  }

  // ------------------------------------------------------- language picker in the header
  // Deferred: the markup is in lang_picker.html. While it is not on the page, the block does nothing.

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
  function openMenu() {
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    var sel = menu.querySelector('[aria-selected="true"]') || opts[0];
    sel.focus();
  }
  function closeMenu(focusButton) {
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
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
      setLanguage(o.getAttribute("data-v"), true);
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

  // ------------------------------------------------------------ the live window

  function jsonForScript(o) { return JSON.stringify(o).replace(/<\//g, "<\\/"); }

  // The same app window in two forms: the full one and the "overlay" (/panel/live).
  function srcdoc(opts) {
    var d = LANGDATA[lang] || LANGDATA.en;
    var cfg = "<script>window.ARGUS_LANG=" + JSON.stringify(lang) +
              ";window.__ARGUS_ID__=" + JSON.stringify(opts.id) +
              ";window.__ARGUS_START__=" + (opts.start || 0) +
              ";window.__ARGUS_SCREENS__=" + jsonForScript(d.screens) +
              ";window.__ARGUS_SCENARIO__=" + jsonForScript(d.scenario) + ";" +
              (reduce ? "window.__ARGUS_FREEZE__=54;" : "") +
              (opts.glass ? "document.documentElement.dataset.glass='1';" : "") + "<\/script>";
    var html = DEMO;
    if (opts.solo) html = html.split("location.pathname.match(").join("'/panel/live'.match(");
    var HEAD = "<" + "head>";   // we do not write the document markup in this file, not even as a string
    // The replacement is a function, not a string: in a string `$$` means "one dollar sign", and
    // the formulas in the answer text (`$$S = 10\,000 \cdot ...$$`) lost half of their characters
    // and were shown as raw TeX.
    return html.replace(HEAD, function () { return HEAD + cfg; });
  }

  function mount(host, opts) {
    var f = document.createElement("iframe");
    f.title = opts.title;
    f.setAttribute("tabindex", "-1");
    f.setAttribute("aria-hidden", "true");
    f.loading = "eager";
    f.style.width = opts.w + "px";
    f.style.height = opts.h + "px";
    f.srcdoc = srcdoc(opts);
    host.appendChild(f);
    var d = { frame: f, host: host, watch: opts.watch || host, w: opts.w, h: opts.h, id: opts.id, visible: false };
    live.push(d);
    fit(d);
    return d;
  }
  function fit(d) {
    var k = d.host.clientWidth / d.w;
    if (k > 0) d.frame.style.transform = "scale(" + k + ")";
  }
  function post(d, cmd) {
    try { d.frame.contentWindow.postMessage({ argus: "demo", cmd: cmd }, "*"); } catch (e) { /* the window is already removed */ }
  }

  window.addEventListener("message", function (e) {
    var m = e.data;
    if (!m) return;
    if (m.argus !== "demo-ready") return;
    for (var i = 0; i < live.length; i++) {
      if (live[i].frame.contentWindow === e.source) {
        live[i].frame.classList.add("on");
        if (!live[i].visible) post(live[i], "pause");
        // the poster is needed while the window loads; under the glass it would show through the live window
        (function (host) {
          setTimeout(function () {
            host.querySelectorAll("img").forEach(function (im) { im.style.visibility = "hidden"; });
          }, 450);
        })(live[i].host);
      }
    }
  });

  // Play only what is on screen: two windows with a live feed must not heat up the tab while
  // they are out of view.
  var io = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      for (var i = 0; i < live.length; i++) {
        if (live[i].watch === en.target) {
          live[i].visible = en.isIntersecting;
          post(live[i], en.isIntersecting && !reduce ? "play" : "pause");
        }
      }
    });
  }, { threshold: 0.2 }) : null;

  var main = null;

  function start() {
    var narrow = window.matchMedia("(max-width:719px)").matches;
    var stage = document.getElementById("stage-view");
    var slot = document.getElementById("slot");
    var title = t("img.stage") || "Argus";
    main = mount(stage, narrow
      ? { id: "stage", solo: true, w: 400, h: 440, start: 8, title: title }
      : { id: "stage", w: 1200, h: 700, start: 8, title: title });
    var over = mount(slot, { id: "slot", solo: true, glass: true, w: 400, h: 440, start: 24, title: t("img.solo") || "Argus" });
    if (io) {
      io.unobserve(stage); io.unobserve(slot);     // again, so that the first visibility notification arrives
      io.observe(stage); io.observe(slot);
    } else {
      main.visible = over.visible = true;
    }
  }

  function stop() {
    live.forEach(function (d) { if (d.frame.parentNode) d.frame.parentNode.removeChild(d.frame); });
    live = [];
  }

  function setLanguage(next, remember) {
    if (LANGS.indexOf(next) < 0) next = "en";
    lang = next;
    if (remember) { try { localStorage.setItem(STORE, lang); } catch (e) { /* no harm done */ } }
    applyCopy();
    memLiveDrop();                     // the record page in the "Remembers" block comes up again in the new language
    if (MEM && MEM.on) memLiveMount();
    if (main) { stop(); start(); }     // the window restarts in the new language
  }

  window.addEventListener("resize", function () { live.forEach(fit); });
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(function () { live.forEach(fit); });
    ro.observe(document.getElementById("stage-view"));
    ro.observe(document.getElementById("slot"));
  }


  // ------------------------------------------- "how they see": the before and after slider

  var cmp = document.getElementById("compare");
  var handle = document.getElementById("cmp-handle");
  var pos = 50, sweeping = 0;
  function setPos(p) {
    pos = Math.max(0, Math.min(100, p));
    cmp.style.setProperty("--p", pos + "%");
    handle.setAttribute("aria-valuenow", String(Math.round(pos)));
  }
  function stopSweep() { sweeping = 0; }
  function fromEvent(e) {
    var r = cmp.getBoundingClientRect();
    return r.width ? (e.clientX - r.left) / r.width * 100 : 50;
  }
  var dragging = false;
  cmp.addEventListener("pointerdown", function (e) {
    if (e.button) return;
    dragging = true; stopSweep();
    try { cmp.setPointerCapture(e.pointerId); } catch (x) { /* works without pointer capture too */ }
    cmp.classList.add("drag");
    setPos(fromEvent(e));
  });
  cmp.addEventListener("pointermove", function (e) { if (dragging) setPos(fromEvent(e)); });
  function release(e) {
    dragging = false;
    cmp.classList.remove("drag");
    try { cmp.releasePointerCapture(e.pointerId); } catch (x) { /* already released */ }
  }
  cmp.addEventListener("pointerup", release);
  cmp.addEventListener("pointercancel", release);
  handle.addEventListener("keydown", function (e) {
    var step = { ArrowLeft: -5, ArrowDown: -5, ArrowRight: 5, ArrowUp: 5, PageDown: -20, PageUp: 20 }[e.key];
    if (step != null) { e.preventDefault(); stopSweep(); setPos(pos + step); }
    else if (e.key === "Home") { e.preventDefault(); stopSweep(); setPos(0); }
    else if (e.key === "End") { e.preventDefault(); stopSweep(); setPos(100); }
  });

  // When the block first enters the viewport, the divider swings once: this shows that it can
  // be dragged.
  if ("IntersectionObserver" in window && !reduce) {
    var hint = new IntersectionObserver(function (entries) {
      if (!entries[0].isIntersecting) return;
      hint.disconnect();
      var id = ++sweeping, t0 = performance.now(), keys = [50, 18, 82, 50], span = 2600;
      (function frame(now) {
        if (sweeping !== id) return;
        var k = Math.min(1, (now - t0) / span) * (keys.length - 1);
        var i = Math.min(keys.length - 2, Math.floor(k)), f = k - i;
        var ease = f < .5 ? 2 * f * f : 1 - Math.pow(-2 * f + 2, 2) / 2;
        setPos(keys[i] + (keys[i + 1] - keys[i]) * ease);
        if (k < keys.length - 1) requestAnimationFrame(frame); else sweeping = 0;
      })(t0);
    }, { threshold: 0.6 });
    hint.observe(cmp);
  }

  // ------------------------------------------------ the "how it works" tiles
  // Four tiles with live icons in CSS; only one of them needs the script: the answer format
  // (text, code, formulas, tables, other) switches in turn.
  // Below are the shared helpers for frames and the mouse: the "Remembers" block uses them too.

  // show frame number i from the stack `name`
  function cFrame(el, name, i) {
    [].forEach.call(el.querySelectorAll('[data-swap="' + name + '"] > img'), function (im, j) { im.classList.toggle("on", j === i); });
  }
  function cOn(el, sel, on) {
    var n = el.querySelector(sel);
    if (n) n.classList.toggle("on", on);
  }
  // the mouse in fractions of the frame (x, y in percent); with no arguments it hides
  function cCursor(el, x, y) {
    var c = el.querySelector(".ccur");
    if (!c) return;
    if (x == null) { c.classList.remove("on"); return; }
    c.style.left = x + "%"; c.style.top = y + "%"; c.classList.add("on");
  }
  function cClick(el) {
    var c = el.querySelector(".ccur");
    c.classList.remove("click"); void c.offsetWidth; c.classList.add("click");
  }
  var fmtEl = document.getElementById("tile-fmt");
  function tilesInit() {
    if (!fmtEl) return;
    var chips = fmtEl.querySelectorAll(".fmt span"), prev = fmtEl.querySelectorAll(".prev > *"), i = 0, timer = null;
    function set(n) { for (var k = 0; k < chips.length; k++) { chips[k].classList.toggle("on", k === n); prev[k].classList.toggle("on", k === n); } }
    set(0);
    if (reduce) return;
    function start() { if (!timer) timer = setInterval(function () { i = (i + 1) % chips.length; set(i); }, 2000); }
    function stop() { clearInterval(timer); timer = null; }
    if (!io) { start(); return; }
    new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) start(); else stop(); }); }, { threshold: 0.3 }).observe(fmtEl);
  }

  // ------------------------------------------------ "Remembers": sessions and record
  // Two real windows are on the stage at once, the active one in front. The mouse finds a row in
  // the sessions and presses the record button, the record comes forward, the request is typed
  // in slices of the input line, the finished answer appears from top to bottom, and then a
  // click on the time in the answer opens the conversation text at the right turn. Where things
  // lie in the frames (the record button, the input line, the time in the answer) is taken from
  // `mem.json`: `screens.py` writes it together with the frames.

  var memEl = document.getElementById("mem");
  var MEMPOS = {{MEMPOS}};
  var MEM_W = 640;                // width of the window frames in pixels: the frames were shot at it, and the record page is set to it
  var MEM = null;

  function memFrameOf(which) { return memEl.querySelector('[data-f="' + which + '"]'); }

  // The finale is the real record page (`RECDEMO`) over the frame with the finished answer: a
  // click on the time starts the window's own animation with the text, the same one as in the
  // app. The page is brought up in advance and reports `rec-ready`; until it is up (or without
  // motion), the finale is the ready frame. The page itself says where the time in the answer
  // is: the viewer has their own fonts, and on our frame the mouse would miss the right place.
  function memLiveSrcdoc(d) {
    var cfg = "<script>window.ARGUS_LANG=" + JSON.stringify(d.lang) + ";window.ARGUS_I18N=" + jsonForScript(d.dict) +
              ";window.__ARGUS_RECORD__=" + jsonForScript(d.record) + ";window.__ARGUS_CHATS__=" + jsonForScript(d.chats) + ";<\/script>";
    var HEAD = "<" + "head>";
    return RECDEMO.replace(HEAD, function () { return HEAD + cfg; });
  }
  function memLiveMount() {
    var d = RECDATA[lang] || RECDATA.en;
    if (reduce || !MEM || MEM.live || !d) return;
    var host = memEl.querySelector(".mlive");
    var f = document.createElement("iframe");
    f.title = t("img.record") || "Argus";
    f.setAttribute("tabindex", "-1");
    f.setAttribute("aria-hidden", "true");
    f.loading = "eager";
    f.style.width = MEM_W + "px";
    f.style.height = MEM_W + "px";
    f.srcdoc = memLiveSrcdoc(d);
    host.appendChild(f);
    MEM.live = { f: f, host: host, ready: false, cite: null };
    memLiveFit();
    if (window.ResizeObserver) { MEM.live.ro = new ResizeObserver(memLiveFit); MEM.live.ro.observe(host); }
  }
  function memLiveFit() {
    var L = MEM && MEM.live;
    if (!L) return;
    var k = L.host.clientWidth / MEM_W;
    if (k > 0) L.f.style.transform = "scale(" + k + ")";
  }
  function memLivePost(cmd) {
    var L = MEM && MEM.live;
    if (L && L.ready) { try { L.f.contentWindow.postMessage({ argus: "demo", cmd: cmd }, "*"); } catch (e) { /* the page is already removed */ } }
  }
  function memLiveDrop() {
    var L = MEM && MEM.live;
    if (!L) return;
    if (L.ro) L.ro.disconnect();
    L.host.classList.remove("on");
    if (L.f.parentNode) L.f.parentNode.removeChild(L.f);
    MEM.live = null;
  }
  window.addEventListener("message", function (e) {
    var m = e.data, L = MEM && MEM.live;
    if (!L || !m || e.source !== L.f.contentWindow) return;
    if (m.argus === "rec-ready") L.ready = true;
    if ((m.argus === "rec-ready" || m.argus === "rec-rect") && m.cite) L.cite = m.cite;
  });

  function memReset() {
    var L = MEM.live;
    if (L) { L.host.classList.remove("on"); memLivePost("reset"); }       // the page fades together with the window and closes the text window itself
    memEl.classList.remove("opened");
    memEl.querySelector(".mtype").classList.remove("gone");
    cFrame(memFrameOf("s"), "sess", 0);
    cFrame(memFrameOf("r"), "type", -1);
    cCursor(memFrameOf("s"), null); cCursor(memFrameOf("r"), null);
    [].forEach.call(memEl.querySelectorAll('[data-swap="rec"] > img'), function (im, j) {
      im.classList.toggle("on", j === 0); im.classList.remove("rv");
      im.style.webkitMaskImage = im.style.maskImage = "";
    });
  }

  // the answer appears from top to bottom by rows: the ready frame is laid over "Reading the
  // recording…", and a mask opens it in steps
  function memReveal(img, from, ms, steps) {
    var id = ++MEM.rv, t0 = performance.now();
    img.classList.add("rv");
    (function tick(now) {
      if (MEM.rv !== id || !MEM.on) return;
      var k = Math.min(1, (now - t0) / ms);
      var p = (from + (1 - from) * Math.floor(k * steps) / steps) * 100;
      var g = "linear-gradient(to bottom,#000 " + p + "%,transparent " + (p + 4) + "%)";
      img.style.webkitMaskImage = g; img.style.maskImage = g;
      if (k < 1) requestAnimationFrame(tick);
      else { img.style.webkitMaskImage = ""; img.style.maskImage = ""; }
    })(t0);
  }

  function memStop() {
    if (!MEM) return;
    MEM.on = false; MEM.rv++;
    MEM.timers.forEach(clearTimeout); MEM.timers = [];
  }

  // The mouse on its way: when its tip enters the `band` (in fractions of the frame height,
  // from the bottom up), call `then`. So the hover turns on at the moment the mouse touches the
  // row, not when it reaches its final spot. Requirement: the hover must fire on approach, not
  // only at the click.
  function memEnter(frameEl, band, then) {
    var cur = frameEl.querySelector(".ccur"), gen = MEM.rv, t0 = performance.now();
    (function tick() {
      if (!MEM.on || MEM.rv !== gen) return;
      var f = frameEl.getBoundingClientRect(), c = cur.getBoundingClientRect();
      var y = f.height ? (c.top - f.top) / f.height * 100 : band[1];
      if (y <= band[1] || performance.now() - t0 > 2500) { then(); return; }
      requestAnimationFrame(tick);
    })();
  }

  function memRun() {
    memStop();
    MEM.on = true;
    var P = MEMPOS || { s: { row: [20, 32], record: [78, 35] }, r3: { cite: [19, 41] } };
    var fs = memFrameOf("s"), fr = memFrameOf("r");
    var rec = [].slice.call(memEl.querySelectorAll('[data-swap="rec"] > img'));
    var at = function (ms, fn) { MEM.timers.push(setTimeout(function () { if (MEM.on) fn(); }, ms)); };
    memReset();
    at(500, function () { cCursor(fs, 74, 90); });
    at(1100, function () {
      cCursor(fs, P.s.row[0], P.s.row[1]);
      memEnter(fs, P.s.band || [27, 36], function () { cFrame(fs, "sess", 1); });   // hover: the row lights up and its buttons show as soon as the mouse enters it
    });
    at(2800, function () { cCursor(fs, P.s.record[0], P.s.record[1]); });
    at(3500, function () { cFrame(fs, "sess", 2); cClick(fs); });              // the record button is pressed
    at(3800, function () { memEl.classList.add("opened"); });                  // the record comes forward, the sessions stay behind
    at(4500, function () { cCursor(fs, null); });
    [0, 1, 2, 3, 4, 5].forEach(function (i) {                                  // the request is typed: six slices of the input line
      at(5300 + i * 230, function () { cFrame(fr, "type", i); });
    });
    at(7000, function () {                                                     // sent: "Reading the recording…"
      memEl.querySelector(".mtype").classList.add("gone");
      cFrame(fr, "rec", 1);
    });
    at(8400, function () { memReveal(rec[2], 0.34, 2600, 16); });              // the answer with a table and a PDF file
    at(11500, function () { memLivePost("rect"); });                           // the page says where the time in the answer is now
    at(11800, function () {                                                    // the mouse moves to the time in the answer
      var c = (MEM.live && MEM.live.cite) || P.r3.cite;
      cCursor(fr, c[0], c[1]);
    });
    at(12800, function () {                                                    // click: the conversation text opens at that turn
      cClick(fr);
      if (MEM.live && MEM.live.ready) { MEM.live.host.classList.add("on"); memLivePost("cite"); }
      else { rec[2].classList.remove("rv"); cFrame(fr, "rec", 3); }            // the record page did not come up: the ready frame
    });
    at(13300, function () { cCursor(fr, null); });
    at(15500, function () { cCursor(fs, P.s.row[0], P.s.row[1]); });           // the mouse goes to the sessions window, which is behind the record
    at(16400, function () {                                                    // click: the sessions come forward, the record stays behind
      cFrame(fs, "sess", 1); cClick(fs); memEl.classList.remove("opened");
    });
    at(18000, memRun);
  }

  function memInit() {
    if (!memEl) return;
    MEM = { on: false, timers: [], rv: 0 };
    var P = MEMPOS;
    if (P && P.r && P.r.composer) {                                            // where the input line is in the record frame
      var c = P.r.composer;
      memEl.style.setProperty("--tx", c.x + "%"); memEl.style.setProperty("--ty", c.y + "%");
      memEl.style.setProperty("--tw", c.w + "%"); memEl.style.setProperty("--th", c.h + "%");
    }
    if (reduce) {                                                              // no motion: the record is open, the answer is in place
      cFrame(memFrameOf("s"), "sess", 2); cFrame(memFrameOf("r"), "rec", 2); memEl.classList.add("opened");
      return;
    }
    if (!io) { memLiveMount(); memRun(); return; }
    new IntersectionObserver(function (es) {
      if (es[0].isIntersecting) { memLiveMount(); memRun(); } else memStop();
    }, { threshold: 0.25 }).observe(memEl);
  }

  // ----------------------- transitions between pages always start from the top. Requirement:
  // going to the pricing and help pages must scroll to the top, but now we land at the footer.
  // The artifact is shown in a frame of the full page height, and when you navigate, the scroll of
  // the outer page stays where it was: the new page is shorter, so the person is pressed against
  // its footer.
  // `scrollIntoView` scrolls the outer page as well, while `scrollTo` scrolls only the frame
  // itself. A link to the same page does not reload it but smoothly scrolls up.

  (function () {
    var PAGE = "index";
    function toTop() {
      try { document.body.scrollIntoView({ block: "start", behavior: "instant" }); } catch (e) { window.scrollTo({ top: 0, left: 0, behavior: "instant" }); }
    }
    // Smooth return to the top with our own animation: `behavior:"smooth"` stutters in a heavy page
    // (from the very bottom it jumps straight to the middle), and an instant jump is not smooth.
    // We find where the viewport is through IntersectionObserver (the frame may be full height; then
    // the outer page scrolls and the frame's `scrollY` is always 0), move an invisible marker frame
    // by frame and show it with `scrollIntoView`, which moves the outer page too.
    var smoothRun = 0;
    function smoothTop() {
      if (reduce) { toTop(); return; }
      var id = ++smoothRun;
      var root = document.documentElement;
      var own = root.scrollHeight > window.innerHeight + 2;      // the page scrolls by itself (an ordinary site); otherwise the frame is full height and the
                                                                 // outer page has to be moved
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
            probe.style.top = (y + pad) + "px";      // the page's `scroll-padding-top` shifts the marker down: without a correction the first
                                                     // frame jerked upward
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
      // a link to a site page: to the same page, smoothly up; to another page, the same soft style:
      // the content fades out (the header stays), the page changes, and on the new page the content
      // fades in (CSS `page-in`).
      // Requirement: going to the same page scrolls smoothly, but going to another one switches
      // abruptly; everything must be in one style. We do not use the browser's cross-page
      // transitions: they are not available everywhere
      if (!/\.html(#.*)?$/.test(h)) return;
      if (h.indexOf("#") < 0 && h.replace(/\.html$/, "") === PAGE) { e.preventDefault(); smoothTop(); return; }
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

  lang = detect();
  applyCopy();
  tilesInit();
  memInit();

  // ----------------------- iPad: iPadOS 13+ calls itself desktop Safari ("Macintosh"), and
  // /download would give it the Mac image.
  // nginx cannot tell an iPad from a Mac by the browser string (deploy/landing/nginx/default.conf),
  // but the page can: a Mac has no touch screen (`maxTouchPoints` is 0), an iPad has 5. For such a
  // visitor the download buttons lead where a phone goes, to the explanation "open this page on a
  // computer" (/download/other → /?dl=other).
  (function () {
    var ipad = /Mac/.test(navigator.platform || navigator.userAgent || "") && navigator.maxTouchPoints > 1;
    if (!ipad) return;
    [].slice.call(document.querySelectorAll('a[href="/download"], a[href="/download/mac"]')).forEach(function (a) {
      a.setAttribute("href", "/download/other");
    });
  })();

  // ----------------------- the "Download" button brought the person back here with an explanation:
  // there is no file for their system yet (/?dl=soon), or it is a phone or another system the app
  // does not exist for (/?dl=other). The server decides which file to give
  // (deploy/landing/nginx/default.conf).
  (function () {
    var box = document.getElementById("dl-note");
    var m = /[?&]dl=([a-z]+)/.exec(location.search);
    if (!box || !m) return;
    var notes = [].slice.call(box.querySelectorAll("[data-note]"));
    var on = notes.filter(function (s) { return s.getAttribute("data-note") === m[1]; })[0];
    if (!on) return;
    notes.forEach(function (s) { s.hidden = s !== on; });
    box.hidden = false;
    // we clean the address so that a page refresh does not show the same thing again
    try { history.replaceState(null, "", location.pathname + location.hash); } catch (e) { /* works without history too */ }
    var timer = setTimeout(close, 9000);
    function close() { clearTimeout(timer); box.hidden = true; }
    box.addEventListener("click", close);
  })();

  // Start after the first paint of the page: the poster is already in place, and the demo
  // loads under it.
  if (document.readyState === "complete") setTimeout(start, 50);
  else window.addEventListener("load", function () { setTimeout(start, 50); });

})();
