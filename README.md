# Fotoserver

Ein Bilderrahmen für die eigenen Fotos, aus einem Google-Takeout-Export
gespeist und vollständig offline betrieben. Läuft auf einem kleinen
Mini-PC im Heimnetz: Import, Slideshow im Browser, Ausgabe auf einen
Chromecast, alles ohne fremden Dienst und ohne Konto.

Entstanden aus einem konkreten Bedürfnis: rund 24.000 Fotos aus zwanzig
Jahren sollten morgens beim Frühstück laufen — mit Datum und Ort darunter,
groß genug, um sie vom Tisch aus zu lesen.

## Was es kann

- **Import aus Google Takeout.** Liest den entpackten Baum ein, dedupliziert
  über SHA-256, zieht Datum und Position aus EXIF und den Takeout-JSONs.
  Inkrementell: unveränderte Dateien werden nicht neu gehasht.
- **Slideshow** im Vollbild mit Überblendung, Wischgesten, Bedienleiste für
  Geräte ohne Tastatur. Schriftgröße und Leistengröße lassen sich mit der
  Maus ziehen.
- **Reverse-Geocoding offline** aus GeoNames-Daten, ohne Netzabfrage und
  ohne numpy — ein Gradraster reicht für 24.000 Fotos in fünf Sekunden.
- **Ausgabe auf Google Cast.** Der Server sendet selbst, ohne offenen
  Browser. Datum und Ort werden ins Bild gerechnet, weil ein Chromecast nur
  fertige Bilder anzeigt.
- **Fernbedienung vom Tablet.** Läuft ein Cast, zeigt die Slideshow dasselbe
  Bild wie der Fernseher, und Wischen blättert dort weiter.
- **Korrekturen von Hand** für Bilder ohne verlässliches Datum oder ohne
  Position, mit Ortssuche, Albumvorschlägen und Filmstreifen der zeitlichen
  Nachbarn. Sie stehen in eigenen Spalten und überleben jeden Neuimport.
- **Papierkorb**, geschlüsselt über die Prüfsumme — ein gelöschtes Bild
  bleibt auch nach einem überlappenden Export draußen.
- **Sicherung auf eine USB-Platte** aus dem Browser heraus: angesteckte
  Datenträger stehen zur Auswahl, wahlweise alles oder nur Datenbank und
  Einstellungen.

**Originaldateien werden nie verändert.** Alle Korrekturen leben in der
Datenbank, die Anzeige bedient sich aus erzeugten Thumbnails.

## Aufbau

| Datei | Aufgabe |
|---|---|
| `import_photos.py` | Takeout einlesen, dedupliziert, inkrementell |
| `make_thumbs.py` | Thumbnails in zwei Größen erzeugen |
| `geocode.py` | Orte zuordnen, offline |
| `castbild.py` | Bild mit Datum und Ort für den Chromecast |
| `viewer.py` | Webserver (Flask): Slideshow, Einstellungen, Cast |
| `templates/` | Slideshow, Einstellungsseite, Papierkorb |

Datenhaltung ist SQLite, eine Datei. Kein ORM, keine Migrationen, keine
Fremdabhängigkeit außer Flask, Pillow und PyChromecast.

## Einrichten

```bash
python3 -m venv ~/photoserver-venv
~/photoserver-venv/bin/pip install flask pillow pillow-heif PyChromecast

# Ortsdaten aufbereiten (einmalig, braucht kurz Netz)
cd geo
curl -O https://download.geonames.org/export/dump/cities1000.zip
curl -O https://download.geonames.org/export/dump/countryInfo.txt
curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
curl -O https://download.geonames.org/export/dump/alternateNamesV2.zip
unzip -o cities1000.zip && unzip -o alternateNamesV2.zip
cd .. && python3 geocode.py --aufbereiten

# Fotos einlesen
python3 import_photos.py --source ~/takeout-import/Takeout
~/photoserver-venv/bin/python3 make_thumbs.py
~/photoserver-venv/bin/python3 geocode.py

# Starten
~/photoserver-venv/bin/python3 viewer.py --port 8080
```

Als Dienst: `photoserver.service.beispiel` anpassen und nach
`/etc/systemd/system/` kopieren. `exiftool` wird für den Import gebraucht.

Soll der Rechner sich per `POST /api/ausschalten` selbst herunterfahren
können — gedacht für einen kopflosen Betrieb —, gehört zusätzlich
`50-fotoserver-ausschalten.rules.beispiel` mit eingetragenem Benutzernamen
nach `/etc/polkit-1/rules.d/`. Ohne die Regel antwortet der Aufruf mit 503,
statt wirkungslos OK zu melden.

Der Aufruf hält den Rechner an (`systemctl halt`), schaltet ihn aber nicht
aus: das Board bleibt unter Strom, geschrieben wird nichts mehr. Den Strom
nimmt anschließend eine schaltbare Steckdose weg. Das ist Absicht — viele
BIOS bringen den Rechner mit „After Power Loss: Power On" nur dann wieder
hoch, wenn der Strom **im Betrieb** verschwand; nach einem echten
`poweroff` bliebe er beim Wiedereinschalten der Dose aus. Die Steckdose ist
damit der Ein- und Ausschalter. Wer stattdessen Wake-on-LAN oder einen
BIOS-Zeitplan zum Einschalten nutzt, ändert die beiden Stellen in
`viewer.py` und der polkit-Regel auf `poweroff` zurück.

### Sicherung aus dem Browser

Der Knopf auf der Einstellungsseite sichert auf eine angesteckte USB-Platte,
wahlweise das ganze Benutzerverzeichnis oder nur Datenbank und
Einstellungen. Das eigentliche Sichern erledigt ein Shell-Skript
(`sicherung.sh`, nicht im Repository: es enthält die Seriennummer der
eigenen Platte).

Der Webdienst bekommt dafür **kein einziges zusätzliches Recht**. Er läuft
mit `NoNewPrivileges` und darf nur in sein eigenes Verzeichnis schreiben —
eine Platte einhängen kann er nicht. Stattdessen legt er einen Auftrag ab,
zwei Zeilen in `sicherung-auftrag.txt`:

```
photoserver-sicherung.path      sieht die Datei auftauchen
  └── photoserver-sicherung.service  schiebt sie beiseite und ruft
        sicherung.sh --auftrag       als root auf
```

Beide Units liegen als `*.beispiel` bei. Das Skript prüft anschließend
selbst, was ihm da aufgetragen wurde: nur ein USB-Datenträger, niemals die
Systemplatte, niemals formatieren, und bei mehr als 500 zu löschenden
Dateien Abbruch statt Rückfrage — vor dem Browser sitzt niemand, der
bestätigen könnte. Der Fortschritt geht über zwei Dateien zurück an die
Seite (`sicherung-stand.json`, `sicherung-web.log`).

Gemeldet wird „fertig" erst **nach** `sync` und `umount` — vorher wäre es
eine Lüge, denn der Schreibcache des Kernels ist dann noch nicht leer. Die
Meldung sagt deshalb gleich, ob die Platte abgezogen werden darf.

## Zwei Dinge, die überraschen können

**GeoNames `cities1000` kennt viele deutsche Gemeinden nicht.** Die Datei
enthält nur Feature-Klasse P ab 1000 Einwohnern; zahlreiche Gemeinden führt
GeoNames als ADM4, ihre Ortsteile mit Einwohnerzahl 0. Beides fällt heraus,
und die Fotos tragen dann den Namen der nächsten Großstadt.
`geocode.py --gemeinden` trägt die fehlenden Gemeinden aus der Länderdatei
nach; `geo/orte-ergaenzung.tsv` ist das Ergebnis für Deutschland.

**Die Unschärfe beim Zuordnen muss anteilig sein.** Mit einer festen Grenze
(„der größere Ort gewinnt, wenn er höchstens 6 km weiter weg ist") schluckt
jede Großstadt ihre Nachbarorte. `UNSCHAERFE_ANTEIL` bezieht das Fenster
stattdessen auf die Entfernung zum nächstgelegenen Ort — steht man mitten im
Ort, zählt nur er.

## Datengrundlage

Ortsdaten von [GeoNames](https://www.geonames.org/), lizenziert unter
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Das gilt auch für
die abgeleitete Datei `geo/orte-ergaenzung.tsv`.

## Lizenz

MIT — siehe `LICENSE`.

Die Oberfläche und alle Kommentare im Quelltext sind auf Deutsch. Das ist
Absicht: es ist ein Werkzeug für den eigenen Haushalt, kein Produkt.
