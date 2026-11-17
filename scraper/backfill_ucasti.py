"""Jednorázové naplnění Moje účasti z historie (pokyn 17. 11. 2026).

Denní běh vidí jen ~4 poslední měsíční exporty ISVZ; historické účasti
se proto jednou dohledají ze VŠECH exportů od 2/2024 (start JSON open
dat) a z archivů, které už v repozitáři jsou (competition.json,
tenders.json — VZMR z profilů). Opakované spuštění je bezpečné
(merge_save jen doplňuje / zpřesňuje). Spouštět jen z ČR/EU (ISVZ
blokuje mimoevropské IP) — běží pár desítek minut, stahuje ~1 GB.

    python -X utf8 scraper/backfill_ucasti.py [RRRR-MM]   (od, výchozí 2024-02)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config        # noqa: E402
import fetch_isvz    # noqa: E402
import ucasti        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _archivy() -> list[dict]:
    """Záznamy z archivů repozitáře převedené na tvar zakázky s award."""
    out: list[dict] = []
    for c in json.loads((ROOT / config.OUT_COMPETITION).read_text("utf-8")):
        out.append({**c, "award": {
            "date": c.get("awarded"), "winner": c.get("winner"),
            "winner_ico": c.get("winner_ico"),
            "price_contracted": None if c.get("estimated")
            else c.get("price_contracted")}})
    out.extend(json.loads((ROOT / config.OUT_TENDERS).read_text("utf-8")))
    return out


def _oprav_konkurenci(comp: dict[str, dict], recs: list[dict]) -> int:
    """Archiv Konkurence: doplnění vítěze (dřív se neuměl přeložit odkaz
    na účastníka — 39 % „vítěz neuveden") a nabídek účastníků. Jen
    chybějící pole, nic se nepřepisuje; denní běh vidí jen ~4 měsíce."""
    n = 0
    for t in recs:
        c = comp.get(t["id"])
        if c is None:
            continue
        aw = t.get("award") or {}
        zmena = False
        if not c.get("winner") and aw.get("winner"):
            c["winner"], c["winner_ico"] = aw["winner"], aw.get("winner_ico") or ""
            zmena = True
        if t.get("bidders"):     # doplní i vyloučení/odstoupení a důvody
            b = ucasti.slouc_bidders(c.get("bidders") or [], t["bidders"])
            if b != (c.get("bidders") or []):
                c["bidders"] = b
                zmena = True
        if c.get("value") is None and t.get("value") is not None:
            c["value"] = t["value"]          # poměr vítězná cena / předpokl.
            zmena = True
        if not c.get("registr_url") and aw.get("registr_url"):
            c["registr_url"] = aw["registr_url"]
            zmena = True
        n += zmena
    return n


def main(od: str) -> int:
    y, m = map(int, od.split("-"))
    today = dt.date.today()
    comp_path = ROOT / config.OUT_COMPETITION
    comp_list = json.loads(comp_path.read_text("utf-8"))
    comp = {c["id"]: c for c in comp_list}
    opraveno = 0
    nalezeno: dict[str, dict] = ucasti.extract(_archivy())
    print(f"archivy repozitáře: {len(nalezeno)} účastí", flush=True)
    while (y, m) <= (today.year, today.month):
        url = config.ISVZ_DOWNLOAD_URLS[0].format(year=y, month=m)
        # BACKFILL_CACHE=<složka>: už stažené VZ-MM-RRRR.zip (mimo repozitář)
        cached = pathlib.Path(os.environ.get("BACKFILL_CACHE", "-"),
                              f"VZ-{m:02d}-{y}.zip")
        try:
            raw = cached.read_bytes() if cached.is_file() else fetch_isvz._get(url)
            if raw is None:
                print(f"{y}-{m:02d}: export neexistuje", flush=True)
            else:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    name = next(n for n in zf.namelist()
                                if n.lower().endswith(".json"))
                    data = json.loads(zf.read(name).decode("utf-8"))["data"]
                del raw
                recs = [t for r in data if (t := fetch_isvz.normalize(r))]
                del data
                mesic = ucasti.extract(recs)
                opraveno += _oprav_konkurenci(comp, recs)
                del recs
                for i, u in mesic.items():
                    nalezeno[i] = ucasti._lepsi(u, nalezeno.get(i))
                print(f"{y}-{m:02d}: {len(mesic)} účastí "
                      f"(celkem {len(nalezeno)})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{y}-{m:02d}: CHYBA {exc}", flush=True)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    meta = ucasti.merge_save(nalezeno)
    if opraveno:
        import main as hlavni           # slučování duplicit Konkurence
        comp_list = sorted(hlavni._dedup_competition(comp_list),
                           key=lambda c: c["awarded"], reverse=True)
        comp_path.write_text(json.dumps(comp_list, ensure_ascii=False,
                                        indent=1), "utf-8")
    print(f"Konkurence: doplněno u {opraveno} záznamů "
          f"(bez vítěze zbývá {sum(1 for c in comp_list if not c['winner'])})")
    print("hotovo:", meta)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "2024-02"))
