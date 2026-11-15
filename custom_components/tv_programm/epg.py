"""Laden und Auswerten von XMLTV-Programmdaten.

Schutzmodell: Die XMLTV-Datei kommt von einer fremden Adresse (Gemeinschaftsangebote mit
unbekannten Betreibern, eigene Adressen auch über http://). Ihr Inhalt ist fremde Eingabe.
Wer eine Quelle kontrolliert, soll Home Assistant weder den Datenträger füllen noch den
Speicher sprengen können. Deshalb ist alles begrenzt: die heruntergeladene Menge, die
entpackte Menge, die Länge jedes Texts, die Zahl der Sendungen je Sender. Gelesen wird ohne
Baum (expat direkt), Dokumente mit eigenen Entitäten werden abgelehnt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
import gzip
import json
import os
from pathlib import Path
import re
from typing import Any
from xml.parsers import expat

import aiohttp

FASSUNG = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))["version"]
USER_AGENT = f"HomeAssistant-tv_programm/{FASSUNG} (+https://github.com/Celestial0579/ha-tv-programm)"

MAX_DOWNLOAD = 200_000_000  # Byte; die große Vorgabe-Quelle hat rund 45 MB
MAX_ENTPACKT = 1_000_000_000  # Byte nach dem Entpacken
MAX_TEXT = 1000  # Zeichen, die je Feld überhaupt gesammelt werden
MAX_SENDUNGEN = 5000  # je Sender; fünf Tage haben selten mehr als 300
LAENGEN = {"title": 200, "sub-title": 200, "desc": 300, "category": 100, "display-name": 100}
_STUECK = 1 << 16


class EpgFehler(Exception):
    """Programmdaten nicht ladbar oder unbrauchbar."""


def norm(text: str) -> str:
    """Vergleichsform eines Sendernamens: „Kabel Eins“, „kabel eins“ und „KABEL-EINS“ sind gleich."""
    return "".join(z for z in text.lower() if z.isalnum())


def anzeigename(roh: str) -> str:
    """„DE - Das Erste“ -> „Das Erste“."""
    return re.sub(r"^[A-Z]{2,3}\s*[-|:]\s*", "", (roh or "").strip())


def _zeit(wert: str | None) -> float | None:
    """XMLTV-Zeit; ohne Angabe der Zeitzone gilt laut XMLTV UTC."""
    if not wert:
        return None
    try:
        return datetime.strptime(wert.strip()[:20], "%Y%m%d%H%M%S %z").timestamp()
    except ValueError:
        try:
            return datetime.strptime(wert.strip()[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
        except ValueError:
            return None


async def herunterladen(sitzung: aiohttp.ClientSession, url: str, ziel: str, grenze: int = MAX_DOWNLOAD) -> int:
    """XMLTV-Datei herunterladen (folgt Weiterleitungen), Größe in Byte.

    Dateizugriffe laufen im Executor, nicht in der Ereignisschleife.
    """
    schleife = asyncio.get_running_loop()
    teil = ziel + ".teil"
    datei = None
    groesse = 0
    try:
        async with sitzung.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=600)
        ) as antwort:
            if antwort.status != 200:
                raise EpgFehler(f"HTTP {antwort.status}")
            if (antwort.content_length or 0) > grenze:
                raise EpgFehler(f"Datei größer als {grenze // 1_000_000} MB")
            datei = await schleife.run_in_executor(None, open, teil, "wb")
            puffer = bytearray()
            async for stueck in antwort.content.iter_chunked(_STUECK):
                groesse += len(stueck)
                if groesse > grenze:
                    raise EpgFehler(f"Datei größer als {grenze // 1_000_000} MB")
                puffer.extend(stueck)
                if len(puffer) >= 1 << 20:
                    await schleife.run_in_executor(None, datei.write, bytes(puffer))
                    puffer.clear()
            await schleife.run_in_executor(None, datei.write, bytes(puffer))
            await schleife.run_in_executor(None, datei.close)
            datei = None
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        await schleife.run_in_executor(None, _weg, datei, teil)
        raise EpgFehler(f"{type(err).__name__}: {str(err)[:100]}" if str(err) else type(err).__name__) from err
    except EpgFehler:
        await schleife.run_in_executor(None, _weg, datei, teil)
        raise
    await schleife.run_in_executor(None, os.replace, teil, ziel)
    return groesse


def _weg(datei: Any, pfad: str) -> None:
    if datei is not None:
        datei.close()
    if os.path.exists(pfad):
        os.remove(pfad)


class _Leser:
    """Sammelt Sender und Sendungen aus den expat-Ereignissen, ohne einen Baum aufzubauen."""

    FELDER_SENDER = ("display-name",)
    FELDER_SENDUNG = ("title", "sub-title", "desc", "category")

    def __init__(self, gewuenscht: set[str] | None) -> None:
        self.gewuenscht = gewuenscht
        self.kanaele: dict[str, str] = {}
        self.programm: dict[str, list[list[Any]]] = {}
        self._art: str | None = None
        self._attr: dict[str, str] = {}
        self._felder: dict[str, str] = {}
        self._feld: str | None = None
        self._text: list[str] = []
        self._laenge = 0

    def start(self, name: str, attr: dict[str, str]) -> None:
        if name in ("channel", "programme"):
            self._art, self._attr, self._felder, self._feld = name, attr, {}, None
            return
        erlaubt = self.FELDER_SENDER if self._art == "channel" else self.FELDER_SENDUNG if self._art else ()
        if self._feld is None and name in erlaubt and name not in self._felder:
            self._feld, self._text, self._laenge = name, [], 0

    def text(self, daten: str) -> None:
        if self._feld is not None and self._laenge < MAX_TEXT:
            stueck = daten[: MAX_TEXT - self._laenge]
            self._text.append(stueck)
            self._laenge += len(stueck)

    def ende(self, name: str) -> None:
        if self._feld is not None and name == self._feld:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self._felder[name] = text[: LAENGEN[name]]
            self._feld = None
        elif name == "channel" and self._art == "channel":
            self.kanaele[self._attr.get("id", "")[:200]] = anzeigename(self._felder.get("display-name", ""))
            self._art = None
        elif name == "programme" and self._art == "programme":
            self._sendung()
            self._art = None

    def _sendung(self) -> None:
        name = self.kanaele.get(self._attr.get("channel") or "")
        if not name or (self.gewuenscht is not None and norm(name) not in self.gewuenscht):
            return
        eintraege = self.programm.setdefault(name, [])
        if len(eintraege) >= MAX_SENDUNGEN:
            return
        start, ende = _zeit(self._attr.get("start")), _zeit(self._attr.get("stop"))
        if start is None:
            return
        f = self._felder
        eintraege.append([start, ende, f.get("title", ""), f.get("sub-title", ""), f.get("desc", ""), f.get("category", "")])


def _keine_entitaeten(*_args: Any) -> None:
    raise EpgFehler("Datei deklariert eigene Entitäten")


def auswerten(pfad: str, auswahl: Iterable[str] | None, grenze: int = MAX_ENTPACKT) -> dict[str, Any]:
    """XMLTV-Datei lesen (blockierend, auch gzip-gepackt).

    Gibt alle Sendernamen zurück, aber das Programm nur der ausgewählten Sender (Vergleich über
    norm()). Der Speicherbedarf hängt nur von den ausgewählten Sendern ab, nicht von der Datei.
    """
    leser = _Leser({norm(s) for s in auswahl} if auswahl is not None else None)
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    # Ohne Aufschub meldet expat langen Text stückweise, statt ihn ganz zu puffern.
    if hasattr(parser, "SetReparseDeferralEnabled"):
        parser.SetReparseDeferralEnabled(False)
    parser.EntityDeclHandler = _keine_entitaeten
    parser.StartElementHandler = leser.start
    parser.EndElementHandler = leser.ende
    parser.CharacterDataHandler = leser.text
    gelesen = 0
    try:
        with open(pfad, "rb") as probe:
            gepackt = probe.read(2) == b"\x1f\x8b"
        with (gzip.open(pfad, "rb") if gepackt else open(pfad, "rb")) as datei:
            while stueck := datei.read(_STUECK):
                gelesen += len(stueck)
                if gelesen > grenze:
                    raise EpgFehler(f"Entpackt größer als {grenze // 1_000_000} MB")
                parser.Parse(stueck, False)
            parser.Parse(b"", True)
    except (expat.ExpatError, OSError, EOFError) as err:
        raise EpgFehler(f"Datei ist kein gültiges XMLTV: {str(err)[:100]}") from err
    if not leser.kanaele:
        raise EpgFehler("Datei enthält keine Sender")
    for eintraege in leser.programm.values():
        eintraege.sort(key=lambda e: e[0])
    return {"alle_sender": sorted(set(leser.kanaele.values()) - {""}, key=str.lower), "programm": leser.programm}
