"""AI filtr relevance zakázek (Claude API, pokyn 29. 7. 2026).

Denně po scraperu posoudí Claude (claude-opus-5) zakázky, které ještě
nemají verdikt: "ano" / "ne" / "nejisto" + krátké zdůvodnění. Chytá,
co strojové CPV/klíčové třídění chytit neumí (kontext názvu, typ
zadavatele, kombinace oborů). Verdikty se ukládají do ai_filter.json
a UI verdikt "ne" pouze SKRÝVÁ — nic se nemaže, přepínač vše zobrazí.

PLNÁ AUTOMATIKA: bez ANTHROPIC_API_KEY (GitHub secret) nebo bez
nainstalovaného SDK modul tiše skončí s exit 0 — denní data tím nikdy
nespadnou. Jednou posouzené id se znovu neposílá (cache verdiktů).

Náklady drží AI_MAX_PER_RUN a prompt caching (stабilní systémový prompt
s cache_control; dávky v rámci běhu čtou z cache).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import config  # noqa: E402

try:
    from anthropic import Anthropic
except ImportError:          # SDK chybí — tichý přeskok, ne pád
    Anthropic = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / config.OUT_DIR / "ai_filter.json"

SCHEMA = {
    "type": "object",
    "properties": {
        "verdikty": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdikt": {"type": "string",
                                "enum": ["ano", "ne", "nejisto"]},
                    "duvod": {"type": "string"},
                },
                "required": ["id", "verdikt", "duvod"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdikty"],
    "additionalProperties": False,
}


def _system_prompt() -> str:
    """Profil firmy + skutečná historie prací z Registru smluv (97 smluv
    od 2016 vytěžených 29. 7. 2026) + případná dodatečná pravidla.
    Text je stabilní ⇒ celý se kešuje (prompt caching)."""
    parts = [config.AI_PROFILE]
    hist = pathlib.Path(__file__).parent / "historie_praci.txt"
    if hist.exists():
        parts.append(
            "\nHISTORIE SKUTEČNĚ REALIZOVANÝCH ZAKÁZEK FIRMY (rok: předmět "
            "smlouvy z Registru smluv) — ber ji jako nejsilnější vodítko "
            "toho, co je pro firmu relevantní:\n" + hist.read_text("utf-8"))
    if config.AI_EXTRA:
        parts.append("\nDalší pravidla zadavatele:\n" + config.AI_EXTRA)
    return "\n".join(parts)


def _classify(client, batch: list[dict]) -> list[dict]:
    """Jedna dávka zakázek → verdikty (structured output, validní JSON)."""
    response = client.messages.create(
        model=config.AI_MODEL,
        max_tokens=16000,
        system=[{
            "type": "text",
            "text": _system_prompt(),
            "cache_control": {"type": "ephemeral"},
        }],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": "Posuď relevanci těchto zakázek:\n"
            + json.dumps(batch, ensure_ascii=False),
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model požadavek odmítl (safety)")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["verdikty"]


def _load(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return default


def run() -> int:
    if Anthropic is None:
        print("AI filtr: SDK anthropic není nainstalováno — přeskakuji.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("AI filtr: ANTHROPIC_API_KEY není nastaven — přeskakuji "
              "(nastavte GitHub secret pro zapnutí).")
        return 0

    tenders = _load(ROOT / config.OUT_TENDERS, [])
    state = _load(OUT, {})
    verdikty: dict = state.get("verdikty", {})

    todo = [t for t in tenders
            if not t.get("expired") and t["id"] not in verdikty]
    todo = todo[: config.AI_MAX_PER_RUN]

    client = Anthropic()
    errors: list[str] = []
    done = 0
    for i in range(0, len(todo), config.AI_BATCH):
        batch = [{
            "id": t["id"],
            "nazev": t["title"],
            "zadavatel": t["authority"],
            "cpv": t.get("cpv") or [],
            "hodnota_bez_dph": t.get("value"),
        } for t in todo[i:i + config.AI_BATCH]]
        try:
            for v in _classify(client, batch):
                if v["id"] in {b["id"] for b in batch}:
                    verdikty[v["id"]] = {"verdikt": v["verdikt"],
                                         "duvod": v["duvod"][:120]}
                    done += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"AI dávka {i // config.AI_BATCH}: {exc}")
            if len(errors) >= 3:
                break            # opakovaná chyba (kredit, síť) — nedřít dál

    # úklid verdiktů zakázek, které už v datech nejsou
    known = {t["id"] for t in tenders}
    verdikty = {k: v for k, v in verdikty.items() if k in known}

    counts = {"ano": 0, "ne": 0, "nejisto": 0}
    for v in verdikty.values():
        counts[v["verdikt"]] = counts.get(v["verdikt"], 0) + 1
    OUT.write_text(json.dumps({
        "meta": {
            "updated": dt.datetime.now(dt.timezone.utc)
            .isoformat(timespec="seconds"),
            "model": config.AI_MODEL,
            "posouzeno_nyni": done,
            "celkem": len(verdikty),
            "counts": counts,
            "errors": errors,
        },
        "verdikty": verdikty,
    }, ensure_ascii=False, indent=1), "utf-8")
    print(f"AI filtr: +{done} verdiktů (celkem {len(verdikty)}: "
          f"{counts}), chyb {len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
