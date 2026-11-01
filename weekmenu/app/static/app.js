/* Weekmenu app: plain JS, no build step.
 * Every fetch uses a relative path (api/week, not /api/week): Home Assistant ingress
 * puts /api/hassio_ingress/<token>/ in front of the page.
 */
"use strict";

const app = document.getElementById("app");
const nav = document.getElementById("nav");
const toastEl = document.getElementById("toast");
const sheetEl = document.getElementById("sheet");

const DAGEN_KORT = { maandag: "ma", dinsdag: "di", woensdag: "wo", donderdag: "do",
  vrijdag: "vr", zaterdag: "za", zondag: "zo" };
const WEEK_ORDER = ["woensdag", "donderdag", "vrijdag", "zaterdag", "zondag", "maandag", "dinsdag"];
const MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
  "september", "oktober", "november", "december"];
const GEEN_OUDERAVOND = "Niet deze week";
const BUSY_TEXT = "Er loopt al een ronde";

const state = { status: null, week: null, weekParam: null, savings: null, settings: null,
  dishes: {}, open: {}, polling: false };

// --- helpers ---------------------------------------------------------------------------

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function eur(x) {
  const n = Number(x) || 0;
  return "€ " + n.toFixed(2).replace(".", ",");
}

/* Only https links from the API reach the page; anything else is dropped. */
function safeUrl(u) {
  return typeof u === "string" && /^https:\/\/[^\s"'<>]+$/i.test(u) ? u : "";
}

function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""; }

function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function todayIso() {
  const t = new Date();
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
}

function shortDate(dt) {
  const dag = ["zo", "ma", "di", "wo", "do", "vr", "za"][dt.getDay()];
  return `${dag} ${dt.getDate()} ${MAANDEN[dt.getMonth()].slice(0, 3)}`;
}

function isoWeek(iso) {
  const d = parseDate(iso);
  d.setDate(d.getDate() + 3 - ((d.getDay() + 6) % 7));
  const jan4 = new Date(d.getFullYear(), 0, 4);
  return 1 + Math.round(((d - jan4) / 864e5 - 3 + ((jan4.getDay() + 6) % 7)) / 7);
}

function minutes(m) {
  if (!m) return "";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  if (r === 0) return `${h} u`;
  if (r === 30) return `${h}½ u`;
  return `${h} u ${r} min`;
}

function cuisineLabel(c) {
  const map = { "pasta en Italiaans": "Italiaans", "Mexicaans en wraps": "Mexicaans",
    "Hollandse pot": "Hollands" };
  return map[c] || cap(c || "");
}

function cuisineClass(c) {
  const s = (c || "").toLowerCase();
  if (s.includes("aziat")) return "b2";
  if (s.includes("ital") || s.includes("pasta")) return "b4";
  if (s.includes("mexic") || s.includes("wrap")) return "b1";
  if (s.includes("holland")) return "b3";
  return "b5";
}

function cookMeta(r) {
  let t = minutes(r.cook) || "?";
  if (r.oven) t += ` + ${minutes(r.oven)} oven`;
  if (r.wait) t += ` + ${minutes(r.wait)} wachten`;
  return t;
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

const ICON = {
  plate: '<svg class="fallback" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="6.5"/><path d="M3 3.5v5a2 2 0 0 0 2 2v10M5 3.5v4M7 3.5v5a2 2 0 0 1-2 2M21 3.5c-1.7.6-2.5 2.6-2.5 5.5v3.5H21v8"/></svg>',
  home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5h4v5"/></svg>',
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>',
  people: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3.2"/><path d="M3 19.5c.6-3.3 3-5 6-5s5.4 1.7 6 5"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.6c2.6-.2 4.5 1.4 5 4.4"/></svg>',
  book: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5C6.5 4 9.5 4 12 5.5v14C9.5 18 6.5 18 4 19.5z"/><path d="M20 5.5C17.5 4 14.5 4 12 5.5v14c2.5-1.5 5.5-1.5 8 0z"/></svg>',
  swap: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9h13l-3.5-3.5M20 15H7l3.5 3.5"/></svg>',
  undo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>',
};

function photo(recipe, cls) {
  const src = safeUrl(recipe && recipe.image);
  const img = src ? `<img src="${esc(src)}" alt="" loading="lazy" onerror="this.remove()">` : "";
  return `<div class="${cls} ${cuisineClass(recipe && recipe.cuisine)}">${ICON.plate}${img}`;
}

// --- api -------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status, body) { super(message); this.status = status; this.body = body; }
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("Geen verbinding met de add-on", 0);
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const msg = (data && data.error) || `Fout ${r.status}`;
    if (r.status === 401 && data && data.login) {
      state.status = Object.assign({}, state.status, { logged_in: false });
      render();
    }
    throw new ApiError(msg, r.status, data);
  }
  return data;
}

function errorText(e) {
  if (e && e.status === 409 && String(e.message).startsWith(BUSY_TEXT)) {
    return "Even geduld, er loopt al een ronde";
  }
  return (e && e.message) || "Er ging iets mis";
}

let toastTimer = null;
function toast(msg, bad) {
  toastEl.textContent = msg;
  toastEl.className = "toast" + (bad ? " bad" : "");
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, bad ? 5000 : 3200);
}

/* Run `fn` with `btn` showing a spinner and disabled until the answer is there. */
async function busy(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("busy");
  try {
    return await fn();
  } catch (e) {
    toast(errorText(e), true);
  } finally {
    btn.classList.remove("busy");
    btn.disabled = false;
  }
}

function confirmSheet(text, sub, okLabel) {
  return new Promise((resolve) => {
    sheetEl.innerHTML = `<div class="dialog" role="dialog" aria-modal="true">
      <p>${esc(text)}</p>${sub ? `<p class="muted">${esc(sub)}</p>` : ""}
      <div class="btnrow"><button class="btn sec2" data-a="no">Annuleren</button>
      <button class="btn pri" data-a="yes">${esc(okLabel)}</button></div></div>`;
    sheetEl.hidden = false;
    const done = (v) => { sheetEl.hidden = true; sheetEl.innerHTML = ""; resolve(v); };
    sheetEl.onclick = (ev) => {
      const a = ev.target.closest("[data-a]");
      if (a) done(a.dataset.a === "yes");
      else if (ev.target === sheetEl) done(false);
    };
  });
}

// --- loading ---------------------------------------------------------------------------

async function loadWeek() {
  const q = state.weekParam ? `api/week?week=${encodeURIComponent(state.weekParam)}` : "api/week";
  state.week = await api(q);
}

async function loadDish(day) {
  state.dishes[day] = await api(`api/dish/${encodeURIComponent(day)}`);
}

async function loadStatus() {
  state.status = await api("api/status");
}

/* While a round runs: look again every few seconds, then refresh the week. */
function pollRound() {
  if (state.polling) return;
  state.polling = true;
  const tick = async () => {
    try {
      await loadStatus();
    } catch (e) { /* keep polling */ }
    if (state.status && state.status.running) {
      setTimeout(tick, 3000);
      return;
    }
    state.polling = false;
    state.dishes = {};
    try { await loadWeek(); } catch (e) { /* shown on the tab */ }
    if (state.status && state.status.error) toast(state.status.error, true);
    else toast("Het menu is bijgewerkt");
    render();
  };
  setTimeout(tick, 2500);
}

// --- screens: week ---------------------------------------------------------------------

function notesHtml(w, soft) {
  const out = [];
  const note = (cls, title, sub, items) => out.push(
    `<div class="card note ${cls}"><span class="dot"></span><div><b>${esc(title)}</b>` +
    (sub ? `<span class="muted">${esc(sub)}</span>` : "") +
    (items && items.length ? `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "") +
    `</div></div>`);
  if (soft) return softNotes(w, note, out);
  if (w.period_warning) note("", w.period_warning, "", []);
  if (w.short_by > 0) {
    note("", `${plural(w.short_by, "avond", "avonden")} zonder menu`,
      "Er waren te weinig recepten die binnen de regels passen.", []);
  }
  if (w.pending && w.pending.length) {
    note("bad", "Onzeker of gelukt", "Controleer in de AH-app.", w.pending);
  }
  if (w.unknown && w.unknown.length) {
    note("bad", "Status onbekend", "Controleer in de AH-app.", w.unknown);
  }
  return out.join("");
}

function softNotes(w, note, out) {
  if (w.leftovers && w.leftovers.length) {
    note("soft", "Over van het vorige gerecht", "Haal weg in de AH-app als je wilt.",
      w.leftovers.map((l) => `${l.qty}× ${l.product}`));
  }
  if (w.budget_exhausted) {
    note("soft", "Snel gekozen", "Het zoeken duurde lang; dit menu klopt, maar is misschien niet het allerbeste.", []);
  }
  if (w.not_found && w.not_found.length) {
    note("soft", "Niet gevonden bij AH", "Koop dit zelf als je het nodig hebt.", w.not_found);
  }
  return out.length ? `<div class="sec">Goed om te weten</div>${out.join("")}` : "";
}

function dayCard(d) {
  const dt = parseDate(d.date);
  const weekend = dt.getDay() === 6 || dt.getDay() === 0;
  const tonight = d.date === todayIso();
  if (d.ouders) {
    return `<div class="card skip"><div class="ic">${ICON.home}</div>
      <div><b>${esc(cap(d.dag))}</b> · bij de ouders</div></div>`;
  }
  const r = d.recipe;
  const label = esc(cap(d.dag)) + (tonight ? ' · <span class="now">vanavond</span>'
    : weekend ? " · weekend" : "");
  return `<button class="card dish" data-dish="${esc(d.date)}">
    ${photo(r, "ph")}</div>
    <div class="grow"><div class="day">${label}</div>
    <div class="title">${esc(r.title)}</div>
    <div class="meta">⏱ ${esc(cookMeta(r))} · ${esc(cuisineLabel(r.cuisine))}</div></div>
    <div class="eur">${eur(d.savings)}</div></button>`;
}

function weekScreen() {
  const w = state.week;
  const running = state.status && state.status.running;
  const start = parseDate(w.week);
  const end = new Date(start); end.setDate(end.getDate() + 6);
  const where = w.target === "order" ? "in bestelling" : w.target === "list" ? "op de lijst" : "";
  let html = `<header class="top"><div><h1>Weekmenu</h1>
    <div class="sub">${shortDate(start)} – ${shortDate(end)}${w.target === "order" ? " · bezorging woensdag" : ""}</div></div>
    ${where ? `<span class="pill grey">${where}</span>` : ""}</header>`;

  if (running) {
    html += `<div class="card note"><span class="spin dark"></span><div><b>Er wordt een menu gemaakt</b>
      <span class="muted">Dit duurt meestal een minuut.</span></div></div>`;
  }
  if (!w.days.length) {
    html += `<div class="card empty"><b>Nog geen menu voor deze week</b>
      Het menu wordt zondagochtend vanzelf gemaakt. Wil je niet wachten, tik dan op Nu maken.</div>
      <button class="btn pri full" data-run="nu_maken">Nu maken</button>`;
  } else {
    html += `<div class="save"><div><div class="lbl">Bonus bespaard deze week <em>· geschat</em></div>
      <div class="big">${eur(w.savings)}</div></div>
      <div class="right">${plural(w.products, "product", "producten")}<br>${w.bonus_products} in de bonus</div></div>`;
    html += notesHtml(w, false);
    html += w.days.map(dayCard).join("");
    html += notesHtml(w, true);
  }
  if (w.next_week) {
    html += `<div class="linkrow"><button class="pill grey" data-week="${esc(w.next_week)}">Volgende week bekijken →</button></div>`;
  } else if (state.weekParam) {
    html += `<div class="linkrow"><button class="pill grey" data-week="">← Terug naar deze week</button></div>`;
  }
  return html;
}

// --- screens: one dish -----------------------------------------------------------------

function ingredientRows(d) {
  const rows = d.lines.map((l) => {
    if (l.pantry) {
      return `<div class="row home"><span class="q">–</span><span class="nm">${esc(l.product)} · in huis</span></div>`;
    }
    const sub = l.ingredient && l.ingredient !== l.product ? `<small>${esc(l.ingredient)}</small>` : "";
    const pill = l.bonus && l.mechanism ? `<span class="pill green b">${esc(l.mechanism)}</span>` : "";
    return `<div class="row"><span class="q">${l.qty}×</span><span class="nm">${esc(l.product)}${sub}</span>${pill}</div>`;
  });
  for (const text of d.not_found || []) {
    rows.push(`<div class="row missing"><span class="q">?</span><span class="nm">${esc(text)}</span><span class="pill warn b">niet gevonden</span></div>`);
  }
  return rows.join("");
}

function dishScreen(day) {
  const d = state.dishes[day];
  const r = d.recipe;
  const dt = parseDate(d.date);
  const tags = [`<span class="pill grey">⏱ ${esc(minutes(r.cook) || "?")}</span>`];
  if (r.oven) tags.push(`<span class="pill grey">+ ${esc(minutes(r.oven))} oven</span>`);
  if (r.wait) tags.push(`<span class="pill grey">+ ${esc(minutes(r.wait))} wachten</span>`);
  if (r.cuisine) tags.push(`<span class="pill grey">${esc(cuisineLabel(r.cuisine))}</span>`);
  tags.push(`<span class="pill green">${eur(d.savings)} bespaard</span>`);
  const open = !!state.open[day];
  const steps = d.steps && d.steps.length
    ? `<ol>${d.steps.map((s) => `<li><span>${esc(s)}</span></li>`).join("")}</ol>`
    : `<p class="empty">Geen bereiding gevonden. Bekijk het recept op Allerhande.</p>`;
  const left = d.leftovers && d.leftovers.length
    ? `<div class="card note soft"><span class="dot"></span><div><b>Over van het vorige gerecht</b>
       <span class="muted">Haal weg in de AH-app als je wilt.</span>
       <ul>${d.leftovers.map((l) => `<li>${esc(l.qty)}× ${esc(l.product)}</li>`).join("")}</ul></div></div>` : "";
  return `${photo(r, "hero")}<button class="back" data-back aria-label="Terug naar de week">${ICON.back}</button></div>
    <div class="sheet">
    <div class="day" style="margin:0 20px">${esc(cap(d.dag))} ${dt.getDate()} ${MAANDEN[dt.getMonth()]}</div>
    <h2>${esc(r.title)}</h2>
    <div class="tags">${tags.join("")}</div>
    <div class="card pers">${ICON.people} Personen <div class="stp">
      <button data-persons="${d.persons - 1}" aria-label="Minder personen" ${d.persons <= 1 ? "disabled" : ""}>−</button>
      <b>${d.persons}</b>
      <button data-persons="${d.persons + 1}" aria-label="Meer personen" ${d.persons >= 8 ? "disabled" : ""}>+</button></div></div>
    ${left}
    <div class="sec">Ingrediënten</div>
    <div class="card ing">${ingredientRows(d)}</div>
    <div class="btnrow">
      <button class="btn sec2" data-steps aria-expanded="${open}">${ICON.book} Bereiding</button>
      <button class="btn pri" data-swap>${ICON.swap} Ander gerecht</button></div>
    ${open ? `<div class="sec">Bereiding</div><div class="card steps">${steps}</div>` : ""}
    ${d.can_back ? `<button class="btn sec2 full" data-undo>${ICON.undo} Terug naar het vorige gerecht</button>` : ""}
    ${safeUrl(r.url) ? `<a class="src" href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">Recept op Allerhande</a>` : ""}
    </div>`;
}

// --- screens: savings ------------------------------------------------------------------

function savingsScreen() {
  const s = state.savings;
  const month = cap(MAANDEN[new Date().getMonth()]);
  let html = `<header class="top"><div><h1>Besparing</h1>
    <div class="sub">geschat, tegen de normale AH-prijs</div></div></header>
    <div class="tot"><div class="card"><small>${month}</small><b>${eur(s.month)}</b></div>
    <div class="card"><small>Sinds start</small><b>${eur(s.total)}</b></div></div>`;
  const weeks = s.weeks.slice(-6);
  if (!weeks.length) {
    return html + `<div class="card empty"><b>Nog geen weken</b>Na het eerste menu staat hier per week wat de bonus scheelde.</div>`;
  }
  const max = Math.max(...weeks.map((w) => w.savings), 0.01);
  const last = weeks.length - 1;
  html += `<div class="card chart"><div class="bars">${weeks.map((w, i) =>
    `<div class="bar${i === last ? " now" : ""}${w.counted ? "" : " open"}" style="height:${Math.max(3, Math.round(w.savings / max * 100))}%"><span>${eur(w.savings)}</span></div>`).join("")}
    </div><div class="xl">${weeks.map((w) => `<div>wk ${isoWeek(w.week)}</div>`).join("")}</div></div>`;
  if (weeks.some((w) => !w.counted)) {
    html += `<p class="foot">Gestreept: die week is nog niet begonnen en telt nog niet mee.</p>`;
  }
  html += `<p class="foot">Wat het menu toevoegde, tegen de normale AH-prijs. Wat je daarna in de AH-app weghaalt of bijzet, ziet de add-on niet.</p>`;
  return html;
}

// --- screens: settings -----------------------------------------------------------------

function settingsScreen() {
  const s = state.settings;
  const opts = s.ouders_options || [GEEN_OUDERAVOND].concat(WEEK_ORDER);
  const chips = opts.map((o) => `<button class="chip${o === s.ouders ? " x" : ""}${o === GEEN_OUDERAVOND ? " wide" : ""}" data-ouders="${esc(o)}">${o === GEEN_OUDERAVOND ? "Niet" : esc(DAGEN_KORT[o] || o)}</button>`).join("");
  const boxes = WEEK_ORDER.map((dag) => {
    const ouders = dag === s.ouders;
    const n = s.persons[dag];
    return `<button class="box${ouders ? " x" : ""}" data-pers="${dag}" ${ouders ? "disabled" : ""} aria-label="Personen op ${dag}"><b>${ouders ? "–" : esc(n)}</b>${DAGEN_KORT[dag]}</button>`;
  }).join("");
  const note = s.defaulted && s.defaulted.length
    ? `<div class="card note"><span class="dot"></span><div><b>Niet alle helpers gevonden in Home Assistant</b><span class="muted">Waar een helper ontbreekt, staat de standaardwaarde.</span></div></div>` : "";
  return `<header class="top"><div><h1>Instellingen</h1>
    <div class="sub">dezelfde instellingen als in Home Assistant</div></div></header>
    ${note}
    <div class="card set">
      <div class="srow">Elke zondag een menu maken
        <button class="toggle ${s.enabled ? "on" : "off"}" data-toggle>${s.enabled ? "● aan" : "○ uit"}</button></div>
      <div class="srow">Kookavonden <div class="stp">
        <button data-avonden="${s.avonden - 1}" aria-label="Minder kookavonden" ${s.avonden <= 1 ? "disabled" : ""}>−</button>
        <b>${esc(s.avonden)}</b>
        <button data-avonden="${s.avonden + 1}" aria-label="Meer kookavonden" ${s.avonden >= 7 ? "disabled" : ""}>+</button></div></div>
      <div class="lbl2">Bij de ouders</div>
      <div class="tw">${chips}</div>
      <div class="lbl2">Personen per avond <span class="hint">· tik om te wijzigen</span></div>
      <div class="days">${boxes}</div>
    </div>
    <div class="btnrow"><button class="btn sec2" data-run="nieuw_menu">${ICON.refresh} Nieuw menu</button>
      <button class="btn pri" data-run="nu_maken">Nu maken</button></div>
    <p class="foot">Nu maken vult aan wat nog ontbreekt. Nieuw menu kiest andere gerechten.</p>`;
}

// --- screens: login --------------------------------------------------------------------

function loginScreen() {
  const url = safeUrl(state.status && state.status.login_url);
  return `<header class="top"><div><h1>Weekmenu</h1><div class="sub">inloggen bij Albert Heijn</div></div></header>
    <div class="card login">
      <h2>Log één keer in bij AH</h2>
      <p>De add-on is niet (meer) ingelogd. Inloggen kan alleen met de hand, omdat AH een captcha gebruikt. Daarna houdt de add-on de login zelf bij.</p>
      <ol>
        <li><span>Open op een computer de AH-inlogpagina in Chrome of Edge.</span></li>
        <li><span>Druk op F12 en kies het tabblad Console.</span></li>
        <li><span>Log in. In de console verschijnt een regel met <code>appie://login-exit?code=…</code></span></li>
        <li><span>Kopieer die hele regel en plak hem hieronder. De code is maar kort geldig.</span></li>
      </ol>
      ${url ? `<a class="btn sec2 full" style="margin:0 0 12px;width:100%" href="${esc(url)}" target="_blank" rel="noopener">AH-inlogpagina openen</a>` : ""}
      <textarea id="code" placeholder="appie://login-exit?code=…" autocomplete="off" spellcheck="false"></textarea>
      <div class="btnrow"><button class="btn pri" data-login>Inloggen</button></div>
    </div>`;
}

function errorScreen(msg) {
  return `<header class="top"><div><h1>Weekmenu</h1></div></header>
    <div class="card empty"><b>Dit lukte even niet</b>${esc(msg)}</div>
    <button class="btn pri full" data-retry>Opnieuw proberen</button>`;
}

// --- routing and rendering -------------------------------------------------------------

function route() {
  const h = location.hash.replace(/^#/, "");
  if (h.startsWith("gerecht/")) return { tab: "week", dish: h.slice(8) };
  if (h === "besparing" || h === "instellingen") return { tab: h };
  return { tab: "week" };
}

let renderSeq = 0;

async function render() {
  const seq = ++renderSeq;
  const r = route();
  const loggedIn = state.status && state.status.logged_in;
  nav.hidden = !loggedIn || !!r.dish;
  app.classList.toggle("bare", !loggedIn || !!r.dish);
  for (const a of nav.querySelectorAll("a")) a.classList.toggle("on", a.dataset.tab === r.tab);
  if (!state.status) return;
  if (!loggedIn) { app.innerHTML = loginScreen(); return; }

  const need = r.dish ? (state.dishes[r.dish] ? null : () => loadDish(r.dish))
    : r.tab === "besparing" ? (state.savings ? null : () => api("api/savings").then((x) => { state.savings = x; }))
    : r.tab === "instellingen" ? (state.settings ? null : () => api("api/settings").then((x) => { state.settings = x; }))
    : (state.week ? null : loadWeek);
  if (need) {
    app.innerHTML = `<div class="loading"><span class="spin dark"></span></div>`;
    try {
      await need();
    } catch (e) {
      if (seq !== renderSeq) return;
      if (state.status && !state.status.logged_in) { app.innerHTML = loginScreen(); return; }
      app.innerHTML = errorScreen(errorText(e));
      toast(errorText(e), true);
      return;
    }
    if (seq !== renderSeq) return;
  }
  app.innerHTML = r.dish ? dishScreen(r.dish)
    : r.tab === "besparing" ? savingsScreen()
    : r.tab === "instellingen" ? settingsScreen()
    : weekScreen();
}

/* Re-read in the background what a tab shows, without a loading screen. */
async function refreshQuietly(what) {
  try {
    if (what === "week") await loadWeek();
    if (what === "besparing") state.savings = await api("api/savings");
    if (what === "instellingen") state.settings = await api("api/settings");
    if (route().tab === what && !route().dish) render();
  } catch (e) { /* the cached view stays */ }
}

// --- actions ---------------------------------------------------------------------------

async function afterDishWrite(day, data) {
  state.dishes[day] = data;
  state.savings = null;
  render();
  try { await loadWeek(); } catch (e) { /* week refreshes on the next visit */ }
}

async function setSetting(entity, value) {
  await api("api/settings", { entity_id: entity, value });
}

async function startRound(btn, mode) {
  if (mode === "nieuw_menu") {
    const ok = await confirmSheet("Een nieuw menu kiezen? Wat al in je bestelling staat blijft staan.",
      "", "Nieuw menu");
    if (!ok) return;
  }
  await busy(btn, async () => {
    await api("api/run", { mode });
    toast(mode === "nieuw_menu" ? "Er wordt een nieuw menu gekozen…" : "Het menu wordt gemaakt…");
    state.status = Object.assign({}, state.status, { running: true });
    pollRound();
  });
}

app.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, [data-dish]");
  if (!t || t.disabled) return;
  const r = route();

  if (t.dataset.dish) { location.hash = "#gerecht/" + t.dataset.dish; return; }
  if ("back" in t.dataset) { location.hash = "#week"; return; }
  if ("retry" in t.dataset) { state.week = state.savings = state.settings = null; state.dishes = {}; boot(); return; }
  if ("week" in t.dataset) {
    state.weekParam = t.dataset.week || null;
    state.week = null;
    render();
    return;
  }
  if (t.dataset.run) { startRound(t, t.dataset.run); return; }

  if ("login" in t.dataset) {
    const field = document.getElementById("code");
    await busy(t, async () => {
      await api("api/login", { code: field.value });
      field.value = "";
      toast("Ingelogd bij AH");
      await loadStatus();
      render();
    });
    return;
  }

  // one dish
  if (r.dish) {
    const day = r.dish;
    if ("steps" in t.dataset) { state.open[day] = !state.open[day]; render(); return; }
    if (t.dataset.persons) {
      const n = Number(t.dataset.persons);
      const fewer = n < state.dishes[day].persons;
      await busy(t, async () => {
        const data = await api(`api/dish/${encodeURIComponent(day)}/persons`, { persons: n });
        await afterDishWrite(day, data);
        if (fewer) toast("Minder personen haalt niets weg uit je bestelling. Wat over is, staat bij het gerecht.");
      });
      return;
    }
    if ("swap" in t.dataset) {
      const where = state.week && state.week.target === "list" ? "op je lijst" : "in je bestelling";
      const ok = await confirmSheet("Een ander gerecht voor deze avond?",
        `De producten voor het nieuwe gerecht komen direct ${where}. Wat er al ligt, blijft staan.`,
        "Ander gerecht");
      if (!ok) return;
      await busy(t, async () => {
        const data = await api(`api/dish/${encodeURIComponent(day)}/swap`, {});
        await afterDishWrite(day, data);
        toast("Nieuw gerecht: " + data.recipe.title);
      });
      return;
    }
    if ("undo" in t.dataset) {
      await busy(t, async () => {
        const data = await api(`api/dish/${encodeURIComponent(day)}/back`, {});
        await afterDishWrite(day, data);
        toast("Het vorige gerecht staat er weer");
      });
      return;
    }
  }

  // settings
  const s = state.settings;
  if (!s) return;
  if ("toggle" in t.dataset) {
    await busy(t, async () => {
      await setSetting(s.entities.enabled, !s.enabled);
      s.enabled = !s.enabled;
    });
    render();
    return;
  }
  if (t.dataset.avonden) {
    const n = Number(t.dataset.avonden);
    await busy(t, async () => { await setSetting(s.entities.avonden, n); s.avonden = n; });
    render();
    return;
  }
  if (t.dataset.ouders) {
    const o = t.dataset.ouders;
    if (o === s.ouders) return;
    await busy(t, async () => { await setSetting(s.entities.ouders, o); s.ouders = o; });
    render();
    return;
  }
  if (t.dataset.pers) {
    const dag = t.dataset.pers;
    const n = (Number(s.persons[dag]) % 8) + 1;
    await busy(t, async () => { await setSetting(s.entities.persons[dag], n); s.persons[dag] = n; });
    render();
  }
});

window.addEventListener("hashchange", () => {
  const r = route();
  window.scrollTo(0, 0);
  render();
  if (!r.dish) refreshQuietly(r.tab);
});

async function boot() {
  try {
    await loadStatus();
  } catch (e) {
    app.innerHTML = errorScreen(errorText(e));
    toast(errorText(e), true);
    return;
  }
  await render();
  if (state.status.running) pollRound();
}

boot();
