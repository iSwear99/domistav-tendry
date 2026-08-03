"""DOMISTAV Tendry — hlavní běh scraperu.

Použití:
    python scraper/main.py            # ostrý běh, zapíše docs/data/*.json
    python scraper/main.py --dry-run  # bez zápisu, jen report na stdout

Zásady (viz CLAUDE.md):
- idempotence (dedup dle id),
- nikdy nepřepsat platná data podezřele malým výsledkem (MIN_RESULT_RATIO),
- chyby zdrojů se propisují do meta.json, běh pokračuje.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import os
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config            # noqa: E402
import re                # noqa: E402
import fetch_isvz        # noqa: E402
import fetch_pvu         # noqa: E402
import fetch_nen         # noqa: E402
import fetch_profily     # noqa: E402
import geo               # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _norm(s: str) -> str:
    """Malá písmena bez diakritiky pro porovnávání klíčových slov."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


_KW_POS = None
_KW_NEG = None


def _kw_match(title: str) -> bool:
    """Pozitivní klíčové slovo v názvu a žádné negativní.

    Porovnává se na začátku slova (před klíčovým slovem musí být hranice) —
    jinak by kmen „oprav" chytil i „dopravce/doprava" apod."""
    global _KW_POS, _KW_NEG
    if _KW_POS is None:
        _KW_POS = sorted({" " + _norm(k).lstrip() for k in config.KEYWORDS_POSITIVE})
        _KW_NEG = sorted({" " + _norm(k).lstrip() for k in config.KEYWORDS_NEGATIVE})
    # interpunkce → mezery, aby hranici slova tvořila i závorka či pomlčka
    t = " " + "".join(c if c.isalnum() else " " for c in _norm(title)) + " "
    if any(k in t for k in _KW_NEG):
        return False
    return any(k in t for k in _KW_POS)


def _kw_negative(title: str) -> bool:
    """Název obsahuje negativní klíčové slovo (stejné hranice slov)."""
    _kw_match("")   # inicializace seznamů
    t = " " + "".join(c if c.isalnum() else " " for c in _norm(title)) + " "
    return any(k in t for k in _KW_NEG)


# agenturní profily ("multi": True) hostí cizí zadavatele z celé ČR —
# benevolence konfigurovaných profilů (obor/NUTS) jim nepatří, viz config
_MULTI_SOURCES = {f"profil:{k}"
                  for k, m in config.PROFILY_ZADAVATELU.items()
                  if m.get("multi")}


def _from_configured_profile(t: dict) -> bool:
    s = t["source"]
    return (s.startswith("profil:")
            and not s.startswith("profil:pvu:")
            and s not in _MULTI_SOURCES)


def _sector_ok(t: dict) -> bool:
    """Obor: (a) CPV prefix 45 — autoritativní, negativní slova nepřebíjí;
    (b) záchytná síť: klíčová slova v názvu (příznak kw_match);
    (c) záznam Z KONFIGUROVANÉHO profilu bez CPV — zdroj je předvybraný,
    ponechat. PVU RSS (profil:pvu:*) a agenturní multi profily jsou
    celostátní a víceoborové, proto benevolenci (c) nedostávají — bez
    CPV rozhodují jen klíčová slova, stejně jako u ISVZ.
    Nastavuje t["kw_match"]."""
    from_profile = _from_configured_profile(t)
    cpv = t.get("cpv") or []
    # CPV 45 platí, jen pokud kód nespadá do vyřazených podskupin
    # (dopravní infrastruktura) — takové zakázky rozhodují klíčová slova
    cpv_ok = any(
        c.startswith(tuple(config.CPV_PREFIXES))
        and not c.startswith(tuple(config.CPV_NEGATIVE_PREFIXES))
        for c in cpv
    )
    # Negativní slovo v názvu přebíjí OBECNÉ CPV 45000000 (pokyn 31. 7.):
    # „Kanalizace obce X" s CPV 45000000 se vyřadí; zakázka s konkrétním
    # stavebním CPV (např. 45214200 školy) zůstává i s negativním slovem.
    if cpv_ok and _kw_negative(t.get("title", "")):
        cpv_ok = any(
            c.startswith(tuple(config.CPV_PREFIXES))
            and not c.startswith(tuple(config.CPV_NEGATIVE_PREFIXES))
            and not c.startswith("450000")
            for c in cpv
        )
    t["kw_match"] = False
    if cpv_ok:
        return True
    if _kw_match(t.get("title", "")):
        t["kw_match"] = True
        return True
    return from_profile and not cpv


def _nuts_ok(t: dict) -> bool:
    """Hrubý předfiltr krajů; nastavuje t["no_nuts"]."""
    from_profile = _from_configured_profile(t)
    nuts = t.get("nuts") or []
    if nuts:
        if not any(n[:5] in config.NUTS_ALLOWED for n in nuts):
            return False
        t["no_nuts"] = False
        return True
    if not from_profile and not config.KEEP_MISSING_NUTS:
        return False
    t["no_nuts"] = not from_profile
    return True


def _passes_filters(t: dict) -> bool:
    if not _sector_ok(t) or not _nuts_ok(t):
        return False
    # hodnota (jen záložka Zakázky — Konkurence cenový strop nemá)
    val = t.get("value")
    if val is None:
        if not config.KEEP_MISSING_VALUE:
            return False
        t["no_value"] = True
    else:
        t["no_value"] = False
        if val < config.MIN_VALUE_CZK:
            return False
    return True


_DONE_STATES = ("dokoncen", "zrusen", "ukoncen", "zadan")


def _mark_expired(t: dict, today: str) -> dict:
    """Expirace: prošlá lhůta, nebo ukončený stav zadávacího postupu
    („dokončen/zadán", „ukončeno plnění", „zrušen"). startswith záměrně —
    stav „neukončen" expirovaný není."""
    d = (t.get("deadline") or "")[:10]
    state = _norm(t.get("state") or "")
    t["expired"] = (bool(d) and d < today) or state.startswith(_DONE_STATES)
    return t


_SPA_HOSTS = ("nen.nipez.cz", "vvz.nipez.cz")


def _check_dead_links(tenders: list[dict], prev_by_id: dict) -> None:
    """Eliminace zakázek s neexistující stránkou detailu (pokyn 29. 7.).

    Kontrolují se jen AKTIVNÍ záznamy. Za mrtvý odkaz se považuje POUZE
    tvrdé HTTP 404/410; síťové chyby a timeouty se nepočítají. NEN a VVZ
    se NEKONTROLUJÍ VŮBEC: od 8/2026 vracejí HTTP 404 se SPA skořápkou
    i na ŽIVÉ detaily (dřív 200 na všechno) — stavový kód tam není
    verdikt; historické příznaky z nich se čistí a expirace z nich
    plynoucí se vrací (2 falešně mrtvé živé VZ zjištěny 3. 8. 2026).
    Expirace až po DVOU mrtvých bězích po sobě (čítač url_dead se
    přenáší z předchozího snapshotu) — jednorázový výpadek profilu tak
    zakázku nevyřadí. Vyřazené zůstávají v historii s příznakem
    link_dead (štítek v UI)."""
    import concurrent.futures
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    # samoléčba falešných verdiktů ze SPA portálů (i u přenášených)
    today = dt.date.today().isoformat()
    for t in tenders:
        host = urlparse(t.get("url") or "").netloc
        if host in _SPA_HOSTS and (t.get("url_dead") or t.get("link_dead")):
            was_link_dead = t.pop("link_dead", False)
            t.pop("url_dead", None)
            if was_link_dead and not t.get("auto_expired"):
                _mark_expired(t, today)

    def status(url: str) -> int | None:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": config.USER_AGENT}, method="HEAD"
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            if exc.code == 405:  # HEAD zakázán — zkusit GET
                try:
                    g = urllib.request.Request(
                        url, headers={"User-Agent": config.USER_AGENT})
                    with urllib.request.urlopen(g, timeout=30) as r:
                        return r.status
                except urllib.error.HTTPError as exc2:
                    return exc2.code
                except Exception:  # noqa: BLE001
                    return None
            return exc.code
        except Exception:  # noqa: BLE001
            return None    # síť/timeout — žádný verdikt

    active = [t for t in tenders if not t["expired"] and t.get("url")
              and urlparse(t["url"]).netloc not in _SPA_HOSTS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        codes = list(ex.map(lambda t: status(t["url"]), active))
    for t, code in zip(active, codes):
        prev_dead = int((prev_by_id.get(t["id"]) or {}).get("url_dead", 0))
        if code in (404, 410):
            t["url_dead"] = prev_dead + 1
            if t["url_dead"] >= 2:
                t["expired"] = True
                t["link_dead"] = True
        elif code is not None:
            pass          # stránka žije — čítač se nepřenáší (reset)
        elif prev_dead:
            t["url_dead"] = prev_dead   # bez verdiktu čítač jen držet


def _nen_embedded(url: str) -> dict | None:
    """Vytěží vložená data z NEN detailu (server-rendered JSON stav).
    Vrací klíče podaniLhuta / datumZruseni / datumUkonceni / stavZP /
    predpokladHodnota; None při síťové chybě (= žádný verdikt)."""
    import urllib.parse
    import urllib.request
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": config.USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    dec = urllib.parse.unquote(page)
    out = {}
    for k in ("podaniLhuta", "datumZruseni", "datumUkonceni", "stavZP",
              "predpokladHodnota"):
        m = re.search(rf'"{k}":(?:"([^"]*)"|(null)|([\d.]+))', dec)
        if m:
            out[k] = m.group(1) if m.group(1) is not None else (
                None if m.group(2) else m.group(3))
    return out


def _verify_no_deadline(result: list[dict], errors: list[str]) -> None:
    """Tvrdý úklid registrových torz + ZPĚTNÁ KONTROLA (pokyn 31. 7. 2026).

    BEZPEČNOSTNÍ INVARIANTY — aktivní zakázka NESMÍ omylem propadnout:
    1. Sahá se výhradně na ISVZ záznamy BEZ lhůty a BEZ stavu; na nic
       jiného se tento úklid nikdy nevztahuje.
    2. Před expirací se záznam ověřuje u zdroje: NEN detail (vložená
       data) nebo XML profilu zadavatele podle názvu. Nalezené živé se
       OBOHATÍ o lhůtu/stav/hodnotu místo expirace.
    3. Bez verdiktu (síťová chyba, profil neodpovídá) se NIC nemění.
    4. Expirace je VRATNÁ: auto_expired nese datum a záznam se 21 dní
       denně znovu ověřuje — při nálezu živých dat se vrací mezi
       aktivní. Čerstvý výskyt v libovolném zdroji má vždy přednost
       (dedup podle ID upřednostňuje čerstvé záznamy).
    """
    today = dt.date.today()
    prof_cache: dict[str, dict | None] = {}

    def profile_lookup(profile_url: str, title: str):
        """(nalezený záznam | None, profil odpověděl?)"""
        if profile_url not in prof_cache:
            try:
                recs, errs = fetch_profily._fetch_profile(
                    "overeni", {"nazev": "", "ico": "",
                                "profile_url": profile_url})
                prof_cache[profile_url] = None if errs else {
                    _norm_title(r["title"]): r for r in recs}
            except Exception:  # noqa: BLE001
                prof_cache[profile_url] = None
        mapa = prof_cache[profile_url]
        if mapa is None:
            return None, False
        return mapa.get(_norm_title(title)), True

    stats = {"enriched": 0, "expired": 0, "revived": 0, "checked": 0}
    for t in result:
        if t.get("deadline") or t.get("state") or t["source"] != "isvz":
            continue
        ae = t.get("auto_expired")
        if t["expired"] and not ae:
            continue                      # expiroval jinou cestou
        if ae and (today - dt.date.fromisoformat(ae)).days > 21:
            continue                      # zpětná kontrola skončila
        stats["checked"] += 1
        url = t.get("url") or ""
        live: dict | None = None
        dead = False
        if "nen.nipez.cz/verejne-zakazky/detail-zakazky" in url:
            d = _nen_embedded(url)
            if d is None:
                continue                  # bez verdiktu — neměnit nic
            if d.get("datumZruseni") or d.get("datumUkonceni") \
                    or (d.get("stavZP") or "neukoncena") != "neukoncena":
                dead = True
                t["state"] = ("zrušeno (NEN)" if d.get("datumZruseni")
                              else "ukončeno (NEN)")
            else:
                live = {}
                if d.get("podaniLhuta"):
                    live["deadline"] = d["podaniLhuta"][:19]
                if d.get("predpokladHodnota") and t.get("value") is None:
                    try:
                        live["value"] = float(d["predpokladHodnota"])
                        t["no_value"] = False
                    except ValueError:
                        pass
        elif url.startswith("http"):
            rec, odpovedel = profile_lookup(url, t["title"])
            if rec:
                if rec.get("deadline") \
                        and rec["deadline"][:10] >= today.isoformat():
                    live = {"deadline": rec["deadline"],
                            "state": rec.get("state") or ""}
                else:
                    dead = True
                    t["state"] = rec.get("state") or ""
            elif odpovedel:
                dead = True               # profil běží a zakázku nezná
            else:
                continue                  # profil nedostupný — bez verdiktu
        else:
            dead = True                   # není kde ověřit: bez lhůty,
                                          # bez stavu, bez jakéhokoli odkazu
        if live is not None:
            t.update(live)
            if ae:
                t.pop("auto_expired", None)
                t.pop("no_activity", None)
                stats["revived"] += 1
            _mark_expired(t, today.isoformat())
            stats["enriched"] += 1
        elif dead:
            if not t["expired"]:
                stats["expired"] += 1
            t["expired"] = True
            t.setdefault("auto_expired", today.isoformat())
            t["no_activity"] = True
    print(f"úklid torz: ověřeno {stats['checked']}, obohaceno "
          f"{stats['enriched']}, expirováno {stats['expired']}, "
          f"oživeno {stats['revived']}")


def _norm_title(s: str) -> str:
    """Normalizace názvu pro porovnání napříč zdroji: malá písmena,
    bez diakritiky, bez interpunkce, sjednocené mezery."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = "".join(c if c.isalnum() else " " for c in s)
    return " ".join(s.split())


def _info_score(t: dict) -> int:
    """Informační bohatost záznamu. Lhůta a předpokládaná hodnota váží
    dvojnásobně (pokyn 2. 8. 2026: při duplicitě má přednost záznam,
    který je nese)."""
    s = 2 * bool(t.get("deadline")) + 2 * (t.get("value") is not None)
    return s + sum(1 for f in ("cpv", "place", "state", "published",
                               "url", "nuts", "site_visit")
                   if t.get(f))


_MERGE_FILL = ("deadline", "value", "url", "published", "place", "state",
               "site_visit", "cpv", "nuts", "authority_seat", "bidders")


def dedup_cross_source(tenders: list[dict],
                       today: str) -> tuple[list[dict], set[str]]:
    """Stejná zakázka přichází z více zdrojů (ISVZ ↔ profil) i pod dvěma
    registrovými id (ISVZ agreguje NEN/VVZ/Tender arenu — totéž řízení
    mívá dva RVZ záznamy, jeden s hodnotou a lhůtou, druhý torzo).

    Klíč shody: IČO zadavatele + normalizovaný název; bez IČO se
    deduplikace neprovádí (riziko falešné shody). Kandidáti se slučují
    JEN při kompatibilní lhůtě (shodné datum, nebo jedna chybí) — dvě
    řízení s různými lhůtami jsou zrušený a znovu vypsaný tendr a
    zůstávají oddělená (případ AGAPÉ, 2026-07-29). Vyhrává záznam
    s VÍCE informacemi (_info_score: především lhůta a předpokládaná
    hodnota; při shodě přednost ISVZ), chybějící pole se doplní
    z poraženého a URL profilu se zachová v `profile_url`.

    Vrací (ponechané, id sloučených pryč) — sloučená id potřebuje
    main.py, aby je trvalá retence neoživila ze starého snapshotu.
    """
    def key(t):
        ico = (t.get("authority_ico") or "").strip()
        return (ico, _norm_title(t.get("title", ""))) if ico else None

    def compatible(a, b):
        da, db = (a.get("deadline") or "")[:10], (b.get("deadline") or "")[:10]
        if da and db and da == db:
            return True
        # Jinak se smí sloučit jen dvojice ŽIVÝCH záznamů: různá data
        # lhůt mezi dvěma živými = prodloužení lhůty (zdroje se liší
        # aktuálností; bere se pozdější), chybějící lhůta u živého
        # torza = tentýž běžící tendr. S EXPIROVANÝM záznamem se
        # neslučuje nikdy — živé torzo se nesmí schovat do starého
        # uzavřeného řízení téhož názvu (zjištěno 3. 8. 2026) a
        # zrušený pokus nesmí přemazat běžící (případ AGAPÉ).
        return not a.get("expired") and not b.get("expired")

    def rank(t):
        return (_info_score(t), 1 if t["source"] == "isvz" else 0)

    groups: dict[tuple, list[dict]] = {}
    out: list[dict] = []
    merged_away: set[str] = set()
    for t in tenders:
        k = key(t)
        if k is None or not k[1]:
            out.append(t)
            continue
        bucket = groups.setdefault(k, [])
        for i, cur in enumerate(bucket):
            if not compatible(cur, t):
                continue
            winner, loser = (t, cur) if rank(t) > rank(cur) else (cur, t)
            for f in _MERGE_FILL:
                if not winner.get(f) and loser.get(f):
                    winner[f] = loser[f]
            # prodloužení lhůty: mezi dvěma živými platí pozdější termín
            dl_w, dl_l = winner.get("deadline") or "", loser.get("deadline") or ""
            if dl_w and dl_l and dl_l > dl_w:
                winner["deadline"] = dl_l
            if loser["source"].startswith("profil:") and loser.get("url"):
                winner.setdefault("profile_url", loser["url"])
            merged_away.add(loser["id"])
            bucket[i] = _mark_expired(winner, today)
            break
        else:
            bucket.append(t)    # neslučitelné lhůty ⇒ jiné řízení
    for bucket in groups.values():
        out.extend(bucket)
    return out, merged_away


def build_competition(all_t: list[dict], today: str) -> list[dict]:
    """Konkurence: zadané zakázky v oboru a okruhu, klouzavých 12 měsíců
    dle data uzavření smlouvy, BEZ cenového stropu.

    Sloučí se s předchozím competition.json — starší měsíce už v ISVZ
    exportech nejsou (stahují se ~4 zpět), archiv je drží; novější běh
    záznam přepíše (uhrazené ceny přibývají průběžně). Mimo okno se
    záznamy odmazávají (jediné povolené mazání — klouzavé okno dle
    zadání). Sporné polohy se ponechávají s příznakem loc_unknown."""
    cutoff = (dt.date.fromisoformat(today)
              - dt.timedelta(days=config.COMPETITION_WINDOW_DAYS)).isoformat()

    fresh: dict[str, dict] = {}
    for t in all_t:
        aw = t.get("award")
        if not aw or not aw.get("date") or aw["date"] < cutoff:
            continue
        t = dict(t)
        if not _sector_ok(t) or not _nuts_ok(t):
            continue
        fresh[t["id"]] = t

    located, _ = geo.apply_radius(list(fresh.values()))

    def flat(t: dict) -> dict:
        aw = t["award"]
        contracted = aw.get("price_contracted")
        estimated = False
        if contracted is None and t.get("value") is not None:
            contracted = t["value"]     # fallback: předpokládaná hodnota
            estimated = True
        paid = aw.get("price_paid")
        growth = (round((paid / contracted - 1) * 100, 1)
                  if paid and contracted else None)
        return {
            "id": t["id"], "source": t["source"],
            "title": t["title"], "authority": t["authority"],
            "authority_ico": t["authority_ico"],
            "winner": aw.get("winner") or "",
            "winner_ico": aw.get("winner_ico") or "",
            "awarded": aw["date"],
            "price_contracted": contracted, "estimated": estimated,
            "price_paid": paid, "growth_pct": growth,
            "dist_km": t.get("dist_km"), "loc_unknown": t.get("loc_unknown"),
            "cpv": t.get("cpv") or [], "kind": t["kind"],
            "kw_match": t.get("kw_match", False),
            "url": t.get("url") or "",
            **({"bidders": t["bidders"]} if t.get("bidders") else {}),
            **({"registr_url": aw["registr_url"]}
               if aw.get("registr_url") else {}),
        }

    # Dřívější záznamy archivu se znovu prohánějí oborovým filtrem —
    # zpřísnění konfigurace (např. vyřazení silnic) pročistí i historii.
    # Od 29. 7. 2026 se okno NEMAŽE (trvalá retence jako u zakázek) —
    # historie konkurence se hromadí a UI ji řeže filtrem od–do.
    merged = {c["id"]: c for c in _load_json(config.OUT_COMPETITION, [])
              if _sector_ok(c)}
    for t in located:
        merged[t["id"]] = flat(t)
    return sorted(merged.values(), key=lambda c: c["awarded"], reverse=True)


def _prev_count() -> int:
    path = ROOT / config.OUT_TENDERS
    if not path.exists():
        return 0
    try:
        return len(json.loads(path.read_text("utf-8")))
    except Exception:  # noqa: BLE001
        return 0


def _load_json(rel: str, default):
    path = ROOT / rel
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _fmt(field: str, val) -> str:
    if val in (None, "") or (val == 0 and field != "clarifications"):
        return "—"
    if field == "value":
        return f"{val:,.0f} Kč".replace(",", " ")
    if field in ("deadline", "site_visit"):
        return str(val)[:16].replace("T", " ")
    return str(val)


def compute_changes(result: list[dict], today: str) -> dict[str, list]:
    """Diff proti předchozímu snapshotu → docs/data/changes.json.

    Archiv {id: [{date, field, label, old, new}, …]} je verzovaný v gitu,
    tedy shodný na všech zařízeních. Zachytí mj. posun lhůty a nárůst počtu
    vysvětlení ZD (typické projevy doplnění zadávací dokumentace).
    """
    prev_tenders = {t["id"]: t for t in _load_json(config.OUT_TENDERS, [])}
    changes: dict[str, list] = _load_json(config.OUT_CHANGES, {})
    current_ids = {t["id"] for t in result}

    # úklid: zakázky mimo výstup a příliš staré záznamy
    cutoff = (dt.date.fromisoformat(today)
              - dt.timedelta(days=config.CHANGES_MAX_AGE_DAYS)).isoformat()
    changes = {
        tid: kept for tid, entries in changes.items()
        if tid in current_ids
        and (kept := [c for c in entries if c["date"] >= cutoff])
    }

    for t in result:
        prev = prev_tenders.get(t["id"])
        if not prev:
            continue
        for field, label in config.TRACK_FIELDS.items():
            old, new = prev.get(field), t.get(field)
            if old in (None, "", 0) and new in (None, "", 0):
                continue
            if old != new:
                changes.setdefault(t["id"], []).append({
                    "date": today, "field": field, "label": label,
                    "old": _fmt(field, old), "new": _fmt(field, new),
                })
                changes[t["id"]] = changes[t["id"]][-config.CHANGES_KEEP:]
    return changes


def run(dry_run: bool = False) -> int:
    today = dt.date.today().isoformat()
    all_t: list[dict] = []
    errors: list[str] = []

    source_counts: dict[str, int] = {}
    # SKIP_ISVZ=1: cloudový běh (GitHub-hosted runner, USA) — ISVZ blokuje
    # mimoevropské IP; jeho záznamy drží retence a doplní je běh na PC
    skip_isvz = bool(os.environ.get("SKIP_ISVZ"))
    for name, mod in (("isvz", fetch_isvz), ("profily", fetch_profily),
                      ("pvu", fetch_pvu)):
        if name == "isvz" and skip_isvz:
            source_counts[name] = 0
            continue
        try:
            t, e = mod.fetch()
            all_t.extend(t)
            errors.extend(e)
            source_counts[name] = len(t)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: neočekávaná chyba: {exc}")
            source_counts[name] = 0

    # Výpadek celého zdroje (např. ISVZ blokuje IP GitHub runnerů) kryje
    # TRVALÁ RETENCE níže (carried) — záznamy se přenášejí se zachovanými
    # příznaky (expired/auto_expired). Dřívější větev, která je vracela
    # do čerstvé pipeline, omylem oživovala expirovaná torza přepočtem
    # _mark_expired (zjištěno 31. 7. 2026 na cloudovém běhu bez ISVZ).
    prev_snapshot = _load_json(config.OUT_TENDERS, [])
    for name in ("isvz", "profily"):
        if source_counts.get(name) == 0 \
                and not (name == "isvz" and skip_isvz):
            errors.append(f"{name}: zdroj nedostupný — záznamy drží "
                          "trvalá retence.")

    # filtrace + dedup dle ID. NIPEZ id (rvz:…) sdílí ISVZ i NEN profily —
    # při kolizi vyhrává úplnější záznam z ISVZ, u shodného zdroje poslední
    # výskyt (novější měsíční export); URL profilu se zachová.
    prev_count = _prev_count()
    filtered: dict[str, dict] = {}
    for t in all_t:
        if not _passes_filters(t):
            continue
        t = _mark_expired(t, today)
        cur = filtered.get(t["id"])
        if cur is not None:
            rank = lambda x: 1 if x["source"] == "isvz" else 0  # noqa: E731
            winner, loser = (t, cur) if rank(t) >= rank(cur) else (cur, t)
            if loser["source"].startswith("profil:") and loser.get("url"):
                winner.setdefault("profile_url", loser["url"])
            # doplnění chybějících polí z duplikátu (profil XML mívá lhůtu
            # a odkaz i tam, kde je ISVZ zatím nemá) + přepočet expirace
            for f in ("deadline", "url", "published", "place"):
                if not winner.get(f) and loser.get(f):
                    winner[f] = loser[f]
            winner = _mark_expired(winner, today)
            filtered[t["id"]] = winner
        else:
            filtered[t["id"]] = t
    # poslední záchrana prokliku: profil zadavatele dle IČO — z konfigurace
    # a z adres profilů viděných u JINÝCH záznamů téhož zadavatele v běhu
    # (VVZ záznamy v open datech často nemají odkaz ani adresu profilu)
    ico_to_profile = {
        m["ico"]: m["profile_url"] for m in config.PROFILY_ZADAVATELU.values()
    }
    for t in all_t:
        ico, prof = t.get("authority_ico"), t.get("authority_profile")
        if ico and prof:
            ico_to_profile.setdefault(ico, prof)
    for t in filtered.values():
        if not t.get("url"):
            t["url"] = ico_to_profile.get(t.get("authority_ico") or "", "")
        # adresa_profilu z ISVZ někdy přichází bez schématu („www.…") —
        # bez doplnění by proklik v UI vedl relativně a kontrola odkazů padala
        if t["url"] and not t["url"].startswith(("http://", "https://")):
            t["url"] = "https://" + t["url"]

    # dedup duplicit — IČO + normalizovaný název + kompatibilní lhůta;
    # vyhrává informačně bohatší záznam (lhůta, hodnota), pole se slučují
    deduped, merged_away = dedup_cross_source(
        list(filtered.values()), today)
    # geografický okruh od HK — nad limit se zahazuje, neurčené se značí
    deduped, geo_dropped = geo.apply_radius(deduped)
    # TRVALÁ RETENCE (pokyn 29. 7. 2026 — kompletní historie): jednou
    # zachycená zakázka se z archivu už nikdy neztrácí. Záznamy mimo
    # aktuální stahovací okno (starší ISVZ měsíce, pomíjivé PVU RSS) se
    # přenášejí z předchozího snapshotu; znovu se prohánějí oborovým
    # filtrem, aby zpřísnění konfigurace pročistilo i historii. Čerstvý
    # záznam má vždy přednost (přenáší se jen nespatřená ID). NEpřenáší
    # se id, která tento běh vyřadil geo filtr (zdroj je stále publikuje,
    # ale mimo okruh) ani id sloučená dedupem (žijí dál ve vítězi) —
    # jinak by je retence obratem oživila ze starého snapshotu.
    fresh_ids = {t["id"] for t in deduped}
    carried = [
        t for t in prev_snapshot
        if t["id"] not in fresh_ids
        and t["id"] not in geo_dropped
        and t["id"] not in merged_away
        and _sector_ok(t)
    ]
    # zpřesněná geolokace (sídlo/jméno zadavatele) doplní polohu i dřív
    # neurčeným záznamům archivu — mimo okruh se zahazují (nikdy do něj
    # nepatřily), určené v okruhu dostanou dist_km
    carried = geo.relocate_unknown(carried)
    # Přenášený záznam bez lhůty i stavu, který se v aktuálních exportech
    # už neobjevuje, je prakticky jistě uzavřený (živé VZ dostávají změny
    # a v okně se ukazují znovu) — bez tohoto by backfill historie zaplnil
    # výchozí pohled tisíci zdánlivě aktivních zombie záznamů.
    for t in carried:
        if not t.get("deadline") and not t.get("state"):
            t["expired"] = True
    # druhý průchod dedupu: čerstvé × přenášené — pomíjivý PVU záznam
    # z retence se s pozdějším ISVZ id téže zakázky potká až tady
    # (případ AGAPÉ, 2026-08-02); expirovanou historii chrání podmínka
    # kompatibility lhůt v dedup_cross_source
    combined, _ = dedup_cross_source(deduped + carried, today)
    result = sorted(
        combined,
        key=lambda t: (t.get("deadline") or "9999", t.get("published") or ""),
    )
    # úklid registrových torz bez lhůty: ověření u zdroje (NEN/profil),
    # obohacení živých, expirace mrtvých, 21denní zpětná kontrola
    if not dry_run:
        _verify_no_deadline(result, errors)
    # mrtvé odkazy: 404/410 ve 2 bězích po sobě => expired (viz docstring)
    if not dry_run:
        _check_dead_links(result, {t["id"]: t for t in prev_snapshot})
    # obohacení detailů z NEN API (prohlídka, vysvětlení ZD) — PŘED diffem,
    # aby změny těchto polí vstoupily do archivu changes.json
    errors.extend(fetch_nen.enrich(result))
    # diff proti předchozímu snapshotu — PŘED přepsáním tenders.json
    changes = compute_changes(result, today)
    # Konkurence: z KOMPLETNÍHO all_t (bez cenového stropu), vlastní archiv
    competition = build_competition(all_t, today)
    # award patří do competition.json — v tenders.json by jen duplikoval
    for t in result:
        t.pop("award", None)

    out_tenders = ROOT / config.OUT_TENDERS

    meta = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "total": len(result),
            "vz": sum(1 for t in result if t["kind"] == "VZ"),
            "vzmr": sum(1 for t in result if t["kind"] == "VZMR"),
            "active": sum(1 for t in result if not t["expired"]),
            "changed": len(changes),
            "competition": len(competition),
            "previous": prev_count,
        },
        "errors": errors,
    }
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    # pojistka proti přepsání dat prázdným/degradovaným výsledkem
    if prev_count and len(result) < prev_count * config.MIN_RESULT_RATIO:
        print(
            f"CHYBA: výsledek ({len(result)}) < {config.MIN_RESULT_RATIO:.0%} "
            f"předchozího ({prev_count}) — data NEPŘEPSÁNA.",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print("Dry-run: bez zápisu.")
        return 0

    out_tenders.parent.mkdir(parents=True, exist_ok=True)
    out_tenders.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), "utf-8"
    )
    (ROOT / config.OUT_CHANGES).write_text(
        json.dumps(changes, ensure_ascii=False, indent=1), "utf-8"
    )
    (ROOT / config.OUT_COMPETITION).write_text(
        json.dumps(competition, ensure_ascii=False, indent=1), "utf-8"
    )
    (ROOT / config.OUT_META).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), "utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run(dry_run="--dry-run" in sys.argv))
