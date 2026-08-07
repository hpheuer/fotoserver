#!/usr/bin/env python3
"""
Reverse-Geocoding fuer den Fotoserver - offline, ohne fremden Dienst.

Ordnet jedem Foto mit Koordinaten den naechstgelegenen Ort zu und schreibt
das Ergebnis in die Datenbank (Spalten place, place_country, place_km).
Die Originaldateien werden nicht angefasst.

Datengrundlage: GeoNames (cities1000, CC BY 4.0).  Einmal aufbereitet zu
geo/orte.tsv, danach laeuft alles ohne Netz.

Ablauf:
    1) python3 geocode.py --aufbereiten     aus den Rohdaten orte.tsv bauen
    2) python3 geocode.py --gemeinden       deutsche Gemeinden ergaenzen
    3) python3 geocode.py                   Fotos zuordnen
    4) python3 geocode.py --stats           Ergebnis ansehen

Nach Schritt 1 und 2 koennen cities1000.*, alternateNamesV2.* und DE.txt
geloescht werden; gebraucht werden nur noch geo/orte.tsv und
geo/orte-ergaenzung.tsv (wenige MB).
"""

import argparse
import math
import sqlite3
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "photos.db"
GEO = BASE / "geo"
ORTE = GEO / "orte.tsv"
# Nachtrag von Hand bzw. aus --gemeinden.  Steht in einer eigenen Datei,
# damit ein erneutes --aufbereiten sie nicht ueberschreibt.
ORTE_EXTRA = GEO / "orte-ergaenzung.tsv"
ORTE_DATEIEN = (ORTE, ORTE_EXTRA)

# Ab dieser Entfernung gilt ein Ort nicht mehr als "dort aufgenommen".
# Der Viewer entscheidet ueber die Formulierung, hier wird nur begrenzt.
MAX_KM = 300.0

# Liegen mehrere Orte fast gleich weit weg, gewinnt der groessere - sonst
# heisst Stuttgart schnell "Gablenberg".  Wie weit "fast gleich weit" reicht,
# haengt aber daran, wie nah der naechste Ort ueberhaupt ist: steht man
# mitten in einem 25.000-Einwohner-Ort, darf ihn die Grossstadt 7 km weiter
# nicht mehr schlucken.  Deshalb anteilig, gedeckelt durch UNSCHAERFE_KM.
#
# Genau dieser Deckel sorgte dafuer, dass Vororte jahrelang den Namen der
# Grossstadt nebenan trugen.  Wer anders gewichten will, dreht am Anteil:
# groesser heisst "lieber der bekanntere Ort", kleiner "lieber der naehere".
UNSCHAERFE_KM = 6.0
UNSCHAERFE_ANTEIL = 0.25

# Suchring in Gradzellen; 3 Grad Breite sind rund 330 km.
MAX_RING = 3


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------- Rohdaten aufbereiten

def deutsche_namen(ids_gesucht):
    """geonameid -> deutscher Name, aus alternateNamesV2.txt gestreamt.

    Vorrang: bevorzugt und kurz, dann bevorzugt, dann kurz, dann der erste
    Treffer.  Die Kurzform zuerst zu pruefen macht aus der "Bundesrepublik
    Deutschland" wieder "Deutschland".
    Umgangssprachliches und Historisches wird uebergangen.
    """
    quelle = GEO / "alternateNamesV2.txt"
    if not quelle.exists():
        log(f"  {quelle.name} fehlt - Ortsnamen bleiben in der Originalsprache.")
        return {}

    treffer = {}
    rang = {}
    with quelle.open(encoding="utf-8") as f:
        for zeile in f:
            teile = zeile.rstrip("\n").split("\t")
            if len(teile) < 4 or teile[2] != "de":
                continue
            try:
                gid = int(teile[1])
            except ValueError:
                continue
            if gid not in ids_gesucht:
                continue
            colloquial = len(teile) > 6 and teile[6] == "1"
            historisch = len(teile) > 7 and teile[7] == "1"
            if colloquial or historisch:
                continue
            bevorzugt = len(teile) > 4 and teile[4] == "1"
            kurz = len(teile) > 5 and teile[5] == "1"
            if bevorzugt and kurz:
                r = 0
            elif bevorzugt:
                r = 1
            elif kurz:
                r = 2
            else:
                r = 3
            if gid not in rang or r < rang[gid]:
                rang[gid] = r
                treffer[gid] = teile[3]
    return treffer


def aufbereiten():
    staedte_txt = GEO / "cities1000.txt"
    laender_txt = GEO / "countryInfo.txt"
    admin1_txt = GEO / "admin1CodesASCII.txt"
    if not staedte_txt.exists():
        zip_datei = GEO / "cities1000.zip"
        if zip_datei.exists():
            with zipfile.ZipFile(zip_datei) as z:
                z.extract("cities1000.txt", GEO)
        else:
            sys.exit(f"Fehlt: {staedte_txt}\n"
                     "  curl -o geo/cities1000.zip "
                     "https://download.geonames.org/export/dump/cities1000.zip")
    if not laender_txt.exists():
        sys.exit(f"Fehlt: {laender_txt}")

    # Staedte lesen.  admin1 (Bundesland/Region) wird mitgenommen, damit
    # sich gleichnamige Orte spaeter unterscheiden lassen - "Oregon" gibt
    # es in Wisconsin, Ohio und Illinois.
    staedte = []
    ids = set()
    with staedte_txt.open(encoding="utf-8") as f:
        for zeile in f:
            t = zeile.rstrip("\n").split("\t")
            if len(t) < 15:
                continue
            try:
                gid = int(t[0])
                lat, lon = float(t[4]), float(t[5])
                pop = int(t[14] or 0)
            except ValueError:
                continue
            staedte.append([gid, t[1], lat, lon, t[8], pop,
                            f"{t[8]}.{t[10]}" if t[10] else ""])
            ids.add(gid)
    log(f"  {len(staedte)} Orte gelesen")

    # Regionen lesen (Schluessel "DE.01", geonameid in Spalte 4)
    regionen = {}
    if admin1_txt.exists():
        with admin1_txt.open(encoding="utf-8") as f:
            for zeile in f:
                t = zeile.rstrip("\n").split("\t")
                if len(t) < 4:
                    continue
                try:
                    gid = int(t[3])
                except ValueError:
                    continue
                regionen[t[0]] = [gid, t[1]]
                ids.add(gid)
        log(f"  {len(regionen)} Regionen gelesen")
    else:
        log(f"  {admin1_txt.name} fehlt - ohne Regionsangabe")

    # Laender lesen (geonameid steht in Spalte 17)
    laender = {}
    with laender_txt.open(encoding="utf-8") as f:
        for zeile in f:
            if zeile.startswith("#"):
                continue
            t = zeile.rstrip("\n").split("\t")
            if len(t) < 17 or not t[0]:
                continue
            try:
                gid = int(t[16])
            except ValueError:
                continue
            laender[t[0]] = [gid, t[4]]        # cc -> [geonameid, engl. Name]
            ids.add(gid)
    log(f"  {len(laender)} Laender gelesen")

    log("  deutsche Namen suchen (das dauert ein bis zwei Minuten) ...")
    de = deutsche_namen(ids)
    log(f"  {len(de)} deutsche Namen gefunden")

    for cc, eintrag in laender.items():
        eintrag[1] = de.get(eintrag[0], eintrag[1])
    for schluessel, eintrag in regionen.items():
        eintrag[1] = de.get(eintrag[0], eintrag[1])

    GEO.mkdir(exist_ok=True)
    with ORTE.open("w", encoding="utf-8") as f:
        f.write("# name\tlat\tlon\tcc\tland\tpop\tregion\n")
        for gid, name, lat, lon, cc, pop, a1 in staedte:
            name_de = de.get(gid, name)
            land = laender.get(cc, [0, cc])[1]
            region = regionen.get(a1, [0, ""])[1] if a1 else ""
            f.write(f"{name_de}\t{lat:.5f}\t{lon:.5f}\t{cc}\t{land}\t{pop}"
                    f"\t{region}\n")

    log(f"  geschrieben: {ORTE}  ({ORTE.stat().st_size/1024**2:.1f} MB)")
    log("  cities1000.* und alternateNamesV2.* werden jetzt nicht mehr "
        "gebraucht.")


# Verwaltungszusaetze aus den Gemeindenamen: unter einem Foto steht
# "Garmisch-Partenkirchen", nicht "Garmisch-Partenkirchen, Markt".
def _gemeindename(roh):
    return roh.split(",")[0].strip()


def gemeinden():
    """Deutsche Gemeinden aus DE.txt nachtragen.

    cities1000 kennt nur Feature-Klasse P (bewohnte Orte).  Viele deutsche
    Gemeinden fuehrt GeoNames aber als ADM4, und ihre Ortsteile stehen mit
    Einwohnerzahl 0 in den Rohdaten - beides faellt aus cities1000 heraus.
    So fehlen Gemeinden mit zwanzig- bis dreissigtausend Einwohnern
    vollstaendig, und ihre Fotos tragen den Namen der naechsten Grossstadt.

    Uebernommen wird nur, was wirklich fehlt: liegt schon ein aehnlich
    grosser Ort in Sichtweite, ist es dieselbe Gemeinde unter einem anderen
    Namen ("Rotenburg (Wümme)" neben "Rotenburg an der Wümme").
    """
    quelle = GEO / "DE.txt"
    if not quelle.exists():
        sys.exit(f"Fehlt: {quelle}\n"
                 "  curl -o geo/DE.zip https://download.geonames.org"
                 "/export/dump/DE.zip && unzip -o geo/DE.zip DE.txt -d geo/")
    if not ORTE.exists():
        sys.exit(f"Fehlt: {ORTE}\n  Erst 'python3 geocode.py --aufbereiten'")

    kandidaten = []
    with quelle.open(encoding="utf-8") as f:
        for zeile in f:
            t = zeile.rstrip("\n").split("\t")
            if len(t) < 15 or t[6] != "A" or t[7] != "ADM4":
                continue
            try:
                lat, lon, pop = float(t[4]), float(t[5]), int(t[14] or 0)
            except ValueError:
                continue
            if pop < 1000:
                continue
            kandidaten.append((_gemeindename(t[1]), lat, lon, pop))
    log(f"  {len(kandidaten)} Gemeinden in {quelle.name}")

    # bekannte deutsche Orte ins Gradraster, fuer den Dublettenabgleich
    raster = defaultdict(list)
    with ORTE.open(encoding="utf-8") as f:
        for zeile in f:
            if zeile.startswith("#"):
                continue
            t = zeile.rstrip("\n").split("\t")
            if len(t) < 6 or t[3] != "DE":
                continue
            lat, lon, pop = float(t[1]), float(t[2]), int(t[5])
            raster[(int(lat), int(lon))].append((lat, lon, pop))

    neu = []
    for name, lat, lon, pop in kandidaten:
        nachbarn = [o for dlat in (-1, 0, 1) for dlon in (-1, 0, 1)
                    for o in raster.get((int(lat) + dlat, int(lon) + dlon), ())]
        if any(haversine(lat, lon, o[0], o[1]) < 5 and o[2] >= 0.5 * pop
               for o in nachbarn):
            continue
        neu.append((name, lat, lon, pop))
    neu.sort()

    ORTE_EXTRA.write_text(
        "# Nachtrag zu orte.tsv - deutsche Gemeinden aus DE.txt (ADM4),\n"
        "# die in cities1000 fehlen.  Erzeugt mit 'geocode.py --gemeinden'.\n"
        "# name\tlat\tlon\tcc\tland\tpop\tregion\n"
        + "".join(f"{n}\t{la:.5f}\t{lo:.5f}\tDE\tDeutschland\t{p}\t\n"
                  for n, la, lo, p in neu),
        encoding="utf-8")
    log(f"  {len(neu)} Gemeinden ergaenzt -> {ORTE_EXTRA}")
    log(f"  darunter: {', '.join(n for n, *_ in neu[:6])} ...")


# ------------------------------------------------------------------ Zuordnung

def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


class Ortsindex:
    """Naechster Ort ueber ein Gradraster - schnell genug ohne numpy."""

    def __init__(self, pfade=ORTE_DATEIEN):
        if not ORTE.exists():
            sys.exit(f"Fehlt: {ORTE}\n  Erst 'python3 geocode.py --aufbereiten'")
        self.orte = []
        self.raster = defaultdict(list)
        for pfad in pfade:
            if not pfad.exists():        # die Ergaenzung ist freiwillig
                continue
            with pfad.open(encoding="utf-8") as f:
                for zeile in f:
                    if zeile.startswith("#"):
                        continue
                    t = zeile.rstrip("\n").split("\t")
                    if len(t) < 6:
                        continue
                    lat, lon, pop = float(t[1]), float(t[2]), int(t[5])
                    i = len(self.orte)
                    self.orte.append((t[0], lat, lon, t[3], t[4], pop))
                    self.raster[(int(math.floor(lat)),
                                 int(math.floor(lon)))].append(i)

    def suche(self, lat, lon):
        """-> (name, land, cc, km) oder None"""
        z_lat, z_lon = int(math.floor(lat)), int(math.floor(lon))
        # Ein Gradschritt sind 111 km in der Breite, in der Laenge weniger.
        # Der kleinere Wert sagt, bis wohin ein Ring sicher abgedeckt ist.
        km_je_ring = 111.0 * max(math.cos(math.radians(lat)), 0.05)

        gemessen = []
        gesehen = set()
        for ring in range(MAX_RING + 1):
            for dlat in range(-ring, ring + 1):
                for dlon in range(-ring, ring + 1):
                    if ring and max(abs(dlat), abs(dlon)) != ring:
                        continue                      # nur der neue Rand
                    # Laengengrad springt bei 180 Grad um
                    schluessel = (z_lat + dlat, (z_lon + dlon + 180) % 360 - 180)
                    for i in self.raster.get(schluessel, ()):
                        if i in gesehen:
                            continue
                        gesehen.add(i)
                        _, o_lat, o_lon, _, _, pop = self.orte[i]
                        gemessen.append(
                            (haversine(lat, lon, o_lat, o_lon), pop, i))
            # weitersuchen, solange der Treffer ausserhalb des sicher
            # abgedeckten Bereichs liegen koennte
            if gemessen:
                beste = min(g[0] for g in gemessen)
                if beste <= ring * km_je_ring or beste > MAX_KM:
                    break

        if not gemessen:
            return None
        beste_km = min(g[0] for g in gemessen)
        if beste_km > MAX_KM:
            return None
        # Unter fast gleich weiten Orten den groessten nehmen.  Das Fenster
        # waechst mit der Entfernung zum naechsten Ort: mitten im Ort zaehlt
        # nur er selbst, weit draussen im Feld darf der bekanntere gewinnen.
        band = min(UNSCHAERFE_KM, beste_km * UNSCHAERFE_ANTEIL)
        nah = [g for g in gemessen if g[0] <= beste_km + band]
        km, _, idx = max(nah, key=lambda x: x[1])
        name, _, _, cc, land, _ = self.orte[idx]
        return name, land, cc, km


def spalten_anlegen(conn):
    vorhanden = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    for spalte, typ in (("place", "TEXT"), ("place_country", "TEXT"),
                        ("place_cc", "TEXT"), ("place_km", "REAL")):
        if spalte not in vorhanden:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {spalte} {typ}")
    conn.commit()


def zuordnen(neu=False):
    if not DB_PATH.exists():
        sys.exit(f"Keine Datenbank unter {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    spalten_anlegen(conn)

    bedingung = "lat IS NOT NULL AND lon IS NOT NULL"
    if not neu:
        bedingung += " AND place IS NULL"
    # Von Hand eingetragene Orte bleiben unangetastet, auch bei --neu.
    spalten = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    if "hand_place" in spalten:
        bedingung += " AND hand_place IS NULL"
    rows = conn.execute(
        f"SELECT id, lat, lon FROM photos WHERE {bedingung}").fetchall()
    if not rows:
        log("Nichts zu tun.")
        stats(conn)
        return

    log(f"{len(rows)} Fotos mit Koordinaten")
    index = Ortsindex()
    log(f"  {len(index.orte)} Orte im Index")

    # Viele Fotos teilen sich denselben Ort - gerundet nachschlagen und merken
    merker = {}
    ergebnis = []
    ohne = 0
    for pid, lat, lon in rows:
        schluessel = (round(lat, 3), round(lon, 3))
        if schluessel not in merker:
            merker[schluessel] = index.suche(lat, lon)
        treffer = merker[schluessel]
        if treffer is None:
            ohne += 1
            ergebnis.append((None, None, None, None, pid))
        else:
            name, land, cc, km = treffer
            ergebnis.append((name, land, cc, round(km, 2), pid))

    conn.executemany(
        "UPDATE photos SET place=?, place_country=?, place_cc=?, place_km=? "
        "WHERE id=?", ergebnis)
    conn.commit()
    log(f"  {len(merker)} verschiedene Koordinaten nachgeschlagen")
    log(f"  {len(rows) - ohne} zugeordnet, {ohne} ohne Ort")
    stats(conn)
    conn.close()


def stats(conn=None):
    schliessen = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        schliessen = True

    gesamt, mit_gps, mit_ort = conn.execute(
        "SELECT COUNT(*), SUM(lat IS NOT NULL), SUM(place IS NOT NULL) "
        "FROM photos").fetchone()
    print(f"\n  Fotos gesamt        {gesamt}")
    print(f"  mit Koordinaten     {mit_gps}")
    print(f"  mit Ortsnamen       {mit_ort}")

    print("\n  Haeufigste Orte:")
    for name, land, n, km in conn.execute(
            "SELECT place, place_country, COUNT(*), ROUND(AVG(place_km),1) "
            "FROM photos WHERE place IS NOT NULL "
            "GROUP BY place, place_country ORDER BY 3 DESC LIMIT 15"):
        print(f"    {n:6}  {name}, {land}  (Ø {km} km)")

    print("\n  Entfernung zum Ortsmittelpunkt:")
    for unten, oben, text in ((0, 2, "unter 2 km"), (2, 10, "2 - 10 km"),
                              (10, 30, "10 - 30 km"), (30, 100, "30 - 100 km"),
                              (100, 1e9, "ueber 100 km")):
        n = conn.execute(
            "SELECT COUNT(*) FROM photos "
            "WHERE place_km >= ? AND place_km < ?", (unten, oben)).fetchone()[0]
        print(f"    {text:16} {n}")

    if schliessen:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Reverse-Geocoding, offline")
    ap.add_argument("--aufbereiten", action="store_true",
                    help="orte.tsv aus den GeoNames-Rohdaten bauen")
    ap.add_argument("--gemeinden", action="store_true",
                    help="deutsche Gemeinden aus DE.txt nachtragen")
    ap.add_argument("--neu", action="store_true",
                    help="alle Fotos neu zuordnen, nicht nur offene")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.aufbereiten:
        aufbereiten()
    elif args.gemeinden:
        gemeinden()
    elif args.stats:
        stats()
    else:
        zuordnen(neu=args.neu)


if __name__ == "__main__":
    main()
