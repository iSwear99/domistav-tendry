"""Moje účasti — řízení, kterých se firma účastnila (pokyn 17. 11. 2026).

Zdroj: VEŘEJNÁ data — účastníci a výsledky z ISVZ (RVZ) a XML profilů
zadavatelů (`bidders`, `award` z fetch_isvz / fetch_profily). Firma se
pozná VÝHRADNĚ podle IČO (config.FIRMA_ICO), nikdy podle názvu.

Bez oborového i geo filtru (účast je fakt, ne doporučení) a s trvalou
retencí: archiv docs/data/ucasti.json jen roste, čerstvý záznam přepíše
starší, ale nikdy nesníží známý výsledek na „čeká". Ruční označení
„podal jsem nabídku" sem NEPATŘÍ — je to zájem uživatele a drží ho
výhradně prohlížeč / soukromý Gist (viz app.js).

Výstup: {"meta": {updated, count, vyhra, prohra, ceka},
         "ucasti": {id: {…záznam…, my, vysledek, winner, …}}}
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import unicodedata

import config

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _je_moje(b: dict) -> bool:
    return config.FIRMA_ICO in ([b.get("ico") or ""] + (b.get("icos") or []))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _zaznam(t: dict) -> dict | None:
    """Zakázka → záznam účasti, nebo None (firma v ní není)."""
    bidders = t.get("bidders") or []
    aw = t.get("award") or {}
    win_icos = [i.strip() for i in (aw.get("winner_ico") or "").split(";")]
    moje = next((b for b in bidders if _je_moje(b)), None)
    vyhra_dle_aw = config.FIRMA_ICO in win_icos
    if moje is None and not vyhra_dle_aw:
        return None
    if vyhra_dle_aw or (moje and moje.get("vybran")):
        vysledek = "vyhra"
    elif (aw.get("winner") or any(i for i in win_icos)
          or any(b.get("vybran") for b in bidders)):
        vysledek = "prohra"
    else:
        vysledek = "ceka"
    vitez = next((b for b in bidders if b.get("vybran")), None)
    return {
        "id": t["id"], "source": t["source"],
        "title": t["title"], "authority": t.get("authority") or "",
        "authority_ico": t.get("authority_ico") or "",
        "url": t.get("url") or "", "kind": t.get("kind") or "",
        "published": t.get("published") or "",
        "deadline": t.get("deadline") or "",
        "value": t.get("value"),
        "my": ({k: moje.get(k) for k in ("cena", "poradi", "vyloucen")}
               if moje else None),
        "bidders": bidders,
        "vysledek": vysledek,
        "winner": aw.get("winner") or (vitez or {}).get("nazev") or "",
        "winner_ico": aw.get("winner_ico") or (vitez or {}).get("ico") or "",
        "winner_cena": (vitez or {}).get("cena"),
        "price_contracted": aw.get("price_contracted"),
        "awarded": aw.get("date") or "",
        **({"registr_url": aw["registr_url"]} if aw.get("registr_url") else {}),
    }


_DOPLNIT = ("value", "deadline", "published", "url", "registr_url",
            "price_contracted", "awarded")


def _lepsi(a: dict, b: dict | None) -> dict:
    """Vítěz dle _skore; chybějící pole (předpokládaná hodnota bývá jen
    v některé verzi záznamu / v jiném zdroji) se doplní z poraženého."""
    if b is None:
        return a
    win, lose = (a, b) if _skore(a) >= _skore(b) else (b, a)
    for k in _DOPLNIT:
        if win.get(k) in (None, "") and lose.get(k) not in (None, ""):
            win[k] = lose[k]
    if not (win.get("my") or {}).get("cena") and (lose.get("my") or {}).get("cena"):
        win["my"] = {**(win.get("my") or {}), "cena": lose["my"]["cena"]}
    return win


def _skore(u: dict) -> tuple:
    return (u["vysledek"] != "ceka", bool(u.get("price_contracted")),
            len(u.get("bidders") or []), u["source"] == "isvz")


def extract(records: list[dict]) -> dict[str, dict]:
    """Účasti z dávky zakázek; tatáž zakázka z ISVZ i profilu (jiná id)
    se slučuje dle IČO zadavatele + normalizovaného názvu."""
    by_id: dict[str, dict] = {}
    for t in records:
        u = _zaznam(t)
        if u is None:
            continue
        # pozdější výskyt (novější měsíční export) vyhrává, ale nesmí
        # zahodit výsledek ani pole, která měl dřívější
        by_id[u["id"]] = _lepsi(u, by_id.get(u["id"]))
    return extract_merged(by_id)


def merge_save(fresh: dict[str, dict]) -> dict:
    """Sloučení s archivem a zápis; vrací meta. Trvalá retence."""
    path = ROOT / config.OUT_UCASTI
    try:
        prev = json.loads(path.read_text("utf-8")).get("ucasti", {})
    except Exception:  # noqa: BLE001
        prev = {}
    merged = dict(prev)
    for i, u in fresh.items():
        merged[i] = _lepsi(u, merged.get(i))
    # duplicita napříč zdroji, kterou archiv zná pod jiným id
    merged = extract_merged(merged)
    counts = {"vyhra": 0, "prohra": 0, "ceka": 0}
    for u in merged.values():
        counts[u["vysledek"]] += 1
    meta = {"updated": dt.datetime.now(dt.timezone.utc)
            .isoformat(timespec="seconds"), "count": len(merged), **counts}
    ordered = dict(sorted(
        merged.items(),
        key=lambda kv: kv[1].get("awarded") or kv[1].get("deadline")
        or kv[1].get("published") or "", reverse=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": meta, "ucasti": ordered},
                               ensure_ascii=False, indent=1), "utf-8")
    return meta


def extract_merged(entries: dict[str, dict]) -> dict[str, dict]:
    """Duplicita napříč zdroji: tatáž zakázka z profilu i z ISVZ (jiné id)
    se pozná dle IČO zadavatele + normalizovaného názvu a ponechá se ISVZ
    záznam (úplnější výsledek). Záznamy TÉHOŽ zdroje se neslučují — stejný
    název = zrušené a znovu vypsané řízení (případ AGAPÉ u zakázek)."""
    groups: dict[tuple, list[dict]] = {}
    for u in entries.values():
        k = ((u["authority_ico"], _norm(u["title"])) if u["authority_ico"]
             else (u["id"],))
        groups.setdefault(k, []).append(u)
    out: dict[str, dict] = {}
    for g in groups.values():
        isvz = [u for u in g if u["source"] == "isvz"]
        ostatni = [u for u in g if u["source"] != "isvz"]
        for u in (isvz or g):
            # profilový duplikát doplní, co ISVZ verze nemá (lhůta, odkaz)
            for o in (ostatni if isvz else []):
                for k in _DOPLNIT:
                    if u.get(k) in (None, "") and o.get(k) not in (None, ""):
                        u[k] = o[k]
            out[u["id"]] = u
    return out
