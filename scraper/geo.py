"""Určení vzdálenosti zakázky od Hradce Králové (okruh dle configu).

Priorita určení polohy:
1. Obec z textu místa plnění, poté z názvu zakázky — číselník
   `scraper/obce.csv` (nazev;lat;lon, generováno z RÚIAN/ČÚZK pro kraje
   CZ052/CZ053/CZ020/CZ051/CZ063). Názvy se porovnávají i ve skloňovaných
   tvarech („v Týnci nad Labem" → Týnec nad Labem) přes množinu
   koncovkových variant — přesné tokenové shody, žádné prefixové hádání.
2. Sídlo okresu dle NUTS4 (CZ0521…) — ISVZ open data ale uvádějí NUTS
   jen na úrovni kraje, takže tato úroveň zafunguje zřídka.
3. Sídlo zadavatele u VZMR z profilů (souřadnice v configu).
4. Obec ze sídla zadavatele (pole `authority_seat` z ISVZ) — poslední
   vodítko; sídlo bývá shodné s místem plnění u obecních zakázek.

Nelze-li polohu určit, zakázka se PONECHÁVÁ s příznakem loc_unknown.
"""
from __future__ import annotations

import csv
import math
import pathlib
import unicodedata

import config

_HERE = pathlib.Path(__file__).parent

# Sídla okresů v dosahu úvahy (NUTS4 → lat, lon)
OKRES_SEATS = {
    "CZ0521": (50.209, 15.833),  # Hradec Králové
    "CZ0522": (50.437, 15.352),  # Jičín
    "CZ0523": (50.417, 16.163),  # Náchod
    "CZ0524": (50.163, 16.275),  # Rychnov nad Kněžnou
    "CZ0525": (50.561, 15.913),  # Trutnov
    "CZ0531": (49.951, 15.795),  # Chrudim
    "CZ0532": (50.038, 15.779),  # Pardubice
    "CZ0533": (49.756, 16.468),  # Svitavy
    "CZ0534": (49.974, 16.394),  # Ústí nad Orlicí
    "CZ0208": (50.186, 15.042),  # Nymburk
    "CZ0204": (50.028, 15.200),  # Kolín
    "CZ0205": (49.948, 15.268),  # Kutná Hora
    "CZ0514": (50.602, 15.335),  # Semily
    "CZ0631": (49.606, 15.579),  # Havlíčkův Brod
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    # interpunkce → mezery („žst. Pardubice" / uvozovky / závorky)
    return "".join(c if c.isalnum() else " " for c in s)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _variants(word: str) -> set[str]:
    """Skloňované tvary slova z názvu obce (bez diakritiky).

    Generuje se konečná množina tvarů — porovnává se pak přesná shoda
    tokenů, takže „mostu" (most) nikdy nespáruje obec Mostek apod.
    """
    v = {word}
    add = v.add
    if word.endswith("ec"):            # Týnec → Týnci, Kostelec → Kostelci
        add(word[:-2] + "ci"); add(word[:-2] + "ce"); add(word[:-2] + "cem")
    if word.endswith("ek"):            # Mostek → Mostku
        add(word[:-2] + "ku"); add(word[:-2] + "ka"); add(word[:-2] + "kem")
    if word.endswith("ce"):            # Pardubice → Pardubicích/-cím
        add(word + "mi"); add(word[:-1] + "ich"); add(word[:-1] + "im")
    if word.endswith("ka"):            # Paka → Pace, Bystřička → …
        add(word[:-2] + "ce"); add(word[:-1] + "u"); add(word[:-1] + "ou")
    if word.endswith("a"):             # Nová → Nové, Skuhrov?; Praha → Praze
        add(word[:-1] + "e"); add(word[:-1] + "y")
        add(word[:-1] + "u"); add(word[:-1] + "ou")
        if word.endswith("ha"):
            add(word[:-2] + "ze")      # Praha → Praze
    if word.endswith("o"):             # Hlinsko → Hlinsku/Hlinsce
        add(word[:-1] + "u"); add(word[:-1] + "a"); add(word[:-1] + "em")
        if word.endswith("ko"):
            add(word[:-2] + "ce")
    if word.endswith("y"):             # Svitavy → Svitavách/Svitavám
        add(word[:-1] + "ach"); add(word[:-1] + "am"); add(word[:-1] + "");
        add(word[:-1] + "ami")
    if word.endswith("i") or word.endswith("e"):
        add(word + "ch"); add(word + "m")
    if word.endswith("uv"):            # Špindlerův → Špindlerově, Králův → …
        add(word[:-2] + "ove"); add(word[:-2] + "ova"); add(word[:-2] + "ovu")
    if word == "dvur":                 # Dvůr → Dvoře/Dvora (kmenová změna)
        add("dvore"); add("dvora"); add("dvorem")
    if word[-1:] not in "aeiouy":      # souhláska: Náchod → Náchodě/-du/-dem
        add(word + "e"); add(word + "u"); add(word + "a"); add(word + "em")
    return v


_OBCE_INDEX: dict[str, list[tuple[list[set[str]], int, tuple[float, float]]]] | None = None


def _obce_index():
    """Index: první token (všechny varianty) → kandidátní obce.

    Kandidát = (varianty všech slov názvu, počet slov, souřadnice).
    """
    global _OBCE_INDEX
    if _OBCE_INDEX is not None:
        return _OBCE_INDEX
    _OBCE_INDEX = {}
    path = _HERE / "obce.csv"
    if not path.exists():
        return _OBCE_INDEX
    with path.open(encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter=";"):
            if len(row) < 3:
                continue
            try:
                coords = (float(row[1]), float(row[2]))
            except ValueError:
                continue
            words = _norm(row[0]).split()
            if not words or len(words[0]) < 3 and len(words) == 1:
                continue
            wvars = [_variants(w) if len(w) >= 3 else {w} for w in words]
            cand = (wvars, len(words), coords)
            for first in wvars[0]:
                _OBCE_INDEX.setdefault(first, []).append(cand)
    return _OBCE_INDEX


def _find_obec(text: str) -> tuple[float, float] | None:
    """Shoda názvu obce v textu.

    Preferuje se víceslovná shoda ([]přesnější); při stejném počtu slov
    vyhrává obec bližší centru — stejnojmenných obcí je v ČR řada a pro
    50km bránu je bezpečnější přiklonit se k interpretaci v regionu
    (vzdálená interpretace by zakázku chybně vyřadila)."""
    index = _obce_index()
    if not index:
        return None
    tokens = _norm(text).split()
    best: tuple[int, float, tuple[float, float]] | None = None
    for i, tok in enumerate(tokens):
        for wvars, nwords, coords in index.get(tok, ()):
            if i + nwords > len(tokens):
                continue
            if all(tokens[i + j] in wvars[j] for j in range(1, nwords)):
                d = haversine_km(config.GEO_CENTER, coords)
                if (best is None or nwords > best[0]
                        or (nwords == best[0] and d < best[1])):
                    best = (nwords, d, coords)
    return best[2] if best else None


def locate(t: dict, profil_coords: dict[str, tuple[float, float]]) -> float | None:
    """Vzdálenost od centra v km, nebo None (nelze určit)."""
    # 1) obec z místa plnění, poté z názvu zakázky
    for text in (t.get("place"), t.get("title")):
        if text:
            coords = _find_obec(text)
            if coords:
                return haversine_km(config.GEO_CENTER, coords)
    # 2) NUTS4 → sídlo okresu (ISVZ uvádí většinou jen kraj — viz docstring)
    for n in t.get("nuts") or []:
        seat = OKRES_SEATS.get(n[:6])
        if seat:
            return haversine_km(config.GEO_CENTER, seat)
    # 3) sídlo zadavatele u profilových VZMR (souřadnice v configu)
    if t["source"].startswith("profil:"):
        coords = profil_coords.get(t["source"].removeprefix("profil:"))
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    # 4) obec ze sídla zadavatele (ISVZ)
    seat_text = t.get("authority_seat")
    if seat_text:
        coords = _find_obec(seat_text)
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    return None


def apply_radius(tenders: list[dict]) -> list[dict]:
    """dist_km + loc_unknown; zakázky nad limit se zahazují."""
    profil_coords = {
        key: (meta["lat"], meta["lon"])
        for key, meta in config.PROFILY_ZADAVATELU.items()
        if "lat" in meta and "lon" in meta
    }
    kept: list[dict] = []
    for t in tenders:
        dist = locate(t, profil_coords)
        if dist is None:
            t["dist_km"] = None
            t["loc_unknown"] = True
            if config.GEO_KEEP_UNKNOWN:
                kept.append(t)
            continue
        if dist <= config.GEO_RADIUS_KM:
            t["dist_km"] = round(dist)
            t["loc_unknown"] = False
            kept.append(t)
    return kept
