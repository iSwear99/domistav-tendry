"""Stažení a parsování ISVZ Open Dat (Registr veřejných zakázek).

Struktura ověřena 2026-07-28 proti VZ-06-2026.zip a dokumentaci 2.9.0:
měsíční ZIP (VZ-{MM}-{YYYY}.zip) obsahuje jeden JSON s obálkou
{obdobi_od, obdobi_do, verze, data: [{verejna_zakazka, historie_lhut,
zdroj_dat}]}. Skutečné cesty polí viz komentář v config.py.

Soubor za běžící měsíc ještě neexistuje (publikace cca 1.–5. dne
následujícího měsíce) — 404 u aktuálního/minulého měsíce se tiše
přeskakuje, u starších měsíců se hlásí jako chyba.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import time
import urllib.error
import urllib.request
import zipfile

import config


def _get(url: str) -> bytes | None:
    """Stažení s retry; 404 vrací None (soubor ještě/už nepublikován)."""
    last_exc: Exception | None = None
    for attempt in range(config.RETRIES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": config.USER_AGENT}
            )
            with urllib.request.urlopen(
                req, timeout=config.ISVZ_TIMEOUT
            ) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_exc = exc
            time.sleep(2 ** attempt)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Stažení selhalo: {url}: {last_exc}")


def _uniq(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _authority(vz: dict) -> tuple[str, str, str]:
    """(název, IČO, sídlo) zadavatele — preferuje se ten, kdo postup zadává.
    Sídlo (např. „Chrudimská 1882, Čáslav 28601") slouží jako poslední
    vodítko pro geo lokalizaci, když chybí místo plnění i obec v názvu."""
    for zp in vz.get("zadavaci_postupy") or []:
        zzp = zp.get("zadavatel_zadavaciho_postupu") or {}
        zadavatele = zzp.get("zadavatele") or []
        chosen = next(
            (z for z in zadavatele if z.get("zadava_zadavaci_postup")),
            zadavatele[0] if zadavatele else None,
        )
        if chosen:
            subj = chosen.get("subjekt") or {}
            return (
                str(subj.get("nazev_subjektu") or "").strip(),
                str(subj.get("ico") or "").strip(),
                str(subj.get("sidlo") or "").strip(),
            )
    return "", "", ""


def _predmet_fields(vz: dict) -> tuple[list[str], list[str], str]:
    """(cpv, nuts, place) z předmětu VZ i všech částí."""
    cpv: list[str] = []
    nuts: list[str] = []
    places: list[str] = []
    predmety = [vz.get("predmet") or {}] + [
        c.get("predmet") or {} for c in vz.get("casti_verejne_zakazky") or []
    ]
    for p in predmety:
        if p.get("hlavni_kod_CPV"):
            cpv.append(str(p["hlavni_kod_CPV"]))
        cpv.extend(str(k) for k in p.get("vedlejsi_kod_CPV") or [] if k)
        for m in p.get("mista_plneni") or []:
            if m.get("nuts"):
                nuts.append(str(m["nuts"]))
            for key in ("misto_plneni_jine", "dalsi_informace_o_miste_plneni"):
                if m.get(key):
                    places.append(str(m[key]).strip())
    return _uniq(cpv), _uniq(nuts), "; ".join(_uniq(places))


def _value(vz: dict) -> float | None:
    val = vz.get("predpokladana_hodnota_bez_DPH_v_CZK")
    if val is not None:
        return float(val)
    if vz.get("predpokladana_hodnota_bez_DPH_mena") in (None, "CZK"):
        val = vz.get("predpokladana_hodnota_bez_DPH")
        if val is not None:
            return float(val)
    # fallback: součet hodnot částí (jen pokud ji všechny části uvádějí)
    parts = vz.get("casti_verejne_zakazky") or []
    vals = [c.get("predpokladana_hodnota_casti_bez_DPH_v_CZK") for c in parts]
    if vals and all(v is not None for v in vals):
        return float(sum(vals))
    return None


def _part_zps(vz: dict) -> list[dict]:
    """Zadávací postupy pro části — primárně z casti_verejne_zakazky."""
    zps = [
        c.get("zadavaci_postup_pro_cast")
        for c in vz.get("casti_verejne_zakazky") or []
        if c.get("zadavaci_postup_pro_cast")
    ]
    if zps:
        return zps
    out: list[dict] = []
    for zp in vz.get("zadavaci_postupy") or []:
        out.extend(z for z in zp.get("zadavaci_postupy_pro_casti") or [] if z)
    return out


def _deadline(zps: list[dict]) -> str:
    """Lhůta pro podání dle priority druhů; preferuje aktivní záznam,
    při více částech se bere nejpozdější konec."""
    for kind in config.ISVZ_DEADLINE_KINDS:
        for active_only in (True, False):
            ends = [
                str(l.get("datum_a_cas_konce_lhuty") or "")
                for zp in zps
                for l in zp.get("lhuty") or []
                if l.get("druh_lhuty") == kind
                and (l.get("aktivni") if active_only else True)
                and l.get("datum_a_cas_konce_lhuty")
            ]
            if ends:
                return max(ends)[:19]
    return ""


def normalize(rec: dict) -> dict | None:
    """Převede záznam ISVZ na interní model; None = nevalidní záznam."""
    vz = rec.get("verejna_zakazka") or {}
    rid = vz.get("identifikator_NIPEZ")
    title = vz.get("nazev_verejne_zakazky")
    if not rid or not title:
        return None
    cpv, nuts, place = _predmet_fields(vz)
    authority, ico, seat = _authority(vz)
    zps = _part_zps(vz)
    published = min(
        (str(z.get("datum_zahajeni_zadavaciho_postupu") or "")[:10]
         for z in zps if z.get("datum_zahajeni_zadavaciho_postupu")),
        default="",
    )
    url = next(
        (str(z["odkaz_na_profil"]) for z in zps if z.get("odkaz_na_profil")),
        "",
    )
    state = next((str(z["stav"]) for z in zps if z.get("stav")), "")
    clar = sum(
        int(z.get("pocet_zaslanych_zaevidovanych_vysvetleni_zadavaci_dokumentace") or 0)
        for z in zps
    )
    vzmr = vz.get("rezim_verejne_zakazky") == config.ISVZ_REZIM_VZMR
    nen_id = next(
        (str(i.get("identifikator") or "")
         for i in vz.get("identifikatory_v_elektronickem_nastroji") or []
         if i.get("kod_nastroje") == "NEN"),
        "",
    )
    return {
        # NIPEZ id sdílí i XML NEN profilů (id_nipez) ⇒ prefix rvz: dává
        # stejné stabilní ID záznamu z obou zdrojů a dedup je přesný.
        "id": f"rvz:{rid}",
        "source": "isvz",
        "nen_id": nen_id,
        "title": str(title).strip(),
        "authority": authority,
        "authority_ico": ico,
        "cpv": cpv,
        "nuts": nuts,
        "value": _value(vz),
        "published": published,
        "deadline": _deadline(zps),
        "url": url,
        "place": place,
        "authority_seat": seat,
        "state": state,
        "site_visit": "",       # v open datech není (viz config.py)
        "clarifications": clar,
        "kind": "VZMR" if vzmr else "VZ",
    }


def _months() -> list[tuple[int, int]]:
    """(rok, měsíc) od nejstaršího po aktuální — novější měsíc obsahuje
    aktuálnější verze záznamů a při dedupu dle id má vyhrát."""
    today = dt.date.today()
    y, m = today.year, today.month
    out: list[tuple[int, int]] = []
    for _ in range(config.ISVZ_MONTHS_BACK):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _recent(y: int, m: int) -> bool:
    """Aktuální nebo minulý měsíc — jeho export ještě nemusí existovat."""
    today = dt.date.today()
    cur = today.year * 12 + today.month
    return cur - (y * 12 + m) <= 1


def fetch() -> tuple[list[dict], list[str]]:
    """Vrací (zakázky, chyby)."""
    errors: list[str] = []
    tenders: list[dict] = []
    for y, m in _months():
        for tpl in config.ISVZ_DOWNLOAD_URLS:
            url = tpl.format(year=y, month=m)
            try:
                resp = _get(url)
                if resp is None:
                    if not _recent(y, m):
                        errors.append(f"ISVZ: {url}: 404 (soubor chybí)")
                    continue
                with zipfile.ZipFile(io.BytesIO(resp)) as zf:
                    name = next(
                        n for n in zf.namelist() if n.lower().endswith(".json")
                    )
                    payload = json.loads(zf.read(name).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ISVZ: {url}: {exc}")
                continue
            for raw in payload.get("data") or []:
                item = normalize(raw)
                if item:
                    tenders.append(item)
    return tenders, errors


if __name__ == "__main__":
    data, errs = fetch()
    print(json.dumps({"count": len(data), "errors": errs}, ensure_ascii=False))
