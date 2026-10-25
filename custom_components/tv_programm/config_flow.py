"""Einrichtung über die Oberfläche."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_SENDER, CONF_UHRZEIT, CONF_URL, DEFAULT_UHRZEIT, DEFAULT_URL, DOMAIN


class TvProgrammConfigFlow(ConfigFlow, domain=DOMAIN):
    """Quelle und Ladezeit wählen."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        fehler: dict[str, str] = {}
        if user_input is not None:
            if not str(user_input[CONF_URL]).startswith(("http://", "https://")):
                fehler[CONF_URL] = "ungueltige_adresse"
            else:
                await self.async_set_unique_id(user_input[CONF_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="TV-Programm", data=user_input)
        return self.async_show_form(
            step_id="user",
            errors=fehler,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): str,
                    vol.Required(CONF_UHRZEIT, default=DEFAULT_UHRZEIT): selector.TimeSelector(),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TvProgrammOptionsFlow()


class TvProgrammOptionsFlow(OptionsFlow):
    """Sender auswählen."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        programm = self.config_entry.runtime_data
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        alle = programm.alle_sender or sorted(programm.auswahl)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SENDER, default=programm.auswahl): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=alle, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )
