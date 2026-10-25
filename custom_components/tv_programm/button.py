"""Knopf: Programmdaten jetzt neu laden."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ProgrammEntitaet
from .programm import Programm


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([AktualisierenKnopf(entry.runtime_data)])


class AktualisierenKnopf(ProgrammEntitaet, ButtonEntity):
    _attr_icon = "mdi:refresh"

    def __init__(self, programm: Programm) -> None:
        super().__init__(programm, "aktualisieren")

    async def async_press(self) -> None:
        self.hass.async_create_background_task(self._programm.async_aktualisieren(), "tv_programm_knopf")
