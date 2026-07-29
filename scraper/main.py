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
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config            # noqa: E402
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


def _sector_ok(t: dict) -> bool:
    """Obor: (a) CPV prefix 45 — autoritativní, negativní slova nepřebíjí;
    (b) záchytná síť: klíčová slova v názvu (příznak kw_match);
    (c) záznam Z KONFIGUROVANÉHO profilu bez CPV — zdroj je předvybraný,
    ponechat. PVU RSS (profil:pvu:*) je celostátní a víceoborové, proto
    benevolenci (c) nedostává — bez CPV rozhodují jen klíčová slova,
    stejně jako u ISVZ. Nastavuje t["kw_match"]."""
    from_profile = (t["source"].startswith("profil:")
                    and not t["source"].startswith("profil:pvu:"))
    cpv = t.get("cpv") or []
    # CPV 45 platí, jen pokud kód nespadá do vyřazených podskupin
    # (dopravní infrastruktura) — takové zakázky rozhodují klíčová slova
    cpv_ok = any(
        c.startswith(tuple(config.CPV_PREFIXES))
        and not c.startswith(tuple(config.CPV_NEGATIVE_PREFIXES))
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
    from_profile = (t["source"].startswith("profil:")
                    and not t["source"].startswith("profil:pvu:"))
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


def _check_dead_links(tenders: list[dict], prev_by_id: dict) -> None:
    """Eliminace zakázek s neexistující stránkou detailu (pokyn 29. 7.).

    Kontrolují se jen AKTIVNÍ záznamy. Za mrtvý odkaz se považuje POUZE
    tvrdé HTTP 404/410 — SPA portály (NEN, VVZ) vracejí 200 na všechno,
    takže falešně pozitivní být nemohou; síťové chyby a timeouty se
    nepočítají. Expirace až po DVOU mrtvých bězích po sobě (čítač
    url_dead se přenáší z předchozího snapshotu) — jednorázový výpadek
    profilu tak zakázku nevyřadí. Vyřazené zůstávají v historii
    s příznakem link_dead (štítek v UI)."""
    import concurrent.futures
    import urllib.error
    import urllib.request

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

    active = [t for t in tenders if not t["expired"] and t.get("url")]
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


def _norm_title(s: str) -> str:
    """Normalizace názvu pro porovnání napříč zdroji: malá písmena,
    bez diakritiky, bez interpunkce, sjednocené mezery."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = "".join(c if c.isalnum() else " " for c in s)
    return " ".join(s.split())


def dedup_cross_source(tenders: list[dict]) -> list[dict]:
    """Stejná zakázka může přijít z ISVZ i z profilu zadavatele.

    Klíč shody: IČO zadavatele + normalizovaný název. Přednost má záznam
    z ISVZ (úplnější: NUTS, prohlídka, vysvětlení); z odstraněného
    duplikátu se převezme URL profilu jako `profile_url` (přímý odkaz
    na dokumentaci) a nižší lhůta/hodnota se NEslučuje — platí ISVZ.
    Bez IČO se deduplikace neprovádí (riziko falešné shody).
    Slučuje se POUZE napříč zdroji (ISVZ ↔ profil): dva záznamy téhož
    zdroje se shodným názvem jsou dvě různá řízení (typicky zrušené
    a znovu vypsané) — obě se ponechávají, jinak by zrušený pokus
    přemazal běžící (případ AGAPÉ, 2026-07-29).
    """
    def key(t):
        ico = (t.get("authority_ico") or "").strip()
        return (ico, _norm_title(t.get("title", ""))) if ico else None

    def rank(t):  # vyšší = přednost
        return 1 if t["source"] == "isvz" else 0

    best: dict[tuple, dict] = {}
    keyless: list[dict] = []
    for t in tenders:
        k = key(t)
        if k is None or not k[1]:
            keyless.append(t)
            continue
        cur = best.get(k)
        if cur is None:
            best[k] = t
        elif rank(t) == rank(cur):
            keyless.append(t)   # týž zdroj ⇒ jiné řízení, ponechat obě
        else:
            winner, loser = (t, cur) if rank(t) > rank(cur) else (cur, t)
            if loser["source"].startswith("profil:") and loser.get("url"):
                winner.setdefault("profile_url", loser["url"])
            best[k] = winner
    return list(best.values()) + keyless


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

    located = geo.apply_radius(list(fresh.values()))

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
        }

    # dřívější záznamy archivu se znovu prohánějí oborovým filtrem —
    # zpřísnění konfigurace (např. vyřazení silnic) tak pročistí i okno
    merged = {c["id"]: c for c in _load_json(config.OUT_COMPETITION, [])
              if c.get("awarded", "") >= cutoff and _sector_ok(c)}
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
    for name, mod in (("isvz", fetch_isvz), ("profily", fetch_profily),
                      ("pvu", fetch_pvu)):
        try:
            t, e = mod.fetch()
            all_t.extend(t)
            errors.extend(e)
            source_counts[name] = len(t)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: neočekávaná chyba: {exc}")
            source_counts[name] = 0

    # Výpadek celého zdroje (např. ISVZ blokuje IP GitHub runnerů) nesmí
    # smazat jeho dřívější záznamy — převezmou se z předchozího snapshotu.
    prev_snapshot = _load_json(config.OUT_TENDERS, [])
    # PVU: RSS drží jen ~24 h, takže jednou zachycené záznamy se přenášejí
    # z předchozího snapshotu VŽDY — vkládají se PŘED čerstvé, aby při
    # dedupu podle ID vyhrál novější stav (a ISVZ dle ranku úplně nejvíc)
    carried_pvu = [
        t for t in prev_snapshot
        if t.get("source", "").startswith("profil:pvu:")
    ]
    if carried_pvu:
        all_t = carried_pvu + all_t

    for name, prefix in (("isvz", ("isvz",)), ("profily", ("profil:",))):
        if source_counts.get(name) == 0:
            retained = [
                t for t in prev_snapshot
                if t.get("source", "").startswith(prefix)
            ]
            if retained:
                all_t.extend(retained)
                errors.append(
                    f"{name}: zdroj nedostupný — ponecháno {len(retained)} "
                    "záznamů z předchozího běhu."
                )

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

    # dedup napříč zdroji (ISVZ vs. profil) — IČO + normalizovaný název
    deduped = dedup_cross_source(list(filtered.values()))
    # geografický okruh od HK — nad limit se zahazuje, neurčené se značí
    deduped = geo.apply_radius(deduped)
    result = sorted(
        deduped,
        key=lambda t: (t.get("deadline") or "9999", t.get("published") or ""),
    )
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
