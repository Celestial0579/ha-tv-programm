"""Laden und Auswerten von XMLTV-Programmdaten.

Schutzmodell: Die XMLTV-Datei kommt von einer fremden Adresse (Gemeinschaftsangebote mit
unbekannten Betreibern, eigene Adressen auch über http://). Ihr Inhalt ist fremde Eingabe.
Wer eine Quelle kontrolliert, soll Home Assistant weder den Datenträger füllen noch den
Speicher sprengen können. Deshalb ist alles begrenzt: die heruntergeladene Menge, die
entpackte Menge, die Länge jedes Texts und jedes einzelnen Tokens der Auszeichnung, die Tiefe der Verschachtelung, die Zahl der Sender, die Sendungen je Sender (alle
Schreibweisen zusammen) und die Sendungen insgesamt. Gelesen wird ohne
Baum (expat direkt), Dokumente mit eigenen Entitäten werden abgelehnt.

Auch die Weiterleitungen einer Quelle sind fremde Eingabe (GHSA-m7cx-3q6v-c42v): Sie werden
selbst verfolgt, höchstens MAX_WEITERLEITUNGEN, nie von https auf http und von einer
öffentlichen Quelle nie ins lokale Netz (Loopback, privat, Link-Local, ULA, Multicast,
reserviert). Eine eigene Quelle im Heimnetz (Tvheadend) darf innerhalb des Heimnetzes
weiterleiten. Ob eine Quelle im Heimnetz liegt, steht allein in der eingetragenen Adresse
(IP-Adresse, localhost, .local, .lan, .home.arpa), nie im DNS: Wer das DNS einer Quelle
beherrscht, könnte sonst bei der Prüfung „privat“ antworten und so jede Prüfung abschalten.
Die Vorgabe-Quellen gelten immer als öffentlich. Beim Verbinden prüft der Resolver der
eigenen Sitzung jede Adresse noch einmal (DNS-Rebinding zwischen Prüfung und Verbindung).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
from typing import Any
from urllib.parse import urlsplit
from xml.parsers import expat
import zlib

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from homeassistant.util.ssl import client_context

from .const import DEFAULT_URL, DEFAULT_URL_ERSATZ

FASSUNG = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))["version"]
USER_AGENT = f"HomeAssistant-tv_programm/{FASSUNG} (+https://github.com/Celestial0579/ha-tv-programm)"

MAX_DOWNLOAD = 200_000_000  # Byte; die große Vorgabe-Quelle hat rund 45 MB
MAX_ENTPACKT = 1_000_000_000  # Byte nach dem Entpacken
MAX_TEXT = 1000  # Zeichen, die je Feld überhaupt gesammelt werden
MAX_SENDUNGEN = 5000  # je Sender (alle Schreibweisen zusammen); fünf Tage haben selten mehr als 300
MAX_SENDER = 5000  # Sender in einer Datei; die großen Sammelquellen haben einige Hundert
MAX_GESAMT = 50_000  # gespeicherte Sendungen insgesamt
MAX_TOKEN = 1_000_000  # Byte, die expat ohne Ereignis puffern darf (ein Tag, ein Attributwert, ein Name)
MAX_TIEFE = 8  # Verschachtelung; XMLTV kommt mit 3 Ebenen aus (tv > programme > title)
MAX_WEITERLEITUNGEN = 5
WEITERLEITUNG = (301, 302, 303, 307, 308)
ZEIT_GESAMT = 600  # Sekunden für Weiterleitungen und Herunterladen zusammen
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


ZEIT_BIS = 4_102_444_800  # 2100-01-01 UTC: spätere und frühere Zeiten (vor 1970) sind unplausibel (#13)


def plausibel(zeit: Any) -> bool:
    """Zeitstempel zwischen 1970 und 2099: Den rechnet Home Assistant sicher in Ortszeit um (#13)."""
    return isinstance(zeit, (int, float)) and 0 <= zeit < ZEIT_BIS


def _zeit(wert: str | None) -> float | None:
    """XMLTV-Zeit; ohne Angabe der Zeitzone gilt laut XMLTV UTC. Unplausible Zeiten ergeben None."""
    if not wert:
        return None
    try:
        zeit = datetime.strptime(wert.strip()[:20], "%Y%m%d%H%M%S %z").timestamp()
    except (ValueError, OverflowError):
        try:
            zeit = datetime.strptime(wert.strip()[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
        except (ValueError, OverflowError):
            return None
    return zeit if plausibel(zeit) else None


def ohne_zugang(url: str) -> str:
    """Adresse zum Anzeigen: nur Schema, Rechner, Port und Pfad, ohne Zugangsdaten und Query.

    Eigene Quellen (etwa Tvheadend) tragen oft „user:pass@“ oder ein Token im Query.
    """
    try:
        teile = urlsplit(url)
        rechner = teile.hostname or ""
        if ":" in rechner:
            rechner = f"[{rechner}]"
        port = f":{teile.port}" if teile.port else ""
    except ValueError:
        return "(ungültige Adresse)"
    return f"{teile.scheme}://{rechner}{port}{teile.path}"


def _fehlertext(err: BaseException) -> str:
    """Nur Art und Status, nie der Text der Ausnahme: aiohttp nennt darin die volle Adresse."""
    if isinstance(err, aiohttp.ClientResponseError):
        return f"{type(err).__name__} (HTTP {err.status})"
    if isinstance(err, aiohttp.ClientError) or not isinstance(err, OSError) or not err.strerror:
        return type(err).__name__
    return f"{type(err).__name__}: {err.strerror}"  # Datenträger: errno-Text ohne Pfad


def _nicht_oeffentlich(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, privat, Link-Local, ULA, Multicast, reserviert oder unbestimmt (auch als ::ffff:a.b.c.d)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified


async def _adressen(rechner: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """IP-Adressen eines Rechners; eine IP-Adresse steht für sich selbst."""
    try:
        return [ipaddress.ip_address(rechner)]
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(rechner, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


def _name(rechner: str) -> str:
    return rechner.lower().rstrip(".")


def _localhost(rechner: str) -> bool:
    name = _name(rechner)
    return name == "localhost" or name.endswith(".localhost")


HEIMNETZ_ENDUNGEN = (".local", ".lan", ".home.arpa")
_VORGABE_RECHNER = frozenset(_name(URL(u).host or "") for u in (DEFAULT_URL, DEFAULT_URL_ERSATZ))


def quelle_lokal(url: URL) -> bool:
    """Eingestellte Quelle im Heimnetz? Nur nach der Adresse selbst, ohne DNS (GHSA-m7cx-3q6v-c42v).

    Lokal ist eine private IP-Adresse, localhost oder ein Name auf .local, .lan oder .home.arpa.
    Die Vorgabe-Quellen sind es nie. Ein anderer Name gilt als öffentlich, auch wenn ihn das
    DNS im Heimnetz auflöst: Seine Weiterleitungen dürfen dann nicht ins Heimnetz.
    """
    rechner = _name(url.host or "")
    if not rechner or rechner in _VORGABE_RECHNER:
        return False
    if _localhost(rechner) or rechner.endswith(HEIMNETZ_ENDUNGEN):
        return True
    try:
        return _nicht_oeffentlich(ipaddress.ip_address(rechner.split("%", 1)[0]))
    except ValueError:
        return False


def _innerer_aufloeser() -> AbstractResolver:
    return aiohttp.DefaultResolver()


class _Aufloeser(AbstractResolver):
    """Resolver der eigenen Sitzung: prüft jede Adresse beim Verbinden (gegen DNS-Rebinding).

    Ist die Quelle nicht lokal, darf keine aufgelöste Adresse nicht öffentlich sein. Einzige
    Ausnahme ist die erste Auflösung des eingetragenen Rechners selbst: Ein Name, den das DNS
    im Heimnetz privat auflöst, bleibt als Quelle nutzbar. Jede weitere Auflösung prüft auch
    ihn; bis dahin verbindet aiohttp aus dem DNS-Zwischenspeicher zur selben Adresse.
    """

    def __init__(self, lokal_erlaubt: bool, start: str | None) -> None:
        self._innen = _innerer_aufloeser()
        self._lokal_erlaubt = lokal_erlaubt
        self._start = _name(start) if start else None

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        eintraege = await self._innen.resolve(host, port, family)
        if self._lokal_erlaubt:
            return eintraege
        if self._start is not None and _name(host) == self._start:
            self._start = None
            return eintraege
        if _localhost(host) or not eintraege or any(
            _nicht_oeffentlich(ipaddress.ip_address(str(e["host"]).split("%", 1)[0])) for e in eintraege
        ):
            raise OSError("Ziel im lokalen Netz")  # nie die Adresse im Text
        return eintraege

    async def close(self) -> None:
        await self._innen.close()


def eigene_sitzung(url: str) -> aiohttp.ClientSession:
    """Sitzung nur für einen Abruf, mit dem prüfenden Resolver. Der Aufrufer schließt sie."""
    try:
        start = URL(url)
        lokal = quelle_lokal(start)
    except (ValueError, TypeError):
        start, lokal = None, False
    aufloeser = _Aufloeser(lokal, start.host if start is not None else None)
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aufloeser, ssl=client_context()))


async def _lokal(rechner: str, port: int) -> bool:
    """Ziel einer Weiterleitung: lokal, sobald eine Adresse nicht öffentlich ist. Nicht auflösbar gilt als lokal."""
    if _localhost(rechner):
        return True
    try:
        adressen = await _adressen(rechner, port)
    except (OSError, ValueError):
        return True
    return not adressen or any(_nicht_oeffentlich(a) for a in adressen)


async def _weiterleitung_abgelehnt(alt: URL, neu: URL, start_lokal: bool) -> str | None:
    """Grund, aus dem eine Weiterleitung nicht verfolgt wird, sonst None. Nie mit der Adresse im Text."""
    if neu.scheme not in ("http", "https") or not neu.host:
        return "kein http(s)"
    if alt.scheme == "https" and neu.scheme == "http":
        return "von https auf http"
    if not start_lokal and await _lokal(neu.host, neu.port or 80):
        return "Ziel im lokalen Netz"
    return None


async def _oeffnen(sitzung: aiohttp.ClientSession, url: str) -> aiohttp.ClientResponse:
    """GET mit selbst verfolgten Weiterleitungen (GHSA-m7cx-3q6v-c42v). Der Aufrufer gibt die Antwort frei."""
    try:
        aktuell = URL(url)
    except (ValueError, TypeError) as err:
        raise EpgFehler("Adresse ungültig") from err
    if aktuell.scheme not in ("http", "https") or not aktuell.host:
        raise EpgFehler("Adresse ist kein http(s)")
    start_lokal = quelle_lokal(aktuell)
    for _ in range(MAX_WEITERLEITUNGEN + 1):
        antwort = await sitzung.get(
            aktuell, headers={"User-Agent": USER_AGENT}, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=ZEIT_GESAMT),
        )
        if antwort.status not in WEITERLEITUNG:
            return antwort
        ort = antwort.headers.get("Location", "")
        antwort.release()
        try:
            neu = aktuell.join(URL(ort))
        except (ValueError, TypeError) as err:
            raise EpgFehler("Weiterleitung abgelehnt: ungültiges Ziel") from err
        if not ort or (grund := await _weiterleitung_abgelehnt(aktuell, neu, start_lokal)):
            raise EpgFehler(f"Weiterleitung abgelehnt: {grund if ort else 'ohne Ziel'}")
        aktuell = neu
    raise EpgFehler(f"Mehr als {MAX_WEITERLEITUNGEN} Weiterleitungen")


async def herunterladen(sitzung: aiohttp.ClientSession, url: str, ziel: str, grenze: int = MAX_DOWNLOAD) -> int:
    """XMLTV-Datei herunterladen, Größe in Byte.

    Weiterleitungen verfolgt _oeffnen, mit den Grenzen aus dem Schutzmodell oben.
    Dateizugriffe laufen im Executor, nicht in der Ereignisschleife.
    """
    schleife = asyncio.get_running_loop()
    teil = ziel + ".teil"
    datei = None
    groesse = 0
    antwort: aiohttp.ClientResponse | None = None
    try:
        async with asyncio.timeout(ZEIT_GESAMT):
            antwort = await _oeffnen(sitzung, url)
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
        await schleife.run_in_executor(None, os.replace, teil, ziel)
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        await schleife.run_in_executor(None, _weg, datei, teil)
        raise EpgFehler(_fehlertext(err)) from err
    except EpgFehler:
        await schleife.run_in_executor(None, _weg, datei, teil)
        raise
    except asyncio.CancelledError:
        # Abbruch beim Entladen (#18): kein await mehr möglich, also direkt aufräumen.
        _weg(datei, teil)
        raise
    finally:
        if antwort is not None:
            antwort.release()
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
        self._namen: dict[str, str] = {}  # norm(name) -> erste Schreibweise, unter der gespeichert wird
        self._gesamt = 0
        self._art: str | None = None
        self._attr: dict[str, str] = {}
        self._felder: dict[str, str] = {}
        self._feld: str | None = None
        self._text: list[str] = []
        self._laenge = 0
        self._tiefe = 0

    def start(self, name: str, attr: dict[str, str]) -> None:
        # Jedes offene Element kostet expat Speicher, und jedes Start-Tag ist ein Ereignis,
        # MAX_TOKEN greift also nicht: Die Tiefe selbst begrenzen (GHSA-m8r3-c533-2xfq).
        self._tiefe += 1
        if self._tiefe > MAX_TIEFE:
            raise EpgFehler(f"Datei ist tiefer als {MAX_TIEFE} Ebenen verschachtelt")
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
        self._tiefe -= 1
        if self._feld is not None and name == self._feld:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self._felder[name] = text[: LAENGEN[name]]
            self._feld = None
        elif name == "channel" and self._art == "channel":
            kennung = self._attr.get("id", "")[:200]
            if kennung not in self.kanaele and len(self.kanaele) >= MAX_SENDER:
                raise EpgFehler(f"Datei enthält mehr als {MAX_SENDER} Sender")
            self.kanaele[kennung] = anzeigename(self._felder.get("display-name", ""))
            self._art = None
        elif name == "programme" and self._art == "programme":
            self._sendung()
            self._art = None

    def _sendung(self) -> None:
        name = self.kanaele.get(self._attr.get("channel") or "")
        if not name:
            return
        schluessel = norm(name)
        if self.gewuenscht is not None and schluessel not in self.gewuenscht:
            return
        if self._gesamt >= MAX_GESAMT:
            return
        # Alle Schreibweisen eines Senders teilen sich eine Liste und damit ein Kontingent.
        eintraege = self.programm.setdefault(self._namen.setdefault(schluessel, name), [])
        if len(eintraege) >= MAX_SENDUNGEN:
            return
        start, ende = _zeit(self._attr.get("start")), _zeit(self._attr.get("stop"))
        if start is None:
            return
        if ende is not None and ende <= start:
            ende = None  # Ende vor dem Beginn: wie ohne Ende behandeln
        f = self._felder
        self._gesamt += 1
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
    # Ohne Aufschub meldet expat langen Text stückweise, statt ihn ganz zu puffern. Dafür setzt
    # expat bei jedem Stück am Anfang eines unvollständigen Tokens neu an; MAX_TOKEN hält diesen
    # Aufwand (und den Puffer) klein: höchstens MAX_TOKEN / _STUECK Durchläufe über MAX_TOKEN Byte.
    if hasattr(parser, "SetReparseDeferralEnabled"):
        parser.SetReparseDeferralEnabled(False)
    letztes = [0]  # Byteposition des letzten Ereignisses

    def gemerkt(handler):
        def aufruf(*args: Any) -> None:
            letztes[0] = parser.CurrentByteIndex
            handler(*args)
        return aufruf

    def nur_merken(*_args: Any) -> None:
        letztes[0] = parser.CurrentByteIndex

    parser.EntityDeclHandler = _keine_entitaeten
    parser.StartElementHandler = gemerkt(leser.start)
    parser.EndElementHandler = gemerkt(leser.ende)
    parser.CharacterDataHandler = gemerkt(leser.text)
    parser.CommentHandler = nur_merken
    parser.ProcessingInstructionHandler = nur_merken
    parser.StartDoctypeDeclHandler = nur_merken
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
                if gelesen - letztes[0] > MAX_TOKEN:
                    raise EpgFehler(f"Datei enthält ein Token über {MAX_TOKEN // 1_000_000} MB")
            parser.Parse(b"", True)
    except (expat.ExpatError, OSError, EOFError, zlib.error) as err:  # zlib.error: beschädigtes gzip
        raise EpgFehler(f"Datei ist kein gültiges XMLTV: {str(err)[:100]}") from err
    if not leser.kanaele:
        raise EpgFehler("Datei enthält keine Sender")
    for eintraege in leser.programm.values():
        eintraege.sort(key=lambda e: e[0])
    return {"alle_sender": sorted(set(leser.kanaele.values()) - {""}, key=str.lower), "programm": leser.programm}
