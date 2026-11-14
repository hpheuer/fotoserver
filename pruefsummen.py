#!/usr/bin/env python3
"""
Pruefsummen der Bibliothek gegen die Datenbank halten.

Der Takeout-Abgleich - was bei Google geloescht wurde, verschwindet auch
hier - vergleicht Mengen von SHA-256-Hashes, nicht Dateinamen.  Namen
taugen dafuer nicht: Takeout kuerzt sie, haengt bei Kollisionen (1) an
und legt dasselbe Bild in jedem Albumordner erneut ab.  Der Hash ist der
einzige stabile Schluessel, und dieses Skript ist die Vorstufe zum
Abgleich: es laeuft ueber die Bibliothek und prueft, ob zu jeder Datei
ein Hash in der Datenbank steht und ob er noch stimmt.

Nachzutragen gibt es dabei nichts.  photos.sha256 ist NOT NULL UNIQUE,
files.sha256 ist NOT NULL - eine Zeile ohne Hash kann es gar nicht
geben.  Der Backfill aus der Spec ist deshalb ein Pruef- und kein
Schreiblauf.

Und geschrieben wird auch bewusst nicht:  import_photos.py haelt eine
Datei fuer unveraendert, sobald Pfad, Groesse und mtime zu ihrer Zeile in
`files` passen.  Wer hier den Hash einer noch nicht importierten Datei
eintruege, machte sie fuer den Import unsichtbar - sie bekaeme nie eine
Zeile in `photos`, nie ein Thumbnail, und im Abgleich zaehlte sie
trotzdem mit.  Die Datenbank fuellt allein import_photos.py.

Aufruf:
    python3 pruefsummen.py              # alles hashen und vergleichen
    python3 pruefsummen.py --limit 500  # Testlauf
    python3 pruefsummen.py --neu        # von vorn, ohne Wiederaufnahme
    python3 pruefsummen.py --stand      # nur nachsehen, wie weit es ist

Der Lauf dauert.  Es sind rund 75 GB; mit vier Faeden liest die SSD sie
in etwa einer Viertelstunde, eine USB-Platte braucht laenger.  Ein
Abbruch mit Strg-C ist harmlos: der Stand steht in pruefsummen-stand.tsv,
der naechste Aufruf macht dort weiter.  Schon geprueft gilt eine Datei
nur, solange Groesse und mtime unveraendert sind.
"""

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# Dieselbe Vorstellung davon, was ein Medium ist, wie beim Import - sonst
# prueft man etwas anderes, als importiert wurde.
from import_photos import MEDIA_EXT, sha256_of

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "photos.db"
QUELLE = Path.home() / "takeout-import"
JOURNAL = BASE / "pruefsummen-stand.tsv"

JOBS = 4              # Faeden zum Lesen; mehr bringt an einer Platte nichts
MELDE_ALLE = 5.0      # Sekunden zwischen zwei Fortschrittszeilen
BLOCK = 64            # Dateien je Runde, damit Strg-C sofort greift

# Was zu einer Datei herauskommen kann.  Die Reihenfolge ist die der
# Zusammenfassung am Ende.
BEFUNDE = {
    "ok":         "Hash stimmt mit der Datenbank ueberein",
    "unbekannt":  "liegt da, steht aber in keiner Zeile (noch nicht importiert)",
    "abweichung": "Datei geaendert oder Hash veraltet - vor dem Abgleich klaeren",
    "ohne_bild":  "Hash bekannt, aber kein Eintrag in photos (Duplikat?)",
    "fehler":     "nicht lesbar",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def menschlich(bytes_):
    for einheit in ("B", "KB", "MB", "GB", "TB"):
        if bytes_ < 1024 or einheit == "TB":
            return f"{bytes_:.1f} {einheit}" if einheit != "B" else f"{bytes_} B"
        bytes_ /= 1024


def dauer(sekunden):
    sekunden = int(max(sekunden, 0))
    if sekunden < 90:
        return f"{sekunden} s"
    if sekunden < 5400:
        return f"{sekunden // 60} min"
    return f"{sekunden / 3600:.1f} h"


# ------------------------------------------------------------------ Journal

def journal_lesen(pfad):
    """Was frueher schon geprueft wurde -> {pfad: (befund, hash)}.

    Eine Zeile gilt nur weiter, wenn Groesse und mtime der Datei
    unveraendert sind.  Sonst ist sie seither angefasst worden und muss
    neu gelesen werden.
    """
    stand = {}
    try:
        text = pfad.read_text(encoding="utf-8")
    except FileNotFoundError:
        return stand
    for zeile in text.splitlines():
        teile = zeile.split("\t")
        if len(teile) != 5:
            continue                      # halbe Zeile vom harten Abbruch
        befund, groesse, mtime, hash_, datei = teile
        try:
            stand[datei] = (befund, hash_, int(groesse), float(mtime))
        except ValueError:
            continue
    return stand


def journal_zeile(befund, groesse, mtime, hash_, datei):
    return f"{befund}\t{groesse}\t{mtime:.6f}\t{hash_}\t{datei}\n"


def journal_verdichten(pfad, gueltig, quelle):
    """Eine Zeile je Datei, nach einem vollstaendigen Lauf.

    Geschrieben wird waehrend des Laufs immer angehaengt - anders liesse
    sich ein Abbruch nicht ueberleben.  Wer aber dieselbe Datei zweimal
    prueft (weil sie sich geaendert hat), bekommt zwei Zeilen, und nach
    einigen Laeufen steht das Vielfache des Noetigen in der Datei.  Beim
    Lesen gewinnt ohnehin die letzte Zeile je Pfad; genau die bleibt hier
    stehen.

    Weggeworfen wird nur innerhalb der gerade geprueften Bibliothek: dort
    heisst "steht im Journal, aber nicht in der Dateiliste" wirklich, dass
    es die Datei nicht mehr gibt.  Zeilen von ausserhalb bleiben unberuehrt
    - das Journal liegt fest im Projektordner, die Bibliothek kommt aber
    aus --quelle, und ein Probelauf gegen ein anderes Verzeichnis darf
    nicht den Stand der echten Bibliothek loeschen.

    Erst in eine Nebendatei, dann umbenennen: bricht es mittendrin ab,
    bleibt der alte Stand heil.
    """
    stand = journal_lesen(pfad)
    wurzel = os.path.abspath(quelle) + os.sep
    ziel = pfad.with_suffix(pfad.suffix + ".neu")
    vorher = sum(1 for _ in pfad.open(encoding="utf-8")) if pfad.exists() else 0
    with ziel.open("w", encoding="utf-8") as f:
        for datei, (befund, hash_, groesse, mtime) in stand.items():
            if datei in gueltig or not os.path.abspath(datei).startswith(wurzel):
                f.write(journal_zeile(befund, groesse, mtime, hash_, datei))
    os.replace(ziel, pfad)
    nachher = sum(1 for _ in pfad.open(encoding="utf-8"))
    if nachher != vorher:
        log(f"{pfad.name} verdichtet: {vorher} -> {nachher} Zeilen")


# ------------------------------------------------------------------ Bibliothek

def sammeln(quelle):
    """Mediendateien unter quelle -> ([(pfad, groesse, mtime)], [(pfad, grund)]).

    Was sich nicht einsammeln laesst, wird mitgezaehlt und nicht
    verschluckt.  os.walk meldet ein unlesbares Verzeichnis nur, wenn man
    danach fragt, und eine Datei kann zwischen Auflisten und Nachsehen
    verschwinden.  Beides muss im Befund stehen, sonst hiesse es am Ende
    "Alle Hashes stimmen", obwohl ein Teil nie angesehen wurde.
    """
    gefunden, unlesbar = [], []

    def gestolpert(exc):
        unlesbar.append((getattr(exc, "filename", None) or str(quelle), exc))

    for root, dirs, namen in os.walk(quelle, onerror=gestolpert):
        dirs.sort()
        for n in sorted(namen):
            if os.path.splitext(n)[1].lower() not in MEDIA_EXT:
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError as exc:
                unlesbar.append((p, exc))
                continue
            gefunden.append((p, st.st_size, st.st_mtime))
    return gefunden, unlesbar


def datenbank_lesen(db_pfad):
    """(hash je Pfad aus files, alle Hashes aus photos)."""
    conn = sqlite3.connect(f"file:{db_pfad}?mode=ro", uri=True)
    try:
        nach_pfad = {p: h for p, h in
                     conn.execute("SELECT path, sha256 FROM files")}
        in_photos = {h for (h,) in conn.execute("SELECT sha256 FROM photos")}
    finally:
        conn.close()
    return nach_pfad, in_photos


def beurteilen(pfad, hash_, nach_pfad, in_photos):
    bekannt = nach_pfad.get(pfad)
    if bekannt is None:
        return "unbekannt"
    if bekannt != hash_:
        return "abweichung"
    if hash_ not in in_photos:
        return "ohne_bild"
    return "ok"


# ------------------------------------------------------------------ Pruefen

def pruefen(quelle, db_pfad, limit=None, jobs=JOBS, von_vorn=False):
    if not quelle.is_dir():
        sys.exit(f"Bibliothek nicht gefunden: {quelle}")
    if not db_pfad.exists():
        sys.exit(f"Datenbank nicht gefunden: {db_pfad}")

    if von_vorn:
        JOURNAL.unlink(missing_ok=True)

    log(f"Datenbank: {db_pfad}")
    nach_pfad, in_photos = datenbank_lesen(db_pfad)
    log(f"  {len(nach_pfad)} Dateien und {len(in_photos)} Bilder eingetragen")

    log(f"Sammle Dateien unter {quelle} ...")
    dateien, unlesbar = sammeln(quelle)
    log(f"  {len(dateien)} Mediendateien, "
        f"{menschlich(sum(g for _, g, _ in dateien))}")

    stand = journal_lesen(JOURNAL)
    zaehler = {name: 0 for name in BEFUNDE}
    auffaellig = {name: [] for name in BEFUNDE if name != "ok"}
    offen, uebersprungen = [], 0

    # Was gar nicht erst einzusammeln war, zaehlt als Fehler.  Sonst
    # stuende am Ende "Alle Hashes stimmen", obwohl niemand hingesehen hat.
    for pfad, grund in unlesbar:
        log(f"  nicht zugaenglich: {pfad} ({grund})")
        zaehler["fehler"] += 1
        auffaellig["fehler"].append(pfad)

    for pfad, groesse, mtime in dateien:
        frueher = stand.get(pfad)
        # mtime nur auf Sekunden vergleichen: manche Dateisysteme geben
        # die Nachkommastellen nicht wieder her.
        if (frueher and frueher[2] == groesse
                and int(frueher[3]) == int(mtime)):
            befund = frueher[0]
            zaehler[befund] = zaehler.get(befund, 0) + 1
            if befund in auffaellig:
                auffaellig[befund].append(pfad)
            uebersprungen += 1
            continue
        offen.append((pfad, groesse, mtime))

    if uebersprungen:
        log(f"  {uebersprungen} davon aus einem frueheren Lauf uebernommen")
    if limit:
        offen = offen[:limit]
        log(f"  Testlauf: nur {len(offen)} Dateien")

    zu_lesen = sum(g for _, g, _ in offen)
    log(f"Zu pruefen: {len(offen)} Dateien, {menschlich(zu_lesen)}")

    gelesen, fertig, begonnen, zuletzt = 0, 0, time.monotonic(), 0.0
    unterbrochen = False

    with open(JOURNAL, "a", encoding="utf-8") as mit:
        def eine(auftrag):
            pfad, groesse, mtime = auftrag
            try:
                return auftrag, sha256_of(pfad), None
            except OSError as exc:
                return auftrag, None, exc

        # Blockweise statt in einem Rutsch: pool.map wuerde alle Auftraege
        # sofort in die Warteschlange legen, und beim Beenden arbeitet der
        # Pool sie ab, ehe er zurueckkommt - Strg-C haette dann erst nach
        # der letzten Datei Wirkung.  Ein Block ist nach Sekunden durch.
        pool = ThreadPoolExecutor(max_workers=jobs)
        try:
            for anfang in range(0, len(offen), BLOCK):
                for auftrag, hash_, fehler in pool.map(
                        eine, offen[anfang:anfang + BLOCK]):
                    pfad, groesse, mtime = auftrag
                    if fehler is not None:
                        befund, hash_ = "fehler", ""
                        log(f"  nicht lesbar: {pfad} ({fehler})")
                    else:
                        befund = beurteilen(pfad, hash_, nach_pfad, in_photos)
                    zaehler[befund] = zaehler.get(befund, 0) + 1
                    if befund in auffaellig:
                        auffaellig[befund].append(pfad)
                    mit.write(journal_zeile(befund, groesse, mtime, hash_, pfad))

                    fertig += 1
                    gelesen += groesse
                    jetzt = time.monotonic() - begonnen
                    if jetzt - zuletzt >= MELDE_ALLE or fertig == len(offen):
                        zuletzt = jetzt
                        mit.flush()
                        anteil = fertig / max(len(offen), 1)
                        rest = (jetzt / anteil - jetzt) if anteil else 0
                        log(f"  {fertig}/{len(offen)}  ({anteil:5.1%})  "
                            f"{menschlich(gelesen / max(jetzt, 0.001))}/s  "
                            f"noch ~{dauer(rest)}")
        except KeyboardInterrupt:
            unterbrochen = True
            print()
            log(f"Abbruch nach {fertig} Dateien.  Der Stand steht in "
                f"{JOURNAL.name}, der naechste Aufruf macht dort weiter.")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    if not unterbrochen and not limit:
        journal_verdichten(JOURNAL, {p for p, _, _ in dateien}, quelle)

    if not unterbrochen:
        verschwunden = [p for p in nach_pfad
                        if not os.path.exists(p)] if not limit else []
        bericht(zaehler, auffaellig, verschwunden, limit)
    return 1 if (unterbrochen or zaehler["abweichung"] or zaehler["fehler"]) else 0


def bericht(zaehler, auffaellig, verschwunden, limit):
    print()
    print("  Ergebnis")
    print("  " + "-" * 68)
    gesamt = sum(zaehler.values())
    for name, text in BEFUNDE.items():
        n = zaehler.get(name, 0)
        if n or name == "ok":
            print(f"  {n:7}  {name:11} {text}")
    print(f"  {gesamt:7}  gepruefte Dateien")

    for name in ("abweichung", "fehler", "unbekannt", "ohne_bild"):
        pfade = auffaellig.get(name) or []
        if not pfade:
            continue
        print(f"\n  {name} ({len(pfade)}):")
        for p in pfade[:20]:
            print(f"    {p}")
        if len(pfade) > 20:
            print(f"    ... und {len(pfade) - 20} weitere")

    if verschwunden:
        # Genau diese Menge macht spaeter den Abgleich aus: eingetragen,
        # aber nicht mehr auf der Platte.  Hier wird sie nur gezaehlt -
        # geloescht wird nichts, das ist Aufgabe 2.
        print(f"\n  {len(verschwunden)} eingetragene Dateien liegen nicht mehr"
              f" in der Bibliothek.")
        for p in verschwunden[:10]:
            print(f"    {p}")
        if len(verschwunden) > 10:
            print(f"    ... und {len(verschwunden) - 10} weitere")

    print()
    if limit:
        log("Testlauf beendet.")
    elif zaehler.get("abweichung") or zaehler.get("fehler"):
        log("Es gibt Abweichungen - vor dem Takeout-Abgleich klaeren.")
    else:
        log("Alle Hashes stimmen.  Die Datenbank taugt als Vergleichsmenge.")


def stand_zeigen():
    stand = journal_lesen(JOURNAL)
    if not stand:
        sys.exit(f"Kein Stand vorhanden ({JOURNAL.name} fehlt oder ist leer).")
    zaehler = {}
    for befund, _, _, _ in stand.values():
        zaehler[befund] = zaehler.get(befund, 0) + 1
    print(f"  {JOURNAL}")
    print(f"  {len(stand)} Dateien geprueft, zuletzt "
          f"{datetime.fromtimestamp(JOURNAL.stat().st_mtime):%d.%m.%Y %H:%M}")
    for name, text in BEFUNDE.items():
        if zaehler.get(name):
            print(f"  {zaehler[name]:7}  {name:11} {text}")


def main():
    ap = argparse.ArgumentParser(
        description="SHA-256 der Bibliothek gegen die Datenbank pruefen")
    ap.add_argument("--quelle", type=Path, default=QUELLE,
                    help=f"Bibliothek (Vorgabe: {QUELLE})")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--limit", type=int, help="nur N Dateien (Testlauf)")
    ap.add_argument("--jobs", type=int, default=JOBS,
                    help=f"Faeden zum Lesen (Vorgabe: {JOBS})")
    ap.add_argument("--neu", action="store_true",
                    help="von vorn, frueheren Stand verwerfen")
    ap.add_argument("--stand", action="store_true",
                    help="nur zeigen, wie weit der letzte Lauf kam")
    args = ap.parse_args()

    if args.stand:
        stand_zeigen()
        return
    sys.exit(pruefen(args.quelle, args.db, args.limit, args.jobs, args.neu))


if __name__ == "__main__":
    main()
