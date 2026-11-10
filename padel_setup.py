#!/usr/bin/env python3
"""
padel_setup.py - configureerbare opzet voor automatisch baantjes reserveren.

Draai dit op je eigen computer (niet in Home Assistant). De wizard vraagt je
inloggegevens, leest je vereniging/banen/tijden uit het boekingssysteem en
schrijft daarna een kant-en-klare set bestanden voor Home Assistant:

    output/book_padel.py                 -> /config/scripts/book_padel.py
    output/book_padel_secrets.json       -> /config/scripts/book_padel_secrets.json
    output/book_padel_automation.yaml    -> automation in Home Assistant
    output/INSTALL.md                    -> stap-voor-stap installatie

Gebruik:
    python3 padel_setup.py
    python3 padel_setup.py --email jij@example.com --out ./output
"""

import argparse
import base64
import getpass
import json
import pprint
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

SOCIE = "https://api.socie.nl"
AU = "https://booking.allunited.nl"

SOCIE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "appbundle": "nl.tizin.socie.tennis",
    "appversion": "4.17.5",
    "appbuild": "407",
    "platform": "iOS",
    "language": "nl-NL",
    "User-Agent": "Socie/4.17.5 (nl.tizin.socie.tennis; build:407; iOS 26.5.0) Alamofire/5.10.2",
}

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni",
           "juli", "augustus", "september", "oktober", "november", "december"]


class Fout(Exception):
    pass


# --------------------------------------------------------------------- invoer
def vraag(tekst: str, standaard: str = "") -> str:
    """Vraag om invoer; lege regel = standaard. EOF = standaard."""
    suffix = f" [{standaard}]" if standaard else ""
    try:
        antwoord = input(f"{tekst}{suffix}: ").strip()
    except EOFError:
        antwoord = ""
    return antwoord or standaard


def vraag_getal(tekst: str, opties: int, standaard: int = 1) -> int:
    while True:
        antwoord = vraag(tekst, str(standaard))
        if antwoord.isdigit() and 1 <= int(antwoord) <= opties:
            return int(antwoord) - 1
        print(f"  Voer een getal tussen 1 en {opties} in.")


def kies(tekst: str, labels: list, standaard: int = 1) -> int:
    print()
    for i, label in enumerate(labels, 1):
        print(f"  {i}. {label}")
    return vraag_getal(tekst, len(labels), standaard)


def ja(tekst: str, standaard: bool = True) -> bool:
    antwoord = vraag(tekst + (" [J/n]" if standaard else " [j/N]"), "j" if standaard else "n")
    return antwoord.lower().startswith("j")


# ------------------------------------------------------------------ API-hulp
def _get(sess, url, **kwargs):
    """GET met begrip voor rate limiting."""
    for poging in range(3):
        r = sess.get(url, **kwargs)
        if r.status_code != 429:
            return r
        print("  (boekingssysteem is even druk, 5 seconden wachten...)")
        import time
        time.sleep(5)
    return r


def socie_login(sess, email: str, wachtwoord: str) -> str:
    r = sess.post(f"{SOCIE}/v2/auth/login", headers=SOCIE_HEADERS, timeout=30,
                  json={"appType": "TENNIS", "email": email, "password": wachtwoord})
    if r.status_code == 429:
        raise Fout("Socie weigert tijdelijk nieuwe inlogpogingen (HTTP 429). "
                   "Wacht een minuut en probeer opnieuw.")
    if r.status_code in (400, 401, 403):
        raise Fout(f"Inloggen mislukt ({r.status_code}). Controleer e-mailadres en wachtwoord.")
    if r.status_code != 200:
        raise Fout(f"Inloggen mislukt ({r.status_code}): {r.text[:200]}")
    token = r.json().get("accessToken")
    if not token:
        raise Fout("Inloggen gaf geen token terug.")
    return token


def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def community_naam(sess, token: str, cid: str, mid: str) -> str:
    h = dict(SOCIE_HEADERS, Authorization=f"bearer {token}", membership_id=mid)
    r = sess.get(f"{SOCIE}/v2/app/communities/{cid}", headers=h, timeout=20)
    if r.status_code == 200:
        return r.json().get("name") or cid
    return cid


def _menu_modules(boom, gevonden: list) -> list:
    """Verzamel (moduleType, module_id) uit de menu-boom van de clubapp."""
    if isinstance(boom, dict):
        if boom.get("module_id") and boom.get("moduleType"):
            gevonden.append((str(boom["moduleType"]).upper(), boom["module_id"]))
        for waarde in boom.values():
            _menu_modules(waarde, gevonden)
    elif isinstance(boom, list):
        for item in boom:
            _menu_modules(item, gevonden)
    return gevonden


def vind_boekingsmodule(sess, token: str, cid: str, mid: str,
                        override: str | None = None) -> str:
    """Zoek de module-id van de AllUnited-koppeling ('Baan reserveringen').

    Het menu van de clubapp (`customizations/menu`) noemt per tegel het type en de
    module-id; dat is dezelfde lijst die de app gebruikt, dus werkt dit voor elke
    vereniging op hetzelfde platform.
    """
    h = dict(SOCIE_HEADERS, Authorization=f"bearer {token}", membership_id=mid)

    kandidaten = [override] if override else []
    r = sess.get(f"{SOCIE}/v2/app/communities/{cid}/customizations/menu",
                 headers=h, timeout=20)
    if r.status_code == 200:
        modules = _menu_modules(r.json(), [])
        for module_type, module_id in modules:
            if module_type.startswith("ALLUNITED") and module_id not in kandidaten:
                kandidaten.append(module_id)

    if not kandidaten:
        r = sess.get(f"{SOCIE}/v2/app/communities/{cid}/pages", headers=h, timeout=20)
        if r.status_code == 200:
            for pagina in r.json():
                module_id = pagina.get("module_id")
                if module_id and module_id not in kandidaten:
                    kandidaten.append(module_id)

    for module_id in kandidaten:
        r = sess.get(f"{SOCIE}/v2/app/communities/{cid}/modules/{module_id}/allunited_pwa/url",
                     headers=h, timeout=15)
        if r.status_code == 200 and (r.json() or {}).get("url"):
            return module_id
    return ""


def allunited_sessie(sess, token: str, cid: str, mid: str, module_id: str) -> dict:
    h = dict(SOCIE_HEADERS, Authorization=f"bearer {token}", membership_id=mid)
    r = sess.get(f"{SOCIE}/v2/app/communities/{cid}/modules/{module_id}/allunited_pwa/url",
                 headers=h, timeout=20)
    if r.status_code != 200:
        raise Fout(f"Kon de boekingslink niet ophalen ({r.status_code}).")
    ssostring = r.json()["url"].split("/sso/")[1]
    r = sess.get(f"{AU}/api/sso/webshop_sso.php", params={"ssostring": ssostring}, timeout=25)
    if r.status_code != 200:
        raise Fout(f"Aanmelden bij het boekingssysteem mislukt ({r.status_code}).")
    j = r.json()
    return {"headers": {"Accept": "application/json", "x-webshop-key": j["webshopKey"],
                        "Authorization": f"Bearer {j['accessToken']}"},
            "club": j.get("sectionName"), "contact": j.get("contactName")}


def filter_groups(sess, au: dict) -> list:
    r = _get(sess, f"{AU}/api/booking/webshop/filter-groups", headers=au["headers"], timeout=20)
    if r.status_code != 200:
        raise Fout(f"Kon de sporten niet ophalen ({r.status_code}).")
    return r.json().get("data") or []


def schema(sess, au: dict, fg_id, datum: str, playercount: int) -> dict:
    r = _get(sess, f"{AU}/api/booking/webshop/filter-groups/{fg_id}",
             headers=au["headers"], timeout=25,
             params={"playercount": str(playercount), "date": datum, "mode": "schematic"})
    if r.status_code != 200:
        return {}
    return r.json().get("data") or {}


def baan_overzicht(sess, au: dict, fg_id, playercount: int) -> tuple:
    """Baan namen + waar in het uur ze beginnen + openingstijden."""
    probe = {}
    for dagen in range(2, 15):
        datum = (date.today() + timedelta(days=dagen)).isoformat()
        probe = schema(sess, au, fg_id, datum, playercount)
        if probe.get("schematicPeriods") and probe.get("timeLineFrom"):
            break

    banen, gezien = [], set()
    for p in probe.get("schematicPeriods") or []:
        naam = p.get("group")
        if not naam or naam in gezien:
            continue
        gezien.add(naam)
        start = p.get("timeFrom")
        fase = 0
        if start == "00:00":
            fase = 99  # nog niet gevonden: gebruik tweede periode
        else:
            fase = int(start.split(":")[0]) * 60 + int(start.split(":")[1])
        banen.append({"naam": naam, "fase": fase, "tijd": start, "periode": p})

    # tweede periode per baan = het moment dat de baan opengaat
    for b in banen:
        if b["fase"] == 99:
            volgende = [p for p in probe["schematicPeriods"]
                        if p.get("group") == b["naam"] and p.get("timeFrom") != "00:00"]
            if volgende:
                start = volgende[0]["timeFrom"]
                b["fase"] = int(start.split(":")[0]) * 60 + int(start.split(":")[1])
                b["open"] = start
            else:
                b["fase"] = 0
        else:
            b["open"] = b["tijd"]

    open_tijd = probe.get("timeLineFrom") or "08:00"
    dicht_tijd = probe.get("timeLineTo") or "23:00"
    return banen, open_tijd, dicht_tijd


def zoek_context(sess, au: dict, fg_id, playercount: int) -> str:
    """Item-key van een bestaand slot, als context voor het zoeken van spelers."""
    for dagen in (3, 4, 5, 6, 7, 1, 2):
        datum = (date.today() + timedelta(days=dagen)).isoformat()
        data = schema(sess, au, fg_id, datum, playercount)
        for periode in data.get("schematicPeriods") or []:
            if periode.get("status") == "available" and periode.get("items"):
                return periode["items"][0]["key"]
    return ""


def au_zoek_spelers(sess, au: dict, context_key: str, term: str) -> list:
    """Zoek leden via de webshop zelf (players/reservation/{itemkey})."""
    if not context_key:
        return []
    r = _get(sess, f"{AU}/api/booking/webshop/players/reservation/{context_key}",
             headers=au["headers"], params={"searchstring": term}, timeout=25)
    if r.status_code != 200:
        return []
    data = (r.json() or {}).get("data")
    rijen = data if isinstance(data, list) else ([data] if data else [])
    return [{"key": str(x["key"]), "naam": x.get("name") or ""} for x in rijen if x.get("key")]


def fase_label(fase: int) -> str:
    rest = fase % 60
    if rest == 0:
        return "begint op heel uur"
    if rest == 30:
        return "begint op half uur"
    return f"begint op :{rest:02d}"


STATE_BESTAND = "padel_setup_state.json"


def opgeslagen_module_id() -> str:
    """Module-id die eerder is vastgesteld (per vereniging) hergebruiken."""
    try:
        data = json.loads(Path(STATE_BESTAND).read_text(encoding="utf-8"))
    except Exception:
        return ""
    return data.get("module_id", "")


def bewaar_module_id(module_id: str) -> None:
    Path(STATE_BESTAND).write_text(json.dumps({"module_id": module_id}, indent=2) + "\n",
                                   encoding="utf-8")
    print(f"  (module-id opgeslagen in {STATE_BESTAND} voor de volgende keer)")


def _h(token: str, mid: str) -> dict:
    return dict(SOCIE_HEADERS, Authorization=f"bearer {token}", membership_id=mid)


def eigen_external_reference(sess, token: str, cid: str, mid: str) -> tuple:
    """(externalReference, naam) van de ingelogde gebruiker."""
    r = sess.get(f"{SOCIE}/v2/me/communities/{cid}/memberships",
                 headers=_h(token, mid), timeout=25)
    if r.status_code == 200:
        for m in r.json() or []:
            ext = (m.get("extraFields") or {}).get("externalReference")
            if ext:
                persoon = m.get("person") or {}
                naam = ", ".join(x for x in (persoon.get("lastName"),
                                             persoon.get("firstName")) if x)
                return str(ext), naam
    return "", ""


def leden_zoek(sess, token: str, cid: str, mid: str, term: str) -> list:
    """Zoek leden via de zoekfunctie van de app (server-side)."""
    r = sess.get(f"{SOCIE}/v2/app/communities/{cid}/members/search",
                 headers=_h(token, mid), params={"q": term}, timeout=25)
    if r.status_code != 200:
        return []
    uit = []
    for m in r.json() or []:
        a = m.get("appendedMembership") or {}
        ext = a.get("externalReference")
        if ext and a.get("fullName"):
            uit.append({"key": str(ext), "naam": a["fullName"]})
    return uit


def app_banen(sess, token: str, cid: str, mid: str, module_id: str) -> list:
    """Baanoverzicht uit de app-API (alle banen, ongeacht bezetting)."""
    for dagen_vooruit in (3, 4, 5, 1, 2, 6, 7):
        datum = (date.today() + timedelta(days=dagen_vooruit)).isoformat()
        r = sess.get(f"{SOCIE}/v2/app/communities/{cid}/modules/{module_id}"
                     f"/allunited_tennis_courts/overview",
                     headers=_h(token, mid), params={"date": datum}, timeout=30)
        if r.status_code == 200:
            locaties = (r.json() or {}).get("locations") or []
            if locaties:
                return locaties
    return []


def sport_label(group_name: str) -> str:
    g = (group_name or "").lower()
    if "padel" in g:
        return "Padel"
    if "tennis" in g:
        return "Tennis"
    return (group_name or "Onbekend").capitalize()


def sporten_en_banen(sess, token: str, cid: str, mid: str, module_id: str) -> dict:
    """{sport: {'groep': groupName, 'banen': [{'naam', 'fase': None}]}} via de app."""
    sporten: dict = {}
    for loc in app_banen(sess, token, cid, mid, module_id):
        label = sport_label(loc.get("groupName"))
        info = sporten.setdefault(label, {"groep": loc.get("groupName"), "banen": {}})
        info["banen"].setdefault(loc["name"], {"naam": loc["name"], "fase": None})
    return {naam: {"groep": info["groep"],
                   "banen": sorted(info["banen"].values(), key=lambda b: b["naam"])}
            for naam, info in sporten.items()}


def kies_groep(groepen: list, sport_naam: str) -> dict:
    """Filter-groep van de webshop die bij de gekozen sport hoort."""
    wil = "padel" if "padel" in sport_naam.lower() else "tennis"
    for g in groepen:
        if wil in f"{g.get('name', '')} {g.get('icon', '')}".lower():
            return g
    return groepen[0]


# -------------------------------------------------------------------- schrijven
CONFIG_REGEL = re.compile(r"^CONFIG = .*$", re.MULTILINE)


def render_script(template: Path, config: dict) -> str:
    """Het runtime-script met het CONFIG-blok erin."""
    tekst = template.read_text(encoding="utf-8")
    blok = "CONFIG = " + pprint.pformat(config, indent=4, sort_dicts=False, width=88)
    return CONFIG_REGEL.sub(blok, tekst, count=1)


def render_automation(config: dict, notify_entity: str, slot_eind: str) -> tuple:
    dagen = int(config["days_ahead"])
    doel_dag = int(config["weekday"])
    eind_uur, eind_min = (int(x) for x in slot_eind.split(":"))
    open_dt = datetime(2026, 1, 5) + timedelta(days=doel_dag, hours=eind_uur, minutes=eind_min)
    open_dt -= timedelta(days=dagen)
    trig_weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][open_dt.weekday()]
    trig_time = open_dt.strftime("%H:%M") + ":05"

    if notify_entity:
        acties = f"""  - action: notify.send_message
    metadata: {{}}
    target:
      entity_id: {notify_entity}
    data:
      message: |
        {{{{ result.stdout if result.returncode == 0
           else 'Reservering MISLUKT: ' + result.stdout + ' ' + result.stderr }}}}
"""
    else:
        acties = """  # HANDMATIGE STAP: zet hier je notificatie-entiteit en haal het
  # commentaar weg. Zonder deze actie reserveert de automation wel,
  # maar krijg je geen bericht.
  # - action: notify.send_message
  #   metadata: {}
  #   target:
  #     entity_id: notify.MIJN_TELEFOON
  #   data:
  #     message: >-
  #       {{ result.stdout if result.returncode == 0
  #          else 'Reservering MISLUKT: ' + result.stdout + ' ' + result.stderr }}
"""

    yaml = f"""alias: Baan reserveren
description: >
  Reserveert elke {DAGEN[doel_dag]} om {config['time']} {config.get('court') or 'een baan'}
  ({config.get('sport_naam', 'sport')}) bij {config.get('club', 'de vereniging')}.
  Draait {dagen} dagen vooruit om {trig_time[:-3]}, aan het eind van het tijdslot.
triggers:
  - trigger: time
    at: "{trig_time}"
conditions:
  - condition: time
    weekday:
      - {trig_weekday}
actions:
  - action: shell_command.book_padel
    response_variable: result
    data: {{}}
{acties}mode: single
"""
    return yaml, {"weekday": trig_weekday, "time": trig_time}


def render_install(notify_entity: str, trig: dict, bestandsnaam: str = "book_padel_secrets.json") -> str:
    if notify_entity:
        notif_regel = f"- notificatie naar `{notify_entity}`"
        notif_stap = f"""## 4. Notificatie

De automation stuurt het resultaat naar `{notify_entity}`. Klopt die entiteit
niet, pas hem aan in de automation (actie `notify.send_message`)."""
    else:
        notif_regel = "- notificatie: **nog instellen** (zie stap 4)"
        notif_stap = """## 4. Notificatie instellen (handmatig)

De automation is gegenereerd **zonder** notificatie-actie. In
`book_padel_automation.yaml` staat een uitgecommentarieerd blok met
`notify.MIJN_TELEFOON`. Vervang dat door je eigen entiteit (bijvoorbeeld
`notify.iphone_van_ferry`, of `persistent_notification.create` als je alleen
in Home Assistant wilt melden) en haal de `#` aan het begin van die regels weg.

Zonder deze stap reserveert de automation wel, maar krijg je geen bericht als
er iets misgaat."""

    tekst = f"""# Installatie in Home Assistant

Gegenereerd door padel_setup.py. Volg de stappen in deze volgorde.

## 1. Bestanden naar Home Assistant

Kopieer met de File editor (of Studio Code Server / Samba):

| bestand | doelpad in Home Assistant |
|---|---|
| `book_padel.py` | `/config/scripts/book_padel.py` |
| `book_padel_secrets.json` | `/config/scripts/book_padel_secrets.json` |

De map `/config/scripts` aanmaken als hij nog niet bestaat.

## 2. shell_command toevoegen

Zet dit in `configuration.yaml` (eenmalig, onderaan mag):

```yaml
shell_command:
  book_padel: "python3 /config/scripts/book_padel.py"
```

`command_line`/`shell_command` herlaadt niet via de UI: **Home Assistant
opnieuw starten** (Instellingen -> Systeem -> Opnieuw opstarten) of
Ontwikkelaarhulpmiddelen -> Acties -> `shell_command.reload`
(beschikbaar vanaf 2024.11).

## 3. Automation importeren

Instellingen -> Automatiseringen -> **Importeren** (of Nieuwe automatisering ->
Rechtsboven -> YAML bewerken) en plak de inhoud van
`book_padel_automation.yaml`.

Controleer daarna:

- trigger: elke **{trig['weekday']}** om **{trig['time'][:-3]}**
- actie: `shell_command.book_padel`
{notif_regel}

{notif_stap}

## 5. Testen zonder te reserveren

Open een terminal in Home Assistant en run:

```bash
python3 /config/scripts/book_padel.py --dry-run
```

Verwacht: `DRY-RUN: <baan> gereserveerd op <datum> van <tijd> tot <tijd>`.
De winkelwagenregel verdwijnt vanzelf (ongeveer 3 minuten).

Of test met een datum die al boekbaar is:

```bash
python3 /config/scripts/book_padel.py --dry-run --date {date.today().isoformat()}
```

## 6. Echt reserveren

De automation doet dit automatisch. Handmatig:

```bash
python3 /config/scripts/book_padel.py
```

Uitvoer bij succes: `<baan> gereserveerd op <datum> van <tijd> tot <tijd>`.
Bij een vol tijdslot meldt het script of een half uur eerder of later nog
iets vrij is.

## Problemen

| melding | oorzaak |
|---|---|
| `inloggen bij Socie mislukt` | wachtwoord gewijzigd? pas `book_padel_secrets.json` aan |
| `HTTP 429` | te veel inlogpogingen; even wachten |
| `ophalen boekingslink mislukt` | module-id klopt niet meer (club heeft iets gewijzigd) -> wizard opnieuw draaien |
| `geen baan vrij om ...` | slot al bezet; script noemt alternatieven |
| `datum ... is geen ...` | de automation draait op de verkeerde dag |
"""
    return tekst


SECRETS_VELDEN = ("email", "password")


def _alleen_invoer(creds: dict) -> dict:
    """Geef alleen de door de gebruiker ingevoerde velden terug.

    Het secrets-bestand mag nooit meer bevatten dan wat er is ingevoerd: geen
    id's, geen clubgegevens, geen tokens. Onbekende velden zijn een programmeer-
    fout en laten we hard stuklopen in plaats van ze weg te schrijven.
    """
    onbekend = set(creds) - set(SECRETS_VELDEN)
    if onbekend:
        raise Fout(f"secrets-bestand zou onverwachte velden krijgen: {sorted(onbekend)}")
    schoon = {}
    for veld in SECRETS_VELDEN:
        waarde = creds.get(veld)
        if not isinstance(waarde, str):
            raise Fout(f"secrets-veld '{veld}' is geen tekst")
        schoon[veld] = waarde
    return schoon


def bouw_bestanden(config: dict, notify_entity: str, slot_eind: str,
                   creds: dict | None) -> tuple:
    """(bestanden, trigger): alle HA-bestanden als {naam: inhoud}."""
    template = Path(__file__).with_name("template_book_padel.py")
    if not template.exists():
        raise Fout("template_book_padel.py niet gevonden naast padel_setup.py")
    automation, trig = render_automation(config, notify_entity, slot_eind)
    bestanden = {
        "book_padel.py": render_script(template, config),
        "book_padel_automation.yaml": automation,
        "INSTALL.md": render_install(notify_entity, trig),
    }
    if creds:
        bestanden["book_padel_secrets.json"] = (
            json.dumps(_alleen_invoer(creds), indent=2, ensure_ascii=False) + "\n")
    return bestanden, trig


# ---------------------------------------------------------------------- wizard
def main() -> int:
    parser = argparse.ArgumentParser(description="Configureer book_padel.py voor Home Assistant")
    parser.add_argument("--email", help="e-mailadres (anders wordt erom gevraagd)")
    parser.add_argument("--out", default="output", help="map voor de gegenereerde bestanden")
    parser.add_argument("--module-id", help="module-id van de boekingskoppeling (zelden nodig)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Baantjes-reservering instellen voor Home Assistant")
    print("=" * 60)

    sess = requests.Session()
    sess.headers["Accept"] = "application/json"

    email = args.email or vraag("E-mailadres (van je clubaccount)")
    wachtwoord = getpass.getpass("Wachtwoord: ")
    if not wachtwoord:
        raise Fout("Geen wachtwoord opgegeven.")

    token = socie_login(sess, email, wachtwoord)
    claims = jwt_claims(token)
    rollen = claims.get("roles") or {}
    if not rollen:
        raise Fout("Dit account is nog aan geen enkele vereniging gekoppeld.")

    verenigingen = []
    for cid, mids in rollen.items():
        mid = list(mids)[0]
        verenigingen.append({"cid": cid, "mid": mid,
                             "naam": community_naam(sess, token, cid, mid)})

    if len(verenigingen) == 1:
        v = verenigingen[0]
        print(f"\nVereniging: {v['naam']}")
    else:
        idx = kies("Bij welke vereniging wil je reserveren?", [x["naam"] for x in verenigingen])
        v = verenigingen[idx]
        print(f"\nVereniging: {v['naam']}")

    # boekingsmodule
    module_id = args.module_id or opgeslagen_module_id()
    module_id = vind_boekingsmodule(sess, token, v["cid"], v["mid"], module_id)
    if not module_id:
        raise Fout("Deze vereniging heeft geen AllUnited-boekingsmodule in het app-menu.")

    au = allunited_sessie(sess, token, v["cid"], v["mid"], module_id)
    print(f"Boekingssysteem: {au['club'] or v['naam']} (ingelogd als {au['contact']})")

    # eigen gegevens
    eigen_ext, eigen_naam = eigen_external_reference(sess, token, v["cid"], v["mid"])
    if eigen_ext:
        print(f"Jij: {eigen_naam} ({eigen_ext})")

    # sport en banen: webshop eerst
    groepen = filter_groups(sess, au)
    if groepen:
        labels = []
        for grp in groepen:
            opties = [o.get("playercount") for o in (grp.get("playerCountOptions") or [])]
            labels.append(f"{grp.get('name')} ({'/'.join(str(o) for o in opties)} spelers)")
        g = groepen[kies("Welke sport wil je spelen?", labels)]
        sport_naam = g.get("name") or "sport"
    else:
        # terugval: baanoverzicht uit de clubapp
        sporten = sporten_en_banen(sess, token, v["cid"], v["mid"], module_id)
        if not sporten:
            raise Fout("Geen sporten gevonden in het boekingssysteem.")
        labels = [f"{naam} ({len(info['banen'])} banen)" for naam, info in sporten.items()]
        sport_naam = list(sporten)[kies("Welke sport wil je spelen?", labels)]
        groepen = [{"id": None, "name": sport_naam}]
        g = groepen[0]
    fg_id = g["id"]
    playercount = int(g.get("defaultPlayerCount") or 4)
    print(f"\nSport: {sport_naam}")

    # context voor het zoeken van spelers in de webshop
    if fg_id is None:
        fg_id = next((x["id"] for x in filter_groups(sess, au)
                      if sport_naam.lower() in str(x.get("name", "")).lower()), None)
    context_key = zoek_context(sess, au, fg_id, playercount)

    # banen uit het webshop-schema (naam + waar in het uur het slot begint)
    banen, open_tijd, dicht_tijd = baan_overzicht(sess, au, fg_id, playercount)
    if not banen:
        sporten = sporten_en_banen(sess, token, v["cid"], v["mid"], module_id)
        banen = sporten.get(sport_naam, {}).get("banen") or []
        for baan in banen:
            baan["fase"] = baan.get("fase") or 0
        open_tijd, dicht_tijd = "07:00", "23:00"
    labels = [f"{x['naam']} ({fase_label(x['fase'])})" for x in banen]
    b = banen[kies("Welke baan wil je spelen?", labels)]
    print(f"\nBaan: {b['naam']} - {fase_label(b['fase'])}, "
          f"geopend van {open_tijd} tot {dicht_tijd}")

    # dag
    print("\nOp welke dag wil je spelen?")
    for i, d in enumerate(DAGEN, 1):
        print(f"  {i}. {d.capitalize()}")
    weekday = vraag_getal("Keuze", 7, 2)

    # tijd
    for _ in range(10):
        tijd = vraag("Hoe laat begint het tijdslot? (HH:MM, einde van het slot is een uur later)",
                     "20:30")
        try:
            uur, minuut = (int(x) for x in tijd.split(":"))
            minuten = uur * 60 + minuut
        except ValueError:
            print("  Gebruik HH:MM, bijvoorbeeld 20:30.")
            continue
        if minuut % 15:
            print("  Tijden liggen op kwartieren (:00, :15, :30, :45).")
            continue
        if not (int(open_tijd[:2]) * 60 <= minuten <= int(dicht_tijd[:2]) * 60 - 60):
            print(f"  Deze baan is open van {open_tijd} tot {dicht_tijd}; "
                  f"kies een starttijd daarbinnen (uiterlijk één uur voor sluiting).")
            continue
        if (minuten - b["fase"]) % 60:
            print(f"  Let op: {b['naam']} {fase_label(b['fase'])}; "
                  f"een slot van een uur begint dan op "
                  f"{(minuten - b['fase']) % 60} minuten na het hele uur.")
        break

    # hoeveel dagen vooruit
    slot_eind_min = minuten + 60
    slot_eind = f"{slot_eind_min // 60:02d}:{slot_eind_min % 60:02d}"
    print()
    print(f"Je speelt {DAGEN[weekday]} van {tijd} tot {slot_eind}.")
    print("Hoeveel dagen van tevoren gaat de reservering open?")
    print("  (aan het eind van het tijdslot: dus bij 6 dagen opent hij")
    print(f"   {slot_eind} op de {DAGEN[(weekday - 6) % 7]}, precies een week eerder min 1 dag)")
    while True:
        dagen = vraag("Aantal dagen", "6")
        if dagen.isdigit() and 0 <= int(dagen) <= 14:
            dagen = int(dagen)
            break
        print("  Voer een getal tussen 0 en 14 in.")

    open_dt = datetime(2026, 1, 5, slot_eind_min // 60, slot_eind_min % 60) + timedelta(days=weekday)
    open_dt -= timedelta(days=dagen)
    print(f"\nReservering opent: {DAGEN[open_dt.weekday()]} om {open_dt.strftime('%H:%M')} "
          f"({dagen} dagen voor {DAGEN[weekday]} {tijd}).")

    # spelers
    spelers = []
    if eigen_ext:
        spelers.append({"key": eigen_ext, "naam": eigen_naam or "ik"})
        print(f"\nJij doet automatisch mee: {spelers[0]['naam']} ({eigen_ext})")

    mogelijk = {str(o.get("playercount")).strip() for o in (g.get("playerCountOptions") or [])}
    if mogelijk and "4" not in mogelijk:
        aantal_spelers = int(sorted(mogelijk)[-1])
        print(f"\nDeze sport wordt met maximaal {aantal_spelers} spelers geboekt.")
    else:
        idx = kies("Met hoeveel spelers speel je?",
                   ["Ik en één ander (2 spelers)", "Ik en drie anderen (4 spelers)"], 2)
        aantal_spelers = 2 if idx == 0 else 4

    aantal_anderen = max(0, aantal_spelers - len(spelers))
    if aantal_anderen:
        print(f"\nWie spelen er mee? Naast jou nog {aantal_anderen}.")

    for i in range(aantal_anderen):
        while True:
            term = vraag(f"Naam deelnemer {i + 2} van {aantal_spelers}", "")
            if not term:
                print("  Vul een naam in.")
                continue
            gevonden = au_zoek_spelers(sess, au, context_key, term)
            bron = "webshop"
            if not gevonden:
                gevonden = leden_zoek(sess, token, v["cid"], v["mid"], term)
                bron = "app"
            treffers = [x for x in gevonden if x["key"] not in {s["key"] for s in spelers}]
            if not treffers:
                print("  Niemand gevonden met die naam. Probeer een achternaam.")
                continue
            if len(treffers) == 1:
                spelers.append(treffers[0])
                print(f"  Toegevoegd: {treffers[0]['naam']} ({treffers[0]['key']})")
                break
            idx = kies(f"Wie bedoel je met '{term}'?", [x["naam"] for x in treffers[:10]])
            spelers.append(treffers[idx])
            print(f"  Toegevoegd: {treffers[idx]['naam']} ({treffers[idx]['key']})")
            break

    print("\nNaar welke notify-entiteit gaat de bevestiging? (leeg laten mag;")
    print("dan zet je dat later zelf in de automation)")
    notify_entity = vraag("Notify-entiteit", "")
    bewaar_creds = ja("Inloggegevens opslaan in book_padel_secrets.json?", True)

    # ---- schrijven
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    config = {
        "club": v["naam"],
        "community_id": v["cid"],
        "membership_id": v["mid"],
        "module_id": module_id,
        "filter_group_id": fg_id,
        "sport_naam": g.get("name"),
        "sport_icon": (g.get("icon") or "").lower(),
        "weekday": weekday,
        "time": tijd,
        "court": b["naam"],
        "fallback_other_court": True,
        "days_ahead": dagen,
        "playercount": len(spelers) or playercount,
        "players": [s["key"] for s in spelers],
        "players_naam": [s["naam"] for s in spelers],
    }
    creds = {"email": email, "password": wachtwoord} if bewaar_creds else None
    bestanden, trig = bouw_bestanden(config, notify_entity, slot_eind, creds)
    for naam, inhoud in bestanden.items():
        (out / naam).write_text(inhoud, encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"Klaar. Bestanden staan in: {out.resolve()}")
    print("=" * 60)
    print(f"  book_padel.py                -> /config/scripts/book_padel.py")
    print(f"  book_padel_secrets.json      -> /config/scripts/book_padel_secrets.json"
          + ("" if bewaar_creds else "  (NIET opgeslagen)"))
    print(f"  book_padel_automation.yaml   -> automation in Home Assistant")
    print(f"  INSTALL.md                   -> stap-voor-stap installatie")
    print(f"\nReservering: elke {DAGEN[weekday]} {tijd}-{slot_eind}, {b['naam']}, "
          f"{len(spelers)} spelers, {dagen} dagen vooruit.")
    print(f"Automation draait: {trig['weekday']} om {trig['time'][:-3]}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fout as e:
        print(f"\nAfgebroken: {e}")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"\nAfgebroken: netwerkfout ({e})")
        sys.exit(1)
