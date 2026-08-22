#!/usr/bin/env bash
#
# Fotoserver: der Steckdose sagen, dass sie gleich den Strom nehmen soll.
#
# Der Rechner haelt nur an (`systemctl halt`) und schaltet sich nicht ab -
# das Board bleibt unter Strom.  Genau darauf kommt es an: das BIOS des
# ProDesk kennt nur "After Power Loss: Power On", und dieses Merkmal setzt
# es nur, wenn der Strom im laufenden Betrieb verschwindet.  Nach einem
# echten poweroff (S5) zaehlt der Stromausfall nicht, und die Dose waere
# kein Einschalter mehr - hochfahren ginge dann nur noch am Knopf.
#
# Deshalb bestellt der Rechner seinen eigenen Stromausfall, bevor er
# anhaelt.  Genutzt wird der Flip-back-Timer der Shelly:
#
#   /relay/0?turn=on&timer=<n>   bleibt an, kippt nach n Sekunden auf aus
#   /relay/0?turn=on             loescht den Timer wieder
#   /relay/0                     has_timer / timer_remaining zur Gegenprobe
#
# Gefragt wird bewusst /relay/0 und nicht /status.  /status gibt es nur bei
# Gen 1; die neueren Geraete (Gen 2/3) sprechen RPC und antworten darauf mit
# 404 - dafuer bilden sie /relay/0 samt timer= im alten Format nach.  Diese
# eine Adresse ist also die, die auf allen Generationen funktioniert.
#
# Die Gegenprobe ist kein Schnoerkel.  Der Funk zur Dose ist nicht immer
# da, und eine verlorene Bestellung hiesse: der Rechner haelt an, der Strom
# bleibt, niemand schaltet ihn je wieder ein - und im Schrank steht eine
# tote Kiste.  Ohne Bestaetigung meldet dieses Skript deshalb Fehler, und
# der Aufrufer haelt dann bewusst nicht an.
#
# Aufrufe:
#   steckdose.sh bestellen [sekunden]   Timer setzen und bestaetigen lassen
#   steckdose.sh abbrechen              Timer wieder loeschen
#   steckdose.sh pruefen                nur nachsehen, nichts schalten
#   steckdose.sh beim-herunterfahren    fuer photoserver-steckdose.service

set -u

PROJEKT="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$PROJEKT/settings.json"

# Vorgabe 60 s.  Zwischen der Bestellung und dem wirklichen Stillstand
# liegen ueber den Desktop-Weg noch der Abbau der Sitzung und das
# Aushaengen der Dateisysteme; ueber die API sind es nur ein paar Sekunden.
# Zu knapp bemessen hiesse, dass der Strom weggeht, waehrend noch
# geschrieben wird - die 60 s sind der Puffer dafuer, nicht die Wartezeit.
FRIST=60

# Der Funk zur Dose ist schwach (der Stick sendet bewusst gedrosselt),
# einzelne Anfragen gehen verloren.  Sechs Versuche mit kurzer Pause.
VERSUCHE=6
PAUSE=2

meldung() { printf '%s\n' "$*"; }

# Die Adresse steht in settings.json und nicht hier: der Projektordner ist
# ein oeffentliches Repository, IP-Adressen gehoeren nicht hinein.
adresse() {
    python3 - "$SETTINGS" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(str(json.load(f).get("steckdose", "")).strip())
except Exception:
    pass
PY
}

# Liest has_timer/timer_remaining aus der Antwort von /relay/0.  Gibt die
# Restsekunden aus, oder nichts, wenn kein Timer steht.
#
# Genommen wird sowohl das flache Objekt, das /relay/0 liefert, als auch
# ein {"relays":[…]} aus /status - so ist es gleichgueltig, welche der
# beiden Adressen der Aufrufer befragt hat.
#
# Verlangt wird zusaetzlich, dass die Dose noch AN ist.  Der Timer ist ein
# Flip-back-Timer: "turn=on&timer=60" heisst "bleib an, kippe in 60 s auf
# aus".  Legte eine andere Firmware das einmal andersherum aus, waere die
# Dose nach dem Befehl sofort aus - und dieser Lauf hier soll das melden
# und nicht durchwinken.
#
# Die Antwort kommt als Argument, nicht durch die Pipe: bei "python3 -"
# steht das Programm selbst auf der Standardeingabe: das Here-Dokument
# verdraengt dort alles Angeleitete, und json.load(sys.stdin) sieht nur
# noch das Ende der Eingabe.  Genau daran ist die Gegenprobe stumm
# gescheitert - die Dose tat, was sie sollte, gemeldet wurde trotzdem
# "antwortet nicht".
timer_rest() {
    python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
    r = d["relays"][0] if "relays" in d else d
except Exception:
    sys.exit(1)
if r.get("has_timer") and r.get("ison"):
    print(int(r.get("timer_remaining") or 0))
PY
}

hole() {
    curl -sf -m 4 "http://$ADR$1" 2>/dev/null
}

bestellen() {
    local sek="$1" i rest antwort
    for ((i = 1; i <= VERSUCHE; i++)); do
        if hole "/relay/0?turn=on&timer=$sek" >/dev/null; then
            # Gegenprobe: steht der Timer wirklich?  Ein angenommener
            # Befehl ist noch keine laufende Uhr.
            antwort=$(hole "/relay/0")
            if [ -n "$antwort" ]; then
                rest=$(timer_rest "$antwort")
                if [ -n "$rest" ] && [ "$rest" -gt 0 ]; then
                    meldung "Steckdose nimmt den Strom in $rest s (Versuch $i)."
                    return 0
                fi
            fi
        fi
        [ "$i" -lt "$VERSUCHE" ] && sleep "$PAUSE"
    done
    meldung "Steckdose $ADR antwortet nicht - Strom bleibt an."
    return 1
}

abbrechen() {
    local i
    for ((i = 1; i <= VERSUCHE; i++)); do
        if hole "/relay/0?turn=on" >/dev/null; then
            meldung 'Timer geloescht, die Steckdose bleibt an.'
            return 0
        fi
        [ "$i" -lt "$VERSUCHE" ] && sleep "$PAUSE"
    done
    meldung "Steckdose $ADR antwortet nicht - Timer laeuft womoeglich weiter."
    return 1
}

pruefen() {
    local antwort rest
    antwort=$(hole "/relay/0") || { meldung "Steckdose $ADR nicht erreichbar."; return 1; }
    rest=$(timer_rest "$antwort")
    if [ -n "$rest" ]; then
        meldung "Steckdose erreichbar, Timer laeuft noch $rest s."
    else
        meldung 'Steckdose erreichbar, kein Timer gesetzt.'
    fi
    return 0
}

# Aufgerufen von photoserver-steckdose.service beim Herunterfahren.  Hier
# faellt die Entscheidung, ob der Strom ueberhaupt weg soll:
#
#   Neustart      -> nein, sonst kaeme der Rechner nicht wieder hoch
#   Halt/Poweroff -> ja, das ist der gewuenschte Fall
#   sonst         -> nein, dann wurde nur diese Unit gestoppt
#
# Ohne diese Unterscheidung wuerde ein `systemctl reboot` die Dose
# ausknipsen und der Rechner bliebe nach dem Neustart tot.
beim_herunterfahren() {
    local jobs RC
    jobs=$(systemctl list-jobs --no-legend 2>/dev/null)
    RC=$?
    # Eine leere Ausgabe ist der Normalfall (dann laeuft eben kein Auftrag)
    # und wird unten mitbehandelt.  Scheitert der Aufruf dagegen, ist gar
    # nichts zu erkennen - dann wird nichts bestellt: der Rechner haelt an
    # und bleibt unter Strom, laestig, aber harmlos.  Andersherum, im
    # Zweifel zu bestellen, koennte einen Neustart den Strom kosten, und
    # der Rechner kaeme nicht wieder hoch.
    if [ "$RC" -ne 0 ]; then
        meldung "systemctl list-jobs scheitert (Code $RC) - nichts bestellt."
        meldung 'Der Rechner haelt an, bleibt aber unter Strom.'
        return 0
    fi
    if printf '%s' "$jobs" | grep -qE '(reboot|kexec)\.target[[:space:]]+start'; then
        meldung 'Neustart - der Strom muss bleiben.'
        return 0
    fi
    if ! printf '%s' "$jobs" | grep -qE '(halt|poweroff|shutdown)\.target[[:space:]]+start'; then
        meldung 'Kein Herunterfahren im Gange - nichts zu tun.'
        return 0
    fi
    bestellen "$FRIST"
}

# --------------------------------------------------------------------- Start

ADR="$(adresse)"
if [ -z "$ADR" ]; then
    meldung 'Keine Steckdose eingetragen (Schluessel "steckdose" in settings.json).'
    exit 2
fi

befehl="${1:-pruefen}"
case "$befehl" in
    bestellen)          bestellen "${2:-$FRIST}" ;;
    abbrechen)          abbrechen ;;
    pruefen)            pruefen ;;
    beim-herunterfahren) beim_herunterfahren ;;
    *)
        meldung "Unbekannt: $befehl"
        meldung 'bestellen [sekunden] | abbrechen | pruefen | beim-herunterfahren'
        exit 2
        ;;
esac
