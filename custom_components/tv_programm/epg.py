"""Laden und Auswerten von XMLTV-Programmdaten."""

from __future__ import annotations

from datetime import datetime
import os
import re
from typing import Any
import xml.etree.ElementTree as ET

import aiohttp

USER_AGENT = "HomeAssistant-tv_programm/0.1 (+https://github.com/Celestial0579/ha-tv-programm)"


class EpgFehler(Exception):
    """Programmdaten nicht ladbar oder unbrauchbar."""


def anzeigename(roh: str) -> str:
    """„DE - Das Erste“ -> „Das Erste“."""
    return re.sub(r"^[A-Z]{2,3}\s*[-|:]\s*", "", (roh or "").strip())


def _zeit(wert: str | None) -> float | None:
    if not wert:
        return None
    try:
        return datetime.strptime(wert.strip()[:20], "%Y%m%d%H%M%S %z").timestamp()
    except ValueError:
        try:
            return datetime.strptime(wert.strip()[:14], "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            return None


async def herunterladen(sitzung: aiohttp.ClientSession, url: str, ziel: str) -> int:
    """XMLTV-Datei herunterladen (folgt Weiterleitungen), Größe in Byte."""
    try:
        async with sitzung.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=600)
        ) as antwort:
            if antwort.status != 200:
                raise EpgFehler(f"HTTP {antwort.status}")
            groesse = 0
            with open(ziel + ".teil", "wb") as datei:  # noqa: ASYNC230 – blockfrei genug für Streaming in Stücken
                async for stueck in antwort.content.iter_chunked(1 << 16):
                    datei.write(stueck)
                    groesse += len(stueck)
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        raise EpgFehler(str(err) or type(err).__name__) from err
    os.replace(ziel + ".teil", ziel)
    return groesse


def auswerten(pfad: str, auswahl: list[str] | None) -> dict[str, Any]:
    """XMLTV-Datei lesen (blockierend). Gibt alle Sendernamen und das Programm der ausgewählten Sender zurück."""
    kanaele: dict[str, str] = {}
    programm: dict[str, list[list[Any]]] = {}
    gewuenscht = {s.lower() for s in auswahl} if auswahl else None
    try:
        for _ereignis, element in ET.iterparse(pfad, events=("end",)):
            if element.tag == "channel":
                kanaele[element.get("id") or ""] = anzeigename(element.findtext("display-name") or "")
                element.clear()
            elif element.tag == "programme":
                name = kanaele.get(element.get("channel") or "")
                if name and (gewuenscht is None or name.lower() in gewuenscht):
                    start, ende = _zeit(element.get("start")), _zeit(element.get("stop"))
                    if start is not None:
                        programm.setdefault(name, []).append(
                            [
                                start,
                                ende,
                                (element.findtext("title") or "").strip(),
                                (element.findtext("sub-title") or "").strip(),
                                re.sub(r"\s+", " ", (element.findtext("desc") or "").strip())[:300],
                                (element.findtext("category") or "").strip(),
                            ]
                        )
                element.clear()
    except ET.ParseError as err:
        raise EpgFehler(f"Datei ist kein gültiges XMLTV: {err}") from err
    if not kanaele:
        raise EpgFehler("Datei enthält keine Sender")
    for eintraege in programm.values():
        eintraege.sort(key=lambda e: e[0])
    return {"alle_sender": sorted(set(kanaele.values()), key=str.lower), "programm": programm}
