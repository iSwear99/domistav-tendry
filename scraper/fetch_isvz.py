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


def _authority(vz: dict) -> tuple[str, str, str, str]:
    """(název, IČO, sídlo, adresa profilu) zadavatele — preferuje se ten,
    kdo postup zadává. Sídlo slouží jako poslední vodítko geo lokalizace,
    adresa profilu jako záložní proklik, když chybí odkaz na detail VZ."""
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
                str(chosen.get("adresa_profilu") or "").strip(),
            )
    return "", "", "", ""


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


def _ucastnici_map(zps: list[dict]) -> dict[str, tuple[list[str], list[str]]]:
    """identifikace účastníka → (názvy, IČO) jeho dodavatelů.

    Výsledek (vybraní dodavatelé, strany smlouvy) na účastníka často
    odkazuje JEN identifikací bez subjektu — bez tohoto překladu měla
    Konkurence u 39 % ISVZ záznamů „vítěz neuveden" (17. 11. 2026)."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    for zp in zps:
        for u in zp.get("ucastnici") or []:
            ident = str(u.get("identifikace") or "")
            if not ident:
                continue
            names, icos = out.setdefault(ident, ([], []))
            for d in u.get("dodavatele") or []:
                s = d.get("subjekt") or {}
                n = str(s.get("nazev_subjektu") or "").strip()
                i = str(s.get("ico") or "").strip()
                if n and n not in names:
                    names.append(n)
                if i and i not in icos:
                    icos.append(i)
    return out


def _bidders(zps: list[dict]) -> list[dict]:
    """Účastníci řízení s nabídkovou cenou a výsledkem (veřejná data RVZ).

    Stejný tvar jako bidders z XML profilů (nazev, ico, cena, vyloucen)
    + pořadí nabídky a příznak vybrán. Osoby, které podání provedly,
    se záměrně NEČTOU (osobní údaje). Konsorcium = jeden účastník,
    názvy spojené „ + ", všechna IČO v `icos`."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for zp in zps:
        vysl = zp.get("vysledek") or {}
        vybrani = {str(v.get("identifikator_ucastnika_zadavaciho_postupu") or "")
                   for v in vysl.get("vybrani_dodavatele_zadavaciho_postupu") or []}
        for s in vysl.get("smlouva") or []:
            for dd in s.get("dodavatele_se_kterymi_byla_uzavrena_smlouva") or []:
                if dd.get("identifikace"):
                    vybrani.add(str(dd["identifikace"]))
        zanikli = {str(z.get("identifikace") or "")
                   for z in zp.get("ucastnici_jimz_zanikla_ucast_v_zadavacim_postupu") or []}
        for u in zp.get("ucastnici") or []:
            names, icos = [], []
            for d in u.get("dodavatele") or []:
                sub = d.get("subjekt") or {}
                n = str(sub.get("nazev_subjektu") or "").strip()
                i = str(sub.get("ico") or "").strip()
                if n and n not in names:
                    names.append(n)
                if i and i not in icos:
                    icos.append(i)
            if not names and not icos:
                continue
            cena = poradi = None
            for p in u.get("podani") or []:
                if p.get("poradi_nabidky") is not None and poradi is None:
                    poradi = p["poradi_nabidky"]
                for h in p.get("nabidkove_hodnoty") or []:
                    druh = h.get("druh_nabidkove_hodnoty_odpovidajici_"
                                  "ciselne_vyjadritelnym_kriteriem_hodnoceni")
                    if (druh == "Nabídková cena bez DPH" and cena is None
                            and h.get("mena") in (None, "CZK")
                            and h.get("hodnota") is not None):
                        cena = float(h["hodnota"])
            ident = str(u.get("identifikace") or "")
            key = (tuple(icos) or tuple(names), cena)
            if key in seen:            # tentýž účastník ve více částech
                continue
            seen.add(key)
            out.append({
                "nazev": " + ".join(names),
                "ico": icos[0] if icos else "",
                **({"icos": icos} if len(icos) > 1 else {}),
                "cena": cena,
                "poradi": poradi,
                "vybran": bool(ident) and ident in vybrani,
                "vyloucen": bool(ident) and ident in zanikli,
            })
    return out


def _pridej_dodavatele(dd: dict, ucast: dict, winners: list[str],
                       icos: list[str]) -> None:
    """Vítěz ze subjektu, jinak z odkazu na účastníka (identifikace)."""
    subj = dd.get("subjekt") or {}
    names = [str(subj.get("nazev_subjektu") or "").strip()]
    ids = [str(subj.get("ico") or "").strip()]
    if not any(names) and not any(ids):
        ref = (str(dd.get("identifikace") or "")
               or str(dd.get("identifikator_ucastnika_zadavaciho_postupu") or ""))
        names, ids = ucast.get(ref, ([], []))
    for n in names:
        if n and n not in winners:
            winners.append(n)
    for i in ids:
        if i and i not in icos:
            icos.append(i)


def _award(zps: list[dict]) -> dict | None:
    """Výsledek řízení ze smluv všech částí → podklad pro Konkurenci.

    Ceny se sčítají přes smlouvy (části VZ), uhrazená cena přes kalendářní
    roky plnění (skutecne_uhrazene_ceny). Jiná měna než CZK se přeskakuje.
    """
    ucast = _ucastnici_map(zps)
    vysledky = [zp.get("vysledek") or {} for zp in zps]
    smlouvy = [s for v in vysledky for s in v.get("smlouva") or []]
    # vítěz může být uveden i jen v rozhodnutí o výběru (před podpisem)
    vybrani = [d for v in vysledky
               for d in v.get("vybrani_dodavatele_zadavaciho_postupu") or []]
    if not smlouvy:
        return None
    winners: list[str] = []
    icos: list[str] = []
    dates: list[str] = []
    contracted = 0.0
    paid = 0.0
    has_contracted = has_paid = False
    for s in smlouvy:
        if s.get("hodnota_smluvni_ceny_mena") in (None, "CZK"):
            v = s.get("hodnota_smluvni_ceny_bez_DPH")
            if v is not None:
                contracted += float(v)
                has_contracted = True
        for u in s.get("skutecne_uhrazene_ceny") or []:
            if u.get("mena") in (None, "CZK"):
                hv = u.get("hodnota_skutecne_uhrazene_ceny_bez_DPH")
                if hv is not None:
                    paid += float(hv)
                    has_paid = True
        d = str(s.get("datum_uzavreni_smlouvy") or "")[:10]
        if d:
            dates.append(d)
        for dd in s.get("dodavatele_se_kterymi_byla_uzavrena_smlouva") or []:
            _pridej_dodavatele(dd, ucast, winners, icos)
    if not winners:
        for dd in vybrani:
            _pridej_dodavatele(dd, ucast, winners, icos)
    # stránka smlouvy v Registru smluv (přílohy: SoD, položkový rozpočet)
    registr = next((str(s["odkaz_na_umisteni_smlouvy"]) for s in smlouvy
                    if "smlouvy.gov.cz" in str(s.get("odkaz_na_umisteni_smlouvy")
                                               or "")), "")
    return {
        **({"registr_url": registr} if registr else {}),
        "date": max(dates) if dates else "",
        "winner": "; ".join(winners),
        "winner_ico": "; ".join(icos),
        "price_contracted": contracted if has_contracted else None,
        "price_paid": paid if has_paid else None,
    }


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


def _deadline_from_history(rec: dict) -> str:
    """Fallback lhůty z historie_lhut na úrovni záznamu — VVZ formuláře
    často nemají lhůty v zadávacím postupu, ale v historii ano."""
    for kind in config.ISVZ_DEADLINE_KINDS:
        ends = [
            str(z.get("datum_a_cas_konce_lhuty") or "")
            for h in rec.get("historie_lhut") or []
            for z in h.get("zaznamy") or []
            if z.get("druh_lhuty") == kind and z.get("datum_a_cas_konce_lhuty")
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
    authority, ico, seat, profile = _authority(vz)
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
    # proklik: odkaz na detail VZ → detail v NEN (z NEN id) → profil zadavatele
    if not url and nen_id and "/" in nen_id:
        url = ("https://nen.nipez.cz/verejne-zakazky/detail-zakazky/"
               + nen_id.replace("/", "-"))
    if not url:
        url = profile
    deadline = _deadline(zps) or _deadline_from_history(rec)
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
        "deadline": deadline,
        "url": url,
        "place": place,
        "authority_seat": seat,
        "authority_profile": profile,
        "state": state,
        "site_visit": "",       # v open datech není (viz config.py)
        "clarifications": clar,
        "kind": "VZMR" if vzmr else "VZ",
        "award": _award(zps),   # výsledek řízení (Konkurence); None = nezadáno
        # účastníci s nabídkami — Konkurence, Moje účasti (vlastní IČO)
        **({"bidders": b} if (b := _bidders(zps)) else {}),
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
