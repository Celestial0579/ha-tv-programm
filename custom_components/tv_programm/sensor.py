"""Sensoren: Stand der Programmdaten, was jetzt läuft und was heute um 20:15 kommt."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import ABENDZEIT
from .entity import ProgrammEntitaet
from .programm import WOCHENTAGE, Programm


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    programm = entry.runtime_data
    async_add_entities([StandSensor(programm), JetztSensor(programm), AbendSensor(programm)])


class StandSensor(ProgrammEntitaet, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:television-guide"

    def __init__(self, programm: Programm) -> None:
        super().__init__(programm, "stand")

    @property
    def native_value(self) -> datetime | None:
        return self._programm.stand

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        bis = self._programm.bis
        return {
            "sender": len(self._programm.programm),
            "sendungen": self._programm.sendungen,
            "programm_bis": bis.isoformat() if bis else None,
            "aus_ersatzquelle": self._programm.aus_ersatz,
            "fehler": self._programm.fehler,
        }


class _UebersichtSensor(ProgrammEntitaet, SensorEntity):
    """Zustand = Zahl der Sender mit Programm, Attribut „sender“ = Liste je Sender (nicht in der Datenbank)."""

    _unrecorded_attributes = frozenset({"sender", "zeitpunkt"})

    def _zeitpunkt(self) -> datetime:
        raise NotImplementedError

    @property
    def native_value(self) -> int:
        return len(self._liste())

    def _liste(self) -> list[dict[str, Any]]:
        return self._programm.uebersicht(self._zeitpunkt()) if self._programm.programm else []

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zeit = self._zeitpunkt()
        return {"zeitpunkt": f"{WOCHENTAGE[zeit.weekday()]}, {zeit.strftime('%d.%m. %H:%M')}", "sender": self._liste()}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_change(self.hass, self._minute, second=0))

    @callback
    def _minute(self, _jetzt: datetime) -> None:
        self.async_write_ha_state()


class JetztSensor(_UebersichtSensor):
    _attr_icon = "mdi:television-play"

    def __init__(self, programm: Programm) -> None:
        super().__init__(programm, "jetzt")

    def _zeitpunkt(self) -> datetime:
        return dt_util.now().replace(second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # ohne Uhrzeit: sonst ändert sich das Attribut jede Minute, obwohl dasselbe läuft
        return {"sender": self._liste()}


class AbendSensor(_UebersichtSensor):
    _attr_icon = "mdi:television-classic"

    def __init__(self, programm: Programm) -> None:
        super().__init__(programm, "abend")

    def _zeitpunkt(self) -> datetime:
        stunde, minute = ABENDZEIT
        heute = dt_util.now().replace(hour=stunde, minute=minute, second=0, microsecond=0)
        # nach Mitternacht bis 05:00 noch der vergangene Abend, danach der kommende
        return heute - timedelta(days=1) if dt_util.now().hour < 5 else heute
