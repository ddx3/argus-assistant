/* Settings and account are one button, not a side menu.

   Before, there was a panel behind three bars here. It had exactly two items (settings,
   account), and opening a menu to see what there is to open in it was an extra step. The
   button stands right in the header, in the same place where the menu button used to be, and
   does its job at once, without an intermediate panel.

   It opens a window in the middle of the screen, below, `openWindow`: tabs on the left, the
   content of the selected one on the right. The account is the last tab of the same window
   (there is no separate person button in the header any more), and the `#account` links lead
   to it.

   This file is shared by two pages, like `chrome.css`: the dashboard and the settings have
   their own markup, and the buttons are in their old place only on the dashboard. On the
   settings themselves (when they are opened in a separate tab, the `argus settings` mode,
   where there is no full dashboard and nothing to open) there are no buttons at all: there
   is only one transition, "dashboard", and it is simply visible in the header. There is no
   reason to hide one transition behind a separate button. */

(function () {
  "use strict";

  // --------------------------------------------------------------------
  // Our own list instead of the native popup of <select>
  // --------------------------------------------------------------------
  // A closed select is already dressed in the app style (dark background, its own arrow; see
  // `select{...}` in chrome.css). The open list is not: in WebView2 (Chromium) it is a system
  // popup over the page, with a white background and a blue highlight, and the colors on
  // select/option do not affect it (checked live, not from the documentation). So we do not
  // open the popup itself: we suppress it on mousedown/Enter/Space and draw our own list in
  // the same colors as the rest of the interface. The value and the change event stay on the
  // real <select>, so all the field wiring (`stage`, `syncPending`, device choice on the
  // dashboard, etc.) keeps working without knowing about the substitution.
  //
  // This comes before the embedded look (see below): it is needed there too, since the
  // settings are opened next to the dashboard, in an inner tab, and their own select has the
  // same trouble with the native popup.
  let comboOpen = null;
  let comboSeq = 0;   // opening may wait for a fresh list (`_beforeOpen`): closing and a new opening cancel the
                      // pending one

  function closeCombo() {
    comboSeq++;
    if (!comboOpen) return;
    comboOpen.menu.remove();
    comboOpen = null;
  }

  async function openCombo(select) {
    closeCombo();
    // A list that changes by itself (microphones: a headset is plugged and unplugged on the fly)
    // is reread by its owner before showing, through `select._beforeOpen`, which returns a
    // promise. Rereading "on mouse press" with a separate listener was not possible: the answer
    // comes later, while the popup was built at once from the old options, so a fresh headset
    // appeared only at the second opening, and a removed one was still offered (by choosing it,
    // the person got the system microphone instead of the needed one).
    if (typeof select._beforeOpen === "function") {
      const mine = comboSeq;
      try { await select._beforeOpen(); } catch (err) { /* refreshing failed: we show what we have */ }
      if (mine !== comboSeq || !select.isConnected) return;     // while we waited, the list was closed, another was opened, or the select was removed from
                                                                // the page
    }
    // Normally the popup stands level with the select itself. But a select can be small and
    // invisible, lying over a part of something bigger (on the dashboard, over the name next to
    // the check box, not over the whole button: the switch-off check box is deliberately not
    // part of it). Then "under the button" means the whole button, not the select inside it:
    // `data-combo-anchor="label"` explicitly asks to take the coordinates from the nearest
    // `<label>` rather than from the select itself. Without the attribute, as before, the
    // coordinates are taken from the select itself (the ordinary visible selects in /admin).
    const anchorEl = select.dataset.comboAnchor === "label"
      ? (select.closest("label") || select) : select;
    const r = anchorEl.getBoundingClientRect();
    const menu = document.createElement("div");
    menu.className = "combo-menu";
    menu.style.visibility = "hidden";
    menu.style.left = `${r.left}px`;
    // Normally the list is exactly the width of the select. But a select can be narrower than the
    // options in it: it is invisible itself and lies over a short name next to the check box
    // (dashboard, microphone choice), so its width fits the text "Microphone", not
    // «Microphone (HyperX Cloud Alpha Wireless)». `data-combo-min-width` is an explicit opt-in
    // for such a case, not general behavior: without it the list is still exactly the width of
    // the select, like for the ordinary visible selects in /admin.
    const minWidth = Number(select.dataset.comboMinWidth) || 0;
    [...select.options].forEach((o) => {
      const opt = document.createElement("div");
      opt.className = "opt" + (o.value === select.value ? " sel" : "");
      opt.textContent = o.textContent;
      opt.addEventListener("mousedown", (e) => {
        e.preventDefault(); // do not move focus before its own click
        // A native select does not send `change` for the same item; ours did, and a click on an
        // already selected microphone (to close the list) closed and reopened the sound in the
        // middle of a conversation.
        if (select.value !== o.value) {
          select.value = o.value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        closeCombo();
      });
      menu.appendChild(opt);
    });
    // We measure the needed width by letting the list grow freely first, with `max-content`,
    // without the select's width imposed in advance. Before, the select's width was set first
    // and the measuring came after, with `.opt` inside stretched to full width by
    // `align-items:stretch`. On a row shorter than its options ("Language" is narrower
    // than "same as conversation"), this squeezed width sometimes leaked into
    // the measurement instead of the real one, and the list came out slightly narrower than its
    // own text. `max-content` removes this race: while the list is as wide as it wants, there is
    // nothing to guess when measuring.
    menu.style.width = "max-content";
    document.body.appendChild(menu);
    const needed = menu.getBoundingClientRect().width;
    const width = Math.max(r.width, minWidth, needed);
    // The left edge is always the left edge of the select (or of its anchor) and never shifts.
    // The list is a continuation of its field, not a separately hanging window; it is natural for
    // it to grow to the right from where the field stands, rather than jump to the edge of the
    // screen. If it does not fit to the right entirely (device choice in a narrow control-panel
    // window, a long model name), we do not cut the text silently: we let the captions wrap to a
    // second line (`.combo-menu.combo-wrap`, chrome.css). We do not use just `.wrap`, because
    // that name is already taken for the grid of the whole settings page in admin.css
    // (`.wrap{display:grid;...}`), and the popup got someone else's two-column layout through the
    // second class instead of one column. A title that is visibly cut off is worse than two
    // lines.
    menu.style.left = `${r.left}px`;
    const roomToRight = window.innerWidth - r.left - 8;
    if (width <= roomToRight) {
      menu.style.width = `${width}px`;
      menu.classList.remove("combo-wrap");
    } else {
      // The width when wrapping is not the entire width up to the edge of the window
      // (`roomToRight`) and not artificially back to the size of the select, but as much as the
      // long option really needs (`needed`), clamped to `roomToRight` if even that is not enough.
      // Before, only `r.width`/`minWidth` were used, and the list shrank to the select even where
      // there was much more room to the window edge, and the wrapping was rougher than it should
      // have been: a one-letter word on the second line instead of almost the whole option on the
      // first. The emptiness next to short options does not grow because of this: the ceiling is
      // still `needed`, not the room to the window edge. The 160px floor is for the case when at
      // the very right edge of the screen there is almost nothing left on the right.
      menu.style.width = `${Math.max(160, minWidth, Math.min(roomToRight, needed))}px`;
      menu.classList.add("combo-wrap");
    }
    // Downward is always the first choice, even when it is tight below: the list has its own
    // scroll (`overflow:auto` in chrome.css), and a list cropped in height reads fine downward.
    // Before, only "where there is more room" was compared, and a list from the control panel
    // (where there is little room below, while the top of the page is formally far from the
    // select) opened upward and climbed over the neighboring rows of the same popup. From the
    // point of view of geometry alone there was "more room" there, but in fact it was someone
    // else's markup, not emptiness. Upward only when there is really nothing below to show the
    // list in full, less than one item row. `menu.style.maxHeight` in both cases keeps the list
    // from crossing the edge it opens toward, where it is cut with a scroll instead of sticking
    // out.
    const below = window.innerHeight - r.bottom - 4;
    const above = r.top - 4;
    const openAbove = below < 44 && above > below;
    menu.style.maxHeight = `${Math.min(340, openAbove ? above : below)}px`;
    if (openAbove) {
      menu.style.top = `${Math.max(4, r.top - menu.offsetHeight - 4)}px`;
    } else {
      menu.style.top = `${r.bottom + 4}px`;
    }
    menu.style.visibility = "";
    comboOpen = { menu, select };
    const sel = menu.querySelector(".opt.sel");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }

  document.addEventListener("mousedown", (e) => {
    const select = e.target.closest("select");
    if (select) {
      e.preventDefault(); // we do not open the native popup at all
      select.focus();
      if (comboOpen && comboOpen.select === select) closeCombo();
      else openCombo(select);
      return;
    }
    // No `comboOpen &&`: a click elsewhere also cancels an opening that is still waiting for a
    // fresh list.
    if (!e.target.closest(".combo-menu")) closeCombo();
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName !== "SELECT") return;
    if (e.key === "Enter" || e.key === " " || (e.altKey && e.key === "ArrowDown")) {
      e.preventDefault(); // these are exactly the keys that open the native popup
      openCombo(e.target);
    } else if (e.key === "Escape" && comboOpen) {
      // Only the list must be closed: the settings window under it stays (the next Escape closes
      // it, below), otherwise one keypress would take away both the list and the whole window at
      // once.
      e.stopImmediatePropagation();
      closeCombo();
    }
  });

  window.addEventListener("resize", closeCombo);
  window.addEventListener("scroll", (e) => {
    // The list's own scroll (when maxHeight cut it and it scrolls to the selected item, see
    // `scrollIntoView` above) must not close the list itself: this is not "the page moved from
    // under the popup" but normal scrolling inside it. We close only on someone else's scroll,
    // where the list is open over something and that something moved away.
    if (!comboOpen) return;
    const t = e.target;
    if (t === comboOpen.menu) return;
    // We close only if what the list stands on moved: the whole page or the container that holds
    // the select itself. The feed of utterances and answers (`#chat`, `#raw`) scrolls on every
    // new line of the conversation and has nothing to do with the microphone list, while before,
    // any scroll closed the list from under the cursor.
    const moved = t === document || t === document.documentElement || t === document.body
      || (t && t.nodeType === 1 && t.contains(comboOpen.select));
    if (moved || !comboOpen.select.isConnected) closeCombo();
  }, true);

  // A file dropped on the window outside the drop zone is opened by the browser engine
  // instead of the page: the frameless window turns into a viewer for that PDF or image,
  // and the only way back is Alt+← or a restart. The materials drop zone (`#mt-drop` in
  // admin.js) accepts files itself — its handlers also call `preventDefault` and run
  // before this one, in the bubbling phase. There is nowhere else on the page to drop
  // files, so the drag is cancelled there (the "not allowed" cursor). Only files, not text
  // or links: selected text is dragged between fields, and that must not break. This sits
  // above the early exits below, so a page without a settings button and the embedded view
  // are protected the same way.
  const hasFiles = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  document.addEventListener("dragover", (e) => {
    if (!hasFiles(e) || e.defaultPrevented) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "none";
  });
  document.addEventListener("drop", (e) => { if (hasFiles(e)) e.preventDefault(); });

  // Embedded view: the page is opened inside a slide-out panel and does not need its own
  // header — the page that shows it provides the navigation.
  if (new URLSearchParams(location.search).has("embedded")) {
    document.body.dataset.embedded = "1";
    return;
  }

  // Where the page currently on screen came from is no longer shown as a menu row: people
  // ask about it once a month, but it took up space in the interface every day. The answer
  // is still available — the same `curl -I` on any page reads the `X-Argus-Build` header,
  // and `/build` returns the same thing as JSON; see `argus/live.py` and `ui/server.py`.

  const settingsBtn = document.getElementById("settings-btn");

  if (!settingsBtn) {
    // A standalone settings page (`argus settings` mode, with no dashboard next to it) or
    // any other page without these two buttons — there is nothing to open here.
    return;
  }

  // The scrim and the window live at the end of the document, not next to the buttons:
  // the header is sticky and has its own layer, and a window grown inside it would be
  // clipped by its bounds.
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.hidden = true;
  document.body.appendChild(scrim);

  const esc = (x) => String(x ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // --- settings and account window ---
  //
  // Before, "settings" was a link: it navigated to another page, dropped the websocket,
  // lost the conversation feed, and returned through the header. Then it was a side panel,
  // with both account and settings in one long list. Now it is one window in the middle of
  // the screen: tabs on the left, the content of the selected one on the right, and the
  // dashboard behind the scrim keeps working.
  //
  // The tabs are supplied by whoever knows what is in them: settings (`admin.js`) by the
  // sections of its schema, account (`account.js`) with its single one. The window only
  // shows and switches them. It is built once on the first open and afterwards simply
  // shown: the settings page must not reload just because it was closed and opened again,
  // and the account tab may hold an email and password that were typed in.

  // Tab icons are drawn with lines, like all icons in the project. They live here, not
  // with whoever adds a tab: the window has a single set, and the settings tabs take
  // theirs from here by name (`win.icons` in `admin.js`).
  const ICONS = {
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>',
    clip: '<path d="m21.4 11.1-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8"/>',
    lang: '<path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2h1"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/>',
    bolt: '<path d="M13 2L3 14h9l-1 8 10-12h-9z"/>',
    sliders: '<path d="M4 6h8M18 6h2M4 12h12M20 12h0M4 18h5M15 18h5"/><circle cx="14" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="11" cy="18" r="2"/>',
    kbd: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M7 13h.01M11 13h.01M15 13h.01M8 16.5h8"/>',
    mic: '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 11a7 7 0 0 1-14 0"/><path d="M12 18v4"/>',
    speaker: '<path d="M11 5L6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    card: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/><path d="M6 15h4"/>',
    chart: '<path d="M4 20V11M10 20V5M16 20v-6M22 20H2"/>',
  };

  // Tabs that are not about settings: the ⚙ button opens the window on a settings tab,
  // not on these.
  const NOT_SETTINGS = new Set(["account", "billing", "usage"]);

  let win = null;        // the window, once it is built
  let ready = null;      // tab assembly: in progress or finished
  let returnTo = null;   // where to return focus when the window closes
  let lastSettings = null;  // the settings tab the window was closed on last time
  let closeSeq = 0;      // an open during a close must not get `hidden`

  function buildWindow() {
    const el = document.createElement("section");
    el.className = "win";
    el.hidden = true;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "Settings");
    el.innerHTML =
      '<nav class="win-side" aria-label="Sections">' +
        '<button type="button" class="iconbtn ghost win-close" aria-label="Close">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12"/><path d="M18 6L6 18"/></svg>' +
        '</button>' +
        '<div class="win-tabs" role="tablist" aria-orientation="vertical"></div>' +
        '<div class="win-foot"></div>' +
      '</nav>' +
      '<div class="win-main"><div class="win-panels"></div></div>';

    const w = {
      el,
      icons: ICONS,
      tabs: [],            // in `order` order; each one has a `btn` and a `panel`
      want: null,          // which tab was requested (it may not have arrived yet)
      current: null,
      list: el.querySelector(".win-tabs"),
      main: el.querySelector(".win-main"),
      panels: el.querySelector(".win-panels"),
      foot: el.querySelector(".win-foot"),
    };

    // Add a tab. `panel` is the element with its content (the tab's author supplies it or,
    // if it is not placed anywhere, the window puts it into the shared column); the window
    // puts the title into it and hides it until the tab is selected. `lead` is ready-made
    // markup for the subtitle (it comes from the server, not from a person); `onShow` runs
    // once, the first time the tab is opened: blocks that measure sizes and poll hardware
    // are drawn when they are visible, not when the page has just been built.
    // `group`/`groupTitle` are a label above consecutive tabs of the same group.
    w.add = (def) => {
      const tab = { ...def, shown: false };
      // Not `<header>`: the general `header` rule in chrome.css (the page's sticky glass
      // strip) would reach the tab header too.
      const head = document.createElement("div");
      head.className = "win-head";
      head.innerHTML = `<h2 class="win-title">${esc(def.title)}</h2><p class="win-lead">${def.lead || ""}</p>`;
      tab.panel.classList.add("win-panel");
      tab.panel.id = `wpanel-${def.id}`;
      tab.panel.setAttribute("role", "tabpanel");
      tab.panel.setAttribute("aria-labelledby", `wtab-${def.id}`);
      tab.panel.hidden = true;
      tab.panel.prepend(head);
      if (!tab.panel.parentNode) w.panels.appendChild(tab.panel);
      w.tabs.push(tab);
      w.tabs.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
      w.render();
    };

    w.render = () => {
      let html = "";
      let last = null;
      for (const t of w.tabs) {
        if (t.groupTitle && t.group !== last) html += `<div class="win-group">${esc(t.groupTitle)}</div>`;
        last = t.group;
        html += `<button type="button" class="win-tab" role="tab" data-tab="${esc(t.id)}" id="wtab-${esc(t.id)}"` +
          ` aria-controls="wpanel-${esc(t.id)}" aria-selected="false" tabindex="-1">` +
          `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[t.icon] || ""}</svg><span>${esc(t.title)}</span></button>`;
      }
      w.list.innerHTML = html;
      for (const t of w.tabs) t.btn = w.list.querySelector(`[data-tab="${CSS.escape(t.id)}"]`);
      w.show();
    };

    // Show the `want` tab (or the one that is already open, or the first) and hide the
    // rest. Also called on every tab addition: if a tab that is still loading was
    // requested, it takes its place as soon as it arrives.
    w.show = () => {
      const t = w.tabs.find((x) => x.id === w.want)
        || w.tabs.find((x) => x.id === w.current)
        || w.tabs[0];
      if (!t) return;
      w.current = t.id;
      if (!NOT_SETTINGS.has(t.id)) lastSettings = t.id;
      for (const x of w.tabs) {
        const on = x === t;
        x.panel.hidden = !on;
        x.btn.setAttribute("aria-selected", String(on));
        x.btn.tabIndex = on ? 0 : -1;
      }
      // `onShow` runs only when the window is already on screen (`live`): while tabs are
      // being added, the first one is selected on its own, and without this condition the
      // account tab would call the server while the window is still being built, and the
      // block with measurements would measure zeros while hidden.
      if (!t.shown && w.live) {
        t.shown = true;
        try { if (t.onShow) t.onShow(); } catch (err) { console.error(err); }
      } else if (w.live && t.shown && w.entered !== t.id && t.onEnter) {
        // The tab was opened again (or the window was reopened on it): `onEnter` refreshes
        // whatever could have changed while the tab was not visible (for example, the balance
        // after signing in on the "Account" tab).
        try { t.onEnter(); } catch (err) { console.error(err); }
      }
      if (w.live) w.entered = t.id;
    };

    const pick = (id) => { w.want = id; w.show(); w.main.scrollTop = 0; };

    w.setFoot = (text) => { w.foot.textContent = text || ""; };

    el.querySelector(".win-close").addEventListener("click", shutWindow);
    w.list.addEventListener("click", (e) => {
      const b = e.target.closest(".win-tab");
      if (b) pick(b.dataset.tab);
    });
    // Arrow keys move through the tabs like a list: Tab skips past them into the content
    // instead of stepping through every tab in turn.
    w.list.addEventListener("keydown", (e) => {
      const b = e.target.closest(".win-tab");
      if (!b) return;
      const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
      let i = w.tabs.findIndex((t) => t.btn === b);
      if (step) i += step;
      else if (e.key === "Home") i = 0;
      else if (e.key === "End") i = w.tabs.length - 1;
      else return;
      e.preventDefault();
      const t = w.tabs[(i + w.tabs.length) % w.tabs.length];
      pick(t.id);
      t.btn.focus();
    });
    // Narrow window (up to 760px): the tabs sit in one row and scroll sideways, but the
    // mouse wheel does not scroll sideways — without this, "Payment" and
    // "Account" on the right are reachable only by arrow keys or a touchpad
    // swipe. We turn the vertical wheel into a horizontal one while there is room to
    // scroll; in a wide window the list is vertical and this code is never reached.
    w.list.addEventListener("wheel", (e) => {
      if (w.list.scrollWidth <= w.list.clientWidth) return;
      if (Math.abs(e.deltaX) >= Math.abs(e.deltaY)) return;     // a touchpad already scrolls sideways by itself
      w.list.scrollLeft += e.deltaY;
      e.preventDefault();
    }, { passive: false });

    return w;
  }

  // Account is a tab like the others. `account.js` draws into its own container and
  // redraws it entirely for each state (sign-in, registration, signed in), so the tab
  // title sits in a wrapper, not inside it. The block is mounted on the tab's first
  // show: it calls the account server for the balance, and there is no reason to do
  // that just because the session settings were opened.
  function addAccountTab(w) {
    if (!window.argusAccount) return;
    const panel = document.createElement("div");
    panel.classList.add("account");
    const box = document.createElement("section");
    box.className = "sheet-body account";
    panel.appendChild(box);
    w.add({
      id: "account", title: "Account", icon: "user", order: 900, panel,
      onShow: () => window.argusAccount.mount(box, window.argusAccountHooks || {}),
    });
  }

  // "Payment" is a separate tab next to "Account" (2026-10-08:
  // billing gets its own tab instead of living in the account tab): balance, top-up,
  // rates, history. `account.js` draws it (`mountBilling`): the same account server, but
  // its own container. It is mounted on the first show and refreshed on every later one
  // (`onEnter`): signing in on the Account tab or paying in the browser changes the
  // balance while the tab is not visible.
  function addBillingTab(w) {
    if (!window.argusAccount || !window.argusAccount.mountBilling) return;
    const panel = document.createElement("div");
    panel.classList.add("account");
    const box = document.createElement("section");
    box.className = "sheet-body account billing";
    panel.appendChild(box);
    let billing = null;
    w.add({
      id: "billing", title: "Payment", icon: "card", order: 890, panel,
      lead: "One balance for everything: recognition, answers, and voice replies. The money doesn't expire.",
      onShow: () => { billing = window.argusAccount.mountBilling(box, window.argusAccountHooks || {}); },
      onEnter: () => { if (billing) billing.refresh(); },
    });
  }

  // "Usage" is also its own tab (2026-10-08): spend by day and by
  // service, previously in "Account". `mountUsage` in `account.js` draws it;
  // the data changes while the app is in use, so every repeated show refreshes the tab
  // (`onEnter`).
  function addUsageTab(w) {
    if (!window.argusAccount || !window.argusAccount.mountUsage) return;
    const panel = document.createElement("div");
    panel.classList.add("account");
    const box = document.createElement("section");
    box.className = "sheet-body account usage";
    panel.appendChild(box);
    let usage = null;
    w.add({
      id: "usage", title: "Usage", icon: "chart", order: 895, panel,
      lead: "Where the money goes: by day and by service.",
      onShow: () => { usage = window.argusAccount.mountUsage(box, window.argusAccountHooks || {}); },
      onEnter: () => { if (usage) usage.refresh(); },
    });
  }

  // `admin.css`/`admin.js` are not included up front in `index.html`: the `/admin/*`
  // routes are registered only when `[ui] admin` is enabled (`server.py`), and the
  // settings button is shown at the same time (and also when there is an account
  // sign-in — pages: `hidden = !(s.admin || window.argusAccount)`). A tag inserted up
  // front without admin enabled would hit a 404 in the console on every dashboard load.
  // We load on the first open, once — a second visit finds a ready `window.argusAdmin`
  // and skips the load.
  let adminLoading = null;
  function loadAdmin() {
    if (window.argusAdmin) return Promise.resolve();
    if (adminLoading) return adminLoading;
    adminLoading = new Promise((resolve, reject) => {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "/admin/admin.css";
      document.head.appendChild(link);
      const script = document.createElement("script");
      script.src = "/admin/admin.js";
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
    return adminLoading;
  }

  // Settings: the same skeleton as the real `/admin` (`argus run --settings`, without the
  // dashboard), but laid out in the window's tabs — no iframe, using the same technique
  // as the account. It is not a copy of the form: there are around a hundred fields, and
  // a second copy of their markup would drift from the first on the first edit. Neither
  // "Cancel" nor "Save" is needed outside — the page saves itself
  // (`scheduleSave` in admin.js), and the close cross is the only control that belongs
  // here.
  async function addSettingsTabs(w) {
    try {
      await loadAdmin();
      const root = document.createElement("div");
      w.panels.prepend(root);
      await window.argusAdmin.mount(root, { win: w });
    } catch (err) {
      // Admin is off (`[ui] admin`) or failed to load: the window stays with the account
      // alone, not with an empty space.
      console.warn("settings are unavailable", err);
    }
  }

  const sheetOpen = () => !!win && !win.el.hidden;

  // `tab` is the tab id; without it, the settings tab where the window was closed last
  // time (or the first one on the first open).
  async function openWindow(tab) {
    if (!sheetOpen()) returnTo = document.activeElement;
    closeSeq++;
    const first = !win;
    if (!win) {
      win = buildWindow();
      document.body.appendChild(win.el);
      addAccountTab(win);
      addBillingTab(win);
      addUsageTab(win);
      ready = addSettingsTabs(win);
    }
    // Wait for the settings tabs before showing the window: otherwise it would open on
    // "Account" and jump to the first tab as soon as that one arrives. It is a
    // local request, a fraction of a second.
    await ready;
    // Without a request: the settings tab where the window was closed last time, and on
    // the first open the first of them (not "Account": it is added earlier but
    // sits at the end of the list and is not wanted by the settings button).
    const firstSettings = win.tabs.find((t) => !NOT_SETTINGS.has(t.id));
    // We show the window before selecting a tab: a block that needs sizes (the voice list
    // brings the selected voice to the middle) is drawn on the tab's first show, and in a
    // hidden window it would measure zeros.
    scrim.hidden = false;
    win.el.hidden = false;
    win.live = true;
    win.entered = null;
    win.want = tab || lastSettings || (firstSettings && firstSettings.id);
    win.show();
    // The values in the window are a copy from the moment it was built. While it was
    // closed, someone else could have changed them (the popup by the question field —
    // "Think longer" — the microphone, the language; another browser tab),
    // and the window would show the old values, and the next edit would write over the
    // change. We re-read on every open except the first (it was just read). Without
    // waiting: the window appears at once, and the fields update a moment later.
    if (!first) Promise.resolve(window.argusAdmin?.refresh?.()).catch(() => {});
    requestAnimationFrame(() => document.body.classList.add("sheet-open"));
    const cur = win.tabs.find((t) => t.id === win.current);
    if (cur && cur.btn) cur.btn.focus();
  }

  function shutWindow() {
    // Settings save themselves, a pause after the last edit (`scheduleSave` in admin.js) —
    // but closing the window from outside is not the same as unloading the page inside:
    // the window is hidden with `hidden`, not removed, and `beforeunload` is not
    // responsible for it there. We flush an edit that has not yet reached the server
    // ourselves, while the window is still alive, rather than waiting for the next open or
    // for the whole app to close.
    window.argusAdmin?.flushSave?.();
    document.body.classList.remove("sheet-open");
    // `#admin` brought us here — the window was closed, and the address must not keep
    // claiming that it is open. Otherwise reloading the page would open it again, although
    // the person has just closed it by hand.
    if (location.hash === "#admin" || location.hash === "#account" || location.hash === "#billing"
        || location.hash === "#usage") {
      history.replaceState(null, "", location.pathname + location.search);
    }
    if (returnTo && returnTo.focus) returnTo.focus();
    returnTo = null;
    const seq = ++closeSeq;
    setTimeout(() => {
      if (seq !== closeSeq) return;
      if (win) win.el.hidden = true;
      scrim.hidden = true;
    }, 180);
  }

  // Tab cycles inside the window: the page under it is dimmed and there is nothing to
  // press in it, and focus that went under the scrim would look lost.
  function trapFocus(e) {
    const items = [...win.el.querySelectorAll("button, input, select, textarea, a[href], [tabindex]")]
      .filter((el) => !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (!win.el.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  settingsBtn.addEventListener("click", () => openWindow());

  // There is no separate account button in the header any more (2026-10-07): sign-in and
  // balance are the "Account" tab of this same window, and the `#account` links
  // below lead to it. The sign-in window must open both without an account server and
  // without admin: the tab itself (`account.js`) is responsible for the refusal, and it
  // will honestly show "account server not configured"
  // when it comes to that, while the settings button stays visible as long as there is a
  // way to open the account (pages: `hidden = !(s.admin || window.argusAccount)`).

  // A hash in the address, not a separate route: the server redirects a direct visit to
  // `/admin` to `/#admin`, and the "settings" links in the sign-in hints lead to
  // `#account` — on the same page the window opens without a reload.
  const openByHash = () => {
    if (location.hash === "#admin") openWindow();
    else if (location.hash === "#account") openWindow("account");
    else if (location.hash === "#billing") openWindow("billing");
    else if (location.hash === "#usage") openWindow("usage");
  };
  openByHash();
  window.addEventListener("hashchange", openByHash);

  window.argusWin = { open: openWindow, close: shutWindow };

  scrim.addEventListener("click", shutWindow);
  document.addEventListener("keydown", (e) => {
    if (!sheetOpen()) return;
    if (e.key === "Escape") shutWindow();
    else if (e.key === "Tab") trapFocus(e);
  });
})();

/* Answer copying is a separate block, not inside the window code above: that code has
   something to "exit early" on (the embedded view, a page without a settings button),
   while copying is needed by both pages with answers in any case.

   The "Copy" button used to put the answer text on the clipboard as it
   came from the model, that is, with markup: into Word, email and messengers went
   `**bold**` with asterisks instead of bold text. Now the clipboard holds two forms of
   the same answer, and the receiving program takes the one it understands:

     text/html   — real markup: bold, italic, lists, tables, code;
     text/plain  — the same answer without markup: no asterisks, no hashes, no
                   backticks (Notepad and an input field have no bold).

   The pages pass in the ready HTML from their own parser (`md` on the working screen,
   `renderMd` on "Recording"): parsing markdown again would mean a third parser
   that drifts from the two existing ones. Plain text is derived from the same tree, not
   from the source, so it matches what the person saw. Page classes do not travel to the
   clipboard: on the dark theme they would produce white on white in Word.

   Also there, at the end of the block, is the "copy code" icon on every code block: it
   copies only the code, not the whole answer, and also in two forms (the code as is and
   a `<pre><code>` block). */
(function () {
  "use strict";

  // A formula in plain text is its TeX in the same delimiters the model writes it in.
  // The source lives in the element itself (`data-tex`, set by `renderMath` of the
  // working screen): it cannot be recovered from MathML, and glued-together glyphs
  // without it are garbage.
  function texOf(math) {
    const src = math.getAttribute("data-tex");
    if (src == null) return math.textContent;
    return math.getAttribute("display") === "block" ? "$$" + src + "$$" : "\\(" + src + "\\)";
  }

  // A time reference in a "Recording" answer: buttons inside `<span class="nb">`
  // together with the sign after the parenthesis. In text it is again what the model
  // wrote: "(14:32)", "(15:06–15:07)", "(15:01, 15:09)." A single button without a
  // wrapper is also a time in parentheses.
  function citeText(span) {
    const chips = [...span.querySelectorAll("button.cite")].map((b) => b.textContent);
    if (!chips.length) return inlineOf(span);
    const tail = span.lastChild.nodeType === 3 ? span.lastChild.nodeValue : "";
    return "(" + chips.join(", ") + ")" + tail;
  }

  // A link from an answer is a `span.lnk`: the page parsers draw it as a label plus a
  // short address as text, not as a navigation (the app window is frameless, and
  // following a link would take it away from the page). The full address lives in
  // `data-href` (earlier in `title`) and must make it to the clipboard: from "See
  // [the docs](https://…)" only "See the docs" used to be pasted.
  function linkUrl(span) { return span.getAttribute("data-href") || span.getAttribute("title") || ""; }
  // The label without the short address in parentheses (`span.lnk-url`): the full
  // address is added separately.
  function linkLabel(span) {
    const own = span.cloneNode(true);
    for (const u of [...own.querySelectorAll(".lnk-url")]) u.remove();
    return own;
  }
  function linkText(span) {
    const url = linkUrl(span);
    if (!url) return inlineOf(span);                  // there is no address in the markup — as on the page: the label and (host)
    const label = inlineOf(linkLabel(span)).trim();
    return !label || label === url ? url : label + " (" + url + ")";
  }

  // A string from a node's content: text as is, <br> is a line break, a "Recording"
  // (Recording) time reference is again in parentheses, as written in the text, a link is
  // its label and address.
  function inlineOf(node) {
    let s = "";
    for (const c of node.childNodes) {
      if (c.nodeType === 3) { s += c.nodeValue; continue; }
      if (c.nodeType !== 1) continue;
      const tag = c.localName;
      if (tag === "br") s += "\n";
      else if (tag === "math") s += texOf(c);
      else if (tag === "span" && c.classList.contains("nb")) s += citeText(c);
      else if (tag === "span" && c.classList.contains("lnk")) s += linkText(c);
      else if (tag === "button" && c.classList.contains("cite")) s += "(" + c.textContent + ")";
      else s += inlineOf(c);
    }
    return s;
  }

  // A list as lines: "- item" and "1. item", a nested one two spaces deeper.
  function listOf(list, depth) {
    const rows = [];
    // A numbered list after a code block or a paragraph continues the count
    // (`<ol start="3">`): it used to start from one again, and "3. run, 4. check" was
    // pasted as "1. 2.".
    const first = parseInt(list.getAttribute("start"), 10);
    let n = (Number.isNaN(first) ? 1 : first) - 1;
    for (const li of list.children) {
      if (li.localName !== "li") continue;
      const own = li.cloneNode(true);
      const nested = [...own.children].filter((c) => c.localName === "ul" || c.localName === "ol");
      nested.forEach((c) => c.remove());
      const mark = list.localName === "ol" ? ++n + ". " : "- ";
      rows.push("  ".repeat(depth) + mark + inlineOf(own).trim());
      nested.forEach((c) => rows.push(listOf(c, depth + 1)));
    }
    return rows.join("\n");
  }

  // Block tags: a quote that contains them is read as a set of blocks, not as one line.
  const BLOCKS = new Set(["p", "ul", "ol", "pre", "blockquote", "table", "hr", "div", "h1", "h2", "h3", "h4", "h5", "h6"]);

  function blockOf(el) {
    switch (el.localName) {
      case "ul": case "ol": return listOf(el, 0);
      case "pre": return el.textContent.replace(/\n+$/, "");
      case "hr": return "---";
      case "math": return texOf(el);
      case "blockquote": {
        // A quote can contain paragraphs, lists and code (`<blockquote><p>…</p><p>…</p></blockquote>`):
        // joining through `inlineOf` gave "first paragraphsecond paragraph". Blocks go as at the
        // top level — separated by a blank line, and "> " is put on every line (an empty one
        // gets a lone ">"). A quote without blocks inside (a recording: text and time
        // references) is one line.
        const inner = [...el.children].some((c) => BLOCKS.has(c.localName)) ? plainOf(el) : inlineOf(el).trim();
        return inner.split("\n").map((l) => (l ? "> " + l : ">")).join("\n");
      }
      // Cells separated by tabs: that is how a table lands in Excel and Google Sheets, cell
      // by cell.
      case "table":
        return [...el.rows]
          .map((r) => [...r.cells].map((c) => inlineOf(c).trim().replace(/\s*\n\s*/g, " ")).join("\t"))
          .join("\n");
      default: return inlineOf(el).trim();
    }
  }

  function plainOf(root) {
    return [...root.childNodes]
      .map((c) => (c.nodeType === 3 ? c.nodeValue.trim() : c.nodeType === 1 ? blockOf(c) : ""))
      .filter(Boolean)
      .join("\n\n");
  }

  // Markup for programs that understand it. Only semantic tags, no styles: everything
  // else (font, color, margins) the receiving program sets by its own rules, as with any
  // pasted text.
  function richOf(doc) {
    // A working-screen heading is a bold line (`b.h`); we make it a separate paragraph, not
    // a bold word stuck to the following text.
    for (const h of doc.querySelectorAll("b.h")) {
      const p = doc.createElement("p");
      h.before(p);
      p.append(h);
    }
    for (const sp of doc.querySelectorAll("span.nb")) sp.replaceWith(doc.createTextNode(citeText(sp)));
    for (const b of doc.querySelectorAll("button.cite")) {
      b.replaceWith(doc.createTextNode("(" + b.textContent + ")"));
    }
    // A link from an answer becomes a real `<a href>`: on the page it is a label and a
    // short address as text, but in Word, email and messengers it must stay a link. Only
    // http(s) and mailto — the parser lets no other schemes through, and we will not
    // either, whatever ends up in the markup. This runs before `class` and `data-*` are
    // stripped below: the address is stored there.
    for (const sp of doc.querySelectorAll("span.lnk")) {
      const url = linkUrl(sp);
      if (!/^(https?:\/\/|mailto:)/i.test(url)) continue;
      const a = doc.createElement("a");
      a.setAttribute("href", url);
      a.append(...linkLabel(sp).childNodes);
      const tail = a.lastChild;
      if (tail && tail.nodeType === 3) tail.nodeValue = tail.nodeValue.replace(/\s+$/, "");
      sp.replaceWith(a);
    }
    // Without borders a table in Word and in email collapses into lines of text.
    for (const t of doc.querySelectorAll("table")) {
      t.setAttribute("border", "1");
      t.setAttribute("cellspacing", "0");
      t.setAttribute("cellpadding", "4");
    }
    // A code block is `<pre><code>`, same as for the "copy code" icon (`codeHtml`): in that
    // form it is recognized by programs that turn a pasted block into a code block (Notion,
    // Slack, Docs).
    for (const pre of doc.querySelectorAll("pre")) {
      if (pre.querySelector("code")) continue;
      const code = doc.createElement("code");
      code.append(...pre.childNodes);
      pre.append(code);
    }
    for (const el of doc.body.querySelectorAll("*")) {
      for (const a of [...el.attributes]) {
        if (a.name === "class" || a.name.startsWith("data-")) el.removeAttribute(a.name);
      }
    }
    return doc.body.innerHTML;
  }

  // `html` is the output of the page's parser, not foreign text: the model's raw text in
  // it is already escaped. Parsing still happens in an inert document (DOMParser):
  // neither an image nor a script in it will come alive, whatever ends up there.
  function parts(html) {
    const doc = new DOMParser().parseFromString(String(html ?? ""), "text/html");
    // The "copy code" button is part of the page, not of the answer: the code block goes
    // back to a plain `<pre>`, as if the button had never been there.
    for (const b of doc.querySelectorAll("button.codecopy")) b.remove();
    for (const w of doc.querySelectorAll("div.codeblock")) w.replaceWith(...w.childNodes);
    // The table wrapper (`div.tablewrap`, gives sideways scrolling on the page) is also
    // part of the page, not of the answer.
    for (const w of doc.querySelectorAll("div.tablewrap")) w.replaceWith(...w.childNodes);
    // The order matters: plain text reads the classes and `data-tex`, and `richOf` erases
    // them.
    const text = plainOf(doc.body);
    return { html: richOf(doc), text };
  }

  async function write(html, text) {
    if (navigator.clipboard?.write && window.ClipboardItem) {
      try {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        })]);
        return true;
      } catch { /* the fallback path follows */ }
    }
    // The fallback path is for when the page has not been granted clipboard permission
    // (opened at an address without https, a window without focus): the answer is selected
    // in a hidden field, and its "copy" event is overridden with our two forms.
    let handled = false;
    const onCopy = (e) => {
      e.clipboardData.setData("text/html", html);
      e.clipboardData.setData("text/plain", text);
      e.preventDefault();
      handled = true;
    };
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(box);
    box.select();
    document.addEventListener("copy", onCopy, true);
    let ok = false;
    try { ok = document.execCommand("copy") && handled; } catch { ok = false; }
    document.removeEventListener("copy", onCopy, true);
    box.remove();
    return ok;
  }

  // Confirmation on the button itself: for a second the icon becomes a white check mark
  // (a plain white check mark was requested, not a green icon, 2026-10-08), and if the
  // copy fails, it turns red. One mechanism for all copy buttons: on the working screen's
  // answer, on the "Recording" answer and on a code block. The check mark is a
  // second `<svg class="done">` inside the button; CSS shows and hides it
  // (`.iconbtn.copied`, chrome.css), so a redrawn feed (the button is replaced with a new
  // one) simply returns to the original icon.
  const DONE = '<svg class="done" viewBox="0 0 24 24" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>';
  const marks = new WeakMap();   // button → the timer that will restore its original icon
  function flash(btn, ok) {
    if (!btn) return;
    clearTimeout(marks.get(btn));
    btn.classList.remove("copied", "failed");
    if (ok && !btn.querySelector(":scope > svg.done")) btn.insertAdjacentHTML("beforeend", DONE);
    btn.classList.add(ok ? "copied" : "failed");
    marks.set(btn, setTimeout(() => btn.classList.remove("copied", "failed"), 1400));
  }

  // Put the answer on the clipboard. `html` is what the page shows as the answer; `btn`
  // is the button that asked for it: its icon confirms the result (`flash`). Whether it
  // worked is reported by a return value, not an exception: the page needs a check mark
  // or a message, not an analysis of causes.
  async function copyAnswer(html, btn) {
    let done = false;
    try {
      const { html: rich, text } = parts(html);
      done = text ? await write(rich, text) : false;
    } catch {
      done = false;
    }
    flash(btn, done);
    return done;
  }

  // `parts` is exposed only so that the parsing can be tested in a real browser
  // (`tests/test_copy_answer.py`).
  window.argusCopy = Object.assign(copyAnswer, { parts });

  // The "copy code" icon on a code block in an answer (`.codeblock > .codecopy`). The
  // markup is supplied by the page parsers (`md`, `renderMd`), but the behavior is one
  // for all and lives here, with a single listener on the document: answers are redrawn
  // entirely, and anything attached to the buttons themselves would be lost along with
  // the markup. Only the code is copied, and also in two forms, like the whole answer:
  // plain text is the code itself as is (editor, terminal), HTML is a `<pre><code>` block
  // (counting on Word, Docs, Slack, Notion and Telegram pasting it as a code block rather
  // than as plain text). The icon itself shows the result (`flash`): a silent button looks
  // broken.
  function codeHtml(text) {
    const pre = document.createElement("pre");
    pre.appendChild(document.createElement("code")).textContent = text;   // escapes by itself, no need to do it by hand
    return pre.outerHTML;
  }
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest?.(".codecopy");
    const code = btn?.closest(".codeblock")?.querySelector("pre");
    if (!code) return;
    const text = code.textContent;
    flash(btn, await write(codeHtml(text), text));
  });
})();
