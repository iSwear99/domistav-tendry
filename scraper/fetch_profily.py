"""VZMR z profilů zadavatelů — XML rozhraní profilů.

Ověřeno 2026-07-28 živě na E-ZAK, NEN, Tender areně, PVU, Gordionu a KDV:
endpoint {profile_url}/XMLdataVZ?od=DDMMYYYY&do=DDMMYYYY vrací XML
s namespace urn:cz:isvz:mmr:schemas:vz-z-profilu-zadavatele:v100.
Struktura: profil > zakazka > (id_objektu, id_nipez?, nazev_vz, druh_vz,
rezim_vz, hlavni_cpv, casti_vz > cast_zakazky > zadavaci_postup_casti >
(stav_zadavaciho_postupu, datum_uverejneni, lhuty_zadavaciho_postupu >
lhuta > (druh_lhuty, datum_konce_lhuty), dokumenty…)).
Předpokládaná hodnota se v XML profilů prakticky nevyskytuje (no_value).
NEN profily vracejí id_nipez = identifikator_NIPEZ z ISVZ ⇒ společné
stabilní ID `rvz:<NIPEZ>` a přesná deduplikace napříč zdroji.
"""
from __future__ import annotations

import datetime as dt
import re
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

import config

TAGY = {
    "zakazka": "zakazka",
    "id": "id_objektu",
    "nipez": "id_nipez",
    "title": "nazev_vz",
    "rezim": "rezim_vz",
    "cpv": ("hlavni_cpv", "hlavni_cpv_casti_vz"),
    "published": "datum_uverejneni",
    "state": "stav_zadavaciho_postupu",
    "lhuta": "lhuta",
    "druh_lhuty": "druh_lhuty",
    "konec_lhuty": "datum_konce_lhuty",
}
# druh_lhuty se porovnává bez diakritiky; bere se lhůta podání (před)nabídky
_DEADLINE_RE = re.compile(r"podani (predbezne )?nabid")

# NEN id_objektu N006/26/V00020667 → detail
# https://nen.nipez.cz/verejne-zakazky/detail-zakazky/N006-26-V00020667
_NEN_ID_RE = re.compile(r"^N\d{3}/\d{2}/V\d+$")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _get(url: str) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(config.RETRIES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": config.USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=config.TIMEOUT) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Stažení selhalo: {url}: {last_exc}")


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def _texts(el: ET.Element, tag: str) -> list[str]:
    return [
        (child.text or "").strip()
        for child in el.iter()
        if _strip_ns(child.tag) == tag and (child.text or "").strip()
    ]


def _first(el: ET.Element, tag: str) -> str:
    vals = _texts(el, tag)
    return vals[0] if vals else ""


def _deadline(el: ET.Element) -> str:
    """Konec lhůty podání (před)nabídky; při více částech nejpozdější."""
    ends: list[str] = []
    for lh in el.iter():
        if _strip_ns(lh.tag) != TAGY["lhuta"]:
            continue
        druh = _norm(_first(lh, TAGY["druh_lhuty"]))
        if _DEADLINE_RE.search(druh):
            konec = _first(lh, TAGY["konec_lhuty"])
            if konec:
                ends.append(konec)
    return max(ends)[:19] if ends else ""


def _detail_url(rid: str, base: str) -> str:
    if _NEN_ID_RE.match(rid):
        return ("https://nen.nipez.cz/verejne-zakazky/detail-zakazky/"
                + rid.replace("/", "-"))
    return base


def _xml_url(meta: dict, od: dt.date, do: dt.date) -> str:
    tpl = meta.get("xml_url")
    if tpl:
        return tpl.format(od=f"{od:%d%m%Y}", do=f"{do:%d%m%Y}")
    base = meta["profile_url"].rstrip("/")
    return f"{base}/XMLdataVZ?od={od:%d%m%Y}&do={do:%d%m%Y}"


def _fetch_profile(key: str, meta: dict) -> tuple[list[dict], list[str]]:
    do = dt.date.today()
    od = do - dt.timedelta(days=config.PROFILY_DAYS_BACK)
    url = _xml_url(meta, od, do)
    try:
        root = ET.fromstring(_get(url))
    except Exception as exc:  # noqa: BLE001
        return [], [f"profil:{key}: {exc}"]

    tenders: list[dict] = []
    for el in root.iter():
        if _strip_ns(el.tag) != TAGY["zakazka"]:
            continue
        rid = _first(el, TAGY["id"])
        title = _first(el, TAGY["title"])
        if not rid or not title:
            continue
        nipez = _first(el, TAGY["nipez"])
        cpv: list[str] = []
        for tag in TAGY["cpv"]:
            for c in _texts(el, tag):
                if c and c != "00000000" and c not in cpv:
                    cpv.append(c)
        published = min(_texts(el, TAGY["published"]), default="")[:10]
        rezim = _norm(_first(el, TAGY["rezim"]))
        vzmr = "maleho rozsahu" in rezim if rezim else True
        tenders.append({
            "id": f"rvz:{nipez}" if nipez else f"profil:{key}:{rid}",
            "source": f"profil:{key}",
            "title": title,
            "authority": meta["nazev"],
            "authority_ico": meta["ico"],
            "cpv": cpv,
            "nuts": [],  # profil NUTS neuvádí — poloha = sídlo zadavatele
            "value": None,  # předpokládaná hodnota v XML profilů není
            "published": published,
            "deadline": _deadline(el),
            "url": _detail_url(rid, meta["profile_url"]),
            "place": "",
            "state": _first(el, TAGY["state"]),
            "site_visit": "",
            "clarifications": 0,
            "kind": "VZMR" if vzmr else "VZ",
        })
    return tenders, []


def fetch() -> tuple[list[dict], list[str]]:
    tenders: list[dict] = []
    errors: list[str] = []
    for key, meta in config.PROFILY_ZADAVATELU.items():
        if meta["profile_url"].startswith("TODO"):
            errors.append(f"profil:{key}: URL profilu nevyplněno")
            continue
        t, e = _fetch_profile(key, meta)
        tenders.extend(t)
        errors.extend(e)
    return tenders, errors
