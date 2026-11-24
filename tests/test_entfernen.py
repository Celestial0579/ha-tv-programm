"""Keine Quelladresse im Speicher; Entfernen löscht Speicher und Dateien (GHSA-ppx8-wvfc-f452)."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.tv_programm import epg
from custom_components.tv_programm.const import CONF_SENDER, CONF_URL, CONF_URL_ERSATZ, DOMAIN

# Erfundene Adressen mit Zugangsdaten, wie bei eigenen Quellen (z. B. Tvheadend).
HAUPT = "http://tvh:geheim1@tv.example/xmltv/channels?token=geheim2"
ERSATZ = "https://nutzer:geheim3@ersatz.example/epg.xml"
GEHEIM = ("geheim1", "geheim2", "geheim3", "tv.example", "ersatz.example")


def _eintrag(hass: HomeAssistant) -> MockConfigEntry:
    eintrag = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id=DOMAIN, data={CONF_URL: HAUPT, CONF_URL_ERSATZ: ERSATZ, "uhrzeit": "04:00:00"},
        options={CONF_URL: HAUPT, CONF_URL_ERSATZ: ERSATZ, CONF_SENDER: ["ZDF"]},
    )
    eintrag.add_to_hass(hass)
    return eintrag


def _ohne_adressen(daten: Any) -> None:
    text = json.dumps(daten)
    assert not [g for g in GEHEIM if g in text]


async def test_speicher_ohne_adressen_und_entfernen(hass: HomeAssistant, hass_storage: dict[str, Any], tmp_path: Any) -> None:
    hass.config.config_dir = str(tmp_path)
    os.makedirs(hass.config.path(".storage"))
    eintrag = _eintrag(hass)
    schluessel = f"{DOMAIN}.{eintrag.entry_id}"
    # Fehlschlag (schreibt "versuch") und Erfolg (schreibt das Programm): beide ohne Adresse.
    with patch.object(epg, "herunterladen", AsyncMock(side_effect=epg.EpgFehler("HTTP 503"))):
        assert await hass.config_entries.async_setup(eintrag.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    _ohne_adressen(hass_storage[schluessel]["data"])
    assert hass_storage[schluessel]["data"]["quellen_fp"]

    programm = eintrag.runtime_data
    with patch.object(programm, "_quelle", AsyncMock(return_value={"alle_sender": ["ZDF"], "programm": {"ZDF": [[1.0, 2.0, "T", "", "", ""]]}})):
        await programm._laden()
    await hass.async_block_till_done()
    _ohne_adressen(hass_storage[schluessel]["data"])

    # Liegengebliebene Dateien (frühere feste Namen und eine Laufdatei) verschwinden mit dem Eintrag.
    for name in (f"{schluessel}.xml", f"{schluessel}.ersatz.xml.teil", f"{schluessel}.ab12cd34.xml"):
        with open(hass.config.path(".storage", name), "w", encoding="utf-8") as datei:
            datei.write("<tv/>")
    fremd = hass.config.path(".storage", f"{DOMAIN}.anderer.xml")
    with open(fremd, "w", encoding="utf-8") as datei:
        datei.write("<tv/>")

    assert await hass.config_entries.async_remove(eintrag.entry_id)
    await hass.async_block_till_done()
    assert schluessel not in hass_storage
    assert sorted(os.listdir(hass.config.path(".storage"))) == [f"{DOMAIN}.anderer.xml"]


async def test_alte_adressen_im_speicher_werden_ersetzt(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    eintrag = _eintrag(hass)
    schluessel = f"{DOMAIN}.{eintrag.entry_id}"
    hass_storage[schluessel] = {
        "version": 1, "key": schluessel,
        "data": {"quellen": [HAUPT, ERSATZ], "wunsch": ["ZDF"], "versuch": 0, "stand": "2099-01-01T00:00:00+00:00"},
    }
    with patch.object(epg, "herunterladen", AsyncMock(side_effect=epg.EpgFehler("HTTP 503"))) as laden:
        assert await hass.config_entries.async_setup(eintrag.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    daten = hass_storage[schluessel]["data"]
    assert "quellen" not in daten
    _ohne_adressen(daten)
    # Dieselben Quellen wie vorher: der Fingerabdruck passt, also kein Neuladen nur wegen der Umstellung.
    assert daten["quellen_fp"] == eintrag.runtime_data._quellen_fp
    assert laden.await_count == 0
    assert await hass.config_entries.async_unload(eintrag.entry_id)
