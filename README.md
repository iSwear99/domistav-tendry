# DOMISTAV Tendry v1

Interní bezplatný monitoring veřejných zakázek na stavební práce pro
DOMISTAV HK s.r.o. — **okruh 50 km od Hradce Králové**, CPV 45* +
záchytná klíčová slova (rekonstrukce, zateplení, výstavba…), od 2 mil. Kč
bez DPH, včetně VZMR z profilů zadavatelů.

## Jak to funguje

- **Data**: GitHub Actions denně v 8:00 (Europe/Prague) stáhne ISVZ Open
  Data + XML profilů zadavatelů, volitelně obohatí detaily z NEN API,
  vyfiltruje (obor, okruh, hodnota), deduplikuje napříč zdroji a commitne
  do `docs/data/` — včetně centrálního archivu změn ZD (`changes.json`,
  shodný na všech zařízeních).
- **Aplikace**: statický dashboard na GitHub Pages (`docs/`): filtry,
  vzdálenost, lhůty s odpočtem, badge „nové" a „změna ZD" s historií.
  Sledování (★) zůstává jen v prohlížeči (localStorage).

## Zprovoznění

Řídí se souborem [CLAUDE.md](CLAUDE.md) — obsahuje závazné parametry,
doménová pravidla a **povinné úvodní úkoly 1–8** (ověření struktury dat,
doplnění profilů, číselník obcí, NEN API, logo, test, nasazení).

## Struktura

```
scraper/            config.py (parametry), fetch_isvz.py, fetch_profily.py,
                    fetch_nen.py, geo.py, main.py (orchestrace)
docs/               GitHub Pages (index.html, app.js, style.css, data/)
.github/workflows/  update-data.yml (denní cron 8:00 Praha + guard DST)
CLAUDE.md           zadání, pravidla, úkoly, nasazení
```
