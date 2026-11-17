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
    return (config.FIRMA_ICO in ([b.get("ico") or ""] + (b.get("icos") or []))
            or (b.get("nid") or "") in config.FIRMA_NIPEZ_IDS)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _profil(t: dict) -> str:
    """Adresa profilu zadavatele (podklad pro XML a dokumenty řízení)."""
    if t.get("authority_profile"):
        return t["authority_profile"]
    src = t.get("source") or ""
    meta = config.PROFILY_ZADAVATELU.get(src.removeprefix("profil:"))
    if meta and not meta.get("multi"):
        return meta.get("profile_url") or ""
    if src.startswith(("profil:sken-", "profil:pvu:")):
        return t.get("url") or ""
    return ""


def _zaznam(t: dict) -> dict | None:
    """Zakázka → záznam účasti, nebo None (firma v ní není)."""
    bidders = [dict(b) for b in t.get("bidders") or []]
    aw = t.get("award") or {}
    win_icos = [i.strip() for i in (aw.get("winner_ico") or "").split(";")]
    moje = next((b for b in bidders if _je_moje(b)), None)
    if moje is None and config.FIRMA_ICO not in win_icos:
        return None
    u = {
        "id": t["id"], "source": t["source"],
        "title": t["title"], "authority": t.get("authority") or "",
        "authority_ico": t.get("authority_ico") or "",
        "url": t.get("url") or "", "kind": t.get("kind") or "",
        "published": t.get("published") or "",
        "deadline": t.get("deadline") or "",
        "value": t.get("value"),
        "bidders": bidders,
        "winner": aw.get("winner") or "",
        "winner_ico": aw.get("winner_ico") or "",
        "price_contracted": aw.get("price_contracted"),
        "awarded": aw.get("date") or "",
        **({"registr_url": aw["registr_url"]} if aw.get("registr_url") else {}),
        **({"authority_profile": p} if (p := _profil(t)) else {}),
    }
    return _prepocti(u)


def _prepocti(u: dict) -> dict:
    """Odvozená pole (my, výsledek, vítěz) z bidders + award — volá se
    po každém sloučení, aby duplicita nikdy nezhoršila výsledek."""
    bidders = u["bidders"] = slouc_bidders(u.get("bidders") or [], [])
    moje = next((b for b in bidders if _je_moje(b)), None)
    win_icos = [i.strip() for i in (u.get("winner_ico") or "").split(";")]
    vitez = next((b for b in bidders if b.get("vybran")), None)
    if config.FIRMA_ICO in win_icos or (moje and moje.get("vybran")):
        vysledek = "vyhra"
    elif u.get("winner") or any(win_icos) or vitez:
        vysledek = "prohra"
    else:
        vysledek = "ceka"
    if u.get("vysledek") in ("vyhra", "prohra") and vysledek == "ceka":
        vysledek = u["vysledek"]        # známý výsledek se nikdy nesnižuje
    u["vysledek"] = vysledek
    u["my"] = ({k: moje.get(k) for k in ("cena", "poradi", "vyloucen",
                                         "odstoupil")} if moje else
               u.get("my"))
    if not u.get("winner") and vitez:
        u["winner"], u["winner_ico"] = vitez.get("nazev") or "", vitez.get("ico") or ""
    u["winner_cena"] = (vitez or {}).get("cena") or u.get("winner_cena")
    return u


_DOPLNIT = ("value", "deadline", "published", "url", "registr_url",
            "price_contracted", "awarded", "authority_profile", "winner",
            "winner_ico", "ceny_dok", "ceny_kontrola")
_BIDDER_POLE = ("cena", "poradi", "duvod", "cena_zdroj", "nid", "ico")


def _bkey(b: dict) -> set:
    keys = {i for i in [b.get("ico") or ""] + (b.get("icos") or []) if i}
    if b.get("nid"):
        keys.add("nid:" + b["nid"])
    return keys


def _same_bidder(a: dict, b: dict) -> bool:
    ka, kb = _bkey(a), _bkey(b)
    if ka & kb:
        return True
    ia = {k for k in ka if not k.startswith("nid:")}
    ib = {k for k in kb if not k.startswith("nid:")}
    if ia and ib:            # obě strany mají IČO a liší se
        return False
    if _je_moje(a) and _je_moje(b):
        return True
    # jedna strana jen s NEN id / bez IČO → shoda dle názvu bez právní formy
    na = re.sub(r"\b(spol|s r o|a s|sro|as)\b", " ", _norm(a.get("nazev") or ""))
    nb = re.sub(r"\b(spol|s r o|a s|sro|as)\b", " ", _norm(b.get("nazev") or ""))
    na, nb = na.replace(" ", ""), nb.replace(" ", "")
    return bool(na) and na == nb


def slouc_bidders(win: list[dict], lose: list[dict]) -> list[dict]:
    """Účastníci dvou verzí téže zakázky: chybějící údaje (cena, pořadí,
    důvod vyloučení…) se doplní, chybějící účastníci přidají — nic se
    neztratí a nikdo nebude dvakrát."""
    def ceny_ok(a: dict, b: dict) -> bool:   # jiná cena = jiná část zakázky
        ca, cb = a.get("cena"), b.get("cena")
        return ca is None or cb is None or abs(ca - cb) <= 0.005 * max(ca, cb)

    out: list[dict] = []
    for b in [*win, *lose]:     # i duplicity uvnitř jednoho seznamu
        m = next((a for a in out if _same_bidder(a, b) and ceny_ok(a, b)), None)
        if m is None:
            out.append(dict(b))
            continue
        for k in _BIDDER_POLE:
            if m.get(k) in (None, "") and b.get(k) not in (None, ""):
                m[k] = b[k]
        for k in ("vybran", "vyloucen", "odstoupil"):
            if b.get(k) and not m.get(k):
                m[k] = True
        if m.get("odstoupil"):
            m["vyloucen"] = False
    return out


def _lepsi(a: dict, b: dict | None) -> dict:
    """Vítěz dle _skore; chybějící pole (předpokládaná hodnota bývá jen
    v některé verzi záznamu / v jiném zdroji) i údaje účastníků se
    doplní z poraženého."""
    if b is None:
        return a
    win, lose = (a, b) if _skore(a) >= _skore(b) else (b, a)
    for k in _DOPLNIT:
        if win.get(k) in (None, "") and lose.get(k) not in (None, ""):
            win[k] = lose[k]
    win["bidders"] = slouc_bidders(win.get("bidders") or [],
                                   lose.get("bidders") or [])
    sl = set(win.get("slouceno") or []) | set(lose.get("slouceno") or [])
    if lose["id"] != win["id"]:
        sl.add(lose["id"])
    sl.discard(win["id"])
    if sl:
        win["slouceno"] = sorted(sl)
    if lose.get("vysledek") in ("vyhra", "prohra") and win.get("vysledek") == "ceka":
        win["vysledek"] = lose["vysledek"]
    return _prepocti(win)


def _skore(u: dict) -> tuple:
    b = u.get("bidders") or []
    return (u["vysledek"] != "ceka", bool(u.get("price_contracted")),
            sum(1 for x in b if x.get("cena") is not None), len(b),
            bool(u.get("deadline")), u["source"] == "isvz")


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
    for u in merged.values():
        _prepocti(u)            # i duplicitní účastníci uvnitř záznamu
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


def _datum(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _nazvy_shodne(a: str, b: str) -> bool:
    """Normalizované názvy shodné, nebo jeden je useknutý začátek druhého
    (ISVZ názvy zkracuje — „…stavební uprav" × „…stavební úpravy")."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    kratsi, delsi = sorted((a, b), key=len)
    return len(kratsi) >= 25 and delsi.startswith(kratsi)


def _tataz(a: dict, b: dict) -> bool:
    """Tatáž zakázka? Stejný zadavatel (IČO) + shodný název + nic, co by
    je odlišovalo. Pravidla jako dedup zakázek (CLAUDE.md): různé lhůty =
    zrušené a znovu vypsané řízení (případ AGAPÉ) ⇒ dvě účasti. Navíc se
    neslučuje při různých vítězích a vzdálených datech uzavření smlouvy."""
    if not a["authority_ico"] or a["authority_ico"] != b["authority_ico"]:
        return False
    da, db = (a.get("deadline") or "")[:10], (b.get("deadline") or "")[:10]
    if da and db and da != db:
        return False
    if not _nazvy_shodne(a["title"], b["title"]):
        # profil zadavatele občas název zkrátí vypuštěním slova („Snížení
        # náročnosti kuchyně…" × „Snížení energetické náročnosti kuchyně…")
        # — připustit jen při shodné lhůtě a když jsou slova kratšího
        # názvu podmnožinou delšího (části „stavební práce" × „technologie"
        # tak zůstanou oddělené)
        ta, tb = set(_norm(a["title"]).split()), set(_norm(b["title"]).split())
        kratsi, delsi = sorted((ta, tb), key=len)
        if not (da and da == db and len(kratsi) >= 5 and kratsi <= delsi):
            return False
    wa = {i for i in (a.get("winner_ico") or "").split(";") if i.strip()}
    wb = {i for i in (b.get("winner_ico") or "").split(";") if i.strip()}
    if wa and wb and not wa & wb:
        return False
    aa, ab = _datum(a.get("awarded")), _datum(b.get("awarded"))
    if aa and ab and abs((aa - ab).days) > 60:
        return False
    pa, pb = _datum(a.get("published")), _datum(b.get("published"))
    if pa and pb and abs((pa - pb).days) > 180:
        return False
    return True


def extract_merged(entries: dict[str, dict]) -> dict[str, dict]:
    """Slučování duplicit (pokyn 17. 11. 2026: „žádné duplicity, ale nic
    nesmí chybět"). Tatáž zakázka chodí z ISVZ i profilu zadavatele
    a v ISVZ i pod DVĚMA registrovými id (úplný záznam + torzo). Záznam
    se přidá do skupiny, jen když je kompatibilní se VŠEMI jejími členy
    (žádné řetězení přes torzo bez lhůty); ve skupině vyhrává informačně
    nejbohatší, ostatní do něj doplní pole i účastníky a jejich id se
    drží v `slouceno` (párování ručních 📨 dle id)."""
    groups: list[list[dict]] = []
    for u in sorted(entries.values(), key=_skore, reverse=True):
        g = next((g for g in groups if all(_tataz(u, o) for o in g)), None)
        if g is None:
            groups.append([u])
        else:
            g.append(u)
    out: dict[str, dict] = {}
    for g in groups:
        win = g[0]
        for o in g[1:]:
            win = _lepsi(win, o)
        out[win["id"]] = win
    return out
