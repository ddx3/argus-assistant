/* Interface translation by dictionary.

   The pages are written in English, and the English string in the code is also the
   translation key. The page is drawn as usual, and this script replaces the finished text:
   text nodes, hints (title, placeholder, aria-label, alt) and the tab title. If there is no
   translation, the English string stays and nothing breaks.

   Why this way and not `t("...")` on every line of code: the page builds markup with
   templates in a dozen places, and several people edit it at once. A wrapper in every
   template would break other people's edits and would be forgotten on every new line. Here a
   new English string simply stays English until it is added to `i18n/keys_manual.py`.

   The server includes it in every HTML page (`ui/i18n.py`) together with the dictionary of
   the chosen language: `window.ARGUS_LANG` and `window.ARGUS_I18N`. For English there is no
   dictionary, and the script does nothing.

   What is not translated: whatever people and the model wrote: lines of the conversation,
   answers, questions, file names. Otherwise a person who said "Me" aloud would see a
   translated word in the feed. The exception is the questions the app asks by itself
   (`CANNED`): they go to the model in the interface language. */

(function () {
  "use strict";

  var LANG = window.ARGUS_LANG || "en";
  var DICT = window.ARGUS_I18N || null;
  if (!DICT && window.ARGUS_I18N_ALL) DICT = window.ARGUS_I18N_ALL[LANG] || null;
  window.ARGUS_LANG = LANG;
  try { document.documentElement.lang = LANG; } catch (e) { /* no document, no problem */ }

  // Questions that the app asks the model on the person's behalf (index.html: `SCREEN_QUESTION`
  // and "Tell me what's in this image.").
  var CANNED = {
    "Tell me what's happening on the screen.": 1,
    "Tell me what's in this image.": 1,
  };

  var TXT = /[A-Za-z]/;                // only nodes with Latin letters can be keys
  var exact = Object.create(null);     // key without placeholders -> translation
  var patterns = [];                   // keys with {placeholders}
  var htmlExact = Object.create(null); // keys with tags, compared with the element's markup
  var htmlPatterns = [];

  function norm(s) {
    return String(s).replace(/[\s  ]+/g, " ").trim();
  }

  function compile(key) {
    var names = [];
    var re = norm(key).replace(/[.*+?^$()|[\]\\]/g, "\\$&")
      .replace(/\{([a-z_]+)\}/g, function (_, n) { names.push(n); return "(.*?)"; });
    return { re: new RegExp("^" + re + "$"), names: names };
  }

  function fill(tpl, names, groups) {
    return tpl.replace(/\{([a-z_]+)\}/g, function (all, n) {
      var i = names.indexOf(n);
      if (i < 0) return all;
      var v = groups[i + 1];
      // the value may itself be a known string ("Basic", "sign-in required")
      var hit = exact[norm(v)];
      return hit != null ? hit : v;
    });
  }

  if (DICT && LANG !== "en") {
    Object.keys(DICT).forEach(function (k) {
      var v = DICT[k];
      if (typeof v !== "string" || !v) return;
      var hasHtml = k.indexOf("<") >= 0;
      var hasSlot = /\{[a-z_]+\}/.test(k);
      var key = norm(k);
      if (hasSlot) {
        var c = compile(k);
        (hasHtml ? htmlPatterns : patterns).push({
          re: c.re, names: c.names, to: v,
          lit: key.replace(/\{[a-z_]+\}/g, "").length,    // how many real letters are in the key, not counting placeholders
        });
      } else if (hasHtml) {
        htmlExact[key] = v;
      } else {
        exact[key] = v;
      }
    });
    // The most specific key goes first. `(.*?)` in a key swallows any prefix, and the generic
    // "{n} chars" would intercept "cut off {n} chars", which comes later in the dictionary: the
    // result was "cut off 5 Zeichen" instead of the whole phrase translated. For equal length the order stays as in the dictionary (the sort is stable).
    var bySpecific = function (a, b) { return b.lit - a.lit; };
    patterns.sort(bySpecific);
    htmlPatterns.sort(bySpecific);
  }

  function lookup(key, html) {
    var table = html ? htmlExact : exact;
    var hit = table[key];
    if (hit != null) return hit;
    var list = html ? htmlPatterns : patterns;
    for (var i = 0; i < list.length; i++) {
      var m = list[i].re.exec(key);
      if (m) return fill(list[i].to, list[i].names, m);
    }
    return null;
  }

  /* For code: translate the string if a translation exists; otherwise return it as is. */
  window.argusT = function (s) {
    if (LANG === "en" || !DICT) return s;
    var hit = lookup(norm(s), false);
    return hit != null ? hit : s;
  };

  if (LANG === "en" || !DICT) return;

  // ---------------------------------------------------------------- DOM walk

  var ATTRS = ["title", "placeholder", "aria-label", "alt"];
  var INLINE = { B: 1, CODE: 1, A: 1, EM: 1, I: 1, SMALL: 1, STRONG: 1, SPAN: 1, BR: 1 };
  var SKIP_TAG = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, PRE: 1, NOSCRIPT: 1 };
  var seen = new WeakMap();   // node -> what we wrote into it

  // Other people's text: lines, model answers, questions. We translate only `CANNED`. The
  // "thinking" card has the same class `.io.output`, but with `.dim`: there it is the
  // app's caption ("over the line:"), and the text of the line lies separately,
  // in `.cause`.
  function userText(el) {
    if (!el || !el.closest) return false;
    if (el.closest(".tag")) return false;    // "question", "answer": app captions above other people's text
    return !!el.closest(
      ".io.output:not(.err):not(.dim), .q-row p, .cause, .cmp-cell, #chat .question p, [data-i18n='off']");
  }
  function speechRow(el) {
    // the text of the line lies right in `.row`, next to the `.row-head` and `.conf` marks
    return !!(el && el.classList && el.classList.contains("row") && el.closest && el.closest("#raw"));
  }

  function skipEl(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentNode) {
      if (SKIP_TAG[n.tagName]) return true;
    }
    return false;
  }

  function textNode(node) {
    var raw = node.nodeValue;
    if (!raw || !TXT.test(raw)) return;
    if (seen.get(node) === raw) return;
    var parent = node.parentNode;
    if (!parent || skipEl(parent)) return;
    var key = norm(raw);
    if ((userText(parent) || speechRow(parent)) && !CANNED[key]) return;
    var out = lookup(key, false);
    if (out == null || out === key) { seen.set(node, raw); return; }
    var lead = /^\s*/.exec(raw)[0], trail = /\s*$/.exec(raw)[0];
    var val = lead + out + trail;
    seen.set(node, val);
    node.nodeValue = val;
  }

  function attrs(el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (!v || !TXT.test(v)) continue;
      var out = lookup(norm(v), false);
      if (out != null && out !== v) el.setAttribute(ATTRS[i], out);
    }
  }

  // An element in which text is interleaved with tags such as `<b>` and `<code>`: the word order
  // is different in another language, so the key is the whole markup of the element, not pieces
  // of it.
  function htmlElement(el) {
    if (!el.children.length || el.children.length > 8 || el.innerHTML.length > 1500) return false;
    for (var i = 0; i < el.children.length; i++) {
      if (!INLINE[el.children[i].tagName]) return false;
    }
    var html = el.innerHTML;
    if (seen.get(el) === html) return true;
    if (!TXT.test(html)) return false;
    var out = lookup(norm(html), true);
    if (out == null) return false;
    el.innerHTML = out;
    seen.set(el, el.innerHTML);
    return true;
  }

  function tree(root) {
    if (!root) return;
    if (root.nodeType === 3) { textNode(root); return; }
    if (root.nodeType !== 1) return;
    attrs(root);                 // the hint in an empty field lives in a `textarea`, whose text we do not touch
    if (skipEl(root)) return;
    if (htmlElement(root)) return;
    for (var c = root.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) textNode(c);
      else if (c.nodeType === 1) tree(c);
    }
  }

  function title() {
    var t = document.title;
    if (t && TXT.test(t)) {
      var out = lookup(norm(t), false);
      if (out != null) document.title = out;
    }
  }

  function run() {
    try { title(); tree(document.body || document.documentElement); } catch (e) { console.warn("i18n:", e); }
  }

  var pending = [];
  var queued = false;
  function flush() {
    queued = false;
    var batch = pending; pending = [];
    for (var i = 0; i < batch.length; i++) {
      try {
        var r = batch[i];
        if (r.type === "attributes") { if (r.target.nodeType === 1) attrs(r.target); }
        else if (r.type === "characterData") textNode(r.target);
        else for (var j = 0; j < r.addedNodes.length; j++) tree(r.addedNodes[j]);
        if (r.type === "childList" && r.target.nodeType === 1) htmlElement(r.target);
      } catch (e) { console.warn("i18n:", e); }
    }
    try { title(); } catch (e) { /* the title is not the main thing */ }
  }

  function start() {
    run();
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) pending.push(records[i]);
      if (!queued) { queued = true; Promise.resolve().then(flush); }
    }).observe(document.documentElement, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  // ------------------------------------------------------ network: questions and language

  var KEY = "argus.interface.language.changed";

  function reloadAll() { try { location.reload(); } catch (e) { /* closed */ } }

  // Other app windows (panels, settings in an embed) learn about a language change through
  // storage: the `storage` event goes to all documents except the one that wrote.
  window.addEventListener("storage", function (e) { if (e.key === KEY) reloadAll(); });

  if (window.fetch) {
    var realFetch = window.fetch.bind(window);
    window.fetch = function (url, init) {
      var u = typeof url === "string" ? url : (url && url.url) || "";
      var body = init && typeof init.body === "string" ? init.body : "";
      var changed = false;
      try {
        // A question that the app asks by itself goes to the model in the interface language.
        if (/\/ask(\/screen)?$/.test(u) && body.indexOf("question") >= 0) {
          var o = JSON.parse(body);
          if (o && typeof o.question === "string" && CANNED[norm(o.question)]) {
            o.question = window.argusT(o.question);
            init = Object.assign({}, init, { body: JSON.stringify(o) });
          }
        }
        changed = /\/admin\/apply$/.test(u) && body.indexOf("channel.assist.interface.language") >= 0;
      } catch (e) { /* not our case, we send it as is */ }
      var p = realFetch(url, init);
      if (changed) {
        // We reload only if the edit was accepted: on an error in any of the pending fields the
        // server refuses as a whole (`ok: false`, response 200), and then the language did not
        // change, while a reload would wipe both the message and what was typed. If the network
        // dropped or the response could not be parsed, there is also no reload (and no unhandled
        // rejection).
        p.then(function (r) { return r.clone().json(); }).then(function (data) {
          if (!data || data.ok !== true) return;
          try { localStorage.setItem(KEY, String(Date.now())); } catch (e) { /* no storage */ }
          setTimeout(reloadAll, 150);
        }).catch(function () { /* the language did not change */ });
      }
      return p;
    };
  }
})();
