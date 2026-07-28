# DOMISTAV Tendry — monitoring veřejných zakázek

Interní nástroj DOMISTAV HK s.r.o. pro sledování veřejných zakázek na stavební práce.
Náhrada za ukončenou službu tendry.cz. Pracuje **výhradně s veřejnými daty** —
neobsahuje žádná firemní data, proto smí běžet v public GitHub repozitáři.

## Architektura (závazná)

- **Bez serveru.** GitHub Actions (cron 1× denně) spouští Python scraper,
  výsledné JSON commitne do `docs/data/`. Frontend = statický web na GitHub Pages
  (vanilla JS/HTML/CSS, žádné build frameworky — stejný princip jako domistav-app).
  **Runner je self-hosted na PC uživatele** (`domistav-pc`, autostart po
  přihlášení ze složky Po spuštění): isvz.nipez.cz blokuje mimoevropské IP,
  GitHub-hosted runnery (USA) se k němu nepřipojí. Při vypnutém PC běh čeká
  ve frontě (≤24 h); výpadek zdroje kryje retence záznamů v `main.py`.
- **Watchlist a poznámky** ukládá frontend pouze do `localStorage` prohlížeče.
  Nikdy neposílat žádná uživatelská data na server.
- Python: pouze stdlib + `requests`. Žádné těžké závislosti.

## Parametry filtrace (v1 — schváleno zadavatelem)

| Parametr | Hodnota |
|---|---|
| Region | **Okruh 50 km od Hradce Králové** (geo.py, haversine). NUTS (CZ052/053/020/051/063) slouží jen jako hrubý předfiltr. Poloha se určuje: 1) obec z místa plnění/názvu dle `scraper/obce.csv`, 2) sídlo okresu z NUTS4, 3) sídlo zadavatele u profilových VZMR. Nad 50 km se zahazuje; neurčitelná poloha se ponechává s příznakem `loc_unknown` a štítkem v UI. |
| CPV | 45* (stavební práce) — prefix match na hlavní i doplňkové CPV |
| Klíčová slova | Záchytná síť vedle CPV: názvy s pozitivním výrazem (rekonstrukce, modernizace, zateplení, výstavba, stavební úpravy, snížení energetické náročnosti…) projdou i bez CPV 45, pokud neobsahují negativní výraz (projektová dokumentace, dozor, software, vozidla…). Seznamy v `config.py`, porovnání bez diakritiky, příznak `kw_match` + štítek „dle názvu" v UI. CPV 45 negativní slova NEpřebíjí. |
| Min. předpokládaná hodnota | 2 000 000 Kč bez DPH; zakázky **bez uvedené hodnoty ponechat** a označit příznakem `no_value` |
| VZMR | ANO — z XML profilů zadavatelů (města + kraje v HK/Pce regionu), viz `scraper/config.py` |
| Notifikace | v1 pouze dashboard (e-mail až v2) |

## Datové zdroje (vše bezplatné, veřejné)

1. **ISVZ Open Data** — https://isvz.nipez.cz/opendata — primární zdroj.
   Od 2/2024 JSON exporty z Registru veřejných zakázek (agreguje VVZ, NEN,
   Tender arenu, TENDERMARKET). Dokumentace struktury je publikována na ISVZ
   v sekci nápovědy („Nová Open Data – dokumentace JSON").
2. **Profily zadavatelů** — strojově čitelné XML dle vyhlášky č. 168/2016 Sb.
   Endpoint: `{URL profilu}/XMLdataVZ?od=...&do=...`. Zdroj VZMR.
3. **NEN veřejné API** — obohacení detailů (prohlídka místa plnění,
   vysvětlení ZD) k aktivním VZ; autentizace certifikátem přes GitHub
   secrets, modul `fetch_nen.py` (neaktivní do zřízení přístupu).
4. (v2) TED API pro nadlimitní, e-mailový souhrn.

## ⚠️ POVINNÉ ÚVODNÍ ÚKOLY — ✅ SPLNĚNO 28. 7. 2026 (kromě úkolu 4)

Stav: úkoly 1–3 a 5–8 hotové, v1 nasazena a ověřena (workflow běží na
self-hosted runneru, dashboard živý). Klíčové odchylky zjištěné z reality:
ISVZ exporty jsou měsíční ZIPy publikované ~1.–5. dne následujícího měsíce
(čerstvá data dodávají profily), mapování polí je přímo ve fetch_isvz.py,
`obce.csv` pokrývá celou ČR (nutné pro vylučování vzdálených zakázek;
geo.py navíc zvládá skloňované tvary a sídlo zadavatele), scraper běží na
stdlib urllib (requests má na PC uživatele rozbité TLS). Úkol 4 (NEN API)
zůstává otevřený — čeká na podání žádosti uživatelem; nasazení na něm
nezávisí. Původní znění úkolů níže ponecháno pro kontext:

Kód je napsán defenzivně s mapovacími konstantami, které NEBYLO možné
ověřit proti reálným datům z vývojového prostředí (portály blokují
automatizovaný přístup). Postupuj přesně v tomto pořadí:

1. **ISVZ Open Data:** otevři https://isvz.nipez.cz/opendata, zjisti
   přesné URL JSON exportů (kategorie „Veřejná zakázka") a stáhni vzorek
   + dokumentaci struktury. Podle reality uprav `ISVZ_DOWNLOAD_URLS`
   a celý `FIELD_MAP` v `scraper/config.py` (včetně nepotvrzených polí
   `place`, `site_visit`, `clarifications` — pokud v datech nejsou,
   ponech prázdné, frontend je skryje a změny pokryje diff). Současně
   ověř podmínky užití open dat a případné požadavky na hlavičky
   požadavků (User-Agent s kontaktem je přednastaven).
2. **Profily zadavatelů (VZMR):** a) doplň skutečné `profile_url`
   u 12 předvyplněných zadavatelů v `PROFILY_ZADAVATELU` (spolehlivě
   přes VVZ — „oznámení profilu zadavatele" dle IČO; IČO ověř přes ARES);
   b) rozšiř seznam o profily všech ORP + krajů v okruhu na platformách
   z `PLATFORMY_PROFILU` (PVU, E-ZAK, Tender arena, TENDERMARKET, NEN,
   EVEZA) včetně souřadnic sídla (`lat`/`lon`) kvůli geo filtru;
   c) na jednom reálném profilu ověř přesný formát parametrů `od`/`do`
   XML rozhraní dle vyhl. 168/2016 Sb. a případně uprav `fetch_profily.py`.
3. **Číselník obcí pro geo filtr:** vygeneruj `scraper/obce.csv`
   (`nazev;lat;lon`, UTF-8) z otevřených dat RÚIAN/ČÚZK pro obce krajů
   CZ052, CZ053, CZ020, CZ051, CZ063 a commitni jej. Bez něj běží jen
   aproximace přes sídla okresů. Ověř název pole místa plnění
   (`FIELD_MAP["place"]`).
4. **NEN API (obohacení detailů):** proveď uživatele podáním žádosti
   o přístup k veřejnému API NEN (referenční → produkční prostředí,
   klientský certifikát). Po obdržení dokumentace doplň `NEN_API_BASE`
   a endpointy/mapování ve `fetch_nen.py`. Certifikát + klíč VÝHRADNĚ
   jako GitHub Actions secrets `NEN_CERT_PEM` / `NEN_KEY_PEM` — nikdy
   do repozitáře. Modul je do té doby neaktivní; nasazení na něj NEČEKÁ.
5. **Logo a firemní barvy:** ✅ HOTOVO v přípravě — `docs/logo.png`
   (ořez, výška 160 px) i barvy nastaveny: bílá lišta, text #0d0d0d,
   firemní oranžová #f87840 extrahovaná z loga (řídí i akcent celé
   aplikace). Neměnit bez pokynu uživatele.
6. **Lokální test:** `pip install -r scraper/requirements.txt` a poté
   `python scraper/main.py --dry-run` → zkontroluj počty, chyby
   v meta výstupu, diakritiku a namátkově vzdálenosti (`dist_km`).
7. **Založ repo a nasaď** (kroky níže) a ručně spusť workflow.
8. Teprve po úspěšném prvním běhu workflow a vizuální kontrole
   dashboardu považuj v1 za hotovou.

## Firemní identita hlavičky

Hlavička načítá `docs/logo.png` (fallback: textové „DOMISTAV") s dovětkem
„TENDRY v1". Barvy hlavičky řídí CSS proměnné `--brand-bg`, `--brand-fg`,
`--brand-accent` v `docs/style.css` — po dodání loga uživatelem nastavit
podle firemních barev DOMISTAV (dominantní barvy extrahovat z loga,
závazné hex kódy od uživatele mají přednost). Logo NIKDY nestahovat
automatizovaně z webu třetí strany — soubor dodá uživatel.

## Nasazení (public repo `domistav-tendry`)

```
cd C:\Users\mates\Documents\domistav-tendry
git init -b main
git add .
git commit -m "v1: DOMISTAV Tendry"
gh repo create domistav-tendry --public --source . --push
```
(bez `gh` CLI: založit repo na github.com a `git remote add origin ...; git push -u origin main`)

Poté v nastavení repa:
- **Settings → Pages** → Source: „Deploy from a branch", branch `main`, složka `/docs`.
- **Settings → Actions → General** → Workflow permissions: „Read and write permissions"
  (workflow commituje data).
- Ručně spusť workflow **Update tender data** (Actions → Run workflow) a ověř,
  že proběhl a commitnul `docs/data/`.

## Doménová pravidla (převzatá zásada DOMISTAV)

- Data se **nemažou** — zakázky po lhůtě se označí `expired`, ale zůstávají
  v historii (git historie = denní snapshoty).
- **Deduplikace napříč zdroji**: tatáž zakázka z ISVZ i profilu zadavatele
  se slučuje podle IČO + normalizovaného názvu (bez diakritiky/interpunkce);
  přednost má záznam z ISVZ, URL profilu se zachová v poli `profile_url`.
  Bez IČO se cross-source dedup neprovádí (riziko falešné shody).
- Scraper musí být **idempotentní**: opakované spuštění ve stejný den nesmí
  vytvořit duplicity (dedup podle stabilního ID zakázky, viz `main.py`).
- Při chybě jednoho zdroje pokračovat s ostatními a chybu zapsat do `meta.json`
  (frontend ji zobrazí) — nikdy nepřepsat platná data prázdným výstupem:
  pokud je výsledek podezřele malý (< 20 % předchozího počtu), workflow selže
  a data se nepřepíší.
- Žádné osobní údaje nad rámec zveřejněných dat zadavatelů.

## Struktura výstupních dat

`docs/data/tenders.json` — pole objektů:
```json
{
  "id": "stabilní ID (systémové číslo VZ / hash zdroj+číslo)",
  "source": "isvz | profil:<klíč zadavatele>",
  "title": "...", "authority": "...", "authority_ico": "...",
  "cpv": ["45..."], "nuts": ["CZ052"],
  "value": 12500000, "no_value": false,
  "published": "2026-07-27", "deadline": "2026-08-15T10:00:00",
  "url": "odkaz na detail (NEN/VVZ/profil)",
  "kind": "VZ | VZMR", "expired": false
}
```
Nová pole v1.1: `site_visit` (datum prohlídky místa plnění, je-li k dispozici),
`clarifications` (počet vysvětlení/doplnění ZD). Pole `changes` ve výstupu NENÍ
— historie změn je čistě klientská (viz níže).

**Detekce změn ZD — server, centrální archiv:** `main.py` při každém
denním běhu (8:00 Praha) porovná zakázky s předchozím `tenders.json`
v polích `TRACK_FIELDS` (lhůta, hodnota, prohlídka, vysvětlení ZD, název)
a rozdíly ukládá do samostatného `docs/data/changes.json`
(`{id: [{date, field, label, old, new}]}`, max. 20 záznamů/zakázku,
starší než 180 dní se mažou, záznamy zaniklých zakázek také). Archiv je
verzovaný v gitu ⇒ shodný a aktuální na všech zařízeních. Frontend jej
načítá a zobrazuje badge „změna ZD" (7 dní), filtr a rozklikávací
historii. Watchlist (★) zůstává výhradně v localStorage prohlížeče —
do repozitáře se NIKDY nezapisuje nic o zájmech uživatele.

**Plánování:** workflow má dva crony (6:00 a 7:00 UTC) a guard krok,
který pokračuje jen pokud je v Europe/Prague právě 8 h — běh je tak
v 8:00 celoročně bez ohledu na letní/zimní čas. Pozn.: GitHub plánované
workflow spouští best-effort, reálný start může být o minuty až desítky
minut opožděn.

`docs/data/meta.json`: `{"updated": ISO, "counts": {...}, "errors": [...]}`.

## Verze 2 (zatím NEIMPLEMENTOVAT)

E-mailový denní souhrn (SMTP secret), TED API, další kraje, export CSV,
párování na SoD v domistav-app.
