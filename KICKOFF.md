# KICKOFF — DOMISTAV Tendry (zadání zadavatele)

Řídicí dokument vedle CLAUDE.md. Původní zadání zadavatele je níže
v plném znění; tento úvodní blok zachycuje stav plnění k 28. 7. 2026.

## Stav plnění

| Úkol zadání | Stav |
|---|---|
| 0 Audit projektu | ✅ 28. 7. 2026 — žádné ruční úpravy nenalezeny, odchylky nasazení schváleny (viz CLAUDE.md) |
| 1 ISVZ Open Data | ✅ hotovo (měsíční ZIPy, parser dle dokumentace 2.9.0) |
| 2 Profily zadavatelů | ✅ hotovo (35 profilů, XML živě ověřeno) |
| 3 Konkurence | 🔄 v práci (pole výsledků, backfill 12 měsíců, competition.json) |
| 4 Geo (sídlo firmy + obce.csv) | 🔄 obce.csv ✅ (celá ČR z RÚIAN — schválená odchylka); GEO_CENTER na Pražská třída 901/145 v práci |
| 5 NEN API podklady | ✅ postup předán (komerční certifikát → referenční prostředí spravcenen@mmr.cz → produkce); modul neaktivní |
| 6 Dry-run test | ✅ pro Zakázky; pro Konkurenci proběhne po implementaci |
| 7 Nasazení | ✅ repo iSwear99/domistav-tendry, Pages běží, workflow na self-hosted runneru `domistav-pc` (ISVZ blokuje IP GitHub runnerů — schválená odchylka) |
| 8 Akceptace | ✅ Zakázky; Konkurence po dokončení úkolu 3 |

Schválené odchylky od původní specifikace (audit 28. 7. 2026, detaily
v CLAUDE.md): self-hosted runner, stdlib urllib místo requests,
obce.csv pro celou ČR, explicitní parser místo plochého FIELD_MAP,
stabilní ID `rvz:<NIPEZ>`, klíčová slova na hranici slova, stavová
expirace, retence záznamů při výpadku zdroje, 👎 s učením relevance.

---

## Původní zadání (plné znění)

## Kontext
Interní aplikace DOMISTAV HK s.r.o. pro monitoring veřejných zakázek na
stavební práce. Serverless: GitHub Actions (denní cron) + GitHub Pages,
Python scraper (stdlib + requests), vanilla JS/HTML/CSS frontend.

## Závazné parametry (neměnit bez pokynu zadavatele)
- Region: okruh 50 km od sídla firmy Pražská třída 901/145, 500 04
  Hradec Králové (Kukleny). NUTS CZ052/053/020/051/063 jen předfiltr.
- Obor: CPV prefix 45 (hlavní i doplňkové) + záchytná klíčová slova
  v názvu (pozitivní/negativní seznamy v config.py); CPV 45 negativní
  slova nepřebíjí; VZMR z profilu bez CPV ponechat.
- Hodnota: od 2 mil. Kč bez DPH; bez hodnoty ponechat s příznakem.
- Sporné případy vždy ponechat s příznakem (no_value, loc_unknown,
  kw_match) — nikdy tiše nezahazovat.
- Konkurence: sbírat celý okruh 50 km bez cenového stropu, klouzavých
  12 měsíců dle data zadání; rozpětí vzdálenosti/ceny filtruje uživatel
  slidery v UI (výchozí 15 km, 5–100 mil.); ceny price_contracted
  (vysoutěžená, fallback předpokládaná + estimated), price_paid
  (uhrazená vč. víceprací, aktualizovat denně), growth_pct.
- Aktualizace denně v 8:00 Europe/Prague (dva UTC crony + guard krok).
- Firemní identita: docs/logo.png + „TENDRY v1", bílá lišta, text
  #0d0d0d, oranžová #f87840 — HOTOVO, neměnit.
- Doménová pravidla: data se nemažou (expired flag), idempotence,
  pojistka proti výsledku < 20 % předchozího počtu, deduplikace napříč
  zdroji (IČO + normalizovaný název, přednost ISVZ, bez IČO nededuplikovat),
  watchlist jen v localStorage — do repa nikdy nic o zájmech uživatele.

## Pravidla spolupráce
- Před každým zásadním rozhodnutím zjištění → schválení → provedení.
- Nic nemazat, jen přidávat/označovat; commity po logických celcích
  s českými popisy.
- Při rozporu ručních úprav se specifikací: zastavit a zeptat se.
