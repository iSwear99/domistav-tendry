"""Konfigurace DOMISTAV Tendry — jediné místo, kde se mění parametry filtrace.

⚠️ Položky označené TODO-OVERIT je nutné ověřit proti reálným datům
(viz CLAUDE.md, sekce Povinné úvodní úkoly).
"""

# ── Filtrace ────────────────────────────────────────────────────────────────
CPV_PREFIXES = ["45"]                 # stavební práce
# NUTS je jen HRUBÝ PŘEDFILTR (kraje, jejichž část leží v okruhu) —
# finální výběr dělá okruh GEO_RADIUS_KM od Hradce Králové (geo.py).
NUTS_ALLOWED = ["CZ052", "CZ053", "CZ020", "CZ051", "CZ063"]
KEEP_MISSING_NUTS = True              # zakázky bez NUTS ponechat s příznakem
MIN_VALUE_CZK = 2_000_000             # bez DPH
KEEP_MISSING_VALUE = True             # bez hodnoty ponechat s příznakem no_value

# ── Geografický okruh ───────────────────────────────────────────────────────
GEO_CENTER = (50.2092, 15.8328)       # Hradec Králové
GEO_RADIUS_KM = 50                    # zakázky dál se zahazují
GEO_KEEP_UNKNOWN = True               # neurčitelná poloha: ponechat + příznak

# ── Klíčová slova (záchytná síť vedle CPV) ──────────────────────────────────
# Zakázka bez CPV 45 projde, pokud název obsahuje pozitivní výraz a žádný
# negativní. Porovnává se bez diakritiky a velikosti písmen, podřetězcem —
# „zateplen" pokryje zateplení/zateplené/zateplování. Záznam dostane příznak
# kw_match (štítek „dle názvu" v UI). CPV 45 negativní slova NEpřebíjí.
KEYWORDS_POSITIVE = [
    # výstavba a její formy
    "novostavb", "vystavb", "výstavb", "dostavb", "pristavb", "přístavb",
    "nastavb", "nástavb", "vestavb", "vybudovani", "vybudování",
    "zhotovitel stavby", "zhotovitel dila", "zhotovitel díla",
    # rekonstrukce a úpravy
    "rekonstrukc", "modernizac", "revitalizac", "regenerac", "adaptac",
    "stavebni uprav", "stavební úprav", "stavebni prac", "stavební prác",
    "udrzovaci prac", "udržovací prác", "oprav",
    # energetika budov
    "zateplen", "snizeni energeticke narocnosti", "snížení energetické náročnosti",
    "energeticke uspor", "energetické úspor", "energeticky usporna opatreni",
    # konstrukce a sanace
    "sanac", "statick", "vymena oken", "výměna oken", "vymena strech",
    "výměna střech", "stresni krytin", "střešní krytin", "fasad", "fasád",
    "demolic", "odstraneni stavby", "odstranění stavby",
    "bezbarierov", "bezbariérov", "pudni vestavb", "půdní vestavb",
    # inženýrské stavby (širší záběr dle zadání)
    "kanalizac", "vodovod", "cistirna odpadnich vod", "čistírna odpadních vod",
    "chodnik", "chodník", "zpevnene ploch", "zpevněné ploch",
    "parkovist", "parkovišt", "sportovni hriste", "sportovní hřiště",
]
KEYWORDS_NEGATIVE = [
    # projekční a dozorová činnost (není realizace)
    "projektova dokumentac", "projektová dokumentac", "projektove prac",
    "projektové prác", "zpracovani pd", "zpracování pd", "pd ", "studie",
    "technicky dozor", "technický dozor", "autorsky dozor", "autorský dozor",
    "koordinator bozp", "koordinátor bozp", "inzenyrska cinnost",
    "inženýrská činnost", "administrace", "energeticky audit", "energetický audit",
    # zjevně nestavební obory
    "informacniho systemu", "informačního systému", "software", "hardware",
    " ict", "vozidl", "automobil", "vozovy park", "vozový park",
    "nabytk", "nábytk", "uklidove sluzby", "úklidové služby",
    "secen", "sečen", "udrzba zelene", "údržba zeleně",
]

# ── HTTP ────────────────────────────────────────────────────────────────────
USER_AGENT = (
    "DOMISTAV-Tendry/1.0 (interni monitoring verejnych zakazek; "
    "kontakt: domistav@domistav.cz)"
)
TIMEOUT = 60
RETRIES = 3
# ISVZ ZIPy (~34 MB) jdou z GitHub runnerů výrazně pomaleji než z ČR —
# první nasazený běh s TIMEOUT=60 vytimeoutoval na všech souborech.
ISVZ_TIMEOUT = 600

# ── Zdroj 1: ISVZ Open Data ─────────────────────────────────────────────────
# OVĚŘENO 2026-07-28 proti VZ-06-2026.zip a dokumentaci 2.9.0 (swagger.json):
# – měsíční ZIP exporty kategorie „VZ", uvnitř jeden JSON (UTF-8) s obálkou
#   {obdobi_od, obdobi_do, verze, data: [{verejna_zakazka, historie_lhut,
#   zdroj_dat}]}; zdroje: VVZ, NEN, Tender arena, TENDERMARKET.
# – Soubor za měsíc M se publikuje až PO skončení měsíce (cca 1.–5. dne M+1);
#   za běžící měsíc vrací server 404 → fetch_isvz jej tiše přeskakuje.
#   Čerstvé zakázky proto primárně pokrývají profily zadavatelů (zdroj 2).
ISVZ_INDEX_URL = "https://isvz.nipez.cz/opendata/nova"
ISVZ_DOWNLOAD_URLS = [
    "https://isvz.nipez.cz/sites/default/files/content/opendata-rvz/"
    "VZ-{month:02d}-{year}.zip",
]
ISVZ_MONTHS_BACK = 4  # aktuální měsíc ještě není publikován ⇒ reálně ~3 měsíce

# Druhy lhůt (Lhuta.druh_lhuty), které se berou jako lhůta pro podání —
# v pořadí priority; preferuje se záznam s aktivni=true.
ISVZ_DEADLINE_KINDS = [
    "Lhůta pro podání nabídky",
    "Lhůta pro podání předběžné nabídky",
    "Lhůta pro podání žádosti o účast",
]
# Hodnota rezim_verejne_zakazky označující zakázku malého rozsahu.
ISVZ_REZIM_VZMR = "Veřejné zakázky malého rozsahu"

# Mapování polí ISVZ JSON → interní model je (kvůli vnořeným strukturám
# a seznamům lhůt) implementováno přímo ve fetch_isvz.normalize().
# Skutečné cesty (dokumentace 2.9.0, ověřeno na datech):
#   id             verejna_zakazka.identifikator_NIPEZ
#   title          verejna_zakazka.nazev_verejne_zakazky
#   authority/ico  verejna_zakazka.zadavaci_postupy[].zadavatel_zadavaciho_
#                  postupu.zadavatele[].subjekt.{nazev_subjektu, ico}
#   cpv            predmet.hlavni_kod_CPV + predmet.vedlejsi_kod_CPV[]
#                  (na úrovni VZ i částí)
#   nuts/place     predmet.mista_plneni[].{nuts, misto_plneni_jine,
#                  dalsi_informace_o_miste_plneni}
#   value          predpokladana_hodnota_bez_DPH_v_CZK
#                  (fallback: součet hodnot částí)
#   published      zadavaci_postup_pro_cast.datum_zahajeni_zadavaciho_postupu
#   deadline       zadavaci_postup_pro_cast.lhuty[] dle ISVZ_DEADLINE_KINDS
#   url            zadavaci_postup_pro_cast.odkaz_na_profil
#   state          zadavaci_postup_pro_cast.stav
#   clarifications zadavaci_postup_pro_cast.pocet_zaslanych_zaevidovanych_
#                  vysvetleni_zadavaci_dokumentace (součet přes části)
#   site_visit     v open datech NENÍ → zůstává prázdné (frontend skryje)

# Detekce změn ZD běží NA SERVERU (denní workflow) pro všechny zakázky
# a ukládá se do samostatného souboru changes.json — archiv je tak shodný
# a aktuální na všech zařízeních. Watchlist zůstává pouze v prohlížeči.
TRACK_FIELDS = {
    "deadline": "lhůta",
    "value": "hodnota",
    "site_visit": "prohlídka",
    "clarifications": "vysvětlení ZD",
    "title": "název",
}
CHANGES_KEEP = 20           # max. záznamů historie na zakázku
CHANGES_MAX_AGE_DAYS = 180  # starší záznamy se odmazávají

# ── Zdroj 2: profily zadavatelů (VZMR), XML rozhraní profilů ────────────────
# OVĚŘENO 2026-07-28 živými požadavky: endpoint je
#   {profile_url}/XMLdataVZ?od=DDMMYYYY&do=DDMMYYYY
# a funguje shodně na E-ZAK, NEN, Tender areně, PVU, Gordionu i KDV
# (profilzadavatele-vz.cz). Odpověď: XML s namespace
# urn:cz:isvz:mmr:schemas:vz-z-profilu-zadavatele:v100 — tj. struktura dle
# vyhl. 168/2016 Sb. se stále používá i po účinnosti vyhl. 345/2023 Sb.
# Skutečné názvy elementů viz TAGY ve fetch_profily.py (zakazka/id_objektu/
# nazev_vz/…; lhůty: lhuty_zadavaciho_postupu/lhuta/druh_lhuty="lhůta podání
# nabídky"). NEN profily navíc vracejí id_nipez = identifikator_NIPEZ z ISVZ
# (přesná deduplikace napříč zdroji).
# URL profilů pocházejí z pole adresa_profilu v ISVZ open datech (04–06/2026),
# IČO ověřena tamtéž; volitelný klíč "xml_url" = šablona s {od}/{do}, pokud
# se endpoint liší od výchozí konvence (Holice: instance chce ?profil=N).
PROFILY_ZADAVATELU = {
    # kraje + původních 12 (schválený seznam)
    "kr-kralovehradecky": {"lat": 50.209, "lon": 15.833, "nazev": "Královéhradecký kraj",  "ico": "70889546", "profile_url": "https://zakazky.cenakhk.cz/profile_display_2.html"},
    "kr-pardubicky":      {"lat": 50.038, "lon": 15.779, "nazev": "Pardubický kraj",       "ico": "70892822", "profile_url": "https://zakazky.pardubickykraj.cz/profile_display_2.html"},
    "hradec-kralove":     {"lat": 50.209, "lon": 15.833, "nazev": "Statutární město Hradec Králové", "ico": "00268810", "profile_url": "https://www.tenderarena.cz/profily/hradeckralove"},
    "pardubice":          {"lat": 50.038, "lon": 15.779, "nazev": "Statutární město Pardubice",      "ico": "00274046", "profile_url": "https://nen.nipez.cz/profil/pardubice"},
    "trutnov":            {"lat": 50.561, "lon": 15.913, "nazev": "Město Trutnov",  "ico": "00278360", "profile_url": "https://zakazky.trutnov.cz/profile_display_2.html"},
    "nachod":             {"lat": 50.417, "lon": 16.163, "nazev": "Město Náchod",   "ico": "00272868", "profile_url": "https://nen.nipez.cz/profil/mestonachod"},
    "jicin":              {"lat": 50.437, "lon": 15.352, "nazev": "Město Jičín",    "ico": "00271632", "profile_url": "https://nen.nipez.cz/profil/mestojicin"},
    "rychnov-nk":         {"lat": 50.163, "lon": 16.275, "nazev": "Město Rychnov nad Kněžnou", "ico": "00275336", "profile_url": "https://zakazky.rychnov-city.cz/profile_display_2.html"},
    "chrudim":            {"lat": 49.951, "lon": 15.795, "nazev": "Město Chrudim",  "ico": "00270211", "profile_url": "https://zakazky.chrudim-city.cz/profile_display_2.html"},
    "svitavy":            {"lat": 49.756, "lon": 16.468, "nazev": "Město Svitavy",  "ico": "00277444", "profile_url": "https://nen.nipez.cz/profil/msvitavy"},
    "usti-no":            {"lat": 49.974, "lon": 16.394, "nazev": "Město Ústí nad Orlicí", "ico": "00279676", "profile_url": "https://zakazky.muuo.cz/profile_display_2.html"},
    "dvur-kralove":       {"lat": 50.432, "lon": 15.814, "nazev": "Město Dvůr Králové nad Labem", "ico": "00277819", "profile_url": "https://zakazky.mudk.cz/profile_display_2.html"},
    # rozšíření: ORP + větší města v okruhu ~50 km od HK (sídlo v okruhu)
    "jaromer":            {"lat": 50.351, "lon": 15.921, "nazev": "Město Jaroměř",  "ico": "00272728", "profile_url": "https://nen.nipez.cz/profil/mesto_jaromer"},
    "nove-mesto-nm":      {"lat": 50.344, "lon": 16.151, "nazev": "Město Nové Město nad Metují", "ico": "00272876", "profile_url": "https://www.egordion.cz/nabidkaGORDION/profilNmnm"},
    "dobruska":           {"lat": 50.292, "lon": 16.160, "nazev": "Město Dobruška", "ico": "00274879", "profile_url": "https://www.vhodne-uverejneni.cz/profil/00274879"},
    "kostelec-no":        {"lat": 50.122, "lon": 16.213, "nazev": "Město Kostelec nad Orlicí", "ico": "00274968", "profile_url": "https://nen.nipez.cz/profil/KostelecnadOrlici"},
    "horice":             {"lat": 50.366, "lon": 15.632, "nazev": "Město Hořice",   "ico": "00271560", "profile_url": "https://tenderarena.cz/dodavatel/seznam-profilu-zadavatelu/detail/Z0000671"},
    "nova-paka":          {"lat": 50.494, "lon": 15.515, "nazev": "Město Nová Paka", "ico": "00271888", "profile_url": "https://www.profilzadavatele-vz.cz/profile_cent_1674.html"},
    "vrchlabi":           {"lat": 50.627, "lon": 15.609, "nazev": "Město Vrchlabí", "ico": "00278475", "profile_url": "https://zakazky.muvrchlabi.cz/profile_display_2.html"},
    "holice":             {"lat": 50.066, "lon": 15.986, "nazev": "Město Holice",   "ico": "00273571", "profile_url": "https://zakazky.mestoholice.cz",
                           "xml_url": "https://zakazky.mestoholice.cz/XMLdataVZ?od={od}&do={do}&profil=2"},
    "prelouc":            {"lat": 50.040, "lon": 15.560, "nazev": "Město Přelouč",  "ico": "00274101", "profile_url": "https://www.vhodne-uverejneni.cz/profil/00274101"},
    "novy-bydzov":        {"lat": 50.242, "lon": 15.491, "nazev": "Město Nový Bydžov", "ico": "00269247", "profile_url": "https://nen.nipez.cz/Profil/MNB"},
    "chlumec-nc":         {"lat": 50.155, "lon": 15.460, "nazev": "Město Chlumec nad Cidlinou", "ico": "00268861", "profile_url": "https://nen.nipez.cz/profil/chlumecnadcidlinou"},
    "vysoke-myto":        {"lat": 49.953, "lon": 16.162, "nazev": "Město Vysoké Mýto", "ico": "00279773", "profile_url": "https://nen.nipez.cz/profil/profilVM"},
    "ceska-skalice":      {"lat": 50.395, "lon": 16.043, "nazev": "Město Česká Skalice", "ico": "00272591", "profile_url": "https://nen.nipez.cz/profil/CeskaSkalice"},
    "opocno":             {"lat": 50.267, "lon": 16.115, "nazev": "Město Opočno",   "ico": "00275191", "profile_url": "https://opocno.profilzadavatele-vz.cz/profile_cent_2138.html"},
    "tyniste-no":         {"lat": 50.151, "lon": 16.078, "nazev": "Město Týniště nad Orlicí", "ico": "00275468", "profile_url": "https://nen.nipez.cz/profil/tynistenadorlici"},
    "upice":              {"lat": 50.512, "lon": 16.015, "nazev": "Město Úpice",    "ico": "00278386", "profile_url": "https://tenderarena.cz/profily/00278386"},
    "cerveny-kostelec":   {"lat": 50.476, "lon": 16.093, "nazev": "Město Červený Kostelec", "ico": "00272566", "profile_url": "https://cervenykostelec.profilzadavatele-vz.cz/profile_cent_2596.html"},
    "hronov":             {"lat": 50.480, "lon": 16.182, "nazev": "Město Hronov",   "ico": "00272680", "profile_url": "https://www.vhodne-uverejneni.cz/profil/00272680"},
    "zamberk":            {"lat": 50.087, "lon": 16.467, "nazev": "Město Žamberk",  "ico": "00279846", "profile_url": "https://nen.nipez.cz/profil/mestozamberk"},
    "vamberk":            {"lat": 50.118, "lon": 16.290, "nazev": "Město Vamberk",  "ico": "00275492", "profile_url": "https://www.vhodne-uverejneni.cz/profil/00275492"},
    "lazne-bohdanec":     {"lat": 50.076, "lon": 15.680, "nazev": "Město Lázně Bohdaneč", "ico": "00273350", "profile_url": "https://www.tenderarena.cz/profily/LazneBohdanec"},
    "skutec":             {"lat": 49.843, "lon": 15.996, "nazev": "Město Skuteč",   "ico": "00270903", "profile_url": "https://nen.nipez.cz/profil/LEOPOSMEST1"},
    "hlinsko":            {"lat": 49.762, "lon": 15.907, "nazev": "Město Hlinsko",  "ico": "00270059", "profile_url": "https://nen.nipez.cz/profil/mestohlinsko"},
}
PROFILY_DAYS_BACK = 90

# Platformy profilů zadavatelů — VŠECHNY certifikované nástroje poskytují
# totožné XML dle vyhl. 168/2016 Sb., fetch_profily.py je tedy obslouží beze
# změny kódu. Úkol (CLAUDE.md): na každé platformě dohledat profily všech
# ORP + krajů v HK/Pce regionu a doplnit je do PROFILY_ZADAVATELU.
# URL vzory jsou orientační — OVĚŘIT na reálném profilu.
PLATFORMY_PROFILU = {
    "pvu":          {"nazev": "Portál pro vhodné uveřejnění (QCM)",
                     "web": "https://www.vhodne-uverejneni.cz"},
    "ezak":         {"nazev": "E-ZAK (QCM) — samostatné instance zadavatelů",
                     "web": "https://www.ezak.cz"},
    "tender-arena": {"nazev": "Tender arena (Tendersystems)",
                     "web": "https://www.tenderarena.cz"},
    "tendermarket": {"nazev": "TENDERMARKET",
                     "web": "https://www.tendermarket.cz"},
    "nen-profil":   {"nazev": "Profily zadavatelů vedené přímo v NEN",
                     "web": "https://nen.nipez.cz"},
    "eveza":        {"nazev": "EVEZA",
                     "web": "https://www.eveza.cz"},
}

# ── Zdroj 3: NEN veřejné API — OBOHACENÍ detailů (prohlídka, vysvětlení ZD) ─
# Přístup: žádost u provozovatele NEN (nejprve referenční prostředí),
# autentizace certifikátem. Certifikát + klíč se předávají výhradně přes
# GitHub Actions secrets (NEN_CERT_PEM, NEN_KEY_PEM) — NIKDY necommitovat.
# Modul je neaktivní, dokud secrets neexistují.
NEN_API_BASE = "TODO-OVERIT"     # po schválení žádosti doplnit URL API
NEN_MAX_DETAILS_PER_RUN = 50     # šetrnost: obohacuje se max. N aktivních VZ/běh

# ── Výstup a pojistky ───────────────────────────────────────────────────────
OUT_DIR = "docs/data"
OUT_TENDERS = f"{OUT_DIR}/tenders.json"
OUT_CHANGES = f"{OUT_DIR}/changes.json"
OUT_META = f"{OUT_DIR}/meta.json"
MIN_RESULT_RATIO = 0.2   # nový výsledek < 20 % předchozího ⇒ selhat, nepřepisovat
