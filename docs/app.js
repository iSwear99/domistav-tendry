/* DOMISTAV Tendry — dashboard. Vše běží v prohlížeči,
   watchlist a čas poslední návštěvy pouze v localStorage. */
"use strict";

const LS_WATCH = "dt.watchlist";
const LS_SEEN = "dt.lastVisit";

const $ = (id) => document.getElementById(id);
const state = {
  tenders: [],
  changes: {},   // centrální archiv změn (docs/data/changes.json, git)
  watch: new Set(JSON.parse(localStorage.getItem(LS_WATCH) || "[]")),
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

async function load() {
  try {
    const [tenders, meta, changes] = await Promise.all([
      fetch("data/tenders.json").then((r) => r.json()),
      fetch("data/meta.json").then((r) => r.json()),
      fetch("data/changes.json").then((r) => r.json()).catch(() => ({})),
    ]);
    state.tenders = tenders;
    state.changes = changes || {};
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
  return true;
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
        ${visit}${clar}
      </div>
      ${history}
    </div>
    ${val}
    ${due}
    <button class="${star}" title="Sledovat" aria-pressed="${state.watch.has(t.id)}">★</button>
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
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".star");
  if (!btn) return;
  const id = btn.closest(".row").dataset.id;
  state.watch.has(id) ? state.watch.delete(id) : state.watch.add(id);
  saveWatch();
  render();
});

["f-q", "f-kind", "f-region", "f-value", "f-active", "f-watch", "f-changed"].forEach((id) =>
  $(id).addEventListener("input", render));

load();
