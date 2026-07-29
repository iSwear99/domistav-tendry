"""Registr smluv (zákon č. 340/2015 Sb.) — dodatky a verifikace cen
pro záložku Konkurence.

Ověřeno 2026-07-28 proti dump_2026_06.xml a dump_2026_07.xml:
- měsíční dumpy https://data.smlouvy.gov.cz/dump_RRRR_MM.xml (XML bez
  komprese, ~110–150 MB; dump běžícího měsíce existuje a denně roste),
- namespace http://portal.gov.cz/rejstriky/ISRS/1.2/,
- zaznam > identifikator/{idSmlouvy, idVerze}, odkaz, platnyZaznam,
  smlouva/{subjekt/ico (zveřejňující), smluvniStrana*/ico, predmet,
  datumUzavreni, hodnotaBezDph, navazanyZaznam (u dodatku = idSmlouvy
  mateřské smlouvy)}.

ŽIVOTNÍ CYKLUS JE PLNĚ AUTOMATICKÝ — žádný ruční krok:
- prázdná/chybějící Konkurence  ⇒ klidný exit 0 (nic k párování),
- naplněná Konkurence + prázdné smlouvy.json ⇒ běh sám provede
  backfill SMLOUVY_BACKFILL_MONTHS měsíců (týdenní pojistný cron
  zaručí spuštění do 7 dnů od naplnění Konkurence),
- jinak přírůstkově: aktuální + minulý měsíc (pozdní publikace).
Toto chování při úpravách zachovej (viz KICKOFF/CLAUDE.md).

Párování (process): IČO zadavatele (subjekt) + IČO vítěze (smluvní
strana) + okno data uzavření vůči datu zadání; shoda ceny ±tolerance
⇒ confidence "high", jinak "low". Vazby "high" se NIKDY nesnižují.
Dodatky: navazanyZaznam → mateřská smlouva; suma hodnot dodatků je
orientační (zadavatelé někdy uvádějí hodnotu dodatku, jindy novou
celkovou cenu — v datech nelze rozlišit).

Použití: python scraper/fetch_smlouvy.py
Výstup: docs/data/smlouvy.json  {"meta": {...}, "links": {id: vazba}}
Surové dumpy se NIKDY necommitují (stahují se do tempu a mažou).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
NS = "{http://portal.gov.cz/rejstriky/ISRS/1.2/}"


# ── stažení a streamové čtení dumpu ─────────────────────────────────────────

def _download(url: str, dest: pathlib.Path) -> bool:
    """Stáhne dump po blocích; 404 (měsíc bez dumpu) vrací False."""
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=config.ISVZ_TIMEOUT) as r, \
                dest.open("wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(NS + tag)
    return (child.text or "").strip() if child is not None else ""


def iter_zaznamy(path: pathlib.Path):
    """Streamově vrací záznamy dumpu jako ploché dicty (peak ~1 MB RAM)."""
    with path.open("rb") as fh:
        for _, el in ET.iterparse(fh, events=["end"]):
            if el.tag != NS + "zaznam":
                continue
            ident = el.find(NS + "identifikator")
            sml = el.find(NS + "smlouva")
            if ident is None or sml is None:
                el.clear()
                continue
            subjekt = sml.find(NS + "subjekt")
            strany_el = sml.findall(NS + "smluvniStrana")
            strany = [(_text(s, "ico") or "").strip() for s in strany_el]
            try:
                hodnota = float(_text(sml, "hodnotaBezDph") or "nan")
            except ValueError:
                hodnota = float("nan")
            yield {
                "id": _text(ident, "idSmlouvy"),
                "verze": _text(ident, "idVerze"),
                "url": _text(el, "odkaz"),
                "platny": _text(el, "platnyZaznam") != "0",
                "ico_zadavatel": _text(subjekt, "ico") if subjekt is not None else "",
                "ico_strany": [i for i in strany if i],
                "nazvy_stran": [n for n in
                                (_text(s, "nazev") for s in strany_el) if n],
                "predmet": _text(sml, "predmet"),
                "datum": _text(sml, "datumUzavreni"),
                "hodnota": None if hodnota != hodnota else hodnota,
                "parent": _text(sml, "navazanyZaznam"),
            }
            el.clear()


# ── párování (čistá, testovatelná logika) ───────────────────────────────────

def process(competition: list[dict], zaznamy, prev_links: dict) -> dict:
    """Spáruje záznamy registru s Konkurencí; vrací nový slovník vazeb.

    `zaznamy` je iterátor dictů (viz iter_zaznamy) — může jít o víc dumpů
    zřetězených za sebou. `prev_links` jsou vazby z minulých běhů:
    confidence "high" se nikdy nesnižuje, dodatky se slučují podle id.
    """
    comp_by_authority: dict[str, list[dict]] = {}
    for c in competition:
        ico = (c.get("authority_ico") or "").strip()
        if ico and (c.get("winner_ico") or "").strip() and c.get("awarded"):
            comp_by_authority.setdefault(ico, []).append(c)

    links: dict[str, dict] = {k: json.loads(json.dumps(v))
                              for k, v in prev_links.items()}
    parent_to_comp: dict[str, str] = {
        str(v["contract_id"]): k for k, v in links.items()
    }

    before = dt.timedelta(days=config.SMLOUVY_DATE_BEFORE_DAYS)
    after = dt.timedelta(days=config.SMLOUVY_DATE_AFTER_DAYS)
    tol = config.SMLOUVY_PRICE_TOLERANCE

    # kandidáti smluv + dodatky se sbírají v jednom průchodu
    candidates: dict[str, list[dict]] = {}   # comp_id -> [zaznam]
    amendments: dict[str, list[dict]] = {}   # parent idSmlouvy -> [zaznam]

    for z in zaznamy:
        if not z["platny"]:
            continue
        if z["parent"]:
            amendments.setdefault(str(z["parent"]), []).append(z)
            continue
        comps = comp_by_authority.get(z["ico_zadavatel"])
        if not comps or not z["datum"]:
            continue
        try:
            d = dt.date.fromisoformat(z["datum"][:10])
        except ValueError:
            continue
        for c in comps:
            aw = dt.date.fromisoformat(c["awarded"])
            if not (aw - before <= d <= aw + after):
                continue
            if c["winner_ico"].split("; ")[0] not in z["ico_strany"] and not any(
                w in z["ico_strany"] for w in c["winner_ico"].split("; ")
            ):
                continue
            candidates.setdefault(c["id"], []).append(z)

    comp_by_id = {c["id"]: c for c in competition}

    for cid, cands in candidates.items():
        c = comp_by_id[cid]
        pc = c.get("price_contracted")

        def price_diff(z):
            if pc and z["hodnota"]:
                return abs(z["hodnota"] - pc) / pc
            return None

        # nejnovější verze téhož idSmlouvy vyhrává
        newest: dict[str, dict] = {}
        for z in cands:
            cur = newest.get(z["id"])
            if cur is None or z["verze"] > cur["verze"]:
                newest[z["id"]] = z
        uniq = list(newest.values())

        exact = [z for z in uniq
                 if price_diff(z) is not None and price_diff(z) <= tol]
        if len(exact) == 1:
            best, confidence = exact[0], "high"
        elif exact:
            best = min(exact, key=price_diff)
            confidence = "low"       # více cenových shod — nejednoznačné
        else:
            best = min(uniq, key=lambda z: (price_diff(z) is None,
                                            price_diff(z) or 0))
            confidence = "low"

        prev = links.get(cid)
        if prev and prev.get("confidence") == "high" and (
                str(prev.get("contract_id")) != best["id"] or confidence == "low"):
            continue  # „high" se nikdy nesnižuje ani nepřepisuje
        links[cid] = {
            "contract_id": best["id"],
            "url": best["url"],
            "date": best["datum"][:10],
            "price": best["hodnota"],
            "confidence": confidence,
            "amendments": (prev or {}).get("amendments", []),
        }
        parent_to_comp[best["id"]] = cid

    # Jedna smlouva registru smí u confidence "low" vázat jen JEDNU
    # zakázku — když je nejlepším kandidátem pro víc zakázek, ponechá se
    # nejbližší cenou (pak datem) a ostatní vazby se zahodí (žádná vazba
    # je lepší než jistě chybná). Vazby "high" se nechávají všechny.
    by_contract: dict[str, list[str]] = {}
    for cid, link in links.items():
        if link["confidence"] == "low":
            by_contract.setdefault(str(link["contract_id"]), []).append(cid)
    for contract_id, cids in by_contract.items():
        if len(cids) < 2:
            continue

        def badness(cid: str) -> tuple:
            c = comp_by_id.get(cid) or {}
            link = links[cid]
            pc, price = c.get("price_contracted"), link.get("price")
            pd = (abs(price - pc) / pc) if pc and price else 9e9
            dd = abs((dt.date.fromisoformat(link["date"])
                      - dt.date.fromisoformat(c["awarded"])).days) \
                if link.get("date") and c.get("awarded") else 9e9
            return (pd, dd)

        for cid in sorted(cids, key=badness)[1:]:
            del links[cid]

    # dodatky k napárovaným smlouvám (i z dřívějších běhů)
    for parent_id, adds in amendments.items():
        cid = parent_to_comp.get(parent_id)
        if not cid or cid not in links:
            continue
        link = links[cid]
        have = {a["id"]: a for a in link.get("amendments", [])}
        for z in adds:
            have[z["id"]] = {
                "id": z["id"], "date": z["datum"][:10],
                "value": z["hodnota"], "url": z["url"],
            }
        merged = sorted(have.values(), key=lambda a: a["date"] or "")
        link["amendments"] = merged[: config.SMLOUVY_MAX_AMENDMENTS]

    # dopočet součtů + úklid vazeb mimo aktuální Konkurenci
    valid_ids = set(comp_by_id)
    out: dict[str, dict] = {}
    for cid, link in links.items():
        if cid not in valid_ids:
            continue
        link["amendments_count"] = len(link.get("amendments", []))
        link["amendments_total"] = round(sum(
            a["value"] for a in link.get("amendments", []) if a["value"]
        ), 2)
        out[cid] = link
    return out


# ── orchestrace (plně automatická) ──────────────────────────────────────────

def _months_back(n: int) -> list[tuple[int, int]]:
    today = dt.date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _load(rel: str, default):
    path = ROOT / rel
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return default


def run() -> int:
    competition = _load(config.OUT_COMPETITION, [])
    if not competition:
        print("Konkurence je prázdná — nic k párování, končím (exit 0).")
        return 0

    state = _load(config.OUT_SMLOUVY, {})
    prev_links = state.get("links", {})
    backfill = not prev_links and not state.get("meta")
    months = _months_back(
        config.SMLOUVY_BACKFILL_MONTHS if backfill else 2
    )
    print(("BACKFILL " if backfill else "Aktualizace ")
          + f"{len(months)} měsíců: {months[0]}–{months[-1]}")

    links = prev_links
    errors: list[str] = []
    processed: list[str] = []
    for y, m in months:
        url = config.SMLOUVY_DUMP_URL.format(year=y, month=m)
        with tempfile.TemporaryDirectory() as td:
            dump = pathlib.Path(td) / "dump.xml"
            t0 = time.time()
            try:
                if not _download(url, dump):
                    print(f"  {y}-{m:02d}: dump neexistuje (404), přeskočeno")
                    continue
                links = process(competition, iter_zaznamy(dump), links)
                processed.append(f"{y}-{m:02d}")
                print(f"  {y}-{m:02d}: OK ({dump.stat().st_size / 1e6:.0f} MB, "
                      f"{time.time() - t0:.0f}s, vazeb {len(links)})")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"smlouvy {y}-{m:02d}: {exc}")
                print(f"  {y}-{m:02d}: CHYBA {exc}")

    if not processed and errors:
        print("Žádný dump se nepodařilo zpracovat — smlouvy.json NEPŘEPISUJI.",
              file=sys.stderr)
        return 1

    high = sum(1 for v in links.values() if v["confidence"] == "high")
    out = {
        "meta": {
            "updated": dt.datetime.now(dt.timezone.utc)
            .isoformat(timespec="seconds"),
            "months": processed,
            "backfill": backfill,
            "linked": len(links), "high": high, "low": len(links) - high,
            "errors": errors,
        },
        "links": links,
    }
    (ROOT / config.OUT_SMLOUVY).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), "utf-8"
    )
    print(f"Hotovo: {len(links)} vazeb (high {high}, "
          f"low {len(links) - high}), chyb {len(errors)}.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
