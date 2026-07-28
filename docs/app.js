/* DOMISTAV Tendry — dashboard. Vše běží v prohlížeči,
   watchlist a čas poslední návštěvy pouze v localStorage. */
"use strict";

const LS_WATCH = "dt.watchlist";
const LS_SEEN = "dt.lastVisit";
const LS_DISLIKE = "dt.dislikes";

const $ = (id) => document.getElementById(id);
const state = {
  tenders: [],
  changes: {},   // centrální archiv změn (docs/data/changes.json, git)
  watch: new Set(JSON.parse(localStorage.getItem(LS_WATCH) || "[]")),
  // 👎 odmítnuté: id → otisk zakázky {cpv, ico, toks, title} uložený
  // v okamžiku odmítnutí (přežije i zmizení zakázky z dat)
  dislikes: JSON.parse(localStorage.getItem(LS_DISLIKE) || "{}"),
  df: new Map(),      // dokumentová četnost tokenů v aktuálních datech
  model: null,        // agregát odmítnutí (počty CPV skupin / IČO / tokenů)
  lastVisit: localStorage.getItem(LS_SEEN) || "",
};
// úklid úložiště starší klientské detekce změn
localStorage.removeItem("dt.snapshots");
localStorage.removeItem("dt.changes");

const fmtKc = new Intl.NumberFormat("cs-CZ", { maximumFractionDigits: 0 });
const todayISO = new Date().toISOString().slice(0, 10);

function daysTo(deadline) {
  if (!deadline) return null;
  const d = new Date(deadline.slice(0, 10) + "T23:59:59");
  return Math.ceil((d - new Date()) / 86400000);
}

function saveWatch() {
  localStorage.setItem(LS_WATCH, JSON.stringify([...state.watch]));
}

/* ── učení relevance z odmítnutých (vše jen v prohlížeči) ────────────────
   Odmítnutí uloží otisk zakázky; z otisků se počítají četnosti CPV skupin,
   zadavatelů a výrazných slov názvu. Opakují-li se, podobné zakázky se
   skrývají (sledované ★ nikdy). Filtr „jen odmítnuté 👎" vše zpřístupní. */

function saveDislikes() {
  localStorage.setItem(LS_DISLIKE, JSON.stringify(state.dislikes));
}

const STOP = new Set(("oprava opravy oprav rekonstrukce stavebni prace praci " +
  "stavba stavby vystavba modernizace udrzba dodavka dodavky dodani sluzby " +
  "zajisteni provedeni provadeni budovy budova objektu objekt mesto mesta " +
  "obec obce kraj kraje etapa cast casti projekt zhotovitel verejna zakazka " +
  "zakazky ulice namesti areal system zarizeni vymena snizeni zvyseni nova " +
  "novy nove pro nad pod").split(" "));

function norm(s) {
  return String(s || "").normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

// výrazná slova názvu: dost dlouhá, mimo stopslova a mimo slova
// běžná napříč aktuálními zakázkami (df filtr vyřadí obecné výrazy)
function titleTokens(title) {
  const limit = Math.max(4, Math.round(state.tenders.length * 0.01));
  return [...new Set(norm(title).split(" "))].filter((w) =>
    w.length >= 4 && !STOP.has(w) &&
    (state.df.get(w) || 0) <= limit);
}

function fingerprint(t) {
  return {
    cpv: [...new Set((t.cpv || []).map((c) => String(c).slice(0, 4)))],
    ico: t.authority_ico || "",
    toks: titleTokens(t.title),
    title: t.title,
  };
}

function buildDf() {
  state.df = new Map();
  for (const t of state.tenders) {
    for (const w of new Set(norm(t.title).split(" "))) {
      if (w.length >= 4) state.df.set(w, (state.df.get(w) || 0) + 1);
    }
  }
}

function buildModel() {
  const m = { cpv: new Map(), ico: new Map(), tok: new Map() };
  const inc = (map, k) => k && map.set(k, (map.get(k) || 0) + 1);
  for (const fp of Object.values(state.dislikes)) {
    (fp.cpv || []).forEach((g) => inc(m.cpv, g));
    inc(m.ico, fp.ico);
    (fp.toks || []).forEach((w) => inc(m.tok, w));
  }
  state.model = m;
}

// Důvod skrytí: přímé odmítnutí, nebo odhad podle opakovaných odmítnutí.
// Prahy jsou záměrně konzervativní; sledované ★ se odhadem nikdy neskryjí.
function irrelevance(t) {
  if (state.dislikes[t.id]) return "odmítnuto 👎";
  if (state.watch.has(t.id) || !state.model) return null;
  const m = state.model;
  // CPV 45* = stavební práce, jádro oboru — takové zakázky se odhadem
  // podle CPV/zadavatele NIKDY neskrývají (jen přímo, či shodou názvu)
  const has45 = (t.cpv || []).some((c) => String(c).startsWith("45"));
  if (!has45) {
    for (const g of new Set((t.cpv || []).map((c) => String(c).slice(0, 4)))) {
      if (!g.startsWith("45") && (m.cpv.get(g) || 0) >= 2) {
        return `odhad: CPV ${g}* odmítnuto ${m.cpv.get(g)}×`;
      }
    }
    const icoN = m.ico.get(t.authority_ico || "") || 0;
    if (icoN >= 3) return `odhad: zadavatel odmítnut ${icoN}×`;
  }
  const hits = titleTokens(t.title).filter((w) => (m.tok.get(w) || 0) >= 2);
  if (hits.length >= 2) return `odhad: název podobný odmítnutým (${hits.join(", ")})`;
  return null;
}

async function load() {
  try {
    const [tenders, meta, changes] = await Promise.all([
      fetch("data/tenders.json").then((r) => r.json()),
      fetch("data/meta.json").then((r) => r.json()),
      fetch("data/changes.json").then((r) => r.json()).catch(() => ({})),
    ]);
    state.tenders = tenders;
    state.changes = changes || {};
    buildDf();
    buildModel();
    renderMeta(meta);
  } catch (e) {
    $("meta-line").textContent = "Data se nepodařilo načíst.";
    showError(["Chyba načtení dat: " + e.message]);
    return;
  }
  render();
  localStorage.setItem(LS_SEEN, todayISO); // až po vykreslení badge „nové"
}

function renderMeta(meta) {
  $("meta-line").textContent = meta.updated
    ? "Aktualizováno " +
      new Date(meta.updated).toLocaleString("cs-CZ", {
        day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit",
      }) +
      " · zdroje: ISVZ · profily · NEN"
    : "Data zatím nebyla vygenerována (čeká se na první běh).";
  if (meta.errors && meta.errors.length) showError(meta.errors);
}

function showError(errors) {
  const b = $("error-banner");
  b.hidden = false;
  b.textContent = "Upozornění zdrojů dat: " + errors.join(" · ");
}

function isNew(t) {
  return state.lastVisit && t.published && t.published > state.lastVisit;
}

const CHANGED_DAYS = 7;
function changesOf(t) {
  return state.changes[t.id] || [];
}
function changedRecently(t) {
  const ch = changesOf(t);
  if (!ch.length) return false;
  const limit = new Date(Date.now() - CHANGED_DAYS * 86400000)
    .toISOString().slice(0, 10);
  return ch.some((c) => c.date >= limit);
}

function fmtDate(s) {
  if (!s) return "";
  const [d, t] = [s.slice(0, 10), s.slice(11, 16)];
  return d.split("-").reverse().join(".") + (t ? " " + t : "");
}

function passes(t) {
  const q = $("f-q").value.trim().toLowerCase();
  if (q && !(t.title + " " + t.authority).toLowerCase().includes(q)) return false;

  const kind = $("f-kind").value;
  if (kind && t.kind !== kind) return false;

  const region = $("f-region").value;
  if (region === "unknown") { if (!t.loc_unknown) return false; }
  else if (region && !(t.dist_km != null && t.dist_km <= +region)) return false;

  const fv = $("f-value").value;
  if (fv === "novalue") { if (!t.no_value) return false; }
  else if (fv && !(t.value != null && t.value >= +fv)) return false;

  if ($("f-active").checked && t.expired) return false;
  if ($("f-watch").checked && !state.watch.has(t.id)) return false;
  if ($("f-changed").checked && !changedRecently(t)) return false;

  // relevance: běžný pohled skrývá odmítnuté i odhadem nerelevantní;
  // filtr „jen odmítnuté 👎" zobrazí právě je (kontrola, že nic neuteklo)
  const irr = irrelevance(t);
  if ($("f-disliked").checked) return !!irr;
  return !irr;
}

function rowHTML(t) {
  const dn = daysTo(t.deadline);
  let due;
  if (t.expired || (dn != null && dn < 0)) {
    due = `<span class="due gone">–<small>po lhůtě</small></span>`;
  } else if (dn == null) {
    due = `<span class="due">?<small>lhůta neuvedena</small></span>`;
  } else {
    const cls = dn <= 7 ? "due soon" : "due";
    due = `<span class="${cls}">${dn} d<small>do ${t.deadline.slice(0, 10)
      .split("-").reverse().join(".")}</small></span>`;
  }
  const val = t.no_value || t.value == null
    ? `<span class="val">—<small>hodnota neuvedena</small></span>`
    : `<span class="val">${fmtKc.format(t.value)}<small>Kč bez DPH</small></span>`;
  const star = state.watch.has(t.id) ? "star on" : "star";
  const nuts = t.dist_km != null
    ? `<span class="tag dist">~${t.dist_km} km</span>` : "";
  const noNuts = t.loc_unknown
    ? `<span class="tag" title="Místo plnění se nepodařilo určit — zakázka ponechána">poloha neurčena</span>` : "";
  const title = t.url
    ? `<a href="${t.url}" target="_blank" rel="noopener">${esc(t.title)}</a>`
    : esc(t.title);

  const changed = changedRecently(t);
  const badges =
    (isNew(t) ? '<span class="badge-new">nové</span>' : "") +
    (changed ? '<span class="badge-chg">změna ZD</span>' : "");

  const visit = t.site_visit
    ? `<span class="tag visit">Prohlídka ${esc(fmtDate(t.site_visit))}</span>` : "";
  const clar = t.clarifications
    ? `<span class="tag clar">Vysvětlení ZD: ${t.clarifications}×</span>` : "";

  const disliked = !!state.dislikes[t.id];
  const irrBadge = $("f-disliked").checked
    ? `<span class="tag irr">${esc(irrelevance(t) || "")}</span>` : "";

  const chs = changesOf(t);
  const history = chs.length
    ? `<details class="hist"${changed ? " open" : ""}>
        <summary>Historie změn (${chs.length})</summary>
        <ul>${chs.slice().reverse().map((c) =>
          `<li><b>${esc(fmtDate(c.date))}</b> ${esc(c.label)}: ` +
          `<s>${esc(c.old)}</s> → <b>${esc(c.new)}</b></li>`).join("")}
        </ul></details>`
    : "";

  return `<li class="row${t.expired ? " expired" : ""}" data-id="${esc(t.id)}">
    <div class="head">
      <h2>${title}${badges}</h2>
      <div class="auth">${esc(t.authority)}</div>
      <div class="tags">
        <span class="tag ${t.kind === "VZ" ? "vz" : "vzmr"}">${t.kind}</span>
        ${nuts}${noNuts}
        ${t.kw_match ? '<span class="tag kw" title="Zachyceno podle názvu, CPV neodpovídá stavebním pracím">dle názvu</span>' : ""}
        ${(t.cpv || []).slice(0, 2).map((c) => `<span class="tag">CPV ${esc(c)}</span>`).join("")}
        ${visit}${clar}${irrBadge}
      </div>
      ${history}
    </div>
    ${val}
    ${due}
    <span class="acts">
      <button class="${star}" title="Sledovat" aria-pressed="${state.watch.has(t.id)}">★</button>
      <button class="thumb${disliked ? " on" : ""}" title="Označit jako nerelevantní — podobné zakázky se přestanou zobrazovat" aria-pressed="${disliked}">👎</button>
    </span>
  </li>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render() {
  const shown = state.tenders.filter(passes);
  $("list").innerHTML = shown.map(rowHTML).join("");
  $("empty").hidden = shown.length > 0;

  const active = state.tenders.filter((t) => !t.expired);
  $("st-active").textContent = active.length;
  $("st-new").textContent = active.filter(isNew).length;
  $("st-watch").textContent =
    state.tenders.filter((t) => state.watch.has(t.id)).length;
  $("st-week").textContent = active.filter((t) => {
    const d = daysTo(t.deadline);
    return d != null && d >= 0 && d <= 7;
  }).length;
  $("st-changed").textContent = state.tenders.filter(changedRecently).length;
  $("st-hidden").textContent =
    state.tenders.filter((t) => irrelevance(t)).length;
}

document.addEventListener("click", (e) => {
  const star = e.target.closest(".star");
  const thumb = e.target.closest(".thumb");
  if (!star && !thumb) return;
  const row = (star || thumb).closest(".row");
  const id = row.dataset.id;
  if (star) {
    state.watch.has(id) ? state.watch.delete(id) : state.watch.add(id);
    saveWatch();
  } else {
    if (state.dislikes[id]) {
      delete state.dislikes[id];
    } else {
      const t = state.tenders.find((x) => x.id === id);
      if (t) state.dislikes[id] = fingerprint(t);
    }
    saveDislikes();
    buildModel();
  }
  render();
});

["f-q", "f-kind", "f-region", "f-value", "f-active", "f-watch", "f-changed",
 "f-disliked"].forEach((id) => $(id).addEventListener("input", render));

load();
