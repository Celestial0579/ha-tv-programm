"""Gemeinsame Basis der Entitäten."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .programm import Programm


class ProgrammEntitaet(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, programm: Programm, schluessel: str) -> None:
        self._programm = programm
        self._attr_translation_key = schluessel
        self._attr_unique_id = f"{programm.entry.entry_id}_{schluessel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, programm.entry.entry_id)}, name="TV-Programm", entry_type=DeviceEntryType.SERVICE
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._programm.zuhoerer_hinzufuegen(self._aktualisieren))

    @callback
    def _aktualisieren(self) -> None:
        self.async_write_ha_state()
