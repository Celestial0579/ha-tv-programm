"""Hält das Programm aktuell und beantwortet Abfragen."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import epg
from .const import (
    CONF_SENDER,
    CONF_UHRZEIT,
    CONF_URL,
    CONF_URL_ERSATZ,
    DEFAULT_UHRZEIT,
    DEFAULT_URL,
    DOMAIN,
    MAX_TREFFER,
    SPITZNAMEN,
    STANDARD_SENDER,
)

_LOGGER = logging.getLogger(__name__)
_norm = epg.norm


WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _sendung(eintrag: list[Any] | None) -> dict[str, Any] | None:
    if eintrag is None:
        return None
    start = dt_util.as_local(dt_util.utc_from_timestamp(eintrag[0]))
    ende = dt_util.as_local(dt_util.utc_from_timestamp(eintrag[1])) if eintrag[1] else None
    ergebnis = {
        "titel": eintrag[2],
        "tag": f"{WOCHENTAGE[start.weekday()]}, {start.strftime('%d.%m.')}",
        "beginn": start.strftime("%H:%M"),
        "ende": ende.strftime("%H:%M") if ende else None,
    }
    if eintrag[3]:
        ergebnis["untertitel"] = eintrag[3]
    if eintrag[5]:
        ergebnis["genre"] = eintrag[5]
    if eintrag[4]:
        ergebnis["beschreibung"] = eintrag[4]
    return ergebnis


def _uhr(zeitstempel: float) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(zeitstempel)).strftime("%H:%M")


class Programm:
    """Eine Instanz je Eintrag."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.alle_sender: list[str] = []
        self.alle_ersatz: list[str] = []  # zuletzt bekannte Sender der Ersatzquelle, für die Auswahl
        self.aus_ersatz: list[str] = []  # Sender, deren Programm gerade aus der Ersatzquelle stammt
        self.programm: dict[str, list[list[Any]]] = {}
        self.stand: datetime | None = None
        self.fehler: str | None = None
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._datei = hass.config.path(".storage", f"{DOMAIN}.{entry.entry_id}.xml")
        self._datei_ersatz = hass.config.path(".storage", f"{DOMAIN}.{entry.entry_id}.ersatz.xml")
        self._zuhoerer: list[Callable[[], None]] = []
        self._abmelden: list[CALLBACK_TYPE] = []
        self._laeuft = False

    # ------------------------------------------------------------ Einstellungen
    @property
    def haupt_url(self) -> str:
        return str(self.entry.options.get(CONF_URL) or self.entry.data.get(CONF_URL) or DEFAULT_URL)

    @property
    def ersatz_url(self) -> str:
        if CONF_URL_ERSATZ in self.entry.options:
            return str(self.entry.options[CONF_URL_ERSATZ] or "")
        return str(self.entry.data.get(CONF_URL_ERSATZ) or "")

    @property
    def wunsch(self) -> list[str]:
        return list(self.entry.options.get(CONF_SENDER) or STANDARD_SENDER)

    @property
    def auswahl(self) -> list[str]:
        """Gewünschte Sender in der Schreibweise der Quelle, aus der sie kommen (Vergleich über _norm)."""
        if not self.alle_sender:
            return self.wunsch
        vorhanden = {_norm(s): s for s in self.alle_sender}
        return [vorhanden[_norm(s)] for s in self.wunsch if _norm(s) in vorhanden]

    @property
    def sendungen(self) -> int:
        return sum(len(e) for e in self.programm.values())

    @property
    def bis(self) -> datetime | None:
        enden = [e[-1][1] or e[-1][0] for e in self.programm.values() if e]
        return dt_util.utc_from_timestamp(max(enden)) if enden else None

    def zuhoerer_hinzufuegen(self, rueckruf: Callable[[], None]) -> Callable[[], None]:
        self._zuhoerer.append(rueckruf)
        return lambda: self._zuhoerer.remove(rueckruf)

    @callback
    def _melden(self) -> None:
        for rueckruf in list(self._zuhoerer):
            rueckruf()

    # ------------------------------------------------------------ Lebenszyklus
    async def async_start(self) -> None:
        daten = await self._store.async_load() or {}
        self.alle_sender = daten.get("alle_sender", [])
        self.alle_ersatz = daten.get("alle_ersatz", [])
        self.aus_ersatz = daten.get("aus_ersatz", [])
        self.programm = daten.get("programm", {})
        if daten.get("stand"):
            self.stand = dt_util.parse_datetime(daten["stand"])
        stunde, minute, *_ = (int(x) for x in str(self.entry.data.get(CONF_UHRZEIT, DEFAULT_UHRZEIT)).split(":"))
        self._abmelden.append(
            async_track_time_change(self.hass, self._taeglich, hour=stunde, minute=minute, second=0)
        )
        veraltet = self.stand is None or dt_util.utcnow() - self.stand > timedelta(hours=20)
        quelle_neu = daten.get("quellen") != [self.haupt_url, self.ersatz_url]
        if veraltet or quelle_neu or set(self.auswahl) - set(self.programm):
            self.hass.async_create_background_task(self.async_aktualisieren(), f"{DOMAIN}_start")

    async def async_stop(self) -> None:
        for abmelden in self._abmelden:
            abmelden()
        self._abmelden.clear()

    async def _taeglich(self, _jetzt: datetime) -> None:
        await self.async_aktualisieren()

    async def async_aktualisieren(self) -> None:
        if self._laeuft:
            return
        self._laeuft = True
        try:
            await self._laden()
        finally:
            self._laeuft = False
            await self.hass.async_add_executor_job(self._datei_weg)
            self._melden()

    async def _quelle(self, url: str, datei: str, auswahl: list[str]) -> dict[str, Any]:
        groesse = await epg.herunterladen(async_get_clientsession(self.hass), url, datei)
        daten = await self.hass.async_add_executor_job(epg.auswerten, datei, auswahl)
        _LOGGER.info("%s: %d MB, %d von %d gewünschten Sendern", url, groesse // 1_000_000, len(daten["programm"]), len(auswahl))
        return daten

    async def _laden(self) -> None:
        """Hauptquelle zuerst; aus der Ersatzquelle nur die Sender, die der Hauptquelle fehlen.

        Fällt die Hauptquelle aus, fehlen ihr alle Sender – dann liefert die Ersatzquelle alles.
        Liefern beide nichts, bleibt das bisherige Programm stehen.
        """
        wunsch = self.wunsch
        fehler: list[str] = []
        try:
            haupt = await self._quelle(self.haupt_url, self._datei, wunsch)
        except epg.EpgFehler as err:
            fehler.append(f"Hauptquelle: {err}")
            haupt = {"alle_sender": [], "programm": {}}
        gefunden = {_norm(k) for k in haupt["programm"]}
        fehlend = [s for s in wunsch if _norm(s) not in gefunden]
        ersatz: dict[str, Any] = {"alle_sender": [], "programm": {}}
        if fehlend and self.ersatz_url:
            try:
                ersatz = await self._quelle(self.ersatz_url, self._datei_ersatz, fehlend)
                self.alle_ersatz = ersatz["alle_sender"]
            except epg.EpgFehler as err:
                fehler.append(f"Ersatzquelle: {err}")
        if not haupt["programm"] and not ersatz["programm"]:
            self.fehler = "; ".join(fehler)[:200] or "Keine Sendungen der gewählten Sender gefunden"
            _LOGGER.warning("Programmdaten nicht geladen: %s", self.fehler)
            return
        haupt_norm = {_norm(s) for s in haupt["alle_sender"]}
        self.alle_sender = sorted(
            set(haupt["alle_sender"]) | {s for s in (ersatz["alle_sender"] or self.alle_ersatz) if _norm(s) not in haupt_norm},
            key=str.lower,
        )
        self.programm = {**haupt["programm"], **ersatz["programm"]}
        self.aus_ersatz = sorted(ersatz["programm"])
        self.stand = dt_util.utcnow()
        self.fehler = "; ".join(fehler)[:200] or None
        _LOGGER.info("Programm: %d Sender (%d aus der Ersatzquelle), %d Sendungen", len(self.programm), len(self.aus_ersatz), self.sendungen)
        await self._store.async_save(
            {
                "alle_sender": self.alle_sender, "alle_ersatz": self.alle_ersatz, "aus_ersatz": self.aus_ersatz,
                "programm": self.programm, "stand": self.stand.isoformat(), "quellen": [self.haupt_url, self.ersatz_url],
            }
        )

    def _datei_weg(self) -> None:
        for pfad in (self._datei, self._datei + ".teil", self._datei_ersatz, self._datei_ersatz + ".teil"):
            if os.path.exists(pfad):
                os.remove(pfad)

    # ------------------------------------------------------------ Abfragen
    def _sender_waehlen(self, wunsch: list[str] | None) -> list[str]:
        if not wunsch:
            return [s for s in self.auswahl if s in self.programm]
        ergebnis = []
        for teil in wunsch:
            t = _norm(teil)
            for vorsilbe in ("das", "die", "im", "in", "auf"):
                if t.startswith(vorsilbe) and SPITZNAMEN.get(t[len(vorsilbe):]):
                    t = t[len(vorsilbe):]
            t = SPITZNAMEN.get(t, t)
            exakt = [s for s in self.programm if _norm(s) == t]
            treffer = exakt or [s for s in self.programm if t and t in _norm(s)]
            ergebnis.extend(s for s in treffer if s not in ergebnis)
        if not ergebnis:
            raise HomeAssistantError(f"Keinen passenden Sender gefunden: {', '.join(wunsch)}")
        return ergebnis

    def uebersicht(self, zeitpunkt: datetime) -> list[dict[str, Any]]:
        """Knapp je Sender: was zum Zeitpunkt läuft und was danach kommt (für Sensoren und Dashboard)."""
        zeit = zeitpunkt.timestamp()
        liste = []
        for name in (s for s in self.auswahl if s in self.programm):
            eintraege = self.programm[name]
            jetzt = next((e for e in eintraege if e[0] <= zeit < (e[1] or e[0] + 1)), None)
            danach = next((e for e in eintraege if e[0] > zeit), None)
            if not (jetzt or danach):
                continue
            zeile: dict[str, Any] = {"sender": name}
            if jetzt:
                zeile.update(titel=jetzt[2], beginn=_uhr(jetzt[0]), ende=_uhr(jetzt[1]) if jetzt[1] else None)
            if danach:
                zeile.update(danach=danach[2], danach_beginn=_uhr(danach[0]))
            liste.append(zeile)
        return liste

    def abfragen(self, zeitpunkt: datetime | None, sender: list[str] | None, suche: str | None) -> dict[str, Any]:
        if not self.programm:
            raise HomeAssistantError("Noch keine Programmdaten geladen.")
        zeit = (zeitpunkt or dt_util.now()).timestamp()
        auswahl = self._sender_waehlen(sender)
        if suche:
            wort = suche.strip().lower()
            treffer = []
            for name in auswahl:
                for e in self.programm[name]:
                    if (e[1] or e[0]) >= zeit and (wort in e[2].lower() or wort in e[3].lower()):
                        treffer.append((e[0], name, e))
            treffer.sort(key=lambda x: x[0])
            return {
                "suche": suche,
                "treffer": [{"sender": name, **_sendung(e)} for _t, name, e in treffer[:MAX_TREFFER]],
                "weitere": max(0, len(treffer) - MAX_TREFFER),
            }
        liste = []
        for name in auswahl:
            eintraege = self.programm[name]
            jetzt = next((e for e in eintraege if e[0] <= zeit < (e[1] or e[0] + 1)), None)
            danach = next((e for e in eintraege if e[0] > zeit), None)
            if jetzt or danach:
                laufend = _sendung(jetzt)
                if laufend:
                    laufend.pop("beschreibung", None) if len(auswahl) > 3 else None
                folgend = _sendung(danach)
                if folgend:
                    folgend.pop("beschreibung", None)
                liste.append({"sender": name, "laeuft": laufend, "danach": folgend})
        lokal = dt_util.as_local(dt_util.utc_from_timestamp(zeit))
        return {"zeitpunkt": f"{WOCHENTAGE[lokal.weekday()]}, {lokal.strftime('%d.%m. %H:%M')}", "sender": liste}
