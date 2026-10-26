"""TV-Programm: Fernsehprogramm aus einer XMLTV-Quelle, abfragbar für Automationen und den Sprachassistenten."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import ALTE_URL, CONF_URL, CONF_URL_ERSATZ, DEFAULT_URL, DIENST_ABFRAGEN, DOMAIN
from .programm import Programm

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TvProgrammConfigEntry = ConfigEntry[Programm]

ABFRAGEN_SCHEMA = vol.Schema(
    {
        vol.Optional("zeitpunkt"): cv.string,
        vol.Optional("sender"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("suche"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def abfragen(aufruf: ServiceCall) -> ServiceResponse:
        eintraege = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
        if not eintraege:
            raise ServiceValidationError("TV-Programm ist nicht eingerichtet.")
        zeitpunkt = None
        if text := str(aufruf.data.get("zeitpunkt") or "").strip():
            zeitpunkt = dt_util.parse_datetime(text)
            if zeitpunkt is None:
                uhr = dt_util.parse_time(text)
                if uhr is None:
                    raise ServiceValidationError(f"Zeitpunkt nicht verstanden: {text}")
                zeitpunkt = dt_util.now().replace(hour=uhr.hour, minute=uhr.minute, second=0, microsecond=0)
            elif zeitpunkt.tzinfo is None:
                zeitpunkt = zeitpunkt.replace(tzinfo=dt_util.get_default_time_zone())
        programm: Programm = eintraege[0].runtime_data
        return programm.abfragen(zeitpunkt, aufruf.data.get("sender"), aufruf.data.get("suche"))

    hass.services.async_register(
        DOMAIN, DIENST_ABFRAGEN, abfragen, schema=ABFRAGEN_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 -> v2: Ersatzquelle. Wer die alte Vorgabe nutzte, bekommt die neue Hauptquelle und die alte als Ersatz."""
    if entry.version == 1:
        daten = dict(entry.data)
        if daten.get(CONF_URL) == ALTE_URL:
            daten[CONF_URL], daten[CONF_URL_ERSATZ] = DEFAULT_URL, ALTE_URL
        else:
            daten.setdefault(CONF_URL_ERSATZ, "")
        hass.config_entries.async_update_entry(entry, data=daten, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TvProgrammConfigEntry) -> bool:
    programm = Programm(hass, entry)
    entry.runtime_data = programm
    await programm.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_optionen_geaendert))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TvProgrammConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return ok


async def _optionen_geaendert(hass: HomeAssistant, entry: TvProgrammConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
