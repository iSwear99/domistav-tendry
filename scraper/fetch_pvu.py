"""Čerstvé zakázky z RSS Portálu vhodného uveřejnění (PVU).

Zaceluje slepé místo pokrytí: zakázky z běžícího měsíce od zadavatelů
mimo PROFILY_ZADAVATELU (církve, spolky, malé obce, dotovaní zadavatelé…)
se v ISVZ open datech objeví až s měsíčním exportem (~1.–5. dne dalšího
měsíce). PVU je největší profilová platforma a publikuje veřejné RSS
nových zakázek (ověřeno 2026-07-29: ~25 položek/den, celá ČR, TTL 15 min).

Tok: RSS → detail zakázky (HTML: IČO, odkaz na profil, sídlo zadavatele)
→ XMLdataVZ daného profilu (týž parser jako fetch_profily) → standardní
filtry v main.py. Zadavatelé už pokrytí konfigurací se přeskakují.
Co RSS přeteče (nárazová špička), dožene měsíční ISVZ export zpětně.
"""
from __future__ import annotations

import html as html_mod
import re
import urllib.parse
import xml.etree.ElementTree as ET

import config
import fetch_profily

# IČO v tabulce detailu („IČO" i starší „IČ"), s tolerancí mezer v čísle
_ICO_RE = re.compile(
    r"<th>\s*IČ[O0]?\s*</th>\s*<td[^>]*>\s*([\d ]{8,11})", re.I
)
_PROFIL_RE = re.compile(
    r'href="(https://www\.vhodne-uverejneni\.cz/profil/[^"]+)"'
    r'[^>]*title="([^"]*)"'
)
# sídlo zadavatele (řádek „Adresa" v sekci Informace o zadavateli)
_ADRESA_RE = re.compile(
    r"<th>\s*Adresa(?:\s+sídla)?\s*</th>\s*<td[^>]*>([^<]+)", re.I
)
# název zadavatele z H1 stránky profilu — title atribut odkazu na detailu
# zakázky bývá i generický text („URL detailu veřejné zakázky", případ
# Mladá Boleslav 14. 8. 2026), H1 profilu je autoritativní
_H1_NAZEV_RE = re.compile(
    r"<h1[^>]*>\s*Profil zadavatele:\s*(.*?)</h1>", re.S | re.I
)


def _profile_name(profile_url: str) -> str:
    try:
        page = fetch_profily._get(profile_url).decode("utf-8", "replace")
        m = _H1_NAZEV_RE.search(page)
        if m:
            return html_mod.unescape(
                re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _parse_detail(url: str) -> dict | None:
    """Z detailu zakázky vytáhne zadavatele; None = nepodařilo se."""
    page = fetch_profily._get(url).decode("utf-8", "replace")
    ico_m = _ICO_RE.search(page)
    prof_m = _PROFIL_RE.search(page)
    if not ico_m or not prof_m:
        return None
    adresa_m = _ADRESA_RE.search(page)
    return {
        "ico": ico_m.group(1).replace(" ", "").zfill(8),
        "profile_url": prof_m.group(1),
        "nazev": html_mod.unescape(prof_m.group(2)).strip(),
        "seat": html_mod.unescape(adresa_m.group(1)).strip()
        if adresa_m else "",
    }


def fetch() -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    try:
        root = ET.fromstring(fetch_profily._get(config.PVU_RSS_URL))
    except Exception as exc:  # noqa: BLE001
        return [], [f"pvu: RSS nedostupné: {exc}"]

    known_icos = {m["ico"] for m in config.PROFILY_ZADAVATELU.values()}
    profily: dict[str, dict] = {}
    for item in root.iterfind(".//item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        try:
            info = _parse_detail(link)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pvu: {urllib.parse.urlparse(link).path}: {exc}")
            continue
        if not info:
            continue  # detail bez IČO/profilu (např. neveřejný náhled)
        if info["ico"] in known_icos:
            continue  # zadavatele už kryje fetch_profily
        profily.setdefault(info["ico"], info)

    tenders: list[dict] = []
    for ico, info in profily.items():
        info["nazev"] = _profile_name(info["profile_url"]) or info["nazev"]
        t, e = fetch_profily._fetch_profile(f"pvu:{ico}", {
            "nazev": info["nazev"],
            "ico": ico,
            "profile_url": info["profile_url"],
        })
        for rec in t:
            rec["authority_seat"] = info["seat"]  # geo fallback přes obec
        tenders.extend(t)
        errors.extend(e)
    return tenders, errors
