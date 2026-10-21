/* ============================================================
   /design/panel.js - the runtime of the four surfaces.

   EDITED ONLY IN master (package argus/design/).
   There is not a single channel word here: what to show is decided by the branch's page.
   Here is what would otherwise be written three times and three times differently:

     • a reconnecting websocket (a copy of connect() from index.html:534);
     • the setHTML/setText differ, learned the hard way in index.html:315: a full
       redraw twice a second killed text selection and closed the developer tools;
     • escaping (index.html:334): each branch having its own means its own hole
       in each branch;
     • recomputing line age and the data-stale steps from --age-*-s;
     • the countdown on .key[data-armed];
     • delegation by [data-act]: per-element binding has already been lost
       entirely (see the comment at index.html:467);
     • hotkeys by the content of kbd.key__hot;
     • Escape and a click on the backdrop on .overlay;
     • theme memory.

   One timer for the whole page, exactly 1 Hz. More often would mean jerking the layout
   in the very minute the processor is busy with recognition.
   ============================================================ */

(function () {
  "use strict";

  // ---------------------------------------------------------------- differ
  //
  // Change the markup only if it really changed. The screen is silent most of the time, so
  // an equality check removes almost all redraws, and all the pain from them with them.
  const lastHTML = new WeakMap();

  function setHTML(el, html) {
    if (!el) return false;
    if (lastHTML.get(el) === html) return false;
    lastHTML.set(el, html);
    el.innerHTML = html;
    return true;
  }

  function setText(el, text) {
    if (!el) return false;
    const s = String(text ?? "");
    if (el.textContent === s) return false;
    el.textContent = s;
    return true;
  }

  const esc = (x) =>
    String(x ?? "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const ts = (s) => {
    const n = Math.max(0, Math.floor(Number(s) || 0));
    return `${String(Math.floor(n / 60)).padStart(2, "0")}:${String(n % 60).padStart(2, "0")}`;
  };

  // ------------------------------------------------------------------ connection
  let ws = null;
  let socketOpen = false;
  const listeners = [];
  let last = null;

  function connect(host) {
    ws = new WebSocket(`ws://${host}/ws`);
    ws.onopen = () => { socketOpen = true; };
    ws.onmessage = (m) => {
      let data;
      try {
        data = JSON.parse(m.data);
      } catch {
        return;  // garbage in a frame must not bring down the whole page
      }
      last = data;
      for (const fn of listeners) {
        try {
          fn(data);
        } catch (e) {
          // One failed render must not disable the others and break the connection.
          console.error("panel: the snapshot handler failed", e);
        }
      }
    };
    // Reconnect after a second: the server is restarted by hand more often than it seems, and
    // the page must come back to life by itself.
    ws.onclose = () => { socketOpen = false; setTimeout(() => connect(host), 1000); };
  }

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  const control = (patch) => post("/control", patch);

  // ------------------------------------------------------- actions and hotkeys
  //
  // One handler for the whole document. Per-element binding has already been lost entirely
  // when neighboring code was edited: a class on a button changed, and it looked like "the
  // buttons do not press", although the handler simply did not exist.
  const acts = new Map();

  function act(name, fn) {
    acts.set(name, fn);
  }

  function fire(name, el, event) {
    const fn = acts.get(name);
    if (!fn) return;
    try {
      fn(el, event);
    } catch (e) {
      console.error(`panel: the action "${name}" failed`, e);
    }
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    fire(el.dataset.act, el, e);
  });

  // The hotkey is printed on the button itself; that is the cheat sheet. So it must also be
  // read from there: nothing can drift apart.
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;

    if (e.key === "Escape") {
      const top = [...document.querySelectorAll(".overlay:not([hidden])")].pop();
      if (top) { top.hidden = true; return; }
    }
    if (e.ctrlKey || e.altKey || e.metaKey) return;

    const key = e.key.toUpperCase();
    for (const btn of document.querySelectorAll(".key[data-act]")) {
      const hot = btn.querySelector("kbd.key__hot");
      if (!hot || btn.disabled) continue;
      if (hot.textContent.trim().toUpperCase() !== key) continue;
      if (btn.offsetParent === null) continue;   // hidden by the phase: there is no key
      e.preventDefault();
      fire(btn.dataset.act, btn, e);
      return;
    }
  });

  // A click on the backdrop closes, as in the instrument.
  document.addEventListener("click", (e) => {
    if (e.target.classList && e.target.classList.contains("overlay")) e.target.hidden = true;
  });

  // ------------------------------------------------------------ time and tick
  //
  // Line age and the gag countdown tick by themselves. The branch's page does not know about
  // this and must not: its job is to set data-t and data-armed.
  function num(el, name, fallback) {
    const raw = getComputedStyle(el).getPropertyValue(name).trim();
    const v = parseFloat(raw);
    return Number.isFinite(v) ? v : fallback;
  }

  function ageText(sec) {
    if (sec < 1) return "just now";
    if (sec < 60) return `${Math.floor(sec)} s`;
    if (sec < 3600) return `${Math.floor(sec / 60)} min`;
    return `${Math.floor(sec / 3600)} h`;
  }

  function tickAges(now) {
    for (const el of document.querySelectorAll("[data-t]")) {
      const t = parseFloat(el.dataset.t);
      if (!Number.isFinite(t)) continue;

      // One attribute has two meanings, and they can be told apart for certain: unix time is
      // over a billion seconds, and an offset inside a recording never lives that long.
      if (t < 1e9) { setText(el, ts(t)); continue; }

      const age = Math.max(0, now - t);
      setText(el, ageText(age));

      const stale = num(el, "--age-stale-s", 20);
      const trap = num(el, "--age-trap-s", 40);
      const step = age >= trap ? "trap" : age >= stale ? "aging" : "fresh";
      const host = el.closest("[data-stale]") || el;
      if (host.getAttribute("data-stale") !== step) host.setAttribute("data-stale", step);
    }
  }

  function tickArmed() {
    for (const el of document.querySelectorAll(".key[data-armed]")) {
      const left = Math.max(0, Math.round(parseFloat(el.dataset.armed) || 0) - 1);
      const count = el.querySelector(".key__count");
      if (left <= 0) {
        el.removeAttribute("data-armed");
        if (count) setText(count, "");
        continue;
      }
      el.dataset.armed = String(left);
      if (count) setText(count, ts(left));
    }
  }

  setInterval(() => {
    const now = Date.now() / 1000;
    tickAges(now);
    tickArmed();
  }, 1000);

  // ------------------------------------------------------------------- theme
  //
  // A live surface nails data-theme right in the HTML; then we touch nothing: a white
  // rectangle next to a dark game blinds, and the system theme does not vote here.
  const root = document.documentElement;
  const themeIsFixed = root.hasAttribute("data-theme");
  if (!themeIsFixed) {
    let saved = null;
    try { saved = localStorage.getItem("argus.theme"); } catch { /* private mode */ }
    if (saved === "dark" || saved === "light") root.setAttribute("data-theme", saved);
  }

  function theme(next) {
    if (themeIsFixed) return root.getAttribute("data-theme");
    const now = root.getAttribute("data-theme");
    const value = next || (now === "dark" ? "light" : "dark");
    root.setAttribute("data-theme", value);
    try { localStorage.setItem("argus.theme", value); } catch { /* private mode */ }
    return value;
  }

  // ------------------------------------------------------------------ start
  function start(options) {
    const opts = options || {};
    window.Argus.channel = opts.channel || "";
    connect(opts.host || location.host);
    return window.Argus;
  }

  window.Argus = {
    start,
    on: (fn) => { listeners.push(fn); if (last) fn(last); return fn; },
    act,
    fire: (name) => fire(name, null, null),
    control,
    post,
    setText,
    setHTML,
    esc,
    ts,
    ageText,
    theme,
    get state() { return last; },
    get online() { return socketOpen; },
    channel: "",
  };
})();
