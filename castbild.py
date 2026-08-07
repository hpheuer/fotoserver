#!/usr/bin/env python3
"""
Fotoserver - Bildaufbereitung fuer Google Cast.

Der Chromecast zeigt nur ein fertiges Bild an; Datum und Ort zeichnet im
Browser sonst die Seite selbst.  Fuer den Fernseher muessen sie also mit
ins JPEG.  Herauskommt immer eine 1920x1080-Leinwand mit schwarzem Rand -
genau das, was der Fernseher erwartet - mit dem Foto mittig darin und der
Beschriftung unten links, wie in der Slideshow.

Aufruf zum Ausprobieren:
    ~/photoserver-venv/bin/python3 castbild.py <bild.jpg> raus.jpg \\
        --datum "16. Mai 2020" --ort "Bremen"
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BREITE, HOEHE = 1920, 1080

SCHRIFT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SCHRIFT_FETT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Die Slideshow rechnet in vmin (3vmin fuer das Datum, 2.3vmin fuer den Ort).
# Bei 1080 Bildhoehe sind das 32 bzw. 25 Pixel - am Fruehstueckstisch, zwei
# bis drei Meter vom Fernseher weg, viel zu klein.  Der Faktor gilt fuer
# beide Zeilen; feinjustiert wird ueber die Einstellung "cast_skala", die
# hier als multiplikator ankommt.
FERN_FAKTOR = 4.8
RAND = round(HOEHE * 0.03)          # Abstand zum Bildrand, wie --rand: 3vmin
ECKE_ANTEIL = 0.09                  # Eckenradius, bezogen auf die Datumszeile


def _masse(skala):
    """Schriftgroessen und Polster zur gewuenschten Skala.

    Das Polster waechst mit: bei 150-px-Schrift saehen 14 px Luft aus wie
    ein Versehen.
    """
    faktor = FERN_FAKTOR * skala
    datum = round(HOEHE * 0.030 * faktor)
    ort = round(HOEHE * 0.023 * faktor)
    return datum, ort, round(datum * 0.42), round(datum * 0.26), \
        round(datum * ECKE_ANTEIL)

_schriften = {}


def schrift(pfad, groesse):
    schluessel = (pfad, groesse)
    if schluessel not in _schriften:
        try:
            _schriften[schluessel] = ImageFont.truetype(pfad, groesse)
        except OSError:
            _schriften[schluessel] = ImageFont.load_default(groesse)
    return _schriften[schluessel]


def _kasten_zeichnen(leinwand, zeilen, polster_x, polster_y, ecke):
    """Den halbtransparenten Kasten samt Text unten links setzen.

    Im Browser liegt hinter der Schrift ein abgedunkelter, weichgezeichneter
    Kasten, damit sie auch auf hellen Bildern lesbar bleibt.  Dasselbe hier:
    der Ausschnitt wird weichgezeichnet und abgedunkelt, statt einfach eine
    graue Flaeche darueberzulegen - sonst sieht es nach Fremdkoerper aus.
    """
    zeichner = ImageDraw.Draw(leinwand)
    masse = []
    for text, gr, pfad in zeilen:
        kasten = zeichner.textbbox((0, 0), text, font=schrift(pfad, gr))
        masse.append((kasten[2] - kasten[0], kasten[3] - kasten[1], text,
                      gr, pfad))

    text_breite = max(m[0] for m in masse)
    zeilen_hoehe = [round(m[3] * 1.32) for m in masse]
    text_hoehe = sum(zeilen_hoehe)

    b = min(text_breite + 2 * polster_x, BREITE - 2 * RAND)
    h = text_hoehe + 2 * polster_y
    x0, y1 = RAND, HOEHE - RAND
    y0, x1 = y1 - h, x0 + b

    # Hintergrund weichzeichnen und abdunkeln
    ausschnitt = leinwand.crop((x0, y0, x1, y1)).filter(
        ImageFilter.GaussianBlur(12))
    dunkel = Image.new("RGB", ausschnitt.size, (0, 0, 0))
    ausschnitt = Image.blend(ausschnitt, dunkel, 0.55)

    # runde Ecken ueber eine Maske
    maske = Image.new("L", ausschnitt.size, 0)
    ImageDraw.Draw(maske).rounded_rectangle(
        (0, 0, ausschnitt.size[0] - 1, ausschnitt.size[1] - 1),
        radius=ecke, fill=255)
    leinwand.paste(ausschnitt, (x0, y0), maske)

    y = y0 + polster_y
    for (_, _, text, gr, pfad), zh in zip(masse, zeilen_hoehe):
        f = schrift(pfad, gr)
        # weicher Schatten, damit die Schrift auf jedem Grund steht
        versatz = max(1, round(gr * 0.03))
        zeichner.text((x0 + polster_x + versatz, y + versatz), text, font=f,
                      fill=(0, 0, 0))
        farbe = (255, 255, 255) if pfad == SCHRIFT_FETT else (231, 231, 231)
        zeichner.text((x0 + polster_x, y), text, font=f, fill=farbe)
        y += zh


def aufbereiten(quelle, datum="", ort="", skala=1.0):
    """Foto auf 1920x1080 setzen und beschriften -> PIL-Bild."""
    gr_datum, gr_ort, polster_x, polster_y, ecke = _masse(skala)
    leinwand = Image.new("RGB", (BREITE, HOEHE), (0, 0, 0))
    with Image.open(quelle) as foto:
        foto = foto.convert("RGB")
        # einpassen, nie beschneiden - der Rest bleibt schwarz
        foto.thumbnail((BREITE, HOEHE), Image.LANCZOS)
        leinwand.paste(foto, ((BREITE - foto.width) // 2,
                              (HOEHE - foto.height) // 2))

    zeilen = []
    if datum:
        zeilen.append((datum, gr_datum, SCHRIFT_FETT))
    if ort:
        zeilen.append((ort, gr_ort, SCHRIFT))
    # Ohne Datum und ohne Ort gar keinen Kasten zeichnen - genau wie im
    # Browser, sonst faellt jedes Bild ohne Angaben unangenehm auf.
    if zeilen:
        _kasten_zeichnen(leinwand, zeilen, polster_x, polster_y, ecke)
    return leinwand


def als_jpeg(quelle, datum="", ort="", skala=1.0, guete=88):
    """Fertiges JPEG als Bytes."""
    import io
    puffer = io.BytesIO()
    aufbereiten(quelle, datum, ort, skala).save(puffer, "JPEG", quality=guete,
                                                optimize=True)
    return puffer.getvalue()


def main():
    ap = argparse.ArgumentParser(description="Bild fuer den Fernseher")
    ap.add_argument("quelle", type=Path)
    ap.add_argument("ziel", type=Path)
    ap.add_argument("--datum", default="")
    ap.add_argument("--ort", default="")
    ap.add_argument("--skala", type=float, default=1.0)
    args = ap.parse_args()
    args.ziel.write_bytes(
        als_jpeg(args.quelle, args.datum, args.ort, args.skala))
    print(f"{args.ziel}  ({args.ziel.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
