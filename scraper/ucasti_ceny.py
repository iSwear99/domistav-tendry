"""Nabídkové ceny ostatních účastníků z dokumentů řízení (pokyn 17. 11. 2026).

ISVZ nabídkové ceny u většiny řízení NEVYPLŇUJE (z 29 účastí měly ceny
všech účastníků 2) a XML profilů také jen zřídka. Zadavatel je ale musí
zveřejnit v písemné zprávě (§ 217 ZZVZ) a obvykle i v oznámení o výběru
dodavatele — tabulka „účastník — nabídková cena". Tento krok proto pro
Moje účasti s chybějícími cenami:

1. přečte XMLdataVZ profilu zadavatele v okně kolem data řízení → doplní
   účastníky (vč. příznaků vyloučen/odstoupil a důvodu) a najde dokumenty
   typu písemná zpráva / výběr dodavatele / hodnocení,
2. z textu dokumentu (PDF/DOCX) vytěží ceny ZNÁMÝCH účastníků: řádek se
   jménem (nebo IČO) účastníka + nejbližší částka. Pojistky: částka musí
   být věrohodná vůči smluvní ceně / naší ceně / předpokládané hodnotě,
   cena vítěze se ověří proti smluvní ceně (sloupec s DPH se pozná
   poměrem 1,21 a přepočte), existující ceny se NIKDY nepřepisují,
   výsledek platí, jen když se našly aspoň dvě ceny.

Ceny z dokumentu nesou `cena_zdroj: "dokument"`, záznam `ceny_dok` (odkaz
na veřejný dokument). Stažené soubory se neukládají. Jednou zkontrolovaný
záznam se znovu zkouší až po RETRY_DAYS (výsledek mohl přibýt později).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config  # noqa: E402
import fetch_dokumenty as fd  # noqa: E402
import fetch_profily as fp  # noqa: E402
import ucasti  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_PER_RUN = 25
RETRY_DAYS = 30
MAX_DOCS = 4

# pořadí důležitosti dokumentů (typ_dokumentu z XML / text odkazu)
_DOC_KINDS = ("pisemna zprava", "vyber", "oznameni o vyberu", "hodnoceni",
              "posouzeni", "protokol", "otevirani")
_DOC_SKIP = ("vysvetleni", "zadavaci dokumentace", "smlouva", "dodatek",
             "vyzva k podani", "uhrazen", "oznameni o zahajeni")

_PRAVNI_FORMA = re.compile(
    r"\b(spol(ecnost)?\s*s\s*r\s*o|s\s*r\s*o|a\s*s|v\s*o\s*s|k\s*s|se|z\s*s"
    r"|o\s*p\s*s|druzstvo|spol)\b")
# částka: tisíce oddělené mezerou/tečkou (+ desetiny), nebo dlouhé číslo
# s desetinami; holé dlouhé číslo NE (IČO, čísla jednací)
_CASTKA = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ . ]\d{3})+(?:,\d{1,2})?|\d{5,}[,.]\d{2})"
    r"(?![\d])")
_PORADI = re.compile(r"^\s*(\d{1,2})\s*[.)]\s")


def _num(s: str) -> float | None:
    s = s.replace(" ", " ")
    if re.fullmatch(r"\d{5,}\.\d{2}", s):
        return float(s)
    s = s.replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _jadro(nazev: str) -> str:
    """Rozlišující část jména firmy bez právní formy (slova s mezerami)."""
    n = ucasti._norm(nazev)
    jadro = " ".join(_PRAVNI_FORMA.sub(" ", n).split())
    return jadro if len(jadro.replace(" ", "")) >= 3 else n


def _shoda(b: dict, line: str) -> bool:
    """Je na řádku účastník? Dlouhé jméno se hledá i bez mezer (pypdf
    občas slova rozdělí: „Hra dci"), krátké (ASJ) jen jako celé slovo."""
    ico = b.get("ico") or ""
    if ico and re.search(rf"(?<!\d){ico}(?!\d)", line):
        return True
    jadro = _jadro(b.get("nazev") or "")
    if not jadro:
        return False
    nl = ucasti._norm(line)
    if len(jadro.replace(" ", "")) >= 6:
        return jadro.replace(" ", "") in nl.replace(" ", "")
    return re.search(rf"\b{re.escape(jadro)}\b", nl) is not None


def _reference(u: dict) -> float | None:
    for k in ("price_contracted", "winner_cena", "value"):
        if u.get(k):
            return float(u[k])
    return (u.get("my") or {}).get("cena")


def ceny_z_textu(text: str, bidders: list[dict], u: dict) -> dict[int, tuple]:
    """index účastníka → (cena bez DPH, pořadí|None). Prázdné = nic jistého."""
    ref = _reference(u)
    lo, hi = ((0.3 * ref, 3.0 * ref) if ref else (50_000, 5e10))
    icos = {b.get("ico") for b in bidders if b.get("ico")}
    lines = text.splitlines()
    found: dict[int, tuple] = {}
    for i, b in enumerate(bidders):
        for li in range(len(lines)):
            if not _shoda(b, lines[li]):
                continue
            cena = None
            # buňka tabulky bývá víceřádková (jméno, IČO, sídlo… cena)
            for cand in (lines[li], *lines[li + 1:li + 7]):
                vals = [v for m in _CASTKA.finditer(cand)
                        if (v := _num(m.group(1))) and lo <= v <= hi
                        and m.group(1).replace(" ", "") not in icos]
                if vals:
                    cena = vals[0]
                    # „s DPH | bez DPH" vedle sebe → menší z dvojice
                    if len(vals) > 1 and abs(vals[0] / vals[1] - 1.21) < 0.01:
                        cena = vals[1]
                    break
                if cand is not lines[li] and any(_shoda(x, cand) for x in bidders):
                    break            # další řádek už patří jinému účastníkovi
            if cena:
                m = _PORADI.match(lines[li])
                found[i] = (cena, int(m.group(1)) if m else None)
                break
    if len(found) < 2:
        return {}
    # ověření proti smluvní ceně vítěze
    vit = next((i for i, b in enumerate(bidders) if b.get("vybran")), None)
    pc = u.get("price_contracted")
    if vit is not None and vit in found and pc:
        r = found[vit][0] / pc
        if abs(r - 1.21) < 0.02:          # dokument uvádí ceny s DPH
            found = {i: (round(c / 1.21, 2), p) for i, (c, p) in found.items()}
        elif not 0.8 <= r <= 1.25:
            return {}
    # pořadí z dokumentu jen když je jednoznačné
    por = [p for _, p in found.values() if p]
    if len(por) != len(set(por)):
        found = {i: (c, None) for i, (c, _) in found.items()}
    return found


def _potrebuje(u: dict) -> bool:
    b = [x for x in u.get("bidders") or [] if not x.get("odstoupil")]
    return u.get("vysledek") != "ceka" and (
        len(b) <= 1 or any(x.get("cena") is None for x in b))


def _okno(u: dict) -> tuple[dt.date, dt.date] | None:
    dny = [d for d in (ucasti._datum(u.get(k)) for k in
                       ("published", "deadline", "awarded")) if d]
    if not dny:
        return None
    od = min(dny) - dt.timedelta(days=14)
    do = min(dt.date.today(), max(dny) + dt.timedelta(days=120))
    return od, do


def _xml_zakazka(u: dict, cache: dict) -> ET.Element | None:
    prof = (u.get("authority_profile") or "").rstrip("/")
    okno = _okno(u)
    if not prof or not okno or "XMLdataVZ" in prof:
        return None
    url = f"{prof}/XMLdataVZ?od={okno[0]:%d%m%Y}&do={okno[1]:%d%m%Y}"
    if url not in cache:
        try:
            cache[url] = ET.fromstring(fp._get(url))
        except Exception:  # noqa: BLE001
            cache[url] = None
        time.sleep(1)
    root = cache[url]
    if root is None:
        cache["_chyba"] = True          # výpadek ≠ „nic nenalezeno"
        return None
    ids = {i.split(":")[-1] for i in [u["id"], *(u.get("slouceno") or [])]}
    kandidati = []
    for el in root.iter():
        if fp._strip_ns(el.tag) != "zakazka":
            continue
        zid = {fp._first(el, "id_nipez"), fp._first(el, "id_objektu")} - {""}
        if zid & ids:
            return el
        if ucasti._nazvy_shodne(fp._first(el, "nazev_vz"), u["title"]):
            kandidati.append(el)
    return kandidati[0] if len(kandidati) == 1 else None


def _dokumenty(el: ET.Element | None, u: dict) -> list[str]:
    docs: list[tuple[int, str]] = []
    if el is not None:
        for d in el.iter():
            if fp._strip_ns(d.tag) != "dokument":
                continue
            typ = ucasti._norm(fp._first(d, "typ_dokumentu"))
            href = fp._first(d, "url").replace("&amp;", "&")
            if not href or any(s in typ for s in _DOC_SKIP):
                continue
            pr = next((i for i, k in enumerate(_DOC_KINDS) if k in typ), None)
            if pr is not None:
                docs.append((pr, href))
    if not docs and u.get("url") and "/profil" not in u["url"]:
        href = fd._find_doc_link(u["url"], ["pisemna zprava",
                                            "oznameni o vyberu",
                                            "rozhodnuti o vyberu"])
        if href:
            docs.append((0, href))
    seen, out = set(), []
    for _, h in sorted(docs):
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:MAX_DOCS]


_EZAK_SOUBOR = re.compile(r'href="(document_\d+/[^"]+)"')


def _stahni(href: str) -> bytes:
    """Dokument; E-ZAK `document_download_N.html` je jen stránka s odkazem
    na soubor (`document_N/<hash>-<jméno>`) → stáhnout ten. Tender arena
    soubory chrání ochranou proti robotům (Cloudflare) — NEOBCHÁZÍ se,
    HTML odpověď se prostě nevytěží."""
    data = fd._get(href)
    if data[:400].lstrip().lower().startswith((b"<!doctype", b"<html")):
        m = _EZAK_SOUBOR.search(data.decode("utf-8", "replace"))
        if m:
            import urllib.parse
            data = fd._get(urllib.parse.urljoin(href, m.group(1)))
    return data


def doplnit(u: dict, cache: dict, errors: list[str]) -> bool:
    """Doplní účastníky/ceny jedné účasti; True = něco přibylo."""
    zmena = False
    el = _xml_zakazka(u, cache)
    if el is not None:
        xml_b = [fp.xml_ucastnik(x) for x in el.iter()
                 if fp._strip_ns(x.tag) == "ucastnik"]
        xml_b = [b for b in xml_b if b.get("nazev") or b.get("ico")]
        if xml_b:
            pred = json.dumps(u.get("bidders"), sort_keys=True)
            u["bidders"] = ucasti.slouc_bidders(u.get("bidders") or [], xml_b)
            zmena |= pred != json.dumps(u["bidders"], sort_keys=True)
    if not _potrebuje(u):
        return zmena
    bidders = u.get("bidders") or []
    for href in _dokumenty(el, u):
        try:
            text = fd._text_from(_stahni(href), href)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ucasti_ceny {u['id']}: {str(exc)[:80]}")
            cache["_chyba"] = True
            continue
        found = ceny_z_textu(text, bidders, u)
        if not found:
            continue
        for i, (cena, poradi) in found.items():
            b = bidders[i]
            if b.get("cena") is None:
                b["cena"], b["cena_zdroj"] = cena, "dokument"
                zmena = True
            if b.get("poradi") is None and poradi:
                b["poradi"] = poradi
                zmena = True
        u.setdefault("ceny_dok", href)
        # další dokument jen pro chybějící pořadí (oznámení o výběru ho
        # uvádí, písemná zpráva často ne)
        if all(b.get("poradi") or b.get("cena") is None for b in bidders):
            break
    return zmena


def run(max_n: int = MAX_PER_RUN) -> str:
    path = ROOT / config.OUT_UCASTI
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return "ucasti_ceny: ucasti.json chybí"
    entries = data.get("ucasti") or {}
    today = dt.date.today()
    cache: dict = {}
    errors: list[str] = []
    n = zmen = 0
    for u in entries.values():
        if n >= max_n:
            break
        if not _potrebuje(u):
            continue
        k = ucasti._datum(u.get("ceny_kontrola"))
        if k and (today - k).days < RETRY_DAYS:
            continue
        n += 1
        cache["_chyba"] = False
        try:
            zmen += doplnit(u, cache, errors)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ucasti_ceny {u['id']}: {str(exc)[:80]}")
            cache["_chyba"] = True
        # síťový výpadek (NEN XML občas timeoutuje) ⇒ zkusit hned příště
        if not cache.get("_chyba"):
            u["ceny_kontrola"] = today.isoformat()
        ucasti._prepocti(u)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    return (f"ucasti_ceny: zkontrolováno {n}, doplněno {zmen}, "
            f"chyb {len(errors)}" + (f" ({errors[0]})" if errors else ""))


if __name__ == "__main__":
    print(run(int(sys.argv[1]) if len(sys.argv) > 1 else MAX_PER_RUN))
