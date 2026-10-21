/* A stand-in server for the "Record" page in the demo of the "Remembers" block
   (`build_demo.build_record`).
   There is no server: the page gets a made-up record and a list of sessions, and everything
   else is empty responses. The page itself is real, and the window with the conversation text
   is opened by its own code. The host (page.js) talks to it with messages:
     rect  — where the time in the answer is now (fractions of the window): the mouse on the
             landing page moves to it
     cite  — a click on that time: the text window opens, the same animation as in the app
     reset — return the page to its initial state for the next round
   The page answers the host: rec-ready (the answer is drawn) and rec-rect (the position of the
   time in the answer). */
(function () {
  "use strict";
  var REC = window.__ARGUS_RECORD__ || {};
  var CHATS = window.__ARGUS_CHATS__ || {};

  function json(o) {
    return Promise.resolve(new Response(JSON.stringify(o), { status: 200, headers: { "Content-Type": "application/json" } }));
  }
  window.fetch = function (url, opts) {
    var p = String(url).split("?")[0];
    if (opts && opts.method === "POST") return new Promise(function () { /* nobody processes the request in the demo */ });
    if (p === "/chats") return json(CHATS);
    if (/^\/record\/[^/]+$/.test(p)) return json(REC);
    if (p === "/admin/account") return json({ signed_in: true, enabled: true });
    return json({});
  };
  // The record page itself connects to nothing, but the window's shared scripts may hold a socket
  window.WebSocket = function () { this.close = function () {}; this.send = function () {}; };

  function say(m) { try { parent.postMessage(m, "*"); } catch (e) { /* without a host — nothing */ } }

  function cite() { var all = document.querySelectorAll("#turns .cite"); return all[1] || all[0]; }   // the second time: its turn in the record is not in view, and the list scrolls to it
  function where() {
    var c = cite();
    if (!c) return null;
    var r = c.getBoundingClientRect();
    return [(r.left + r.width / 2) / window.innerWidth * 100, (r.top + r.height / 2) / window.innerHeight * 100];
  }

  // We wait until the page draws the answer, and report that it can be shown
  var tries = 0;
  var iv = setInterval(function () {
    if (!cite()) { if (++tries >= 120) clearInterval(iv); return; }
    clearInterval(iv);
    var sc = document.getElementById("scroller");
    if (sc) sc.scrollTop = 0;
    say({ argus: "rec-ready", cite: where() });
  }, 50);

  window.addEventListener("message", function (e) {
    var m = e.data;
    if (!m || m.argus !== "demo") return;
    if (m.cmd === "rect") say({ argus: "rec-rect", cite: where() });
    if (m.cmd === "cite") { var c = cite(); if (c) c.click(); }
    if (m.cmd === "reset") {
      if (typeof window.closeText === "function") window.closeText();
      var sc = document.getElementById("scroller");
      if (sc) sc.scrollTop = 0;
    }
  });
})();
