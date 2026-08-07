#!/usr/bin/env python3
"""
Google-Takeout-Import -> SQLite

Liest Fotos/Videos aus einem entpackten Takeout-Verzeichnis, bestimmt
Aufnahmedatum und Position (EXIF bevorzugt, Takeout-JSON als Fallback),
dedupliziert per SHA-256 und schreibt alles in eine SQLite-Datenbank.

Aufruf:
    python3 import_photos.py                 # Vollimport
    python3 import_photos.py --limit 200     # Testlauf
    python3 import_photos.py --stats         # nur Auswertung der DB
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- Konfiguration

DEFAULT_SOURCE = Path.home() / "takeout-import" / "Takeout"
DEFAULT_DB = Path.home() / "photoserver" / "photos.db"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp",
             ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".3gp", ".mkv", ".webm"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

# Motion-Photo-Spuren, Apple-Sidecars, Takeout-Beiwerk -> ignorieren
IGNORE_EXT = {".json", ".mp", ".aae", ".txt", ".html", ".csv", ".pdf", ".xml"}

# Suffixe, die Google an bearbeitete Kopien haengt (Sprachabhaengig)
EDIT_SUFFIXES = [
    "-bearbeitet", "-edited", "-modifié", "-modifie", "-bewerkt",
    "-editado", "-modificato", "-redigerad", "-muokattu", "-edytowane",
]

SUPPLEMENTAL = "supplemental-metadata"
EXIFTOOL_CHUNK = 300      # Dateien pro exiftool-Aufruf
HASH_BLOCK = 1024 * 1024  # 1 MiB


# ------------------------------------------------------------------- Hilfsmittel

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_BLOCK):
            h.update(chunk)
    return h.hexdigest()


def parse_exif_datetime(value):
    """'2014:05:12 10:33:21' oder mit Zeitzone -> ISO-String, sonst None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.startswith(("0000", "    ")):
        return None
    v = re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", v).strip()
    v = v.split(".")[0]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y:%m:%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).isoformat(sep=" ")
        except ValueError:
            continue
    return None


def valid_coord(lat, lon):
    """Google setzt fehlende Positionen auf exakt 0/0."""
    if lat is None or lon is None:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


# ------------------------------------------------------- Sidecar-Zuordnung

def strip_edit_suffix(stem):
    low = stem.lower()
    for suf in EDIT_SUFFIXES:
        if low.endswith(suf):
            return stem[: -len(suf)]
    return stem


def json_candidate_keys(json_path):
    """Aus dem JSON-Dateinamen moegliche Medien-Dateinamen ableiten."""
    name = json_path.name
    if name.lower().endswith(".json"):
        name = name[:-5]

    keys = set()

    def add_variants(n):
        keys.add(n.lower())
        # Zaehler wandert: 'IMG.jpg(1)' -> 'IMG(1).jpg'
        m = re.match(r"^(.*)(\.[^.()/\\]+)\((\d+)\)$", n)
        if m:
            keys.add(f"{m.group(1)}({m.group(3)}){m.group(2)}".lower())

    add_variants(name)

    # '.supplemental-metadata' bzw. abgeschnittene Formen entfernen
    idx = name.rfind(".")
    if idx != -1:
        tail = name[idx + 1:]
        base = name[:idx]
        if tail and SUPPLEMENTAL.startswith(tail.lower()):
            add_variants(base)
            # Zaehler kann auch hinter dem Suffix stehen
            m = re.match(r"^(.*)\((\d+)\)$", tail)
            if m and SUPPLEMENTAL.startswith(m.group(1).lower()):
                add_variants(base)

    return keys


def load_sidecars(directory):
    """Alle JSONs eines Verzeichnisses einlesen und indizieren."""
    by_key = {}      # abgeleiteter Dateiname -> Eintrag
    by_title = {}    # 'title'-Feld -> Liste von Eintraegen
    ambiguous = set()

    for jp in sorted(directory.glob("*.json")):
        try:
            with open(jp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        # Albummetadaten haben kein 'title' + 'photoTakenTime'
        if "photoTakenTime" not in data and "title" not in data:
            continue

        entry = {"path": jp, "data": data}

        for key in json_candidate_keys(jp):
            if key in by_key and by_key[key] is not entry:
                ambiguous.add(key)
            by_key[key] = entry

        title = data.get("title")
        if isinstance(title, str) and title:
            by_title.setdefault(title.lower(), []).append(entry)

    for key in ambiguous:
        by_key.pop(key, None)

    return by_key, by_title


def match_sidecar(media_path, by_key, by_title):
    """Passenden JSON-Eintrag suchen. Rueckgabe: (data, methode) oder (None, None)."""
    stem, ext = os.path.splitext(media_path.name)
    base_stem = strip_edit_suffix(stem)

    # Duplikat-Zaehler abtrennen: 'IMG(1)' -> 'IMG', n=1
    counter = 0
    m = re.match(r"^(.*)\((\d+)\)$", base_stem)
    plain_stem = base_stem
    if m:
        plain_stem, counter = m.group(1), int(m.group(2))

    names = [
        (stem + ext).lower(),
        (base_stem + ext).lower(),
        (plain_stem + ext).lower(),
    ]

    # 1. Direkter Treffer ueber den JSON-Dateinamen
    for n in names:
        if n in by_key:
            return by_key[n]["data"], "name"

    # 2. Treffer ueber das 'title'-Feld im JSON
    for n in names:
        entries = by_title.get(n)
        if entries:
            idx = counter if 0 < counter < len(entries) else 0
            return entries[idx]["data"], "title"

    # 3. Gekuerzte JSON-Namen: eindeutiger Praefix-Treffer
    target = names[1]
    hits = [e for k, e in by_key.items() if len(k) >= 12 and target.startswith(k)]
    unique = {id(h): h for h in hits}
    if len(unique) == 1:
        return next(iter(unique.values()))["data"], "prefix"

    return None, None


# --------------------------------------------------------------- exiftool

def check_exiftool():
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("exiftool fehlt.  sudo apt install -y libimage-exiftool-perl")


EXIF_TAGS = [
    "-SourceFile", "-MIMEType", "-ImageWidth", "-ImageHeight",
    "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
    "-GPSLatitude", "-GPSLongitude", "-Make", "-Model", "-Orientation",
]


def exif_batch(paths):
    """Metadaten fuer eine Liste von Dateien holen -> dict pfad -> tags."""
    if not paths:
        return {}
    cmd = ["exiftool", "-j", "-n", "-q", "-q",
           "-charset", "filename=utf8", "-api", "QuickTimeUTC=1"] + EXIF_TAGS
    cmd += [str(p) for p in paths]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=600)
        out = res.stdout.decode("utf-8", "replace").strip()
        if not out:
            return {}
        records = json.loads(out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    return {r.get("SourceFile"): r for r in records if r.get("SourceFile")}


# --------------------------------------------------------------- Datenbank

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    path          TEXT NOT NULL,
    filename      TEXT,
    ext           TEXT,
    is_video      INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER,
    width         INTEGER,
    height        INTEGER,
    taken_at      TEXT,
    taken_source  TEXT,
    lat           REAL,
    lon           REAL,
    geo_source    TEXT,
    camera_make   TEXT,
    camera_model  TEXT,
    orientation   INTEGER,
    first_seen    TEXT,
    last_seen     TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path    TEXT PRIMARY KEY,
    sha256  TEXT NOT NULL,
    album   TEXT,
    bytes   INTEGER,
    mtime   REAL,
    seen_at TEXT
);
CREATE TABLE IF NOT EXISTS photo_albums (
    photo_id INTEGER NOT NULL,
    album    TEXT NOT NULL,
    PRIMARY KEY (photo_id, album)
);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_photos_geo   ON photos(lat, lon);
CREATE INDEX IF NOT EXISTS idx_files_sha    ON files(sha256);
"""


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


# ------------------------------------------------------------------- Import

def collect_media(source):
    """Medien nach Verzeichnis gruppiert einsammeln."""
    groups = {}
    skipped = {}
    for root, dirs, names in os.walk(source):
        dirs.sort()
        rp = Path(root)
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext in MEDIA_EXT:
                groups.setdefault(rp, []).append(rp / n)
            elif ext not in IGNORE_EXT:
                skipped[ext] = skipped.get(ext, 0) + 1
    return groups, skipped


def run_import(source, db_path, limit=None):
    check_exiftool()
    if not source.is_dir():
        sys.exit(f"Quellverzeichnis nicht gefunden: {source}")

    conn = open_db(db_path)
    cur = conn.cursor()

    known = {row[0]: (row[1], row[2], row[3])
             for row in cur.execute("SELECT path, sha256, bytes, mtime FROM files")}
    log(f"Datenbank: {db_path}  ({len(known)} Dateien bereits bekannt)")

    log("Sammle Dateien ...")
    groups, skipped_ext = collect_media(source)
    total = sum(len(v) for v in groups.values())
    log(f"{total} Medien in {len(groups)} Verzeichnissen gefunden")
    if skipped_ext:
        log(f"Ignorierte Endungen: {dict(sorted(skipped_ext.items()))}")

    stats = {
        "seen": 0, "hashed": 0, "cached": 0, "new": 0, "dupe": 0,
        "sidecar_name": 0, "sidecar_title": 0, "sidecar_prefix": 0,
        "sidecar_none": 0, "no_date": 0, "no_geo": 0, "error": 0,
    }
    date_src = {}
    geo_src = {}
    unmatched = []
    start = time.time()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    stop = False

    for directory in sorted(groups):
        if stop:
            break
        media = groups[directory]
        album = directory.name
        by_key, by_title = load_sidecars(directory)

        for i in range(0, len(media), EXIFTOOL_CHUNK):
            chunk = media[i:i + EXIFTOOL_CHUNK]
            exif = exif_batch(chunk)

            for path in chunk:
                stats["seen"] += 1
                if limit and stats["seen"] > limit:
                    stop = True
                    break
                try:
                    st = path.stat()
                except OSError:
                    stats["error"] += 1
                    continue

                spath = str(path)
                prev = known.get(spath)
                if prev and prev[1] == st.st_size and abs(prev[2] - st.st_mtime) < 1:
                    digest = prev[0]
                    stats["cached"] += 1
                else:
                    try:
                        digest = sha256_of(path)
                    except OSError:
                        stats["error"] += 1
                        continue
                    stats["hashed"] += 1

                # --- Sidecar
                sc, method = match_sidecar(path, by_key, by_title)
                if method:
                    stats[f"sidecar_{method}"] += 1
                else:
                    stats["sidecar_none"] += 1
                    if len(unmatched) < 40:
                        unmatched.append(spath)

                tags = exif.get(spath, {})

                # --- Datum: EXIF vor Takeout-JSON vor Dateizeit
                taken = taken_src = None
                for tag in ("DateTimeOriginal", "CreateDate", "MediaCreateDate"):
                    taken = parse_exif_datetime(tags.get(tag))
                    if taken:
                        taken_src = "exif:" + tag
                        break
                if not taken and sc:
                    ts = (sc.get("photoTakenTime") or {}).get("timestamp")
                    if ts:
                        try:
                            taken = datetime.fromtimestamp(
                                int(ts), tz=timezone.utc).replace(
                                tzinfo=None).isoformat(sep=" ")
                            taken_src = "json:photoTakenTime"
                        except (ValueError, OSError):
                            pass
                if not taken:
                    taken = datetime.fromtimestamp(st.st_mtime).isoformat(
                        sep=" ", timespec="seconds")
                    taken_src = "dateisystem"
                    stats["no_date"] += 1
                date_src[taken_src] = date_src.get(taken_src, 0) + 1

                # --- Position: EXIF vor Takeout-JSON
                lat = lon = geosrc = None
                if valid_coord(tags.get("GPSLatitude"), tags.get("GPSLongitude")):
                    lat = float(tags["GPSLatitude"])
                    lon = float(tags["GPSLongitude"])
                    geosrc = "exif"
                elif sc:
                    for field, name in (("geoData", "json:geoData"),
                                        ("geoDataExif", "json:geoDataExif")):
                        g = sc.get(field) or {}
                        if valid_coord(g.get("latitude"), g.get("longitude")):
                            lat = float(g["latitude"])
                            lon = float(g["longitude"])
                            geosrc = name
                            break
                if geosrc is None:
                    stats["no_geo"] += 1
                    geosrc = "keine"
                geo_src[geosrc] = geo_src.get(geosrc, 0) + 1

                ext = path.suffix.lower()
                row = cur.execute(
                    "SELECT id FROM photos WHERE sha256=?", (digest,)).fetchone()
                if row:
                    photo_id = row[0]
                    cur.execute("UPDATE photos SET last_seen=? WHERE id=?",
                                (now, photo_id))
                    stats["dupe"] += 1
                else:
                    cur.execute("""
                        INSERT INTO photos (sha256, path, filename, ext, is_video,
                            bytes, width, height, taken_at, taken_source,
                            lat, lon, geo_source, camera_make, camera_model,
                            orientation, first_seen, last_seen)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (digest, spath, path.name, ext,
                         1 if ext in VIDEO_EXT else 0, st.st_size,
                         tags.get("ImageWidth"), tags.get("ImageHeight"),
                         taken, taken_src, lat, lon,
                         None if geosrc == "keine" else geosrc,
                         tags.get("Make"), tags.get("Model"),
                         tags.get("Orientation"), now, now))
                    photo_id = cur.lastrowid
                    stats["new"] += 1

                cur.execute(
                    "INSERT OR IGNORE INTO photo_albums (photo_id, album) VALUES (?,?)",
                    (photo_id, album))
                cur.execute("""INSERT OR REPLACE INTO files
                    (path, sha256, album, bytes, mtime, seen_at)
                    VALUES (?,?,?,?,?,?)""",
                    (spath, digest, album, st.st_size, st.st_mtime, now))

            if stop:
                break
            conn.commit()
            done = stats["seen"]
            if done % 1500 < EXIFTOOL_CHUNK:
                rate = done / max(time.time() - start, 1)
                rest = (total - done) / rate if rate else 0
                log(f"{done}/{total}  ({rate:.0f}/s, noch ~{rest/60:.0f} min)")

    conn.commit()

    if unmatched:
        report = db_path.parent / "unmatched_sidecars.txt"
        report.write_text("\n".join(unmatched) + "\n", encoding="utf-8")

    dur = time.time() - start
    log(f"Fertig in {dur/60:.1f} min")
    print()
    print(f"  Verarbeitet        {stats['seen']}")
    print(f"  neu gehasht        {stats['hashed']}   aus Cache {stats['cached']}")
    print(f"  neue Fotos         {stats['new']}")
    print(f"  Duplikate          {stats['dupe']}")
    print(f"  Fehler             {stats['error']}")
    print()
    print(f"  Sidecar ueber Name    {stats['sidecar_name']}")
    print(f"  Sidecar ueber Titel   {stats['sidecar_title']}")
    print(f"  Sidecar ueber Praefix {stats['sidecar_prefix']}")
    print(f"  ohne Sidecar          {stats['sidecar_none']}")
    if unmatched:
        print(f"  -> Beispiele in {db_path.parent / 'unmatched_sidecars.txt'}")
    print()
    print("  Datumsquelle:")
    for k, v in sorted(date_src.items(), key=lambda x: -x[1]):
        print(f"    {k:28} {v}")
    print("  Positionsquelle:")
    for k, v in sorted(geo_src.items(), key=lambda x: -x[1]):
        print(f"    {k:28} {v}")
    conn.close()


# ------------------------------------------------------------------- Statistik

def show_stats(db_path):
    if not db_path.exists():
        sys.exit(f"Keine Datenbank unter {db_path}")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    geo = c.execute("SELECT COUNT(*) FROM photos WHERE lat IS NOT NULL").fetchone()[0]
    vid = c.execute("SELECT COUNT(*) FROM photos WHERE is_video=1").fetchone()[0]
    size = c.execute("SELECT COALESCE(SUM(bytes),0) FROM photos").fetchone()[0]
    print(f"Eindeutige Medien : {total}")
    print(f"Dateien insgesamt : {files}  (Duplikate: {files - total})")
    print(f"davon Videos      : {vid}")
    print(f"mit Position      : {geo}  ({geo*100//max(total,1)} %)")
    print(f"Groesse eindeutig : {size/1024**3:.1f} GB")
    print("\nFotos pro Jahr:")
    for year, n in c.execute(
            "SELECT substr(taken_at,1,4) y, COUNT(*) FROM photos "
            "GROUP BY y ORDER BY y"):
        print(f"  {year}  {n:6}  {'#' * min(n // 200, 50)}")
    print("\nGroesste Alben:")
    for album, n in c.execute(
            "SELECT album, COUNT(*) n FROM photo_albums "
            "GROUP BY album ORDER BY n DESC LIMIT 15"):
        print(f"  {n:6}  {album}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Google-Takeout-Fotoimport")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, help="nur N Dateien (Testlauf)")
    ap.add_argument("--stats", action="store_true", help="nur Auswertung")
    args = ap.parse_args()

    if args.stats:
        show_stats(args.db)
    else:
        run_import(args.source, args.db, args.limit)


if __name__ == "__main__":
    main()
