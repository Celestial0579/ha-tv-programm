# TV-Programm für Home Assistant

Lädt einmal täglich das Fernsehprogramm aus einer **XMLTV-Datei** und macht es abfragbar – für
Automationen und für den Sprachassistenten („Was läuft gerade?“, „Was kommt heute um 20:15 im ZDF?“,
„Wann läuft der nächste Tatort?“).

*English summary below.*

## Was es liefert

| | |
|---|---|
| Aktion **`tv_programm.abfragen`** | Antwort je Sender mit laufender und folgender Sendung zum Zeitpunkt (Vorgabe: jetzt) – oder mit `suche` die nächsten Ausstrahlungen einer Sendung |
| Sensor **Programmstand** | Zeitpunkt des letzten Ladens, Anzahl Sender/Sendungen, Programm bis, letzter Fehler |
| Knopf **Jetzt neu laden** | lädt die Programmdaten sofort |

Felder der Aktion: `zeitpunkt` (z. B. `20:15` oder `2026-10-26T20:15`), `sender` (Liste, Groß-/Kleinschreibung und
Bindestriche egal: „n-tv“ = „ntv“), `suche` (Titel oder Teil davon).

## Einrichtung

1. HACS › **Benutzerdefinierte Repositories** › diese Repository-URL, Typ **Integration**; installieren, neu starten.
2. **Einstellungen › Geräte & Dienste › Integration hinzufügen › TV-Programm**: Adresse der XMLTV-Datei und Ladezeit.
3. Unter **Konfigurieren** die Sender wählen (Vorgabe: 25 große deutsche Sender). Nur diese werden gespeichert.

## Datenquelle

Vorgabe ist die deutsche Datei von **[iptv-epg.org](https://iptv-epg.org)** (rund 440 Sender, gut eine Woche voraus,
etwa 45 MB). Es ist ein Gemeinschaftsangebot ohne erkennbare Nutzungsbedingungen; die Herkunft der Daten ist
nicht angegeben. Die Integration lädt die Datei höchstens einmal täglich mit eigenem User-Agent. Jede andere
XMLTV-Adresse funktioniert ebenso – etwa die eines eigenen Tvheadend-Servers.

---

## English summary

Downloads a TV guide in XMLTV format once a day (default: the German file from iptv-epg.org) and exposes the action
`tv_programm.abfragen` (response: current and next show per channel at a given time, or the next airings matching a
search). Pick the channels to keep under *Configure*. Set up from the UI after installing via HACS as a custom
repository (type *Integration*).

Lizenz / License: MIT
