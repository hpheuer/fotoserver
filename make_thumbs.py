#!/usr/bin/env python3
"""
Thumbnails fuer den Fotoserver erzeugen.

Erzeugt je Bild zwei verkleinerte JPEG-Versionen:
    display  1920 px lange Kante  (Slideshow)
    grid      320 px lange Kante  (Uebersicht im Webinterface)

Ablage:  cache/<groesse>/<ab>/<sha256>.jpg
Der Lauf ist wiederaufnehmbar: vorhandene Dateien werden uebersprungen.

Aufruf:
    python3 make_thumbs.py
    python3 make_thumbs.py --limit 200      # Testlauf
    python3 make_thumbs.py --force          # alles neu erzeugen
    python3 make_thumbs.py --stats          # nur Auswertung
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from multiprocessing import Pool, cpu_count
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Pillow fehlt.  ~/photoserver-venv/bin/pip install Pillow pillow-heif")

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

Image.MAX_IMAGE_PIXELS = 300_000_000  # grosse Scans zulassen

DB_PATH = Path.home() / "photoserver" / "photos.db"
CACHE = Path.home() / "photoserver" / "cache"

SIZES = {"display": 1920, "grid": 320}
QUALITY = {"display": 85, "grid": 78}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def thumb_path(sha, size_name):
    return CACHE / size_name / sha[:2] / f"{sha}.jpg"


def build_one(job):
    """(id, sha, pfad, force) -> (id, status, meldung).  Laeuft im Worker."""
    photo_id, sha, src, force = job
    try:
        targets = {n: thumb_path(sha, n) for n in SIZES}
        if not force and all(t.exists() and t.stat().st_size > 0
                             for t in targets.values()):
            return photo_id, "vorhanden", None

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            # groesste Variante zuerst
            for name in sorted(SIZES, key=lambda n: -SIZES[n]):
                edge = SIZES[name]
                out = targets[name]
                out.parent.mkdir(parents=True, exist_ok=True)
                copy = im.copy()
                copy.thumbnail((edge, edge), Image.LANCZOS)
                if copy.mode != "RGB":
                    copy = copy.convert("RGB")
                tmp = out.with_suffix(".tmp")
                copy.save(tmp, "JPEG", quality=QUALITY[name],
                          optimize=True, progressive=(name == "display"))
                os.replace(tmp, out)
        return photo_id, "ok", None

    except Exception as exc:                      # defekte Datei, Format, Speicher
        return photo_id, "fehler", f"{type(exc).__name__}: {exc}"


def ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    if "thumb_state" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN thumb_state TEXT")
    if "thumb_error" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN thumb_error TEXT")
    conn.commit()


def run(limit=None, force=False, workers=None):
    if not DB_PATH.exists():
        sys.exit(f"Keine Datenbank unter {DB_PATH}")
    if not HEIF:
        log("WARNUNG: pillow-heif fehlt, HEIC-Dateien werden scheitern.")

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)

    if force:
        query = """SELECT id, sha256, path FROM photos
                   WHERE is_video = 0
                   ORDER BY display_date DESC"""
    else:
        query = """SELECT id, sha256, path FROM photos
                   WHERE is_video = 0
                     AND (thumb_state IS NULL OR thumb_state <> 'ok')
                   ORDER BY display_date DESC"""
    rows = conn.execute(query).fetchall()
    if limit:
        rows = rows[:limit]

    videos = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE is_video = 1").fetchone()[0]
    if videos:
        conn.execute("UPDATE photos SET thumb_state='video' WHERE is_video=1")
        conn.commit()

    if not rows:
        log("Nichts zu tun - alle Thumbnails vorhanden.")
        show_stats(conn)
        return

    n_workers = workers or max(1, min(cpu_count(), 6))
    log(f"{len(rows)} Bilder, {n_workers} Prozesse ({videos} Videos uebersprungen)")

    jobs = [(r[0], r[1], r[2], force) for r in rows]
    counts = {"ok": 0, "vorhanden": 0, "fehler": 0}
    errors = []
    start = time.time()
    pending = []

    with Pool(n_workers) as pool:
        for i, (pid, status, msg) in enumerate(
                pool.imap_unordered(build_one, jobs, chunksize=8), 1):
            counts[status] = counts.get(status, 0) + 1
            pending.append(("ok" if status != "fehler" else "fehler", msg, pid))
            if status == "fehler" and len(errors) < 30:
                errors.append(msg)

            if len(pending) >= 200:
                conn.executemany(
                    "UPDATE photos SET thumb_state=?, thumb_error=? WHERE id=?",
                    pending)
                conn.commit()
                pending.clear()

            if i % 500 == 0:
                rate = i / max(time.time() - start, 1)
                rest = (len(jobs) - i) / rate if rate else 0
                log(f"{i}/{len(jobs)}  ({rate:.1f}/s, noch ~{rest/60:.0f} min)")

    if pending:
        conn.executemany(
            "UPDATE photos SET thumb_state=?, thumb_error=? WHERE id=?", pending)
        conn.commit()

    dauer = time.time() - start
    log(f"Fertig in {dauer/60:.1f} min")
    print(f"\n  neu erzeugt   {counts['ok']}")
    print(f"  schon da      {counts['vorhanden']}")
    print(f"  Fehler        {counts['fehler']}")
    if errors:
        print("\n  Fehlerbeispiele:")
        for e in errors[:8]:
            print(f"    {e}")
    show_stats(conn)
    conn.close()


def show_stats(conn=None):
    close = False
    if conn is None:
        if not DB_PATH.exists():
            sys.exit(f"Keine Datenbank unter {DB_PATH}")
        conn = sqlite3.connect(DB_PATH)
        close = True

    print("\n  Status:")
    for state, n in conn.execute(
            "SELECT COALESCE(thumb_state,'offen'), COUNT(*) FROM photos "
            "GROUP BY 1 ORDER BY 2 DESC"):
        print(f"    {state:12} {n}")

    for name in SIZES:
        d = CACHE / name
        if not d.exists():
            continue
        files = list(d.rglob("*.jpg"))
        total = sum(f.stat().st_size for f in files)
        print(f"    {name:12} {len(files)} Dateien, {total/1024**3:.2f} GB")

    if close:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Thumbnails erzeugen")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.stats:
        show_stats()
    else:
        run(args.limit, args.force, args.workers)


if __name__ == "__main__":
    main()
