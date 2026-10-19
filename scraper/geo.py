"""Určení vzdálenosti zakázky od Hradce Králové (okruh dle configu).

Priorita určení polohy:
1. Obec z explicitního místa plnění — číselník `scraper/obce.csv`
   (nazev;lat;lon, celá ČR). Názvy se porovnávají i ve skloňovaných
   tvarech („v Týnci nad Labem" → Týnec nad Labem) přes množinu
   koncovkových variant — přesné tokenové shody, žádné prefixové hádání.
2. Obec ze JMÉNA zadavatele u municipálních zadavatelů („Město X",
   „Obec X", „statutární město X", „městská část/obvod X") — shoda
   musí pokrýt celý zbytek jména; u části/obvodu nejdelší prefix
   (mateřské město). PŘED názvem zakázky (19. 10. 2026): obec staví
   na svém území a fuzzy shoda v titulu umí lhát.
3. Obec z názvu zakázky.
4. Sídlo okresu dle NUTS4 (CZ0521…) — ISVZ open data ale uvádějí NUTS
   jen na úrovni kraje, takže tato úroveň zafunguje zřídka.
5. Obec ze sídla zadavatele (`authority_seat` — z ISVZ i z detailu
   E-ZAK profilu) — sídlo bývá shodné s místem plnění obecních zakázek.
6. Sídlo zadavatele u VZMR z profilů (souřadnice v configu) — NIKDY
   pro profily s "multi": True (agenturní E-ZAK s cizími zadavateli).

Nelze-li polohu určit, zakázka se PONECHÁVÁ s příznakem loc_unknown.
"""
from __future__ import annotations

import csv
import math
import pathlib
import re
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


# „Město X" / „Obec X" / „statutární město X" / „městská část Praha-X" /
# „městský obvod X" — jméno municipálního zadavatele je spolehlivé určení
# obce; na jiná jména zadavatelů se záměrně nesahá (firma pojmenovaná
# po obci by zakázku chybně vyřadila).
_MUNICIPAL_RE = re.compile(
    r"^(?:statutarni\s+)?(mestska\s+cast|mestsky\s+obvod"
    r"|mesto|mestys|obec)\s+(.+)$")


def _obec_z_nazvu(rest: str, prefix_ok: bool) -> tuple[float, float] | None:
    """Obec ze zbytku jména zadavatele. Shoda musí pokrýt CELÝ zbytek —
    částečná shoda uprostřed lže („Praha-Velká Chuchle" spárovala obec
    Velká u HK, zjištěno 19. 10. 2026). U městské části/obvodu
    (prefix_ok) se zkouší nejdelší PREFIX: mateřské město stojí na
    začátku („Praha-Satalice" → Praha, „Ústí nad Labem-Střekov" →
    Ústí nad Labem, „Pardubice I" → Pardubice)."""
    tokens = _norm(rest).split()
    index = _obce_index()
    if not tokens or not index:
        return None
    delky = range(len(tokens), 0, -1) if prefix_ok else (len(tokens),)
    for k in delky:
        best: tuple[float, tuple[float, float]] | None = None
        for wvars, nwords, coords in index.get(tokens[0], ()):
            if nwords != k:
                continue
            if all(tokens[j] in wvars[j] for j in range(1, k)):
                d = haversine_km(config.GEO_CENTER, coords)
                if best is None or d < best[0]:
                    best = (d, coords)
        if best:
            return best[1]
    return None


def locate(t: dict, profil_coords: dict[str, tuple[float, float]]) -> float | None:
    """Vzdálenost od centra v km, nebo None (nelze určit)."""
    # 1) obec z explicitního místa plnění
    if t.get("place"):
        coords = _find_obec(t["place"])
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    # 2) obec ze jména MUNICIPÁLNÍHO zadavatele („Město Mnichovo
    #    Hradiště") — PŘED názvem zakázky: obec staví na svém území
    #    a fuzzy shoda v titulu umí lhát („…přes potok Vrutici"
    #    spárovalo obec Vrutice, 19. 10. 2026)
    auth = " ".join(_norm(t.get("authority") or "").split())
    m = _MUNICIPAL_RE.match(auth)
    if m:
        coords = _obec_z_nazvu(
            m.group(2), prefix_ok=m.group(1) in ("mestska cast",
                                                 "mestsky obvod"))
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    # 3) obec z názvu zakázky
    if t.get("title"):
        coords = _find_obec(t["title"])
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    # 4) NUTS4 → sídlo okresu (ISVZ uvádí většinou jen kraj — viz docstring)
    for n in t.get("nuts") or []:
        seat = OKRES_SEATS.get(n[:6])
        if seat:
            return haversine_km(config.GEO_CENTER, seat)
    # 5) obec ze sídla zadavatele (ISVZ i E-ZAK detail) — před souřadnicemi
    #    profilu z configu: sídlo je per záznam, config per profil
    seat_text = t.get("authority_seat")
    if seat_text:
        coords = _find_obec(seat_text)
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    # 6) sídlo zadavatele u profilových VZMR (souřadnice v configu)
    if t["source"].startswith("profil:"):
        coords = profil_coords.get(t["source"].removeprefix("profil:"))
        if coords:
            return haversine_km(config.GEO_CENTER, coords)
    return None


def _profil_coords() -> dict[str, tuple[float, float]]:
    """Souřadnice konfigurovaných profilů — BEZ agenturních (multi):
    tam sedí na profilu cizí zadavatelé a config by lhal."""
    return {
        key: (meta["lat"], meta["lon"])
        for key, meta in config.PROFILY_ZADAVATELU.items()
        if "lat" in meta and "lon" in meta and not meta.get("multi")
    }


def apply_radius(tenders: list[dict]) -> tuple[list[dict], set[str]]:
    """dist_km + loc_unknown; zakázky nad limit se zahazují.

    Vrací (ponechané, id vyřazených nad limit) — vyřazená id potřebuje
    main.py, aby je trvalá retence neoživila ze starého snapshotu."""
    profil_coords = _profil_coords()
    kept: list[dict] = []
    dropped: set[str] = set()
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
        else:
            dropped.add(t["id"])
    return kept, dropped


def relocate_unknown(tenders: list[dict]) -> list[dict]:
    """Doplňkové určení polohy záznamů s loc_unknown (archiv/retence).

    Zpřesněná lokalizace (sídlo/jméno zadavatele) smí zpětně vyřadit
    záznam mimo okruh — nad limit se zahazuje odjakživa, tyhle prošly
    jen dírou v určování polohy. Záznamy s už určenou polohou se
    nepřepočítávají (jejich vstupy se nemění)."""
    kept: list[dict] = []
    profil_coords = _profil_coords()
    for t in tenders:
        if not t.get("loc_unknown"):
            kept.append(t)
            continue
        dist = locate(t, profil_coords)
        if dist is None:
            kept.append(t)
        elif dist <= config.GEO_RADIUS_KM:
            t["dist_km"] = round(dist)
            t["loc_unknown"] = False
            kept.append(t)
    return kept
