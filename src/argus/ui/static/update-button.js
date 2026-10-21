/* The "Update" button is bright, at the top, on any page of the app.

   It appears only when the running app has learned about a new release (`update_service.py`,
   `GET /update/state`); until there is a release, it does not exist. Requirement (2026-10-13):
   bright, somewhere at the top, always present on any page, which is why it is one script for
   all pages and not a button in one of them.

   Where it stands: in the window strip (`winbar.js`) on the right, before the window buttons
   (on Mac, at the right edge); on a page without the strip (a browser tab, settings as a
   separate page) it floats in the top right corner.

   What it does on a press matches what it says: "Update" installs the release right
   there (download, verify, close the windows, and the swap will start the new version);
   "Download", when it cannot be installed from here (launched from Downloads, no
   rights to the folder), opens the download page in an ordinary browser. If recognition is
   running, the first press only asks "Recording in progress. Update?",
   and the second (within a few seconds) agrees.

   The color is light blue (the same as the app's indicators): chosen from three variants on
   2026-10-13.

   It shows nothing technical: no versions, no failure reasons (they are in the app log). */

(function () {
  "use strict";

  if (window.top !== window) return;                 // framed (the demo on the landing page, the settings window over a page)
  if (document.body && document.body.hasAttribute("data-solo")) return;      // detached windows get none

  const css = document.createElement("style");
  css.textContent = `
  /* The page's general button rules (button:hover and the like) would recolor it gray under the cursor, so
     the colors and the border are pinned with important. */
  .upd { --upd-bg:#4fb8d8; --upd-fg:#05212b; --upd-glow:rgba(79,184,216,.38); --p:0%;
    position:relative; flex:none; align-self:center; margin:0 12px; height:26px; box-sizing:border-box;
    display:inline-flex; align-items:center; gap:7px; padding:0 14px 0 10px; border:0; border-radius:13px;
    background:var(--upd-bg) !important; color:var(--upd-fg) !important; border:0 !important;
    font-weight:600; font-size:13px; line-height:1; font-family:inherit; letter-spacing:0;
    white-space:nowrap; cursor:default; overflow:hidden;
    box-shadow:0 0 0 1px rgba(255,255,255,.4), 0 2px 12px var(--upd-glow);
    transition:filter .12s, transform .12s; }
  .upd svg { width:14px; height:14px; flex:none; fill:none; stroke:currentColor; stroke-width:2.2;
    stroke-linecap:round; stroke-linejoin:round; }
  .upd:hover:not(:disabled) { filter:brightness(1.08); }
  .upd:active:not(:disabled) { transform:translateY(1px); filter:brightness(.94); }
  .upd:focus-visible { outline:2px solid rgba(242,244,247,.8); outline-offset:2px; }
  /* The general button rule dims a disabled button to 45%, but this one must stay bright while downloading. */
  .upd:disabled { cursor:default; opacity:1 !important; }
  /* Downloading: a thin progress strip along the bottom edge; we do not print the number. */
  .upd::after { content:""; position:absolute; left:0; bottom:0; height:2px; width:var(--p); background:currentColor;
    opacity:0; transition:width .3s ease, opacity .2s; }
  .upd[data-phase="downloading"]::after { opacity:.55; }
  .upd[data-phase="installing"] svg { animation:upd-spin 1s linear infinite; }
  @keyframes upd-spin { to { transform:rotate(360deg); } }
  .upd[data-phase="failed"], .upd[data-ask] { --upd-bg:#ffd166; --upd-glow:rgba(255,209,102,.3); }
  /* There is no window strip, so the button hangs in the corner of the page. */
  .upd.floating { position:fixed; top:8px; right:12px; margin:0; z-index:2000; }
  @media (prefers-reduced-motion:reduce) { .upd[data-phase="installing"] svg { animation:none; } }
  `;
  document.head.appendChild(css);

  const ARROW = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11"/><path d="M7 11l5 5 5-5"/><path d="M5 20h14"/></svg>';
  const SPIN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4a8 8 0 1 1-8 8"/></svg>';

  let btn = null;
  let state = null;
  let timer = 0;
  let askUntil = 0;         // until when we wait for the second press, "update despite the recording"
  let retried = false;      // after a failure we already tried installing once more: from here on, only the download page

  const post = (path, extra) =>
    fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(Object.assign({}, extra)) })
      .then((r) => r.json())
      .catch(() => null);

  function label(s) {
    if (s.phase === "downloading") return ["Downloading…", ARROW];
    if (s.phase === "installing") return ["Restarting…", SPIN];
    // After a failure, the first press repeats the attempt right here (`act`), and only if the
    // retry also fails, or installing from here is impossible altogether, a press opens the
    // download page: the label says what the press will do.
    if (s.phase === "failed") return [s.can_install && !retried ? "Failed — try again" : "Failed — download", ARROW];
    if (!s.can_install) return ["Download", ARROW];
    if (Date.now() < askUntil) return ["Recording in progress. Update?", ARROW];
    return ["Update", ARROW];
  }

  function place() {
    if (!btn) return;
    const bar = document.querySelector(".winbar");
    if (bar) {
      btn.classList.remove("floating");
      if (btn.parentNode !== bar) bar.insertBefore(btn, bar.querySelector(".winbar-btns"));   // no such block: at the end (Mac)
    } else {
      btn.classList.add("floating");
      if (btn.parentNode !== document.body) document.body.appendChild(btn);
    }
  }

  function render() {
    if (!state || !state.available) {
      if (btn) { btn.remove(); btn = null; }
      return;
    }
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "upd";
      btn.addEventListener("click", onClick);
    }
    const [text, icon] = label(state);
    const key = text + "|" + state.phase;
    if (btn.dataset.key !== key) {
      btn.dataset.key = key;
      btn.innerHTML = icon + "<span>" + text + "</span>";
    }
    btn.dataset.phase = state.phase;
    if (state.phase === "idle") retried = false;
    btn.toggleAttribute("data-ask", Date.now() < askUntil && state.phase === "idle");
    btn.disabled = state.phase === "downloading" || state.phase === "installing";
    btn.style.setProperty("--p", (state.percent || 0) + "%");
    place();
  }

  function schedule() {
    clearTimeout(timer);
    const busy = state && (state.phase === "downloading" || state.phase === "installing");
    timer = setTimeout(refresh, busy ? 700 : 60000);
  }

  function refresh() {
    return fetch("/update/state", { cache: "no-store" })
      .then((r) => r.json())
      .then((s) => { state = s; render(); })
      .catch(() => { /* the server is restarting or unavailable, we will ask next time */ })
      .then(schedule);
  }

  function onClick() {
    if (!state || btn.disabled) return;
    // The "recording is running" flag can be more than a minute old (polling once every 60 s):
    // before deciding, we ask again.
    refresh().then(act);
  }

  function ask() {
    askUntil = Date.now() + 6000;
    render();
    setTimeout(render, 6100);
  }

  function act() {
    if (!state || !btn || btn.disabled) return;
    if (!state.can_install) {
      post("/update/open");
      return;
    }
    // After a failure (the network blinked, the hash did not match) the first press tries to
    // install again: the server resets the failure by itself. If it fails the second time, the
    // download page opens.
    const wasFailed = state.phase === "failed";
    if (wasFailed && retried) {
      post("/update/open");
      return;
    }
    if (state.busy && Date.now() >= askUntil) {
      ask();
      return;
    }
    const confirmed = !!state.busy;        // a second press within a few seconds means agreement to interrupt the recording
    askUntil = 0;
    post("/update/install", { confirm: confirmed }).then((s) => {
      if (s && s.phase) state = Object.assign({}, state, s);
      if (s && s.confirm) {                // the server itself saw a recording that the page did not yet know about
        ask();
        return;
      }
      if (wasFailed && s && s.ok) retried = true;
      render();
      clearTimeout(timer);
      timer = setTimeout(refresh, 400);
    });
  }

  // The first query is made when the page is already drawn (the window strip is built right
  // after the page is parsed), then once a minute and on returning to the window.
  setTimeout(refresh, 1200);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
