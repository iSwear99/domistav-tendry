"""NEN veřejné API — obohacení detailů zakázek (prohlídka, vysvětlení ZD).

Zákonné VZ z NEN jsou už v seznamu přes ISVZ Open Data; tento modul k nim
DOPLŇUJE detailová pole, která open data nemusí obsahovat:
- datum prohlídky místa plnění (`site_visit`)
- počet vysvětlení/doplnění ZD (`clarifications`)

Aktivace: modul běží jen tehdy, existují-li GitHub Actions secrets
NEN_CERT_PEM a NEN_KEY_PEM (klientský certifikát pro API) a je-li
v configu doplněno NEN_API_BASE. Do té doby se tiše přeskočí — aplikace
funguje i bez něj (změny ZD pak detekuje diff snapshotů z dostupných polí).

⚠️ TODO-OVERIT (CLAUDE.md): přesné endpointy a názvy polí NEN API dle
dokumentace obdržené po schválení žádosti o přístup. Níže je pouze kostra
s bezpečným zacházením s certifikátem.
"""
from __future__ import annotations

import os
import tempfile

import requests

import config


def _cert_paths() -> tuple[str, str] | None:
    cert, key = os.environ.get("NEN_CERT_PEM"), os.environ.get("NEN_KEY_PEM")
    if not cert or not key:
        return None
    cf = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    kf = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    cf.write(cert); cf.close()
    kf.write(key); kf.close()
    os.chmod(cf.name, 0o600)
    os.chmod(kf.name, 0o600)
    return cf.name, kf.name


def _nen_system_number(t: dict) -> str | None:
    """Systémové číslo NEN (N006/26/V…) — plní fetch_isvz do pole nen_id."""
    return t.get("nen_id") or None


def enrich(tenders: list[dict]) -> list[str]:
    """Doplní site_visit / clarifications k aktivním VZ z NEN.

    Vrací seznam chyb; mutuje předané záznamy. Bez secrets/API_BASE
    se vrací prázdný seznam (tichý přeskok, nejde o chybu)."""
    if config.NEN_API_BASE.startswith("TODO"):
        return []
    certs = _cert_paths()
    if certs is None:
        return []

    errors: list[str] = []
    candidates = [
        t for t in tenders
        if t["source"] == "isvz" and not t["expired"] and _nen_system_number(t)
    ][: config.NEN_MAX_DETAILS_PER_RUN]

    session = requests.Session()
    session.cert = certs
    session.headers["User-Agent"] = config.USER_AGENT

    for t in candidates:
        num = _nen_system_number(t)
        try:
            # TODO-OVERIT: endpoint detailu VZ dle dokumentace NEN API,
            # např. f"{config.NEN_API_BASE}/vz/{num}" — upravit po obdržení
            # dokumentace, včetně názvů polí v odpovědi.
            r = session.get(f"{config.NEN_API_BASE}/vz/{num}",
                            timeout=config.TIMEOUT)
            r.raise_for_status()
            detail = r.json()
            # TODO-OVERIT: mapování polí odpovědi
            if detail.get("prohlidkaMistaPlneni"):
                t["site_visit"] = str(detail["prohlidkaMistaPlneni"])
            vys = detail.get("vysvetleniZD")
            if isinstance(vys, list):
                t["clarifications"] = len(vys)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"nen:{num}: {exc}")

    for p in certs:
        try:
            os.unlink(p)
        except OSError:
            pass
    return errors
