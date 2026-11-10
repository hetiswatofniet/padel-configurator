#!/usr/bin/env python3
"""Rooktest voor de gebouwde configurator: start hem, haal de pagina op, stop hem.

Gebruik: python scripts/rooktest.py <pad naar binary> [--verwacht 1.2.2]

Faalt (exit 1) als het programma niet start, geen URL print, de pagina geen 200
geeft of de verwachte versie niet in de HTML staat. Werkt op Windows, macOS en
Linux; wordt in de CI op elke runner uitgevoerd voordat de release-assets
worden geupload.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL_RE = re.compile(r"http://\S+?/([A-Za-z0-9_\-]{6,})/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--verwacht", default="", help="versienummer dat in de pagina moet staan")
    ap.add_argument("--poort", default="8129")
    ap.add_argument("--logbestand", default="",
                    help="bestand waarin het programma de URL schrijft (voor een windowed app)")
    ap.add_argument("--log", default="", help="logbestand waarin het programma de URL schrijft")
    args = ap.parse_args()

    if not os.path.exists(args.binary):
        print(f"FOUT: {args.binary} bestaat niet")
        return 1

    proc = subprocess.Popen(
        [args.binary, "--geen-browser", "--poort", args.poort],
        stdout=subprocess.PIPE if not args.logbestand else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if not args.logbestand else subprocess.DEVNULL,
        text=True,
    )

    token, regels = None, []
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            if args.logbestand:
                if os.path.exists(args.logbestand):
                    tekst = open(args.logbestand, encoding="utf-8", errors="replace").read()
                    m = URL_RE.search(tekst)
                    if m:
                        token = m.group(1)
                        break
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
                continue

            regel = proc.stdout.readline()
            if not regel:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
                continue
            regels.append(regel.rstrip())
            m = URL_RE.search(regel)
            if m:
                token = m.group(1)
                break

        if not token:
            print("FOUT: geen URL in de uitvoer. Uitvoer was:")
            print("\n".join(regels[-15:]))
            return 1

        url = f"http://127.0.0.1:{args.poort}/{token}/"
        laatste = None
        for _ in range(30):
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    html = r.read().decode("utf-8", "replace")
                if r.status == 200:
                    laatste = html
                    break
            except (urllib.error.URLError, TimeoutError) as fout:
                laatste = None
                foutmelding = fout
                time.sleep(1)

        if laatste is None:
            print(f"FOUT: pagina {url} niet bereikbaar")
            return 1

        print(f"OK: pagina bereikbaar op {url} ({len(laatste)} bytes)")

        gevonden = re.search(r"versie\s+([\d.]+)", laatste)
        versie = gevonden.group(1) if gevonden else "?"
        print(f"OK: versie in de pagina: {versie}")
        if args.verwacht and versie != args.verwacht:
            print(f"FOUT: verwachtte versie {args.verwacht}, kreeg {versie}")
            return 1

        for kenmerk in ("padel_installatie.yaml", "toonHandleiding"):
            if kenmerk not in laatste:
                print(f"FOUT: '{kenmerk}' ontbreekt in de pagina")
                return 1
        print("OK: pakket-bestand en handleiding worden aangeboden")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
