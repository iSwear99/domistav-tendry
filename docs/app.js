/* DOMISTAV Tendry — dashboard. Vše běží v prohlížeči; ★/👎 se ukládají
   v localStorage a volitelně synchronizují přes PRIVÁTNÍ GitHub Gist
   uživatele (token jen se scope gist, vložený na každém zařízení).
   Do veřejného repozitáře se o zájmech uživatele nezapisuje nic. */
"use strict";

const LS_WATCH = "dt.watchlist";
const LS_SEEN = "dt.lastVisit";
const LS_DISLIKE = "dt.dislikes";
const LS_SYNC = "dt.sync";        // {token, gistId}
const LS_DOC = "dt.syncDoc";      // {watch:{id:{ts,on}}, dislikes:{id:{ts,on,fp}}}
const SYNC_FILE = "domistav-tendry-sync.json";

const $ = (id) => document.getElementById(id);
const state = {
  tenders: [],
  changes: {},   // centrální archiv změn (docs/data/changes.json, git)
  watch: new Set(),      // naplní deriveFromDoc() ze syncDoc
  // 👎 odmítnuté: id → otisk zakázky {cpv, ico, toks, title} uložený
  // v okamžiku odmítnutí (přežije i zmizení zakázky z dat)
  dislikes: {},          // naplní deriveFromDoc() ze syncDoc
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

/* ── synchronizace ★/👎 mezi zařízeními (privátní GitHub Gist) ───────────
   Dokument nese u každé položky časovou značku; sloučení = novější
   vyhrává (funguje i offline, historie odznačení se zachovává).
   Bez nastaveného tokenu vše běží dál čistě v localStorage. */

const syncDoc = (() => {
  const d = JSON.parse(localStorage.getItem(LS_DOC) || "null")
    || { watch: {}, dislikes: {} };
  // migrace starších úložišť bez časových značek
  const now = Date.now();
  for (const id of JSON.parse(localStorage.getItem(LS_WATCH) || "[]")) {
    if (!d.watch[id]) d.watch[id] = { ts: now, on: true };
  }
  const oldDis = JSON.parse(localStorage.getItem(LS_DISLIKE) || "{}");
  for (const id of Object.keys(oldDis)) {
    if (!d.dislikes[id]) d.dislikes[id] = { ts: now, on: true, fp: oldDis[id] };
  }
  return d;
})();

// sync po pull ze vzdáleného zařízení musí překreslit i AI výběr
function deriveFromDoc() {
  state.watch = new Set(
    Object.keys(syncDoc.watch).filter((id) => syncDoc.watch[id].on));
  state.dislikes = {};
  for (const [id, e] of Object.entries(syncDoc.dislikes)) {
    if (e.on) state.dislikes[id] = e.fp;
  }
}

function persistDoc() {
  localStorage.setItem(LS_DOC, JSON.stringify(syncDoc));
  localStorage.setItem(LS_WATCH, JSON.stringify([...state.watch]));
  localStorage.setItem(LS_DISLIKE, JSON.stringify(state.dislikes));
  schedulePush();
}

function mergeDocs(remote) {
  let changed = false, localNewer = false;
  for (const kind of ["watch", "dislikes"]) {
    const loc = syncDoc[kind], rem = (remote || {})[kind] || {};
    for (const [id, e] of Object.entries(rem)) {
      if (!loc[id] || e.ts > loc[id].ts) { loc[id] = e; changed = true; }
    }
    for (const id of Object.keys(loc)) {
      if (!rem[id] || loc[id].ts > rem[id].ts) localNewer = true;
    }
  }
  return { changed, localNewer };
}

const gistApi = (path, opts = {}) => {
  const cfg = JSON.parse(localStorage.getItem(LS_SYNC) || "null");
  if (!cfg || !cfg.token) return Promise.reject(new Error("bez tokenu"));
  return fetch("https://api.github.com" + path, {
    ...opts,
    headers: {
      Authorization: "Bearer " + cfg.token,
      Accept: "application/vnd.github+json",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
    },
  }).then((r) => {
    if (!r.ok) throw new Error("GitHub API " + r.status);
    return r.json();
  });
};

let pushTimer = null;
function schedulePush() {
  const cfg = JSON.parse(localStorage.getItem(LS_SYNC) || "null");
  if (!cfg || !cfg.gistId) return;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    gistApi("/gists/" + cfg.gistId, {
      method: "PATCH",
      body: JSON.stringify({
        files: { [SYNC_FILE]: { content: JSON.stringify(syncDoc) } },
      }),
    }).then(() => setSyncStatus("✓ synchronizováno"))
      .catch((e) => setSyncStatus("⚠ " + e.message));
  }, 1500);
}

async function syncPull() {
  const cfg = JSON.parse(localStorage.getItem(LS_SYNC) || "null");
  if (!cfg || !cfg.token) return;
  try {
    if (!cfg.gistId) {
      const gists = await gistApi("/gists?per_page=100");
      const mine = gists.find((g) => g.files && g.files[SYNC_FILE]);
      if (mine) cfg.gistId = mine.id;
      else {
        const made = await gistApi("/gists", {
          method: "POST",
          body: JSON.stringify({
            description: "DOMISTAV Tendry — synchronizace oblíbených (soukromé)",
            public: false,
            files: { [SYNC_FILE]: { content: JSON.stringify(syncDoc) } },
          }),
        });
        cfg.gistId = made.id;
      }
      localStorage.setItem(LS_SYNC, JSON.stringify(cfg));
    }
    const gist = await gistApi("/gists/" + cfg.gistId);
    const file = gist.files && gist.files[SYNC_FILE];
    const remote = file && file.content ? JSON.parse(file.content) : null;
    const { changed, localNewer } = mergeDocs(remote);
    if (changed) {
      deriveFromDoc();
      localStorage.setItem(LS_DOC, JSON.stringify(syncDoc));
      buildModel();
      render();
      renderAI();
      renderComp();
    }
    if (localNewer) schedulePush();
    setSyncStatus("✓ synchronizováno");
  } catch (e) {
    setSyncStatus("⚠ " + e.message);
  }
}

function setSyncStatus(msg) {
  const el = $("sync-status");
  if (el) el.textContent = msg;
  const btn = $("sync-btn");
  if (btn) btn.classList.toggle(
    "on", !!JSON.parse(localStorage.getItem(LS_SYNC) || "null"));
}

function saveWatch() {
  const now = Date.now();
  for (const id of state.watch) {
    if (!syncDoc.watch[id] || !syncDoc.watch[id].on) {
      syncDoc.watch[id] = { ts: now, on: true };
    }
  }
  for (const id of Object.keys(syncDoc.watch)) {
    if (syncDoc.watch[id].on && !state.watch.has(id)) {
      syncDoc.watch[id] = { ts: now, on: false };  // historie zůstává
    }
  }
  persistDoc();
}

/* ── učení relevance z odmítnutých (vše jen v prohlížeči) ────────────────
   Odmítnutí uloží otisk zakázky; z otisků se počítají četnosti CPV skupin,
   zadavatelů a výrazných slov názvu. Opakují-li se, podobné zakázky se
   skrývají (sledované ★ nikdy). Filtr „jen odmítnuté 👎" vše zpřístupní. */

function saveDislikes() {
  const now = Date.now();
  for (const [id, fp] of Object.entries(state.dislikes)) {
    if (!syncDoc.dislikes[id] || !syncDoc.dislikes[id].on) {
      syncDoc.dislikes[id] = { ts: now, on: true, fp };
    }
  }
  for (const id of Object.keys(syncDoc.dislikes)) {
    if (syncDoc.dislikes[id].on && !(id in state.dislikes)) {
      syncDoc.dislikes[id] = { ...syncDoc.dislikes[id], ts: now, on: false };
    }
  }
  persistDoc();
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
    // no-cache = vždy revalidace přes ETag (na Pages levné 304); bez ní
    // prohlížeč heuristicky drží starý JSON i po denní aktualizaci
    const j = (url) => fetch(url, { cache: "no-cache" }).then((r) => r.json());
    const [tenders, meta, changes, comp, smlouvy, dok, ai] = await Promise.all([
      j("data/tenders.json"),
      j("data/meta.json"),
      j("data/changes.json").catch(() => ({})),
      j("data/competition.json").catch(() => []),
      j("data/smlouvy.json").catch(() => ({})),
      j("data/dokumenty.json").catch(() => ({})),
      j("data/ai_filter.json").catch(() => ({})),
    ]);
    state.tenders = tenders;
    state.changes = changes || {};
    state.comp = comp || [];
    state.smlouvy = (smlouvy && smlouvy.links) || {};
    state.zd = (dok && dok.zd) || {};
    state.zpravy = (dok && dok.zpravy) || {};
    state.ai = (ai && ai.verdikty) || {};
    buildDf();
    buildModel();
    renderMeta(meta);
  } catch (e) {
    $("meta-line").textContent = "Data se nepodařilo načíst.";
    showError(["Chyba načtení dat: " + e.message]);
    return;
  }
  render();
  renderAI();
  renderComp();
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

  // rychlý filtr klikem na kartu „nových" v horní statistice
  if (state.quickNew && !isNew(t)) return false;

  // AI filtr: verdikt „ne" se skrývá (sledované ★ nikdy); přepínač
  // „vč. AI-vyřazených" vše zobrazí — data se nemažou, jen skrývají
  const ai = (state.ai || {})[t.id];
  if (ai && ai.verdikt === "ne" && !$("f-ai").checked
      && !state.watch.has(t.id)) return false;

  // vzdálenost sliderem; poloha neurčena dle checkboxu (nezahazovat tiše)
  if (t.loc_unknown) {
    if (!$("t-unknown").checked) return false;
  } else if (!(t.dist_km != null && t.dist_km <= +$("t-dist").value)) {
    return false;
  }

  // cenové rozpětí v mil.; horní slider na maximu = bez stropu;
  // zakázky bez hodnoty se zobrazují vždy (štítek „hodnota neuvedena")
  if (t.value != null) {
    const m = t.value / 1e6;
    if (m < +$("t-min").value) return false;
    const max = +$("t-max").value;
    if (max < +$("t-max").max && m > max) return false;
  }

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
        ${t.link_dead ? '<span class="tag" title="Stránka detailu opakovaně vrací 404 — zakázka byla vyřazena z aktivních">odkaz nedostupný</span>' : ""}
        ${(() => {
          const ai = (state.ai || {})[t.id];
          if (!ai) return "";
          if (ai.verdikt === "ne") return `<span class="tag ai-ne" title="${esc(ai.duvod)}">AI: nerelevantní</span>`;
          if (ai.verdikt === "nejisto") return `<span class="tag ai-nejisto" title="${esc(ai.duvod)}">AI: nejisté</span>`;
          return "";
        })()}
        ${(t.cpv || []).slice(0, 2).map((c) => `<span class="tag">CPV ${esc(c)}</span>`).join("")}
        ${visit}${clar}${irrBadge}
      </div>
      ${zdLine(t)}
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

// Řádek s poli vytěženými ze zadávací dokumentace (data/dokumenty.json).
function zdLine(t) {
  const zd = (state.zd || {})[t.id];
  if (!zd || !zd.doc_url) return "";
  const parts = [];
  if (zd.misto) parts.push("místo: " + esc(zd.misto));
  if (zd.termin) parts.push("termín: " + esc(zd.termin));
  if (zd.hodnota) parts.push("hodnota: " + fmtKc.format(zd.hodnota) + " Kč");
  return `<div class="zd" title="Automaticky vytěženo ze zadávací dokumentace — orientační, ověřte v dokumentu">
    <a href="${esc(zd.doc_url)}" target="_blank" rel="noopener">📄 zadávací dokumentace</a>${parts.length ? " · " + parts.join(" · ") : ""}</div>`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// dvojitý slider: jezdce se nesmí překřížit + vybarvení rozsahu na dráze
function dualSync(minEl, maxEl, fillEl, changed) {
  if (+minEl.value > +maxEl.value) {
    if (changed === maxEl) maxEl.value = minEl.value;
    else minEl.value = maxEl.value;
  }
  const lo = +minEl.min, hi = +minEl.max;
  const a = ((+minEl.value - lo) / (hi - lo)) * 100;
  const b = ((+maxEl.value - lo) / (hi - lo)) * 100;
  fillEl.style.left = a + "%";
  fillEl.style.width = Math.max(0, b - a) + "%";
}

function render() {
  dualSync($("t-min"), $("t-max"), $("t-fill"), null);
  // zvýraznění aktivních rychlých filtrů na kartách statistik
  $("stat-new").classList.toggle("on", !!state.quickNew);
  $("stat-watch").classList.toggle("on", $("f-watch").checked);
  $("stat-changed").classList.toggle("on", $("f-changed").checked);
  $("stat-hidden").classList.toggle("on", $("f-disliked").checked);
  $("t-dist-val").textContent = $("t-dist").value;
  $("t-min-val").textContent = $("t-min").value;
  $("t-max-val").textContent =
    +$("t-max").value >= +$("t-max").max ? "∞" : $("t-max").value;
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

/* ── Konkurence ─────────────────────────────────────────────────────────── */

// Vazba na Registr smluv (data/smlouvy.json) — dodatky a verifikace cen.
function linkOf(c) {
  return (state.smlouvy || {})[c.id];
}

// Cena včetně dodatků — používají ji slidery i agregace TOP firem.
function effPrice(c) {
  if (c.price_contracted == null) return null;
  const l = linkOf(c);
  return c.price_contracted + (l && l.amendments_total ? l.amendments_total : 0);
}

// % nárůstu: priorita price_paid, jinak vysoutěženo + dodatky,
// vždy proti price_contracted.
function effGrowth(c) {
  const base = c.price_contracted;
  if (!base) return null;
  const l = linkOf(c);
  const current = c.price_paid != null
    ? c.price_paid
    : (l && l.amendments_total ? base + l.amendments_total : null);
  if (current == null) return null;
  return Math.round((current / base - 1) * 1000) / 10;
}

function compPasses(c) {
  const q = $("c-q").value.trim().toLowerCase();
  if (q && !(c.title + " " + c.winner + " " + c.authority)
    .toLowerCase().includes(q)) return false;

  // datum zadání od–do (prázdné = bez omezení)
  const from = $("c-from").value, to = $("c-to").value;
  if (from && (c.awarded || "") < from) return false;
  if (to && (c.awarded || "") > to) return false;

  // relevance dtto Zakázky: odmítnuté a odhadem nerelevantní se skrývají,
  // filtr „jen odmítnuté 👎" je naopak zobrazí ke kontrole
  const irr = irrelevance(c);
  if ($("c-disliked").checked) return !!irr;
  if (irr) return false;

  if (c.loc_unknown) {
    if (!$("c-unknown").checked) return false;
  } else if (c.dist_km == null || c.dist_km > +$("c-dist").value) {
    return false;
  }

  // cenové rozpětí v mil. POČÍTÁ S CENOU VČETNĚ DODATKŮ; horní slider
  // na maximu = bez stropu; zakázky bez ceny se nezahazují (štítek)
  const ep = effPrice(c);
  if (ep != null) {
    const m = ep / 1e6;
    if (m < +$("c-min").value) return false;
    const max = +$("c-max").value;
    if (max < +$("c-max").max && m > max) return false;
  }
  return true;
}

function compRowHTML(c) {
  const l = linkOf(c);
  const g = effGrowth(c);
  const price = c.price_contracted != null
    ? `<span class="val">${fmtKc.format(c.price_contracted)}<small>${c.estimated ? "předpokládaná (odhad)" : "vysoutěžená"} Kč bez DPH</small></span>`
    : `<span class="val">—<small>cena neuvedena</small></span>`;
  const growth = g != null
    ? `<span class="tag growth ${g > 0 ? "up" : "down"}" title="Aktuální cena (uhrazeno, jinak vysoutěženo + dodatky) vs. vysoutěženo">${g > 0 ? "+" : ""}${g} %</span>`
    : "";
  // cenový řetězec: vysoutěženo → + dodatky → uhrazeno/aktuální
  const chainParts = [];
  if (l && l.amendments_count) {
    chainParts.push(
      `<a class="tag amend" href="${esc(l.url)}" target="_blank" rel="noopener"
        title="Dodatky ke smlouvě v Registru smluv — pozor, hodnota dodatku může být i nová celková cena">dodatky: ${l.amendments_count}×${l.amendments_total ? " (+" + fmtKc.format(l.amendments_total) + " Kč)" : ""}</a>`);
  }
  if (l && l.confidence === "low") {
    chainParts.push(
      `<a class="tag warn" href="${esc(l.url)}" target="_blank" rel="noopener"
        title="Párování na Registr smluv podle IČO a data — cena smlouvy neodpovídá přesně, ověřte ručně">⚠ ověřit párování</a>`);
  }
  const zp = (state.zpravy || {})[c.id];
  if (zp && zp.doc_url) {
    const zpParts = [];
    if (zp.vitez && !c.winner) zpParts.push(esc(zp.vitez));
    if (zp.cena) zpParts.push(fmtKc.format(zp.cena) + " Kč");
    chainParts.push(
      `<a class="tag amend" href="${esc(zp.doc_url)}" target="_blank" rel="noopener"
        title="Písemná zpráva zadavatele — vytěženo automaticky, orientační">zpráva zadavatele${zpParts.length ? ": " + zpParts.join(" · ") : ""}</a>`);
  }
  const current = c.price_paid != null
    ? `uhrazeno ${fmtKc.format(c.price_paid)} Kč`
    : (l && l.amendments_total && c.price_contracted != null
        ? `aktuálně ${fmtKc.format(effPrice(c))} Kč`
        : "");
  const paid = (current || chainParts.length || growth)
    ? `<span class="paid">${current} ${growth} ${chainParts.join(" ")}</span>`
    : "";
  const title = c.url
    ? `<a href="${c.url}" target="_blank" rel="noopener">${esc(c.title)}</a>`
    : esc(c.title);
  const irrBadge = $("c-disliked").checked
    ? `<span class="tag irr">${esc(irrelevance(c) || "")}</span>` : "";
  return `<li class="row" data-id="${esc(c.id)}">
    <div class="head">
      <h2>${title}</h2>
      <div class="auth">${esc(c.authority)}</div>
      <div class="winner">${c.winner
        ? "🏆 " + esc(c.winner) + (c.winner_ico ? ` <small>IČO ${esc(c.winner_ico)}</small>` : "")
        : "vítěz v datech neuveden"}</div>
      <div class="tags">
        <span class="tag ${c.kind === "VZ" ? "vz" : "vzmr"}">${c.kind}</span>
        ${c.dist_km != null ? `<span class="tag dist">~${c.dist_km} km</span>` : ""}
        ${c.loc_unknown ? '<span class="tag">poloha neurčena</span>' : ""}
        ${(c.cpv || []).slice(0, 1).map((x) => `<span class="tag">CPV ${esc(x)}</span>`).join("")}
        ${c.kw_match ? '<span class="tag kw">dle názvu</span>' : ""}
        ${irrBadge}
      </div>
      ${paid}
    </div>
    ${price}
    <span class="due">${esc((c.awarded || "").split("-").reverse().join("."))}<small>zadáno</small></span>
    <button class="thumb${state.dislikes[c.id] ? " on" : ""}" title="Označit jako nerelevantní — promítne se i do Zakázek" aria-pressed="${!!state.dislikes[c.id]}">👎</button>
  </li>`;
}

function renderTopFirms(shown) {
  const agg = new Map();
  let total = 0;
  for (const c of shown) {
    if (!c.winner) continue;
    const p = effPrice(c) || 0;   // cena včetně dodatků
    total += p;
    const a = agg.get(c.winner) || { n: 0, sum: 0 };
    a.n += 1;
    a.sum += p;
    agg.set(c.winner, a);
  }
  const top = [...agg.entries()].sort((a, b) => b[1].sum - a[1].sum).slice(0, 10);
  const maxSum = top.length ? top[0][1].sum : 1;
  $("c-top-total").textContent = total
    ? `— celkový objem vyhraných zakázek v aktuálním filtru: ${fmtKc.format(total)} Kč bez DPH`
    : "";
  $("c-top-list").innerHTML = top.map(([name, a], i) =>
    `<li data-firm="${esc(name)}" title="Kliknutím vyfiltrovat zakázky této firmy">
      <span class="tf-name"><b>${i + 1}.</b> ${esc(name)}</span>
      <span class="tf-bar"><i style="width:${Math.max(2, Math.round(a.sum / maxSum * 100))}%"></i></span>
      <small>${a.n}× · ${fmtKc.format(a.sum)} Kč</small>
    </li>`
  ).join("");
  $("c-top").hidden = top.length === 0;
}

// klik na firmu v žebříčku vyfiltruje její vyhrané zakázky (a druhým
// klikem na tutéž firmu se filtr zase zruší)
document.addEventListener("click", (e) => {
  const li = e.target.closest("#c-top-list li[data-firm]");
  if (!li) return;
  const q = $("c-q");
  q.value = q.value === li.dataset.firm ? "" : li.dataset.firm;
  renderComp();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

function renderComp() {
  if (!$("comp-list")) return;
  dualSync($("c-min"), $("c-max"), $("c-fill"), null);
  $("c-dist-val").textContent = $("c-dist").value;
  $("c-min-val").textContent = $("c-min").value;
  $("c-max-val").textContent =
    +$("c-max").value >= +$("c-max").max ? "∞" : $("c-max").value;
  const shown = (state.comp || []).filter(compPasses);
  $("comp-list").innerHTML = shown.map(compRowHTML).join("");
  $("comp-empty").hidden = shown.length > 0;
  $("c-count").textContent = `${shown.length} z ${(state.comp || []).length}`;
  renderTopFirms(shown);
}

// AI výběr: aktivní zakázky s verdiktem „ano" (volitelně i „nejisto"),
// seřazené podle lhůty — stejné řádky a interakce jako záložka Zakázky
function renderAI() {
  if (!$("ai-list")) return;
  const withNejisto = $("a-nejisto").checked;
  const shown = state.tenders.filter((t) => {
    if (t.expired) return false;
    const v = (state.ai || {})[t.id];
    return v && (v.verdikt === "ano"
      || (withNejisto && v.verdikt === "nejisto"));
  });
  $("ai-list").innerHTML = shown.map(rowHTML).join("");
  $("ai-empty").hidden = shown.length > 0
    || Object.keys(state.ai || {}).length > 0;
  $("a-count").textContent = Object.keys(state.ai || {}).length
    ? `${shown.length} vybraných`
    : "";
}

function switchTab(name) {
  for (const p of ["tenders", "ai", "comp"]) {
    $("panel-" + p).hidden = p !== name;
    $("tab-" + p).classList.toggle("on", p === name);
    $("tab-" + p).setAttribute("aria-selected", String(p === name));
  }
}
$("tab-tenders").addEventListener("click", () => switchTab("tenders"));
$("tab-ai").addEventListener("click", () => switchTab("ai"));
$("tab-comp").addEventListener("click", () => switchTab("comp"));
$("a-nejisto").addEventListener("input", renderAI);
$("c-min").addEventListener("input", (e) =>
  dualSync($("c-min"), $("c-max"), $("c-fill"), e.target));
$("c-max").addEventListener("input", (e) =>
  dualSync($("c-min"), $("c-max"), $("c-fill"), e.target));
["c-q", "c-dist", "c-min", "c-max", "c-unknown", "c-disliked", "c-from",
 "c-to"].forEach((id) => $(id).addEventListener("input", renderComp));

/* ── klikací statistiky: rychlé filtry (pokyn 29. 7. 2026) ── */
$("stat-new").addEventListener("click", () => {
  state.quickNew = !state.quickNew;
  render();
});
$("stat-watch").addEventListener("click", () => {
  $("f-watch").checked = !$("f-watch").checked;
  render();
});
$("stat-changed").addEventListener("click", () => {
  $("f-changed").checked = !$("f-changed").checked;
  render();
});
$("stat-hidden").addEventListener("click", () => {
  $("f-disliked").checked = !$("f-disliked").checked;
  render();
});

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
      const t = state.tenders.find((x) => x.id === id)
        || (state.comp || []).find((x) => x.id === id);
      if (t) state.dislikes[id] = fingerprint(t);
    }
    saveDislikes();
    buildModel();
  }
  render();
  renderAI();
  renderComp();
});

// clamp dvojitých sliderů PŘED renderem (tažený jezdec se zastaví o druhý)
$("t-min").addEventListener("input", (e) =>
  dualSync($("t-min"), $("t-max"), $("t-fill"), e.target));
$("t-max").addEventListener("input", (e) =>
  dualSync($("t-min"), $("t-max"), $("t-fill"), e.target));
["f-q", "f-kind", "t-dist", "t-min", "t-max", "t-unknown", "f-active",
 "f-watch", "f-changed", "f-disliked", "f-ai"].forEach((id) =>
  $(id).addEventListener("input", render));

/* ── dialog synchronizace ── */
$("sync-btn").addEventListener("click", () => {
  const cfg = JSON.parse(localStorage.getItem(LS_SYNC) || "null");
  $("sync-token").value = "";
  $("sync-info").textContent = cfg
    ? "Připojeno (gist " + (cfg.gistId || "se vytvoří") + ")."
    : "Nepřipojeno — ★/👎 zůstávají jen v tomto prohlížeči.";
  $("sync-dlg").showModal();
});
$("sync-save").addEventListener("click", (e) => {
  e.preventDefault();
  const token = $("sync-token").value.trim();
  if (token) {
    localStorage.setItem(LS_SYNC, JSON.stringify({ token, gistId: "" }));
    setSyncStatus("připojuji…");
    syncPull();
  }
  $("sync-dlg").close();
});
$("sync-off").addEventListener("click", (e) => {
  e.preventDefault();
  localStorage.removeItem(LS_SYNC);
  setSyncStatus("");
  $("sync-dlg").close();
});
$("sync-close").addEventListener("click", (e) => {
  e.preventDefault();
  $("sync-dlg").close();
});

deriveFromDoc();
setSyncStatus("");
load().then(syncPull);
