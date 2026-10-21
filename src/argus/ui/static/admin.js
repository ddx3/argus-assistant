/* The scaffold of the admin page: it draws the sections from the description sent by the
   server and collects the edits.

   The channel page draws no field itself. It declares the sections in Python and adds its
   own blocks to them through `adminWidgets`. The reason is simple: there are about a
   hundred fields across three channels, and three copies of one form would drift apart at
   the first edit. The channels have room to differ in what they configure, not in how an
   input field looks.

   Edits are not sent to the server on every keystroke. The pipeline settings are tied
   together: the VAD threshold and the confidence threshold are tuned together, and
   applying them one at a time would run the pipeline through obviously bad intermediate
   states. So we accumulate and then save. */

// Everything is in its own scope, like `account.js`: this file is now loaded not only on
// its own page but also straight into the dashboard document (`chrome.js`, `openSettings`),
// and the shared `$`/`esc`/`save` and about a dozen other top-level names would collide
// there with the names of the dashboard itself (`SyntaxError: Identifier '$' has already
// been declared`, caught live).
(function () {
"use strict";

// `ROOT` is where the page is mounted: the whole document on the real `/admin`
// (`argus run --settings`, without the dashboard, see `mount()` below), or its own
// container inside the dashboard, when the panel is opened over it. The same scaffold works
// in both cases; the only difference is this search boundary.
let ROOT = document;
const $ = (sel, root) => (root || ROOT).querySelector(sel);
const $$ = (sel, root) => [...(root || ROOT).querySelectorAll(sel)];
// An `id` may contain dots (`hint-channel.assist.profile.tone`), and a plain `#id` selector
// would read them as classes. An attribute selector does not parse them at all, so
// `CSS.escape` is not needed.
const byId = (id) => ROOT.querySelector(`[id="${id}"]`);
const esc = (x) => String(x ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

window.adminWidgets = window.adminWidgets || {};

const A = {
  schema: null,
  values: {},
  pending: {},          // path -> new value, not saved yet
  failed: {},           // path -> why the server did not accept the edit; it left the queue and does not block the
                        // others until the field is fixed
  problem: "",          // why saving failed altogether (no connection, unexpected response); the edits stay in the
                        // queue
  stale: false,         // the page is open for one session, but the app already has another: writing from here is
                        // not allowed
  chat: undefined,      // id of the session the page is open for (if the server reports it in the schema)
  overridden: new Set(),
  restart: new Set(),
  live: [],             // subscribers to the pipeline snapshot
  snapshot: null,
};

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

// The settings and account window to which `chrome.js` brings the tabs (`openWindow`). On
// the real `/admin` without the dashboard (`argus run --settings`) there is none: there
// the page is a whole document of its own, with a table of contents on the left and the
// sections one after another, as it always was.
let WIN = null;

// The scaffold of the settings page: the table of contents, the "restart needed"
// bar, and a place for the sections. It is one template for the real
// `/admin` (the `--settings` tab, which also has the link back to the dashboard built in);
// inside the dashboard window there is no table of contents, and its role belongs to the
// window tabs (`chrome.js`); what stays here is the "restart needed" bar and a place for
// the tab panels.
function shellHTML(standalone) {
  const restart = `
      <div class="restart" id="restart" hidden>
        <b>restart needed:</b><span id="restart-n"></span>
        <span class="dim">Saved, but it will take effect after a restart.</span>
      </div>`;
  // An edit that was not saved. It is a bar above all the tabs, not a line under the field:
  // the field sits on one tab, and the person may have moved to another, and then the refusal
  // message would stay where they no longer look.
  const unsaved = `
      <div class="restart unsaved" id="unsaved" role="alert" hidden>
        <b>not saved:</b><span id="unsaved-n"></span>
      </div>`;
  if (WIN) return `${restart}${unsaved}<div id="main"></div>`;
  return `
    <header>
      ${standalone ? `
      <div class="top">
        <nav class="tabs"><a href="/">dashboard</a></nav>
        <span class="version-badge" id="version-badge"></span>
      </div>` : ""}${restart}${unsaved}
    </header>
    <div class="wrap">
      <aside class="side"><ol id="side-nav"></ol></aside>
      <section class="card" id="window-card" hidden>
        <h2>Window</h2>
        <div class="widget" data-widget="window-visibility"></div>
      </section>
      <main id="main"></main>
    </div>`;
}

// Entry point. `el` is where to draw: the whole document on the real `/admin` (see the
// call in `admin.html`), or a container inside the dashboard window (`chrome.js`,
// `addSettingsTabs`; `win` is the window itself, to which we add tabs), by the same
// technique as `account.js`. The tab title and the app theme color (`--accent`) are touched
// only in the first case: in the second it is someone else's document, the dashboard, and
// replacing its title or accent would mean repainting the dashboard for the sake of
// something that was opened over it for a minute.
async function mount(el, { standalone = false, win = null } = {}) {
  ROOT = el;
  WIN = win;
  el.classList.add("admin-root");
  el.classList.toggle("standalone", standalone);
  el.innerHTML = shellHTML(standalone);

  // The main page (the list of chats) asks only for the general settings: there is nothing to
  // configure for "this chat" while you are not in one. The chat work screen sets no scope
  // and gets everything.
  const scope = document.body.dataset.settingsScope || "";
  const res = await fetch("/admin/schema" + (scope ? `?scope=${encodeURIComponent(scope)}` : ""));
  A.schema = await res.json();
  A.values = A.schema.values;
  A.overridden = new Set(A.schema.overridden);
  A.restart = new Set(A.schema.restart_pending);
  // Which session this page belongs to: an edit goes out with this id, and the app will
  // refuse it if another session is active (the tab stayed on `/chat/A`, and `/chat/B` was
  // opened in a neighboring one), rather than write the context of one session into another.
  A.chat = "chat" in A.schema ? A.schema.chat : undefined;

  if (standalone) {
    document.documentElement.style.setProperty("--accent", A.schema.accent);
    document.title = A.schema.title;
  }

  renderSections();
  // In the window the channel blocks are raised when the tab is opened for the first time
  // (`onShow` in `renderTabs`): a block that measures sizes (the voice list) measures zeros
  // on a hidden tab, and the microphone test and hardware polling are not needed while they
  // are not visible.
  if (!WIN) mountWidgets();
  bindBars();
  connectLive();
  loadLayout();
  if (navigator.keyboard && navigator.keyboard.addEventListener) navigator.keyboard.addEventListener("layoutchange", loadLayout);

  // The person gets only the number, "v1.0". The build hash (`build` in the schema) is not
  // shown on screen: the interface has no tooltips.
  const { number } = A.schema.version;
  const label = number === "source" ? "source" : `v${number}`;

  if (standalone) {
    const badge = $("#version-badge");
    if (badge) badge.textContent = label;
  } else if (WIN) {
    // In the window the version is a quiet caption at the bottom of the left column: drawing a
    // lone badge in an empty strip looked crooked, a bare label with no reason to stand exactly
    // there.
    WIN.setFoot(label);
  }
}

// --------------------------------------------------------------------------
// Markup
// --------------------------------------------------------------------------

// Inside a chat there are two kinds of sections, those of this chat and those of the whole
// app, and by name ("Language", "Audio") they cannot be told apart:
// "Language" has its own setting for each chat, "Sound" is shared. So each group has its
// own heading. Without a chat (the main page, `argus settings`) there is one group, and a
// heading would only get in the way.
function sectionsHTML() {
  const inChat = A.schema.sections.some((s) => s.settings.some((f) => f.path === "chat.name"));
  const GROUP = { chat: "This session", app: "App" };
  let last = null;
  return A.schema.sections.map((s) => {
    const head = inChat && s.scope !== last ? `<h3 class="group">${GROUP[s.scope] || ""}</h3>` : "";
    last = s.scope;
    return head + section(s);
  }).join("");
}

function renderSections() {
  if (WIN) renderTabs(); else $("#main").innerHTML = sectionsHTML();
  $$(".fields .f").forEach(bindField);
  $$(".card .info").forEach(bindInfo);
  if (!WIN) renderSideNav();
}

// The "Window" card is not from the schema but part of the scaffold: its block
// (`window-visibility`) shows it itself when it is ready.
const WINDOW_CARD = "window-card";

// Window tabs. A schema section lives on one of them; the small sections are gathered on
// "General", as in the reference app, because a tab with one row would look empty.
// A section that is not listed here (a new one was added on the server) gets its own tab:
// it must not be lost because the client does not know about it. A tab that has no section
// in the schema (the main page has no sections of the current session) is not drawn.
const TABS = [
  { id: "context",   scope: "chat", title: "Context",        icon: "doc",     sections: ["about"] },
  { id: "materials", scope: "chat", title: "Materials",      icon: "clip",    sections: ["materials"] },
  { id: "language",  scope: "chat", title: "Language",       icon: "lang",    sections: ["language"] },
  { id: "answering", scope: "chat", title: "When to answer", icon: "bolt",    sections: ["answering"] },
  // "General" is two rows from two different sections: without their own subheadings
  // ("Window" and "App"; the second also repeated the name of the group on
  // the left) and with its own explanation in the header, like the other tabs.
  { id: "general",   scope: "app",  title: "General",        icon: "sliders", sections: [WINDOW_CARD, "interface"],
    flat: true, lead: "The app window and the interface language." },
  { id: "hotkeys",   scope: "app",  title: "Hotkeys",        icon: "kbd",     sections: ["hotkeys"] },
  { id: "audio",     scope: "app",  title: "Audio",          icon: "mic",     sections: ["audio"] },
  { id: "voice",     scope: "app",  title: "Voice-over",     icon: "speaker", sections: ["tts"] },
];

// A section as a window tab. The group names are the same as the headings above the groups
// of sections on the real `/admin` (`sectionsHTML`): inside a session "This session"
// and "App"; without a session (the main page) there is one group,
// and a label would only get in the way.
function renderTabs() {
  const GROUP = { chat: "This session", app: "App" };
  const inChat = A.schema.sections.some((s) => s.settings.some((f) => f.path === "chat.name"));
  const byName = new Map(A.schema.sections.map((s) => [s.id, s]));
  const known = new Set(TABS.flatMap((t) => t.sections));
  const plan = [...TABS];
  for (const s of A.schema.sections) {
    if (!known.has(s.id)) plan.push({ id: s.id, scope: s.scope, title: s.title, icon: "sliders", sections: [s.id] });
  }
  const main = $("#main");
  plan.forEach((t, n) => {
    const secs = t.sections.map((id) => (id === WINDOW_CARD ? { id, card: true } : byName.get(id))).filter(Boolean);
    if (!secs.length) return;
    // A tab with a single section: its name and explanation go into the tab header, and the
    // section itself stays without a header of its own. With several sections, each has its
    // own header.
    const single = secs.length === 1 && !secs[0].card;
    const bare = single || !!t.flat;
    const panel = document.createElement("div");
    if (t.flat) panel.className = "flat";
    panel.innerHTML = secs.map((s) => (s.card ? windowCardHTML(bare) : section(s, bare))).join("");
    main.appendChild(panel);
    WIN.add({
      id: t.id, title: t.title, icon: t.icon, panel,
      order: (t.scope === "chat" ? 100 : 200) + n,
      group: t.scope, groupTitle: inChat ? GROUP[t.scope] || "" : "",
      lead: single ? secs[0].lead || "" : t.lead || "",
      onShow: () => mountWidgets(panel),
    });
  });
}

function windowCardHTML(bare = false) {
  return `
    <section class="card${bare ? " bare" : ""}" id="${WINDOW_CARD}" hidden>
      ${bare ? "" : "<h2>Window</h2>"}
      <div class="widget" data-widget="window-visibility"></div>
    </section>`;
}

// The "i" button at the section heading shows/hides its explanation. The expanded one is
// highlighted with the same neutral technique as the open header of the answer panel
// (`#ask-settings-btn[aria-pressed]` in index.html): lighter background and border, no
// accent. It is an open state, not an "important" one.
function bindInfo(btn) {
  const lead = byId(btn.getAttribute("aria-controls"));
  if (!lead) return;
  btn.addEventListener("click", () => {
    lead.hidden = !lead.hidden;
    btn.setAttribute("aria-expanded", String(!lead.hidden));
  });
}

/* The table of contents on the left. It is built from the same sections as the page: there
   is no second list of names and there must not be one. It would drift from the first at
   the first edit, and a person would look for a section that no longer exists. */
function renderSideNav() {
  const nav = $("#side-nav");
  if (!nav) return;
  nav.innerHTML = A.schema.sections.map((s) =>
    `<li><a href="#${esc(s.id)}" data-for="${esc(s.id)}">${esc(s.title)}</a></li>`
  ).join("");

  // The highlight is based on intersection with the top of the window, not on scrolling:
  // computing the position of each section on every scroll frame is work for nothing, and on
  // a long page it shows as lag.
  //
  // Without an observer we carry on silently. It exists everywhere the page really lives, but
  // `renderSideNav` is called from `renderSections`, and an exception thrown from here would
  // leave the person with an empty page instead of the form, just for the sake of something
  // that only underlines a list item.
  if (typeof IntersectionObserver === "undefined") return;

  const links = new Map($$("#side-nav a").map((a) => [a.dataset.for, a]));
  const seen = new Set();
  const spy = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) seen.add(e.target.id); else seen.delete(e.target.id);
    }
    const first = A.schema.sections.find((s) => seen.has(s.id));
    links.forEach((a, id) => a.classList.toggle("here", !!first && id === first.id));
  }, { rootMargin: "-100px 0px -60% 0px" });
  A.schema.sections.forEach((s) => {
    const el = byId(s.id);
    if (el) spy.observe(el);
  });
}

/* A dot next to a table-of-contents item: the section asks for attention. It is set by
   whoever knows the reason; right now that is the account block, when the user has not
   signed in. */
function markSide(id, on) {
  const a = $(`#side-nav a[data-for="${id}"]`);
  if (!a) return;
  const dot = $(".dot", a);
  if (on && !dot) a.insertAdjacentHTML("beforeend", `<span class="dot"></span>`);
  if (!on && dot) dot.remove();
}

// The "i" button: the same markup everywhere an explanation is hidden behind it, at the
// section heading (`section`, below) and at the field name (`field`, below). The
// explanation is not a separate line by itself: it is almost always unnecessary, but the
// space above a field or section is taken for everyone, including those who need no
// explanation. `bindInfo`, in `renderSections`, expands and hides it.
//
// `.ghost`: a shell that is transparent at rest. Without it, a square with a border (the
// plain `.iconbtn`, made for the header) broke the line of text with a heavy button in the
// middle of it. The circle with an "i" just stands at the end of the line and shows a
// border on hover, by the same technique as the "ghost" text button `.ghost` in chrome.css.
function infoBtn(forId) {
  return `<button type="button" class="iconbtn sm ghost info" aria-expanded="false"
      aria-controls="${forId}" aria-label="Info">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle>
      <line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
    </button>`;
}

// The settings window modeled on a reference app (2026-10-07; requirement: rework the
// settings window closer to that style): at the left of a row there is a tile with an icon,
// and under the name there is an explanation. The icon of a row is `data-ico`; the drawing
// itself is in `admin.css` (`.f[data-ico="…"]`), and here we only say which icon goes to
// which setting. A setting that is not listed here gets "sliders", not an empty space: the
// columns of the rows must not drift apart.
const ROW_ICON = {
  "channel.assist.prompting.tone": "smile",
  "channel.assist.materials.enabled": "clip",
  "channel.assist.profile.language": "lang",
  "realtime_stt.language": "mic",
  "realtime_stt.smart": "level",
  "channel.assist.answering.mic_mode": "mic",
  "channel.assist.answering.system_mode": "monitor",
  "channel.assist.answering.wake_word": "at",
  "channel.assist.answering.think_longer": "clock",
  "channel.assist.hotkeys.ask": "message",
  "channel.assist.hotkeys.hide_main": "window",
  "channel.assist.hotkeys.hide_panel": "panel",
  "channel.assist.hotkeys.ask_screen": "monitor",
  "channel.assist.hotkeys.recognition": "mic",
  "channel.assist.interface.language": "globe",
  "ui.main_taskbar_button": "window",
  "chat.name": "pen",
};

// The caption under the name of a row in the window (2026-10-08; requirement: if there is an
// icon, there must be only two lines of text on the row): exactly one line, up to 58
// characters (the test `test_settings_window.py`), while the full explanation stays behind
// the "i". It is the same technique as `ROW_ICON`: the look of a row in the window lives
// here, keyed by the setting path, and the schema on the server is not affected. Rows
// without an entry take the first phrase of the explanation (`hintParts`), and if it does
// not fit, `admin.css` cuts it with an ellipsis. This is a safety net, not a look to aim
// for: a long explanation must be given a caption here.
const ROW_CAPTION = {
  "chat.name": "You can leave it empty — it names itself.",
  "realtime_stt.smart": "Hears accents and unclear speech better, but costs more.",
  "channel.assist.answering.mic_mode": "For in-person meetings with one microphone for two.",
  "channel.assist.answering.system_mode": "Same, but for system audio.",
  "channel.assist.answering.wake_word": "The bot responds to this word.",
  "channel.assist.interface.language": "Language of buttons, labels, and hints.",
  "ui.main_taskbar_button": "Turn it off and it won’t show in screen sharing.",
};

// The explanation of a row. On the real `/admin` it is entirely behind the "i" button
// (`more`). In the window there is always one short line under the name (`first`), and the
// "i" button remains only on rows that have something to add, and it expands the whole
// explanation. A row with an icon has exactly two lines of text, the name and this caption
// (a rule, 2026-10-08), so the caption, if one is set (`ROW_CAPTION`, above), is taken as
// is; otherwise the first phrase of the explanation is used, which may be long, and then
// `admin.css` cuts it with an ellipsis. We do not cut an explanation with markup: the
// phrase boundary at a period cannot be found inside tags.
function hintParts(hint, caption = "") {
  const text = String(hint || "");
  if (!WIN) return { first: "", more: !!text };
  if (caption) return { first: caption, more: !!text };
  if (!text || text.includes("<")) return { first: text, more: false };
  const m = text.match(/^(.+?[.!?…])\s+(?=[«"“A-ZА-ЯЁ])/s);
  const first = m ? m[1] : text;
  return { first, more: first.length < text.length };
}

// `bare`: the section is the only one on a window tab. Its name and explanation are already
// in the tab header (`renderTabs`), and a header of its own would be a second copy of it.
function section(s, bare = false) {
  const badge = s.badge ? `<span class="badge ${badgeKind(s.badge)}">${esc(s.badge)}</span>` : "";
  const widget = s.widget ? `<div class="widget" data-widget="${esc(s.widget)}"></div>` : "";
  // In the window the explanation of a section stands right under the name (like "Audio
  // Settings" in the reference app), without the "i" button; on the real `/admin` it is
  // behind the button, as it was.
  const info = s.lead && !WIN ? infoBtn(`${s.id}-lead`) : "";
  // `hidden`: the setting is in `A.values` and is accepted by `apply()`, but it gets no row of
  // its own: the widget of the section draws it itself (see `Setting.hidden` in
  // `admin_base.py`).
  const shown = s.settings.filter((f) => !f.hidden);
  const head = bare ? "" : `
    <h2>${esc(s.title)}${badge}${info}</h2>
    ${s.lead ? `<p class="lead" id="${s.id}-lead"${WIN ? "" : " hidden"}>${s.lead}</p>` : ""}`;
  return `
  <section class="card${bare ? " bare" : ""}" id="${s.id}">${head}
    ${widget}
    ${shown.length ? `<div class="fields">${shown.map(field).join("")}</div>` : ""}
  </section>`;
}

function badgeKind(text) {
  const t = text.toLowerCase();
  if (t.includes("not done") || t.includes("none") || t.includes("planned")) return "todo";
  if (t.includes("working") || t.includes("ready")) return "on";
  return "";
}

const WIDE = new Set(["text", "chips"]);

function field(f) {
  const wide = WIDE.has(f.kind);
  const notes = [];
  if (f.todo) notes.push(`<span class="todo">⚑ ${esc(f.todo)}</span>`);
  if (!f.hot) notes.push(`<span class="cold">Applies after restart</span>`);

  // There may be no label at all. In a section with one field it is replaced by the section
  // heading, and a second copy of the same word under it is noise. An empty column in its
  // place would be worse than a label: the row would look shifted relative to its neighbors,
  // and the reason would not be visible.
  const hintId = `hint-${esc(f.path)}`;
  const { first, more } = hintParts(f.hint, ROW_CAPTION[f.path]);
  const lab = f.label || notes.length
    ? `<div class="lab">
         ${f.label ? `<div class="name">${esc(f.label)}${more ? infoBtn(hintId) : ""}</div>` : ""}
         ${first ? `<div class="desc">${first}</div>` : ""}
         ${notes.join(" ")}
       </div>`
    : "";
  // The icon tile appears only in the window and only on an ordinary row with a label.
  const ico = WIN && f.label && !wide ? ` data-ico="${ROW_ICON[f.path] || "sliders"}"` : "";

  // The row is in the form and takes part in apply() always: it is hidden only visually
  // while none of the `depends_on` conditions is met. Removing it from the markup would mean
  // that the value cannot be touched until you switch the mode back and forth, and it should
  // simply wait for its time. The conditions are a list, not a single pair: "Bot name"
  // must be shown if ANY of the speech sources (microphone, system sound) is set to
  // "by name", not just one particular source. It is JSON in an attribute, not
  // pairs of attributes: the list may be longer than one condition, and breeding
  // `data-depends-path-2` would be a second place that stores the same rule.
  const dep = f.depends_on
    ? ` data-depends-on="${esc(JSON.stringify(f.depends_on))}"`
    : "";
  return `
  <div class="f ${wide ? "wide" : ""} ${lab ? "" : "bare"} ${A.overridden.has(f.path) ? "over" : ""}"
       data-path="${esc(f.path)}" data-kind="${f.kind}"${ico}${dep}
       ${f.depends_on && !dependencyMet(f.depends_on) ? "hidden" : ""}>
    ${lab}
    <div class="ctl">${control(f)}</div>
    ${f.hint && more ? `<div class="hint" id="${hintId}" hidden>${f.hint}</div>` : ""}
    <div class="err" hidden></div>
  </div>`;
}

// `depends_on` is a list of `{path, value}` conditions joined by "or": the row is shown if
// at least one of them matches the current value (the unsaved one, taking A.pending into
// account). It is one function for both the first render (`field`) and the live redraw on
// every change (`stage`), so that the "when to show" rule does not drift apart in two
// places.
function dependencyMet(depends_on) {
  if (!depends_on || !depends_on.length) return true;
  return depends_on.some(({ path, value }) => {
    const v = path in A.pending ? A.pending[path] : A.values[path];
    return v === value;
  });
}

// Recheck all the rows with `depends_on`. It is called after every edit (`stage`, below),
// not only on load: if the mode changes, the neighboring row must appear or hide right
// away, without reloading the page.
function applyDependencies() {
  $$(".f[data-depends-on]").forEach((row) => {
    row.hidden = !dependencyMet(JSON.parse(row.dataset.dependsOn));
  });
}

function control(f) {
  const v = A.values[f.path];
  switch (f.kind) {
    case "bool":
      return `<div class="switch ${v ? "on" : ""}" role="switch" tabindex="0"></div>`;

    case "int":
    case "float": {
      const step = f.step ?? (f.kind === "int" ? 1 : 0.1);
      const slider = f.min !== null && f.max !== null
        ? `<input type="range" min="${f.min}" max="${f.max}" step="${step}" value="${num(v)}">`
        : "";
      return `<div class="range">${slider}
        <input type="number" step="${step}"
               ${f.min !== null ? `min="${f.min}"` : ""} ${f.max !== null ? `max="${f.max}"` : ""}
               value="${num(v)}"></div>
        ${f.unit ? `<span class="unit">${esc(f.unit)}</span>` : ""}`;
    }

    case "choice":
      return `<select>${f.options.map((o) =>
        `<option value="${esc(o.value)}" ${o.value === v ? "selected" : ""}>${esc(o.label)}</option>`
      ).join("")}</select>`;

    case "text":
      return `<textarea rows="${f.rows}" placeholder="${esc(f.placeholder)}">${esc(v ?? "")}</textarea>`;

    case "chips":
      return `<div class="chips">${(v || []).map(chip).join("")}
        <input type="text" placeholder="Add and press Enter"></div>`;

    case "secret":
      return `<input type="text" value="${esc(v ?? "")}" placeholder="${esc(f.placeholder)}">
              <span class="unit" data-env="${esc(v ?? "")}"></span>`;

    case "hotkey":
      return `<button class="hk" type="button">${esc(prettyCombo(v) || "not set")}</button>`;

    case "note":
      return "";

    default:
      return `<input type="text" value="${esc(v ?? "")}" placeholder="${esc(f.placeholder)}">`;
  }
}

const chip = (word) => `<span class="chip"><b>${esc(word)}</b><x>×</x></span>`;
const num = (v) => (v === null || v === undefined ? "" : v);

// --------------------------------------------------------------------------
// Input
// --------------------------------------------------------------------------

function bindField(el) {
  const path = el.dataset.path;
  const kind = el.dataset.kind;

  if (kind === "bool") {
    const sw = $(".switch", el);
    const flip = () => { sw.classList.toggle("on"); stage(path, sw.classList.contains("on")); };
    sw.addEventListener("click", flip);
    sw.addEventListener("keydown", (e) => {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); }
    });
    return;
  }

  if (kind === "int" || kind === "float") {
    const range = $("input[type=range]", el);
    const box = $("input[type=number]", el);
    // A slider and a number are one value in two forms. The slider sends the edit at once:
    // people find a threshold by ear, dragging back and forth. The field sends on losing
    // focus, otherwise "1" on the way to "15" would go to the pipeline as one.
    if (range) {
      range.addEventListener("input", () => { box.value = range.value; stage(path, cast(kind, box.value)); });
    }
    box.addEventListener("change", () => {
      if (range) range.value = box.value;
      stage(path, cast(kind, box.value));
    });
    return;
  }

  if (kind === "hotkey") {
    bindHotkey(el, path);
    return;
  }

  if (kind === "choice") {
    $("select", el).addEventListener("change", (e) => stage(path, e.target.value));
    return;
  }

  if (kind === "chips") {
    const box = $(".chips", el);
    box.addEventListener("click", (e) => {
      if (e.target.tagName !== "X") return;
      e.target.closest(".chip").remove();
      stage(path, readChips(box));
    });
    const input = $("input", box);
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !input.value.trim()) return;
      e.preventDefault();
      input.insertAdjacentHTML("beforebegin", chip(input.value.trim()));
      input.value = "";
      stage(path, readChips(box));
    });
    return;
  }

  const input = $("textarea, input[type=text]", el);
  if (input) input.addEventListener("input", () => stage(path, input.value));
}

// We take physical keys, not letters. On a Cyrillic layout `e.key` gives the Cyrillic "ф"
// instead of "a", and with alt held it gives a service character altogether; `e.code` stays
// `KeyA` on any layout. A combination is the PLACE of a key (by QWERTY), and the app itself
// translates it into a code for the current layout (`hotkeys.py`, `POSITION_SCAN`: on a
// German layout Z sits where Y is, on a French one A and Q are swapped). A letter taken from
// the key (`e.key`) must not be recorded here: it would be translated a second time and a
// different key would fire.
const HOTKEY_CODES = {
  Space: "space", Escape: "esc", Enter: "enter", Tab: "tab", Backspace: "backspace",
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  PageUp: "pgup", PageDown: "pgdn", Home: "home", End: "end",
  Insert: "insert", Delete: "delete",
};

// On macOS combinations are shown as key symbols (⌃⌥A), not as words. Settings keep the
// words, `ctrl+alt+a`, `cmd` instead of `win`; the app parses both spellings the same way,
// and only what the person sees changes.
const ON_MAC = /Mac/i.test(navigator.platform || "");
const MAC_SYMBOLS = { ctrl: "⌃", control: "⌃", alt: "⌥", option: "⌥", shift: "⇧", win: "⌘", cmd: "⌘", command: "⌘" };

// The key label is as it is printed on the keycap under the current layout. A combination is
// stored by the PLACE of the key (`q` is the key at the Q position on QWERTY), and on a
// French layout "A" stands at that place: without translation the button would show "Q",
// while the person has to press "A" (the same for Y and Z on a German layout). The "place ->
// letter" map comes from Chromium (`navigator.keyboard`); until it is available, and for
// non-Latin scripts (on a Cyrillic layout the key there is "ф"), the recorded name is kept.
let layoutMap = null;
function keyLabel(name) {
  if (!layoutMap) return name;
  const key = String(name).toLowerCase();
  const code = /^[a-z]$/.test(key) ? "Key" + key.toUpperCase() : /^[0-9]$/.test(key) ? "Digit" + key : "";
  const ch = code && layoutMap.get(code);
  return ch && /^[a-z0-9]$/i.test(ch) ? ch.toLowerCase() : name;
}
// The map is read when the page is built and again when the layout changes; the combination
// buttons are redrawn from it.
function loadLayout() {
  const kb = navigator.keyboard;
  if (!kb || !kb.getLayoutMap) return;
  kb.getLayoutMap().then((map) => {
    layoutMap = map;
    $$(".f[data-kind=hotkey]").forEach((row) => {
      const btn = $(".hk", row);
      if (btn.classList.contains("catching")) return;                // the person is pressing a combination right now: the button is busy
      const path = row.dataset.path;
      btn.textContent = prettyCombo(path in A.pending ? A.pending[path] : A.values[path]) || "not set";
    });
  }).catch(() => {});
}

function prettyCombo(combo) {
  if (!combo) return combo;
  const parts = String(combo).split("+").map((p) => p.trim());
  parts[parts.length - 1] = keyLabel(parts[parts.length - 1]);
  if (!ON_MAC) return parts.join("+");
  return parts.map((p) => MAC_SYMBOLS[p.toLowerCase()] || p.charAt(0).toUpperCase() + p.slice(1)).join("");
}

function hotkeyName(code) {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code.toLowerCase();
  return HOTKEY_CODES[code] || "";
}

// Assigning a combination happens one at a time (2026-10-08; bug report: in the hotkeys it
// was possible to select all of them at once). Each button has its own state, and pressing
// the second one did not release the first. All the "catching" buttons listened to the
// keyboard at once, and a single keypress was recorded into all of them. Now the start of a
// new assignment ends the previous one, and a click outside the buttons cancels the current
// one.
let activeHotkey = null;   // {finish} of the button that is currently catching a combination

function bindHotkey(el, path) {
  const btn = $(".hk", el);
  const shown = () => prettyCombo(path in A.pending ? A.pending[path] : A.values[path]) || "not set";
  let catching = false;
  const self = { finish: (keepHold) => finish(keepHold) };

  // While a combination is being assigned, the app releases its own combinations. Without
  // this, "ctrl+alt+a" could not be assigned again: the system gives the keypress to whoever
  // registered it, so it never reaches the page, and instead of an assignment the "ask"
  // action fires. We wait for the response: listening to the keyboard before the registration
  // is released means losing the race to the very first keypress. `keepalive`: the "give
  // back" request must get through even if the page is being closed right now (`beforeunload`
  // below), otherwise the app would be left without its combinations (the app also returns
  // them by itself, on a timer, but until then they are silent).
  const hold = (on) => fetch("/admin/hotkeys/capture", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on }), keepalive: true,
  }).catch(() => {});

  // `keepHold`: a move to another button. The combinations of the app stay released, and the
  // "give back" request is not needed (and could overtake the "release" request from the next
  // button).
  const finish = (keepHold = false) => {
    catching = false;
    btn.classList.remove("catching");
    btn.textContent = shown();
    window.removeEventListener("keydown", onKey, true);
    if (activeHotkey === self) activeHotkey = null;
    if (!keepHold) hold(false);
  };

  function onKey(e) {
    if (!catching) return;
    e.preventDefault();
    e.stopPropagation();

    const bare = !(e.ctrlKey || e.altKey || e.shiftKey || e.metaKey);
    if (bare && e.code === "Escape") return finish();
    // Clearing a combination works the same way as erasing text. The app understands an empty
    // value as "I don't need this".
    if (bare && (e.code === "Delete" || e.code === "Backspace")) {
      stage(path, "");
      return finish();
    }

    const name = hotkeyName(e.code);
    if (!name) return;             // one modifier is pressed so far: we wait for the key

    const mods = [];
    if (e.ctrlKey) mods.push("ctrl");
    if (e.altKey) mods.push("alt");
    if (e.shiftKey) mods.push("shift");
    if (e.metaKey) mods.push(ON_MAC ? "cmd" : "win");
    // Without a modifier the combination would take the key away from the whole system:
    // pressing "a" in someone else's window would become impossible. Shift does not count as a
    // modifier: shift+a is a capital "A", and shift+space, shift+1 ("!"), and shift+enter are
    // ordinary input too, and a combination with them would take them away from every program.
    // With shift alone only F-keys are acceptable.
    const strong = e.ctrlKey || e.altKey || e.metaKey;
    if (!strong && !(e.shiftKey && /^f\d+$/.test(name))) {
      btn.textContent = ON_MAC ? "Needs ⌃, ⌥, or ⌘" : "Needs ctrl, alt, or win";
      return;
    }
    // AltGr: on Polish, Czech, German, Turkish, and many other layouts, ctrl+alt+letter (which
    // is also AltGr+letter) types "ą", "ś", "@"... The combination would take that letter away
    // from the whole system, and the paid "ask" action would fire instead of it. The app checks
    // this on its side too (`hotkeys.py`), but we tell the person at once, on the button.
    if (e.getModifierState && e.getModifierState("AltGraph") && /^[a-z0-9]$/.test(name)) {
      btn.textContent = "AltGr types a letter — pick another";
      return;
    }

    stage(path, [...mods, name].join("+"));
    finish();
  }

  btn.addEventListener("click", () => {
    if (catching) return finish();
    if (activeHotkey) activeHotkey.finish(true);
    activeHotkey = self;
    catching = true;
    btn.classList.add("catching");
    btn.textContent = "Press a combo…";
    hold(true).then(() => window.addEventListener("keydown", onKey, true));
  });

  // Leaving the page in the middle of an assignment would leave the app without hotkeys until
  // a restart.
  window.addEventListener("beforeunload", () => { if (catching) hold(false); });
}

// A click outside the combination buttons cancels the assignment in progress: otherwise the
// button would stay at "Press a combo…", and the keyboard would stay
// busy until Esc is pressed.
document.addEventListener("mousedown", (e) => {
  if (activeHotkey && !(e.target.closest && e.target.closest(".hk"))) activeHotkey.finish();
}, true);

const readChips = (box) => $$(".chip b", box).map((b) => b.textContent);
const cast = (kind, raw) => (raw === "" ? null : kind === "int" ? parseInt(raw, 10) : parseFloat(raw));

function stage(path, value) {
  const same = JSON.stringify(value) === JSON.stringify(A.values[path]);
  // While a save is in flight, "equal to what the app has" does not yet mean "nothing to
  // edit": the response will bring back what was sent, and a person who restored the previous
  // value without waiting for the response would be left with the field rolled back.
  if (same && !saving) delete A.pending[path]; else A.pending[path] = value;
  delete A.failed[path];       // the field was touched: the earlier refusal was about a different value
  const row = $(`.f[data-path="${cssPath(path)}"]`);
  if (row) {
    const err = $(".err", row);
    err.hidden = true;
  }
  applyDependencies();
  refreshBars();
  scheduleSave();
}

// It saves by itself, without a button, after a short pause following the last edit, not on
// every keystroke. Free text is typed character by character, a slider is dragged frame by
// frame: sending each of those steps to the server would run the pipeline through a pile of
// obviously intermediate states for a value that is about to be overwritten by the next one
// anyway. It is the same `A.pending`/`save()` as before; only the trigger for calling it
// has changed.
let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 700);
}
// Force an immediate save: on Ctrl+S and before the window/panel closes, so that an edit
// made inside the pause is not silently lost.
function flushSave() {
  clearTimeout(saveTimer);
  saveTimer = null;
  return save();
}

const cssPath = (p) => p.replace(/"/g, '\\"');

// --------------------------------------------------------------------------
// Saving
// --------------------------------------------------------------------------

// One save at a time. A second `save()` does not open its own request in the middle of a
// running one: two POSTs with overlapping sets would overtake each other, and the response
// of the first would erase an edit made while it was in flight. The second asks the running
// one to go another round when it finishes, and waits on the same promise (closing the
// window and Ctrl+S wait for the real end).
let saving = null;
let again = false;

function save() {
  if (saving) { again = true; return saving; }
  saving = (async () => {
    try {
      // A safeguard against looping: every round either saves or removes the rejected field from
      // the queue.
      for (let round = 0; round < 6; round++) {
        again = false;
        const next = await saveOnce();
        if (next !== "retry" && !again) break;
      }
    } finally {
      saving = null;
    }
    // An edit made while the request was in flight stayed in the queue: it will go after the
    // usual pause and will not be lost.
    if (Object.keys(A.pending).length && !A.problem && !A.stale) scheduleSave();
  })();
  return saving;
}

// The request must outlive the page: people move between screens by a link ("←" to the
// list of sessions), `beforeunload` below flushes an edit that did not wait for the pause,
// and without `keepalive` the departing page would abort the request on the fly. The
// browser accepts such a request up to 64 KB and counts BYTES, not characters: Cyrillic in
// UTF-8 is twice as long, and a long session context (30-35 thousand characters) exceeded
// the limit, so the request was rejected on the spot, and the edit was not saved and did
// not say why. If it did not fit or did not arrive, try again with a normal request.
const KEEPALIVE_MAX = 60000;
async function postApply(payload) {
  const init = { method: "POST", headers: { "Content-Type": "application/json" }, body: payload };
  let r = null;
  if (new TextEncoder().encode(payload).length < KEEPALIVE_MAX) {
    try { r = await fetch("/admin/apply", { ...init, keepalive: true }); } catch (err) { r = null; }
  }
  if (!r) r = await fetch("/admin/apply", init);
  try { return await r.json(); } catch (err) { return null; }
}

// One request with whatever has accumulated. It returns "ok" if the app accepted it;
// "retry" if some fields were rejected and the others are still in the queue and must go
// right away; "failed" if it did not work: the queue is intact and will be repeated with
// the next edit.
async function saveOnce() {
  if (A.stale) return "failed";
  const sent = { ...A.pending };
  if (!Object.keys(sent).length) return "ok";
  const body = { values: sent };
  if (A.chat !== undefined) body.chat = A.chat;
  let res;
  try {
    res = await postApply(JSON.stringify(body));
  } catch (err) {
    A.problem = "can't reach the app";
    refreshBars();
    return "failed";
  }
  if (res && res.stale) { markStale(); return "failed"; }
  if (!res || !res.ok) return rejected(res, sent);
  A.problem = "";
  adopt(res, sent);
  return "ok";
}

function showError(path, message) {
  const row = $(`.f[data-path="${cssPath(path)}"]`);
  if (!row) return;
  const err = $(".err", row);
  err.textContent = message;
  err.hidden = false;
}

// The app applies a set as a whole or not at all: one unsuitable value (an empty session
// name) would reject all the other edits from all the tabs too, every time while it sits in
// the queue, and the message would be shown only at its own row, which is not visible when
// another tab is open. The fields it pointed to leave the queue (the message goes under the
// field and into the "not saved" bar), and the rest are sent right after.
function rejected(res, sent) {
  const errors = (res && res.errors) || {};
  let handled = 0;
  for (const [path, message] of Object.entries(errors)) {
    if (!(path in sent)) continue;                                    // not ours: it concerns something we did not send
    handled++;
    if (JSON.stringify(A.pending[path]) !== JSON.stringify(sent[path])) continue;   // it was fixed in time: there is no refusal for the new value
    delete A.pending[path];
    A.failed[path] = String(message);
    showError(path, message);
  }
  if (!handled) {
    // A refusal that is not tied to fields (a malformed request, an error in the app): there is
    // nothing to name, and the queue is intact.
    A.problem = String((res && (res.detail || res.error)) || "unexpected reply from the app");
    refreshBars();
    return "failed";
  }
  A.problem = "";
  refreshBars();
  showFailed();
  return Object.keys(A.pending).length ? "retry" : "failed";
}

// The rejected field sits on a tab that is not visible now, so we switch to it: otherwise
// the message under the field would stay where the person no longer looks (the "not saved"
// bar says what is wrong, but not where). If the window was closed
// without waiting, the next time it opens it will meet the person on this tab.
function showFailed() {
  if (!WIN) return;
  const path = Object.keys(A.failed)[0];
  const row = path && $(`.f[data-path="${cssPath(path)}"]`);
  const panel = row && row.closest(".win-panel");
  if (!panel || !panel.hidden) return;                           // the field is in view anyway
  WIN.want = panel.id.replace(/^wpanel-/, "");
  WIN.show();
}

// The page is open for one session, but the app now has another (or none): writing from
// here is not allowed, because the context of one session would land in another, and with
// no session the session settings would end up in the shared file.
function markStale() {
  A.stale = true;
  A.pending = {};
  A.problem = "the app already has another session open — refresh the page, nothing will be saved from here";
  refreshBars();
}

// `sent` is what went out in this request. Only that is removed from the queue, and only
// if it has not been touched since: an edit made while the request was in flight stays and
// is not erased by the response (before, `A.pending = {}` erased everything, and the fields
// were repainted with the old value from the server).
function adopt(res, sent = {}) {
  A.values = res.values || A.values;
  A.overridden = new Set(res.overridden || []);
  A.restart = new Set(res.restart || []);
  for (const path of Object.keys(A.pending)) {
    const mine = JSON.stringify(A.pending[path]);
    if ((path in sent && mine === JSON.stringify(sent[path])) || mine === JSON.stringify(A.values[path])) {
      delete A.pending[path];
    }
  }
  // Only our own rows. A `.f` row is drawn both by the scaffold and by a channel block (there
  // is one grid on the page), but the error node and the setting path exist only on the rows
  // that the scaffold drew itself, inside `.fields`. Walking over all `.f` in a row tripped on
  // the widget row and did not reach the end: the save went to the server, while the "not saved"
  // bar stayed hanging.
  $$(".fields .f").forEach((row) => {
    const path = row.dataset.path;
    row.classList.toggle("over", A.overridden.has(path));
    // A rejected field keeps both the message and what the person typed into it: we erase
    // neither. A field with an edit that has not gone out yet is not rolled back to the old
    // value.
    if (path in A.failed || path in A.pending) return;
    $(".err", row).hidden = true;
    syncControl(row);
  });
  applyDependencies();
  refreshBars();
  A.live.forEach((fn) => { try { fn(A.snapshot, A.values); } catch (e) { console.error(e); } });
}

// The window was reopened (`chrome.js`): the values of the app may have changed while it
// was closed (the popup at the question field ("Think longer", microphone,
// language), another browser tab), while the window holds a copy from the first read. We
// reread the schema and update the fields, except those that the person edited and that
// have not gone out yet. We do not wait for a running save: its response brings fresh
// values itself. If the session the page is open for is no longer active, writing from here
// is not allowed (`markStale`).
async function refresh() {
  if (!A.schema || saving) return;
  const scope = document.body.dataset.settingsScope || "";
  const res = await fetch("/admin/schema" + (scope ? `?scope=${encodeURIComponent(scope)}` : ""));
  const fresh = await res.json();
  if (!fresh || !fresh.values || saving) return;
  if (A.chat !== undefined && "chat" in fresh && fresh.chat !== A.chat) { markStale(); return; }
  adopt({ values: fresh.values, overridden: fresh.overridden, restart: fresh.restart_pending });
}

function syncControl(row) {
  const v = A.values[row.dataset.path];
  switch (row.dataset.kind) {
    case "bool": $(".switch", row).classList.toggle("on", !!v); break;
    case "int": case "float": {
      const box = $("input[type=number]", row); const range = $("input[type=range]", row);
      if (box) box.value = num(v);
      if (range) range.value = num(v);
      break;
    }
    case "choice": $("select", row).value = v; break;
    case "hotkey": $(".hk", row).textContent = prettyCombo(v) || "not set"; break;
    case "chips": {
      const box = $(".chips", row);
      $$(".chip", box).forEach((c) => c.remove());
      const input = $("input", box);
      (v || []).forEach((w) => input.insertAdjacentHTML("beforebegin", chip(w)));
      break;
    }
    case "note": break;
    default: {
      const input = $("textarea, input[type=text]", row);
      // We do not touch the field that is being typed in right now: overwriting would reset the
      // cursor to the start on every server response.
      if (input && document.activeElement !== input) input.value = v ?? "";
    }
  }
}

function bindBars() {
  // Force a save at once, without waiting for the pause: the same meaning that the click on
  // "Save" used to have.
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); flushSave(); }
  });
  // The best that can be done when a browser tab closes: sending a request that may not
  // arrive is still better than saying nothing. The settings panel inside the app is closed
  // differently, not by unloading but with `hidden` (`shutSheets` in chrome.js), and the same
  // place calls this `flushSave` directly, before the panel hides.
  window.addEventListener("beforeunload", () => { if (saveTimer) flushSave(); });
  refreshBars();
}

const labelOf = (path) => {
  for (const s of A.schema.sections) {
    for (const f of s.settings) if (f.path === path) return f.label || "";
  }
  return "";
};

function refreshBars() {
  const restart = [...A.restart];
  $("#restart").hidden = restart.length === 0;
  $("#restart-n").textContent = restart.join(", ");
  // "not saved": one line per rejected field (name and reason) and a general
  // reason if saving failed altogether. Until the field is fixed or a save goes through, the
  // bar stands above all the tabs.
  const lines = Object.entries(A.failed).map(([path, message]) => {
    const label = labelOf(path);
    return label ? `«${label}» — ${message}` : message;
  });
  if (A.problem) lines.push(A.problem);
  const bar = $("#unsaved");
  if (bar) {
    bar.hidden = lines.length === 0;
    $("#unsaved-n").textContent = lines.join("; ");
  }
}

// --------------------------------------------------------------------------
// Live numbers
// --------------------------------------------------------------------------

function connectLive() {
  // The same stream that feeds the dashboard. We do not start our own: a setting without
  // numbers is guesswork, and a second source of the same numbers would sooner or later drift
  // from the first.
  let ws;
  const open = () => {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (e) => {
      A.snapshot = JSON.parse(e.data);
      syncChatName(A.snapshot.chat);
      offlineBanner(A.snapshot);
      A.live.forEach((fn) => { try { fn(A.snapshot, A.values); } catch (err) { console.error(err); } });
    };
    ws.onclose = () => setTimeout(open, 1500);
  };
  open();
}

// The session name on the server may change by itself: the person skipped it, and it was
// invented from the description while the panel was open. The field in the panel would then
// stay empty with the placeholder "it will be made up by itself",
// although the name already exists. We pull it from the snapshot if the person is not
// editing this field and is not typing in it right now.
function syncChatName(chat) {
  const path = "chat.name";
  if (!chat || !(path in A.values) || path in A.pending || path in A.failed) return;
  const want = chat.unnamed ? "" : chat.name;
  if (A.values[path] === want) return;
  const row = $(`.f[data-path="${cssPath(path)}"]`);
  const input = row && $("input", row);
  if (input && document.activeElement === input) return;
  A.values[path] = want;
  if (row) syncControl(row);
}

// The "pipeline not running" bar. It is drawn here, not in the
// channel page: the warning is shared by all three channels, and three copies of one text
// would drift apart at the first edit.
//
// Without it, the settings mode looks like a breakage: the levels are zero, there are no
// utterances, the latency is zero, and all of that looks like a measurement, not like a
// capture that is switched off.
function offlineBanner(snapshot) {
  const main = $("#main");
  if (!main) return;
  const shown = $("#offline");
  if (snapshot.capture !== false) { if (shown) shown.remove(); return; }
  if (shown) return;
  const box = document.createElement("div");
  box.id = "offline";
  box.className = "note warn";
  box.style.margin = WIN ? "0 0 22px" : "0 0 4px";
  box.innerHTML = "<b>Recording isn't running</b> — only the settings are open. "
    + "They save as usual, but there is no live data: no sound levels, "
    + "no recognized lines, no headphone check.";
  main.prepend(box);
}

// --------------------------------------------------------------------------
// Channel blocks
// --------------------------------------------------------------------------

// `root` is where to look for the blocks: the whole page (the default) or one window tab
// that has just been opened for the first time.
function mountWidgets(root) {
  const api = {
    get: (path) => (path in A.pending ? A.pending[path] : A.values[path]),
    // A snapshot of all the values at once, with the unsaved edit on top: what `get` already
    // does for one path. It is needed by blocks that do not know in advance which paths to ask
    // for (a voice per character is added not by the form here but by
    // `argus sync-voices`/`import-voice`) and that read them from the shared snapshot by prefix
    // instead of calling `get` in a loop over guessed names.
    values: () => ({ ...A.values, ...A.pending }),
    set: (path, value) => {
      stage(path, value);
      const row = $(`.f[data-path="${cssPath(path)}"]`);
      if (row) syncPending(row, value);
    },
    save,
    onLive: (fn) => { A.live.push(fn); if (A.snapshot) fn(A.snapshot, A.values); },
    post: (url, body) => fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json()),
    get_: (url) => fetch(url).then((r) => r.json()),
    // Mark its own item in the table of contents. Only the block itself knows about it: "needs
    // attention" is its state, not a property of the section.
    mark: markSide,
    esc,
  };
  $$(".widget", root).forEach((el) => {
    const fn = window.adminWidgets[el.dataset.widget];
    if (!fn) { el.innerHTML = `<div class="note bad">block “${esc(el.dataset.widget)}” isn't defined</div>`; return; }
    try { fn(el, api); } catch (e) { console.error(e); el.innerHTML = `<div class="note bad">${esc(e)}</div>`; }
  });
}

function syncPending(row, value) {
  switch (row.dataset.kind) {
    case "bool": $(".switch", row).classList.toggle("on", !!value); break;
    case "int": case "float": {
      const box = $("input[type=number]", row); const range = $("input[type=range]", row);
      if (box) box.value = num(value);
      if (range) range.value = num(value);
      break;
    }
    case "choice": $("select", row).value = value; break;
    case "hotkey": $(".hk", row).textContent = prettyCombo(value) || "not set"; break;
    default: {
      const input = $("textarea, input[type=text]", row);
      if (input) input.value = value ?? "";
    }
  }
}

// --------------------------------------------------------------------------
// Channel blocks (assist). They used to be in `admin.html` and moved here together with the
// scaffold: the dashboard page mounts exactly this file, and we do not keep a second one
// with the same content.
// --------------------------------------------------------------------------

// Why the model is silent
window.adminWidgets["gate-live"] = (el, api) => {
  el.innerHTML = `
    <div class="tiles" style="padding:0 0 10px" id="gl-tiles"></div>
    <div class="note" id="gl-last">The filter hasn't decided anything yet</div>`;
  const tiles = el.querySelector("#gl-tiles");
  const last = el.querySelector("#gl-last");

  api.onLive((s) => {
    if (!s) return;
    const g = s.gate || {};
    const order = ["requests", "filtered out", "chatter", "too short", "not a question", "pause", "repeat"];
    tiles.innerHTML = order.filter((k) => k in g).map((k) =>
      `<div class="tile ${k === "requests" ? "ok" : ""}">
         <div class="k">${k}</div><div class="v">${g[k]}</div></div>`).join("");
    const reason = s.last_decision || "";
    last.innerHTML = reason
      ? `last decision: <b>${api.esc(reason)}</b>`
      : "The filter hasn't decided anything yet";
  });
};

// Materials: what lies in the folder and what it costs.
//
// Files go as the request body one at a time, not as a form and not as a batch. One at a
// time because a hundred-megabyte lecture takes noticeable time to upload, and the person
// should see which one is going now, not a shared bar without names.
window.adminWidgets["materials"] = (el, api) => {
  el.innerHTML = `
    <label class="mt-drop" id="mt-drop">
      <input type="file" id="mt-file" multiple
             accept=".pdf,.docx,.pptx,.txt,.md,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff">
      <span class="ico" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 16V4M7.5 8.5 12 4l4.5 4.5"/><path d="M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"/></svg></span>
      <span class="t">Drop files or click</span>
      <span class="dim">PDF, Word, PowerPoint, text, photos of pages</span>
    </label>
    <div id="mt-list"></div>
    <div class="dim" style="font-size:12.5px" id="mt-sum"></div>`;

  const drop = el.querySelector("#mt-drop");
  const input = el.querySelector("#mt-file");
  const list = el.querySelector("#mt-list");
  const sum = el.querySelector("#mt-sum");
  let lastNotes = "";   // what the line under the list says about the materials (without upload errors)

  // A file row is an icon, a name, and a cross, and that is all: the size, the number of
  // characters and pages, and the price in tokens tell the person nothing (requirement: this
  // kind of information should not be written at all). The exceptions are the things the
  // person can influence: a file was not read (the reason goes under the name) and some of
  // the materials did not fit (one line without numbers, below). Silently cutting would be a
  // lie about what the model answers from.
  const FILE = '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>';
  const CROSS = '<path d="M6 6l12 12M18 6L6 18"/>';
  const show = (r) => {
    list.innerHTML = (r.items || []).map((i) => `<div class="mt-row${i.error ? " bad" : ""}">
        <span class="ico" aria-hidden="true"><svg viewBox="0 0 24 24">${FILE}</svg></span>
        <span class="nm"><span class="fn" data-i18n="off">${api.esc(i.name)}</span>${
          i.error ? `<span class="why">${api.esc(i.error)}</span>` : ""}</span>
        <button class="iconbtn act x" data-name="${api.esc(i.name)}" aria-label="Remove"><svg viewBox="0 0 24 24">${CROSS}</svg></button>
      </div>`).join("");

    const notes = [];
    if (r.dropped || r.dropped_pages) notes.push(`<span class="bad">Not everything fit — some materials are not being used</span>`);
    if (!r.enabled && r.items && r.items.length) notes.push("<b>currently off</b>");
    lastNotes = notes.join(" · ");
    sum.innerHTML = lastNotes;
  };

  const load = () => fetch("/admin/assist/materials").then((x) => x.json()).then(show);

  // The same limits as the app has (`MAX_UPLOAD` in `ui/admin.py`, 100 MB, and the extensions
  // from `accept` above): a file that will be rejected anyway should not first travel over the
  // network and sit in memory in full.
  const MAX_BYTES = 100 * 1024 * 1024;
  const ALLOWED = /\.(pdf|docx|pptx|txt|md|png|jpe?g|webp|bmp|tiff?)$/i;

  const send = async (files) => {
    // The list is copied at once: `input.files` is live, and `input.value = ""` in the handler
    // below empties it in Chromium and WebKit before the loop reaches the second file, so only
    // the first one went out, without a single error. Drag and drop (`dataTransfer.files`) did
    // not suffer from this: it has a separate snapshot.
    const queue = [...files];
    const bad = [];   // "name - reason" for each file that failed: the next "Parsing…" label does
                      // not erase them
    for (const file of queue) {
      if (!ALLOWED.test(file.name)) { bad.push(`“${file.name}” — this file type can't be read`); continue; }
      if (file.size > MAX_BYTES) { bad.push(`“${file.name}” — larger than 100 MB`); continue; }
      sum.innerHTML = `Parsing “${api.esc(file.name)}”…`;
      try {
        const r = await fetch(
          "/admin/assist/materials?name=" + encodeURIComponent(file.name),
          { method: "POST", body: file },
        ).then((x) => x.json());
        if (!r.ok) { bad.push(`“${file.name}” — ${r.error || "couldn't parse"}`); continue; }
        show(r);
      } catch (err) {
        // The file itself could not be read (a folder was dropped), the app does not answer or
        // answered something unexpected: the other files go out anyway, and the "Parsing…"
        // label does not hang forever.
        bad.push(`“${file.name}” — couldn't be uploaded`);
      }
    }
    sum.innerHTML = [lastNotes, ...bad.map((m) => `<span class="bad">${api.esc(m)}</span>`)].filter(Boolean).join(" · ");
  };

  input.addEventListener("change", () => { send(input.files); input.value = ""; });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    send(e.dataTransfer.files);
  });

  // Deletion goes out at once, bypassing "save", like the microphone choice next to it. It is
  // not a setting but an action on a file: accumulating it until a button press would mean
  // showing a list that no longer exists on disk.
  list.addEventListener("click", async (e) => {
    const button = e.target.closest(".x");
    if (!button) return;
    show(await api.post("/admin/assist/materials/delete", { name: button.dataset.name }));
  });

  load();
};

window.adminWidgets["audio-check"] = (el, api) => {
  el.innerHTML = `
    <div class="f" id="ac-mic-row" data-ico="mic">
      <div class="lab"><div class="name">Microphone</div></div>
      <div class="ctl"><select id="ac-mic" aria-label="Microphone"></select></div>
    </div>
    <div class="f" id="ac-mic-meter-row" data-ico="level">
      <div class="lab"><div class="name">Current level</div></div>
      <div class="ctl"><div class="track"><div class="fill" id="ac-mic-level"></div></div></div>
    </div>`;

  const at = (id) => el.querySelector("#" + id);
  const mic = at("ac-mic");
  const meterRow = at("ac-mic-meter-row");
  const meter = at("ac-mic-level");

  let busy = false;      // reconnecting is in progress
  let offline = false;   // the pipeline is not running: settings mode
  // There is no "Check" button any more (requirement: remove it, 2026-10-08): the
  // level is always shown while the pipeline runs, so the check needs no click. Without the
  // pipeline (settings mode) there are no live numbers, and a silent zero level would look
  // like a measurement rather than like having no numbers at all, so we hide the row.
  const sync = () => {
    mic.disabled = busy || offline;
    meterRow.hidden = offline;
  };

  // The same conversion to decibels as in the volume bars on the dashboard (`level()` in
  // index.html): on a linear scale speech would look like a flicker of zero. We recompute it
  // here instead of moving it to a shared place: the pages are different, and there is no
  // shared file between them for this one formula.
  const level = (rms) => {
    if (!rms || rms <= 1e-9) return 0;
    return Math.max(0, Math.min(1, (20 * Math.log10(rms) + 60) / 60));
  };

  // The list is reread on every opening, not once on load: a wireless headset appears and
  // disappears by itself, and a list taken five minutes ago would show devices that are no
  // longer there.
  const loadMics = async () => {
    const r = await fetch("/audio/devices").then((x) => x.json());
    const options = [
      `<option value="">system default</option>`,
      ...r.inputs.map((d) =>
        `<option value="${api.esc(d.name)}">${api.esc(d.name)}${d.default ? " · system default" : ""}</option>`),
    ];
    // We are listening to a device that is not in the list: it disappeared on the fly. Silently
    // substituting the first one at hand would lie about what is happening with the sound right
    // now.
    if (r.current && !r.inputs.some((d) => d.name === r.current)) {
      options.push(`<option value="${api.esc(r.current)}">${api.esc(r.current)} · missing</option>`);
    }
    mic.innerHTML = options.join("");
    // The pinned microphone may be missing from the list (a wireless headset fell asleep or is
    // switched off): the app then listens to the system one (`find_microphone` in
    // capture/audio.py), and the list shows the same, "system default",
    // not an empty place (requirement: show the system default when the pinned one is
    // missing instead of an empty list). That the pinned one has disappeared is reported by the
    // complaint at the top of the page. The name in the config may be a part of the device name,
    // and we search for it by the same rule.
    const find = (spec) => r.inputs.find((d) => d.name === spec)
      || r.inputs.find((d) => d.name.toLowerCase().includes(spec.toLowerCase()));
    let chosen = "";
    if (typeof r.pinned === "number") chosen = r.current || "";           // a number is pinned, not a name
    else if (r.pinned) chosen = (find(String(r.pinned)) || {}).name || "";
    else chosen = r.current || "";
    mic.value = chosen;
    if (mic.selectedIndex < 0) mic.selectedIndex = 0;                     // "system default"
  };
  // Before the list is shown, not next to the click: the popup (`openCombo` in chrome.js)
  // waits for this promise and builds the list from fresh devices. A separate `mousedown`
  // listener read them in parallel with building the popup: a headset inserted since the last
  // time was missing from the first showing, and a removed one was still offered.
  mic._beforeOpen = loadMics;

  // The choice goes out at once, bypassing "save", and this is not a departure from the rule
  // "accumulate and apply at once". The other fields describe how to work; this is an attempt
  // to fix the sound here and now. Accumulating it until a button press would mean keeping
  // the sound broken until the next save. The choice is still written to the same file by the
  // same path.
  mic.addEventListener("change", async () => {
    busy = true; sync();
    try {
      await api.post("/control", { mic_device: mic.value });
    } finally {
      busy = false; sync(); loadMics();
    }
  });

  // Complaints (the window disappeared, the device is unavailable) no longer come here: the
  // same line already pops up as a banner on the dashboard itself (`s.problems` in
  // index.html). Here it was a duplicate that also did not go away by itself: the window title
  // in a pinned source changes on the fly (the active file in an IDE, the active browser tab),
  // and "window not found" hung almost permanently where nothing was
  // actually broken.
  api.onLive((s) => {
    if (!s) return;
    offline = s.capture === false;
    sync();
    meter.style.width = level(s.levels?.me?.rms) * 100 + "%";
  });

  loadMics();
};

// The "Window now" toggle: hide/return the main window of the app within the
// same second, not through a key combination. It is ordinary minimizing, `window.hide()` of
// pywebview, not "invisible in capture": a hidden window disappears for the person too,
// exactly like any minimized program. The state does not live in `channel.assist.*` and
// does not go through `stage`/"save": it is not a setting for the future but a command
// right now, the same meaning as the opacity knob on the panel itself.
window.adminWidgets["window-visibility"] = (el, api) => {
  // On the real `/admin` the card sits in the markup hidden, next to the main tag (see
  // `shellHTML` above), not inside it: `renderSections()` erases main completely each time
  // and would draw it again from the schema, of which this card is not a part. We move it
  // here, first, and then it gets the same padding as the other cards (`gap` of main in
  // `admin.css`), without a separate calculation on the side. In the window it is already on
  // its own tab ("General"), and it only needs to be shown.
  const card = el.closest("section.card");
  const main = byId("main");
  if (card) {
    if (main && !card.closest(".win-panel")) main.prepend(card);
    card.hidden = false;
  }

  // On macOS the "do not show in the recording" flag is set the same way, but not all
  // screen-sharing programs on newer systems (from macOS 15) honor it. It is more honest to
  // say so at once than to promise something that cannot be verified.
  const onMac = /Mac/i.test(navigator.platform || "");
  const wcHint = onMac
    ? "You can see the window, but it won't show up in screen sharing, streams, or recordings. On macOS 15 and newer this doesn't work in every screen-sharing app — check on your own setup. You can hide the window itself with the “Open/close main window” hotkey in the “Hotkeys” section."
    : "You can see the window, but it won't show up in screen sharing, streams, or recordings (Windows 10 2004+). You can hide the window itself with the “Open/close main window” hotkey in the “Hotkeys” section.";
  const wc = hintParts(wcHint, "Visible to you, hidden in sharing and recordings.");
  el.innerHTML = `
    <div class="f" data-ico="eye-off">
      <div class="lab"><div class="name">Invisible in recordings${wc.more ? infoBtn("wc-hint") : ""}</div>
        ${wc.first ? `<div class="desc">${wc.first}</div>` : ""}</div>
      <div class="ctl">
        <div class="switch" id="wc-switch" role="switch" tabindex="0"></div>
      </div>
      ${wc.more ? `<div class="hint" id="wc-hint" hidden>${wcHint}</div>` : ""}
      <div class="err" id="wc-err" hidden></div>
    </div>`;

  el.querySelectorAll(".info").forEach(bindInfo);

  // The window stays on your screen in any case, so there is no delay and no toast: the
  // settings page itself is in no danger of hiding here, and the click can be undone right
  // away.
  const csw = el.querySelector("#wc-switch");
  let cAvailable = true;

  const cPaint = (excluded) => {
    csw.classList.toggle("on", excluded);
  };

  // The person must see the same refusal: the switch promises "Invisible in recordings", and if the app could not hide the window, we must not stay
  // silent about it (before, on failure nothing changed, neither the switch nor the label).
  // The switch is redrawn according to what is actually the case (`r.excluded`), not
  // according to the click.
  const cErr = el.querySelector("#wc-err");
  const cSend = async (excluded) => {
    if (!cAvailable) return;
    let r;
    try {
      r = await api.post("/window/capture", { excluded });
    } catch (err) {
      r = { ok: false, detail: "can't reach the app" };
    }
    if (typeof r.excluded === "boolean") cPaint(r.excluded);
    cErr.textContent = r.ok ? "" : String(r.detail || r.error || "didn't work");
    cErr.hidden = !!r.ok;
    // The window strip (winbar.js) keeps the red mark "Visible in recordings"
    // if hiding the window failed at startup: a successful toggle removes it, and a
    // failed attempt to hide brings it back.
    try {
      window.dispatchEvent(new CustomEvent("argus-capture", { detail: { ok: !!r.ok, wanted: excluded } }));
    } catch (err) { /* without the mark in the strip the switch works as it did */ }
  };

  const cFlip = () => cSend(!csw.classList.contains("on"));
  csw.addEventListener("click", cFlip);
  csw.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); cFlip(); }
  });

  api.get_("/window/capture").then((r) => {
    cAvailable = Boolean(r.available);
    csw.setAttribute("aria-disabled", String(!cAvailable));
    cPaint(Boolean(r.excluded));
  });
};

// Voice choice: a closed button with the current character, and on click a popup with
// search, a filter by language/favorites, and cards (avatar, language, favorite star,
// preview). It is the same gesture as an ordinary `<select>` in this app (our own popup
// instead of the system one, `openCombo` in chrome.js), only the options are not text but
// cards, which cannot be squeezed into a system list.
//
// The list is read from `A.values` through `api.values()`, not from a separate route:
// `/admin/schema` already carries every field of every character in full (the ordinary path
// `channel.tts.characters.<key>.*`), and a second source of the same data would let them
// drift apart at the first edit. Preview goes first through the free `preview_url` (a
// ready-made sample from ElevenLabs, with no quota used), and only if there is none through
// the paid `POST /tts/preview` (the same price as an ordinary voicing of an answer, per
// character), see `play()` below.
window.adminWidgets["voice-picker"] = (el, api) => {
  el.innerHTML = `
    <div class="f" data-ico="speaker">
      <div class="lab"><div class="name">Read answers aloud</div></div>
      <div class="ctl"><div class="switch" id="vp-enabled" role="switch" tabindex="0"></div></div>
    </div>
    <div class="vp-list" id="vp-list"></div>`;

  const enabledSwitch = el.querySelector("#vp-enabled");
  const list = el.querySelector("#vp-list");

  // The same gesture as the ordinary `kind="bool"` in the scaffold (`bindField`, above): a
  // click and space/enter toggle it, but wired by hand: the row lives inside the widget, not
  // in `.fields`, where `bindField` looks by itself through `$$(".fields .f")`.
  function syncEnabled() {
    enabledSwitch.classList.toggle("on", !!api.get("channel.tts.enabled"));
  }
  function flipEnabled() {
    api.set("channel.tts.enabled", !api.get("channel.tts.enabled"));
    syncEnabled();
  }
  enabledSwitch.addEventListener("click", flipEnabled);
  enabledSwitch.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); flipEnabled(); }
  });

  // The avatar palette is the colors of the theme itself (`--ok`/`--warn`/`--bad`/`--in`),
  // not new shades for one detail. No `--accent`: it is taken for "selected"/"playing" (CSS,
  // `admin.css`), and the same blue on an avatar would read as a third meaning of the same
  // color, unrelated to those two. The hash is simple and stable: the same voice always lands
  // on the same color between page reloads and does not "blink" with another one on every
  // render.
  const HUES = ["var(--ok)", "var(--warn)", "var(--bad)", "var(--in)"];
  function hueFor(slug) {
    let h = 0;
    for (let i = 0; i < slug.length; i++) h = (h * 31 + slug.charCodeAt(i)) >>> 0;
    return HUES[h % HUES.length];
  }

  // `dict[str, VoiceProfile]` is assembled back into objects from flat paths: the same parsing
  // that `flatten()`/`get_path()` do on the server (see `core/settings_store.py`), only in
  // the opposite direction and in the browser. The character name in the path is not known in
  // advance, so we extract it with a regular expression.
  const CHAR_RE = /^channel\.tts\.characters\.([^.]+)\.(label|language|favorite|voice_id|preview_url)$/;
  function characters() {
    const map = {};
    for (const [path, v] of Object.entries(api.values())) {
      const m = CHAR_RE.exec(path);
      if (!m) continue;
      (map[m[1]] ??= { slug: m[1] })[m[2]] = v;
    }
    return Object.values(map).filter((c) => c.voice_id);
  }

  // No visible player: the sound starts at once, with no window under it. The indicator is on
  // the button itself: while it plays, "▶" becomes "■" and is highlighted; a click on a
  // playing one stops it. It is keyed by slug, not by the button node: the list is redrawn
  // (favorites, search) and replaces the node, while the slug of the same character stays the
  // same, and `syncPlayButtons` finds it again after every render.
  let currentAudio = null;
  let currentSlug = null;
  let centered = false;   // the selected voice has already been brought into the visible part of the list

  function stopCurrent() {
    if (currentAudio) currentAudio.pause();
    currentAudio = null;
    currentSlug = null;
  }

  function syncPlayButtons() {
    list.querySelectorAll(".vp-play").forEach((b) => {
      const playing = currentSlug && b.closest(".vp-item").dataset.slug === currentSlug;
      b.classList.toggle("playing", !!playing);
      b.textContent = playing ? "■" : "▶";
    });
  }

  // An error (network, a refusal from ElevenLabs) goes to the console, not to a line of its
  // own on the page: it happens rarely, and there is no reason to occupy space permanently
  // for a rare case.
  async function play(character, btn) {
    if (currentSlug === character.slug) {
      stopCurrent();
      syncPlayButtons();
      return;
    }
    stopCurrent(); // one sound at a time: a second click cuts off the first and does not overlay it
    syncPlayButtons();
    try {
      let url = character.preview_url;
      if (!url) {
        // The character was created before `preview_url` appeared (by hand, through
        // `clone-voice`/`add-voice`): there is no sample, so we fall back to the old path, a paid
        // synthesis of a test phrase through `POST /tts/preview`. The button is disabled for
        // EXACTLY this request, not for the whole playback: otherwise a quick second click, "stop",
        // during playback would not go through at all. The browser does not send a click to a
        // `disabled` button, even a synthetic one, and `play()` would simply not be called for the
        // second click.
        btn.disabled = true;
        try {
          const r = await fetch("/tts/preview", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ character: character.slug }),
          });
          if (!r.ok) {
            const e = await r.json().catch(() => ({}));
            throw new Error(e.detail || "couldn't synthesize");
          }
          url = URL.createObjectURL(await r.blob());
        } finally {
          btn.disabled = false;
        }
      }
      const audio = new Audio(url);
      currentAudio = audio;
      currentSlug = character.slug;
      const onDone = () => { if (currentSlug === character.slug) { stopCurrent(); syncPlayButtons(); } };
      audio.addEventListener("ended", onDone);
      audio.addEventListener("error", onDone);
      syncPlayButtons();
      await audio.play();
    } catch (e) {
      stopCurrent();
      syncPlayButtons();
      console.error(e.message || "couldn't play sample");
    }
  }

  function render() {
    syncEnabled();
    const all = characters();

    if (!all.length) {
      list.innerHTML = `<div class="vp-empty">No voices yet.</div>`;
      return;
    }

    const active = api.get("channel.tts.active_character");

    // Favorites are always at the top of the list, not a separate filter: there are few of them
    // anyway (the star is put on the few that are chosen often), and a switch on top of that
    // would be an extra control for the same order the list already keeps by sorting.
    const shown = all
      .sort((a, b) => (b.favorite - a.favorite)
        || (a.label || a.slug).localeCompare(b.label || b.slug));

    if (!shown.length) {
      list.innerHTML = `<div class="vp-empty">Nothing matches the filter.</div>`;
      return;
    }

    // Namesakes are not hypothetical: the ElevenLabs library had two voices that were both
    // named literally "Hoffmann" (see the regression test in
    // `test_tts_admin_live.py`). The slug under the name is printed ONLY when the name is not
    // unique in the list: for the other forty it would be a line for the sake of a line, not a
    // hint.
    const nameCounts = {};
    shown.forEach((c) => {
      const n = c.label || c.slug;
      nameCounts[n] = (nameCounts[n] || 0) + 1;
    });

    list.innerHTML = shown.map((c) => {
      const name = c.label || c.slug;
      const initial = (name.trim()[0] || "?").toUpperCase();
      const isTwin = nameCounts[name] > 1;
      return `
      <div class="vp-item ${c.slug === active ? "active" : ""}" data-slug="${api.esc(c.slug)}">
        <div class="vp-avatar" style="--vp-hue:${hueFor(c.slug)}">${api.esc(initial)}</div>
        <div class="vp-body">
          <div class="vp-name">${api.esc(name)}</div>
          ${isTwin ? `<div class="vp-slug">${api.esc(c.slug)}</div>` : ""}
        </div>
        <button class="vp-star ${c.favorite ? "on" : ""}" type="button"
                aria-label="${c.favorite ? "Remove from favorites" : "Add to favorites"}">${c.favorite ? "★" : "☆"}</button>
        <button class="vp-play" type="button" aria-label="Play a sample">▶</button>
      </div>`;
    }).join("");
    syncPlayButtons();

    // The list has its own scroll (`.vp-list`, admin.css), and the selected voice may be off the
    // edge. On the first showing we bring it to the middle, once: after that the person scrolls
    // by themselves, and a redraw after a click on a star or on a voice must not drag the list
    // from under their hand. If the list is not visible yet (no height), we try again on the
    // next render.
    if (!centered) {
      const row = list.querySelector(".vp-item.active");
      if (row && list.clientHeight) {
        centered = true;
        const top = row.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
        list.scrollTop = Math.max(0, top - (list.clientHeight - row.offsetHeight) / 2);
      }
    }
  }

  list.addEventListener("click", (e) => {
    const row = e.target.closest(".vp-item");
    if (!row) return;
    const slug = row.dataset.slug;

    if (e.target.closest(".vp-star")) {
      const path = `channel.tts.characters.${slug}.favorite`;
      api.set(path, !api.get(path));
      render();
      return;
    }
    if (e.target.closest(".vp-play")) {
      const c = characters().find((x) => x.slug === slug);
      if (c) play(c, e.target.closest(".vp-play"));
      return;
    }
    api.set("channel.tts.active_character", slug);
    render();
  });

  // `onLive` fires not only after a save (an edit made from somewhere else, such as another
  // open settings tab, not by this widget): the same subscribers are called on every
  // websocket snapshot too, and that feeds the dashboard numbers several times a second and
  // has nothing to do with voices. Redrawing the whole list (`innerHTML`) on every such tick
  // would reset the scroll and the mouse hover several times a second: it looks exactly like
  // what is called "flickering". We compare a short signature of all the `channel.tts.*`
  // values and redraw only if it really changed: a websocket tick does not touch it, and a
  // save (here or from another tab) always does.
  let lastSignature = "";
  function renderIfChanged() {
    const relevant = JSON.stringify(
      Object.entries(api.values()).filter(([k]) => k.startsWith("channel.tts."))
    );
    if (relevant === lastSignature) return;
    lastSignature = relevant;
    render();
  }
  api.onLive(renderIfChanged);
  renderIfChanged();
};

// Entry point for someone else's document (the dashboard, `chrome.js`): it calls `mount`
// itself, explicitly, without auto-start, the same way as `window.argusAccount.mount` of
// account.js. The real `/admin` page calls `mount` from its own `<script>` at the end of
// the body (see admin.html), so it does not load itself here.
window.argusAdmin = { mount, flushSave, refresh };

})();
