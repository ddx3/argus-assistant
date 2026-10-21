"use strict";
/* A mini-DOM for running the real `chrome.js`, `admin.js` and `winbar.js` in node (jsdom
   cannot be installed).

   It has exactly what these scripts reach for: a tree of nodes with parsing and writing of
   `innerHTML`, attributes, `dataset`, `classList`, selector matching (tags, classes, ids,
   attributes, `:not`, space and `>`), events with three phases, `value` on fields and
   lists, a live `FileList` for file inputs, `DOMParser`, fake timers, and `fetch` with
   responses supplied by the test. There is no layout: sizes are set in the test (`_w`,
   `_h`, `_left`, `_top`), and `getBoundingClientRect` returns them.
   It is included like this: `const { makeEnv } = require(process.env.FRONT_DOM)`; the page
   scripts run in an isolated context (`env.run`), and `window` in it is the context itself,
   as on a page. */

const vm = require("vm");

const VOID = new Set(["br", "hr", "img", "input", "meta", "link", "col"]);
const dec = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
const encText = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const encAttr = (s) => encText(s).replace(/"/g, "&quot;");
const kebab = (s) => s.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

// ---------------------------------------------------------------- events

class Evt {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = !!init.bubbles;
    this.cancelable = true;
    this.defaultPrevented = false;
    this.target = null;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; this._stopNow = true; }
}
class CustomEvt extends Evt {
  constructor(type, init = {}) { super(type, init); this.detail = init.detail; }
}

class Target {
  constructor() { this._ls = []; }
  addEventListener(type, fn, opts) {
    const capture = opts === true || !!(opts && opts.capture);
    this._ls.push({ type, fn, capture });
  }
  removeEventListener(type, fn, opts) {
    const capture = opts === true || !!(opts && opts.capture);
    this._ls = this._ls.filter((l) => !(l.type === type && l.fn === fn && l.capture === capture));
  }
  dispatchEvent(evt) { return fire(this, evt, (this.ownerDocument || this)._win); }
}

// An event goes through three phases, as in a browser: top down, the `capture` listeners;
// on the target, all of them; then bottom up.
function fire(target, evt, win) {
  evt.target = target;
  const chain = [];
  for (let n = target; n; n = n.parentNode) chain.push(n);
  if (chain[chain.length - 1].nodeType === 9) chain.push(win);
  const run = (node, capture) => {
    for (const l of [...(node._ls || [])]) {
      if (l.type !== evt.type) continue;
      if (capture !== null && l.capture !== capture) continue;
      evt.currentTarget = node;
      l.fn.call(node, evt);
      if (evt._stopNow) return;
    }
  };
  for (let i = chain.length - 1; i > 0 && !evt._stop; i--) run(chain[i], true);
  if (!evt._stop) run(chain[0], null);
  if (evt.bubbles) for (let i = 1; i < chain.length && !evt._stop; i++) run(chain[i], false);
  return !evt.defaultPrevented;
}

// ---------------------------------------------------------------- tree

function detach(n) {
  const p = n.parentNode;
  if (!p) return;
  const i = p.childNodes.indexOf(n);
  if (i >= 0) p.childNodes.splice(i, 1);
  n.parentNode = null;
}
function insertBefore(parent, nodes, ref) {
  for (const n of nodes) {
    detach(n);
    const i = ref ? parent.childNodes.indexOf(ref) : -1;
    if (i >= 0) parent.childNodes.splice(i, 0, n); else parent.childNodes.push(n);
    n.parentNode = parent;
  }
}
function toNodes(doc, items) {
  return items.map((x) => (typeof x === "object" && x ? x : new Text_(doc, String(x))));
}
const connected = (n) => { for (; n; n = n.parentNode) if (n.nodeType === 9) return true; return false; };

class Text_ {
  constructor(doc, v) { this.nodeType = 3; this.ownerDocument = doc; this.nodeValue = v; this.parentNode = null; }
  get textContent() { return this.nodeValue; }
  set textContent(v) { this.nodeValue = String(v); }
  get nextSibling() { const p = this.parentNode; return p ? p.childNodes[p.childNodes.indexOf(this) + 1] || null : null; }
  get isConnected() { return connected(this); }
  cloneNode() { return new Text_(this.ownerDocument, this.nodeValue); }
  remove() { detach(this); }
  before(...ns) { insertBefore(this.parentNode, toNodes(this.ownerDocument, ns), this); }
  after(...ns) { insertBefore(this.parentNode, toNodes(this.ownerDocument, ns), this.nextSibling); }
  replaceWith(...ns) { const p = this.parentNode; insertBefore(p, toNodes(this.ownerDocument, ns), this); detach(this); }
  contains(n) { return n === this; }
}

// ---------------------------------------------------------------- selectors

function splitTop(s, sep) {
  const out = [];
  let depth = 0, q = "", cur = "";
  for (const ch of s) {
    if (q) { cur += ch; if (ch === q) q = ""; continue; }
    if (ch === '"' || ch === "'") { q = ch; cur += ch; continue; }
    if (ch === "[" || ch === "(") depth++;
    if (ch === "]" || ch === ")") depth--;
    if (ch === sep && depth === 0) { out.push(cur); cur = ""; } else cur += ch;
  }
  out.push(cur);
  return out;
}

function parseCompound(s) {
  const c = { tag: null, id: null, classes: [], attrs: [], nots: [], scope: false };
  let i = 0;
  const m = /^(\*|[a-zA-Z][\w-]*)/.exec(s);
  if (m) { c.tag = m[1] === "*" ? null : m[1].toLowerCase(); i = m[0].length; }
  while (i < s.length) {
    const rest = s.slice(i);
    let m2;
    if ((m2 = /^\.([\w-]+)/.exec(rest))) { c.classes.push(m2[1]); i += m2[0].length; }
    else if ((m2 = /^#([\w-]+)/.exec(rest))) { c.id = m2[1]; i += m2[0].length; }
    else if (rest[0] === "[") {
      const end = rest.indexOf("]");
      const body = rest.slice(1, end).trim();
      const a = /^([\w-]+)(?:\s*([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+)))?$/.exec(body);
      if (!a) throw new Error("selector not parsed: " + s);
      c.attrs.push({ n: a[1], op: a[2] || null, v: a[3] ?? a[4] ?? a[5] });
      i += end + 1;
    }
    else if ((m2 = /^:not\(([^)]*)\)/.exec(rest))) { c.nots.push(parseCompound(m2[1].trim())); i += m2[0].length; }
    else if ((m2 = /^:scope/.exec(rest))) { c.scope = true; i += m2[0].length; }
    else throw new Error("selector not parsed: " + s);
  }
  return c;
}

const selCache = new Map();
function parseSel(sel) {
  if (selCache.has(sel)) return selCache.get(sel);
  const alts = splitTop(sel, ",").map((alt) => {
    const out = [];
    let i = 0, comb = " ";
    const s = alt.trim();
    while (i < s.length) {
      if (/\s/.test(s[i])) { i++; continue; }
      if (s[i] === ">") { comb = ">"; i++; continue; }
      let j = i, depth = 0, q = "";
      for (; j < s.length; j++) {
        const ch = s[j];
        if (q) { if (ch === q) q = ""; continue; }
        if (ch === '"' || ch === "'") { q = ch; continue; }
        if (ch === "[" || ch === "(") depth++;
        if (ch === "]" || ch === ")") depth--;
        if (depth === 0 && (/\s/.test(ch) || ch === ">")) break;
      }
      out.push({ comb, c: parseCompound(s.slice(i, j)) });
      comb = " ";
      i = j;
    }
    return out;
  });
  selCache.set(sel, alts);
  return alts;
}

function matchCompound(el, c, scope) {
  if (c.scope && el !== scope) return false;
  if (c.tag && el.localName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  if (c.classes.some((k) => !el.classList.contains(k))) return false;
  for (const a of c.attrs) {
    const v = el.getAttribute(a.n);
    if (v === null) return false;
    if (a.op === "=" && v !== a.v) return false;
    if (a.op === "^=" && !v.startsWith(a.v)) return false;
    if (a.op === "*=" && !v.includes(a.v)) return false;
  }
  return !c.nots.some((n) => matchCompound(el, n, scope));
}
function matchComplex(el, alt, scope, k = alt.length - 1) {
  if (!matchCompound(el, alt[k].c, scope)) return false;
  if (k === 0) return true;
  if (alt[k].comb === ">") {
    const p = el.parentNode;
    return !!p && p.nodeType === 1 && matchComplex(p, alt, scope, k - 1);
  }
  for (let p = el.parentNode; p && p.nodeType === 1; p = p.parentNode) if (matchComplex(p, alt, scope, k - 1)) return true;
  return false;
}

// ---------------------------------------------------------------- element

class El extends Target {
  constructor(doc, tag) {
    super();
    this.nodeType = 1;
    this.ownerDocument = doc;
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this._attrs = new Map();
    this.style = {};
    this.scrollTop = 0; this.scrollLeft = 0; this.scrollWidth = 0;
    this.clientWidth = 0; this.clientHeight = 0; this.offsetHeight = 0;
  }
  // attributes
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  removeAttribute(n) { this._attrs.delete(n); }
  hasAttribute(n) { return this._attrs.has(n); }
  get attributes() { return [...this._attrs].map(([name, value]) => ({ name, value })); }
  get id() { return this.getAttribute("id") || ""; }
  set id(v) { this.setAttribute("id", v); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(v) { if (v) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(v) { if (v) this.setAttribute("disabled", ""); else this.removeAttribute("disabled"); }
  get type() { return this.getAttribute("type") || (this.localName === "input" ? "text" : ""); }
  get tabIndex() {
    const t = this.getAttribute("tabindex");
    if (t !== null) return Number(t);
    return ["button", "input", "select", "textarea", "a"].includes(this.localName) ? 0 : -1;
  }
  set tabIndex(v) { this.setAttribute("tabindex", v); }
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get: (_, p) => (typeof p === "string" ? (el.getAttribute("data-" + kebab(p)) ?? undefined) : undefined),
      set: (_, p, v) => { el.setAttribute("data-" + kebab(p), v); return true; },
      has: (_, p) => el.hasAttribute("data-" + kebab(p)),
      ownKeys: () => el.attributes.filter((a) => a.name.startsWith("data-")).map((a) => camel(a.name.slice(5))),
      getOwnPropertyDescriptor: (_, p) => (el.hasAttribute("data-" + kebab(String(p)))
        ? { enumerable: true, configurable: true, value: el.getAttribute("data-" + kebab(String(p))) } : undefined),
    });
  }
  get classList() {
    const el = this;
    const get = () => el.className.split(/\s+/).filter(Boolean);
    const set = (a) => { el.className = a.join(" "); };
    return {
      add: (...c) => { const a = get(); for (const x of c) if (!a.includes(x)) a.push(x); set(a); },
      remove: (...c) => set(get().filter((x) => !c.includes(x))),
      contains: (c) => get().includes(c),
      toggle: (c, force) => {
        const has = get().includes(c);
        const want = force === undefined ? !has : !!force;
        if (want && !has) set([...get(), c]);
        if (!want && has) set(get().filter((x) => x !== c));
        return want;
      },
    };
  }
  // values of fields and lists
  get value() {
    if (this.localName === "select") {
      const i = this.selectedIndex;
      const o = this.options[i];
      return o ? (o.getAttribute("value") ?? o.textContent) : "";
    }
    if (this.localName === "option") return this.getAttribute("value") ?? this.textContent;
    if (this.localName === "textarea") return this._value !== undefined ? this._value : this.textContent;
    return this._value !== undefined ? this._value : (this.getAttribute("value") || "");
  }
  set value(v) {
    if (this.localName === "select") {
      this._sel = this.options.findIndex((o) => (o.getAttribute("value") ?? o.textContent) === String(v));
      return;
    }
    this._value = String(v);
    // like a real file input: clearing the field empties the same live list
    if (this.localName === "input" && this.type === "file" && this._value === "" && Array.isArray(this.files)) this.files.length = 0;
  }
  get options() { return this.localName === "select" ? this.querySelectorAll("option") : []; }
  get selectedIndex() {
    if (this.localName !== "select") return -1;
    if (this._sel !== undefined) return this._sel;
    const o = this.options;
    const i = o.findIndex((x) => x.hasAttribute("selected"));
    return i >= 0 ? i : (o.length ? 0 : -1);
  }
  set selectedIndex(i) { this._sel = i; }
  // tree
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() { const p = this.parentNode; return p ? p.childNodes[p.childNodes.indexOf(this) + 1] || null : null; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get isConnected() { return connected(this); }
  get offsetParent() { return this.isConnected ? this.parentNode : null; }
  appendChild(n) { insertBefore(this, [n], null); return n; }
  append(...ns) { insertBefore(this, toNodes(this.ownerDocument, ns), null); }
  prepend(...ns) { insertBefore(this, toNodes(this.ownerDocument, ns), this.childNodes[0] || null); }
  before(...ns) { insertBefore(this.parentNode, toNodes(this.ownerDocument, ns), this); }
  after(...ns) { insertBefore(this.parentNode, toNodes(this.ownerDocument, ns), this.nextSibling); }
  remove() { detach(this); }
  replaceWith(...ns) { const p = this.parentNode; insertBefore(p, toNodes(this.ownerDocument, ns), this); detach(this); }
  insertAdjacentHTML(pos, html) {
    const nodes = parseHTML(this.ownerDocument, html);
    if (pos === "beforeend") insertBefore(this, nodes, null);
    else if (pos === "afterbegin") insertBefore(this, nodes, this.childNodes[0] || null);
    else if (pos === "beforebegin") insertBefore(this.parentNode, nodes, this);
    else insertBefore(this.parentNode, nodes, this.nextSibling);
  }
  cloneNode(deep) {
    const c = new El(this.ownerDocument, this.localName);
    for (const [k, v] of this._attrs) c._attrs.set(k, v);
    if (this._value !== undefined) c._value = this._value;
    if (deep) for (const ch of this.childNodes) insertBefore(c, [ch.cloneNode(true)], null);
    return c;
  }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  // content
  get innerHTML() { return this.childNodes.map(serialize).join(""); }
  set innerHTML(html) {
    for (const c of [...this.childNodes]) detach(c);
    this._sel = undefined;
    insertBefore(this, parseHTML(this.ownerDocument, String(html)), null);
  }
  get outerHTML() { return serialize(this); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) {
    for (const c of [...this.childNodes]) detach(c);
    if (String(v) !== "") insertBefore(this, [new Text_(this.ownerDocument, String(v))], null);
  }
  // queries
  querySelectorAll(sel) {
    const alts = parseSel(sel);
    const out = [];
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c.nodeType !== 1) continue;
        if (alts.some((a) => matchComplex(c, a, this))) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    const alts = parseSel(sel);
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (alts.some((a) => matchComplex(n, a, this))) return n;
    return null;
  }
  matches(sel) { return parseSel(sel).some((a) => matchComplex(this, a, this)); }
  // layout is only what the test has set
  getBoundingClientRect() {
    const w = this._w ?? 100, h = this._h ?? 30, l = this._left ?? 0, t = this._top ?? 0;
    return { left: l, top: t, right: l + w, bottom: t + h, width: w, height: h };
  }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }
  scrollIntoView() {}
  click() { return fire(this, new Evt("click", { bubbles: true }), this.ownerDocument._win); }
}

function serialize(n) {
  if (n.nodeType === 3) return encText(n.nodeValue);
  const attrs = [...n._attrs].map(([k, v]) => (v === "" ? ` ${k}` : ` ${k}="${encAttr(v)}"`)).join("");
  if (VOID.has(n.localName)) return `<${n.localName}${attrs}>`;
  return `<${n.localName}${attrs}>${n.childNodes.map(serialize).join("")}</${n.localName}>`;
}

function parseHTML(doc, html) {
  const root = new El(doc, "div");
  const stack = [root];
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*(\/?)>|([^<]+)|(<)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[0].startsWith("<!--")) continue;
    const top = stack[stack.length - 1];
    if (m[1]) {
      const tag = m[1].toLowerCase();
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].localName === tag) { stack.length = i; break; }
    } else if (m[2]) {
      const el = new El(doc, m[2]);
      const ar = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?/g;
      let a;
      while ((a = ar.exec(m[3]))) el._attrs.set(a[1].toLowerCase(), dec(a[2] ?? a[3] ?? a[4] ?? ""));
      insertBefore(top, [el], null);
      if (!VOID.has(el.localName) && !m[4]) stack.push(el);
    } else if (m[5] !== undefined) {
      insertBefore(top, [new Text_(doc, dec(m[5]))], null);
    } else {
      insertBefore(top, [new Text_(doc, "<")], null);
    }
  }
  return [...root.childNodes];
}

class Doc extends Target {
  constructor() {
    super();
    this.nodeType = 9;
    this.parentNode = null;
    this.ownerDocument = this;
    this.childNodes = [];
    this.documentElement = new El(this, "html");
    this.head = new El(this, "head");
    this.body = new El(this, "body");
    insertBefore(this.documentElement, [this.head, this.body], null);
    insertBefore(this, [this.documentElement], null);
    this.activeElement = this.body;
    this.readyState = "complete";
    this.title = "";
  }
  createElement(tag) { return new El(this, tag); }
  createTextNode(t) { return new Text_(this, String(t)); }
  getElementById(id) { return this.documentElement.querySelector(`[id="${id}"]`); }
  querySelector(sel) { return this.documentElement.querySelector(sel); }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
  hasFocus() { return true; }
}

class DOMParserStub {
  parseFromString(html) {
    const d = new Doc();
    insertBefore(d.body, parseHTML(d, String(html)), null);
    return d;
  }
}

// ---------------------------------------------------------------- page environment

function makeEnv() {
  const doc = new Doc();
  const timers = [];
  let now = 0, seq = 0;
  const calls = [];          // everything that went out through fetch: { url, method, body, raw, init }
  const routes = {};         // address without the query string -> (body, init) => { status?, body } | promise
  const sockets = [];
  const store = new Map();

  const sb = {
    document: doc, console, TextEncoder, URL, URLSearchParams, encodeURIComponent, decodeURIComponent, setImmediate,
    Event: Evt, CustomEvent: CustomEvt, DOMParser: DOMParserStub,
    navigator: { platform: "Win32", userAgent: "node" },
    location: { search: "", hash: "", host: "127.0.0.1:8420", pathname: "/", href: "http://127.0.0.1:8420/" },
    history: { replaceState() {} },
    screen: { width: 1920, height: 1080 },
    innerWidth: 1280, innerHeight: 800, devicePixelRatio: 1, screenY: 0,
    CSS: { escape: (s) => String(s).replace(/[^\w-]/g, "\\$&") },
    sessionStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    },
    WebSocket: class { constructor(url) { this.url = url; sockets.push(this); } },
    getComputedStyle: () => ({}),
    _ls: [], parentNode: null, nodeType: 0,
  };
  sb.window = sb;
  sb.top = sb;
  doc._win = sb;
  sb.addEventListener = Target.prototype.addEventListener;
  sb.removeEventListener = Target.prototype.removeEventListener;
  sb.dispatchEvent = (e) => fire(sb, e, sb);
  sb.setTimeout = (fn, ms = 0) => { const id = ++seq; timers.push({ id, at: now + ms, fn }); return id; };
  sb.clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
  sb.requestAnimationFrame = (fn) => sb.setTimeout(fn, 16);
  sb.fetch = async (url, init = {}) => {
    const raw = init.body;
    let body = null;
    if (typeof raw === "string") { try { body = JSON.parse(raw); } catch (e) { body = null; } }
    calls.push({ url, method: init.method || "GET", body, raw, init });
    const route = routes[String(url).split("?")[0]];
    if (!route) return { ok: false, status: 404, json: async () => ({ detail: "no such route" }) };
    const res = await route(body, init);
    const status = res.status ?? 200;
    return {
      ok: status >= 200 && status < 300, status,
      json: async () => { if (res.text !== undefined) throw new SyntaxError("not JSON"); return JSON.parse(JSON.stringify(res.body)); },
    };
  };
  vm.createContext(sb);

  const settle = async () => { for (let i = 0; i < 25; i++) await new Promise((r) => setImmediate(r)); };
  const env = {
    window: sb, document: doc, calls, routes, sockets, settle,
    run(code, name) { return vm.runInContext(code, sb, { filename: name || "page.js" }); },
    el(tag, attrs = {}) { const e = doc.createElement(tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; },
    // an event with fields, as the browser sets them (`ctrlKey`, `code`, …)
    ev(type, fields = {}, bubbles = true) { return Object.assign(new Evt(type, { bubbles }), fields); },
    fire(target, event) { return fire(target, event, sb); },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        timers.splice(timers.indexOf(due), 1);
        now = Math.max(now, due.at);
        due.fn();
        await settle();
      }
      now = end;
      await settle();
    },
    gate() { let open; const promise = new Promise((r) => { open = r; }); return { promise, open }; },
    posts(url) { return calls.filter((c) => c.method === "POST" && String(c.url).split("?")[0] === url); },
  };
  return env;
}

module.exports = { makeEnv, El, Text_, Evt };
