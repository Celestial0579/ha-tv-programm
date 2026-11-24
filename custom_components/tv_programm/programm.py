"""Hält das Programm aktuell und beantwortet Abfragen."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import glob
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
        # Je Ladelauf eigene Dateien (#18): Eine alte Instanz, die beim Neuladen noch
        # aufräumt, trifft so nie die Dateien der neuen.
        self._datei = self._pfad("start", "")
        self._datei_ersatz = self._pfad("start", ".ersatz")
        self._zuhoerer: list[Callable[[], None]] = []
        self._abmelden: list[CALLBACK_TYPE] = []
        self._laeuft = False
        self._gespeichert: dict[str, Any] = {}

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

    def gewaehlt(self) -> list[str]:
        """Gewählte Sender mit Programm, in der Schreibweise des Programms (Vergleich über _norm)."""
        im_programm = {_norm(k): k for k in self.programm}
        ergebnis: list[str] = []
        for s in self.auswahl:
            if (name := im_programm.get(_norm(s))) is not None and name not in ergebnis:
                ergebnis.append(name)
        return ergebnis

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
    def _pfad(self, lauf: str, art: str) -> str:
        return self.hass.config.path(".storage", f"{DOMAIN}.{self.entry.entry_id}.{lauf}{art}.xml")

    def starten(self, name: str) -> None:
        """Laden als Hintergrundaufgabe des Eintrags.

        Home Assistant bricht sie beim Entladen ab und wartet bis zu 10 s darauf (#18).
        """
        self.entry.async_create_background_task(self.hass, self.async_aktualisieren(), f"{DOMAIN}_{name}")

    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._reste_weg)
        daten = await self._store.async_load() or {}
        if "quellen" in daten:
            # Bis 0.3.x standen hier die vollen Adressen, samt möglicher Zugangsdaten (GHSA-ppx8-wvfc-f452).
            # Nur noch den Fingerabdruck behalten und sofort überschreiben.
            alt = daten.pop("quellen")
            daten["quellen_fp"] = _fingerabdruck(*(list(alt) + ["", ""])[:2]) if isinstance(alt, list) else None
            await self._store.async_save(daten)
        self._gespeichert = daten
        self.alle_sender = daten.get("alle_sender", [])
        self.alle_ersatz = daten.get("alle_ersatz", [])
        self.aus_ersatz = daten.get("aus_ersatz", [])
        # Gespeicherte Sendungen mit unplausiblen Zeiten (vor #13 geladen) nicht übernehmen.
        self.programm = {
            name: [e for e in eintraege if epg.plausibel(e[0]) and (not e[1] or epg.plausibel(e[1]))]
            for name, eintraege in (daten.get("programm") or {}).items()
        }
        if daten.get("stand"):
            self.stand = dt_util.parse_datetime(daten["stand"])
        stunde, minute, *_ = (int(x) for x in str(self.entry.data.get(CONF_UHRZEIT, DEFAULT_UHRZEIT)).split(":"))
        self._abmelden.append(
            async_track_time_change(self.hass, self._taeglich, hour=stunde, minute=minute, second=0)
        )
        if self.beim_start_laden(daten):
            self.starten("start")

    @property
    def _quellen_fp(self) -> str:
        return _fingerabdruck(self.haupt_url, self.ersatz_url)

    def beim_start_laden(self, daten: dict[str, Any]) -> bool:
        """Nur laden, wenn es nötig ist: alt, andere Quelle oder neu gewünschte Sender.

        Ein Sender, der in der Quelle ohne Programm steht, ist kein Grund: Er hätte auch
        beim nächsten Laden keins. Nach einem Fehlschlag frühestens nach einer Stunde.
        """
        veraltet = self.stand is None or dt_util.utcnow() - self.stand > timedelta(hours=20)
        quelle_neu = daten.get("quellen_fp") != self._quellen_fp
        vorher = {_norm(w) for w in daten.get("wunsch", self.wunsch)}
        neu_gewuenscht = bool({_norm(w) for w in self.wunsch} - vorher)
        zu_frueh = time.time() - float(daten.get("versuch") or 0) < 3600
        return quelle_neu or neu_gewuenscht or (veraltet and not zu_frueh)

    async def async_stop(self) -> None:
        for abmelden in self._abmelden:
            abmelden()
        self._abmelden.clear()

    @callback
    def _taeglich(self, _jetzt: datetime) -> None:
        self.starten("taeglich")

    async def async_aktualisieren(self) -> None:
        if self._laeuft:
            return
        self._laeuft = True
        lauf = secrets.token_hex(4)
        self._datei, self._datei_ersatz = self._pfad(lauf, ""), self._pfad(lauf, ".ersatz")
        dateien = (self._datei, self._datei_ersatz)
        try:
            await self._laden()
        finally:
            self._laeuft = False
            await self.hass.async_add_executor_job(_datei_weg, dateien)
            self._melden()

    async def _quelle(self, url: str, datei: str, auswahl: list[str]) -> dict[str, Any]:
        async with epg.eigene_sitzung(url) as sitzung:  # Resolver prüft beim Verbinden (GHSA-m7cx-3q6v-c42v)
            groesse = await epg.herunterladen(sitzung, url, datei)
        daten = await self.hass.async_add_executor_job(epg.auswerten, datei, auswahl)
        _LOGGER.info("%s: %d MB, %d von %d gewünschten Sendern", epg.ohne_zugang(url), groesse // 1_000_000, len(daten["programm"]), len(auswahl))
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
            # Den Versuch merken, damit nicht jeder Neustart sofort wieder lädt.
            await self._store.async_save({**self._gespeichert, "versuch": time.time(), "quellen_fp": self._quellen_fp, "wunsch": wunsch})
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
        self._gespeichert = {
            "alle_sender": self.alle_sender, "alle_ersatz": self.alle_ersatz, "aus_ersatz": self.aus_ersatz,
            "programm": self.programm, "stand": self.stand.isoformat(), "quellen_fp": self._quellen_fp,
            "wunsch": wunsch, "versuch": time.time(),
        }
        await self._store.async_save(self._gespeichert)

    def _reste_weg(self) -> None:
        """Dateien abgebrochener Läufe: frühere feste Namen und Laufdateien älter als eine Stunde."""
        dateien_weg(self.hass, self.entry.entry_id, aelter_als=3600)

    # ------------------------------------------------------------ Abfragen
    def _sender_waehlen(self, wunsch: list[str] | None) -> list[str]:
        if not wunsch:
            return self.gewaehlt()
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
        for name in self.gewaehlt():
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


def _datei_weg(dateien: tuple[str, ...]) -> None:
    """Nur die Dateien des eigenen Laufs löschen."""
    for datei in dateien:
        for pfad in (datei, datei + ".teil"):
            if os.path.exists(pfad):
                os.remove(pfad)


def _fingerabdruck(haupt: str, ersatz: str) -> str:
    """Erkennt eine geänderte Quelle, ohne die Adressen (samt Zugangsdaten) zu speichern."""
    return hashlib.sha256(json.dumps([haupt, ersatz]).encode()).hexdigest()


def dateien_weg(hass: HomeAssistant, entry_id: str, aelter_als: float | None = None) -> None:
    """Heruntergeladene Dateien des Eintrags löschen; mit aelter_als nur ältere (Sekunden)."""
    jetzt = time.time()
    basis = hass.config.path(".storage", f"{DOMAIN}.{entry_id}")
    for pfad in glob.glob(glob.escape(basis) + ".*"):
        if not pfad.endswith((".xml", ".xml.teil")):
            continue  # z. B. der Store selbst
        try:
            if aelter_als is None or jetzt - os.path.getmtime(pfad) > aelter_als:
                os.remove(pfad)
        except OSError:
            pass
