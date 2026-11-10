#!/usr/bin/env python3
"""
book_padel.py - reserveer automatisch een baan via Socie -> AllUnited.

Dit bestand wordt NIET met de hand aangepast: het configuratieblok hieronder
wordt gevuld door Starthier.html (of padel_setup.py). Genereer je het
bestand opnieuw, dan wordt dit bestand overschreven.

Plaats in Home Assistant:
    /config/scripts/book_padel.py
    /config/scripts/book_padel_secrets.json   ({"email": ..., "password": ...})

Gebruik:
    python3 book_padel.py                     # echt reserveren
    python3 book_padel.py --dry-run           # alles behalve bevestigen
    python3 book_padel.py --date 2026-11-17   # andere datum (testen)
    python3 book_padel.py --verbose

Exitcode 0 = gelukt (of dry-run geslaagd), 1 = mislukt.
Stdout is de tekst voor de notificatie in Home Assistant.
"""

import base64
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------- configuratie
CONFIG = {}  # <<CONFIG>>
# ---------------------------------------------------------------------------

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

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni",
           "juli", "augustus", "september", "oktober", "november", "december"]
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

VERBOSE = "--verbose" in sys.argv


def log(msg: str) -> None:
    if VERBOSE:
        print(msg)


def fout(reden: str) -> None:
    print(f"Reservering niet gelukt: {reden}")
    sys.exit(1)


def datum_nl(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def plus_minuten(hhmm: str, minuten: int) -> str:
    t = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=minuten)
    return t.strftime("%H:%M")


def volgende_speeldag(weekday: int) -> date:
    """Eerstvolgende keer dat er gespeeld wordt (vandaag telt niet mee)."""
    vandaag = date.today()
    return vandaag + timedelta(days=(weekday - vandaag.weekday()) % 7 or 7)


def arg_waarde(naam: str):
    if naam in sys.argv:
        i = sys.argv.index(naam)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    for a in sys.argv:
        if a.startswith(naam + "="):
            return a.split("=", 1)[1]
    return None


def _au_headers(sessie: dict) -> dict:
    return {"Accept": "application/json", "x-webshop-key": sessie["webshopKey"],
            "Authorization": f"Bearer {sessie['accessToken']}"}


def _sso_exchange(s, ssostring: str):
    r = s.get(f"{AU}/api/sso/webshop_sso.php", params={"ssostring": ssostring}, timeout=30)
    if r.status_code != 200:
        return None
    sessie = r.json()
    return (_au_headers(sessie), sessie.get("sectionName")) if sessie.get("webshopKey") else None


def au_sessie(s, creds: dict) -> tuple:
    """Sessie bij het boekingssysteem.

    Eerst de bewaarde boekingslink uit secrets; die geeft elke run een verse
    sessie zonder inloggen. Werkt die niet meer, dan via de clubapp-login.
    """
    ssostring = (creds.get("ssostring") or "").strip()
    if ssostring:
        sessie = _sso_exchange(s, ssostring)
        if sessie:
            return sessie
        log("bewaarde boekingslink werkt niet meer; opnieuw inloggen bij de clubapp")

    if not (creds.get("email") and creds.get("password")):
        fout("De bewaarde boekingslink is verlopen. Open Starthier.html, plak een nieuwe "
             "boekingslink en werk /config/scripts/book_padel_secrets.json bij.")

    r = s.post(f"{SOCIE}/v2/auth/login", headers=SOCIE_HEADERS, timeout=30,
               json={"appType": "TENNIS", "email": creds["email"], "password": creds["password"]})
    if r.status_code == 429:
        fout("Socie weigert tijdelijk (te veel inlogpogingen). Probeer het later opnieuw.")
    if r.status_code != 200:
        fout(f"inloggen bij Socie mislukt ({r.status_code}): {r.text[:200]}")
    token = (r.json() or {}).get("accessToken")
    if not token:
        fout("inloggen bij Socie gaf geen token terug")
    log("Socie-login ok")

    cid, mid = socie_context(token)
    module_id = boekingsmodule(s, token, cid, mid)
    h = dict(SOCIE_HEADERS, Authorization=f"bearer {token}", membership_id=mid)
    r = s.get(f"{SOCIE}/v2/app/communities/{cid}/modules/{module_id}/allunited_pwa/url",
              headers=h, timeout=30)
    if r.status_code != 200:
        fout(f"ophalen boekingslink mislukt ({r.status_code}): {r.text[:200]}")
    sessie = _sso_exchange(s, r.json()["url"].split("/sso/")[1])
    if not sessie:
        fout("inloggen bij het boekingssysteem mislukt (boekingslink ongeldig)")
    return sessie


def socie_context(token: str) -> tuple:
    """Vereniging- en lid-id uit het inlogtoken (geen vaste ids nodig)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    rollen = (json.loads(base64.urlsafe_b64decode(payload)) or {}).get("roles") or {}
    for cid, mids in rollen.items():
        return cid, list(mids)[0]
    return CONFIG.get("community_id"), CONFIG.get("membership_id")


def boekingsmodule(s, token: str, cid: str, mid: str) -> str:
    """Module-id van de boekingskoppeling: uit de config of zelf opzoeken."""
    if CONFIG.get("module_id"):
        return CONFIG["module_id"]
    h = dict(SOCIE_HEADERS, Authorization=f"bearer {token}", membership_id=mid)
    r = s.get(f"{SOCIE}/v2/app/communities/{cid}/pages", headers=h, timeout=30)
    kandidaten = []
    if r.status_code == 200:
        for pagina in r.json() or []:
            module_id = pagina.get("module_id")
            if module_id and module_id not in kandidaten:
                kandidaten.append(module_id)
    for module_id in kandidaten:
        rr = s.get(f"{SOCIE}/v2/app/communities/{cid}/modules/{module_id}/allunited_pwa/url",
                   headers=h, timeout=20)
        if rr.status_code == 200 and (rr.json() or {}).get("url"):
            return module_id
    fout("De bewaarde boekingslink is verlopen en de boekingsmodule kon niet zelf "
         "gevonden worden. Open Starthier.html, plak een nieuwe boekingslink en werk "
         "book_padel_secrets.json bij.")


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    # ---- doeldata
    booking_date = arg_waarde("--date") or volgende_speeldag(int(CONFIG["weekday"])).isoformat()
    d = date.fromisoformat(booking_date)
    if d.weekday() != int(CONFIG["weekday"]) and not arg_waarde("--date"):
        fout(f"datum {datum_nl(booking_date)} is geen {DAGEN[int(CONFIG['weekday'])]} "
             f"(klopt de automatisering wel?)")

    # ---- inloggegevens
    secrets_path = Path(__file__).with_name("book_padel_secrets.json")
    if not secrets_path.exists():
        fout(f"bestand met inloggegevens ontbreekt ({secrets_path.name})")
    try:
        creds = json.loads(secrets_path.read_text(encoding="utf-8"))
    except ValueError:
        fout("book_padel_secrets.json is geen geldige JSON")

    if "VUL_IN" in (creds.get("email") or "") or "VUL_IN" in (creds.get("password") or ""):
        fout("De inloggegevens zijn nog niet ingevuld. Draai in Home Assistant het script "
             "'Padel: bestanden klaarzetten' en vul daar je e-mailadres en wachtwoord in.")

    s = requests.Session()
    s.headers["Accept"] = "application/json"

    au_headers, section = au_sessie(s, creds)
    log(f"boekingssysteem: {section}")

    # 4. schema ophalen
    playercount = str(len(CONFIG["players"]) or CONFIG.get("playercount") or 4)
    r = s.get(f"{AU}/api/booking/webshop/filter-groups/{CONFIG['filter_group_id']}",
              params={"playercount": playercount, "date": booking_date, "mode": "schematic"},
              headers=au_headers, timeout=30)
    if r.status_code != 200:
        fout(f"ophalen schema mislukt ({r.status_code}): {r.text[:200]}")
    data = r.json()["data"]
    periodes = data.get("schematicPeriods") or []

    # 5. vrij slot zoeken
    doel = arg_waarde("--time") or CONFIG["time"]
    voorkeur_baan = arg_waarde("--court") or CONFIG.get("court")
    icon = (CONFIG.get("sport_icon") or "").lower()

    def juiste_sport(p):
        if not icon:
            return True
        return (p.get("icon") or "").lower() == icon or icon in (p.get("groupDescription") or "").lower()

    vrij = [p for p in periodes
            if p.get("status") == "available" and p.get("items") and juiste_sport(p)]
    op_tijd = [p for p in vrij if p.get("timeFrom") == doel]

    if not op_tijd:
        eerder, later = plus_minuten(doel, -30), plus_minuten(doel, 30)
        vrije_tijden = {p.get("timeFrom") for p in vrij}
        alt = [t for t in (eerder, later) if t in vrije_tijden]
        if alt:
            fout(f"geen baan vrij om {doel} op {datum_nl(booking_date)}. "
                 f"Wel vrij om {' en om '.join(alt)}.")
        fout(f"geen baan vrij om {doel} op {datum_nl(booking_date)}, "
             f"ook niet om {eerder} of {later}.")

    voorkeur = voorkeur_baan
    slot = next((p for p in op_tijd if p.get("group") == voorkeur), None)
    if slot is None:
        if not CONFIG.get("fallback_other_court", True):
            fout(f"{voorkeur} is bezet om {doel} op {datum_nl(booking_date)}")
        slot = op_tijd[0]
        log(f"{voorkeur} bezet, uitwijk naar {slot['group']}")

    item = next((i for i in slot["items"] if str(i.get("playerCount")) == playercount),
                slot["items"][0])

    # 6. in winkelwagen
    r = s.post(f"{AU}/api/booking/webshop/cart/lines", headers=au_headers, timeout=30,
               json={"itemType": item["type"], "itemKey": item["key"], "dateSchedule": booking_date,
                     "extraItems": [], "players": [], "actionType": "add_to_cart"})
    d_resp = r.json().get("data", {}) if r.status_code == 200 else {}
    if r.status_code != 200 or d_resp.get("error") or "key" not in d_resp:
        fout(f"baan in winkelwagen zetten mislukt ({r.status_code}): {r.text[:300]}")
    reservation_key = d_resp["key"]

    bericht = (f"{slot['group']} gereserveerd op {datum_nl(booking_date)} "
               f"van {slot['timeFrom']} tot {slot['timeTo']}")

    # 7. spelers
    if CONFIG.get("players"):
        r = s.put(f"{AU}/api/booking/webshop/cart/lines/reservation/{reservation_key}",
                  headers=au_headers, timeout=30,
                  json={"itemType": "reservation", "itemKey": reservation_key,
                        "dateSchedule": booking_date, "extraItems": [],
                        "players": [{"key": str(k)} for k in CONFIG["players"]],
                        "actionType": "update_cartline"})
        d_resp = r.json().get("data", {}) if r.status_code == 200 else {}
        if r.status_code != 200 or d_resp.get("error"):
            fout(f"spelers instellen mislukt ({r.status_code}): {r.text[:300]}")

    if dry_run:
        print(f"DRY-RUN: {bericht} (niet bevestigd; de winkelwagenregel vervalt vanzelf)")
        return

    # 8. bevestigen
    r = s.put(f"{AU}/api/booking/webshop/cart", headers=au_headers, timeout=30,
              json={"actionType": "confirm_cart"})
    d_resp = r.json().get("data", {}) if r.status_code == 200 else {}
    if r.status_code != 200 or d_resp.get("error"):
        fout(f"bevestigen mislukt ({r.status_code}): {r.text[:300]}")
    print(bericht)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        fout(f"netwerkfout: {e}")
