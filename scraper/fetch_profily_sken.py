"""Týdenní sken VŠECH profilů zadavatelů v okruhu (pokyn 19. 10. 2026).

Zaceluje poslední systémovou díru pokrytí: VZMR uveřejněné na profilu
NEkonfigurovaného zadavatele mimo systémy agregované v ISVZ (E-ZAK
instance, Gordion, e-zakazky.cz, profilyzadavatelu.cz…) nejsou jinde
k nalezení — přesně tudy dřív utekly zakázky Kalendova a Lodín.

Katalog profilů se buduje AUTOMATICKY z vlastního archivu: každý ISVZ
záznam nese adresu profilu zadavatele (authority_profile); bere se každý
profil zadavatele, který měl kdy zakázku v okruhu 50 km (k 19. 10. 2026
to je ~1 065 profilů, po vynechání NEN ~580). Katalog tedy roste sám
s každým novým zadavatelem v archivu — žádný ruční seznam.

Vynechává se:
- nen.nipez.cz (zakázky kryje ISVZ/RVZ, XMLdataVZ generuje pomalu),
- profily už v config.PROFILY_ZADAVATELU (skenují se denně).

Záznamy nesou source "profil:sken-<ičo>" a NEDOSTÁVAJÍ benevolenci
konfigurovaných profilů (obor/NUTS rozhodují CPV a klíčová slova,
stejně jako u PVU) — katalog je široký a víceoborový.

Spouštění: main.py s env SKEN_PROFILU=1 (workflow update-profily-sken,
denní cron + hlídka stáří v docs/data/sken_meta.json ⇒ efektivně běží
při prvním zapnutí PC po ≥6 dnech). Env SKEN_MAX omezí počet profilů
(testování).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time
from urllib.parse import urlparse

import config
import fetch_profily

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_META = ROOT / config.OUT_DIR / "sken_meta.json"

VYNECHAT_HOSTY = ("nen.nipez.cz",)
PREKRYV_DNU = 7          # překryv okna přes minulý sken (pozdní uveřejnění)
MAX_OKNO_DNU = 90        # strop okna (první běh / dlouhá pauza)
PAUZA_S = 0.7            # šetrnost k platformám (Tender arena vrací 429)


def _katalog() -> list[dict]:
    try:
        tenders = json.loads((ROOT / config.OUT_TENDERS).read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    konfig = {m["profile_url"].rstrip("/")
              for m in config.PROFILY_ZADAVATELU.values()}
    kat: dict[str, dict] = {}
    for x in tenders:
        url = (x.get("authority_profile") or "").rstrip("/")
        if not url.startswith("http"):
            continue
        if x.get("dist_km") is None or x["dist_km"] > config.GEO_RADIUS_KM:
            continue
        host = urlparse(url).netloc.removeprefix("www.")
        if url in konfig or any(h in host for h in VYNECHAT_HOSTY):
            continue
        kat.setdefault(url, {
            "nazev": x.get("authority", ""),
            "ico": x.get("authority_ico", ""),
            "profile_url": url,
        })
    return sorted(kat.values(), key=lambda m: m["profile_url"])


def _okno_dnu() -> int:
    try:
        meta = json.loads(OUT_META.read_text("utf-8"))
        posledni = dt.date.fromisoformat(meta["updated"][:10])
        return min((dt.date.today() - posledni).days + PREKRYV_DNU,
                   MAX_OKNO_DNU)
    except Exception:  # noqa: BLE001
        return MAX_OKNO_DNU


def fetch() -> tuple[list[dict], list[str]]:
    katalog = _katalog()
    cap = os.environ.get("SKEN_MAX")
    if cap:
        katalog = katalog[: int(cap)]
    dny = _okno_dnu()
    tenders: list[dict] = []
    chyb = 0
    prvni_chyby: list[str] = []
    for i, meta in enumerate(katalog, 1):
        key = f"sken-{meta['ico'] or 'x'}"
        try:
            t, e = fetch_profily._fetch_profile(key, meta, days_back=dny)
            tenders.extend(t)
            if e:
                chyb += 1
                if len(prvni_chyby) < 5:
                    prvni_chyby.extend(e[:1])
        except Exception as exc:  # noqa: BLE001
            chyb += 1
            if len(prvni_chyby) < 5:
                prvni_chyby.append(f"sken {meta['profile_url']}: {exc}")
        if i % 100 == 0:
            print(f"sken profilů: {i}/{len(katalog)}, "
                  f"zakázek {len(tenders)}, chyb {chyb}")
        time.sleep(PAUZA_S)
    OUT_META.parent.mkdir(parents=True, exist_ok=True)
    OUT_META.write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds"),
        "profilu": len(katalog), "zakazek": len(tenders),
        "okno_dnu": dny, "chyb_profilu": chyb,
    }, ensure_ascii=False, indent=1), "utf-8")
    print(f"sken profilů HOTOVO: {len(katalog)} profilů, "
          f"{len(tenders)} zakázek, {chyb} nedostupných profilů")
    # nedostupné profily nejsou chyba běhu (mrtvé adresy v katalogu jsou
    # očekávané) — do meta jde jen souhrn, ať nezaplaví chybovou lištu
    errors = ([f"sken: {chyb}/{len(katalog)} profilů nedostupných "
               f"(např. {prvni_chyby[0]})"] if chyb > len(katalog) // 2
              else [])
    return tenders, errors
