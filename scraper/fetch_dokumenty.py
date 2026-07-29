"""Čtení dokumentů zakázek (pokyn 29. 7. 2026).

- AKTIVNÍ relevantní zakázky: stáhnout „zadávací dokumentaci" a vytěžit
  místo plnění, termíny a předpokládanou hodnotu (v metadatech často chybí).
- KONKURENCE: stáhnout „písemnou zprávu zadavatele" a vytěžit vítěze
  a konečnou cenu.

Vytěžování je HEURISTICKÉ (regulární výrazy nad extrahovaným textem) —
server nemá AI; co se nenajde, zůstává prázdné a UI to skryje.
Zdroje dokumentů: XMLdataVZ profilů (typ_dokumentu + url) a HTML detailů
zakázek (odkazy s textem „zadávací dokumentace" / „písemná zpráva").
PDF čte pypdf (jediná povolená výjimka ze stdlib — čistě pythonní,
viz requirements.txt), DOCX se čte stdlib zipfile.

Stažené soubory se NEUKLÁDAJÍ — jen vytěžená pole do dokumenty.json.
Limity: max. MAX_DOWNLOADS nových dokumentů na běh (postupné doplňování),
MAX_BYTES na soubor; jednou vytěžené id se znovu nestahuje.
"""
from __future__ import annotations

import datetime as dt
import html as html_mod
import io
import json
import pathlib
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config  # noqa: E402

try:
    from pypdf import PdfReader
except ImportError:              # bez pypdf se PDF přeskakují (chyba v meta)
    PdfReader = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / config.OUT_DIR / "dokumenty.json"
MAX_DOWNLOADS = 30               # nových dokumentů na běh (ZD i zpráv zvlášť)
MAX_BYTES = 40 * 1024 * 1024
MAX_PDF_PAGES = 40


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _get(url: str, binary: bool = True) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=config.TIMEOUT) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("soubor přes limit velikosti")
    return data


# ── nalezení dokumentu na HTML detailu zakázky ──────────────────────────────

_A_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def _find_doc_link(page_url: str, patterns: list[str],
                   depth: int = 0) -> str | None:
    """Na stránce detailu najde odkaz, jehož text odpovídá vzoru
    (např. „zadavaci dokumentace"); jednou smí sestoupit do podstránky
    dokumentů (NEN má sekce jako /zadavaci-dokumentace)."""
    try:
        page = _get(page_url).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    candidates: list[tuple[str, str]] = []
    for href, text in _A_RE.findall(page):
        text = _norm(html_mod.unescape(re.sub(r"<[^>]+>", " ", text)))
        href = html_mod.unescape(href)
        candidates.append((href, text))
    for href, text in candidates:
        if any(p in text for p in patterns) and "vysvetleni" not in text:
            return urllib.parse.urljoin(page_url, href)
    if depth == 0:      # podstránka se sekcí dokumentů (NEN)
        for href, text in candidates:
            if "dokument" in text or "dokument" in _norm(href):
                found = _find_doc_link(
                    urllib.parse.urljoin(page_url, href), patterns, 1)
                if found:
                    return found
    return None


# ── extrakce textu ──────────────────────────────────────────────────────────

def _text_from(data: bytes, url: str) -> str:
    head = data[:8]
    if head.startswith(b"%PDF"):
        if PdfReader is None:
            raise RuntimeError("pypdf není nainstalováno")
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(
            (p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES])
    if head.startswith(b"PK"):   # docx nebo zip příloh
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "word/document.xml" in z.namelist():
                xml = z.read("word/document.xml").decode("utf-8", "replace")
                return re.sub(r"<[^>]+>", " ", xml)
            # zip: vezmi první pdf/docx s „zadavaci/zprava" v názvu, pak první
            names = sorted(
                (n for n in z.namelist()
                 if n.lower().endswith((".pdf", ".docx"))),
                key=lambda n: ("zadavaci" not in _norm(n)
                               and "zprava" not in _norm(n), len(n)))
            for n in names[:3]:
                try:
                    return _text_from(z.read(n), n)
                except Exception:  # noqa: BLE001
                    continue
            raise RuntimeError("v ZIPu není čitelné PDF/DOCX")
    if b"<html" in data[:2000].lower():
        return re.sub(r"<[^>]+>", " ", data.decode("utf-8", "replace"))
    return data.decode("utf-8", "replace")


# ── vytěžení polí ───────────────────────────────────────────────────────────

def _num(s: str) -> float | None:
    s = s.replace("\xa0", " ").replace(" ", "").replace(".", "") \
         .replace(",", ".")
    try:
        v = float(s)
        return v if v >= 10_000 else None
    except ValueError:
        return None


_RE_HODNOTA = re.compile(
    r"predpokladan[aeá]\s+hodnot[aey][^0-9]{0,120}?([\d][\d\s.,\xa0]{4,20})"
    r"\s*(?:kc|kč|czk|,-)", re.I)
_RE_MISTO = re.compile(
    r"mist[oe]m?\s+plneni[^:\n]{0,40}[:\s]\s*([^\n;]{3,140})", re.I)
_RE_TERMIN = re.compile(
    r"(?:doba|termin|lhut[ay])\s+(?:plneni|dokonceni|realizace|vystavby)"
    r"[^:\n]{0,40}[:\s]\s*([^\n;]{3,140})", re.I)
_RE_VITEZ = re.compile(
    r"(?:vybran[yý]m?\s+dodavatel(?:em)?|smlouv[au][^\n]{0,30}uzavren[aá]?\s*s)"
    r"[^:\n]{0,30}[:\s]\s*([^\n;,(]{3,100})", re.I)
# vítěz se bere jen když vypadá jako firma — regex jinak chytá útržky vět
_RE_FIRMA = re.compile(
    r"s\.?\s?r\.?\s?o|a\.?\s?s\.?($|\W)|spol\.|v\.?\s?o\.?\s?s"
    r"|druzstvo|holding|group|stavby|stavebni", re.I)
_RE_CENA = re.compile(
    r"(?:nabidkov[aá]|celkov[aá]|sjednan[aá]|smluvn[ií])\s+cen[aey]"
    r"[^0-9]{0,120}?([\d][\d\s.,\xa0]{4,20})\s*(?:kc|kč|czk|,-)", re.I)


def _mine_zd(text: str) -> dict:
    t = _norm(text)
    out = {}
    if m := _RE_HODNOTA.search(t):
        out["hodnota"] = _num(m.group(1))
    if m := _RE_MISTO.search(t):
        out["misto"] = " ".join(m.group(1).split())[:140]
    if m := _RE_TERMIN.search(t):
        out["termin"] = " ".join(m.group(1).split())[:140]
    return out


def _mine_zprava(text: str) -> dict:
    t = _norm(text)
    out = {}
    if (m := _RE_VITEZ.search(t)) and _RE_FIRMA.search(m.group(1)):
        out["vitez"] = " ".join(m.group(1).split())[:100]
    if m := _RE_CENA.search(t):
        out["cena"] = _num(m.group(1))
    return out


# ── dokumenty z XML profilů ─────────────────────────────────────────────────

def _profile_docs(profile_url: str) -> dict[str, list[tuple[str, str]]]:
    """id_objektu/id_nipez → [(typ, url)] za posledních 180 dní."""
    import xml.etree.ElementTree as ET
    do = dt.date.today()
    od = do - dt.timedelta(days=180)
    url = (profile_url.rstrip("/")
           + f"/XMLdataVZ?od={od:%d%m%Y}&do={do:%d%m%Y}")
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        root = ET.fromstring(_get(url))
    except Exception:  # noqa: BLE001
        return out
    strip = lambda t: t.split("}", 1)[-1].lower()  # noqa: E731
    for zak in root.iter():
        if strip(zak.tag) != "zakazka":
            continue
        ids = [el.text.strip() for el in zak.iter()
               if strip(el.tag) in ("id_objektu", "id_nipez")
               and (el.text or "").strip()]
        docs = []
        for d in zak.iter():
            if strip(d.tag) != "dokument":
                continue
            typ = href = ""
            for ch in d.iter():
                if strip(ch.tag) == "typ_dokumentu":
                    typ = _norm(ch.text or "")
                elif strip(ch.tag) == "url":
                    href = (ch.text or "").strip()
            if typ and href:
                docs.append((typ, href))
        for i in ids:
            out.setdefault(i, []).extend(docs)
    return out


def _doc_from_profile(t: dict, cache: dict, kinds: list[str]) -> str | None:
    """URL dokumentu daného typu z XML profilu (cache XML na profil/běh)."""
    src = t.get("source", "")
    base = None
    if src.startswith("profil:pvu:"):
        base = t.get("url")
    elif src.startswith("profil:"):
        meta = config.PROFILY_ZADAVATELU.get(src.removeprefix("profil:"))
        base = meta and meta["profile_url"]
    if not base:
        return None
    if base not in cache:
        cache[base] = _profile_docs(base)
    native = t["id"].split(":")[-1]
    for key in (native, t["id"].removeprefix("rvz:")):
        for typ, href in cache[base].get(key, []):
            if any(k in typ for k in kinds) and "vysvetleni" not in typ:
                return href
    return None


# ── hlavní běh ──────────────────────────────────────────────────────────────

def _load(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _process(items, store, kinds, patterns, miner, errors, xml_cache):
    done = 0
    # týž dokument nesmí patřit dvěma zakázkám — stává se, když je URL
    # záznamu jen obecná stránka profilu (první nalezená ZD by se chybně
    # přiřadila všem zakázkám téhož zadavatele)
    seen_urls = {v["doc_url"] for v in store.values() if v.get("doc_url")}
    for t in items:
        if done >= MAX_DOWNLOADS:
            break
        rid = t["id"]
        if rid in store:
            continue
        doc_url = _doc_from_profile(t, xml_cache, kinds) \
            or (_find_doc_link(t["url"], patterns) if t.get("url") else None)
        if not doc_url or doc_url in seen_urls:
            store[rid] = {"nenalezeno": True}
            continue
        seen_urls.add(doc_url)
        try:
            data = _get(doc_url)
            fields = miner(_text_from(data, doc_url))
            store[rid] = {"doc_url": doc_url, **fields}
            done += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"dokumenty {rid}: {exc}")
            store[rid] = {"doc_url": doc_url, "chyba": str(exc)[:120]}
            done += 1
    return done


def run() -> int:
    tenders = _load(ROOT / config.OUT_TENDERS, [])
    comp = _load(ROOT / config.OUT_COMPETITION, [])
    state = _load(OUT, {})
    zd, zpravy = state.get("zd", {}), state.get("zpravy", {})
    errors: list[str] = []
    xml_cache: dict = {}

    active = [t for t in tenders if not t.get("expired")]
    n1 = _process(active, zd, ["zadavaci dokumentace"],
                  ["zadavaci dokumentace", "zadavaci a kvalifikacni"],
                  _mine_zd, errors, xml_cache)
    n2 = _process(comp, zpravy, ["pisemna zprava"],
                  ["pisemna zprava"], _mine_zprava, errors, xml_cache)

    # úklid: id mimo aktuální data (zaniklé zakázky) se odmazávají
    keep_t = {t["id"] for t in tenders}
    keep_c = {c["id"] for c in comp}
    zd = {k: v for k, v in zd.items() if k in keep_t}
    zpravy = {k: v for k, v in zpravy.items() if k in keep_c}

    OUT.write_text(json.dumps({
        "meta": {
            "updated": dt.datetime.now(dt.timezone.utc)
            .isoformat(timespec="seconds"),
            "zd": len(zd), "zpravy": len(zpravy),
            "novych": n1 + n2, "errors": errors[:20],
        },
        "zd": zd, "zpravy": zpravy,
    }, ensure_ascii=False, indent=1), "utf-8")
    print(f"dokumenty: ZD {len(zd)} (+{n1}), zprávy {len(zpravy)} (+{n2}), "
          f"chyb {len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
