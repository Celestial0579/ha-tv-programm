"""Sensor: Stand der Programmdaten."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ProgrammEntitaet
from .programm import Programm


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([StandSensor(entry.runtime_data)])


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
            "fehler": self._programm.fehler,
        }
