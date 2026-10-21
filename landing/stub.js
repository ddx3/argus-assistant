/* A stand-in server for the demo: the real Argus dashboard page reads state snapshots over a
   websocket, and here a scenario replaces the websocket. The page takes its own markup and
   styles from the app, unchanged. */
(function () {
  "use strict";

  // ---- images: the screen frame and the frames in the feed -------------------------------
  const SCREENS = window.__ARGUS_SCREENS__ || {};
  const mapSrc = (u) => {
    if (typeof u !== "string") return u;
    if (u.indexOf("/screen.mjpg") === 0) return SCREENS.idle || u;
    const m = u.match(/\/shot\/(\d+)\.jpg/);
    if (m) return SCREENS[m[1]] || SCREENS.idle || u;
    return u;
  };
  const d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
  Object.defineProperty(HTMLImageElement.prototype, "src", {
    get() { return d.get.call(this); },
    set(v) { d.set.call(this, mapSrc(v)); },
  });
  // Frames in the feed arrive as a markup string (`innerHTML`), and the browser starts loading
  // the address before the observer below has time to replace it: the request went to a server
  // that does not exist. So we replace the address in the string itself, before parsing.
  const rewrite = (html) => typeof html !== "string" ? html
    : html.replace(/(src|data-src)="(\/shot\/\d+\.jpg|\/screen\.mjpg[^"]*)"/g, (m, attr, u) => attr + '="' + mapSrc(u) + '"');
  const ih = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  Object.defineProperty(Element.prototype, "innerHTML", {
    get() { return ih.get.call(this); },
    set(v) { ih.set.call(this, rewrite(v)); },
  });
  const iah = Element.prototype.insertAdjacentHTML;
  Element.prototype.insertAdjacentHTML = function (pos, html) { return iah.call(this, pos, rewrite(html)); };
  new MutationObserver((list) => {
    for (const rec of list) for (const n of rec.addedNodes) {
      if (n.nodeType !== 1) continue;
      const imgs = n.tagName === "IMG" ? [n] : n.querySelectorAll ? n.querySelectorAll("img") : [];
      for (const im of imgs) {
        const raw = im.getAttribute("src");
        if (raw && raw.indexOf("data:") !== 0 && mapSrc(raw) !== raw) im.src = raw;
        const ds = im.getAttribute("data-src");
        if (ds && mapSrc(ds) !== ds) im.setAttribute("data-src", mapSrc(ds));
      }
    }
  }).observe(document, { childList: true, subtree: true });

  // ---- fetch: there is no server, the page gets empty responses ----------------
  const controls = {
    running: true,
    sources: { mic: true, loopback: true, screen: true },
    target_window: "", loopback_windows: [], send_screenshot: true,
    current_window: null, target_missing: 0,
  };
  window.__controls = controls;
  // The "window picker" frame: open the real picker window with some windows already marked (set
  // by `shots.py`, for screenshots only)
  const PK = window.__ARGUS_PICKER__;
  if (PK) {
    controls.loopback_windows = PK.picked || [];
    window.addEventListener("load", () => setTimeout(() => {
      try { window.openPicker(PK.what || "audio"); } catch (e) { /* a page without the picker window */ }
    }, 500));
  }
  const json =(o) => Promise.resolve(new Response(JSON.stringify(o), {
    status: 200, headers: { "Content-Type": "application/json" } }));
  window.fetch = function (url) {
    if (String(url).indexOf("/control") === 0) return json(controls);
    // The list of windows for the picker ("What to listen to"): empty, except in the
    // frames where the picker is shot (`shots.py`), where it is made up
    if (String(url).indexOf("/windows") === 0) return json({ windows: window.__ARGUS_WINDOWS__ || [] });
    if (String(url).indexOf("/audio/devices") === 0) return json({ inputs: [], outputs: [] });
    if (String(url).indexOf("/admin/schema") === 0) {
      const MODES = [["off", "don't answer"], ["all", "everything"], ["by_form", "by phrasing"],
                     ["wake_word", "by name"], ["semantic", "by meaning"]]
        .map(([value, label]) => ({ value, label }));
      const LANGS = [["", "same as conversation"], ["ru", "русский"], ["uk", "українська"], ["en", "English"]]
        .map(([value, label]) => ({ value, label }));
      return json({
        values: { "channel.assist.answering.think_longer": !!window.__ARGUS_DEEP__,
                  "channel.assist.profile.language": "",
                  "channel.assist.answering.mic_mode": "off",
                  "channel.assist.answering.system_mode": "by_form" },
        sections: [{ settings: [
          { path: "channel.assist.profile.language", options: LANGS },
          { path: "channel.assist.answering.mic_mode", options: MODES },
          { path: "channel.assist.answering.system_mode", options: MODES } ] }],
      });
    }
    return json({});
  };

  // ---- scenario -------------------------------------------------------------
  const SC = window.__ARGUS_SCENARIO__;
  const rnd = (a, b) => a + Math.random() * (b - a);

  function stateAt(t) {
    const speech = [], shots = [], answers = [];
    let thinking = null;
    for (const e of SC.speech) if (e.at <= t) speech.push({
      t: e.clock, source: e.source, text: e.text, confidence: 0, error: null });  // we do not show the confidence number on the landing page
    for (const s of SC.shots) if (s.at <= t) shots.push({
      t: s.clock, id: s.id, kb: s.kb, kind: "screen" });
    for (const q of SC.qa) {
      if (t < q.askAt) continue;
      const shotKb = q.shot ? SC.shots.find((s) => s.id === q.shot).kb : 0;
      const meta = { cause: q.cause, deep: !!q.deep, image_kb: shotKb, image_src: shotKb ? "screen" : "",
                     model: "", audio_ids: [], error: "" };
      if (t < q.firstAt) {
        thinking = { reason: q.reason, cause: q.cause, deep: !!q.deep, image_kb: shotKb,
                     image_src: shotKb ? "screen" : "", t: q.askAt };
        continue;
      }
      // with no typing the answer arrives whole (typeFor 0); with typing (typeFor > 0) the text is
      // revealed character by character
      const k = q.typeFor > 0 ? Math.min(1, (t - q.firstAt) / q.typeFor) : 1;
      const n = Math.floor(q.text.length * k);
      answers.push(Object.assign({ text: q.text.slice(0, n), latency_ms: q.latency }, meta));
    }
    // sound levels: "me" is lit while I speak; "the other speaker" while they speak
    let me = 0.0008, them = 0.0008;
    for (const e of SC.speech) {
      if (t >= e.at - e.dur && t <= e.at) {
        if (e.source === "me") me = rnd(0.02, 0.09); else them = rnd(0.02, 0.09);
      }
    }
    me = Math.max(me, rnd(0.0004, 0.0016)); them = Math.max(them, rnd(0.0004, 0.0016));
    return {
      admin: false, problems: [], account_hint: "", can_paste_image: false,
      controls, levels: { me: { rms: me, peak: me * 2 }, them: { rms: them, peak: them * 2 } },
      speech, speech_b: [], engines: { a: "", b: "" },
      answers, thinking, shots, frames: 0, stages: {}, gate: {}, healthy: true, capture: true,
      screen: { lines: [], t: 0, window: "", new: [] },
    };
  }

  // ---- websocket ------------------------------------------------------------
  // Which frame is in the "screen" panel now: the one that went to the model last.
  function liveFor(t) {
    let cur = SCREENS.idle || null;
    for (const s of SC.shots) if (s.showAt <= t) cur = SCREENS[s.id];
    return cur || SCREENS.idle;
  }
  let lastLive = null;
  function push(ws, t) {
    const img = document.getElementById("shot");
    const live = liveFor(t);
    if (img && live && live !== lastLive) { lastLive = live; img.src = live; }
    if (ws.onmessage) ws.onmessage({ data: JSON.stringify(stateAt(t)) });
    if (!push.sent) {
      push.sent = true;
      try { parent.postMessage({ argus: "demo-ready", id: window.__ARGUS_ID__ || "" }, "*"); } catch (e) {}
    }
    // The scenario time goes outward: the "how it works" scheme on the page highlights its blocks
    // by the same clock that runs in the window
    if (parent !== window) {
      try { parent.postMessage({ argus: "demo-time", id: window.__ARGUS_ID__ || "", t }, "*"); } catch (e) {}
    }
  }

  const START = window.__ARGUS_START__ || 0;
  let paused = false, clockStart = 0, offset = START;
  const nowT = () => offset + (performance.now() - clockStart) / 1000;
  window.addEventListener("message", (e) => {
    const m = e.data;
    if (!m || m.argus !== "demo") return;
    if (m.cmd === "pause" && !paused) { if (clockStart) offset = nowT(); paused = true; }
    if (m.cmd === "play" && paused) { clockStart = performance.now(); paused = false; }
    if (m.cmd === "restart") { offset = START; clockStart = performance.now(); paused = false; }
    // seek to a second of the scenario (a tour of the window and page checks); when paused, it
    // draws the frame at once
    if (m.cmd === "seek") {
      offset = +m.t || 0; clockStart = performance.now();
      if (paused && lastWS) push(lastWS, offset);
    }
  });

  let lastWS = null;
  function FakeWS() {
    const self = this;
    lastWS = this;
    const freeze = window.__ARGUS_FREEZE__;
    setTimeout(() => {
      clockStart = performance.now();
      const tick = () => {
        if (document.hidden && freeze == null) return;
        let t;
        if (freeze != null) t = freeze;
        else {
          if (paused) { if (!push.sent) push(self, offset); return; }
          t = nowT();
          if (t > SC.length) { offset = START; clockStart = performance.now(); t = START; }
        }
        push(self, t);
      };
      self._timer = setInterval(tick, freeze != null ? 400 : 200);
      tick();
    }, 50);
    this.close = () => clearInterval(this._timer);
  }
  window.WebSocket = FakeWS;
})();
