#!/usr/bin/env python3
"""
Fotoserver - Webviewer mit Slideshow und Einstellungsseite.

Zeigt die Bilder aus photos.db als Vollbild-Slideshow (Ziel 1920x1080).
Ausgeliefert werden die fertigen 1920-px-Thumbnails aus cache/display/,
die Originaldateien werden nie angefasst.

Aufruf:
    ~/photoserver-venv/bin/python3 viewer.py
    ~/photoserver-venv/bin/python3 viewer.py --port 8080 --host 0.0.0.0

Im Browser:
    http://<rechner>:8080/                 Slideshow
    http://<rechner>:8080/einstellungen    Einstellungen und Import

Die Slideshow richtet sich nach settings.json.  Zum Ausprobieren lassen sich
die Werte per URL uebersteuern, ohne etwas zu speichern:
    ?modus=zufall|chrono|neueste    ?dauer=12    ?album=<Name>

Tasten:  Leertaste = Pause,  Pfeil links/rechts = blaettern,  F = Vollbild
"""

import argparse
import hashlib
import json
import logging
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    from flask import (Flask, abort, g, has_app_context, jsonify, redirect,
                       render_template, request, send_file)
except ImportError:
    sys.exit("Flask fehlt.  ~/photoserver-venv/bin/pip install flask")

# Beides nur fuer die Ausgabe auf dem Fernseher.  Fehlt es, laeuft der
# Viewer unveraendert weiter - die Cast-Schaltflaeche bleibt dann grau.
try:
    import pychromecast
except ImportError:
    pychromecast = None
try:
    import castbild
except ImportError:
    castbild = None

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "photos.db"
CACHE = BASE / "cache"
SETTINGS = BASE / "settings.json"
IMPORT_LOG = BASE / "import_web.log"

DAUER_MIN, DAUER_MAX = 2, 300
MODI = ("zufall", "chrono", "neueste")

# Schriftgroesse der Beschriftung und Groesse der Bedienleiste sind von Hand
# ziehbar.  Die Grenzen halten beides in einem Bereich, in dem es noch
# bedienbar bzw. lesbar bleibt.
SKALA_MIN, SKALA_MAX = 0.4, 4.0

# Alles, was hier steht, ueberlebt einen Neustart - es landet in
# settings.json.  Die Slideshow schickt Aenderungen einzeln, der Server
# mischt sie in den Bestand (siehe api_einstellungen).
VORGABEN = {
    "dauer": 8,
    "modus": "zufall",
    "alben": [],
    "ohne_album": True,      # Bilder, die in keinem Album stecken, mitzeigen
    "info_skala": 1.0,       # Datum und Ort
    "leiste_skala": 1.0,     # Knopfleiste unten rechts
    "cast_an": False,        # parallel an einen Fernseher senden
    "cast_geraet": "",       # dessen Name im Netz
    "cast_skala": 1.0,       # Feinjustierung der Schrift auf dem Fernseher
}

# Port, unter dem der Server laeuft.  Der Chromecast holt sich die Bilder
# selbst und braucht dafuer eine vollstaendige Adresse.
PORT = 8080

HEIMATLAND = "DE"       # Land wird bei eigenen Bildern nicht mitgeschrieben
NAH_KM = 25             # weiter weg heisst es "bei <Ort>"

# GeoNames liefert die amtlichen Bezeichnungen.  Unter einem Bild steht
# lieber der gebraeuchliche Name.  Greift bei der Anzeige, die Datenbank
# bleibt unveraendert - hier laesst sich jederzeit nachbessern.
LAENDER_ANZEIGE = {
    "GB": "Großbritannien",       # amtlich "Vereinigtes Königreich"
    "US": "USA",                  # amtlich "Vereinigte Staaten"
}


def land_name(cc, land):
    return LAENDER_ANZEIGE.get(cc, land)


# Von Hand geklaerte Mehrdeutigkeiten.  Es gibt ein Interlaken in
# Kalifornien, das sogar groesser ist als das schweizerische - gemeint
# ist in dieser Sammlung aber immer die Schweiz.  Schlaegt die Zaehlung nach
# Laendern (siehe land_gewicht) hier nicht an, entscheidet diese Liste.
ORTS_VORGABEN = {
    "interlaken": "CH",
}

# Namen, bei denen die Region gemeint ist und nicht der gleichnamige Ort.
# "Oregon" ist der Bundesstaat, nicht das Dorf in Wisconsin.
REGION_VOR_ORT = {"oregon", "hawaii", "england", "washington", "new york"}

MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")

# make_thumbs.py braucht Pillow, also den venv-Interpreter.  Die beiden
# anderen kommen mit der Standardbibliothek aus.
VENV_PY = BASE.parent / "photoserver-venv" / "bin" / "python3"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)

app = Flask(__name__)
_lokal = threading.local()


def _leseverbindung():
    if not DB_PATH.exists():
        sys.exit(f"Keine Datenbank unter {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # waehrend eines Imports kann die Datei kurz gesperrt sein
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def db():
    """Leseverbindung fuer die laufende Anfrage.

    Sie haengt am Anwendungskontext und wird in db_schliessen() wieder
    zugemacht.  Das ist wichtig: Werkzeugs Server macht fuer jede Anfrage
    einen eigenen Thread auf, und eine Verbindung je Thread offen zu lassen
    fuellt in ein paar Stunden die 1024 erlaubten Dateideskriptoren -
    danach kommt bei jedem Aufruf nur noch "Too many open files".

    Ausserhalb einer Anfrage (beim Start, in Hintergrundfaeden) gibt es
    stattdessen eine dauerhafte Verbindung je Thread; davon existieren nur
    eine Handvoll.
    """
    if has_app_context():
        conn = getattr(g, "db_conn", None)
        if conn is None:
            conn = g.db_conn = _leseverbindung()
        return conn
    conn = getattr(_lokal, "conn", None)
    if conn is None:
        conn = _lokal.conn = _leseverbindung()
    return conn


@app.teardown_appcontext
def db_schliessen(_ausnahme=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def db_schreiben():
    """Kurzlebige Schreibverbindung fuer Korrekturen von Hand.

    Die Leseverbindung bleibt bewusst read-only.  Geschrieben wird nur
    hier und vom Import, und der laeuft als eigener Prozess.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# Korrekturen von Hand stehen in eigenen Spalten.  Die Werte aus dem Import
# bleiben daneben unangetastet - so ueberschreibt ein spaeterer Lauf von
# import_photos.py oder geocode.py nichts von Hand Eingetragenes.
HAND_SPALTEN = (
    ("hand_date", "TEXT"),
    ("hand_date_confidence", "TEXT"),
    ("hand_place", "TEXT"),
    ("hand_country", "TEXT"),
    ("hand_cc", "TEXT"),
    ("hand_at", "TEXT"),
)


def schema_sicherstellen():
    conn = db_schreiben()
    try:
        vorhanden = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
        neu = [(s, t) for s, t in HAND_SPALTEN if s not in vorhanden]
        for spalte, typ in neu:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {spalte} {typ}")
        if neu:
            print(f"Spalten angelegt: {', '.join(s for s, _ in neu)}")

        # Streichliste.  Schluessel ist der SHA-256, nicht die id - dann
        # greift sie auch, wenn ein spaeterer Takeout dieselbe Datei noch
        # einmal liefert.  Genau dafuer dedupliziert der Import ohnehin
        # ueber die Pruefsumme.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS geloescht (
                sha256      TEXT PRIMARY KEY,
                photo_id    INTEGER,
                filename    TEXT,
                geloescht_at TEXT
            )""")
        # endgueltig_at: aus dem Papierkorb entfernt.  Der Eintrag bleibt
        # trotzdem stehen - sonst kaeme das Bild beim naechsten Takeout
        # zurueck.  Er wird nur nicht mehr angezeigt.
        weg = {r[1] for r in conn.execute("PRAGMA table_info(geloescht)")}
        if "endgueltig_at" not in weg:
            conn.execute("ALTER TABLE geloescht ADD COLUMN endgueltig_at TEXT")
        conn.commit()
    finally:
        conn.close()


def thumb_path(sha, groesse="display"):
    return CACHE / groesse / sha[:2] / f"{sha}.jpg"


# ------------------------------------------------------------- Einstellungen

_einst_sperre = threading.Lock()


def einstellungen_lesen():
    """settings.json lesen und auf gueltige Werte begrenzen."""
    werte = dict(VORGABEN)
    try:
        werte.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        app.logger.warning("settings.json unlesbar (%s), nehme Vorgaben", exc)
        return dict(VORGABEN)
    return saubere_werte(werte)


def _skala(wert, vorgabe):
    try:
        z = float(wert)
    except (TypeError, ValueError):
        return vorgabe
    if z != z:                                  # NaN
        return vorgabe
    return round(max(SKALA_MIN, min(SKALA_MAX, z)), 3)


def saubere_werte(werte):
    try:
        dauer = int(werte.get("dauer", VORGABEN["dauer"]))
    except (TypeError, ValueError):
        dauer = VORGABEN["dauer"]
    modus = werte.get("modus")
    alben = werte.get("alben")
    if not isinstance(alben, list):
        alben = []
    return {
        "dauer": max(DAUER_MIN, min(DAUER_MAX, dauer)),
        "modus": modus if modus in MODI else VORGABEN["modus"],
        "alben": [str(a) for a in alben if str(a).strip()],
        "ohne_album": bool(werte.get("ohne_album", VORGABEN["ohne_album"])),
        "info_skala": _skala(werte.get("info_skala"),
                             VORGABEN["info_skala"]),
        "leiste_skala": _skala(werte.get("leiste_skala"),
                               VORGABEN["leiste_skala"]),
        "cast_an": bool(werte.get("cast_an", VORGABEN["cast_an"])),
        "cast_geraet": str(werte.get("cast_geraet")
                           or VORGABEN["cast_geraet"]),
        "cast_skala": _skala(werte.get("cast_skala"),
                             VORGABEN["cast_skala"]),
    }


def einstellungen_schreiben(werte):
    """Nur die mitgeschickten Schluessel aendern, der Rest bleibt stehen.

    Die Slideshow schickt beim Ziehen an der Schrift allein info_skala -
    ohne das Mischen wuerde sie damit Anzeigedauer, Modus und Albenauswahl
    auf die Vorgaben zuruecksetzen.
    """
    with _einst_sperre:
        bestand = dict(VORGABEN)
        try:
            vorhanden = json.loads(SETTINGS.read_text(encoding="utf-8"))
            if isinstance(vorhanden, dict):
                bestand.update(vorhanden)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        bestand.update({k: v for k, v in werte.items() if k in VORGABEN})
        sauber = saubere_werte(bestand)
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(sauber, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(SETTINGS)
    return sauber


def stand():
    """Kennung des aktuellen Zustands.  Aendert sie sich, holt sich die
    Slideshow eine neue Playlist - so wirken Einstellungen, Loeschungen
    und ein fertiger Import, ohne dass jemand den Bildschirm anfasst.

    Eingerechnet wird nur, was die Playlist wirklich betrifft.  Waere die
    Schreibzeit von settings.json der Massstab, wuerde jedes Ziehen an der
    Schriftgroesse auf allen Bildschirmen die Playlist neu mischen.
    """
    e = einstellungen_lesen()
    kern = json.dumps([e["dauer"], e["modus"], e["alben"], e["ohne_album"]],
                      ensure_ascii=False, sort_keys=True)
    try:
        weg = db().execute("SELECT COUNT(*) FROM geloescht").fetchone()[0]
    except sqlite3.Error:
        weg = 0
    kurz = hashlib.md5(kern.encode("utf-8")).hexdigest()[:10]
    return f"{kurz}-{IMPORT['laeufe']}-{weg}"


# ---------------------------------------------------------------- Playlist

def datum_text(display_date, confidence):
    """Beschriftung nach date_confidence.

    exakt     -> '16. Mai 2020'
    monat     -> 'Juli 1985'   (nur von Hand, wenn der Tag unbekannt ist)
    jahr      -> '1991'
    unsicher  -> '' (kein Datum anzeigen, siehe CLAUDE.md)

    Angezeigt wird nie mehr, als bekannt ist.  Der Tag im Feld dient bei
    'monat' und 'jahr' allein der Sortierung.
    """
    if not display_date or confidence == "unsicher":
        return ""
    try:
        jahr = int(display_date[0:4])
        monat = int(display_date[5:7])
        tag = int(display_date[8:10])
    except (ValueError, IndexError):
        return ""
    if confidence == "jahr" or not 1 <= monat <= 12:
        return str(jahr)
    if confidence == "monat":
        return f"{MONATE[monat - 1]} {jahr}"
    return f"{tag}. {MONATE[monat - 1]} {jahr}"


def datum_feld(display_date, confidence):
    """Wert fuer das Eingabefeld im Edit-Modus - so genau wie bekannt.

    Bei 'unsicher' bleibt das Feld leer: dieses Datum ist das Empfangs-,
    nicht das Aufnahmedatum und taugt nicht als Vorschlag.
    """
    if not display_date or confidence == "unsicher":
        return ""
    j, m, t = display_date[0:4], display_date[5:7], display_date[8:10]
    if confidence == "jahr":
        return j
    if confidence == "monat":
        return f"{m}.{j}"
    return f"{t}.{m}.{j}"


def ort_text(place, land, cc, km):
    """Ortszeile aus den Feldern, die geocode.py gefuellt hat.

    Nah am Ort steht nur der Name, weiter weg ein "bei".  Das Heimatland
    wird weggelassen - "Karlsruhe" reicht, "Karlsruhe, Deutschland" nicht.
    Ohne Koordinaten bleibt die Zeile leer (57 % der Bilder).
    """
    if not place:
        return ""
    name = place if (km is None or km <= NAH_KM) else f"bei {place}"
    land = land_name(cc, land)
    # "Portugal, Portugal" vermeiden, wenn der Ort selbst das Land ist
    if land and cc != HEIMATLAND and land != place:
        return f"{name}, {land}"
    return name


def hat_orte():
    """Ob geocode.py schon gelaufen ist.  Ohne die Spalten laeuft der
    Viewer trotzdem, dann eben ohne Ortszeile."""
    spalten = {r[1] for r in db().execute("PRAGMA table_info(photos)")}
    return {"place", "place_country", "place_cc", "place_km"} <= spalten


# Von Hand eingetragene Werte haben Vorrang vor allem aus dem Import.
# Ein Ort von Hand gilt als genau getroffen, deshalb km = 0 - sonst
# stuende spaeter "bei ..." davor.
def ausdruecke():
    """Die Ortsspalten gibt es erst, wenn geocode.py gelaufen ist."""
    if hat_orte():
        place, country = "p.place", "p.place_country"
        cc, km = "p.place_cc", "p.place_km"
    else:
        place = country = cc = km = "NULL"
    return {
        "datum": "COALESCE(p.hand_date, p.display_date)",
        "conf": "COALESCE(p.hand_date_confidence, p.date_confidence)",
        "ort": f"COALESCE(p.hand_place, {place})",
        "land": f"COALESCE(p.hand_country, {country})",
        "cc": f"COALESCE(p.hand_cc, {cc})",
        "km": f"CASE WHEN p.hand_place IS NOT NULL THEN 0 ELSE {km} END",
        "hand": "(p.hand_date IS NOT NULL OR p.hand_place IS NOT NULL)",
    }


# Gestrichene Bilder tauchen nirgends mehr auf.  Der Abgleich laeuft ueber
# den SHA-256, damit er einen erneuten Import derselben Datei ueberlebt.
NICHT_GELOESCHT = ("NOT EXISTS (SELECT 1 FROM geloescht g"
                   " WHERE g.sha256 = p.sha256)")


IN_KEINEM_ALBUM = ("NOT EXISTS (SELECT 1 FROM photo_albums a"
                   " WHERE a.photo_id = p.id)")


def album_filter(alben, ohne_album):
    """SQL-Bedingung fuer die Albenauswahl -> (Bedingung, Parameter).

    Die beiden Regler sind unabhaengig: die Albenliste sagt, welche Alben
    gezeigt werden, die Checkbox, ob die Bilder ohne jede Albumzuordnung
    dazukommen.  Nichts ausgewaehlt heisst weiterhin "alle Alben".
    """
    teile, args = [], []
    if alben:
        platz = ",".join("?" * len(alben))
        teile.append(f"EXISTS (SELECT 1 FROM photo_albums a"
                     f" WHERE a.photo_id = p.id AND a.album IN ({platz}))")
        args.extend(alben)
    elif ohne_album:
        return "", []                      # alle Alben plus albenlose Bilder
    else:
        teile.append(f"NOT {IN_KEINEM_ALBUM}")
    if alben and ohne_album:
        teile.append(IN_KEINEM_ALBUM)
    return "(" + " OR ".join(teile) + ")", args


def playlist(modus="zufall", alben=None, nur=None, ohne_album=True):
    a = ausdruecke()
    felder = (f"{a['datum']} AS d_datum, {a['conf']} AS d_conf,"
              f" {a['ort']} AS d_ort, {a['land']} AS d_land,"
              f" {a['cc']} AS d_cc, {a['km']} AS d_km,"
              f" {a['hand']} AS von_hand")
    sql = ["SELECT p.id, " + felder + " FROM photos p"]
    args = []
    sql.append("WHERE p.is_video = 0 AND p.thumb_state = 'ok'")
    sql.append("AND " + NICHT_GELOESCHT)
    bedingung, bed_args = album_filter(alben, ohne_album)
    if bedingung:
        sql.append("AND " + bedingung)
        args.extend(bed_args)

    if nur == "luecken":
        # Bilder, bei denen unten nichts oder nur die Haelfte steht
        sql.append(f"AND ({a['datum']} IS NULL OR {a['conf']} = 'unsicher'"
                   f"     OR {a['ort']} IS NULL)")

    if modus == "neueste":
        sql.append("ORDER BY d_datum DESC, p.id DESC")
    else:                                  # chrono und zufall gleich lesen
        sql.append("ORDER BY d_datum, p.id")

    rows = db().execute(" ".join(sql), args).fetchall()
    items = [[r["id"],
              datum_text(r["d_datum"], r["d_conf"]),
              ort_text(r["d_ort"], r["d_land"], r["d_cc"], r["d_km"]),
              1 if r["von_hand"] else 0]
             for r in rows]

    if modus == "zufall":
        random.shuffle(items)
    return items


# ----------------------------------------------------------------- Importlauf

# Ablage fuer neue Takeout-Archive: entweder ueber die Samba-Freigabe vom
# Windows-Rechner hineinkopiert oder ueber den Browser hochgeladen.
INBOX = BASE / "inbox"
INBOX_FERTIG = INBOX / "verarbeitet"
TAKEOUT_WURZEL = Path.home() / "takeout-import"
TAKEOUT_BAUM = TAKEOUT_WURZEL / "Takeout"
ARCHIV_ENDUNGEN = (".zip", ".tgz", ".tar.gz", ".tar")


def archive_in_inbox():
    """Noch nicht entpackte Archive, aelteste zuerst."""
    if not INBOX.is_dir():
        return []
    raus = []
    for p in sorted(INBOX.iterdir()):
        if p.is_file() and p.name.lower().endswith(ARCHIV_ENDUNGEN):
            raus.append(p)
    return raus


def _grosse(pfad):
    """(Bytes, Dateien) eines Verzeichnisbaums.  Fehlt er, kommt (0, 0)."""
    summe = anzahl = 0
    if not pfad.exists():
        return 0, 0
    for eintrag in pfad.rglob("*"):
        try:
            if eintrag.is_file():
                summe += eintrag.stat().st_size
                anzahl += 1
        except OSError:
            pass
    return summe, anzahl


def entpacken():
    """Archive aus der Inbox in den Takeout-Baum auspacken.

    Google packt alles unterhalb von "Takeout/", deshalb wird in das
    Elternverzeichnis entpackt - dann fliesst ein Nachschub-Export in den
    vorhandenen Baum, und der Import findet ihn an gewohnter Stelle.
    Erfolgreich verarbeitete Archive wandern nach inbox/verarbeitet und
    lassen sich spaeter ueber "Takeout aufraeumen" wegwerfen.
    """
    archive = archive_in_inbox()
    if not archive:
        _zeile("Keine neuen Archive in der Inbox - nichts zu entpacken.")
        return 0
    TAKEOUT_WURZEL.mkdir(parents=True, exist_ok=True)
    INBOX_FERTIG.mkdir(parents=True, exist_ok=True)
    ziel = TAKEOUT_WURZEL.resolve()

    for archiv in archive:
        _zeile(f"Entpacke {archiv.name} "
               f"({archiv.stat().st_size / 1024**3:.2f} GB) ...")
        if not archiv.name.lower().endswith(".zip"):
            _zeile(f"!! {archiv.name}: nur .zip wird entpackt, uebersprungen.")
            continue
        try:
            with zipfile.ZipFile(archiv) as z:
                n = 0
                for eintrag in z.infolist():
                    if eintrag.is_dir():
                        continue
                    # Zip-Slip: kein Eintrag darf aus dem Ziel ausbrechen
                    wohin = (ziel / eintrag.filename).resolve()
                    if not str(wohin).startswith(str(ziel) + "/"):
                        _zeile(f"!! Eintrag uebersprungen: {eintrag.filename}")
                        continue
                    wohin.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(eintrag) as quelle, wohin.open("wb") as f:
                        shutil.copyfileobj(quelle, f, 1024 * 1024)
                    n += 1
                    if n % 500 == 0:
                        _zeile(f"  {n} Dateien ...")
            _zeile(f"  {n} Dateien aus {archiv.name}")
        except (zipfile.BadZipFile, OSError) as exc:
            _zeile(f"!! {archiv.name} nicht lesbar: {exc}")
            return 1
        try:
            archiv.rename(INBOX_FERTIG / archiv.name)
        except OSError as exc:
            _zeile(f"!! {archiv.name} nicht verschiebbar: {exc}")
    return 0


# Die Schritte in der Reihenfolge, in der sie laufen muessen: erst auspacken,
# was neu hereingekommen ist, dann in die DB, dann Thumbnails, dann Orte.
# Ein Eintrag ist entweder eine Befehlszeile oder eine Funktion.
SCHRITTE = (
    ("Archive entpacken", entpacken),
    ("Fotos einlesen", [str(VENV_PY), "-u", "import_photos.py"]),
    ("Thumbnails",     [str(VENV_PY), "-u", "make_thumbs.py"]),
    ("Orte zuordnen",  [str(VENV_PY), "-u", "geocode.py"]),
)

IMPORT = {
    "aktiv": False,
    "schritt": None,
    "schritt_nr": 0,
    "begonnen": None,
    "beendet": None,
    "erfolg": None,
    "meldung": None,
    "laeufe": 0,            # zaehlt fertige Laeufe, geht in stand() ein
    "zeilen": deque(maxlen=400),
}
_import_sperre = threading.Lock()
_prozess = None


def import_status():
    with _import_sperre:
        return {
            "aktiv": IMPORT["aktiv"],
            "schritt": IMPORT["schritt"],
            "schritt_nr": IMPORT["schritt_nr"],
            "schritte_gesamt": len(SCHRITTE),
            "begonnen": IMPORT["begonnen"],
            "beendet": IMPORT["beendet"],
            "erfolg": IMPORT["erfolg"],
            "meldung": IMPORT["meldung"],
            "zeilen": list(IMPORT["zeilen"]),
        }


def _zeile(text):
    with _import_sperre:
        IMPORT["zeilen"].append(text)
    try:
        with IMPORT_LOG.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _import_lauf():
    """Laeuft im Hintergrundfaden, ruft die drei Skripte nacheinander auf."""
    global _prozess
    erfolg = True
    meldung = None
    try:
        for nr, (name, befehl) in enumerate(SCHRITTE, 1):
            with _import_sperre:
                IMPORT["schritt"] = name
                IMPORT["schritt_nr"] = nr
            _zeile(f"===== {nr}/{len(SCHRITTE)}  {name} "
                   f"({datetime.now():%H:%M:%S}) =====")

            if callable(befehl):
                code = befehl()
            else:
                p = subprocess.Popen(befehl, cwd=BASE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     bufsize=1)
                with _import_sperre:
                    _prozess = p
                for zeile in p.stdout:
                    _zeile(zeile.rstrip("\n"))
                code = p.wait()
                with _import_sperre:
                    _prozess = None

            if code != 0:
                erfolg = False
                meldung = f"{name} endete mit Code {code} - abgebrochen."
                _zeile(f"!!!!! {meldung}")
                break
        else:
            meldung = "Alle drei Schritte fertig."
            _zeile(f"===== {meldung} ({datetime.now():%H:%M:%S}) =====")
    except Exception as exc:                       # pragma: no cover
        erfolg = False
        meldung = f"{type(exc).__name__}: {exc}"
        _zeile(f"!!!!! {meldung}")
    finally:
        with _import_sperre:
            _prozess = None
            IMPORT["aktiv"] = False
            IMPORT["schritt"] = None
            IMPORT["beendet"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            IMPORT["erfolg"] = erfolg
            IMPORT["meldung"] = meldung
            IMPORT["laeufe"] += 1          # laesst die Slideshow nachladen


def import_starten():
    with _import_sperre:
        if IMPORT["aktiv"]:
            return False
        IMPORT.update(aktiv=True, schritt=SCHRITTE[0][0], schritt_nr=0,
                      begonnen=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                      beendet=None, erfolg=None, meldung=None)
        IMPORT["zeilen"].clear()
    threading.Thread(target=_import_lauf, daemon=True).start()
    return True


# ---------------------------------------------------------------- Routen

@app.route("/")
def start():
    return render_template("slideshow.html")


@app.route("/einstellungen")
def seite_einstellungen():
    return render_template("einstellungen.html")


@app.route("/api/stand")
def api_stand():
    """Klein und billig - die Slideshow fragt das im Minutentakt ab."""
    return jsonify({"stand": stand()})


@app.route("/api/einstellungen", methods=["GET", "POST"])
def api_einstellungen():
    if request.method == "POST":
        daten = request.get_json(silent=True)
        if not isinstance(daten, dict):
            return jsonify({"fehler": "Kein gültiges JSON"}), 400
        gespeichert = einstellungen_schreiben(daten)
        return jsonify({"einstellungen": gespeichert, "stand": stand()})
    return jsonify({"einstellungen": einstellungen_lesen(), "stand": stand()})


@app.route("/api/playlist")
def api_playlist():
    e = einstellungen_lesen()
    modus = request.args.get("modus", e["modus"])
    if modus not in MODI:
        modus = e["modus"]

    if "album" in request.args:                    # einzelnes Album probeweise
        alben = [a for a in request.args.getlist("album") if a]
    else:
        alben = e["alben"]

    try:
        dauer = int(request.args.get("dauer", e["dauer"]))
    except ValueError:
        dauer = e["dauer"]
    dauer = max(DAUER_MIN, min(DAUER_MAX, dauer))

    nur = request.args.get("nur") if request.args.get("nur") == "luecken" \
        else None

    items = playlist(modus, alben, nur, e["ohne_album"])
    return jsonify({"modus": modus, "alben": alben, "dauer": dauer,
                    "nur": nur, "anzahl": len(items), "stand": stand(),
                    "info_skala": e["info_skala"],
                    "leiste_skala": e["leiste_skala"],
                    "bilder": items})


@app.route("/bild/<int:photo_id>")
def bild(photo_id):
    row = db().execute("SELECT sha256 FROM photos WHERE id = ?",
                       (photo_id,)).fetchone()
    if not row:
        abort(404)
    pfad = thumb_path(row["sha256"])
    if not pfad.exists():
        abort(404)
    # id -> sha ist stabil, Thumbnails aendern sich nicht
    return send_file(pfad, mimetype="image/jpeg", max_age=30 * 24 * 3600)


@app.route("/vorschau/<int:photo_id>")
def vorschau(photo_id):
    """320-px-Variante aus cache/grid - fuer Listen im Webinterface."""
    row = db().execute("SELECT sha256 FROM photos WHERE id = ?",
                       (photo_id,)).fetchone()
    if not row:
        abort(404)
    pfad = thumb_path(row["sha256"], "grid")
    if not pfad.exists():
        abort(404)
    return send_file(pfad, mimetype="image/jpeg", max_age=30 * 24 * 3600)


@app.route("/api/alben")
def api_alben():
    rows = db().execute(
        "SELECT a.album, COUNT(*) n FROM photo_albums a "
        "JOIN photos p ON p.id = a.photo_id "
        "WHERE p.is_video = 0 AND p.thumb_state = 'ok' "
        "GROUP BY a.album ORDER BY a.album").fetchall()
    return jsonify([{"album": r["album"], "anzahl": r["n"]} for r in rows])


@app.route("/api/zahlen")
def api_zahlen():
    a = ausdruecke()
    lebt = f"({NICHT_GELOESCHT})"
    z = db().execute(
        f"SELECT COUNT(*) gesamt,"
        f" SUM(is_video = 0 AND thumb_state = 'ok' AND {lebt}) zeigbar,"
        f" SUM(is_video = 1) videos,"
        f" SUM(thumb_state = 'fehler') fehler,"
        f" SUM({a['ort']} IS NOT NULL AND {lebt}) mit_ort,"
        f" SUM({a['hand']} AND {lebt}) von_hand,"
        f" SUM(is_video = 0 AND thumb_state = 'ok' AND {lebt} AND ("
        f"   {a['datum']} IS NULL OR {a['conf']} = 'unsicher'"
        f"   OR {a['ort']} IS NULL)) luecken,"
        f" (SELECT COUNT(*) FROM geloescht WHERE endgueltig_at IS NULL)"
        f"   geloescht"
        f" FROM photos p").fetchone()
    return jsonify(dict(z))


# --------------------------------------------------- Korrekturen von Hand

# "1985" -> nur Jahr,  "07.1985" -> Monat,  "12.07.1985" -> voller Tag.
# Punkte, Schraegstriche und Bindestriche sind alle erlaubt.
_DATUM_MUSTER = (
    (re.compile(r"^(\d{4})$"), "jahr"),
    (re.compile(r"^(\d{1,2})[./-](\d{4})$"), "monat"),
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$"), "tag"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "iso"),
)


def datum_deuten(text):
    """Eingabe -> (display_date, confidence) oder None bei Unsinn.

    Bei Jahr und Monat wird bewusst die Monatsmitte bzw. der 1. Juli
    eingesetzt, damit die Sortierung stimmt; angezeigt wird davon nur das,
    was date_confidence zulaesst.
    """
    text = (text or "").strip()
    if not text:
        return None
    for muster, art in _DATUM_MUSTER:
        m = muster.match(text)
        if not m:
            continue
        try:
            if art == "jahr":
                jahr, monat, tag, conf = int(m[1]), 7, 1, "jahr"
            elif art == "monat":
                jahr, monat, tag, conf = int(m[2]), int(m[1]), 15, "monat"
            elif art == "tag":
                jahr, monat, tag, conf = int(m[3]), int(m[2]), int(m[1]), "exakt"
            else:
                jahr, monat, tag, conf = int(m[1]), int(m[2]), int(m[3]), "exakt"
            datetime(jahr, monat, tag)          # prueft 31.02. und Aehnliches
        except ValueError:
            return None
        if not 1800 <= jahr <= 2100:
            return None
        return f"{jahr:04d}-{monat:02d}-{tag:02d} 12:00:00", conf
    return None


_orte_liste = None
_orte_namen = None          # kleingeschrieben -> [Eintraege]
_laender_namen = None       # kleingeschrieben -> (Name, cc)
_regionen_namen = None      # kleingeschrieben -> (Name, cc, Land)
_land_gewicht = None        # cc -> Anzahl eigener Fotos
_orte_sperre = threading.Lock()


def land_gewicht():
    """Wie viele Fotos es je Land schon gibt.

    Dient als Stichentscheid bei gleichnamigen Orten: Interlaken gibt es
    in der Schweiz und in Kalifornien, und das kalifornische ist sogar
    groesser - aber die Bilder hier stammen nun einmal aus den Alpen.
    """
    global _land_gewicht
    if _land_gewicht is None:
        try:
            _land_gewicht = {
                r[0]: r[1] for r in db().execute(
                    "SELECT place_cc, COUNT(*) FROM photos"
                    " WHERE place_cc IS NOT NULL GROUP BY place_cc")}
        except sqlite3.Error:
            _land_gewicht = {}
    return _land_gewicht


def orte_laden():
    """orte.tsv einmalig in den Speicher, fuer Ortssuche und Albumvorschlag."""
    global _orte_liste, _orte_namen, _laender_namen, _regionen_namen
    with _orte_sperre:
        if _orte_liste is not None:
            return _orte_liste
        liste, namen, laender, regionen = [], {}, {}, {}
        # orte-ergaenzung.tsv traegt nach, was cities1000 fehlt (kleinere
        # deutsche Gemeinden) - siehe geocode.py --gemeinden.
        for pfad in (BASE / "geo" / "orte.tsv",
                     BASE / "geo" / "orte-ergaenzung.tsv"):
            if not pfad.exists():
                if pfad.name == "orte.tsv":
                    app.logger.warning("geo/orte.tsv fehlt - Ortssuche leer")
                continue
            try:
                with pfad.open(encoding="utf-8") as f:
                    for zeile in f:
                        if zeile.startswith("#"):
                            continue
                        t = zeile.rstrip("\n").split("\t")
                        if len(t) < 6:
                            continue
                        name, cc, land, pop = t[0], t[3], t[4], int(t[5])
                        region = t[6] if len(t) > 6 else ""
                        eintrag = (name, cc, land, pop, region)
                        liste.append(eintrag)
                        namen.setdefault(name.lower(), []).append(eintrag)
                        if region:
                            regionen.setdefault(
                                region.lower(),
                                (region, cc, land_name(cc, land)))
                        if land:
                            # amtlicher und gebraeuchlicher Name sollen beide
                            # als Albumname erkannt werden
                            for schreibweise in {land, land_name(cc, land)}:
                                lk = schreibweise.lower()
                                if lk not in laender:
                                    laender[lk] = (land_name(cc, land), cc)
            except OSError:
                app.logger.warning("%s nicht lesbar", pfad.name)
        _orte_liste, _orte_namen = liste, namen
        _laender_namen, _regionen_namen = laender, regionen
        return _orte_liste


@app.route("/api/ortsuche")
def api_ortsuche():
    """Vorschlaege fuer das Ortsfeld.  Treffer am Wortanfang zuerst,
    darunter die groesseren Orte.

    Gleichnamige Orte im selben Land werden ueber die Region auseinander-
    gehalten - "Oregon" gibt es in Missouri, Illinois, Ohio und Wisconsin.
    Bleiben sie auch damit ununterscheidbar, wird nur der groesste gezeigt.
    """
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    orte_laden()

    # Laender und Regionen gehoeren mit in die Liste, sonst findet man
    # den Bundesstaat Oregon nie - nur die vier Doerfer gleichen Namens.
    treffer = []
    for lk, (land, cc) in _laender_namen.items():
        if lk.startswith(q):
            treffer.append((0, 0, land, cc, land, "", "Land"))
        elif q in lk:
            treffer.append((2, 0, land, cc, land, "", "Land"))
    for rk, (region, cc, land) in _regionen_namen.items():
        rang = 0 if rk.startswith(q) else (2 if q in rk else None)
        if rang is None:
            continue
        # ausdruecklich vorgemerkte Namen ganz nach oben
        if rk in REGION_VOR_ORT and rk.startswith(q):
            rang = -1
        treffer.append((rang, 0, region, cc, land, "", "Region"))
    # Bei Orten zaehlt, wo tatsaechlich fotografiert wurde - sonst stuende
    # Interlaken/Kalifornien (7321 Einwohner) vor Interlaken/Schweiz (5067).
    gewicht = land_gewicht()
    for name, cc, land, pop, region in _orte_liste:
        k = name.lower()
        rang = 1 if k.startswith(q) else (3 if q in k else None)
        if rang is None:
            continue
        vorgabe = 1 if ORTS_VORGABEN.get(k) == cc else 0
        treffer.append((rang, -vorgabe, -gewicht.get(cc, 0), -pop,
                        name, cc, land, region, "Ort"))
    # Land und Region haben nur fuenf Felder - auf dieselbe Form bringen
    treffer = [t if len(t) == 9 else (t[0], 0, 0, t[1], *t[2:]) for t in treffer]
    treffer.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))

    raus, gesehen = [], set()
    for *_, name, cc, land, region, art in treffer:
        schluessel = (name.lower(), cc, region, art)
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        raus.append({"ort": name, "cc": cc, "land": land_name(cc, land),
                     "region": region, "art": art})
        if len(raus) >= 12:
            break
    return jsonify(raus)


# Fuehrende Jahreszahlen und Trennzeichen aus Albumnamen schneiden:
# "1991 Hawaii" -> "Hawaii",  "2012 April München" -> "April München"
_JAHR_VORNE = re.compile(r"^\s*(?:19|20)\d{2}\s*[-–—/,.]?\s*")


def album_als_ort(album):
    """Albumnamen als Ortsangabe deuten.  None, wenn nichts passt.

    Erst die ganze Bezeichnung, dann die Teile vor den Kommas, zuletzt
    einzelne Woerter.  Je Kandidat gilt: Land vor Region vor Stadt -
    sonst wuerde "England" im Kaff England, Arkansas landen statt in
    Grossbritannien, und "Hawaii" gar nicht gefunden.
    """
    if not album:
        return None
    orte_laden()
    rest = _JAHR_VORNE.sub("", album).strip()
    if not rest:
        return None

    kandidaten = [rest]
    kandidaten += [t.strip() for t in rest.split(",") if t.strip()]
    kandidaten += [w for w in rest.replace(",", " ").split() if len(w) > 2]

    gewicht = land_gewicht()
    for kand in kandidaten:
        k = kand.lower()
        if k in _laender_namen:
            land, cc = _laender_namen[k]
            return {"ort": land, "land": land, "cc": cc, "quelle": "Land"}
        if k in _regionen_namen:
            region, cc, land = _regionen_namen[k]
            return {"ort": region, "land": land, "cc": cc, "quelle": "Region"}
        if k in _orte_namen:
            # unter gleichnamigen Orten den aus einem Land waehlen, in dem
            # ueberhaupt schon fotografiert wurde; erst dann nach Groesse.
            # Eine Vorgabe von Hand sticht beides.
            vorgabe = ORTS_VORGABEN.get(k)
            name, cc, land, pop, region = max(
                _orte_namen[k],
                key=lambda e: (e[1] == vorgabe, gewicht.get(e[1], 0), e[3]))
            return {"ort": name, "land": land_name(cc, land), "cc": cc,
                    "quelle": "Ort", "region": region}
    return None


@app.route("/api/album-als-ort")
def api_album_als_ort():
    """Einen Albumnamen als Ort deuten.  Trifft nichts, kommt der Name
    ohne Jahreszahl zurueck - besser als ein leeres Feld."""
    album = (request.args.get("album") or "").strip()
    treffer = album_als_ort(album)
    if treffer:
        return jsonify(treffer)
    rest = _JAHR_VORNE.sub("", album).strip()
    if not rest:
        return jsonify({})
    return jsonify({"ort": rest, "land": "", "cc": "", "quelle": "Albumname"})


@app.route("/api/foto/<int:photo_id>")
def api_foto(photo_id):
    """Alles, was der Edit-Modus zum Anzeigen und Vorbelegen braucht."""
    r = db().execute(
        "SELECT id, filename, width, height, camera_model, taken_at,"
        " display_date, date_confidence, lat, lon,"
        " place, place_country, place_cc, place_km,"
        " hand_date, hand_date_confidence, hand_place, hand_country,"
        " hand_cc, hand_at, sha256,"
        " EXISTS (SELECT 1 FROM geloescht g WHERE g.sha256 = photos.sha256)"
        "   AS geloescht"
        " FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not r:
        abort(404)
    d = dict(r)
    d["alben"] = [x[0] for x in db().execute(
        "SELECT album FROM photo_albums WHERE photo_id = ? ORDER BY album",
        (photo_id,))]

    # Vorbelegung der Felder: was gilt gerade, egal woher es stammt
    a = ausdruecke()
    e = db().execute(
        f"SELECT {a['datum']} AS d_datum, {a['conf']} AS d_conf,"
        f" {a['ort']} AS d_ort, {a['land']} AS d_land, {a['cc']} AS d_cc"
        f" FROM photos p WHERE p.id = ?", (photo_id,)).fetchone()
    d["feld_datum"] = datum_feld(e["d_datum"], e["d_conf"])
    d["feld_ort"] = e["d_ort"] or ""
    d["feld_land"] = e["d_land"] or ""
    d["feld_cc"] = e["d_cc"] or ""
    d["hatte_ort"] = e["d_ort"] is not None

    # Steht noch kein Ort da, das erste Album als Vorschlag deuten.
    # Besser ein Vorschlag aus "1991 Hawaii" als ein leeres Feld.
    d["vorschlag_ort"] = None
    if not d["feld_ort"] and d["alben"]:
        d["vorschlag_ort"] = album_als_ort(d["alben"][0])
    return jsonify(d)


@app.route("/api/foto/<int:photo_id>", methods=["POST"])
def api_foto_speichern(photo_id):
    """Datum und Ort von Hand eintragen.

    Leeres Feld loescht die jeweilige Korrektur und laesst wieder den Wert
    aus dem Import gelten.  Die Originalspalten werden nie angefasst.
    """
    daten = request.get_json(silent=True)
    if not isinstance(daten, dict):
        return jsonify({"fehler": "Kein gültiges JSON"}), 400

    felder = {}
    if "datum" in daten:
        roh = (daten.get("datum") or "").strip()
        if not roh:
            felder["hand_date"] = None
            felder["hand_date_confidence"] = None
        else:
            gedeutet = datum_deuten(roh)
            if not gedeutet:
                return jsonify({"fehler":
                                f"Datum '{roh}' nicht verstanden. "
                                "Erlaubt: 1985, 07.1985, 12.07.1985"}), 400
            felder["hand_date"], felder["hand_date_confidence"] = gedeutet

    if "ort" in daten:
        ort = (daten.get("ort") or "").strip()
        if not ort:
            felder["hand_place"] = None
            felder["hand_country"] = None
            felder["hand_cc"] = None
        else:
            felder["hand_place"] = ort
            felder["hand_country"] = (daten.get("land") or "").strip() or None
            felder["hand_cc"] = (daten.get("cc") or "").strip().upper() or None

    if not felder:
        return jsonify({"fehler": "Nichts zu speichern"}), 400
    felder["hand_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    satz = ", ".join(f"{k} = ?" for k in felder)
    conn = db_schreiben()
    try:
        cur = conn.execute(f"UPDATE photos SET {satz} WHERE id = ?",
                           [*felder.values(), photo_id])
        if cur.rowcount == 0:
            return jsonify({"fehler": "Foto nicht gefunden"}), 404
        conn.commit()
    finally:
        conn.close()

    a = ausdruecke()
    r = db().execute(
        f"SELECT {a['datum']} AS d_datum, {a['conf']} AS d_conf,"
        f" {a['ort']} AS d_ort, {a['land']} AS d_land, {a['cc']} AS d_cc,"
        f" {a['km']} AS d_km FROM photos p WHERE p.id = ?",
        (photo_id,)).fetchone()
    return jsonify({
        "id": photo_id,
        "datum": datum_text(r["d_datum"], r["d_conf"]),
        "ort": ort_text(r["d_ort"], r["d_land"], r["d_cc"], r["d_km"]),
    })


@app.route("/api/foto/<int:photo_id>/umgebung")
def api_umgebung(photo_id):
    """Die zeitlich benachbarten Bilder, aelter wie neuer.

    Beim Nachtragen eines Ortes hilft fast immer der Zusammenhang: was
    davor und danach aufgenommen wurde, verraet meist, wo man war.
    Sortiert wird nach display_date, nicht nach der Playlist - die ist
    im Zufallsmodus wertlos fuer diese Frage.
    """
    try:
        n = min(int(request.args.get("n", 4)), 12)
    except ValueError:
        n = 4
    a = ausdruecke()
    hier = db().execute(
        f"SELECT {a['datum']} AS d FROM photos p WHERE p.id = ?",
        (photo_id,)).fetchone()
    if not hier:
        abort(404)

    rumpf = (f"SELECT p.id, {a['datum']} AS d_datum, {a['conf']} AS d_conf,"
             f" {a['ort']} AS d_ort, {a['land']} AS d_land,"
             f" {a['cc']} AS d_cc, {a['km']} AS d_km"
             f" FROM photos p"
             f" WHERE p.is_video = 0 AND p.thumb_state = 'ok'"
             f"   AND {NICHT_GELOESCHT}")

    def hole(richtung):
        if richtung > 0:
            wo = f" AND ({a['datum']} > ? OR ({a['datum']} = ? AND p.id > ?))"
            ordn = " ORDER BY d_datum, p.id LIMIT ?"
        else:
            wo = f" AND ({a['datum']} < ? OR ({a['datum']} = ? AND p.id < ?))"
            ordn = " ORDER BY d_datum DESC, p.id DESC LIMIT ?"
        rows = db().execute(rumpf + wo + ordn,
                            (hier["d"], hier["d"], photo_id, n)).fetchall()
        return [{"id": r["id"],
                 "datum": datum_text(r["d_datum"], r["d_conf"]),
                 "ort": ort_text(r["d_ort"], r["d_land"], r["d_cc"],
                                 r["d_km"])} for r in rows]

    vorher = hole(-1)
    vorher.reverse()                    # aelteste zuerst, wie im Filmstreifen
    return jsonify({"vorher": vorher, "nachher": hole(+1)})


@app.route("/api/foto/<int:photo_id>/album-ohne-ort")
def api_album_ohne_ort(photo_id):
    """Bilder aus denselben Alben, bei denen noch kein Ort steht.

    Grundlage fuer die Rueckfrage nach dem Speichern: wer ein Album
    durchsieht, hat den Ort meist fuer den ganzen Stapel richtig.
    """
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except ValueError:
        limit = 50
    a = ausdruecke()
    rows = db().execute(
        f"SELECT DISTINCT p.id, p.filename FROM photos p"
        f" JOIN photo_albums a ON a.photo_id = p.id"
        f" WHERE a.album IN (SELECT album FROM photo_albums WHERE photo_id = ?)"
        f"   AND p.id <> ?"
        f"   AND p.is_video = 0 AND p.thumb_state = 'ok'"
        f"   AND {a['ort']} IS NULL"
        f"   AND {NICHT_GELOESCHT}"
        f" ORDER BY p.display_date, p.id LIMIT ?",
        (photo_id, photo_id, limit)).fetchall()
    return jsonify([{"id": r["id"], "filename": r["filename"]} for r in rows])


@app.route("/api/orte-uebernehmen", methods=["POST"])
def api_orte_uebernehmen():
    """Denselben Ort auf mehrere Bilder schreiben."""
    daten = request.get_json(silent=True) or {}
    ids = daten.get("ids")
    ort = (daten.get("ort") or "").strip()
    if not isinstance(ids, list) or not ids or not ort:
        return jsonify({"fehler": "ids und ort nötig"}), 400
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify({"fehler": "ids müssen Zahlen sein"}), 400

    land = (daten.get("land") or "").strip() or None
    cc = (daten.get("cc") or "").strip().upper() or None
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = db_schreiben()
    try:
        platz = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE photos SET hand_place = ?, hand_country = ?,"
            f" hand_cc = ?, hand_at = ? WHERE id IN ({platz})",
            [ort, land, cc, jetzt, *ids])
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    return jsonify({"ok": True, "anzahl": n, "stand": stand()})


@app.route("/api/foto/<int:photo_id>/loeschen", methods=["POST"])
def api_foto_loeschen(photo_id):
    """Bild aus der Anzeige nehmen und auf die Streichliste setzen.

    Geloescht wird nur in der Datenbank - Originaldatei und Thumbnails
    bleiben liegen, damit sich das jederzeit zuruecknehmen laesst.
    """
    r = db().execute("SELECT sha256, filename FROM photos WHERE id = ?",
                     (photo_id,)).fetchone()
    if not r:
        abort(404)
    conn = db_schreiben()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO geloescht"
            " (sha256, photo_id, filename, geloescht_at) VALUES (?,?,?,?)",
            (r["sha256"], photo_id, r["filename"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "id": photo_id, "stand": stand()})


@app.route("/api/foto/<int:photo_id>/zurueckholen", methods=["POST"])
def api_foto_zurueckholen(photo_id):
    r = db().execute("SELECT sha256 FROM photos WHERE id = ?",
                     (photo_id,)).fetchone()
    if not r:
        abort(404)
    conn = db_schreiben()
    try:
        conn.execute("DELETE FROM geloescht WHERE sha256 = ?", (r["sha256"],))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "id": photo_id, "stand": stand()})


@app.route("/api/geloescht")
def api_geloescht():
    """Inhalt des Papierkorbs.  Endgueltig entfernte Eintraege bleiben in
    der Tabelle - sie sperren weiter -, tauchen hier aber nicht mehr auf."""
    rows = db().execute(
        "SELECT g.sha256, g.photo_id, g.filename, g.geloescht_at,"
        " p.id IS NOT NULL AS im_bestand"
        " FROM geloescht g LEFT JOIN photos p ON p.sha256 = g.sha256"
        " WHERE g.endgueltig_at IS NULL"
        " ORDER BY g.geloescht_at DESC").fetchall()
    liste = [dict(r) for r in rows]
    platz = 0
    for e in liste:
        for groesse in ("display", "grid"):
            pf = thumb_path(e["sha256"], groesse)
            if pf.exists():
                platz += pf.stat().st_size
    return jsonify({"bilder": liste, "bytes": platz})


@app.route("/api/papierkorb/endgueltig", methods=["POST"])
def api_papierkorb_endgueltig():
    """Aus dem Papierkorb entfernen: Thumbnails weg, Sperre bleibt.

    Die Originaldatei wird nicht angefasst - siehe CLAUDE.md.  Und der
    Eintrag in 'geloescht' bleibt stehen, sonst brachte der naechste
    Takeout das Bild zurueck.
    """
    daten = request.get_json(silent=True) or {}
    shas = daten.get("sha256")
    if not isinstance(shas, list) or not shas:
        return jsonify({"fehler": "sha256-Liste nötig"}), 400
    shas = [str(s) for s in shas]

    entfernt, frei = 0, 0
    for sha in shas:
        for groesse in ("display", "grid"):
            pf = thumb_path(sha, groesse)
            try:
                if pf.exists():
                    frei += pf.stat().st_size
                    pf.unlink()
            except OSError as exc:
                app.logger.warning("Thumbnail %s nicht löschbar: %s", pf, exc)
        entfernt += 1

    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db_schreiben()
    try:
        platz = ",".join("?" * len(shas))
        conn.execute(
            f"UPDATE geloescht SET endgueltig_at = ?"
            f" WHERE sha256 IN ({platz})", [jetzt, *shas])
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "anzahl": entfernt, "bytes": frei})


@app.route("/api/papierkorb/zurueckholen", methods=["POST"])
def api_papierkorb_zurueckholen():
    daten = request.get_json(silent=True) or {}
    shas = daten.get("sha256")
    if not isinstance(shas, list) or not shas:
        return jsonify({"fehler": "sha256-Liste nötig"}), 400
    shas = [str(s) for s in shas]
    conn = db_schreiben()
    try:
        platz = ",".join("?" * len(shas))
        cur = conn.execute(f"DELETE FROM geloescht WHERE sha256 IN ({platz})",
                           shas)
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    return jsonify({"ok": True, "anzahl": n, "stand": stand()})


@app.route("/papierkorb")
def seite_papierkorb():
    return render_template("papierkorb.html")


@app.route("/api/import/start", methods=["POST"])
def api_import_start():
    if not import_starten():
        return jsonify({"fehler": "Es läuft bereits ein Import."}), 409
    return jsonify(import_status())


@app.route("/api/import/status")
def api_import_status():
    return jsonify(import_status())


@app.route("/api/import/abbrechen", methods=["POST"])
def api_import_abbrechen():
    with _import_sperre:
        p = _prozess
        if not IMPORT["aktiv"] or p is None:
            return jsonify({"fehler": "Es läuft kein Import."}), 409
    p.terminate()                      # der Faden raeumt den Rest auf
    return jsonify({"ok": True})


# ------------------------------------------------------------ Google Cast

# Der Chromecast holt sich das Bild selbst per HTTP.  Er braucht also eine
# Adresse, unter der er den Server erreicht - "localhost" nuetzt ihm nichts.
def eigene_adresse(zu="8.8.8.8"):
    """Die IP, unter der dieser Rechner im Heimnetz sichtbar ist.

    Ueber ein UDP-Socket ermittelt, ohne ein einziges Paket zu senden:
    connect() legt bei UDP nur die Route fest.  Zuverlaessiger als
    gethostbyname, das auf 127.0.1.1 hereinfaellt.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((zu, 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


CAST = {
    "an": False,
    "geraet": "",
    "verbunden": False,
    "meldung": "",
    "bild_nr": 0,
    "anzahl": 0,
    "pausiert": False,
    "bild": None,           # [id, datum, ort] - was gerade am Fernseher haengt
    "sofort": False,        # HomeAssistant hat gerufen: nicht laenger warten
    "gefunden": [],
}
_cast_sperre = threading.Lock()
_cast_faden = None
_cast_wecker = threading.Event()      # bricht das Warten sofort ab
_cast_befehle = deque()               # weiter, zurueck, pause, springen


def cast_befehl(was, ziel_id=None):
    """Fernbedienung vom Tablet.  Wirkt beim naechsten Schlag der Schleife,
    und die wird sofort geweckt."""
    with _cast_sperre:
        _cast_befehle.append((was, ziel_id))
    _cast_wecker.set()


# Der Dongle haengt nur waehrend des Fruehstuecks am Strom - HomeAssistant
# schaltet ihn morgens ein und danach wieder aus.  Den Rest des Tages ist er
# schlicht weg.  Deshalb steigende Wartezeiten statt stur alle 30 Sekunden zu
# suchen: bei Tagesanbruch ist er nach spaetestens fuenf Minuten gefunden,
# und dazwischen liegt der Rechner still.
# Ruft HomeAssistant beim Einschalten /api/cast/wecken auf, ist die lange
# Stufe nur noch ein Notnagel fuer den Fall, dass der Anruf ausbleibt.
WARTE_STUFEN = (15, 30, 60, 120, 300, 600)


def _warte(stufe):
    """Bis zum naechsten Versuch schlafen.  Bricht ab, sobald jemand
    ein- oder ausschaltet."""
    _cast_wecker.wait(WARTE_STUFEN[min(stufe, len(WARTE_STUFEN) - 1)])
    _cast_wecker.clear()


def _cast_melden(text, verbunden=None):
    with _cast_sperre:
        neu = text != CAST["meldung"]
        CAST["meldung"] = text
        if verbunden is not None:
            CAST["verbunden"] = verbunden
    # nur Zustandswechsel ins Journal, sonst laeuft es mit jedem Versuch voll
    if neu:
        app.logger.info("Cast: %s", text)


def _suche_beenden(browser):
    """Die mDNS-Suche zumachen.  Bleibt sie offen, sammeln sich Sockets an -
    dieselbe Falle wie bei den Datenbankverbindungen.

    pychromecast.discovery.stop_discovery() ist seit Version 14 veraltet;
    der Weg ueber das Objekt funktioniert in beiden Fassungen.
    """
    if browser is None:
        return
    try:
        if hasattr(browser, "stop_discovery"):
            browser.stop_discovery()
        else:
            pychromecast.discovery.stop_discovery(browser)
    except Exception as exc:
        app.logger.warning("Cast-Suche nicht sauber beendet: %s", exc)


def cast_suchen(timeout=6):
    """Alle Cast-Geraete im Netz auflisten."""
    if pychromecast is None:
        return []
    gefunden, browser = pychromecast.discovery.discover_chromecasts(
        timeout=timeout)
    try:
        namen = sorted({g.friendly_name for g in gefunden if g.friendly_name})
    finally:
        _suche_beenden(browser)
    with _cast_sperre:
        CAST["gefunden"] = namen
    return namen


def _cast_verbinden(name):
    """Chromecast mit diesem Namen suchen und bereitmachen -> (cast, browser)."""
    geraete, browser = pychromecast.get_listed_chromecasts(
        friendly_names=[name], discovery_timeout=8)
    if not geraete:
        _suche_beenden(browser)
        return None, None
    cast = geraete[0]
    cast.wait(timeout=15)
    return cast, browser


def _cast_lauf():
    """Schickt der Reihe nach Bilder an den Fernseher.

    Laeuft unabhaengig von jedem Browser - der Rechner darf kopflos sein.
    Grundlage sind dieselben Einstellungen wie fuer die Slideshow: Dauer,
    Reihenfolge, Albenauswahl.  Aendert sich der Stand, wird die Liste neu
    geholt; ein Bild spaeter merkt man das auf dem Fernseher.
    """
    cast = browser = None
    bilder, marke, n = [], None, 0
    zeigen = True
    fehlversuche = 0
    basis = f"http://{eigene_adresse()}:{PORT}"

    while CAST["an"]:
        try:
            # Ruft HomeAssistant an, faengt die Wartezeit wieder bei 15 s an
            with _cast_sperre:
                if CAST["sofort"]:
                    CAST["sofort"] = False
                    fehlversuche = 0
            if cast is None:
                name = CAST["geraet"]
                cast, browser = _cast_verbinden(name)
                if cast is None:
                    _cast_melden(f"{name} ist gerade nicht erreichbar — "
                                 f"wird weiter gesucht", verbunden=False)
                    _warte(fehlversuche)
                    fehlversuche += 1
                    continue
                fehlversuche = 0
                _cast_melden(f"Verbunden mit {cast.cast_info.friendly_name}",
                             verbunden=True)

            jetzt = stand()
            if jetzt != marke or not bilder:
                e = einstellungen_lesen()
                bilder = playlist(e["modus"], e["alben"], None,
                                  e["ohne_album"])
                marke, n = jetzt, 0
                with _cast_sperre:
                    CAST["anzahl"] = len(bilder)
            if not bilder:
                _cast_melden("Keine Bilder in dieser Auswahl.")
                _warte(2)
                continue

            n %= len(bilder)
            if zeigen:
                bild_id, datum, ort, _ = bilder[n]
                cast.media_controller.play_media(
                    f"{basis}/cast/bild/{bild_id}", "image/jpeg",
                    title=" · ".join(x for x in (datum, ort) if x) or None)
                with _cast_sperre:
                    CAST["bild_nr"] = n + 1
                    CAST["bild"] = [bild_id, datum, ort]

            # Pausiert wird ohne Frist gewartet - geweckt wird ohnehin,
            # sobald ein Befehl kommt oder jemand ausschaltet.
            frist = None if CAST["pausiert"] else \
                max(DAUER_MIN, einstellungen_lesen()["dauer"])
            geweckt = _cast_wecker.wait(frist)
            _cast_wecker.clear()

            schritt, sprung = 0, None
            with _cast_sperre:
                while _cast_befehle:
                    was, ziel = _cast_befehle.popleft()
                    if was == "weiter":
                        schritt += 1
                    elif was == "zurueck":
                        schritt -= 1
                    elif was == "pause":
                        CAST["pausiert"] = not CAST["pausiert"]
                    elif was == "springen":
                        sprung = ziel
            if sprung is not None:
                wo = next((i for i, b in enumerate(bilder) if b[0] == sprung),
                          None)
                zeigen = wo is not None
                if zeigen:
                    n = wo
            elif schritt:
                n += schritt
                zeigen = True
            else:
                # regulaerer Ablauf der Anzeigedauer; im Pausenzustand
                # bleibt das Bild stehen
                zeigen = not geweckt and not CAST["pausiert"]
                if zeigen:
                    n += 1

        except Exception as exc:                       # pragma: no cover
            # Nach dem Fruehstueck nimmt HomeAssistant dem Dongle den Strom -
            # aus Sicht des Servers bricht die Verbindung einfach ab.  Das
            # ist der Normalfall, kein Fehler: wegraeumen und in Ruhe auf
            # den naechsten Morgen warten.
            _cast_melden(f"Verbindung verloren ({type(exc).__name__}) — "
                         f"wird weiter gesucht", verbunden=False)
            if browser is not None:
                try:
                    _suche_beenden(browser)
                except Exception:
                    pass
            cast = browser = None
            _warte(fehlversuche)
            fehlversuche += 1

    # sauber abmelden, sonst bleibt das Bild stehen
    if cast is not None:
        try:
            cast.quit_app()
        except Exception:
            pass
    if browser is not None:
        try:
            _suche_beenden(browser)
        except Exception:
            pass
    _cast_melden("Aus.", verbunden=False)


def cast_schalten(an, geraet=None):
    """Senden ein- oder ausschalten.  Der Zustand landet in settings.json."""
    global _cast_faden
    with _cast_sperre:
        if geraet is not None:
            CAST["geraet"] = geraet
        CAST["an"] = bool(an and CAST["geraet"] and pychromecast)
        laeuft = _cast_faden is not None and _cast_faden.is_alive()
    _cast_wecker.set()                     # laufendes Warten abbrechen

    if CAST["an"] and not laeuft:
        _cast_faden = threading.Thread(target=_cast_lauf, daemon=True)
        _cast_faden.start()
    elif not CAST["an"]:
        CAST["verbunden"] = False
    return CAST["an"]


@app.route("/cast/bild/<int:photo_id>")
def cast_bild(photo_id):
    """Dasselbe Bild wie /bild/<id>, aber mit Datum und Ort im JPEG.

    Der Fernseher zeigt nur ein fertiges Bild - die Beschriftung, die im
    Browser die Seite zeichnet, muss hier hinein.
    """
    if castbild is None:
        abort(503)
    a = ausdruecke()
    r = db().execute(
        f"SELECT p.sha256, {a['datum']} AS d_datum, {a['conf']} AS d_conf,"
        f" {a['ort']} AS d_ort, {a['land']} AS d_land, {a['cc']} AS d_cc,"
        f" {a['km']} AS d_km FROM photos p WHERE p.id = ?",
        (photo_id,)).fetchone()
    if not r:
        abort(404)
    pfad = thumb_path(r["sha256"])
    if not pfad.exists():
        abort(404)
    roh = castbild.als_jpeg(
        pfad,
        datum_text(r["d_datum"], r["d_conf"]),
        ort_text(r["d_ort"], r["d_land"], r["d_cc"], r["d_km"]),
        einstellungen_lesen()["cast_skala"])
    antwort = app.response_class(roh, mimetype="image/jpeg")
    antwort.headers["Cache-Control"] = "public, max-age=86400"
    return antwort


@app.route("/api/cast")
def api_cast():
    with _cast_sperre:
        stand_jetzt = dict(CAST)
    stand_jetzt["moeglich"] = pychromecast is not None and castbild is not None
    stand_jetzt["adresse"] = f"http://{eigene_adresse()}:{PORT}"
    return jsonify(stand_jetzt)


@app.route("/api/cast/suchen", methods=["POST"])
def api_cast_suchen():
    if pychromecast is None:
        return jsonify({"fehler": "PyChromecast fehlt."}), 503
    try:
        return jsonify({"geraete": cast_suchen()})
    except Exception as exc:
        return jsonify({"fehler": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/api/cast/wecken", methods=["POST", "GET"])
def api_cast_wecken():
    """Anstoss von aussen - gedacht fuer HomeAssistant.

    Schaltet HomeAssistant morgens den Strom am Dongle ein, ruft es gleich
    hier an; dann sucht der Server sofort statt bis zu fuenf Minuten zu
    warten.  GET ist mit erlaubt, damit sich das in HomeAssistant auch mit
    einem simplen Kommandozeilenaufruf erledigen laesst.

    Mit ?an=0 laesst sich das Senden abends genauso beenden.
    """
    wunsch = request.args.get("an")
    if wunsch is None and request.is_json:
        wunsch = (request.get_json(silent=True) or {}).get("an")
    if wunsch is not None and str(wunsch).lower() in ("0", "false", "aus",
                                                      "off", "nein"):
        cast_schalten(False)
        einstellungen_schreiben({"cast_an": False})
        return jsonify({"ok": True, "an": False})

    e = einstellungen_lesen()
    geraet = e["cast_geraet"]
    if not geraet:
        return jsonify({"fehler": "Kein Gerät eingestellt."}), 400
    if not CAST["an"]:
        cast_schalten(True, geraet)
        einstellungen_schreiben({"cast_an": True, "cast_geraet": geraet})
    else:
        # laeuft schon, sucht aber vielleicht gerade mit langer Wartezeit
        with _cast_sperre:
            CAST["sofort"] = True
        _cast_wecker.set()
    return jsonify({"ok": True, "an": True, "geraet": geraet})


@app.route("/api/cast/steuern", methods=["POST"])
def api_cast_steuern():
    """Fernbedienung: weiter, zurueck, pause, oder gezielt auf ein Bild.

    Damit wischt man am Tablet durch das, was am Fernseher laeuft - und
    landet beim Bearbeiten auf genau dem Bild, das die anderen gerade sehen.
    """
    daten = request.get_json(silent=True) or {}
    was = daten.get("was")
    if was not in ("weiter", "zurueck", "pause", "springen"):
        return jsonify({"fehler": "was: weiter, zurueck, pause, springen"}), 400
    if not CAST["an"]:
        return jsonify({"fehler": "Es wird gerade nicht gesendet."}), 409
    ziel = daten.get("id")
    if was == "springen":
        try:
            ziel = int(ziel)
        except (TypeError, ValueError):
            return jsonify({"fehler": "id fehlt"}), 400
    cast_befehl(was, ziel)
    with _cast_sperre:
        return jsonify(dict(CAST))


@app.route("/api/cast", methods=["POST"])
def api_cast_schalten():
    if pychromecast is None or castbild is None:
        return jsonify({"fehler": "PyChromecast oder Pillow fehlt."}), 503
    daten = request.get_json(silent=True) or {}
    geraet = daten.get("geraet")
    an = bool(daten.get("an"))
    if an and not (geraet or CAST["geraet"]):
        return jsonify({"fehler": "Kein Gerät gewählt."}), 400
    cast_schalten(an, geraet)
    einstellungen_schreiben({"cast_an": CAST["an"],
                             "cast_geraet": CAST["geraet"]})
    with _cast_sperre:
        return jsonify(dict(CAST))


# --------------------------------------------------------- Takeout und Inbox

# Name der Samba-Freigabe.  Muss zu /etc/samba/smb.conf passen, siehe
# samba-inbox.conf im Projektverzeichnis.
SAMBA_FREIGABE = "inbox"


def takeout_stand():
    """Was liegt an Nachschub bereit, und was belegt noch Platz?"""
    baum_bytes, baum_dateien = _grosse(TAKEOUT_BAUM)
    # die Original-ZIPs des ersten Exports liegen neben dem Baum
    alt_zips = []
    if TAKEOUT_WURZEL.is_dir():
        for p in sorted(TAKEOUT_WURZEL.iterdir()):
            if p.is_file() and p.name.lower().endswith(ARCHIV_ENDUNGEN):
                alt_zips.append(p)
    fertig = []
    if INBOX_FERTIG.is_dir():
        fertig = [p for p in sorted(INBOX_FERTIG.iterdir()) if p.is_file()]

    def liste(pfade):
        return [{"name": p.name, "bytes": p.stat().st_size} for p in pfade]

    frei = shutil.disk_usage(BASE).free
    return {
        "baum": {"pfad": str(TAKEOUT_BAUM), "bytes": baum_bytes,
                 "dateien": baum_dateien},
        "archive": liste(alt_zips) + liste(fertig),
        "wartend": liste(archive_in_inbox()),
        "inbox": str(INBOX),
        "samba": f"\\\\{socket.gethostname()}\\{SAMBA_FREIGABE}",
        "frei": frei,
    }


@app.route("/api/takeout")
def api_takeout():
    return jsonify(takeout_stand())


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Ein Archiv in die Inbox schreiben.

    Der Browser schickt die Datei als reinen Rumpf, nicht als Formular -
    so laeuft sie im Strom auf die Platte, ohne vorher komplett in den
    Speicher oder nach /tmp zu wandern.  Bei mehreren Gigabyte ist das der
    Unterschied zwischen "laeuft" und "Platte voll".
    """
    name = Path((request.args.get("name") or "").strip()).name
    if not name or not name.lower().endswith(ARCHIV_ENDUNGEN):
        return jsonify({"fehler": "Nur .zip, .tar, .tgz oder .tar.gz"}), 400

    INBOX.mkdir(parents=True, exist_ok=True)
    ziel = INBOX / name
    halb = ziel.with_suffix(ziel.suffix + ".teil")
    geschrieben = 0
    try:
        with halb.open("wb") as f:
            while True:
                brocken = request.stream.read(1024 * 1024)
                if not brocken:
                    break
                f.write(brocken)
                geschrieben += len(brocken)
        halb.replace(ziel)
    except OSError as exc:
        halb.unlink(missing_ok=True)
        return jsonify({"fehler": f"Schreiben fehlgeschlagen: {exc}"}), 500
    if not geschrieben:
        ziel.unlink(missing_ok=True)
        return jsonify({"fehler": "Leere Datei"}), 400
    return jsonify({"ok": True, "name": name, "bytes": geschrieben})


@app.route("/api/takeout/loeschen", methods=["POST"])
def api_takeout_loeschen():
    """Eingelesenen Takeout wegwerfen, um Platz zu schaffen.

    Das ist der einzige Punkt im ganzen Projekt, an dem Originaldateien
    verschwinden.  Was danach bleibt, sind die 1920-px-Thumbnails - die
    Bilder in voller Aufloesung gibt es dann nur noch bei Google.  Deshalb
    muss der Aufrufer ausdruecklich bestaetigen.
    """
    daten = request.get_json(silent=True) or {}
    if daten.get("bestaetigt") != "LOESCHEN":
        return jsonify({"fehler": "Nicht bestätigt."}), 400
    was = daten.get("was")
    if was not in ("baum", "archive", "beides"):
        return jsonify({"fehler": "was: baum, archive oder beides"}), 400
    if IMPORT["aktiv"]:
        return jsonify({"fehler": "Während eines Imports nicht."}), 409

    frei, weg = 0, []
    if was in ("baum", "beides") and TAKEOUT_BAUM.is_dir():
        gross, _ = _grosse(TAKEOUT_BAUM)
        try:
            shutil.rmtree(TAKEOUT_BAUM)
            frei += gross
            weg.append(TAKEOUT_BAUM.name)
        except OSError as exc:
            return jsonify({"fehler": f"{TAKEOUT_BAUM}: {exc}"}), 500
    if was in ("archive", "beides"):
        for ordner in (TAKEOUT_WURZEL, INBOX_FERTIG):
            if not ordner.is_dir():
                continue
            for p in list(ordner.iterdir()):
                if not (p.is_file()
                        and p.name.lower().endswith(ARCHIV_ENDUNGEN)):
                    continue
                try:
                    frei += p.stat().st_size
                    p.unlink()
                    weg.append(p.name)
                except OSError as exc:
                    app.logger.warning("%s nicht löschbar: %s", p, exc)
    return jsonify({"ok": True, "bytes": frei, "anzahl": len(weg)})


@app.route("/favicon.ico")
def favicon():
    return redirect("/bild/1")


def main():
    global PORT
    ap = argparse.ArgumentParser(description="Fotoserver-Viewer")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    PORT = args.port

    # Ohne das schluckt Flask alle info-Meldungen, und im Journal stuende
    # nichts darueber, ob der Fernseher gerade erreichbar ist.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app.logger.setLevel(logging.INFO)

    schema_sicherstellen()
    n = db().execute(
        "SELECT COUNT(*) FROM photos WHERE is_video = 0 "
        "AND thumb_state = 'ok'").fetchone()[0]
    e = einstellungen_lesen()
    print(f"{n} Bilder bereit  ->  http://{args.host}:{args.port}/")
    print(f"Einstellungen: {e['dauer']} s, Modus {e['modus']}, "
          f"{len(e['alben']) or 'alle'} Alben")

    # War das Senden an den Fernseher zuletzt an, geht es von allein wieder
    # los - der Rechner soll ohne Browser auskommen.
    if e["cast_an"] and e["cast_geraet"]:
        if pychromecast is None or castbild is None:
            print("Cast war an, aber PyChromecast oder Pillow fehlt.")
        else:
            print(f"Cast: sende an {e['cast_geraet']}")
            cast_schalten(True, e["cast_geraet"])

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
