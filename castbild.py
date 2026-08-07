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
# bis drei Meter vom Bildschirm weg, viel zu klein.
#
# Bei skala = 1.0 stoesst der Kasten genau an MAX_ANTEIL; groesser wird er
# nie, egal was eingestellt ist.  Ein Viertel des Bildes ist die Grenze, ab
# der die Beschriftung anfaengt, das Foto zu verdecken statt es zu erklaeren.
MAX_ANTEIL = 0.25
FERN_FAKTOR = 2.9

RAND = round(HOEHE * 0.03)          # Abstand zum Bildrand, wie --rand: 3vmin
ECKE_ANTEIL = 0.09                  # Eckenradius, bezogen auf die Datumszeile

ANTEIL_DATUM = 0.030                # Schriftgroessen, bezogen auf die Hoehe
ANTEIL_ORT = 0.023
ZEILE = 1.32                        # Zeilenabstand
POLSTER_X_ANTEIL = 0.42             # Luft im Kasten, bezogen auf die Datumszeile
POLSTER_Y_ANTEIL = 0.26


def _masse(skala, hat_datum, hat_ort):
    """Schriftgroessen und Polster - schon auf MAX_ANTEIL gedeckelt.

    Die Kastenhoehe ist ein festes Vielfaches der Datumsschrift; welches,
    haengt daran, ob eine oder zwei Zeilen darin stehen.  Deshalb laesst
    sich die groesste erlaubte Schrift ausrechnen, statt hinterher zu
    beschneiden.  Das Polster waechst mit: bei 100-px-Schrift saehen 14 px
    Luft aus wie ein Versehen.
    """
    vielfaches = 2 * POLSTER_Y_ANTEIL
    if hat_datum:
        vielfaches += ZEILE
    if hat_ort:
        vielfaches += ZEILE * (ANTEIL_ORT / ANTEIL_DATUM)

    gewuenscht = HOEHE * ANTEIL_DATUM * FERN_FAKTOR * skala
    erlaubt = HOEHE * MAX_ANTEIL / vielfaches
    return min(gewuenscht, erlaubt)


def _aus_basis(basis):
    """-> (Datum, Ort, Polster x, Polster y, Ecke)"""
    return (round(basis),
            round(basis * ANTEIL_ORT / ANTEIL_DATUM),
            round(basis * POLSTER_X_ANTEIL),
            round(basis * POLSTER_Y_ANTEIL),
            round(basis * ECKE_ANTEIL))

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
    zeilen_hoehe = [round(m[3] * ZEILE) for m in masse]
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
    leinwand = Image.new("RGB", (BREITE, HOEHE), (0, 0, 0))
    with Image.open(quelle) as foto:
        foto = foto.convert("RGB")
        # einpassen, nie beschneiden - der Rest bleibt schwarz
        foto.thumbnail((BREITE, HOEHE), Image.LANCZOS)
        leinwand.paste(foto, ((BREITE - foto.width) // 2,
                              (HOEHE - foto.height) // 2))

    # Ohne Datum und ohne Ort gar keinen Kasten zeichnen - genau wie im
    # Browser, sonst faellt jedes Bild ohne Angaben unangenehm auf.
    if not (datum or ort):
        return leinwand

    messer = ImageDraw.Draw(leinwand)
    basis = _masse(skala, bool(datum), bool(ort))

    # Die Hoehe ist damit gedeckelt, die Breite noch nicht: "Torroella de
    # Montgri, Spanien" ist dreimal so lang wie "Bremen".  Passt es nicht,
    # ein Stueck verkleinern und noch einmal messen.
    for _ in range(4):
        gr_datum, gr_ort, polster_x, polster_y, ecke = _aus_basis(basis)
        zeilen = []
        if datum:
            zeilen.append((datum, gr_datum, SCHRIFT_FETT))
        if ort:
            zeilen.append((ort, gr_ort, SCHRIFT))
        breite = max(messer.textlength(t, font=schrift(p, g))
                     for t, g, p in zeilen)
        platz = BREITE - 2 * RAND - 2 * polster_x
        if breite <= platz or basis < 24:
            break
        basis *= platz / breite

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
