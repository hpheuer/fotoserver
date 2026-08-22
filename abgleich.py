#!/usr/bin/env python3
"""
Takeout-Abgleich: was bei Google Fotos geloescht wurde, verschwindet hier.

Ein Takeout ist ein Schnappschuss, kein Delta - er sagt nur, was es noch
gibt.  Verglichen werden deshalb Mengen von SHA-256:

    weg  = alt - neu     hier verstecken (photos.geloescht_am)
    dazu = neu - alt     importieren, das macht import_photos.py

Gelesen wird aus den **Archiven**, nicht aus dem entpackten Baum.  Das ist
der Kern der Sache: entpacken() packt jeden Nachschub-Export in denselben
Baum ~/takeout-import.  Ein bei Google geloeschtes Bild liegt dort also
weiter aus dem vorigen Export herum, und ueber den Baum gerechnet waere
'weg' fuer immer leer.  Nur die Zip-Dateien eines Exports sagen, was
Google zu diesem Zeitpunkt wirklich noch hatte.

Daraus folgt die wichtigste Bedingung: **Es sollte ein Vollexport sein.**
Ein Monatsexport enthaelt einen Ausschnitt, und die Differenz dazu waere
die halbe Bibliothek.  Gefragt wird danach nicht - es wird gemessen
(Ueberdeckung, Anteil der Loeschungen, Verteilung ueber die Jahre).

Reissen die Schwellen, ist der Lauf damit aber nicht zu Ende: der Bericht
sagt, was er tun wuerde, und --trotzdem macht es nach getipptem LOESCHEN
doch.  Wer weiss, dass er gerade tausend Bilder bei Google aufgeraeumt
hat, hat recht und nicht die Schwelle.  Rueckgaengig macht es
--zuruecknehmen; angefasst wird ohnehin nur eine Spalte.

Aufruf:
    python3 abgleich.py                    Bericht ueber inbox/*.zip
    python3 abgleich.py --archive P [P...] bestimmte Archive oder Ordner
    python3 abgleich.py --anwenden         Bericht, dann Rueckfrage
    python3 abgleich.py --anwenden --ja    ohne Rueckfrage (Automatik)
    python3 abgleich.py --anwenden --trotzdem     Schwellen uebergehen
    python3 abgleich.py --zuruecknehmen    letzten Lauf ruecknahmen
    python3 abgleich.py --neu              frueheren Hash-Stand verwerfen

Der Lauf liest die Archive vollstaendig - bei einem Vollexport sind das
75 GB, also eine gute Viertelstunde.  Strg-C ist harmlos: fertig gelesene
Archive stehen in abgleich-stand.tsv, der naechste Aufruf ueberspringt sie.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from import_photos import (MEDIA_EXT, HASH_BLOCK, EXIFTOOL_CHUNK,
                           exif_batch, parse_exif_datetime)
from pruefsummen import log, menschlich, dauer

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "photos.db"
INBOX = BASE / "inbox"
STAND = BASE / "abgleich-stand.tsv"

# Ab hier gilt ein Export als vollstaendig genug, um daraus auf Loeschungen
# zu schliessen.  Ein Vollexport trifft ueber 99 % des Bestands, ein
# Monatsexport ein paar Prozent - dazwischen liegen Groessenordnungen,
# die Schwelle muss nicht fein sein.
UEBERDECKUNG_MIN = 0.95

# Und selbst dann: mehr als das hier nimmt kein normaler Lauf weg.
WEG_MAX_ANTEIL = 0.05

# Das getippte Wort fuer den uebergangenen Abbruch.  Dasselbe wie in
# sicherung.sh und bei "Platz schaffen" - wer es kennt, weiss, was es tut.
LOSUNG = "LOESCHEN"

# Soviele Dateien nimmt der Rueckfall hoechstens aus den Archiven.  Er ist
# fuer eine Restmenge gedacht; wird sie so gross, ist etwas anderes falsch.
RUECKFALL_MAX = 5000

JOBS = 3          # Archive gleichzeitig; jedes ist ein eigener Lesestrom
MELDE_ALLE = 5.0

# Teilnummer am Ende: takeout-20260806T103344Z-1-002.zip -> Teil 2
TEIL = re.compile(r"^(?P<kennung>.+)-(?P<nr>\d{3})\.zip$", re.IGNORECASE)


def sha256_von_strom(fh):
    h = hashlib.sha256()
    while chunk := fh.read(HASH_BLOCK):
        h.update(chunk)
    return h.hexdigest()


def nur_lesen(db_pfad):
    return sqlite3.connect(f"file:{db_pfad}?mode=ro", uri=True)


# ------------------------------------------------------------------ Archive

def archive_finden(orte):
    """Zip-Dateien einsammeln; ein Ordner meint alle Archive darin."""
    gefunden = []
    for ort in orte:
        p = Path(ort)
        if p.is_dir():
            gefunden.extend(sorted(x for x in p.glob("*.zip") if x.is_file()))
        elif p.is_file():
            gefunden.append(p)
        else:
            sys.exit(f"Nicht gefunden: {p}")
    return gefunden


def gruppieren(archive):
    """Nach Export-Kennung gruppieren -> {kennung: {nr: pfad}}."""
    gruppen = {}
    for p in archive:
        m = TEIL.match(p.name)
        if m:
            gruppen.setdefault(m["kennung"], {})[int(m["nr"])] = p
        else:
            gruppen.setdefault(p.stem, {})[1] = p
    return gruppen


def vollstaendig(gruppen):
    """Fehlende Teilnummern melden.  Ein Loch heisst: halber Export.

    Google nummeriert die Teile eines Exports lueckenlos durch.  Fehlt
    einer, fehlen dessen Bilder in 'neu' - und wuerden als geloescht
    gelten, obwohl sie nur nicht mitgeliefert wurden.
    """
    fehler = []
    for kennung, teile in sorted(gruppen.items()):
        hoechste = max(teile)
        luecken = [n for n in range(1, hoechste + 1) if n not in teile]
        if luecken:
            fehler.append(f"{kennung}: Teil "
                          f"{', '.join(f'{n:03d}' for n in luecken)}"
                          f" von {hoechste:03d} fehlt")
    return fehler


def medien(z):
    """Die Medieneintraege eines geoeffneten Archivs, ohne Sidecars."""
    return [i for i in z.infolist()
            if not i.is_dir()
            and os.path.splitext(i.filename)[1].lower() in MEDIA_EXT]


def eintraege_zaehlen(archiv):
    """(Zahl der Medien, Summe ihrer Groessen) - nur das Inhaltsverzeichnis."""
    with zipfile.ZipFile(archiv) as z:
        infos = medien(z)
    return len(infos), sum(i.file_size for i in infos)


class ArchivKaputt(Exception):
    """Ein Archiv liess sich nicht vollstaendig lesen."""


def hashes_aus_archiv(archiv, melden):
    """Jedes Medium im Archiv einmal lesen -> {hash: name}.

    Ein kaputter Eintrag darf nicht als Traceback enden und erst recht
    nicht uebersprungen werden: die Bilder, die dann fehlen, gaelten als
    bei Google geloescht.  Deshalb ArchivKaputt - der Aufrufer bricht den
    ganzen Lauf ab und sagt, welches Archiv neu geladen werden muss.
    """
    ergebnis = {}
    try:
        with zipfile.ZipFile(archiv) as z:
            for info in medien(z):
                try:
                    with z.open(info) as fh:
                        ergebnis.setdefault(sha256_von_strom(fh), info.filename)
                except (OSError, EOFError, RuntimeError,
                        zipfile.BadZipFile) as exc:
                    raise ArchivKaputt(
                        f"{archiv.name}: '{info.filename}' liess sich nicht "
                        f"lesen ({type(exc).__name__}: {exc})") from exc
                melden(info.file_size)
    except zipfile.BadZipFile as exc:
        raise ArchivKaputt(f"{archiv.name} ist kein lesbares Zip "
                           f"({exc})") from exc
    return ergebnis


# -------------------------------------------------------------------- Stand

def stand_lesen():
    """Frueher gelesene Archive -> {name: (groesse, {hash: eintrag})}.

    Nur Archive, hinter denen ein #fertig steht.  Was mitten im Lesen
    abgebrochen wurde, ist halb und wird noch einmal gelesen.
    """
    fertig, gross = {}, {}
    # Welche Archive wirklich zu Ende gelesen wurden.  Frueher wurde dafuer
    # geprueft, ob "#fertig\t<name>" irgendwo im Text vorkommt - eine
    # Zeichenkettensuche, die auch bei einem laengeren Namen anschlaegt
    # ("photos.zip" steckt in "photos.zip.teil2").  Ein halb gelesenes
    # Archiv gaelte dann als fertig, und seine ungelesenen Bilder zaehlten
    # als bei Google geloescht.
    abgeschlossen = set()
    try:
        text = STAND.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fertig
    aktuell = None
    for zeile in text.splitlines():
        teile = zeile.split("\t")
        if teile[0] == "#archiv" and len(teile) == 4:
            aktuell = teile[1]
            fertig.setdefault(aktuell, {})
            gross[aktuell] = int(teile[2])
        elif teile[0] == "#fertig" and len(teile) == 2:
            if teile[1] == aktuell:
                abgeschlossen.add(aktuell)
            aktuell = None
        elif aktuell and len(teile) == 2:
            fertig[aktuell][teile[0]] = teile[1]
    return {name: (gross.get(name, -1), hashes)
            for name, hashes in fertig.items()
            if name in abgeschlossen}


def stand_pruefen(fertig, archiv):
    """Gilt der gespeicherte Stand noch?

    Ein Archiv aendert sich nicht mehr - ein gleichnamiges mit anderer
    Groesse ist aber ein anderer Export, und dessen Hashes hier
    weiterzuverwenden waere der teuerste denkbare Fehler.
    """
    eintrag = fertig.get(archiv.name)
    return bool(eintrag) and eintrag[0] == archiv.stat().st_size


def sammeln(archive, von_vorn):
    """Alle Archive lesen -> {hash: (archivname, eintrag)}."""
    if von_vorn:
        STAND.unlink(missing_ok=True)
    fertig = stand_lesen()

    offen = [a for a in archive if not stand_pruefen(fertig, a)]
    if len(offen) < len(archive):
        log(f"  {len(archive) - len(offen)} Archive aus einem frueheren Lauf")

    zu_lesen, zahl = 0, 0
    for a in offen:
        n, groesse = eintraege_zaehlen(a)
        zu_lesen += groesse
        zahl += n
    log(f"Zu lesen: {len(offen)} Archive, {zahl} Medien, "
        f"{menschlich(zu_lesen)}")

    # Nur die Archive, nach denen gefragt wurde.  Das Journal sammelt
    # alles, was je gelesen wurde - auch frueher verarbeitete Exporte.
    # Naehme man die mit, stuende in 'neu' ein Bild, das der jetzige
    # Export gar nicht mehr enthaelt, und die Loeschung fiele unter den
    # Tisch.
    neu = {}
    for a in archive:
        eintrag = fertig.get(a.name)
        if eintrag and stand_pruefen(fertig, a):
            for h, name in eintrag[1].items():
                neu.setdefault(h, (a.name, name))

    gelesen, begonnen, zuletzt = 0, time.monotonic(), 0.0
    zaehl_sperre = threading.Lock()

    def melden(bytes_):
        """Fortschritt.  Wird aus mehreren Faeden gerufen, deshalb die
        Sperre - sonst gehen Zaehlungen verloren."""
        nonlocal gelesen, zuletzt
        with zaehl_sperre:
            gelesen += bytes_
            jetzt = time.monotonic() - begonnen
            if jetzt - zuletzt < MELDE_ALLE:
                return
            zuletzt, stand_jetzt = jetzt, gelesen
        anteil = stand_jetzt / max(zu_lesen, 1)
        rest = (jetzt / anteil - jetzt) if anteil else 0
        log(f"  {menschlich(stand_jetzt)} / {menschlich(zu_lesen)}"
            f"  ({anteil:5.1%})  "
            f"{menschlich(stand_jetzt / max(jetzt, 0.001))}/s  "
            f"noch ~{dauer(rest)}")

    pool = ThreadPoolExecutor(max_workers=JOBS)
    try:
        with open(STAND, "a", encoding="utf-8") as mit:
            for archiv, eintraege in zip(
                    offen, pool.map(lambda a: hashes_aus_archiv(a, melden),
                                    offen)):
                st = archiv.stat()
                mit.write(f"#archiv\t{archiv.name}\t{st.st_size}"
                          f"\t{st.st_mtime:.0f}\n")
                for h, name in eintraege.items():
                    mit.write(f"{h}\t{name}\n")
                mit.write(f"#fertig\t{archiv.name}\n")
                mit.flush()
                for h, name in eintraege.items():
                    neu.setdefault(h, (archiv.name, name))
                log(f"  {archiv.name}: {len(eintraege)} eindeutige Medien")
    except ArchivKaputt as exc:
        sys.exit(f"Abbruch: {exc}\n"
                 f"  Ein unvollstaendig gelesenes Archiv wuerde seine Bilder\n"
                 f"  als bei Google geloescht ausweisen.  Das Archiv neu\n"
                 f"  herunterladen und den Lauf wiederholen.")
    except KeyboardInterrupt:
        sys.exit(f"Abbruch.  Fertige Archive stehen in {STAND.name}, "
                 f"der naechste Aufruf ueberspringt sie.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return neu


# ------------------------------------------------------------------- Wachen

def import_laeuft():
    """Laeuft gerade ein Import?  Dann nicht abgleichen.

    Die Sperre im Viewer ist ein threading.Lock und damit von aussen
    unsichtbar - gefragt wird deshalb nach den Prozessen selbst.
    """
    for name in ("import_photos.py", "make_thumbs.py", "geocode.py"):
        try:
            res = subprocess.run(["pgrep", "-a", "-f", name],
                                 capture_output=True, text=True)
        except FileNotFoundError:
            return None
        for zeile in res.stdout.splitlines():
            teile = zeile.split()[1:]          # die PID vorne weg
            if not teile:
                continue
            # pgrep -f durchsucht die ganze Befehlszeile.  Ohne diese
            # Pruefung meldete auch ein offenes "vim import_photos.py"
            # einen laufenden Import - und der Abgleich liefe nie.
            aufruf = os.path.basename(teile[0])
            wenn_python = "python" in aufruf and any(
                os.path.basename(t) == name for t in teile[1:])
            if aufruf == name or wenn_python:
                return name
    return None


# ----------------------------------------------------- Rueckfall (Aufgabe 3)

def _eindeutig(paare):
    """[(schluessel, wert)] -> {schluessel: wert}, Mehrdeutiges faellt raus.

    Lieber keine Zuordnung als eine falsche - dieselbe Regel wie bei der
    Sidecar-Zuordnung im Import.  Zwei Bilder aus derselben Sekunde mit
    denselben Massen sind nicht unterscheidbar; wer sie trotzdem paart,
    versteckt am Ende das falsche Bild.
    """
    gesehen, einmal = set(), {}
    for schluessel, wert in paare:
        if schluessel in gesehen:
            einmal.pop(schluessel, None)
            continue
        gesehen.add(schluessel)
        einmal[schluessel] = wert
    return einmal


def _db_merkmale(db_pfad, hashes):
    """{hash: (datum, breite, hoehe)} fuer die uebrigen weg-Kandidaten."""
    conn = nur_lesen(db_pfad)
    try:
        conn.execute("CREATE TEMP TABLE frage (sha TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO frage VALUES (?)",
                         ((h,) for h in hashes))
        return {sha: (datum, breite, hoehe) for sha, datum, breite, hoehe in
                conn.execute(
                    "SELECT p.sha256, p.taken_at, p.width, p.height"
                    " FROM photos p JOIN frage f ON f.sha = p.sha256")}
    finally:
        conn.close()


def _export_merkmale(neu, hashes, archive_nach_name):
    """Dieselben Merkmale fuer die Restmenge aus dem Export.

    Dafuer muessen die Dateien kurz auf die Platte - exiftool liest keine
    Zip-Eintraege.  Es geht haeppchenweise und in einem Ordner, der
    hinterher wieder verschwindet; nur die Restmenge, nie die Bibliothek.
    """
    nach_archiv = defaultdict(list)
    for h in hashes:
        archivname, eintrag = neu[h]
        nach_archiv[archivname].append((h, eintrag))

    merkmale = {}
    ordner = Path(tempfile.mkdtemp(prefix="abgleich-", dir=BASE))
    try:
        for archivname, posten in nach_archiv.items():
            archiv = archive_nach_name.get(archivname)
            if not archiv:
                continue
            with zipfile.ZipFile(archiv) as z:
                for anfang in range(0, len(posten), EXIFTOOL_CHUNK):
                    haeppchen = posten[anfang:anfang + EXIFTOOL_CHUNK]
                    nach_datei = {}
                    for nr, (h, eintrag) in enumerate(haeppchen):
                        endung = os.path.splitext(eintrag)[1].lower()
                        ziel = ordner / f"{nr:05d}{endung}"
                        with z.open(eintrag) as quelle, open(ziel, "wb") as f:
                            shutil.copyfileobj(quelle, f)
                        nach_datei[str(ziel)] = h
                    for datei, tags in exif_batch(list(nach_datei)).items():
                        h = nach_datei.get(datei)
                        if not h:
                            continue
                        merkmale[h] = (
                            parse_exif_datetime(tags.get("DateTimeOriginal")),
                            tags.get("ImageWidth"), tags.get("ImageHeight"))
                    for datei in nach_datei:
                        os.unlink(datei)
    finally:
        shutil.rmtree(ordner, ignore_errors=True)
    return merkmale


def rueckfall(weg, dazu, neu, archive_nach_name, db_pfad):
    """Aufgabe 3: was per Hash nicht trifft, ueber EXIF paaren.

    Google liefert Originale bytegleich aus - aber nicht alles ist ein
    Original.  Eine nachtraeglich bearbeitete Fassung, eine HEIC, die als
    JPG herauskommt: derselbe Moment, ein anderer Hash.  Solche Bilder
    stuenden sonst gleichzeitig in 'weg' (die alte Fassung fehlt) und in
    'dazu' (die neue ist unbekannt) - und der Lauf versteckte ein Bild,
    das bei Google quicklebendig ist.

    Zwei Stufen, beide nur auf der Restmenge und beide nur bei
    Eindeutigkeit:
      1) Aufnahmezeitpunkt + Bildmasse
      2) nur der Aufnahmezeitpunkt (fuer Zuschnitte, die die Masse aendern)

    Ein pHash-Vergleich waere die dritte Stufe.  Er ist bewusst nicht
    gebaut: er braucht Pillow (also die venv), eine Toleranzschwelle zum
    Einstellen und wuerde die Restmenge zweimal lesen.  Solange Stufe 1
    und 2 die bekannten Faelle abdecken, ist das Aufwand ohne Ertrag.
    """
    if not weg or not dazu:
        return {}
    if len(dazu) > RUECKFALL_MAX:
        log(f"Rueckfall uebersprungen: {len(dazu)} unbekannte Medien sind"
            f" keine Restmenge mehr (Grenze {RUECKFALL_MAX}).")
        return {}

    log(f"Rueckfall: {len(weg)} weg-Kandidaten gegen {len(dazu)}"
        f" unbekannte Medien pruefen ...")
    db_seite = _db_merkmale(db_pfad, weg)
    export_seite = _export_merkmale(neu, dazu, archive_nach_name)

    gerettet = {}
    offen_db = dict(db_seite)
    offen_ex = dict(export_seite)

    for stufe, schluessel in (
            (1, lambda m: (m[0], m[1], m[2]) if all(x is not None
                                                    for x in m) else None),
            (2, lambda m: (m[0],) if m[0] else None)):
        links = _eindeutig((k, h) for h, m in offen_db.items()
                           if (k := schluessel(m)))
        rechts = _eindeutig((k, h) for h, m in offen_ex.items()
                            if (k := schluessel(m)))
        treffer = set(links) & set(rechts)
        for k in treffer:
            gerettet[links[k]] = (rechts[k], stufe)
            offen_db.pop(links[k], None)
            offen_ex.pop(rechts[k], None)
        if treffer:
            log(f"  Stufe {stufe}: {len(treffer)} Paare"
                f" ({'Zeitpunkt + Masse' if stufe == 1 else 'nur Zeitpunkt'})")
    if not gerettet:
        log("  keine Paare - die weg-Menge bleibt, wie sie ist")
    return gerettet


# ------------------------------------------------------------------ Abgleich

def datenbank_lesen(db_pfad):
    conn = nur_lesen(db_pfad)
    try:
        alt = {}
        markiert = set()
        for sha, weg in conn.execute("SELECT sha256, geloescht_am FROM photos"):
            alt[sha] = weg
            if weg:
                markiert.add(sha)
        papierkorb = {s for (s,) in conn.execute("SELECT sha256 FROM geloescht")}
    finally:
        conn.close()
    return alt, markiert, papierkorb


def jahre(db_pfad, hashes, grenze=8):
    """Wie verteilen sich die betroffenen Bilder ueber die Jahre?

    Ein echter Aufraeumvorgang bei Google trifft meist ein paar Alben aus
    einem Zeitraum.  Zieht sich die Liste gleichmaessig ueber 25 Jahre,
    stimmt eher etwas mit dem Export nicht.
    """
    if not hashes:
        return []
    conn = nur_lesen(db_pfad)
    try:
        conn.execute("CREATE TEMP TABLE frage (sha TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO frage VALUES (?)",
                         ((h,) for h in hashes))
        return conn.execute(
            "SELECT substr(COALESCE(p.hand_date, p.display_date,"
            " p.taken_at), 1, 4) jahr, COUNT(*) n"
            " FROM photos p JOIN frage f ON f.sha = p.sha256"
            " GROUP BY jahr ORDER BY n DESC LIMIT ?", (grenze,)).fetchall()
    finally:
        conn.close()


def vergleichen(alt, markiert, neu, gerettet):
    bestand = set(alt)
    getroffen = bestand & set(neu)
    weg = {h for h in bestand - set(neu) - set(gerettet) if not alt[h]}
    return {
        "bestand": bestand,
        "getroffen": getroffen,
        "gerettet": gerettet,
        "weg": weg,
        "zurueck": {h for h in markiert if h in neu or h in gerettet},
        "dazu": set(neu) - bestand - {e[0] for e in gerettet.values()},
        "markiert": markiert,
        # Wiedergefunden ist auch, was der Rueckfall gepaart hat - es
        # liegt bei Google, nur in anderer Fassung.
        "ueberdeckung": (len(getroffen) + len(gerettet))
                        / max(len(bestand), 1),
    }


def bericht(erg, papierkorb, db_pfad, neu_zahl):
    print()
    print("  Abgleich")
    print("  " + "-" * 68)
    print(f"  {neu_zahl:7}  eindeutige Medien im Export")
    print(f"  {len(erg['bestand']):7}  Bilder in der Datenbank")
    print(f"  {len(erg['getroffen']):7}  davon im Export wiedergefunden")
    if erg["gerettet"]:
        print(f"  {len(erg['gerettet']):7}  zusaetzlich per Rueckfall gepaart"
              f" (bearbeitet oder umgewandelt)")
    print(f"  {'':7}  Ueberdeckung insgesamt: {erg['ueberdeckung']:.1%}")
    print(f"  {len(erg['dazu']):7}  dazu    - noch nicht importiert")
    print(f"  {len(erg['weg']):7}  weg     - bei Google nicht mehr vorhanden")
    print(f"  {len(erg['zurueck']):7}  zurueck - wieder aufgetaucht")
    if erg["markiert"]:
        print(f"  {len(erg['markiert']):7}  bereits als weg markiert")
    ueberschneidung = erg["weg"] & papierkorb
    if ueberschneidung:
        print(f"  {len(ueberschneidung):7}  davon ohnehin schon im Papierkorb")

    verteilung = jahre(db_pfad, erg["weg"])
    if verteilung:
        print("\n  Die weg-Menge nach Jahren:")
        for jahr, n in verteilung:
            print(f"    {jahr or '????'}  {n:6}  {'#' * min(n // 20, 50)}")


def urteilen(erg):
    """-> (urteil, begruendung).  urteil: anwenden | nichts | abbruch."""
    anteil = len(erg["weg"]) / max(len(erg["bestand"]), 1)
    if erg["ueberdeckung"] < UEBERDECKUNG_MIN:
        return "abbruch", (
            f"nur {erg['ueberdeckung']:.1%} des Bestands stehen im Export"
            f" (noetig: {UEBERDECKUNG_MIN:.0%}).\n"
            "  Das sieht nach einem Teilexport aus - daraus laesst sich"
            " normalerweise nicht auf Loeschungen schliessen.")
    if anteil > WEG_MAX_ANTEIL:
        return "abbruch", (
            f"der Lauf wuerde {anteil:.1%} des Bestands wegnehmen"
            f" (Grenze: {WEG_MAX_ANTEIL:.0%}).\n"
            "  Entweder wurde bei Google sehr viel geloescht - dann stimmt"
            " es - oder der Export ist unvollstaendig.")
    if not erg["weg"] and not erg["zurueck"]:
        return "nichts", "der Export deckt sich mit dem Bestand."
    return "anwenden", ""


def bestaetigen(erg, urteil, begruendung, ohne_frage, trotzdem, losung):
    """Darf angewandt werden?  Bei uebergangener Schwelle mit Ansage.

    Die Schwelle ist ein Hinweis, kein Verbot: wer gerade tausend Bilder
    bei Google aufgeraeumt hat, weiss es besser als eine Zahl im Code.
    Deshalb gibt es --trotzdem - aber mit den Folgen im Klartext, dem
    getippten Wort und dem Weg zurueck daneben.
    """
    if urteil == "abbruch":
        print(f"\n  ABBRUCH: {begruendung}")
        if not trotzdem:
            print(f"\n  Wenn das trotzdem richtig ist:"
                  f"  --anwenden --trotzdem")
            print("  Rueckgaengig waere es mit --zuruecknehmen; angefasst"
                  " wird nur eine Spalte,")
            print("  keine Originaldatei und kein Thumbnail.")
            return False
        anteil = len(erg["weg"]) / max(len(erg["bestand"]), 1)
        print("\n  --trotzdem: die Schwelle wird uebergangen.")
        print(f"  Verstecken wuerde das {len(erg['weg'])} von"
              f" {len(erg['bestand'])} Bildern ({anteil:.1%}).")
        print("  Zurueckzunehmen mit:  python3 abgleich.py --zuruecknehmen")
        if losung == LOSUNG:
            return True
        if ohne_frage or not sys.stdin.isatty():
            print(f"  Ohne Rueckfrage geht das nur mit"
                  f" --bestaetigt {LOSUNG}.")
            return False
        return input(f"\n  Zum Bestaetigen {LOSUNG} tippen: ").strip() == LOSUNG

    if ohne_frage:
        return True
    if not sys.stdin.isatty():
        sys.exit("Keine Rueckfrage moeglich - mit --ja aufrufen.")
    return input("  Anwenden?  [j/N] ").strip().lower() in ("j", "ja")


def anwenden(db_pfad, weg, zurueck):
    # Der Zeitstempel ist die Kennung des Laufs, --zuruecknehmen greift
    # genau ihn heraus.  Auf die Sekunde genau waeren zwei Laeufe kurz
    # hintereinander derselbe Lauf - und das Zuruecknehmen des zweiten
    # raeumte den ersten gleich mit ab.  Deshalb mit Bruchteilen.
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    conn = sqlite3.connect(db_pfad, timeout=30)
    try:
        conn.executemany("UPDATE photos SET geloescht_am = ? WHERE sha256 = ?",
                         ((jetzt, h) for h in weg))
        conn.executemany("UPDATE photos SET geloescht_am = NULL"
                         " WHERE sha256 = ?", ((h,) for h in zurueck))
        conn.commit()
    finally:
        conn.close()
    log(f"{len(weg)} Bilder versteckt, {len(zurueck)} zurueckgeholt.")
    if weg:
        log(f"Rueckgaengig:  python3 abgleich.py --zuruecknehmen  "
            f"(Stempel {jetzt})")
    log("Die Slideshow laedt beim naechsten Stand-Abruf neu.")


def zuruecknehmen(db_pfad, was):
    """Einen Abgleichlauf ruecknahmen.

    Jeder Lauf schreibt denselben Zeitstempel in alle Zeilen, die er
    versteckt - der Stempel ist damit die Kennung des Laufs, und
    "letzter Lauf" ist einfach der groesste davon.
    """
    conn = sqlite3.connect(db_pfad, timeout=30)
    try:
        laeufe = conn.execute(
            "SELECT geloescht_am, COUNT(*) FROM photos"
            " WHERE geloescht_am IS NOT NULL"
            " GROUP BY geloescht_am ORDER BY geloescht_am DESC").fetchall()
        if not laeufe:
            log("Nichts zurueckzunehmen - keine Zeile ist markiert.")
            return 0
        print("  Markierte Laeufe:")
        for stempel, n in laeufe:
            print(f"    {stempel}   {n} Bilder")
        if was == "alles":
            cur = conn.execute("UPDATE photos SET geloescht_am = NULL"
                               " WHERE geloescht_am IS NOT NULL")
        else:
            stempel = was if was else laeufe[0][0]
            cur = conn.execute("UPDATE photos SET geloescht_am = NULL"
                               " WHERE geloescht_am = ?", (stempel,))
            if not cur.rowcount:
                sys.exit(f"Kein Lauf mit dem Stempel {stempel}.")
        conn.commit()
        log(f"{cur.rowcount} Bilder wieder sichtbar.")
    finally:
        conn.close()
    return 0


def allein():
    """Nur ein Abgleich auf einmal.

    Zwei Laeufe schrieben gleichzeitig in abgleich-stand.tsv - die Zeilen
    des einen landeten mitten im Archivblock des anderen, und beim naechsten
    Lesen gaelte ein halb gelesenes Archiv als fertig.  Die offene Datei
    bleibt bis zum Ende des Prozesses bestehen; das Betriebssystem gibt die
    Sperre dann von allein frei, auch nach einem Absturz.
    """
    griff = open(BASE / "abgleich.lock", "w")
    try:
        fcntl.flock(griff, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Es laeuft bereits ein Abgleich.")
    return griff


def lauf(orte, db_pfad, tun, ohne_frage, von_vorn, trotzdem, losung,
         ohne_rueckfall):
    if not db_pfad.exists():
        sys.exit(f"Datenbank nicht gefunden: {db_pfad}")
    laeuft = import_laeuft()
    if laeuft:
        sys.exit(f"Es laeuft gerade ein Import ({laeuft}) - erst abwarten.")
    sperre = allein()   # muss stehen bleiben: mit der Datei faellt die Sperre

    archive = archive_finden(orte)
    if not archive:
        sys.exit(f"Keine Archive gefunden in: {', '.join(str(o) for o in orte)}")

    gruppen = gruppieren(archive)
    log(f"{len(archive)} Archive, "
        f"{menschlich(sum(a.stat().st_size for a in archive))}, "
        f"{len(gruppen)} Export{'e' if len(gruppen) > 1 else ''}")
    for kennung, teile in sorted(gruppen.items()):
        log(f"  {kennung}: {len(teile)} Teil(e)")
    fehler = vollstaendig(gruppen)
    if fehler:
        for f in fehler:
            log(f"  FEHLT: {f}")
        sys.exit("Unvollstaendiger Export - kein Abgleich.")

    alt, markiert, papierkorb = datenbank_lesen(db_pfad)
    neu = sammeln(archive, von_vorn)

    roh = vergleichen(alt, markiert, neu, {})
    gerettet = {} if ohne_rueckfall else rueckfall(
        roh["weg"], roh["dazu"], neu, {a.name: a for a in archive}, db_pfad)
    erg = vergleichen(alt, markiert, neu, gerettet)

    bericht(erg, papierkorb, db_pfad, len(neu))
    urteil, begruendung = urteilen(erg)

    if not tun:
        if urteil == "abbruch":
            print(f"\n  ABBRUCH: {begruendung}")
            print("\n  Zum Anwenden trotz Schwelle:"
                  "  --anwenden --trotzdem")
            return 1
        if urteil == "nichts":
            print(f"\n  Nichts zu tun: {begruendung}")
            return 0
        print(f"\n  Anzuwenden: {len(erg['weg'])} verstecken,"
              f" {len(erg['zurueck'])} zurueckholen."
              f"  Originaldateien bleiben unangetastet.")
        print()
        log("Nur Bericht.  Zum Anwenden: --anwenden")
        return 0

    if urteil == "nichts":
        print(f"\n  Nichts zu tun: {begruendung}")
        return 0
    if urteil == "anwenden":
        print(f"\n  Anzuwenden: {len(erg['weg'])} verstecken,"
              f" {len(erg['zurueck'])} zurueckholen."
              f"  Originaldateien bleiben unangetastet.")
    if not bestaetigen(erg, urteil, begruendung, ohne_frage, trotzdem, losung):
        log("Abgebrochen, nichts geaendert.")
        return 1
    anwenden(db_pfad, erg["weg"], erg["zurueck"])
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Loeschungen aus Google Fotos uebernehmen")
    ap.add_argument("--archive", nargs="+", default=[INBOX],
                    help=f"Archive oder Ordner (Vorgabe: {INBOX})")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--anwenden", action="store_true",
                    help="nach Rueckfrage wirklich markieren")
    ap.add_argument("--ja", action="store_true",
                    help="ohne Rueckfrage (nur mit --anwenden)")
    ap.add_argument("--trotzdem", action="store_true",
                    help="Schwellen uebergehen, mit getipptem "
                         f"{LOSUNG}")
    ap.add_argument("--bestaetigt", default="",
                    help=f"'{LOSUNG}' - fuer --trotzdem ohne Rueckfrage")
    ap.add_argument("--ohne-rueckfall", action="store_true",
                    help="nur ueber SHA-256 vergleichen, kein EXIF-Rueckfall")
    ap.add_argument("--neu", action="store_true",
                    help="frueher gelesene Archive noch einmal lesen")
    ap.add_argument("--zuruecknehmen", nargs="?", const="", metavar="STEMPEL",
                    help="letzten Lauf zuruecknehmen ('alles' fuer alle)")
    args = ap.parse_args()

    if args.zuruecknehmen is not None:
        sys.exit(zuruecknehmen(args.db, args.zuruecknehmen))
    sys.exit(lauf(args.archive, args.db, args.anwenden, args.ja, args.neu,
                  args.trotzdem, args.bestaetigt, args.ohne_rueckfall))


if __name__ == "__main__":
    main()
