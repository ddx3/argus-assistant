/* The top block of the app's main window: the whole page header and the window buttons in
   one row.

   The Windows system strip has been removed (`desktop.use_page_caption`): the system does not
   allow changing its height and buttons. This one is drawn by the page, and the window is
   controlled by Python through `/window/main` (`desktop.main_window_action`). Design history
   (2026-10-07): first a taller strip was requested, then it was judged too big ("like VS
   Code", 35 px), then the header buttons were put into a strip and the logo was removed, and
   finally everything went into the top block: back, settings and the combined window,
   "RECOGNITION", sources, levels and "Work with the recording"
   move here from the page header.

   It is drawn only in the app's main window: the address the app opened it with carries
   `desk=1`, the mark is stored in `sessionStorage` and survives navigation between the pages
   of the window. In a browser tab, in the demo on the landing page (it lies in an iframe) and
   in detached windows there is no mark, and the script does nothing. There is no icon or title
   in the strip. */

(function () {
  "use strict";

  if (window.top !== window) return;
  let desk = false;
  try {
    if (/[?&]desk=1\b/.test(location.search)) sessionStorage.setItem("argus-desk", "1");
    desk = sessionStorage.getItem("argus-desk") === "1";
  } catch (e) {
    desk = /[?&]desk=1\b/.test(location.search);
  }
  if (!document.body || document.body.hasAttribute("data-solo")) return;

  // The `desk=1` mark is an agreement with the app: at launch it removes the system strip and
  // asks the page to draw its own. But a launch might remove the strip without passing the mark
  // (2026-10-08: the window was left with no buttons at all, twice at startup, and it did not
  // repeat on restarts), and the mark can also be lost on navigation. So without the mark the
  // page checks by itself, only in the app window (a browser has no `chrome.webview`): if the
  // top of the content is at the very top of the window, there is no system strip above it, and
  // we must draw our own. With a strip, the content is lower than the top of the window by at
  // least its height (31 px or more); without it, by a couple of pixels of border.
  if (!desk) {
    const inApp = !!(window.chrome && window.chrome.webview);
    if (!inApp) return;
    fetch("/window/main", { method: "POST", headers: { "content-type": "application/json" },
                            body: JSON.stringify({ action: "state" }) })
      .then((r) => r.json())
      .then((st) => {
        if (!st || !st.ok || !Array.isArray(st.rect)) return;
        const dpr = window.devicePixelRatio || 1;
        const gap = window.screenY * dpr - st.rect[1];       // physical pixels from the top of the window to the top of the content
        if (gap < 19 * dpr) {
          try { sessionStorage.setItem("argus-desk", "1"); } catch (e) { /* no storage, we will check on the next load */ }
          build();
        }
      })
      .catch(() => {});
    return;
  }
  build();

  function build() {
  const BAR_H = 38;   // px; the system one is 31, VS Code's is 35, but our row holds the whole header. Without the
                      // header, 40 was judged too big,
                      // 35 with the header was judged "small": 38 is slightly more, and everything is larger by 1 px
                      // of text and 2 px of icons
  const BTN_W = 48;
  // The app window on macOS: the same strip and the same contents, but the window buttons are
  // the "traffic lights" on the left, not three squares on the right (that is how everything is
  // arranged on Mac). The app hides the system buttons (`mac/desktop.py: use_page_caption`) and
  // draws these. `navigator.platform` in WKWebView is always "MacIntel", on Apple Silicon too.
  const MAC = /Mac/i.test(navigator.platform || "");

  const css = document.createElement("style");
  css.textContent = `
  html.desk { --winbar:${BAR_H}px; }
  .winbar { position:sticky; top:0; z-index:1000; flex:none; height:${BAR_H}px; box-sizing:border-box;
    display:flex; align-items:stretch; justify-content:space-between;
    border-bottom:1px solid var(--slab-edge, rgba(255,255,255,.09));
    color:var(--dim, #9aa1ad); user-select:none; -webkit-user-select:none;
    /* The same recipe as the page header (header in chrome.css): the color is the same to the pixel, the strip
       and the header read as one block, and from the top to the content there are two tones, not three. */
    background:var(--slab, rgba(255,255,255,.03));
    backdrop-filter:blur(var(--glass-blur, 32px)) saturate(var(--glass-sat, 1.6));
    -webkit-backdrop-filter:blur(var(--glass-blur, 32px)) saturate(var(--glass-sat, 1.6)); }
  /* The header (if anything is left in it, for example the sign-in plate) sticks below the strip, not at
     the very top of the window: otherwise, when scrolling, its top pixels would go under the strip. */
  html.desk header { top:var(--winbar); }

  /* On the left are the page's icon buttons, then everything else from the header in the same order; the
     free space between them drags the window too. */
  .winbar-icons { display:flex; align-items:stretch; flex:none; padding-left:0; }  /* flush with the left edge, like the window buttons on the right (there used to be a gap on the left, none on the right) */
  .winbar-main { flex:1; min-width:0; display:flex; align-items:center; gap:8px; padding:0 8px 0 6px;
    overflow:hidden; cursor:default; }
  .winbar-main > .grow { flex:1; }
  .winbar-btns { display:flex; flex:none; }

  /* "Visible in recordings": the window could not be hidden from screen recording (see below). A red mark between
     the strip's content and the window buttons; it does not shrink: when space is tight, the content gives way, not the mark. */
  .winbar-alert { flex:none; align-self:center; margin:0 10px; padding:3px 10px; border-radius:6px;
    background:rgba(240,113,107,.16); color:var(--bad, #f0716b); font-size:12.5px; line-height:1.3; white-space:nowrap; }
  .winbar-alert[hidden] { display:none; }

  /* Window buttons. */
  .winbar .wb { width:${BTN_W}px; height:100%; padding:0; border:0; border-radius:0; background:transparent;
    color:inherit; display:grid; place-items:center; cursor:default; transition:background .12s, color .12s; }
  .winbar .wb svg { width:12px; height:12px; overflow:visible; fill:none; stroke:currentColor; stroke-width:1; stroke-linecap:butt; }
  .winbar .wb:hover { background:var(--btn-hover, rgba(255,255,255,.09)); color:var(--text, #f2f4f7); }
  .winbar .wb:active { background:rgba(255,255,255,.14); }
  .winbar .wb.close:hover { background:#c42b1c; color:#fff; }
  .winbar .wb:focus-visible { outline:2px solid rgba(242,244,247,.7); outline-offset:-3px; }

  /* macOS: the traffic lights are on the left, in the top-left corner of the window, like in any Mac program. Circles of 12 px
     with 8 px between them, 14 from the edge: that is where the system ones sit, and the hand finds them in the same place.
     The colors and the icons on hover are the system ones; a window that is not in focus is gray, and the color returns
     under the mouse. There are no window buttons on the right at all. */
  .winbar-lights { display:flex; align-items:center; gap:8px; flex:none; padding:0 10px 0 14px; }
  .winbar .tl { width:12px; height:12px; padding:0; margin:0; border:0; border-radius:50%; display:grid;
    place-items:center; cursor:default; color:transparent; background:#6b6f76;
    box-shadow:inset 0 0 0 .5px rgba(0,0,0,.3); transition:filter .12s; }
  .winbar .tl.close { background:#ff5f57; }
  .winbar .tl.min { background:#febc2e; }
  .winbar .tl.zoom { background:#28c840; }
  .winbar .tl svg { width:8px; height:8px; overflow:visible; fill:none; stroke:currentColor; stroke-width:1.3;
    stroke-linecap:round; stroke-linejoin:round; opacity:0; transition:opacity .08s; }
  .winbar .tl.zoom svg { fill:currentColor; stroke:none; }
  .winbar .tl.close svg { color:#4d0000; }
  .winbar .tl.min svg { color:#985600; }
  .winbar .tl.zoom svg { color:#0a5d0a; }
  .winbar-lights:hover .tl svg { opacity:1; }
  .winbar .tl:active { filter:brightness(.78); }
  .winbar .tl:focus-visible { outline:2px solid rgba(242,244,247,.7); outline-offset:2px; }
  .winbar.idle .tl { background:rgba(255,255,255,.2); box-shadow:inset 0 0 0 .5px rgba(255,255,255,.12); }
  .winbar.idle .winbar-lights:hover .tl.close { background:#ff5f57; }
  .winbar.idle .winbar-lights:hover .tl.min { background:#febc2e; }
  .winbar.idle .winbar-lights:hover .tl.zoom { background:#28c840; }
  /* In "full screen" mode the menu bar slides in from the top and covers the header: our circles cannot be clicked there.
     For this mode the app brings back the system buttons (mac/desktop.py, _watch_full_screen), and ours hide. */
  .winbar.fs .winbar-lights { display:none; }

  /* The page's icon buttons (back, settings, overlay window): no border and no plate, a fill only
     under the mouse. These are the same nodes that were in the header (chrome.js and the page find them by id). */
  .winbar-icons > .iconbtn { width:44px; height:100%; margin:0; padding:0; border:0; border-radius:0;
    background:transparent; display:grid; place-items:center; text-decoration:none; }
  .winbar-icons > .iconbtn[hidden] { display:none; }
  .winbar-icons > .iconbtn svg { width:18px; height:18px; }
  .winbar-icons > .iconbtn:hover:not(:disabled) { background:var(--btn-hover, rgba(255,255,255,.09)); }

  /* RECOGNITION, the sources, the levels and "Work with the recording" are fitted to the strip, not just
     inserted: they are items of the row at its full height, like the icons on the left and the window buttons on the right,
     with no border and no plate, a fill only under the mouse (there are no dividers between groups). The state
     is carried by the same marks as in the header: the lamp by "Record" (the label does not change), the colored square
     with a check mark by a source. */
  .winbar-main { gap:0; align-items:stretch; }
  /* One font for the whole top block (the font is the same everywhere): the same as the page's, 12 px,
     regular, with no letter spacing. "Record" used to be bold with letter spacing, and the buttons had the system Arial. */
  .winbar-main, .winbar-main * { font-family:inherit; font-size:13px; font-weight:400; letter-spacing:0; }
  .winbar-main .power .label { font-weight:400; letter-spacing:0; }
  .winbar-main .src .box { font-size:11px; }
  .winbar-main .power, .winbar-main .src, .winbar-main .cta {
    height:auto; align-self:stretch; margin:0; border:0; border-radius:0; background:transparent;
    flex:none; white-space:nowrap; transition:background .12s, color .12s; }
  .winbar-main .power { padding:0 16px; gap:10px; }
  .winbar-main .power .dot { width:10px; height:10px; }
  .winbar-main .sources { gap:0; align-self:stretch; flex:none; }
  .winbar-main .src { padding:0 13px; gap:8px; }
  .winbar-main .src .box { width:16px; height:16px; font-size:11px; }
  .winbar-main .power:hover, .winbar-main .src:hover, .winbar-main .cta:hover:not(:disabled) {
    background:var(--btn-hover, rgba(255,255,255,.09)); border:0; }
  /* "Record" and the sources have no outline at all: pressing the check mark inside must not outline the item
     (focus-within did). Keyboard focus is visible only on the link. */
  .winbar-main .src:focus-within, .winbar-main .src select:focus { outline:0; }
  .winbar-main .cta:focus-visible { outline:2px solid rgba(242,244,247,.7); outline-offset:-3px; }
  /* The level bars sit under the words "Microphone" and "System audio", along the bottom edge of the item: these are the
     same bars that used to be a separate block (they are moved by the ids lvl-mic and lvl-loopback). */
  .winbar-main .src { position:relative; }
  .winbar-main .src > .track { position:absolute; left:13px; right:13px; bottom:4px; width:auto; height:3px;
    border-radius:2px; background:rgba(255,255,255,.07); pointer-events:none; }
  .winbar-main .meters { display:none; }
  .winbar-main .cta { padding:0 16px; gap:8px; }
  .winbar-main .cta svg { width:16px; height:16px; }
  /* A window no wider than 880 px (the app's minimum) fits everything; this is a reserve in case there are more items. */
  @media (max-width:1000px) { .winbar-main .cta { font-size:0; gap:0; padding:0 12px; } }
  `;
  document.head.appendChild(css);
  document.documentElement.classList.add("desk");

  const svg = (inner) => `<svg viewBox="0 0 10 10" aria-hidden="true">${inner}</svg>`;
  // Icons like Windows: 10 by 10, a one-pixel line on the grid (edges on ".5"), not going beyond
  // the frame. Before, "restore down" (two squares) was flush against the edge and got clipped:
  // it looked crooked.
  const ICON = {
    minimize: svg('<path d="M0 5.5h10"/>'),
    maximize: svg('<rect x="0.5" y="0.5" width="9" height="9" rx="1"/>'),
    restore: svg('<rect x="0.5" y="2.5" width="7" height="7" rx="1"/><path d="M2.5 2.5V1.5a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-1"/>'),
    close: svg('<path d="M0 0l10 10M10 0L0 10"/>'),
  };

  // The traffic-light icons are visible only under the mouse: a cross, a dash and two
  // "maximize" arrows (12 by 12, drawn at 8 px). Maximize is the same action as the middle one
  // of the three Windows squares (`toggle`).
  const LIGHT = {
    close: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 2l8 8M10 2L2 10"/></svg>',
    min: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M1.5 6h9"/></svg>',
    zoom: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M10.5 1.5v6l-6-6zM1.5 10.5v-6l6 6z"/></svg>',
  };

  const bar = document.createElement("div");
  bar.className = "winbar";
  bar.innerHTML = MAC
    ? '<div class="winbar-lights" role="group" aria-label="Window">' +
      `<button class="tl close" type="button" data-act="close" aria-label="Close">${LIGHT.close}</button>` +
      `<button class="tl min" type="button" data-act="minimize" aria-label="Minimize">${LIGHT.min}</button>` +
      `<button class="tl zoom" type="button" data-act="fullscreen" aria-label="Full screen">${LIGHT.zoom}</button>` +
      "</div>" +
      '<div class="winbar-icons"></div>' +
      '<div class="winbar-main"></div>'
    : '<div class="winbar-icons"></div>' +
      '<div class="winbar-main"></div>' +
      '<div class="winbar-btns">' +
      `<button class="wb" type="button" data-act="minimize" aria-label="Minimize">${ICON.minimize}</button>` +
      `<button class="wb" type="button" data-act="toggle" aria-label="Maximize">${ICON.maximize}</button>` +
      `<button class="wb close" type="button" data-act="close" aria-label="Close">${ICON.close}</button>` +
      "</div>";
  document.body.prepend(bar);
  if (MAC) {
    document.documentElement.classList.add("mac");
    // Full screen (macOS): our circles are hidden in it, but the `fs` class is set by the app's
    // reply about the window state, which arrives after the first frame. On every new page (back,
    // settings, "Work with the recording") the circles flashed for a fraction
    // of a second by the gear (reported 2026-10-13). So the state is known at once: remembered
    // from the previous page (`argus-fs`) and guessed by size: in full screen the content fills
    // the whole screen, while a maximized window lacks the menu bar row.
    let known = false;
    try { known = sessionStorage.getItem("argus-fs") === "1"; } catch (e) { /* no storage, only by size */ }
    if (known || (window.innerWidth >= screen.width - 1 && window.innerHeight >= screen.height - 1)) bar.classList.add("fs");
  }

  // Everything from the header row (`header > .bar`) moves into the strip in the old order: the
  // icons (back, settings, combined window) to the left, the rest after them. The same nodes,
  // not copies: the handlers on them stay, and pages find them by id. The emptied row and its
  // line are removed: the strip has its own.
  const icons = bar.querySelector(".winbar-icons");
  const main = bar.querySelector(".winbar-main");
  document.querySelectorAll("body > header > .bar").forEach((row) => {
    Array.from(row.children).forEach((el) => {
      (el.classList.contains("iconbtn") ? icons : main).appendChild(el);
    });
    row.style.display = "none";
    row.parentElement.style.borderBottom = "0";
    row.parentElement.style.boxShadow = "none";
  });

  // The level bars move under the words "Microphone" and "System audio";
  // the empty "levels" block is hidden.
  [["mic", "lvl-mic"], ["loopback", "lvl-loopback"]].forEach(([key, fillId]) => {
    const fill = document.getElementById(fillId);
    const src = main.querySelector(`.src[data-k="${key}"]`);
    if (fill && src) src.appendChild(fill.parentElement);
  });

  // "Visible in recordings": if at startup the window could not be hidden
  // from screen capture, the app appends `capture=fail` to the address (`desktop.py`) and says
  // so in a message box, but the message box gets closed while the person keeps sharing the
  // screen. The mark in the strip stays lit while that is true: it is stored in `sessionStorage`,
  // like `desk` (it survives navigation between the pages of the window), and goes out when the
  // window is actually hidden (`GET /window/capture`) or when the user successfully toggled
  // "Invisible in recordings" (the `argus-capture` event from admin.js;
  // a failed attempt to hide the window lights it again).
  const CAP_KEY = "argus-capture-fail";
  let capFail = /[?&]capture=fail\b/.test(location.search);
  try {
    if (capFail) sessionStorage.setItem(CAP_KEY, "1");
    capFail = sessionStorage.getItem(CAP_KEY) === "1";
  } catch (e) { /* no storage, going by the address of this page */ }
  const alarm = document.createElement("div");
  alarm.className = "winbar-alert";
  alarm.setAttribute("role", "alert");
  alarm.textContent = "Visible in recordings";
  alarm.hidden = !capFail;
  const setAlarm = (on) => {
    alarm.hidden = !on;
    try { if (on) sessionStorage.setItem(CAP_KEY, "1"); else sessionStorage.removeItem(CAP_KEY); } catch (e) { /* not a problem */ }
  };
  main.after(alarm);      // between the strip's content and the window buttons (on Mac, rightmost of all)
  if (capFail) {
    fetch("/window/capture").then((r) => r.json())
      .then((st) => { if (st && st.excluded === true) setAlarm(false); })
      .catch(() => {});
  }
  window.addEventListener("argus-capture", (e) => {
    const d = e.detail || {};
    setAlarm(d.wanted === true && d.ok === false);
  });

  const toggle = bar.querySelector('[data-act="toggle"], [data-act="fullscreen"]');   // on Mac the green circle means "full screen"

  const call = (action) =>
    fetch("/window/main", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action }),
    })
      .then((r) => r.json())
      .catch(() => null);

  const paint = (st) => {
    if (!st || !st.ok) return;
    // Full-screen mode (macOS only): there are no system buttons in it, and one can leave only
    // with our green circle; minimizing is not possible.
    if (typeof st.fullscreen === "boolean") {
      bar.classList.toggle("fs", st.fullscreen);
      try { sessionStorage.setItem("argus-fs", st.fullscreen ? "1" : "0"); } catch (e) { /* not a problem: we will find out again */ }
    }
    if (typeof st.maximized !== "boolean") return;
    if (MAC) return;     // the green circle means "full screen"; in the mode itself it is absent (hidden), so there is
                         // a single icon
    const label = st.maximized ? "Restore" : "Maximize";
    toggle.innerHTML = st.maximized ? ICON.restore : ICON.maximize;
    toggle.setAttribute("aria-label", label);
  };

  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".winbar-btns button[data-act], .winbar-lights button[data-act]");
    if (!btn) return;
    // Like the system green circle: with Option it does not go to full screen but maximizes the
    // window.
    const act = btn.dataset.act === "fullscreen" && e.altKey ? "toggle" : btn.dataset.act;
    call(act).then(paint);
  });

  // A window that is not in focus gets a gray traffic light, like the system buttons (macOS
  // only). We take the state from the page: WebKit sends it `blur` and `focus` when the window
  // loses and gets the keyboard. It starts colored: `document.hasFocus()` may lie at startup
  // until the web view becomes the first responder, and a gray traffic light in a live window
  // would look like a breakage. It turns gray only on a real `blur`; `hasFocus()` filters out
  // focus moving into the frame of the page.
  if (MAC) {
    const idle = () => bar.classList.toggle("idle", !document.hasFocus());
    window.addEventListener("focus", idle);
    window.addEventListener("blur", idle);
  }

  // We drag by the free space of the strip: everything that is not a button, a pill or a link.
  // Python moves the window by the cursor position taken from the system; the page only says
  // "started" and "step", and no more often than the server answers.
  const live = "a, button, label, select, input, textarea, .power, .src, .cta";
  // The margins around the traffic-light circles also drag the window (the buttons are excluded
  // by the `live` selector), as with the system one.
  const free = (e) => !e.target.closest(live) && !e.target.closest(".winbar-btns");
  let dragging = false;
  let busy = false;
  let again = false;
  const step = () => {
    busy = true;
    call("drag_move").then(() => {
      busy = false;
      if (again && dragging) { again = false; step(); }
    });
  };
  const stop = () => { dragging = false; again = false; };
  bar.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !free(e)) return;
    dragging = true;
    try { bar.setPointerCapture(e.pointerId); } catch (err) { /* no capture: we drag while the mouse is over the strip */ }
    call("drag_start").then(paint);
  });
  bar.addEventListener("pointermove", () => {
    if (!dragging) return;
    if (busy) again = true; else step();
  });
  bar.addEventListener("pointerup", stop);
  bar.addEventListener("pointercancel", stop);
  bar.addEventListener("lostpointercapture", stop);
  bar.addEventListener("dblclick", (e) => { if (free(e)) call("toggle").then(paint); });

  // The "maximize / restore" icon follows the real state of the window: it is changed both by
  // the Windows keys (Win+arrows) and by the strip itself.
  let timer = 0;
  const sync = () => call("state").then(paint);
  window.addEventListener("resize", () => { clearTimeout(timer); timer = setTimeout(sync, 150); });
  sync();
  }
})();
