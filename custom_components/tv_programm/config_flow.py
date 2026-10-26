"""Einrichtung über die Oberfläche."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_SENDER,
    CONF_UHRZEIT,
    CONF_URL,
    CONF_URL_ERSATZ,
    DEFAULT_UHRZEIT,
    DEFAULT_URL,
    DEFAULT_URL_ERSATZ,
    DOMAIN,
)


def _adressen_pruefen(eingabe: dict[str, Any]) -> dict[str, str]:
    fehler: dict[str, str] = {}
    if not str(eingabe.get(CONF_URL) or "").startswith(("http://", "https://")):
        fehler[CONF_URL] = "ungueltige_adresse"
    ersatz = str(eingabe.get(CONF_URL_ERSATZ) or "")
    if ersatz and not ersatz.startswith(("http://", "https://")):
        fehler[CONF_URL_ERSATZ] = "ungueltige_adresse"
    return fehler


class TvProgrammConfigFlow(ConfigFlow, domain=DOMAIN):
    """Quellen und Ladezeit wählen."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        fehler: dict[str, str] = {}
        if user_input is not None:
            fehler = _adressen_pruefen(user_input)
            if not fehler:
                await self.async_set_unique_id(user_input[CONF_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="TV-Programm", data={**user_input, CONF_URL_ERSATZ: user_input.get(CONF_URL_ERSATZ) or ""})
        return self.async_show_form(
            step_id="user",
            errors=fehler,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): str,
                    vol.Optional(CONF_URL_ERSATZ, default=DEFAULT_URL_ERSATZ): str,
                    vol.Required(CONF_UHRZEIT, default=DEFAULT_UHRZEIT): selector.TimeSelector(),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TvProgrammOptionsFlow()


class TvProgrammOptionsFlow(OptionsFlow):
    """Quellen und Sender."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        programm = self.config_entry.runtime_data
        fehler: dict[str, str] = {}
        if user_input is not None:
            fehler = _adressen_pruefen(user_input)
            if not fehler:
                return self.async_create_entry(data={**user_input, CONF_URL_ERSATZ: user_input.get(CONF_URL_ERSATZ) or ""})
        alle = programm.alle_sender or sorted(programm.auswahl)
        return self.async_show_form(
            step_id="init",
            errors=fehler,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=programm.haupt_url): str,
                    vol.Optional(CONF_URL_ERSATZ, description={"suggested_value": programm.ersatz_url}): str,
                    vol.Required(CONF_SENDER, default=programm.auswahl): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=alle, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN)
                    ),
                }
            ),
        )
