/* Account block: sign-in, balance, top-up.

   Three tabs of the settings window, one file: `mount` is "Account" (sign-in,
   terms, data), `mountBilling` is "Payment" (balance, top-up, prices, history),
   `mountUsage` is "Usage" (spending by day and by service). The shared money
   helpers and icons are above, at module level.

   One markup for two places: the gauge's slide-out menu and a section in the settings.
   There cannot be a copy here: there are few fields, but the block has three states
   (sign-in, registration, signed in), and a second markup would diverge from the first on
   the first edit. They have nowhere to diverge: it is literally the same window shown in
   two places.

   The block decides nothing by itself. The right to use is computed by the server, and
   here is only the display of its reply: the program sits on someone else's computer, and
   a check inside it is cut out in ten minutes.

   We pay for use: money on the balance, everything else is prices. Refusals are shown as
   the server's text, as is: "your balance is empty — top up from
   $5" is the next step, while "no access" would send the person to support
   instead of to payment. */

(function () {
  "use strict";

  const esc = (x) => String(x ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // The page language (`window.ARGUS_LANG`, set by the translation engine): dates and digit
  // grouping follow it, not a hard-coded "ru". The unit labels ("h", "min",
  // "k") are assembled here, in code, and the dictionary would not see them in the
  // ready markup, so they pass through `window.argusT`; without the engine (English) the
  // string is used as is.
  const LOCALE = () => window.ARGUS_LANG || "en";
  const T = (s) => (window.argusT ? window.argusT(s) : s);
  const fmt = (n, opts) => n.toLocaleString(LOCALE(), opts);

  // The reason for a refusal: the server's text (`error`), the text of a refusal by the
  // guard or the framework (`detail`, if it is a string), otherwise a generic phrase. Before,
  // only `error` was taken, and a `{detail: …}` reply showed nothing.
  const reason = (out) => (out && (out.error || (typeof out.detail === "string" && out.detail))) || "Failed";

  // An eye and a crossed-out eye, to peek at a password typed blind. Drawn with lines, like
  // the other icons in the project: font glyphs differ in size between fonts and sit on the
  // line differently.
  const EYE = '<svg viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z"/>'
    + '<circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94"/>'
    + '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>'
    + '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>'
    + '<path d="M1 1l22 22"/></svg>';

  // ------------------------------------------------------------------
  // Terms and policy: read inside the app
  // ------------------------------------------------------------------

  // Pressing "Terms" in the app means reading in the app: a window over the page
  // with the document itself. Not a browser tab: our window is frameless and on top of
  // everything, and throwing the person out of it in the middle of sign-up risks that they
  // will not come back. The text lies inside the build (`/p/legal-<name>`,
  // `legal/build.py`) and is read without a network. One piece of code serves both app pages
  // (the home page and the working one), so it lives here, not in each of them.
  function showDoc(doc) {
    const old = document.getElementById("argus-legal-view");
    if (old) old.remove();
    const title = doc === "terms" ? "Terms of Use" : "Privacy Policy";
    const view = document.createElement("div");
    view.id = "argus-legal-view";
    view.className = "legal-view";
    view.setAttribute("role", "dialog");
    view.setAttribute("aria-modal", "true");
    view.innerHTML = `
      <div class="legal-bar">
        <b>${esc(title)}</b>
        <button type="button" class="legal-close">Close</button>
      </div>
      <iframe class="legal-frame" aria-label="${esc(title)}"
              src="/p/legal-${doc === "terms" ? "terms" : "privacy"}?embed=1"></iframe>`;
    document.body.appendChild(view);

    const close = () => {
      view.remove();
      document.removeEventListener("keydown", onKey, true);
    };
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      // Escape closes only the document window, not what is under it.
      e.stopPropagation();
      close();
    };
    document.addEventListener("keydown", onKey, true);
    // They clicked on the text: focus moved into the document, and keys now go there.
    const frame = view.querySelector("iframe");
    frame.addEventListener("load", () => {
      try { frame.contentDocument.addEventListener("keydown", onKey, true); } catch (e) { /* a foreign address is nothing to fear */ }
    });
    view.querySelector(".legal-close").onclick = close;
    view.querySelector(".legal-close").focus();
  }

  // ------------------------------------------------------------------
  // Shared by the "Account" and "Payment" tabs: icons and numbers
  // ------------------------------------------------------------------

  // Icons are drawn with lines, like the others in the project: font glyphs differ in size
  // between fonts and sit on the line differently.
  const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"
    stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const ICON = {
    stt: svg('<path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"/><path d="M19 11a7 7 0 0 1-14 0M12 18v3"/>'),
    tts: svg('<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>'),
    brain: svg('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12Z"/>'),
    gift: svg('<path d="M20 12v9H4v-9M2 7h20v5H2zM12 22V7"/><path d="M12 7H7.5a2.5 2.5 0 1 1 0-5C11 2 12 7 12 7ZM12 7h4.5a2.5 2.5 0 1 0 0-5C13 2 12 7 12 7Z"/>'),
    plus: svg('<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>'),
    star: svg('<path d="m12 3 2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.8 6.6 19.5l1.2-6L3.3 9.3l6.1-.7Z"/>'),
  };

  // The amount in large type, the cents quieter: at first glance it reads as "how many
  // dollars".
  function bigMoney(micro) {
    const v = Math.max(0, micro) / 1e6;
    let whole = Math.floor(v);
    let cents = Math.round((v - whole) * 100);
    if (cents === 100) { whole += 1; cents = 0; }
    return `<span class="ac-cur">$</span>${fmt(whole)}`
      + `<span class="ac-cents">.${String(cents).padStart(2, "0")}</span>`;
  }

  // Micro-dollars -> "$0.006". The same rule as on the server (`pricing.money`): small
  // amounts get three decimals, otherwise the price of a half-cent reply would show as
  // "$0.01".
  function money(micro) {
    const v = micro / 1e6;
    return "$" + (v > 0 && v < 0.1 ? v.toFixed(3) : v.toFixed(2));
  }

  // If the price list did not load (old server, no connection), the "Payment" tab
  // still shows what to do: the same amounts and limits as on the server
  // (`pricing.TOPUP_PRESETS`, `MIN_TOPUP`, `MAX_TOPUP`). The one thing a person comes there
  // for with an empty balance must not vanish together with the price list. A refusal, if
  // there is one, will come from the server as text on the click.
  const FALLBACK = { topups: [5, 10, 20, 50], min_topup: 5, max_topup: 500 };

  // The server reply arrives over plain HTTP, so it is foreign input: everything that later
  // goes into the markup as a number (top-up amounts, limits, number of days) is converted
  // to a number right on receipt. Remembering `esc` in each of a dozen places is not
  // possible: forget one, and a script from the reply runs in the app page.
  const posInt = (x, fallback) => {
    const v = Number(x);
    return Number.isInteger(v) && v > 0 ? v : fallback;
  };
  function cleanPrice(p) {
    const topups = (Array.isArray(p.topups) ? p.topups : []).map(Number)
      .filter((d) => Number.isInteger(d) && d > 0);
    return {
      ...p,
      topups: topups.length ? topups : FALLBACK.topups,
      min_topup: posInt(p.min_topup, FALLBACK.min_topup),
      max_topup: posInt(p.max_topup, FALLBACK.max_topup),
    };
  }
  // The dictionary's own key, not an inherited one: `kind: "constructor"` from a reply must
  // not be found in `EVENT`/`KIND`.
  const own = (table, key) => (Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined);

  // Numbers for tables. Hours with tenths; large quantities in thousands, otherwise
  // "208 333 characters" reads slower than "208 thousand".
  function hoursText(h) {
    if (h < 1) return T(`${Math.max(1, Math.round(h * 60))} min`);
    return T(`${fmt(Math.round(h * 10) / 10)} h`);
  }
  function count(n) {
    if (n >= 100000) return T(`${fmt(Math.round(n / 1000))}K`);
    if (n >= 10000) return T(`${fmt(Math.round(n / 100) / 10)}K`);
    return fmt(Math.round(n));
  }

  // ------------------------------------------------------------------
  // Prices
  // ------------------------------------------------------------------

  // What costs what, and on the right, how much of it each top-up amount buys. This way the
  // price reads not as "$0.24 per thousand" (is that a lot or a little?) but as "for $10,
  // forty thousand characters of voice-over". The prices already include our markup (the
  // server does not send it): the person cares about the price, not about what it is made
  // of. There are no model names and never will be: replies are divided into levels, and
  // what is under them is our own business.
  function ratesBlock(price) {
    if (!price) {
      return `<section class="ac-sec"><h3 class="ac-h">Pricing</h3><div class="ac-box">
        <div class="ac-empty">Pricing isn't available right now: no connection</div></div></section>`;
    }
    const amounts = price.topups;
    // The unit of measure sits under the service name, not in every cell: a column of numbers
    // reads faster when it holds only numbers.
    const row = (icon, name, unit, cost, costHint, cells) => `
      <tr>
        <td><span class="ac-svc"><i class="ico">${ICON[icon]}</i>
              <span>${name}<small>${unit}</small></span></span></td>
        <td class="r">${cost}${costHint ? `<small>${costHint}</small>` : ""}</td>
        ${cells.map((c) => `<td class="r">${c}</td>`).join("")}
      </tr>`;
    const micro = (d) => d * 1e6;
    const rows = [
      row("stt", "Speech recognition", "hours of speech",
          `${esc(price.stt_hour)} / hour`, `${esc(price.stt_minute)} / min`,
          amounts.map((d) => hoursText(micro(d) / price.stt_hour_micro))),
      // "Smart recognition": a server without this
      // price simply does not send it.
      ...(price.stt_hour_high_micro ? [row("stt", "Smart speech recognition", "hours of speech",
          `${esc(price.stt_hour_high)} / hour`, `${esc(price.stt_minute_high)} / min`,
          amounts.map((d) => hoursText(micro(d) / price.stt_hour_high_micro)))] : []),
      row("tts", "Voice replies", "characters",
          esc(price.tts_1000), "per 1,000 characters",
          amounts.map((d) => count(micro(d) / price.tts_1000_micro * 1000))),
      ...price.answers.map((m) => row("brain",
          esc(m.title), "number of answers",
          `≈ ${esc(m.answer)}`, "per answer",
          amounts.map((d) => count(Math.floor(micro(d) / m.answer_micro))))),
    ];
    return `
      <section class="ac-sec">
        <h3 class="ac-h">Pricing</h3>
        <p class="ac-sub">You pay only for what you use. On the right is how much each top-up amount
          covers. An answer with a long context or a screen costs more than a typical one.</p>
        <div class="ac-box ac-scroll">
          <table class="ac-table">
            <thead>
              <tr class="grp"><th colspan="2"></th>
                  <th class="r grp" colspan="${amounts.length}">What each amount covers</th></tr>
              <tr><th>Service</th><th class="r">Price</th>
                  ${amounts.map((d) => `<th class="r">$${d}</th>`).join("")}</tr>
            </thead>
            <tbody>${rows.join("")}</tbody>
          </table>
        </div>
      </section>`;
  }

  // ------------------------------------------------------------------
  // History: where the money in the account came from, from the journal
  // ------------------------------------------------------------------

  const EVENT = {
    gift: ["Welcome gift", "gift"],
    topup: ["Top-up", "plus"],
    grant: ["Added manually", "star"],
    legacy: ["Carried over from the subscription", "plus"],
  };
  function historyBlock(rows) {
    const date = (t) => new Date(t * 1000).toLocaleDateString(LOCALE(),
      { day: "numeric", month: "short", year: "numeric" });
    return `
      <section class="ac-sec">
        <h3 class="ac-h">Top-up history</h3>
        ${rows.length ? `
        <div class="ac-box ac-scroll">
          <table class="ac-table">
            <thead><tr><th>Event</th><th>Date</th><th class="r">Amount</th></tr></thead>
            <tbody>${rows.map((x) => {
              const [name, icon] = own(EVENT, x.kind) || [esc(x.kind), "plus"];
              return `<tr><td><span class="ac-svc"><i class="ico">${ICON[icon]}</i>${name}</span></td>
                  <td class="nowrap dim">${esc(date(x.at))}</td>
                  <td class="r">+${esc(x.amount)}</td></tr>`;
            }).join("")}
            </tbody>
          </table>
        </div>` : `<div class="ac-box"><div class="ac-empty">No top-ups yet</div></div>`}
      </section>`;
  }

  // ------------------------------------------------------------------
  // Spending: a chart by day and a table by service, the "Usage" tab
  // ------------------------------------------------------------------

  // The kinds of spending: what they are called, how they are shown, and in what shade they
  // are drawn on the chart. The shades are of one color: the denser, the bigger the
  // service. The chart is about "when and how much", and a rainbow would only get in the way
  // here.
  const KIND = {
    stt_seconds: { name: "Speech recognition", short: "Recognition", cls: "k0", icon: "stt",
                   fmt: (u) => hoursText(u / 3600) },
    brain_tokens: { name: "Answers", short: "Answers", cls: "k1", icon: "brain",
                    fmt: (u, v) => (v && v.n != null ? count(v.n) : "—") },
    tts_chars: { name: "Voice replies", short: "Voice-over", cls: "k2", icon: "tts",
                 fmt: (u) => T(`${count(u)} chars`) },
  };
  const ORDER = ["stt_seconds", "brain_tokens", "tts_chars"];   // bottom to top

  // Spending bars by day, each one stacked from services. Drawn as SVG without a library: a
  // dozen rectangles are not worth a dependency. Days without spending are a thin tick on
  // the axis, not emptiness, so you can see that the day existed. `width` is the width of
  // the space under the chart (`el.clientWidth` of the tab): a module-level function does
  // not see its own container.
  function chart(daily, width) {
    if (!daily || !daily.length) {
      return `<div class="ac-empty">No spending data yet</div>`;
    }
    // The canvas width is the width of the space under it, one to one. Otherwise on a narrow
    // panel the labels would shrink together with the picture to an unreadable five pixels. A
    // wide panel is capped: the bars do not need more.
    const W = Math.max(300, Math.min(660, Math.round((width || 660) - 40)));
    const H = 190, L = 46, B = 24, T = 10;
    const n = daily.length;
    const max = Math.max(...daily.map((d) => d.micro), 10000);    // no less than a cent
    const slot = (W - L) / n;
    const bw = Math.max(3, Math.min(16, slot * 0.6));
    const base = H - B, span = H - T - B;
    const date = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(LOCALE(),
      { day: "numeric", month: "short" });

    const bars = daily.map((d, i) => {
      const x = L + i * slot + (slot - bw) / 2;
      const last = i === n - 1;
      if (!d.micro) {
        return `<rect class="zero" x="${x.toFixed(1)}" y="${base - 1}" width="${bw.toFixed(1)}" height="1"/>
                <rect class="hit" data-i="${i}" x="${(L + i * slot).toFixed(1)}" y="${T}"
                      width="${slot.toFixed(1)}" height="${span}"/>`;
      }
      // The bar height is proportional to the amount but no less than two pixels: a day with
      // pennies must not vanish from the chart.
      const total = Math.max(2, span * d.micro / max);
      let y = base;
      const parts = ORDER.filter((k) => (d.by_kind[k] || 0) > 0);
      const segs = parts.map((k, j) => {
        const h = total * d.by_kind[k] / d.micro;
        y -= h;
        const topmost = j === parts.length - 1;
        const r = Math.min(3, bw / 2, h);
        const shape = topmost
          ? `<path d="M${x.toFixed(1)} ${(y + h).toFixed(1)}V${(y + r).toFixed(1)}Q${x.toFixed(1)} ${y.toFixed(1)} ${(x + r).toFixed(1)} ${y.toFixed(1)}H${(x + bw - r).toFixed(1)}Q${(x + bw).toFixed(1)} ${y.toFixed(1)} ${(x + bw).toFixed(1)} ${(y + r).toFixed(1)}V${(y + h).toFixed(1)}Z"/>`
          : `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}"/>`;
        return `<g class="${KIND[k].cls}">${shape}</g>`;
      }).join("");
      return `<g class="ac-bar${last ? " now" : ""}">${segs}
        <rect class="hit" data-i="${i}" x="${(L + i * slot).toFixed(1)}" y="${T}"
              width="${slot.toFixed(1)}" height="${span}"/></g>`;
    }).join("");

    // Three lines: zero, half, and the scale maximum. No more are needed: they give the order
    // of magnitude, and the exact number is in the tooltip.
    const grid = [0, .5, 1].map((f, j) => {
      const yy = (base - span * f).toFixed(1);
      return `<line class="${j ? "dash" : "base"}" x1="${L}" x2="${W}" y1="${yy}" y2="${yy}"/>
              <text x="${L - 8}" y="${(Number(yy) + 3.5).toFixed(1)}" text-anchor="end">${
                esc(money(Math.round(max * f)))}</text>`;
    }).join("");

    // Axis labels: five, evenly spaced, so they do not stick together and "when" can be read.
    const marks = [...new Set([0, .25, .5, .75, 1].map((f) => Math.round((n - 1) * f)))];
    const axis = marks.map((i) => {
      const cx = L + i * slot + slot / 2;
      const anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
      const ax = i === 0 ? L : i === n - 1 ? W : cx;
      return `<text x="${ax.toFixed(1)}" y="${H - 7}" text-anchor="${anchor}">${esc(date(daily[i].day))}</text>`;
    }).join("");

    return `
      <svg class="ac-chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
           aria-label="Spending by day">
        <g class="grid">${grid}</g>
        ${bars}
        <g class="axis">${axis}</g>
      </svg>
      <div class="ac-tip" hidden></div>`;
  }

  // A tooltip over a bar: the day, the amount, and what it is made of. Our own, not the
  // browser's `<title>`: that one appears after a second and looks foreign.
  function wireChart(el, daily) {
    const wrap = el.querySelector(".ac-chartwrap");
    const tip = wrap && wrap.querySelector(".ac-tip");
    if (!wrap || !tip || !daily) return;
    const svgEl = wrap.querySelector("svg");
    const date = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(LOCALE(),
      { day: "numeric", month: "long" });
    const show = (hit) => {
      const d = daily[Number(hit.dataset.i)];
      if (!d) return;
      const rows = ORDER.filter((k) => (d.by_kind[k] || 0) > 0).map((k) =>
        `<div><i class="sw ${KIND[k].cls}"></i>${KIND[k].short}<em>${esc(money(d.by_kind[k]))}</em></div>`).join("");
      tip.innerHTML = `<b>${esc(date(d.day))}</b><strong>${esc(money(d.micro))}</strong>${rows || `<div class="none">no spending</div>`}`;
      tip.hidden = false;
      const w = wrap.getBoundingClientRect(), h = hit.getBoundingClientRect();
      const left = h.left - w.left + h.width / 2 - tip.offsetWidth / 2;
      tip.style.left = `${Math.max(0, Math.min(w.width - tip.offsetWidth, left))}px`;
      tip.style.top = "0px";
    };
    svgEl.addEventListener("mouseover", (e) => { if (e.target.classList.contains("hit")) show(e.target); });
    svgEl.addEventListener("click", (e) => { if (e.target.classList.contains("hit")) show(e.target); });
    svgEl.addEventListener("mouseleave", () => { tip.hidden = true; });
  }

  function usageBlock(spent, daily, width) {
    // No `spent` means no connection (the app gives it only together with the server's reply)
    // or an old server: "$0" would be an invention, so a dash stands in its place.
    const days = daily ? daily.length : 30;
    const kinds = spent ? Object.entries(spent.by_kind || {})
      .filter(([, v]) => v.micro > 0)
      .sort((a, b) => b[1].micro - a[1].micro) : [];
    const rows = kinds.map(([k, v]) => {
      const K = own(KIND, k) || { name: esc(k), icon: "brain", fmt: (u) => count(u) };
      return `<tr><td><span class="ac-svc"><i class="ico">${ICON[K.icon]}</i>${K.name}</span></td>
                  <td class="r">${K.fmt(v.units, v)}</td>
                  <td class="r">${esc(money(v.micro))}</td></tr>`;
    }).join("");

    // What is in the data is what is in the legend: an extra shade with no bars would confuse.
    const present = ORDER.filter((k) => daily && daily.some((d) => (d.by_kind[k] || 0) > 0));
    const legend = present.map((k) =>
      `<span><i class="sw ${KIND[k].cls}"></i>${KIND[k].short}</span>`).join("");

    return `
      <section class="ac-sec first">
        <div class="ac-box ac-chartbox">
          <div class="ac-chead">
            <div>
              <div class="ac-k">Last ${days} days</div>
              <div class="ac-big">${!spent ? "—" : spent.micro ? esc(spent.total) : "$0"}</div>
            </div>
            <div class="ac-legend">${legend}</div>
          </div>
          <div class="ac-chartwrap">${chart(daily, width)}</div>
        </div>
        ${rows ? `
        <div class="ac-box ac-scroll">
          <table class="ac-table">
            <thead><tr><th>Service</th><th class="r">Volume</th><th class="r">Charged</th></tr></thead>
            <tbody>${rows}</tbody>
            <tfoot><tr><td>Total</td><td></td><td class="r">${esc(spent.total)}</td></tr></tfoot>
          </table>
        </div>` : ""}
      </section>`;
  }

  function notice(title, text, button) {
    return `
      <section class="ac-sec first">
        <div class="ac-box ac-empty pay-message">
          <b>${title}</b>
          <p>${text}</p>
          ${button ? `<button type="button" class="cta" data-act="${button[0]}">${button[1]}</button>` : ""}
        </div>
      </section>`;
  }

  // ------------------------------------------------------------------
  // The "Payment" tab: balance, top-up, prices, history
  // ------------------------------------------------------------------

  // A separate tab of the settings window. Requirement: payment gets a tab of its own
  // instead of living under the account tab. Before, the balance, top-up, prices, and
  // history lived in "Account", mixed in with sign-in, spending, and data.
  // "Account" is now about the person (who, terms, spending, data), and "Payment"
  // is about money. The payment itself goes in the browser, on the checkout page
  // (`/admin/account/checkout` opens it): our window is frameless and invisible to capture,
  // so you cannot pay in it. While the person pays, the tab watches the balance and says
  // "credited" by itself: the checkout reports to the server not at once, and a
  // person who came back would decide that the payment failed.
  const WAIT_STEP_MS = 4000;
  const WAIT_STEPS = 225;               // 15 minutes: in that time the person has either paid or changed their mind

  function mountBilling(el, api) {
    api = api || {};
    const changed = api.onChange || (() => {});
    const post = api.post || ((url, body) => fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json()));
    const load = api.get || ((url) => fetch(url, { cache: "no-store" }).then((r) => r.json()));

    let state = null;          // the last account state; null means it has not arrived yet
    let failed = false;        // no connection to the app
    let price = null;
    let hist = [];
    let dollars = null;        // the chosen amount: a number (including an invalid one); null while nothing is chosen
    let view = "pick";         // pick → waiting → paid | late
    let order = null;          // the checkout reply: pay_url, opened, dollars
    let paid = null;           // {added, now}, micro-dollars
    let busy = false;
    let watching = 0;
    let before = null;         // the balance before payment, micro-dollars: "credited" is when it has grown;
                               // null means we do not know yet

    const limits = () => price || FALLBACK;
    const validAmount = (d) => Number.isInteger(d) && d >= limits().min_topup && d <= limits().max_topup;
    const pick = (name) => el.querySelector(`[data-act="${name}"]`);

    async function fetchState() {
      try {
        const st = await load("/admin/account");
        // The account server is not configured: the route answers with a refusal without a
        // `signed_in` field.
        return "signed_in" in st ? st : { signed_in: false, missing: true };
      } catch (e) { return null; }
    }

    async function refresh() {
      const st = await fetchState();
      failed = !st;
      if (st) state = st;
      if (state && state.signed_in) {
        const [p, h] = await Promise.allSettled([load("/admin/account/pricing"), load("/admin/account/history")]);
        const ok = (x) => x.status === "fulfilled" && x.value && x.value.ok !== false ? x.value : null;
        const pv = ok(p);
        // The price list is foreign input (see `cleanPrice`); without the reply levels there is
        // nothing to compute "enough for…" from.
        if (pv && Array.isArray(pv.answers) && pv.answers.length) price = cleanPrice(pv);
        const credits = (ok(h) || {}).credits;
        hist = Array.isArray(credits) ? credits : hist;
      }
      if (dollars === null) {
        const t = limits().topups;
        dollars = t[1] ?? t[0];
      }
      draw();
    }

    // The amount in the account in large type and three numbers under it, answering "and will
    // this last long?". We compute "enough for…" from the cheapest reply level
    // and honestly say "up to": on "think longer" it will be less.
    function balanceBlock(st) {
      const r = st.right || {};
      // An old server does not know about money yet and sends the right without a balance.
      // Showing "$0" would tell the person there is no money, which is untrue: there is just
      // nothing to compute it from. A dash with the reason is more honest.
      const known = "balance_micro" in r;
      const tone = !known ? "" : !r.allowed ? "bad" : r.low ? "warn" : "";
      const badge = !known ? "" : !r.allowed ? `<span class="ac-badge bad">Empty</span>`
        : r.low ? `<span class="ac-badge warn">Low</span>` : "";
      const note = !known
        ? "Your balance isn't available yet. Check back later."
        : !r.allowed ? `Recognition and answers are stopped. Top up from $${limits().min_topup}` : "";
      let stats = "";
      if (known) {
        // Without a connection the spending is not in the reply at all: a dash, not "$0" (that
        // would be untrue about a person who is spending).
        const spent = st.offline ? null : st.spent;
        const cells = [[`Spent in ${posInt(spent && spent.days, 30)} days`,
          !spent ? "—" : spent.micro ? esc(spent.total) : "$0"]];
        if (price && r.allowed) {
          const cheapest = price.answers.reduce((a, b) => (b.answer_micro < a.answer_micro ? b : a));
          cells.push(["Enough for speech", `≈ ${hoursText(r.balance_micro / price.stt_hour_micro)}`]);
          cells.push(["Enough for answers", `up to ${count(Math.floor(r.balance_micro / cheapest.answer_micro))}`]);
        }
        stats = `<div class="ac-stats">${cells.map(([k, v]) =>
          `<div class="ac-stat"><span>${k}</span><b>${v}</b></div>`).join("")}</div>`;
      }
      return `
        <section class="ac-sec first">
          <div class="ac-box ac-balbox ${st.offline ? "stale" : ""}">
            <div class="ac-bal">
              <div class="ac-bal-main">
                <div class="ac-k">Balance ${badge}</div>
                <div class="ac-sum ${tone}">${known ? bigMoney(r.balance_micro) : "—"}</div>
                ${note ? `<div class="ac-note ${tone}">${note}</div>` : ""}
              </div>
            </div>
            ${stats}
          </div>
        </section>`;
    }

    function pickBlock() {
      const t = limits();
      return `
        <section class="ac-sec">
          <h3 class="ac-h">Top-up amount</h3>
          <div class="pay-tiles">
            ${t.topups.map((d) => `
              <button type="button" class="pay-tile" data-dollars="${d}" aria-pressed="false">
                <b>$${d}</b>${price ? `<span>≈ ${hoursText(d * 1e6 / price.stt_hour_micro)} of speech</span>` : ""}
              </button>`).join("")}
          </div>
          <div class="pay-custom">
            <div class="ac-pay" data-act="custom-box">
              <span class="ac-pre">$</span>
              <input type="number" inputmode="numeric" step="1" data-act="custom"
                     min="${t.min_topup}" max="${t.max_topup}"
                     placeholder="Other amount" aria-label="Other amount, in dollars">
            </div>
            <span class="ac-sub">Whole dollars, from $${t.min_topup} to $${t.max_topup}.</span>
          </div>
          <div class="ac-error" data-act="error" hidden></div>
          <div class="ac-box pay-sum" data-act="sum" hidden></div>
          <button type="button" class="cta pay-go" data-act="go">Pay</button>
          <p class="ac-sub pay-fine">Payment opens in your browser, on the payment service's page. The balance tops up by itself as soon as the payment goes through.</p>
          ${price && !price.payment
            ? `<div class="note warn">Payment isn't connected yet. Write to us and we'll top up your balance by hand.</div>` : ""}
        </section>`;
    }

    function waitingBlock() {
      const opened = order && order.opened;
      const url = order && order.pay_url;
      return `
        <section class="ac-sec">
          <div class="ac-box pay-status">
            <div class="pay-spin" aria-hidden="true"></div>
            <div>
              <h3>Waiting for payment${order && order.dollars ? ` — $${order.dollars}` : ""}</h3>
              <p>${opened
                ? "The payment page is open in your browser. As soon as the payment goes through, the balance will update here by itself."
                : "The payment page didn't open by itself. Open it with the link in your browser — the balance will update here as soon as the payment goes through."}</p>
              ${!opened && url ? `<span class="pay-url">${esc(url)}</span>` : ""}
              <div class="pay-row">
                <button type="button" class="cta" data-act="again">${opened ? "Open the payment page again" : "Try opening it again"}</button>
                <button type="button" class="cta ghost" data-act="other">Choose another amount</button>
              </div>
              <div class="ac-error" data-act="error" hidden></div>
            </div>
          </div>
        </section>`;
    }

    function paidBlock() {
      return `
        <section class="ac-sec">
          <div class="ac-box pay-status">
            <div class="pay-mark">${svg('<path d="M20 6 9 17l-5-5"/>')}</div>
            <div>
              <h3>Balance topped up by ${money(paid.added)}</h3>
              <p>Your balance is now ${money(paid.now)}.</p>
              <div class="pay-row"><button type="button" class="cta" data-act="done">Done</button></div>
            </div>
          </div>
        </section>`;
    }

    function lateBlock() {
      return `
        <section class="ac-sec">
          <div class="ac-box pay-status">
            <div>
              <h3>No payment confirmation yet</h3>
              <p>If you've already paid, the money will arrive a little later and the balance will update here. If not, choose the amount again.</p>
              <div class="pay-row">
                <button type="button" class="cta" data-act="recheck">Check now</button>
                <button type="button" class="cta ghost" data-act="other">Choose another amount</button>
              </div>
              <div class="ac-error" data-act="error" hidden></div>
            </div>
          </div>
        </section>`;
    }

    function draw() {
      if (!state) {
        el.innerHTML = failed
          ? notice("No connection", "Couldn't get the account status. Check your connection and try again.",
              ["retry", "Retry"])
          : "";
        const retry = pick("retry");
        if (retry) retry.onclick = refresh;
        return;
      }
      if (state.missing) {
        el.innerHTML = notice("There are no accounts here", "This version works without an account, so there's nothing to top up.");
        return;
      }
      if (!state.signed_in) {
        el.innerHTML = notice("Sign-in required", "To top up your balance, sign in to your account.", ["signin", "Sign in"]);
        pick("signin").onclick = () => { if (window.argusWin) window.argusWin.open("account"); };
        return;
      }
      const offline = state.offline || failed
        ? `<div class="note warn">No connection — showing the last known data.</div>` : "";
      const body = view === "waiting" ? waitingBlock()
        : view === "paid" ? paidBlock()
        : view === "late" ? lateBlock()
        : pickBlock();
      el.innerHTML = offline + balanceBlock(state) + body + ratesBlock(price) + historyBlock(hist);
      wire();
      // The custom amount survives a redraw: the field is created anew, but the chosen number
      // stayed in `dollars`.
      const custom = pick("custom");
      if (custom && dollars !== null && !limits().topups.includes(dollars)) custom.value = dollars;
      refreshChoice();
    }

    // Choosing an amount updates only its own parts (tiles, total, button) and does not redraw
    // the tab. Otherwise the "Other amount" field would lose focus on every
    // digit.
    function refreshChoice() {
      if (view !== "pick") return;
      const preset = limits().topups.includes(dollars);
      el.querySelectorAll(".pay-tile").forEach((b) =>
        b.setAttribute("aria-pressed", String(Number(b.dataset.dollars) === dollars)));
      const box = pick("custom-box");
      if (box) box.classList.toggle("on", !preset && dollars !== null);

      const ok = validAmount(dollars);
      const sum = pick("sum");
      if (sum) {
        if (price && ok) {
          const m = dollars * 1e6;
          const cheapest = price.answers.reduce((a, b) => (b.answer_micro < a.answer_micro ? b : a));
          const cell = (name, value) => `<div class="ac-stat"><span>${name}</span><b>${value}</b></div>`;
          sum.innerHTML = `<div class="pay-sum-h">$${dollars} covers</div><div class="ac-stats">`
            + cell("Speech recognition", `≈ ${hoursText(m / price.stt_hour_micro)}`)
            + cell("Answers", `up to ${count(Math.floor(m / cheapest.answer_micro))}`)
            + cell("Voice replies", `≈ ${T(`${count(m / price.tts_1000_micro * 1000)} chars`)}`)
            + `</div>`;
          sum.hidden = false;
        } else {
          sum.hidden = true;
        }
      }
      const go = pick("go");
      if (go) {
        go.textContent = ok ? `Pay $${dollars}` : "Pay";
        go.disabled = !ok || busy;
      }
    }

    function showError(text) {
      const err = pick("error");
      if (!err) return;
      err.textContent = text || "";
      err.hidden = !text;
    }

    function wire() {
      el.querySelectorAll(".pay-tile").forEach((b) => {
        b.onclick = () => {
          dollars = Number(b.dataset.dollars);
          const c = pick("custom");
          if (c) c.value = "";
          showError("");
          refreshChoice();
        };
      });
      const custom = pick("custom");
      if (custom) {
        custom.oninput = () => {
          dollars = custom.value === "" ? null : Number(custom.value);
          showError("");
          refreshChoice();
        };
        custom.onkeydown = (e) => { if (e.key === "Enter") startPayment(false); };
      }
      const bind = (name, fn) => { const b = pick(name); if (b) b.onclick = fn; };
      bind("go", () => startPayment(false));
      bind("again", () => startPayment(true));
      bind("other", () => { watching += 1; view = "pick"; draw(); });
      // "Done": back to the selection view at once, and the top-up history is read
      // anew, because it now has a new row.
      bind("done", () => { view = "pick"; draw(); refresh(); });
      bind("recheck", recheck);
    }

    async function startPayment(again) {
      if (busy) return;
      const amount = again && order ? order.dollars : dollars;
      if (!validAmount(amount)) {
        showError(`Whole dollars only, from $${limits().min_topup} to $${limits().max_topup}`);
        return;
      }
      busy = true;
      const btn = pick(again ? "again" : "go");
      const was = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "…"; }
      try {
        // The "before" balance is fresh, taken right before payment. While the person was choosing
        // an amount, spending could have moved it, and "credited" against a stale
        // number would fire at the wrong time or not at all. Reopening the payment page does not
        // touch the baseline: the previous payment might have already gone through, and a new
        // number would hide it.
        if (!again) before = await baseline();
        const out = await post("/admin/account/checkout", { dollars: amount });
        // Only an explicit "ok": a reply without it is a refusal by the guard or the framework
        // (`{detail}`), not an order.
        if (!out || out.ok !== true) {
          if (btn) { btn.textContent = was; btn.disabled = false; }
          showError(reason(out));
          return;
        }
        order = { ...out, dollars: amount };
        view = "waiting";
        draw();
        watch();
      } catch (e) {
        if (btn) { btn.textContent = was; btn.disabled = false; }
        showError("Can't reach the app. Try again.");
      } finally {
        busy = false;
        refreshChoice();
      }
    }

    // The balance before payment is a fresh reply from the app, not what remained from when
    // the tab was opened. If we could not find out (no connection, a server without a
    // balance), it is null: `check` will take as the basis the first balance that arrives
    // later.
    async function baseline() {
      const st = await fetchState();
      if (st && st.signed_in) state = st;
      const b = state && state.right && state.right.balance_micro;
      return typeof b === "number" ? b : null;
    }

    // One look at the account: did it grow compared with what it was before payment. If it
    // grew, "credited". How much was credited is told by the order amount, not by
    // the difference of balances: while payment was in progress, recognition was spending
    // money, and "$4.99" instead of "$5" would be untrue about the payment.
    async function check() {
      const st = await fetchState();
      if (view !== "waiting" && view !== "late") return false;   // during the request the person moved to a different amount
      if (!st || !st.signed_in) return false;
      const now = st.right && st.right.balance_micro;
      if (typeof now !== "number") return false;
      state = st;
      if (before === null) { before = now; return false; }
      if (!(now > before)) return false;
      paid = { added: order && order.dollars ? order.dollars * 1e6 : now - before, now };
      view = "paid";
      changed();
      draw();
      refresh();                     // the top-up history and the price list again: the history now has a new row
      return true;
    }

    // "Check now" and showing the tab again while payment is not confirmed:
    // the same look at the account as the watcher's, not just a redraw (otherwise the
    // "no confirmation" card would hang over an already new amount).
    async function recheck() {
      const b = pick("recheck");
      if (b) b.disabled = true;
      await check();
      if (view === "late") draw();   // not credited: the card stays, and the balance above it is fresh
    }

    async function watch() {
      const mine = ++watching;                // a new top-up replaces the previous watch
      for (let i = 0; i < WAIT_STEPS; i++) {
        await new Promise((resolve) => setTimeout(resolve, WAIT_STEP_MS));
        if (mine !== watching || !el.isConnected) return;
        if (await check()) return;
      }
      if (mine === watching) { view = "late"; draw(); }
    }

    refresh();
    // The tab was opened again: signing in under "Account" or paying in the browser
    // could have changed the state. In the ordinary view we reread everything. While we wait
    // for payment or "no confirmation" is shown, we look at the account
    // (the money could have arrived while the tab was not visible). We do not touch the
    // payment result while the person is reading it.
    return { refresh: () => (view === "pick" ? refresh() : view === "paid" ? undefined : recheck()) };
  }


  // ------------------------------------------------------------------
  // The "Usage" tab: spending by day and by service
  // ------------------------------------------------------------------

  // A separate tab of the settings window. Requirement: usage gets a tab of its own. The
  // chart and the table lived in "Account", mixed in with terms and data. Where
  // the numbers come from: `spent` arrives with the account state (`/admin/account`), the
  // spending by day arrives in a separate request, and the tab is completed as the second
  // one arrives.
  function mountUsage(el, api) {
    api = api || {};
    const load = api.get || ((url) => fetch(url, { cache: "no-store" }).then((r) => r.json()));
    let state = null;
    let daily = null;
    let failed = false;

    async function refresh() {
      let st = null;
      try {
        const r = await load("/admin/account");
        // The account server is not configured: the route answers with a refusal without a
        // `signed_in` field.
        st = "signed_in" in r ? r : { signed_in: false, missing: true };
      } catch (e) { /* no connection: we will show the last known state or "No connection" */ }
      failed = !st;
      if (st) state = st;
      draw();
      if (state && state.signed_in) {
        const tz = -new Date().getTimezoneOffset();
        try {
          const u = await load(`/admin/account/usage?tz=${tz}`);
          if (u && u.ok !== false) daily = u.daily || null;
        } catch (e) { /* without the chart: the table and the total are there anyway */ }
        if (el.isConnected) draw();
      }
    }

    function draw() {
      if (!state) {
        el.innerHTML = failed
          ? notice("No connection", "Couldn't get the account status. Check your connection and try again.",
              ["retry", "Retry"])
          : "";
        const retry = el.querySelector('[data-act="retry"]');
        if (retry) retry.onclick = refresh;
        return;
      }
      if (state.missing) {
        el.innerHTML = notice("There are no accounts here", "This version works without an account, so there's nothing to track.");
        return;
      }
      if (!state.signed_in) {
        el.innerHTML = notice("Sign-in required", "To see your spending, sign in to your account.", ["signin", "Sign in"]);
        el.querySelector('[data-act="signin"]').onclick = () => { if (window.argusWin) window.argusWin.open("account"); };
        return;
      }
      // Without a connection the spending did not arrive: we say so honestly, rather than draw
      // "$0" (in the block itself a dash stands in its place).
      const offline = state.offline ? `<div class="note warn">No connection: no data</div>` : "";
      el.innerHTML = offline + usageBlock(state.spent, daily, el.clientWidth);
      wireChart(el, daily);
    }

    refresh();
    return { refresh };
  }

  function mount(el, api) {
    api = api || {};
    const say = api.toast || ((text) => console.log(text));
    const mark = api.mark || (() => {});
    const changed = api.onChange || (() => {});
    const post = api.post || ((url, body) => fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json()));
    const load = api.get || ((url) => fetch(url).then((r) => r.json()));

    let email = "";
    let stage = "in";      // in | up | reset: sign-in, registration, recovery
    let busy = false;

    // ------------------------------------------------------------------
    // Documents: terms and privacy policy
    // ------------------------------------------------------------------

    // Translation of a string from code. The page engine translates ready markup by itself,
    // but a phrase with links inside is beyond it: word order differs between languages, and
    // a key with `<a href>` would carry the address. So the phrase is assembled here: first
    // the template with the places `{terms}` and `{privacy}` is translated, then the places
    // are replaced with links. Without the engine (English, the landing page) the string
    // stays as it is.
    const tr = (s) => (window.argusT ? window.argusT(s) : s);

    // The documents themselves are in English only (`legal/`): the link labels are translated
    // together with the interface, the text behind them is not. The address is real
    // (`/p/legal-…`), so the link can also be opened in a browser tab; a click, however, shows
    // the document in a window over the page (`showDoc`).
    const docLink = (doc, label) =>
      `<a class="ac-doc" data-doc="${doc}" href="/p/legal-${doc}">${esc(label)}</a>`;
    // A phrase with two links. The names are put in whatever grammatical case the phrase
    // requires.
    const withDocs = (template, terms, privacy) => esc(tr(template))
      .replace("{terms}", () => docLink("terms", tr(terms)))
      .replace("{privacy}", () => docLink("privacy", tr(privacy)));

    el.addEventListener("click", (e) => {
      const a = e.target.closest && e.target.closest("a[data-doc]");
      if (!a) return;
      e.preventDefault();
      showDoc(a.dataset.doc);
    });

    async function refresh() {
      let state;
      try {
        state = await load("/admin/account");
        if (!state || typeof state !== "object") throw new Error("couldn't parse the reply");
      } catch (e) {
        // No connection is not a dead end: "Retry", as in the "Payment" and
        // "Usage" tabs. Before, a bare string stood here, and the only way out was
        // to close and reopen the window.
        el.innerHTML = notice("No connection", "Couldn't get the account status. Check your connection and try again.",
          ["retry", "Retry"]);
        bind("retry", refresh);
        return;
      }
      // The account server is not configured. That is not a breakage but an ordinary build on
      // its own keys, and the sign-in window is not hidden for it: the person opened it
      // themselves, with a button, and an empty place instead would look like a malfunction, not
      // a decision. The form is drawn as usual. The fact that there is no server will be
      // reported in an honest text from the server, at the moment when it comes to that
      // (sign-in, registration), and not earlier.
      draw("signed_in" in state ? state : { signed_in: false });
    }

    // What we remember between redraws: the state was updated (`refresh`) while the person is
    // just entering an amount or reading a table, and what was expanded does not collapse.
    let cur = null;                // the last account state

    function draw(state) {
      // The dot on the menu item stays until sign-in is done. After that it is not needed: the
      // gauge itself will say that the money has run out, when the button is pressed.
      mark("account", !state.signed_in);
      if (!state.signed_in) return drawSignIn();

      cur = state;

      // The tab about the person: who you are (a quiet line above everything, a reference and
      // not a section) and the password change. There is no "Data" block (a copy of the
      // data, account deletion): it was judged useless and removed. Money is the "Payment"
      // tab, spending is the "Usage" tab (`mountBilling`,
      // `mountUsage` above): they need their own headings and their own place.
      el.innerHTML = `
        <div class="ac-box ac-top">
          <span class="ac-ava" aria-hidden="true">${esc((state.email || "?").charAt(0).toUpperCase())}</span>
          <span class="ac-mail" data-i18n="off">${esc(state.email)}</span>
          <button class="cta out" data-act="out">Sign out</button>
        </div>
        ${state.offline
          ? `<div class="note warn">No connection — showing the last known data.</div>`
          : ""}
        <div id="ac-pane"></div>`;

      bind("out", async (b) => {
        if (busy) return;
        busy = true;
        b.disabled = true;
        try {
          const out = await post("/admin/account/sign-out", {});
          if (!out || out.ok !== true) { say(reason(out)); return; }
        } catch (e) {
          say("Can't reach the app");
          return;
        } finally {
          busy = false;
          b.disabled = false;
        }
        // The tab redraws itself: the page's `onChange` hook only hides its own things (the strip,
        // the sign-in screen behind the window), and "Account" itself would otherwise
        // stay with the email and "Sign out" for a person who has already signed out.
        stage = "in";
        changed();
        refresh();
      });

      paint();
    }

    function paint() {
      const pane = el.querySelector("#ac-pane");
      if (!pane || !cur) return;
      pane.innerHTML = passwordBlock();
      wirePassword();
    }

    // ------------------------------------------------------------------
    // Password
    // ------------------------------------------------------------------

    // Password change in the app. It was added in place of the useless "Data" block,
    // to put something useful on this page. The server route (`/auth/password`) had existed
    // for a long time, but the app had neither a button nor a call. The server closes all
    // sign-ins of the account, and the client (`change_password`) immediately signs in again
    // with the new password, so the person stays in the account here.
    const MIN_PASSWORD = 8;   // as `MIN_PASSWORD` on the server; if the number differs, the server will name it itself
                              // in the refusal
    let pwDone = false;       // the password was just changed: the line saying so stays until the form is opened again

    function passwordBlock() {
      return `
        <section class="ac-sec">
          <h3 class="ac-h">Password</h3>
          <div class="ac-box ac-databox">
            <div class="ac-sub">${pwDone
              ? "Password changed. On your other devices you'll need to sign in again."
              : "You can change your password at any time. After that, you'll need to sign in again on your other devices."}</div>
            <div class="ac-row"><button data-act="pw-open">Change password</button></div>
            <div class="ac-form" data-act="pw-form" hidden>
              <input class="ac-input" type="password" data-act="pw-old" placeholder="Current password"
                     autocomplete="current-password">
              <input class="ac-input" type="password" data-act="pw-new" placeholder="New password, at least ${MIN_PASSWORD} characters"
                     autocomplete="new-password">
              <input class="ac-input" type="password" data-act="pw-again" placeholder="New password again"
                     autocomplete="new-password">
              <div class="ac-error" data-act="pw-error" hidden></div>
              <div class="ac-row">
                <button class="cta" data-act="pw-go">Change</button>
                <button data-act="pw-cancel">Cancel</button>
              </div>
            </div>
          </div>
        </section>`;
    }

    function wirePassword() {
      const form = pick("pw-form");
      const open = pick("pw-open");
      if (!form || !open) return;
      const fields = ["pw-old", "pw-new", "pw-again"].map(pick);
      const [oldF, newF, again] = fields;
      const err = pick("pw-error");
      const show = (text) => { err.textContent = text || ""; err.hidden = !text; };
      const close = () => {
        form.hidden = true;
        open.parentElement.hidden = false;
        fields.forEach((f) => { f.value = ""; });
        show("");
      };

      open.onclick = () => {
        pwDone = false;
        form.hidden = false;
        open.parentElement.hidden = true;      // the button gives way to the form
        oldF.focus();
      };
      bind("pw-cancel", close);

      const go = async () => {
        if (!oldF.value) return show("Enter your current password");
        if (newF.value.length < MIN_PASSWORD) return show(`The new password is shorter than ${MIN_PASSWORD} characters`);
        if (newF.value !== again.value) return show("The passwords don't match");
        if (newF.value === oldF.value) return show("The new password is the same as the old one");
        if (busy) return;
        show("");
        busy = true;
        const b = pick("pw-go");
        const was = b.textContent;
        b.disabled = true;
        b.textContent = "…";
        try {
          const out = await post("/admin/account/password", { old: oldF.value, new: newF.value });
          // Only an explicit "ok": an app older than this button answers an unknown route with a
          // body without `ok`, and taking that for success would mean saying "password changed" when
          // nothing happened.
          if (out.ok !== true) {
            show(out.error || "Failed: this version of the app can't change the password yet. Update the app.");
            return;
          }
          pwDone = true;
          changed();
          refresh();
        } catch (e) {
          show("Can't reach the app");
        } finally {
          busy = false;
          b.disabled = false;
          b.textContent = was;
        }
      };
      bind("pw-go", go);
      fields.forEach((f) => { f.onkeydown = (e) => { if (e.key === "Enter") go(); }; });
    }

    function drawSignIn() {
      if (stage === "reset") return drawReset();
      if (stage === "sent") return drawResetSent();

      const up = stage === "up";
      // Each field on its own line, and the button across their full width right after. Before,
      // the password and "Sign in" stood on one line: in a panel narrower than a
      // settings section, the button either dropped to a second line or squeezed against a
      // flattened field. It is a form, not a table of values and labels.
      el.innerHTML = `
        <div class="ac-auth">
          <b class="ac-heading">${up ? "Registration" : "Login"}</b>
          <input class="ac-input" type="email" data-act="email" placeholder="Email"
                 autocomplete="email" value="${esc(email)}">
          <div class="ac-pass">
            <input class="ac-input" type="password" data-act="password" placeholder="Password"
                   autocomplete="${up ? "new-password" : "current-password"}">
            <button type="button" class="ac-eye" data-act="eye" aria-pressed="false"
                    aria-label="Show password">${EYE}</button>
          </div>
          ${up ? "" : `<button type="button" class="linklike ac-forgot" data-act="forgot"
                              >Forgot your password?</button>`}
          <div class="ac-error" data-act="error" hidden></div>
          <button class="cta ac-submit" data-act="go">${up ? "Create account" : "Sign in"}</button>
          ${up ? `<p class="ac-legal">${withDocs(
            "By creating an account, you accept the {terms} and the {privacy}.",
            "Terms of Use", "Privacy policy")}</p>` : ""}
          <p class="ac-swap">
            ${up ? "" : "Don't have an account?"}
            <button class="linklike" data-act="swap">${
              up ? "I already have an account" : "Sign up"}</button>
          </p>
        </div>`;

      const mail = pick("email");
      const pass = pick("password");
      const go = pick("go");
      const errBox = pick("error");

      // A refusal is shown in the form itself, not in the gauge strip. The account panel on the
      // dashboard slides out over the header, where the breakage strip also lives, and a server
      // refusal went under the panel unnoticed: "Sign in" just released the button, and
      // why was impossible to see without closing the form.
      //
      // Field highlighting is only for the check on this side (empty, does not look like an
      // email, short password). A server refusal carries no highlighting: the text of the
      // message ("no such account" / "wrong password",
      // `accounts.sign_in` on the server) already says which of the two is wrong, and
      // highlighting a field on top of that would be repeating.
      const showError = (message, field) => {
        errBox.textContent = message;
        errBox.hidden = false;
        mail.classList.toggle("bad", field === mail);
        pass.classList.toggle("bad", field === pass);
      };
      const clearError = () => {
        errBox.hidden = true;
        mail.classList.remove("bad");
        pass.classList.remove("bad");
      };
      for (const box of [mail, pass]) box.addEventListener("input", clearError);

      // The check here is not a duplicate of the server's but what can be said before the round
      // trip to the server and without a network: an empty field, clearly not an email, a short
      // password at registration. The rest ("email is taken", "do not match",
      // "account is closed") is decided only by the server, which has
      // the database for it.
      const MIN_PASSWORD = 8;   // as on the server, accounts.py: MIN_PASSWORD
      function invalid() {
        if (!mail.value.trim()) return ["Enter your email", mail];
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(mail.value.trim())) {
          return ["The address should look like name@domain", mail];
        }
        if (!pass.value) return ["Enter your password", pass];
        if (up && pass.value.length < MIN_PASSWORD) {
          return [`Password is shorter than ${MIN_PASSWORD} characters`, pass];
        }
        return null;
      }

      const enter = () => {
        const problem = invalid();
        if (problem) {
          showError(...problem);
          problem[1].focus();
          return;
        }
        clearError();
        email = mail.value.trim();
        act(go, up ? "/admin/account/register" : "/admin/account/sign-in", null,
            { email, password: pass.value }, (msg) => showError(msg));
      };
      go.onclick = enter;
      for (const box of [mail, pass]) {
        box.onkeydown = (e) => { if (e.key === "Enter") enter(); };
      }
      bind("swap", () => { stage = up ? "in" : "up"; email = mail.value.trim(); refresh(); });
      if (!up) {
        bind("forgot", () => { stage = "reset"; email = mail.value.trim(); refresh(); });
      }

      // A password is typed blind, and a typo in it would otherwise not be seen until the
      // server's refusal. A toggle, not "while held down": to compare a long password character
      // by character, you look at it for more than a moment.
      const eye = pick("eye");
      eye.addEventListener("click", () => {
        const on = pass.type === "password";
        pass.type = on ? "text" : "password";
        eye.innerHTML = on ? EYE_OFF : EYE;
        eye.setAttribute("aria-pressed", on ? "true" : "false");
        eye.setAttribute("aria-label", on ? "Hide password" : "Show password");
        pass.focus();
      });
    }

    // One field, the email, and one button. We do not ask for the password here: it is exactly
    // what was forgotten, so there is nothing to ask. The new one is set on the page behind
    // the link from the email. It opens in a browser, not in this window, because the app sits
    // on one machine and email is read anywhere.
    function drawReset() {
      el.innerHTML = `
        <div class="ac-auth">
          <b class="ac-heading">Reset password</b>
          <input class="ac-input" type="email" data-act="email" placeholder="Email"
                 autocomplete="email" value="${esc(email)}">
          <div class="ac-error" data-act="error" hidden></div>
          <button class="cta ac-submit" data-act="go">Send link</button>
          <p class="ac-swap">
            <button class="linklike" data-act="swap">Back to sign in</button>
          </p>
        </div>`;

      const mail = pick("email");
      const go = pick("go");
      const errBox = pick("error");
      const showError = (message) => { errBox.textContent = message; errBox.hidden = false; };
      const clearError = () => { errBox.hidden = true; };
      mail.addEventListener("input", clearError);

      const send = () => {
        const value = mail.value.trim();
        if (!value) { showError("Enter your email"); mail.focus(); return; }
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
          showError("The address should look like name@domain"); mail.focus(); return;
        }
        clearError();
        email = value;
        // The server answers the same whether or not an account exists; see
        // `argus.server.app.reset_request`. So there is no separate "no such email" refusal here:
        // onError would only ever get the network.
        act(go, "/admin/account/reset", () => { stage = "sent"; },
            { email }, (msg) => showError(msg));
      };
      go.onclick = send;
      mail.onkeydown = (e) => { if (e.key === "Enter") send(); };
      bind("swap", () => { stage = "in"; refresh(); });
    }

    function drawResetSent() {
      el.innerHTML = `
        <div class="ac-auth">
          <b class="ac-heading">Check your email</b>
          <div class="note">If we have an account with <b>${esc(email)}</b>, an email with a link
            is on its way. The link works for 30 minutes and can be used once.</div>
          <p class="ac-swap">
            <button class="linklike" data-act="swap">Back to sign in</button>
          </p>
        </div>`;
      bind("swap", () => { stage = "in"; refresh(); });
    }

    const pick = (name) => el.querySelector(`[data-act="${name}"]`);

    function bind(name, fn) {
      const b = pick(name);
      if (b) b.onclick = () => fn(b);
    }

    // One path for all buttons: block, go to the server, show a refusal as text, redraw.
    // Without blocking, a second press goes out as a second request, and the person gets
    // "email already taken" for their own double click on "Create"
    // (Create).
    async function act(button, url, then, payload, onError) {
      onError = onError || say;
      if (busy) return;
      busy = true;
      const was = button.textContent;
      button.disabled = true;
      button.textContent = "…";
      try {
        const out = await post(url, payload || {});
        // Only an explicit "ok": a reply without it is not success. A build without accounts
        // answers an unknown route with `{detail}`, and taking that for a sign-in would mean
        // staying silent instead of giving a reason.
        if (!out || out.ok !== true) {
          // No `refresh()` here: it redraws the form anew and would erase the refusal just shown
          // before anyone could look at it. The state has not changed, so there is nothing to
          // refresh after a refusal.
          onError(reason(out));
          return;
        }
        if (then) then(out);
        changed();
        refresh();
      } catch (e) {
        // The app answered not what is promised (a stub page instead of a reply, a cut-off) or did
        // not answer at all: without this, the refusal went nowhere and the button was just
        // released.
        onError("Can't reach the app");
      } finally {
        busy = false;
        button.disabled = false;
        button.textContent = was;
      }
    }

    refresh();
    return { refresh };
  }

  window.argusAccount = { mount, mountBilling, mountUsage, showDoc };
})();
