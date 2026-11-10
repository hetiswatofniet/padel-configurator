#!/usr/bin/env python3
"""Configurator voor de baantjes-reservering.

Draait op een machine met Python en netwerktoegang tot de clubapp (bijvoorbeeld
Home Assistant). De wizard zelf is een webpagina; de login gebeurt hier, niet in
de browser, omdat de clubapp geen rechtstreekse browserverbinding toestaat.

    python3 configureer.py                     # schrijft niets, alleen downloaden
    python3 configureer.py --doel /config/scripts   # schrijft de bestanden weg

Bij het starten verschijnt een URL met een toegangscode; open die op je laptop.
"""

import argparse
import json
import secrets
import socket
import sys
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

import padel_setup as setup

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAGINA = ROOT / "Starthier.html"
VERSIE = "1.1.4"
POORT = 8124
TOEGANG = secrets.token_urlsafe(8)
DOEL: Path | None = None

STAAT = {"sess": None, "token": None, "cid": None, "mid": None,
         "au": None, "context": {}, "email": ""}


class Fout(Exception):
    pass


def _sessie() -> tuple:
    if not STAAT["sess"] or not STAAT["token"]:
        raise Fout("Nog niet ingelogd.")
    return STAAT["sess"], STAAT["token"]


def api_login(body: dict) -> dict:
    email = (body.get("email") or "").strip()
    wachtwoord = body.get("wachtwoord") or ""
    if not email or not wachtwoord:
        raise Fout("Vul e-mailadres en wachtwoord in.")

    sess = requests.Session()
    sess.headers["Accept"] = "application/json"
    token = setup.socie_login(sess, email, wachtwoord)
    rollen = setup.jwt_claims(token).get("roles") or {}
    if not rollen:
        raise Fout("Dit account is nog aan geen enkele vereniging gekoppeld.")

    verenigingen = []
    for cid, mids in rollen.items():
        mid = list(mids)[0]
        verenigingen.append({"cid": cid, "mid": mid,
                             "naam": setup.community_naam(sess, token, cid, mid)})

    gekozen = verenigingen[0]
    if len(verenigingen) > 1 and body.get("vereniging"):
        gekozen = next((v for v in verenigingen
                        if v["naam"] == body["vereniging"]), gekozen)

    STAAT.update(sess=sess, token=token, au=None, context={}, email=email,
                 cid=gekozen["cid"], mid=gekozen["mid"])
    ext, naam = setup.eigen_external_reference(sess, token, gekozen["cid"], gekozen["mid"])
    return {"verenigingen": verenigingen, "gekozen": gekozen["naam"],
            "ik": {"key": ext, "naam": naam}}


def api_koppel(body: dict) -> dict:
    sess, token = _sessie()
    cid, mid = STAAT["cid"], STAAT["mid"]
    module_id = setup.vind_boekingsmodule(sess, token, cid, mid)
    if not module_id:
        raise Fout("Deze vereniging heeft geen AllUnited-boekingsmodule in het menu staan.")

    au = setup.allunited_sessie(sess, token, cid, mid, module_id)
    groepen = setup.filter_groups(sess, au)
    if not groepen:
        raise Fout("Geen sporten gevonden in het boekingssysteem.")

    sporten = []
    for g in groepen:
        opties = sorted({int(o.get("playercount") or 0)
                         for o in (g.get("playerCountOptions") or [])} - {0})
        playercount = int(g.get("defaultPlayerCount") or (max(opties) if opties else 4))
        banen, open_tijd, dicht_tijd = setup.baan_overzicht(sess, au, g["id"], playercount)
        sporten.append({
            "id": g["id"], "naam": g.get("name") or "sport",
            "playercounts": opties or [playercount], "playercount": playercount,
            "open": open_tijd, "dicht": dicht_tijd,
            "banen": [{"naam": b["naam"], "fase": b["fase"] % 60} for b in banen],
        })

    STAAT.update(au=au, module_id=module_id, context={})
    setup.bewaar_module_id(module_id)
    return {"club": au.get("club"), "contact": au.get("contact"),
            "module_id": module_id, "sporten": sporten}


def api_spelers(body: dict) -> dict:
    sess, token = _sessie()
    au = STAAT.get("au")
    if not au:
        raise Fout("Koppel eerst het boekingssysteem.")
    term = (body.get("term") or "").strip()
    if len(term) < 2:
        return {"spelers": []}

    fg_id = body.get("fg_id")
    playercount = int(body.get("playercount") or 4)
    sleutel = f"{fg_id}:{playercount}"
    if sleutel not in STAAT["context"]:
        STAAT["context"][sleutel] = setup.zoek_context(sess, au, fg_id, playercount)

    gevonden = setup.au_zoek_spelers(sess, au, STAAT["context"][sleutel], term)
    if not gevonden:
        gevonden = setup.leden_zoek(sess, token, STAAT["cid"], STAAT["mid"], term)
    return {"spelers": gevonden[:15]}


def api_genereer(body: dict) -> dict:
    if not STAAT.get("au"):
        raise Fout("Koppel eerst het boekingssysteem.")
    try:
        weekday = int(body["weekday"])
        dagen = int(body["days_ahead"])
        uur, minuut = (int(x) for x in body["time"].split(":"))
    except (KeyError, TypeError, ValueError):
        raise Fout("Dag, tijd of aantal dagen ontbreekt of is ongeldig.")
    if not (0 <= weekday <= 6 and 0 <= dagen <= 14):
        raise Fout("Dag of aantal dagen buiten bereik.")

    slot_eind_min = uur * 60 + minuut + 60
    slot_eind = f"{slot_eind_min // 60:02d}:{slot_eind_min % 60:02d}"
    spelers_lijst = body.get("spelers") or []

    config = {
        "club": body.get("club") or "",
        "module_id": STAAT.get("module_id", ""),
        "filter_group_id": body.get("fg_id"),
        "sport_naam": body.get("sport_naam") or "",
        "sport_icon": (body.get("sport_icon") or "").lower(),
        "weekday": weekday,
        "time": body["time"],
        "court": body.get("court") or "",
        "fallback_other_court": True,
        "days_ahead": dagen,
        "playercount": len(spelers_lijst) or int(body.get("playercount") or 4),
        "players": [s["key"] for s in spelers_lijst],
        "players_naam": [s["naam"] for s in spelers_lijst],
    }
    # Alleen wat de gebruiker zelf invoerde; nooit id's, clubgegevens of tokens.
    creds = {"email": STAAT["email"], "password": body.get("wachtwoord") or ""}

    bestanden, trig = setup.bouw_bestanden(config, body.get("notify") or "", slot_eind, creds)

    geschreven = []
    if DOEL:
        DOEL.mkdir(parents=True, exist_ok=True)
        for naam, inhoud in bestanden.items():
            pad = DOEL / naam
            pad.write_text(inhoud, encoding="utf-8")
            if naam == "book_padel_secrets.json":
                pad.chmod(0o600)          # alleen de eigenaar mag dit lezen
            geschreven.append(str(pad))

    return {"bestanden": bestanden, "trigger": trig, "slot_eind": slot_eind,
            "config": config, "geschreven": geschreven}


ROUTES = {"/api/login": api_login, "/api/koppel": api_koppel,
          "/api/spelers": api_spelers, "/api/genereer": api_genereer}


class Handler(BaseHTTPRequestHandler):
    server_version = "baantjes-configurator"

    def log_message(self, fmt, *args):
        if "404" in (fmt % args):
            sys.stderr.write("%s\n" % (fmt % args))

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _pad(self) -> str:
        """Pad zonder de toegangscode ervoor."""
        pad = urlparse(self.path).path
        return pad[len("/" + TOEGANG):] if pad.startswith("/" + TOEGANG) else ""

    def do_GET(self) -> None:
        pad = self._pad()
        if pad in ("/", "/index.html"):
            if not PAGINA.exists():
                self._json(500, {"fout": "Starthier.html ontbreekt naast configureer.py"})
                return
            inhoud = PAGINA.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(inhoud)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(inhoud)
            return
        self._json(404, {"fout": "niet gevonden"})

    def do_POST(self) -> None:
        route = ROUTES.get(self._pad())
        if not route:
            self._json(404, {"fout": "onbekend endpoint"})
            return
        try:
            lengte = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(lengte) or b"{}")
            self._json(200, route(body))
        except (setup.Fout, Fout) as e:
            self._json(400, {"fout": str(e)})
        except requests.RequestException as e:
            self._json(502, {"fout": f"Netwerkfout: {e}"})
        except Exception as e:                          # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"fout": f"Onverwachte fout: {e}"})


def lokaal_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> int:
    global DOEL
    parser = argparse.ArgumentParser(description="Configurator voor de baantjes-reservering")
    parser.add_argument("--doel", help="map waar de bestanden direct in geschreven worden "
                                       "(bijv. /config/scripts)")
    parser.add_argument("--poort", type=int, default=POORT)
    parser.add_argument("--geen-browser", action="store_true",
                        help="open de wizard niet automatisch in de browser")
    args = parser.parse_args()
    POORT_LOCAL = args.poort
    DOEL = Path(args.doel).expanduser().resolve() if args.doel else None

    if not PAGINA.exists():
        print(f"Starthier.html ontbreekt (verwacht naast {Path(__file__).name})")
        return 1

    server = None
    for poging in range(POORT_LOCAL, POORT_LOCAL + 10):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", poging), Handler)
            POORT_LOCAL = poging
            break
        except OSError:
            continue
    if server is None:
        print(f"Geen vrije poort gevonden vanaf {POORT_LOCAL}. Stop een eerder gestart venster.")
        return 1
    url = f"http://{lokaal_ip()}:{POORT_LOCAL}/{TOEGANG}/"
    regels = ["=" * 60, f"  Baantjes-reservering: configurator v{VERSIE}", "=" * 60,
              f"  Open op je laptop: {url}"]
    if DOEL:
        regels.append(f"  Bestanden worden geschreven naar: {DOEL}")
    regels += ["  Stoppen: Ctrl+C   (start hem alleen tijdens het instellen)", "=" * 60]
    print("\n".join(regels), flush=True)
    if "--geen-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:                               # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGestopt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
