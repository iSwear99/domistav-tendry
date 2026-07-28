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


def _passes_filters(t: dict) -> bool:
    # Obor: (a) CPV prefix 45 — autoritativní, negativní slova nepřebíjí;
    #       (b) záchytná síť: klíčová slova v názvu (příznak kw_match);
    #       (c) záznam Z PROFILU bez CPV — zdroj je předvybraný, ponechat
    #           (ISVZ pokrývá celou ČR, tam bez CPV rozhodují jen klíčová
    #           slova — jinak by prošly celostátní VZMR mimo obor).
    from_profile = t["source"].startswith("profil:")
    cpv = t.get("cpv") or []
    cpv_ok = any(c.startswith(p) for c in cpv for p in config.CPV_PREFIXES)
    t["kw_match"] = False
    if not cpv_ok:
        if _kw_match(t.get("title", "")):
            t["kw_match"] = True
        elif not (from_profile and not cpv):
            return False
    # NUTS
    nuts = t.get("nuts") or []
    if nuts:
        if not any(n[:5] in config.NUTS_ALLOWED for n in nuts):
            return False
        t["no_nuts"] = False
    else:
        if not from_profile and not config.KEEP_MISSING_NUTS:
            return False
        t["no_nuts"] = not from_profile
    # hodnota
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
        else:
            winner, loser = (t, cur) if rank(t) > rank(cur) else (cur, t)
            if loser["source"].startswith("profil:") and loser.get("url"):
                winner.setdefault("profile_url", loser["url"])
            best[k] = winner
    return list(best.values()) + keyless


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
    for name, mod in (("isvz", fetch_isvz), ("profily", fetch_profily)):
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
            filtered[t["id"]] = winner
        else:
            filtered[t["id"]] = t
    # dedup napříč zdroji (ISVZ vs. profil) — IČO + normalizovaný název
    deduped = dedup_cross_source(list(filtered.values()))
    # geografický okruh od HK — nad limit se zahazuje, neurčené se značí
    deduped = geo.apply_radius(deduped)
    result = sorted(
        deduped,
        key=lambda t: (t.get("deadline") or "9999", t.get("published") or ""),
    )
    # obohacení detailů z NEN API (prohlídka, vysvětlení ZD) — PŘED diffem,
    # aby změny těchto polí vstoupily do archivu changes.json
    errors.extend(fetch_nen.enrich(result))
    # diff proti předchozímu snapshotu — PŘED přepsáním tenders.json
    changes = compute_changes(result, today)

    out_tenders = ROOT / config.OUT_TENDERS

    meta = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "total": len(result),
            "vz": sum(1 for t in result if t["kind"] == "VZ"),
            "vzmr": sum(1 for t in result if t["kind"] == "VZMR"),
            "active": sum(1 for t in result if not t["expired"]),
            "changed": len(changes),
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
    (ROOT / config.OUT_META).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), "utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run(dry_run="--dry-run" in sys.argv))
