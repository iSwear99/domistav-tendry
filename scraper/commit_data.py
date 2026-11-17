"""Commit + push datových souborů, odolný vůči souběhu workflow.

Použití:  python scraper/commit_data.py --uklid      (krok hned po checkoutu)
          python scraper/commit_data.py "zpráva commitu" <cesta> [<cesta>…]

Proč samostatný skript (incident 19. 10. – 17. 11. 2026): workflow
commitovaly přes `git pull --rebase -X theirs`. Konflikt typu „soubor
smazán × změněn" (sken_meta.json) ale -X theirs nevyřeší, rebase zůstal
rozpracovaný v .git/rebase-merge a self-hosted pracovní adresář ho
přenášel do dalších běhů — 4 týdny každý PC běh hodinu počítal a pak
nic neuložil. Skript proto:
1. režim --uklid (hned po checkoutu, ještě bez spočítaných dat) zruší
   zaseknutý rebase/merge i s pracovním adresářem; vlastní commit pak
   na startu volá jen nedestruktivní `--quit` — `--abort` by vrátil
   pracovní adresář a tiše zahodil právě spočítaná data (ověřeno testem),
2. zkusí rebase na origin/main (-X theirs = při konfliktu vyhrává
   čerstvý běh),
3. když rebase selže z jakéhokoli důvodu, NIKDY ho nenechá viset:
   abort a záložní cesta — soubory, které TENTO běh změnil, se položí
   na aktuální origin/main (ostatní soubory zůstávají z main, takže
   souběžný commit jiného workflow se nepřemaže),
4. push opakuje až 4× (souběžné běhy cloud/PC/smlouvy).
"""
from __future__ import annotations

import subprocess
import sys


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def _uklid() -> None:
    """Destruktivní: zruší rozpracovaný rebase/merge VČ. pracovního
    adresáře. Jen na startu jobu nebo pro rebase, který spustil tento
    skript (vlastní commit je bezpečně uložený v `mine`)."""
    git("rebase", "--abort", check=False)
    git("merge", "--abort", check=False)


def _zapomen_rozpracovane() -> None:
    """Nedestruktivní: zahodí jen stav rebase/merge, soubory nechá."""
    git("rebase", "--quit", check=False)
    git("merge", "--quit", check=False)


def _poloz_na_main(mine: str, changed: list[str], msg: str) -> None:
    """Záložní cesta: změněné soubory běhu nad aktuální origin/main."""
    git("reset", "--hard", "origin/main")
    for path in changed:
        if git("cat-file", "-e", f"{mine}:{path}", check=False).returncode == 0:
            git("checkout", mine, "--", path)
        else:                           # běh soubor smazal
            git("rm", "-q", "--ignore-unmatch", "--", path)
    git("add", "--", *changed)
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        git("commit", "-q", "-m", msg)


# Kumulativní archivy: souběžné běhy (cloud × PC v 8:00) si je jinak
# přepisují celé — -X theirs vezme verzi pozdějšího běhu a verdikty
# posouzené souběžným během zmizí (17. 11.: 555 → 532, znovu by se
# platily tokeny). Klíče z origin/main, které tento běh nemá, se proto
# doplní. Jen tam, kde přidání nemůže porušit pravidlo souboru
# (dokumenty.json ne — dokument smí patřit jen jedné zakázce).
_SJEDNOTIT = {"docs/data/ai_filter.json": "verdikty"}


def _sjednot(changed: list[str], msg: str) -> None:
    import json
    from pathlib import Path
    zmena = False
    for path, sekce in _SJEDNOTIT.items():
        if path not in changed or not Path(path).exists():
            continue
        r = git("show", f"origin/main:{path}", check=False)
        if r.returncode != 0:
            continue
        try:
            jejich = json.loads(r.stdout).get(sekce) or {}
            data = json.loads(Path(path).read_text("utf-8"))
        except (ValueError, AttributeError):
            continue
        moje = data.setdefault(sekce, {})
        chybi = {k: v for k, v in jejich.items() if k not in moje}
        if not chybi:
            continue
        moje.update(chybi)
        meta = data.get("meta")
        if isinstance(meta, dict) and sekce == "verdikty":
            counts: dict = {"ano": 0, "ne": 0, "nejisto": 0}
            for v in moje.values():
                counts[v.get("verdikt")] = counts.get(v.get("verdikt"), 0) + 1
            meta["celkem"], meta["counts"] = len(moje), counts
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              "utf-8")
        git("add", "--", path)
        zmena = True
        print(f"{path}: doplněno {len(chybi)} záznamů ze souběžného běhu")
    if zmena and git("diff", "--cached", "--quiet", check=False).returncode:
        vlastni = (git("rev-parse", "HEAD").stdout
                   != git("rev-parse", "origin/main").stdout)
        git("commit", "-q", *(["--amend", "--no-edit"] if vlastni
                              else ["-m", msg]))


def main(msg: str, paths: list[str]) -> int:
    _zapomen_rozpracovane()
    git("config", "user.name", "domistav-tendry-bot")
    git("config", "user.email", "actions@users.noreply.github.com")
    git("add", "-A", "--", *paths)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Beze změn.")
        return 0
    git("commit", "-q", "-m", msg)
    mine = git("rev-parse", "HEAD").stdout.strip()
    changed = git("diff", "--name-only", f"{mine}~1", mine).stdout.split()

    for pokus in range(1, 5):
        git("fetch", "-q", "origin", "main")
        if git("rebase", "-X", "theirs", "origin/main",
               check=False).returncode != 0:
            _uklid()
            print(f"pokus {pokus}: rebase selhal — záložní cesta "
                  f"({len(changed)} souborů nad origin/main)")
            _poloz_na_main(mine, changed, msg)
        _sjednot(changed, msg)
        if git("push", "origin", "HEAD:main", check=False).returncode == 0:
            print(f"Pushnuto (pokus {pokus}): {msg}")
            return 0
        print(f"pokus {pokus}: push odmítnut (souběžný commit) — znovu")
    _uklid()
    print("CHYBA: push se nepodařil ani na 4. pokus.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--uklid"]:
        _uklid()
        print("Úklid gitu: případný rozpracovaný rebase/merge zrušen.")
        sys.exit(0)
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2:]))
