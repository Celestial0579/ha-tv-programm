# TV-Programm für Home Assistant

Lädt einmal täglich das Fernsehprogramm aus einer **XMLTV-Datei** und macht es abfragbar – für
Automationen und für den Sprachassistenten („Was läuft gerade?“, „Was kommt heute um 20:15 im ZDF?“,
„Wann läuft der nächste Tatort?“).

*English summary below.*

## Was es liefert

| | |
|---|---|
| Aktion **`tv_programm.abfragen`** | Antwort je Sender mit laufender und folgender Sendung zum Zeitpunkt (Vorgabe: jetzt) – oder mit `suche` die nächsten Ausstrahlungen einer Sendung |
| Sensor **Programmstand** | Zeitpunkt des letzten Ladens, Anzahl Sender/Sendungen, Programm bis, `aus_ersatzquelle` (Sender aus der Ersatzquelle), letzter Fehler |
| Sensor **Jetzt im TV** | Zahl der Sender; Attribut `sender`: je Sender `titel`, `beginn`, `ende`, `danach`, `danach_beginn` – minütlich neu, nicht in der Datenbank |
| Sensor **Heute um 20:15** | wie oben für 20:15 (bis 05:00 noch der vergangene Abend), Attribut `zeitpunkt` |
| Knopf **Jetzt neu laden** | lädt die Programmdaten sofort |

Felder der Aktion: `zeitpunkt` (z. B. `20:15` oder `2026-10-26T20:15`), `sender` (Liste, Groß-/Kleinschreibung und
Bindestriche egal: „n-tv“ = „ntv“), `suche` (Titel oder Teil davon).

## Einrichtung

1. HACS › **Benutzerdefinierte Repositories** › diese Repository-URL, Typ **Integration**; installieren, neu starten.
2. **Einstellungen › Geräte & Dienste › Integration hinzufügen › TV-Programm**: Haupt- und Ersatzquelle, Ladezeit.
3. Unter **Konfigurieren** Quellen und Sender ändern (Vorgabe: 25 große deutsche Sender). Nur diese werden gespeichert.

## Datenquellen

Zwei Quellen, beide über **Konfigurieren** änderbar (XMLTV, auch gzip-gepackt):

- **Hauptquelle** – Vorgabe **[epgshare01](https://epgshare01.online)** (`epg_ripper_DE1.xml.gz`, rund 270 Sender,
  fünf Tage voraus, gepackt etwa 4 MB). Zeiten korrekt, aber **ohne die dritten Programme** (NDR, WDR, BR …).
- **Ersatzquelle** – Vorgabe **[iptv-epg.org](https://iptv-epg.org)** (rund 440 Sender, etwa 45 MB). Geladen wird sie
  nur, wenn gewählte Sender in der Hauptquelle fehlen, und gelesen werden dann **nur diese Sender**. Fällt die
  Hauptquelle aus, kommt alles von hier. Hintergrund: Nach der Zeitumstellung am 25.10.2026 lagen dort ZDF, SAT.1,
  VOX, RTLZWEI und ARTE eine Stunde zu früh.

Hinweis: XMLTV lässt sich nur als ganze Datei laden. Steht ein Sender nur in der Ersatzquelle zur Auswahl, wird ihre
Datei täglich mitgeladen. Beides sind Gemeinschaftsangebote ohne erkennbare Nutzungsbedingungen; die Integration
lädt höchstens einmal täglich mit eigenem User-Agent. Jede andere XMLTV-Adresse funktioniert ebenso – etwa die eines
eigenen Tvheadend-Servers. Bestehende Einrichtungen mit der alten Vorgabe stellen sich beim Update selbst um.

---

## English summary

Downloads a TV guide in XMLTV format once a day from a main source (default: epgshare01, gzip) and reads only the
channels missing there from a fallback source (default: iptv-epg.org), and exposes the action
`tv_programm.abfragen` (response: current and next show per channel at a given time, or the next airings matching a
search). Pick the channels to keep under *Configure*. Set up from the UI after installing via HACS as a custom
repository (type *Integration*).

Lizenz / License: MIT

### Dashboard-Beispiel (Markdown-Karte)

```yaml
type: markdown
content: >-
  | Sender | Jetzt | 20:15 |
  |---|---|---|
  {%- set abend = state_attr('sensor.tv_programm_heute_um_20_15', 'sender') or [] %}
  {%- for z in state_attr('sensor.tv_programm_jetzt_im_tv', 'sender') or [] %}
  {%- set a = abend | selectattr('sender', 'eq', z.sender) | first %}
  | {{ z.sender }} | {{ z.titel | default('–') }} | {{ a.titel if a else '–' }} |
  {%- endfor %}
```
