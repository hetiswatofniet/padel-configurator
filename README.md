# Baantjes-reservering instellen

Eenmalige configurator die de bestanden maakt voor de reserveringsautomatisering in
Home Assistant. Je logt één keer in met je clubaccount; daarna draait alles vanzelf.

## Snelste route: kant-en-klaar programma (geen Python nodig)

Download bij **Releases** het bestand voor jouw computer:

| bestand | platform |
|---|---|
| `padel-configurator-windows.exe` | Windows 10/11 |
| `padel-configurator-macos-arm64` | Mac met Apple-chip (M1 of nieuwer) |
| `padel-configurator-macos-intel` | Mac met Intel-processor |
| `padel-configurator-linux` | Linux |

Start het programma. Er verschijnt een venster met een adres als
`http://192.168.1.20:8124/Ab12Cd34Ef/`; je browser opent dat meestal vanzelf. Doorloop
de wizard: inloggen, sport en baan kiezen, dag en tijd, met wie je speelt. Klik
**Bestanden maken** en download de vier bestanden. Daarna mag je het venster sluiten.

- **Windows:** SmartScreen zegt "Windows heeft uw pc beschermd" -> *Meer informatie* ->
  *Toch uitvoeren*. Het programma is niet ondertekend.
- **Mac:** de eerste keer weigert macOS het bestand -> rechtsklik op het bestand ->
  *Open* -> *Open*. Of eenmalig in Terminal:
  `xattr -dr com.apple.quarantine padel-configurator-macos-arm64`.
- Het programma luistert alleen zolang het draait, op je eigen netwerk, met een
  eenmalige toegangscode in het adres. Sluiten = klaar.

## Route via Home Assistant

1. Zet deze map in Home Assistant, bijvoorbeeld in `/config/padel/`.
2. Open de add-on **Terminal & SSH** en start
   `cd /config/padel && python3 configureer.py --doel /config/scripts`.
3. Open het adres uit de terminal op je laptop en doorloop de wizard.
4. De bestanden staan daarna direct in `/config/scripts`. Stop met Ctrl+C.

## Wat je krijgt

| bestand | waar het heen gaat |
|---|---|
| `book_padel.py` | `/config/scripts/book_padel.py` |
| `book_padel_secrets.json` | `/config/scripts/book_padel_secrets.json` |
| `book_padel_automation.yaml` | importeren bij Automatiseringen |
| `INSTALL.md` | de exacte stappen in Home Assistant |

## Belangrijk

- `book_padel_secrets.json` bevat je club-wachtwoord. Deel dat bestand niet.
- De wizard haalt de vereniging, de boekingsmodule, de banen en de leden zelf op uit
  het menu van de clubapp. Je hoeft dus geen id's of links op te zoeken.
- Notificaties zijn niet automatisch ingesteld: in `book_padel_automation.yaml` staat een
  uitgecommentarieerd blok waarin je je eigen `notify`-entiteit invult.
- Werkt je club met hetzelfde boekingsplatform, dan vindt de wizard vereniging, sporten,
  banen en leden zelf.

## Zelf bouwen

```bash
pip install pyinstaller requests
pyinstaller --noconfirm --clean build.spec
```

De workflow in `.github/workflows/build.yml` bouwt de Windows-, macOS- en Linux-versie
en zet ze bij een tag als Release-assets.

## Bestanden in deze map

```
Starthier.html            de wizard (webpagina, zit in het programma)
configureer.py            start de wizard en doet de login bij de clubapp
padel_setup.py            gedeelde logica (login, banen, leden, genereren)
template_book_padel.py    basis voor het script in Home Assistant
build.spec                PyInstaller-specificatie
```
