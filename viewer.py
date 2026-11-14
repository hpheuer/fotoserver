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
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
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

# Die Mischung fuer modus="zufall" bleibt, solange der Dienst laeuft.
# Frueher wurde bei jedem Abruf neu gemischt - und die Slideshow holt die
# Playlist alle 30 Sekunden nach, sobald sich der Stand geaendert hat.
# Danach zeigte "zurueck" Bilder, die nie zu sehen waren, und der Fernseher
# fing zusaetzlich wieder bei Bild 1 an.
#
# Gemischt wird deshalb ueber einen Sortierschluessel je Bild, nicht mit
# shuffle(): faellt ein Bild weg oder kommt eines dazu, bleiben alle
# anderen stehen, wo sie waren.  shuffle() mit fester Saat wuerde bei
# geaenderter Laenge eine voellig andere Reihenfolge ergeben.
MISCHUNG = random.randrange(1 << 30)


def _mischwert(photo_id):
    return hashlib.md5(f"{MISCHUNG}:{photo_id}".encode()).digest()

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
    "steckdose": "",         # Adresse der Shelly, an der der Rechner haengt
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
# Spalten, die der Viewer selbst anlegt, wenn sie fehlen.  display_date und
# date_confidence legt inzwischen import_photos.py an und fuellt sie auch -
# hier stehen sie nur, damit der Viewer an einer Datenbank aus einem
# aelteren Import nicht mit "no such column" abbricht.
HAND_SPALTEN = (
    ("display_date", "TEXT"),
    ("date_confidence", "TEXT"),
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

        # geloescht_am: bei Google verschwunden, vom Takeout-Abgleich
        # gesetzt.  Das ist etwas anderes als die Streichliste oben - die
        # ist der Papierkorb, also eine Entscheidung von Hand, und sie gilt
        # fuer immer.  Hier steht nur "stand im letzten Takeout nicht mehr
        # drin"; taucht die Datei in einem spaeteren Export wieder auf,
        # setzt der Abgleich die Spalte zurueck auf NULL.  Deshalb eine
        # Spalte an photos und kein Eintrag in geloescht: sonst muesste
        # der Papierkorb Eintraege anzeigen, die niemand dorthin gelegt
        # hat, und ein Wiederherstellen haette zwei Bedeutungen.
        #
        # Ein eigener Index braucht es nicht.  Gefragt wird immer nach
        # IS NULL, und das ist bei 24.000 Zeilen der Normalfall - dafuer
        # liest SQLite ohnehin die Tabelle.  Der Index auf sha256 steht
        # schon da: die Spalte ist UNIQUE, SQLite legt ihn selbst an.
        if "geloescht_am" not in {r[1] for r in
                                  conn.execute("PRAGMA table_info(photos)")}:
            conn.execute("ALTER TABLE photos ADD COLUMN geloescht_am TEXT")
            print("Spalte angelegt: geloescht_am")
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


# Rechnername oder IP, wahlweise mit Port - mehr nicht.  Die Adresse landet
# in steckdose.sh als http://$ADR/relay/0?…; stuende ein Schraegstrich oder
# Fragezeichen darin, liesse sich der angehaengte Teil der Anfrage von hier
# aus umschreiben.  Ein Schema (http://) gehoert ebenfalls nicht hinein, das
# setzt das Skript selbst.
STECKDOSE_MUSTER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}(?::\d{1,5})?$")


def steckdose_gueltig(adresse):
    """Leer ist erlaubt - dann besorgt den Strom jemand anders."""
    adresse = str(adresse or "").strip()
    return not adresse or bool(STECKDOSE_MUSTER.fullmatch(adresse))


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
        # Muss hier stehen, auch wenn die Seite den Wert nie schickt: was
        # saubere_werte nicht kennt, faellt beim naechsten Speichern raus -
        # und ohne Adresse bliebe der Rechner nach dem Halt fuer immer aus.
        # Unsinn wird verworfen, nicht durchgereicht; die Route meldet ihn
        # vorher schon zurueck, hier ist es nur das Netz darunter.
        "steckdose": (str(werte.get("steckdose") or "").strip()
                      if steckdose_gueltig(werte.get("steckdose"))
                      else VORGABEN["steckdose"]),
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


def wahrheit(wert, vorgabe=False):
    """Was aus JSON oder einer URL kommt, als Wahrheitswert lesen.

    bool("false") ist True, und genau das kommt an, wenn ein Aufrufer
    "an": "false" schickt statt eines echten JSON-false.
    """
    if wert is None:
        return vorgabe
    if isinstance(wert, bool):
        return wert
    return str(wert).strip().lower() in ("1", "true", "ja", "yes", "an", "on")


# Gestrichene Bilder tauchen nirgends mehr auf.  Der Abgleich laeuft ueber
# den SHA-256, damit er einen erneuten Import derselben Datei ueberlebt.
#
# Zwei Wege fuehren hierher: der Papierkorb (Streichliste, von Hand) und
# der Takeout-Abgleich (geloescht_am, weil das Bild bei Google weg ist).
# Beide verstecken das Bild gleich gruendlich, nur zuruecknehmen laesst
# sich jeder auf seine Weise.
NICHT_GELOESCHT = ("NOT EXISTS (SELECT 1 FROM geloescht g"
                   " WHERE g.sha256 = p.sha256)"
                   " AND p.geloescht_am IS NULL")


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

    # Platz 4: der Rang in der Zeit.  Den braucht die Slideshow, um von
    # Hand zeitlich zu blaettern - im Zufallsmodus sagt der Nachbar in der
    # Liste nichts ueber das Bild, der zeitliche Nachbar fast immer.
    for rang, e in enumerate(reversed(items) if modus == "neueste" else items):
        e.append(rang)

    if modus == "zufall":
        items.sort(key=lambda e: _mischwert(e[0]))
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
            meldung = f"Alle {len(SCHRITTE)} Schritte fertig."
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
        # Hier abweisen statt in saubere_werte still zu verwerfen: eine
        # verworfene Adresse waere eine leere Adresse, und der Rechner bliebe
        # nach dem naechsten Halt aus, ohne dass jemand etwas gemerkt haette.
        if "steckdose" in daten and not steckdose_gueltig(daten["steckdose"]):
            return jsonify({
                "fehler": "Adresse der Steckdose: nur Name oder IP, "
                          "wahlweise mit :Port — ohne http:// und ohne Pfad.",
            }), 400
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
_vergeben = None            # kleingeschrieben -> (Name, cc, Land, Anzahl)
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


def vergeben_vergessen():
    """Naechste Ortssuche laedt die vergebenen Namen neu.

    Aufzurufen, wo ein Ort geschrieben wird - sonst fehlte der eben von
    Hand vergebene Name in der Vorschlagsliste, ausgerechnet dort, wo man
    ihn gleich noch einmal braucht.
    """
    global _vergeben
    _vergeben = None


def vergebene_orte():
    """Ortsnamen, die an Bildern schon dranstehen -> Vorschlagsliste.

    Sowohl die geokodierten als auch die von Hand eingetragenen. Sie
    stehen oben: was hier schon einmal vergeben wurde, ist beim naechsten
    Bild fast immer wieder gemeint - und freie Eingaben ("Ferienhaus am
    See") gibt es in orte.tsv ueberhaupt nicht.
    """
    global _vergeben
    if _vergeben is None:
        ort = "COALESCE(p.hand_place, p.place)"
        d = {}
        try:
            for r in db().execute(
                    f"SELECT {ort} AS o,"
                    " COALESCE(p.hand_country, p.place_country) AS land,"
                    " COALESCE(p.hand_cc, p.place_cc) AS cc, COUNT(*) AS n"
                    f" FROM photos p WHERE {ort} IS NOT NULL AND {ort} <> ''"
                    f" AND {NICHT_GELOESCHT}"
                    " GROUP BY o, land, cc"):
                k = r["o"].lower()
                # Gleicher Name, verschiedene Laender: der haeufigere zaehlt
                if k in d and d[k][3] >= r["n"]:
                    continue
                d[k] = (r["o"], r["cc"] or "", r["land"] or "", r["n"])
        except sqlite3.Error:
            d = {}
        _vergeben = d
    return _vergeben


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
    # Was hier schon vergeben ist, steht vor allem anderen: beim naechsten
    # Bild desselben Ausflugs ist fast immer wieder derselbe Ort gemeint.
    for k, (name, cc, land, anzahl) in vergebene_orte().items():
        rang = -2 if k.startswith(q) else (2.5 if q in k else None)
        if rang is None:
            continue
        treffer.append((rang, 0, -anzahl, 0, name, cc, land, "", "Ort", anzahl))
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
                        name, cc, land, region, "Ort", 0))
    # Land und Region haben nur fuenf Felder - auf dieselbe Form bringen
    treffer = [t if len(t) == 10 else (t[0], 0, 0, t[1], *t[2:], 0)
               for t in treffer]
    treffer.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))

    raus, gesehen = [], set()
    for *_, name, cc, land, region, art, anzahl in treffer:
        # Region bleibt aus dem Schluessel, sobald der Ort schon vergeben
        # ist: sonst stuende Stutensee zweimal da, einmal mit Landkreis
        # und einmal ohne.  Der vergebene Eintrag kommt zuerst und gewinnt.
        schluessel = (name.lower(), cc, "" if anzahl else region, art)
        if schluessel in gesehen or (name.lower(), cc, "", art) in gesehen:
            continue
        gesehen.add(schluessel)
        raus.append({"ort": name, "cc": cc, "land": land_name(cc, land),
                     "region": region, "art": art, "vergeben": anzahl})
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
    # 'unsicher' ist das Empfangs-, nicht das Aufnahmedatum - da fehlt die
    # Angabe genauso wie bei NULL.
    d["hatte_datum"] = (e["d_datum"] is not None and e["d_conf"] != "unsicher")

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
    if "hand_place" in felder:
        vergeben_vergessen()

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


# Zeit eines Bildes fuer die Nachbarschaft: was von Hand eingetragen wurde,
# sonst das Aufnahmedatum aus dem Import, sonst das Dateidatum.  taken_at
# ist genau das - EXIF, sonst Takeout-JSON, sonst die Zeit der Datei -,
# und anders als display_date steht es bei jedem Bild.  Ohne diesen
# Rueckfall haetten ausgerechnet die Bilder ohne verlaessliches Datum
# keinen Platz in der Zeitachse, also die, um die es hier geht.
ZEIT = "COALESCE(p.hand_date, p.display_date, p.taken_at)"


def _fehlt(was, a):
    """SQL-Bedingung 'diese Angabe fehlt noch'."""
    teile = []
    if was in ("ort", "beides"):
        teile.append(f"{a['ort']} IS NULL")
    if was in ("datum", "beides"):
        teile.append(f"({a['datum']} IS NULL OR {a['conf']} = 'unsicher')")
    return "(" + " OR ".join(teile) + ")"


@app.route("/api/foto/<int:photo_id>/umfeld")
def api_umfeld(photo_id):
    """Zeitlich benachbarte Bilder, bei denen dieselbe Angabe noch fehlt.

    Grundlage fuer die Rueckfrage nach dem Speichern: wer einen Ort oder
    ein Datum nachtraegt, hat es meist fuer den ganzen Stapel richtig.
    Die Nachbarschaft in der Zeit trifft den Stapel zuverlaessiger als das
    Album - Takeout legt jedes Bild zusaetzlich in ein "Fotos von JJJJ",
    und ein Ausflug erstreckt sich oft ueber mehrere Alben.

    Geblaettert wird ueber den Anker (anker_zeit, anker_id), nicht ueber
    OFFSET: waehrend des Uebernehmens verschieben sich die Bilder in der
    Liste, ein Zaehler zeigte danach auf etwas anderes.

    Geliefert werden alle Bilder der Zeitachse, auch die, bei denen die
    Angabe schon steht - erst die Nachbarn mit Ort verraten, wo die ohne
    Ort entstanden sind.  "fehlt" sagt je Bild, ob es zum Stapel gehoert;
    "hier" markiert das eben bearbeitete, es steht an seinem Platz mit
    im Fenster.  "gesamt" zaehlt weiterhin nur die ohne Angabe.

    richtung: mitte (um das Bild herum), zurueck (aeltere), vor (neuere)
    """
    was = request.args.get("was", "beides")
    if was not in ("ort", "datum", "beides"):
        was = "beides"
    try:
        n = min(max(int(request.args.get("n", 20)), 1), 200)
    except ValueError:
        n = 20
    richtung = request.args.get("richtung", "mitte")

    a = ausdruecke()
    hier = db().execute(f"SELECT {ZEIT} AS z FROM photos p WHERE p.id = ?",
                        (photo_id,)).fetchone()
    if not hier:
        abort(404)
    anker_zeit = request.args.get("anker_zeit") or hier["z"] or ""
    try:
        anker_id = int(request.args.get("anker_id", photo_id))
    except (TypeError, ValueError):
        anker_id = photo_id

    spalten = (f"SELECT p.id, {ZEIT} AS z,"
               f" {a['datum']} AS d_datum, {a['conf']} AS d_conf,"
               f" {a['ort']} AS d_ort, {a['land']} AS d_land,"
               f" {a['cc']} AS d_cc, {a['km']} AS d_km,"
               f" {_fehlt(was, a)} AS d_fehlt"
               f" FROM photos p")
    rumpf = (spalten +
             f" WHERE p.is_video = 0 AND p.thumb_state = 'ok'"
             f"   AND {NICHT_GELOESCHT} AND {ZEIT} IS NOT NULL")

    def eintrag(r):
        return {"id": r["id"], "zeit": r["z"], "fehlt": bool(r["d_fehlt"]),
                "hier": r["id"] == photo_id,
                "datum": datum_text(r["d_datum"], r["d_conf"]),
                "ort": ort_text(r["d_ort"], r["d_land"], r["d_cc"],
                                r["d_km"])}

    def hole(vorwaerts, zeit, pid, wieviel):
        """wieviel Bilder aelter (vorwaerts=False) bzw. neuer als der Anker."""
        if wieviel <= 0:
            return []
        if vorwaerts:
            wo = f" AND (z > ? OR (z = ? AND p.id > ?)) ORDER BY z, p.id LIMIT ?"
        else:
            wo = (f" AND (z < ? OR (z = ? AND p.id < ?))"
                  f" ORDER BY z DESC, p.id DESC LIMIT ?")
        rows = db().execute(rumpf + wo,
                            (zeit, zeit, pid, wieviel)).fetchall()
        liste = [eintrag(r) for r in rows]
        if not vorwaerts:
            liste.reverse()                 # immer aelteste zuerst
        return liste

    if richtung == "zurueck":
        fenster = hole(False, anker_zeit, anker_id, n)
    elif richtung == "vor":
        fenster = hole(True, anker_zeit, anker_id, n)
    else:
        # Um das Bild herum: halb/halb, und was die eine Seite nicht
        # hergibt, holt die andere nach - am Rand der Bibliothek sollen
        # trotzdem 20 Bilder zusammenkommen.
        # Das Bild selbst steht mittendrin, damit man sieht, wo in der
        # Zeitachse man ist.
        selbst = db().execute(rumpf + " AND p.id = ?",
                              (photo_id,)).fetchall()
        rest = n - len(selbst)
        alt = hole(False, anker_zeit, anker_id, rest // 2)
        neu = hole(True, anker_zeit, anker_id, rest - len(alt))
        if len(neu) < rest - len(alt):
            alt = hole(False, anker_zeit, anker_id, rest - len(neu))
        fenster = alt + [eintrag(r) for r in selbst] + neu

    if fenster:
        erste, letzte = fenster[0], fenster[-1]
        mehr_vorher = bool(hole(False, erste["zeit"], erste["id"], 1))
        mehr_nachher = bool(hole(True, letzte["zeit"], letzte["id"], 1))
    else:
        mehr_vorher = mehr_nachher = False

    gesamt = db().execute(
        f"SELECT COUNT(*) FROM photos p"
        f" WHERE p.is_video = 0 AND p.thumb_state = 'ok'"
        f"   AND {NICHT_GELOESCHT} AND {ZEIT} IS NOT NULL"
        f"   AND p.id <> ? AND " + _fehlt(was, a), (photo_id,)).fetchone()[0]

    return jsonify({"bilder": fenster, "gesamt": gesamt,
                    "mehr_vorher": mehr_vorher, "mehr_nachher": mehr_nachher})


@app.route("/api/uebernehmen", methods=["POST"])
def api_uebernehmen():
    """Datum und/oder Ort auf mehrere Bilder schreiben.

    Ergaenzt wird nur, was dort noch fehlt.  Ein Stapel enthaelt fast
    immer auch Bilder, bei denen nur die eine der beiden Angaben fehlt -
    wuerde stumpf ueberschrieben, riebe das Nachtragen eines Ortes das
    schon bekannte Datum der Nachbarn weg.

    Ausnahme "ersetzen": Bilder, bei denen die Angabe schon stand und die
    trotzdem einzeln angetippt wurden.  Das ist eine bewusste Berichtigung,
    dort wird ueberschrieben.  "Alle" waehlt sie deshalb nie mit aus.
    """
    daten = request.get_json(silent=True) or {}
    try:
        ids = [int(x) for x in daten.get("ids") or []]
        ersetzen = [int(x) for x in daten.get("ersetzen") or []]
    except (TypeError, ValueError):
        return jsonify({"fehler": "ids müssen Zahlen sein"}), 400
    if not ids and not ersetzen:
        return jsonify({"fehler": "ids nötig"}), 400

    ort = (daten.get("ort") or "").strip()
    roh = (daten.get("datum") or "").strip()
    if not ort and not roh:
        return jsonify({"fehler": "Datum oder Ort nötig"}), 400
    gedeutet = None
    if roh:
        gedeutet = datum_deuten(roh)
        if not gedeutet:
            return jsonify({"fehler": f"Datum '{roh}' nicht verstanden"}), 400

    land = (daten.get("land") or "").strip() or None
    cc = (daten.get("cc") or "").strip().upper() or None
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    a = ausdruecke()
    platz = ",".join("?" * len(ids))
    # Der Umweg ueber die Unterabfrage ist noetig, weil ausdruecke() den
    # Alias p benutzt, den ein UPDATE nicht kennt.
    platz_e = ",".join("?" * len(ersetzen))

    def nur_fehlende(bedingung):
        wo = f" WHERE id IN (SELECT p.id FROM photos p WHERE p.id IN ({platz}) AND {bedingung})"
        if ersetzen:
            wo += f" OR id IN ({platz_e})"
        return wo

    conn = db_schreiben()
    try:
        n_ort = n_datum = 0
        if ort:
            cur = conn.execute(
                "UPDATE photos SET hand_place = ?, hand_country = ?,"
                " hand_cc = ?, hand_at = ?"
                + nur_fehlende(f"{a['ort']} IS NULL"),
                [ort, land, cc, jetzt, *ids, *ersetzen])
            n_ort = cur.rowcount
        if gedeutet:
            cur = conn.execute(
                "UPDATE photos SET hand_date = ?, hand_date_confidence = ?,"
                " hand_at = ?"
                + nur_fehlende(f"({a['datum']} IS NULL"
                               f" OR {a['conf']} = 'unsicher')"),
                [gedeutet[0], gedeutet[1], jetzt, *ids, *ersetzen])
            n_datum = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if ort:
        vergeben_vergessen()
    return jsonify({"ok": True, "ort": n_ort, "datum": n_datum,
                    "anzahl": max(n_ort, n_datum), "stand": stand()})


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

# So lange darf das Abmelden dauern.  Der Socket-Faden von pychromecast
# reagiert naemlich nicht sofort auf disconnect(): steckt er gerade in einem
# Verbindungsversuch, laeuft erst dessen Zeitgrenze ab (TIMEOUT_TIME, 30 s),
# und danach schlaeft er noch RETRY_TIME (5 s) in einem time.sleep, das vom
# Stopp-Signal nichts weiss.  Wer frueher aufgibt und die Suche zumacht,
# stellt genau die Lage her, gegen die _cast_aufraeumen() gebaut ist.
CAST_ABMELDE_FRIST = 40


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


def _cast_aufraeumen(cast, browser):
    """Verbindung und Suche zumachen - und zwar in dieser Reihenfolge.

    pychromecast haelt fuer jeden Chromecast einen eigenen Faden, der von
    sich aus wieder verbinden will.  Wird nur der Browser beendet, laeuft
    dieser Faden weiter und greift auf die schon gestoppte
    Zeroconf-Instanz zu.  Dann wirft er

        AssertionError: Zeroconf instance loop must be running

    faengt sich selbst wieder ("attempting reconnect") und versucht es
    sofort erneut - ohne jede Pause.  Am 10.8.2026 waren das den halben
    Abend lang 700.000 Journalmeldungen je 30 Sekunden und 44 % CPU, nur
    weil dem Dongle nach dem Fruehstueck der Strom fehlte.

    disconnect() beendet den Faden und wartet selbst auf ihn - erst danach
    darf die Suche zu.  Andersherum entsteht genau der Sturm.

    Das Warten braucht Geduld: fuenf Sekunden reichten am 11.8.2026 nicht,
    der Faden hing noch neun Sekunden laenger im Verbindungsversuch (siehe
    CAST_ABMELDE_FRIST).  Die Suche wurde derweil schon geschlossen - dass
    es gut ging, war Glueck.
    """
    if cast is not None:
        try:
            # Ein eigenes join() danach braucht es nicht: disconnect()
            # wartet bereits auf den Faden und wirft, wenn er nicht endet.
            cast.disconnect(timeout=CAST_ABMELDE_FRIST)
        except Exception as exc:
            app.logger.warning(
                "Cast-Faden lebt nach %s s noch (%s) — Suche wird trotzdem "
                "geschlossen", CAST_ABMELDE_FRIST, exc)
    _suche_beenden(browser)


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
    # Scheitert das Warten, kommt der Aufrufer nie an dieses Objekt - sein
    # Faden liefe aber weiter und suchte endlos neu.  Also hier aufraeumen,
    # solange man es noch in der Hand hat.
    try:
        cast.wait(timeout=15)
    except Exception:
        _cast_aufraeumen(cast, browser)
        raise
    return cast, browser


CAST_VERLAUF = 500          # so viele gezeigte Bilder merkt sich der Fernseher


def _ordnung(bilder):
    """Nachschlagewerke zur Playlist -> (chrono, rang, plaetze).

    chrono[r] ist der Platz des r-ten Bildes in der Zeit, rang[platz] der
    Umkehrweg, plaetze[id] der Platz eines Bildes.  Im Zufallsmodus sagt
    der Nachbar in der Playlist nichts ueber das Bild, der zeitliche
    Nachbar fast immer - deshalb die zweite Ordnung.
    """
    chrono = sorted(range(len(bilder)), key=lambda k: bilder[k][4])
    return (chrono,
            {k: r for r, k in enumerate(chrono)},
            {b[0]: k for k, b in enumerate(bilder)})


def _zeit_nachbar(platz, richtung, chrono, rang):
    """Der zeitliche Nachbar eines Platzes, am Rand umlaufend."""
    r = rang.get(platz)
    if r is None or not chrono:
        return None
    return chrono[(r + richtung) % len(chrono)]


def _verlauf_schritt(verlauf, v_pos, richtung, plaetze):
    """Ein Schritt durch den Verlauf -> (v_pos, platz) oder None.

    Bilder, die inzwischen im Papierkorb liegen, werden uebersprungen;
    ist der Verlauf in dieser Richtung zu Ende, kommt None zurueck.
    """
    p = v_pos
    while True:
        p += richtung
        if not 0 <= p < len(verlauf):
            return None
        platz = plaetze.get(verlauf[p])
        if platz is not None:
            return p, platz


def _verlauf_merken(verlauf, v_pos, bild_id, richtung):
    """Ein neu gezeigtes Bild in den Verlauf haengen -> neues v_pos.

    Rueckwaerts wird vorn angesetzt, vorwaerts hinten - und was vorwaerts
    dazukommt, verwirft den bisherigen Vorwaertsteil: von dort aus ist
    dieses Bild jetzt das naechste.
    """
    if richtung < 0:
        verlauf.insert(0, bild_id)
        del verlauf[CAST_VERLAUF:]
        return 0
    del verlauf[v_pos + 1:]
    verlauf.append(bild_id)
    ueber = len(verlauf) - CAST_VERLAUF
    if ueber > 0:
        del verlauf[:ueber]
    return len(verlauf) - 1


def _cast_lauf():
    """Schickt der Reihe nach Bilder an den Fernseher.

    Laeuft unabhaengig von jedem Browser - der Rechner darf kopflos sein.
    Grundlage sind dieselben Einstellungen wie fuer die Slideshow: Dauer,
    Reihenfolge, Albenauswahl.  Aendert sich der Stand, wird die Liste neu
    geholt; ein Bild spaeter merkt man das auf dem Fernseher.
    """
    cast = browser = None
    bilder, marke, n = [], None, 0
    chrono, rang, plaetze = [], {}, {}
    richtung = 1                # wohin zuletzt geblaettert wurde
    # Was wirklich zu sehen war, in dieser Reihenfolge.  Nur so zeigt
    # "zurueck" genau die zuvor gezeigten Bilder - die Playlist allein
    # weiss nichts davon, wenn zwischendurch nachgeladen wurde.
    verlauf, v_pos = [], -1
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
                lief = bilder[n % len(bilder)][0] if bilder else None
                bilder = playlist(e["modus"], e["alben"], None,
                                  e["ohne_album"])
                chrono, rang, plaetze = _ordnung(bilder)
                marke = jetzt
                # Dort weitermachen, wo der Fernseher steht.  Frueher stand
                # hier n = 0: jede Aenderung - ein geloeschtes Bild, ein
                # nachgetragener Ort - warf die Ausgabe zurueck an den
                # Anfang, und "zurueck" zeigte danach Fremdes.
                n = next((k for k, b in enumerate(bilder) if b[0] == lief),
                         n % len(bilder) if bilder else 0)
                with _cast_sperre:
                    CAST["anzahl"] = len(bilder)
            if not bilder:
                _cast_melden("Keine Bilder in dieser Auswahl.")
                _warte(2)
                continue

            n %= len(bilder)
            if zeigen:
                bild_id, datum, ort = bilder[n][:3]
                cast.media_controller.play_media(
                    f"{basis}/cast/bild/{bild_id}", "image/jpeg",
                    title=" · ".join(x for x in (datum, ort) if x) or None)
                with _cast_sperre:
                    CAST["bild_nr"] = n + 1
                    CAST["bild"] = [bild_id, datum, ort]
                if not (0 <= v_pos < len(verlauf)
                        and verlauf[v_pos] == bild_id):
                    v_pos = _verlauf_merken(verlauf, v_pos, bild_id, richtung)

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
                    richtung, n = 1, wo
            elif schritt:
                # Von Hand geblaettert: erst durch den Verlauf, dann
                # zeitlich weiter.  Ein Zufallsnachbar sagt nichts ueber
                # das Bild - der zeitliche Nachbar zeigt denselben Ausflug.
                richtung = 1 if schritt > 0 else -1
                for _ in range(abs(schritt)):
                    treffer = _verlauf_schritt(verlauf, v_pos, richtung,
                                               plaetze)
                    if treffer is not None:
                        v_pos, n = treffer
                        continue
                    k = _zeit_nachbar(n, richtung, chrono, rang)
                    if k is None:
                        break
                    n = k
                zeigen = True
            else:
                # regulaerer Ablauf der Anzeigedauer; im Pausenzustand
                # bleibt das Bild stehen
                zeigen = not geweckt and not CAST["pausiert"]
                if zeigen:
                    richtung = 1
                    n += 1

        except Exception as exc:                       # pragma: no cover
            # Nach dem Fruehstueck nimmt HomeAssistant dem Dongle den Strom -
            # aus Sicht des Servers bricht die Verbindung einfach ab.  Das
            # ist der Normalfall, kein Fehler: wegraeumen und in Ruhe auf
            # den naechsten Morgen warten.
            _cast_melden(f"Verbindung verloren ({type(exc).__name__}) — "
                         f"wird weiter gesucht", verbunden=False)
            # Auch das cast-Objekt muss weg, nicht nur die Suche - sonst
            # dreht sein Faden im Leerlauf weiter (siehe _cast_aufraeumen).
            _cast_aufraeumen(cast, browser)
            cast = browser = None
            _warte(fehlversuche)
            fehlversuche += 1

    # sauber abmelden, sonst bleibt das Bild stehen
    if cast is not None:
        try:
            cast.quit_app()
        except Exception:
            pass
    _cast_aufraeumen(cast, browser)
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
    # Solange nicht gesendet wird, steht in CAST kein Geraetename - das
    # zuletzt gewaehlte steht aber in den Einstellungen.  Ohne diesen
    # Rueckgriff zeigte die Einstellungsseite nach einem Neustart eine leere
    # Auswahl, obwohl das Geraet laengst gespeichert ist.
    if not stand_jetzt["geraet"]:
        stand_jetzt["geraet"] = einstellungen_lesen()["cast_geraet"]
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
    # bool("false") waere True - JSON-Wahrheitswerte kommen aber nicht
    # immer als echtes true/false an, sondern auch als "0" oder "false".
    an = wahrheit(daten.get("an"))
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


# -------------------------------------------------------------- Sicherung

# Sichern heisst: eine fremde Platte einhaengen und mit vollen Rechten
# darauf schreiben.  Der Viewer kann das nicht - er laeuft als normaler
# Benutzer mit NoNewPrivileges und darf ausserhalb von ~/photoserver und
# ~/takeout-import nicht einmal etwas anlegen.  Das ist Absicht und bleibt so.
#
# Also stellt er nur einen Auftrag: zwei Zeilen in sicherung-auftrag.txt.
# photoserver-sicherung.path sieht die Datei auftauchen und startet
# sicherung.sh als root; das Skript prueft selbst, ob das genannte Ziel ein
# USB-Datentraeger und nicht die Systemplatte ist.  Zurueck kommen
# sicherung-stand.json (Fortschritt) und sicherung-web.log (Mitschrift).
#
# Der Umweg ueber eine Datei statt ueber sudo ist der Punkt: der Dienst
# bekommt kein einziges zusaetzliches Recht dazu.
SICHERUNG_AUFTRAG = BASE / "sicherung-auftrag.txt"
SICHERUNG_STAND = BASE / "sicherung-stand.json"
SICHERUNG_LOG = BASE / "sicherung-web.log"
SICHERUNG_UNIT = "photoserver-sicherung"

# Was sich beschreiben laesst.  iso9660 und squashfs sind Nur-Lese-
# Dateisysteme - ein Installationsstick soll gar nicht erst zur Wahl stehen.
DATEISYSTEME = ("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs",
                "reiserfs", "vfat", "exfat", "ntfs", "ntfs3")
FREMDE_DATEISYSTEME = ("vfat", "exfat", "ntfs", "ntfs3")

# So lange darf ein Auftrag herumliegen, bevor klar ist, dass ihn niemand
# abholt - dann fehlt die systemd-Regel.
AUFTRAG_GEDULD = 30.0


def _by_id(geraet):
    """Stabiler Name eines Geraets unter /dev/disk/by-id.

    /dev/sdb1 wandert beim Umstecken, der Name unter by-id nicht.  Genau
    dieser Name geht in den Auftrag - so sichert niemand versehentlich auf
    die Platte, die gerade zufaellig denselben Buchstaben hat.
    """
    ordner = Path("/dev/disk/by-id")
    if not ordner.is_dir():
        return None
    ziel = os.path.realpath(geraet)
    treffer = []
    for link in ordner.iterdir():
        try:
            if os.path.realpath(link) == ziel:
                treffer.append(link.name)
        except OSError:
            pass
    # wwn-0x5001b4... sagt niemandem etwas; usb-Samsung_... schon.
    treffer.sort(key=lambda n: (n.startswith("wwn-"), len(n)))
    return treffer[0] if treffer else None


def _lsblk():
    try:
        fertig = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,PATH,SIZE,MODEL,LABEL,FSTYPE,TRAN,HOTPLUG,TYPE,MOUNTPOINT"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if fertig.returncode != 0:
        return []
    try:
        return json.loads(fertig.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        return []


def system_platte():
    """Die Platte, auf der / liegt - die kommt als Ziel nie in Frage."""
    try:
        quelle = subprocess.run(["findmnt", "-no", "SOURCE", "/"],
                                capture_output=True, text=True,
                                timeout=10).stdout.strip()
        eltern = subprocess.run(["lsblk", "-no", "PKNAME", quelle],
                                capture_output=True, text=True,
                                timeout=10).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"/dev/{eltern[0].strip()}" if eltern else None


def sicherungs_medien():
    """Angesteckte USB-Datentraeger, auf die sich sichern laesst.

    Angeboten wird, was ein beschreibbares Dateisystem hat - eine Partition
    oder, bei Sticks ohne Partitionstabelle, das Geraet selbst.
    """
    system = system_platte()
    raus = []
    for platte in _lsblk():
        if platte.get("type") != "disk" or platte.get("tran") != "usb":
            continue
        if platte.get("path") == system:
            continue
        teile = platte.get("children") or [platte]
        for teil in teile:
            fs = (teil.get("fstype") or "").lower()
            if fs not in DATEISYSTEME:
                continue
            kennung = _by_id(teil.get("path", ""))
            if not kennung:
                continue
            frei = None
            einhaeng = teil.get("mountpoint")
            if einhaeng:
                try:
                    frei = shutil.disk_usage(einhaeng).free
                except OSError:
                    pass
            raus.append({
                "id": kennung,
                "geraet": teil.get("path"),
                "modell": (platte.get("model") or "").strip(),
                "etikett": teil.get("label") or "",
                "dateisystem": fs,
                "bytes": teil.get("size") or 0,
                "frei": frei,
                "eingehaengt": einhaeng or "",
                "fremd": fs in FREMDE_DATEISYSTEME,
            })
    return raus


def _dienst_laeuft(unit):
    """Laeuft die Unit gerade?  Ohne systemctl gilt: keine Ahnung, also nein."""
    try:
        fertig = subprocess.run(["systemctl", "is-active", unit],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return fertig.stdout.strip() in ("active", "activating", "reloading")


def sicherung_eingerichtet():
    """Gibt es die systemd-Regel, die den Auftrag abholt?"""
    return Path(f"/etc/systemd/system/{SICHERUNG_UNIT}.path").exists()


def _letzte_zeilen(pfad, anzahl=400):
    try:
        with pfad.open("r", encoding="utf-8", errors="replace") as f:
            return [z.rstrip("\n") for z in deque(f, maxlen=anzahl)]
    except OSError:
        return []


def sicherung_status():
    """Wie weit ist der Lauf?  Zusammengesetzt aus Auftrag, Stand und Log."""
    stand = {}
    if SICHERUNG_STAND.exists():
        try:
            stand = json.loads(SICHERUNG_STAND.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stand = {}

    wartet = SICHERUNG_AUFTRAG.exists()
    laeuft = _dienst_laeuft(f"{SICHERUNG_UNIT}.service")

    if wartet and not laeuft:
        # Der Auftrag liegt da und niemand holt ihn ab.  Nach einer halben
        # Minute ist klar: die systemd-Regel fehlt oder ist aus.  Dann den
        # Auftrag zuruecknehmen, statt ihn auf ewig liegen zu lassen - beim
        # naechsten Start wuerde er sonst ploetzlich losgehen.
        try:
            alt = time.time() - SICHERUNG_AUFTRAG.stat().st_mtime
        except OSError:
            alt = 0
        if alt > AUFTRAG_GEDULD:
            SICHERUNG_AUFTRAG.unlink(missing_ok=True)
            wartet = False
            stand = {
                "aktiv": False, "erfolg": False, "schritt_nr": 0,
                "meldung": "Der Sicherungsdienst hat den Auftrag nicht "
                           "abgeholt - „Fotoserver einrichten“ starten.",
            }

    aktiv = bool(wartet or laeuft)
    if stand.get("aktiv") and not aktiv:
        # Stehengeblieben, etwa weil der Rechner mitten im Lauf ausging.
        stand["aktiv"] = False
        stand["erfolg"] = False
        stand.setdefault("meldung", "Abgebrochen - der Lauf endete unerwartet.")

    return {
        "eingerichtet": sicherung_eingerichtet(),
        "aktiv": aktiv,
        "wartet": wartet,
        "schritt": stand.get("schritt"),
        "schritt_nr": stand.get("schritt_nr", 0),
        "schritte_gesamt": stand.get("schritte_gesamt", 6),
        "begonnen": stand.get("begonnen"),
        "beendet": stand.get("beendet"),
        "erfolg": stand.get("erfolg"),
        "meldung": stand.get("meldung"),
        "umfang": stand.get("umfang"),
        "ziel": stand.get("ziel"),
        "zeilen": _letzte_zeilen(SICHERUNG_LOG),
    }


@app.route("/api/sicherung")
def api_sicherung():
    return jsonify({"medien": sicherungs_medien(), **sicherung_status()})


@app.route("/api/sicherung/status")
def api_sicherung_status():
    return jsonify(sicherung_status())


@app.route("/api/sicherung/start", methods=["POST"])
def api_sicherung_start():
    """Einen Sicherungsauftrag hinterlegen.

    Geprueft wird hier nur das Naheliegende: gibt es das Medium ueberhaupt,
    ist der Umfang einer von zweien, laeuft nicht schon etwas.  Ob auf das
    Ziel geschrieben werden darf, entscheidet sicherung.sh als root - der
    Auftrag ist eine Bitte, keine Anweisung.
    """
    daten = request.get_json(silent=True) or {}
    ziel = str(daten.get("ziel", "")).strip()
    umfang = str(daten.get("umfang", "klein")).strip()

    if umfang not in ("alles", "klein"):
        return jsonify({"fehler": "Umfang: alles oder klein"}), 400
    if not re.fullmatch(r"[A-Za-z0-9._:+-]{1,120}", ziel):
        return jsonify({"fehler": "Unzulässiger Zielname"}), 400
    if not sicherung_eingerichtet():
        return jsonify({
            "fehler": "Der Sicherungsdienst ist nicht eingerichtet.",
            "hinweis": "Einmalig „Fotoserver einrichten“ starten.",
        }), 503
    if ziel not in [m["id"] for m in sicherungs_medien()]:
        return jsonify({"fehler": "Dieses Medium ist nicht (mehr) da."}), 400
    if IMPORT["aktiv"]:
        return jsonify({"fehler": "Erst den Import abwarten."}), 409
    stand = sicherung_status()
    if stand["aktiv"]:
        return jsonify({"fehler": "Es läuft schon eine Sicherung."}), 409

    # Das Protokoll leeren, bevor der Auftrag herausgeht: sonst stuende bis
    # zum ersten Lebenszeichen des Skripts der Text des letzten Laufs da,
    # und man haelt ihn fuer den aktuellen.
    try:
        SICHERUNG_LOG.write_text("Auftrag abgeschickt, der Dienst startet …\n",
                                 encoding="utf-8")
        SICHERUNG_STAND.write_text(json.dumps({
            "aktiv": True, "schritt": "Auftrag abgeschickt", "schritt_nr": 0,
            "schritte_gesamt": 6, "umfang": umfang, "ziel": ziel,
            "begonnen": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        }), encoding="utf-8")
        # Erst schreiben, dann an seinen Platz schieben - sonst koennte die
        # systemd-Regel eine halb geschriebene Datei erwischen.
        halb = SICHERUNG_AUFTRAG.with_suffix(".teil")
        halb.write_text(f"ziel={ziel}\numfang={umfang}\n", encoding="utf-8")
        halb.replace(SICHERUNG_AUFTRAG)
    except OSError as exc:
        return jsonify({"fehler": f"Auftrag nicht ablegbar: {exc}"}), 500

    app.logger.info("Sicherung angefordert: %s, Umfang %s", ziel, umfang)
    return jsonify({"ok": True, "ziel": ziel, "umfang": umfang})


# ------------------------------------------------------------- Ausschalten

# Der Rechner steht kopflos im Schrank, es gibt keinen Bildschirm und
# niemanden, der auf den Knopf drueckt.  Also sagt HomeAssistant Bescheid,
# so wie es das beim Cast-Dongle schon tut.
#
# Heruntergefahren wird hier, ausgeschaltet aber nicht: `systemctl halt`
# beendet alle Dienste, leert die Puffer und haengt die Dateisysteme
# read-only ein - und laesst das Board danach unter Strom stehen.  Den Strom
# nimmt die Steckdose selbst weg: der Rechner bestellt sich beim
# Herunterfahren seinen eigenen Stromausfall (photoserver-steckdose.service
# ruft steckdose.sh auf, siehe dort).  HomeAssistant muss danach nichts mehr
# abpassen, es schaltet die Dose morgens nur wieder ein.
#
# Das ist kein Umweg, sondern der Weg zurueck.  Das BIOS kennt nur
# "After Power Loss: Power On", und dieses Merkmal setzt es nur, wenn der
# Strom im laufenden Betrieb verschwindet.  Nach einem echten `poweroff`
# stuende der Rechner in S5, der Stromausfall zaehlte nicht, und beim
# Wiedereinschalten der Dose bliebe er einfach aus.  Nach `halt` ist er aus
# Sicht des Boards an - die Dose wird damit zum Einschalter.
#
# Geschrieben wird zu diesem Zeitpunkt nichts mehr; das harte Wegnehmen des
# Stroms kostet keine Daten.
#
# Den Befehl gibt am Ende logind.  `systemctl halt` redet mit ihm nur
# ueber den D-Bus - kein sudo, kein setuid.  Das ist der Grund fuer diesen
# Weg: NoNewPrivileges=yes in der Unit bleibt damit unangetastet.  Erlaubt
# wird der Aufruf durch die polkit-Regel 50-fotoserver-ausschalten.rules,
# die `einrichten.sh` hinterlegt.
AUSSCHALT_AKTION = "org.freedesktop.login1.halt"
AUSSCHALT_FRIST = 2.0           # Sekunden zwischen Antwort und Halt
STECKDOSE = BASE / "steckdose.sh"

# Wer in den letzten so vielen Minuten Tastatur oder Maus angefasst hat,
# arbeitet noch - dann wird nicht angehalten.  Die angemeldete Sitzung
# allein sagt nichts: der Rechner meldet sich per Autologin selbst an, die
# Sitzung ist also immer da, auch wenn niemand davor sitzt.
ARBEIT_RUHE_MIN = 15


def steckdose_erreichbar():
    """Antwortet die Steckdose, die uns gleich den Strom nehmen soll?

    Bestellt wird hier nichts - das macht beim Herunterfahren
    photoserver-steckdose.service, damit der Weg fuer alle gleich ist
    (Desktop-Knopf, `systemctl halt`, diese Route).  Hier wird nur
    nachgesehen, ob die Dose ueberhaupt erreichbar ist.

    Der Grund fuer die Probe: geht die Bestellung spaeter im Funkloch
    verloren, haelt der Rechner an, der Strom bleibt stehen, das BIOS
    merkt sich keinen Stromausfall - und die Kiste kommt nie wieder hoch,
    ohne dass jemand in den Schrank greift.  Lieber ein ehrlicher Fehler
    an HomeAssistant als ein Rechner, den man nur noch von Hand weckt.

    Rueckgabe wie ausschalten_erlaubt(): (ja, grund).
    """
    code, text = steckdose_probe()
    if code == 0:
        return True, ""
    if code is None:
        return True, ""                     # kein Skript, alter Weg
    if code == 2:
        # Keine Adresse eingetragen.  Dann besorgt den Strom jemand anders
        # (frueher HomeAssistant) - das ist eine Entscheidung, keine Panne.
        return True, ""
    return False, text or "keine Antwort"


def steckdose_probe():
    """steckdose.sh pruefen aufrufen -> (rueckgabewert, Text).

    Geschaltet wird dabei nichts, gelesen nur /status.  None heisst: es
    gibt das Skript hier gar nicht.
    """
    if not STECKDOSE.exists():
        return None, "steckdose.sh ist nicht vorhanden."
    try:
        fertig = subprocess.run([str(STECKDOSE), "pruefen"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"steckdose.sh nicht ausfuehrbar: {exc}"
    return fertig.returncode, (fertig.stdout or fertig.stderr).strip()


@app.route("/api/steckdose")
def api_steckdose():
    """Was steht drin, und antwortet die Dose darauf?

    Nur Lesen - deshalb GET.  Ohne diese Probe merkte man eine falsch
    eingetippte Adresse erst beim naechsten Ausschalten, und dann ist der
    Rechner still und niemand mehr da, der es korrigieren koennte.
    """
    adresse = einstellungen_lesen()["steckdose"]
    if not adresse:
        return jsonify({"adresse": "", "erreichbar": False,
                        "meldung": "Keine Adresse eingetragen — den Strom "
                                   "muss dann jemand anders wegnehmen."})
    code, text = steckdose_probe()
    return jsonify({"adresse": adresse, "erreichbar": code == 0,
                    "meldung": text})


def ausschalten_erlaubt():
    """Darf dieser Prozess den Rechner anhalten?

    Fragt polkit, ohne etwas auszuloesen.  Ohne diese Probe bekaeme
    HomeAssistant ein froehliches OK und der Rechner liefe weiter - der
    Fehler faende sich nur im Journal.  Fehlt pkcheck, wird es einfach
    versucht; die Antwort ist dann nur weniger ehrlich.
    """
    if shutil.which("pkcheck") is None:
        return True, ""
    try:
        fertig = subprocess.run(
            ["pkcheck", "--action-id", AUSSCHALT_AKTION,
             "--process", str(os.getpid())],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True, ""
    if fertig.returncode == 0:
        return True, ""
    return False, (fertig.stderr or fertig.stdout).strip() or "polkit sagt nein"


def _sitzungen():
    """Angemeldete Benutzersitzungen als Liste von dicts (loginctl)."""
    try:
        liste = subprocess.run(["loginctl", "list-sessions", "--no-legend"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    ergebnis = []
    for zeile in liste.stdout.splitlines():
        teile = zeile.split()
        if not teile:
            continue
        try:
            fertig = subprocess.run(
                ["loginctl", "show-session", teile[0], "-p", "Name",
                 "-p", "User", "-p", "Class", "-p", "Type", "-p", "Remote",
                 "-p", "RemoteHost", "-p", "State", "-p", "TTY"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        werte = dict(z.split("=", 1) for z in fertig.stdout.splitlines()
                     if "=" in z)
        if werte.get("Class") == "user" and werte.get("State") != "closing":
            ergebnis.append(werte)
    return ergebnis


def _leerlauf_grafisch(uid):
    """Sekunden seit der letzten Eingabe am Desktop, None = unbekannt.

    Gefragt wird die Shell selbst (Mutter), nicht logind: dessen IdleHint
    setzt GNOME erst nach der Bildschirmsperr-Frist, und die steht hier auf
    "nie" - IdleHint waere damit fuer immer "nicht im Leerlauf".
    """
    umgebung = dict(os.environ,
                    DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{uid}/bus")
    try:
        fertig = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.gnome.Mutter.IdleMonitor",
             "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
             "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
            capture_output=True, text=True, timeout=10, env=umgebung)
    except (OSError, subprocess.SubprocessError):
        return None
    treffer = re.search(r"(\d+)", fertig.stdout)
    if fertig.returncode != 0 or not treffer:
        return None
    return int(treffer.group(1)) / 1000


def _leerlauf_terminal(tty):
    """Sekunden seit der letzten Eingabe am Terminal (wie `w`)."""
    if not tty:
        return None
    try:
        return max(0.0, time.time() - os.stat(f"/dev/{tty}").st_atime)
    except OSError:
        return None


def jemand_arbeitet():
    """Sitzt gerade jemand am Rechner?  -> Beschreibung oder None.

    Gezaehlt wird die letzte Eingabe, nicht die Anmeldung (siehe
    ARBEIT_RUHE_MIN).  Laesst sich der Leerlauf nicht bestimmen, wird
    nicht gesperrt, nur protokolliert: sonst bliebe der Rechner bei einer
    gestoerten Abfrage jeden Abend an, und niemand saehe warum.
    """
    grenze = ARBEIT_RUHE_MIN * 60
    for s in _sitzungen():
        wer = s.get("Name", "?")
        if s.get("Type") in ("wayland", "x11"):
            ruhe, wo = _leerlauf_grafisch(s.get("User", "")), "am Desktop"
        else:
            ruhe = _leerlauf_terminal(s.get("TTY"))
            wo = (f"per SSH von {s['RemoteHost']}" if s.get("RemoteHost")
                  else f"an {s.get('TTY') or 'einem Terminal'}")
        if ruhe is None:
            app.logger.warning("Leerlauf von %s %s nicht feststellbar.",
                               wer, wo)
            continue
        if ruhe < grenze:
            return f"{wer} arbeitet {wo} (letzte Eingabe vor " \
                   f"{int(ruhe // 60)} min)"
    return None


@app.route("/api/arbeit")
def api_arbeit():
    """Wuerde /api/ausschalten gerade wegen Arbeit am Rechner ablehnen?

    Nur Lesen.  Zum Nachsehen, ohne dass ein Fehlgriff den Rechner anhaelt.
    """
    arbeit = jemand_arbeitet()
    return jsonify({"arbeit": bool(arbeit), "meldung": arbeit or "",
                    "ruhe_min": ARBEIT_RUHE_MIN})


def _ausschalten_gleich():
    """Laeuft im eigenen Faden, kurz nachdem die Antwort raus ist."""
    try:
        fertig = subprocess.run(["systemctl", "halt"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.error("Ausschalten fehlgeschlagen: %s", exc)
        return
    if fertig.returncode != 0:
        app.logger.error("Ausschalten fehlgeschlagen: %s",
                         (fertig.stderr or fertig.stdout).strip())


@app.route("/api/ausschalten", methods=["POST"])
def api_ausschalten():
    """Den ganzen Rechner herunterfahren - gedacht fuer HomeAssistant.

    Faehrt bis zum Halt herunter, schaltet aber nicht ab; den Strom nimmt
    die Steckdose kurz darauf selbst weg (siehe oben).  Ein "ok" heisst
    also: der Rechner ist gleich still, und die Dose macht den Rest.

    Nur POST, anders als /api/cast/wecken: eine Adresse, die einen Rechner
    abschaltet, darf nicht durch einen Browser ausloesbar sein, der Links
    im Voraus laedt.

    Waehrend eines Imports wird abgelehnt - ein halb entpackter Takeout
    kostet beim naechsten Lauf Stunden.  Ebenso, solange jemand am Rechner
    arbeitet (jemand_arbeitet()).  Mit {"trotzdem": true} bzw.
    ?trotzdem=1 geht es dennoch.
    """
    daten = request.get_json(silent=True) or {}
    trotzdem = wahrheit(daten.get("trotzdem",
                                  request.args.get("trotzdem", "")))
    if IMPORT["aktiv"] and not trotzdem:
        return jsonify({"fehler": "Es läuft ein Import.",
                        "import": True}), 409

    # Kein Halt unter den Fingern weg: wer gerade arbeitet, verloere alles
    # Ungespeicherte - und die Kueche weiss davon nichts.
    arbeit = None if trotzdem else jemand_arbeitet()
    if arbeit:
        app.logger.info("Ausschalten abgelehnt: %s.", arbeit)
        return jsonify({"fehler": f"{arbeit}.",
                        "hinweis": "Mit ?trotzdem=1 geht es dennoch.",
                        "arbeit": True}), 409

    erlaubt, grund = ausschalten_erlaubt()
    if not erlaubt:
        return jsonify({
            "fehler": f"Nicht erlaubt: {grund}",
            "hinweis": "polkit-Regel fehlt - „Fotoserver einrichten“ starten.",
        }), 503

    # Erst nachsehen, ob die Steckdose antwortet.  Ohne sie wuerde der
    # Rechner zwar sauber anhalten, aber nie wieder von allein hochfahren.
    da, grund = steckdose_erreichbar()
    if not da:
        app.logger.error("Ausschalten abgelehnt, Steckdose: %s", grund)
        return jsonify({
            "fehler": f"Steckdose antwortet nicht: {grund}",
            "hinweis": "Ohne sie bliebe der Rechner nach dem Halt aus.",
            "steckdose": False,
        }), 503

    # Erst antworten, dann ausschalten.  Andersherum bricht die Verbindung
    # mitten im Satz ab, und in HomeAssistant steht ein Fehler, obwohl alles
    # geklappt hat.
    cast_schalten(False)        # der Fernseher soll sich sauber verabschieden
    app.logger.info("Ausschalten angefordert - in %.0f s ist Schluss.",
                    AUSSCHALT_FRIST)
    threading.Timer(AUSSCHALT_FRIST, _ausschalten_gleich).start()
    return jsonify({"ok": True, "in_sekunden": AUSSCHALT_FRIST})


@app.route("/favicon.ico")
def favicon():
    # Nicht stur auf /bild/1: die id 1 kann im Papierkorb liegen, ohne
    # Thumbnail sein oder nach einem Neuaufbau der Datenbank gar nicht mehr
    # existieren - dann holt sich der Browser bei jedem Laden eine 404.
    zeile = db().execute(
        f"SELECT id FROM photos p WHERE is_video = 0 AND thumb_state = 'ok'"
        f" AND {NICHT_GELOESCHT} LIMIT 1").fetchone()
    if not zeile:
        return "", 204
    return redirect(f"/bild/{zeile[0]}")


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
