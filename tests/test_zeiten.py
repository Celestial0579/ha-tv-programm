"""Unplausible Sendezeiten werden verworfen und legen nichts lahm (#13)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.tv_programm import epg
from custom_components.tv_programm.const import CONF_SENDER, CONF_URL, CONF_URL_ERSATZ, DOMAIN
from custom_components.tv_programm.programm import Programm


@pytest.mark.parametrize(
    "wert",
    [
        "99991231235959 +0000",
        "99991231235959 -2359",
        "00010101000000 +2359",
        "19691231235959 +0000",
        "21000101000000 +0000",
        "99991231235959",
    ],
)
def test_unplausible_zeiten_ergeben_none(wert: str) -> None:
    assert epg._zeit(wert) is None


def test_plausible_zeit() -> None:
    assert epg._zeit("20261118201500 +0100") == datetime(2026, 11, 18, 19, 15, tzinfo=UTC).timestamp()


def test_auswerten_verwirft_unplausible_zeiten(tmp_path: Path) -> None:
    pfad = tmp_path / "epg.xml"
    pfad.write_text(
        '<tv><channel id="a"><display-name>ZDF</display-name></channel>'
        '<programme channel="a" start="20261118201500 +0100" stop="99991231235959 -2359"><title>Lang</title></programme>'
        '<programme channel="a" start="99991231235959 +0000" stop="99991231235959 +0000"><title>Weg</title></programme>'
        '<programme channel="a" start="20261118211500 +0100" stop="20261118201500 +0100"><title>Rückwärts</title></programme>'
        "</tv>",
        encoding="utf-8",
    )
    eintraege = epg.auswerten(str(pfad), ["ZDF"])["programm"]["ZDF"]
    assert [e[2] for e in eintraege] == ["Lang", "Rückwärts"]
    assert all(e[1] is None for e in eintraege)


async def test_gespeicherte_unplausible_zeiten_werden_verworfen(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    eintrag = MockConfigEntry(
        domain=DOMAIN, version=2, data={CONF_URL: "https://haupt.example/epg.xml", CONF_URL_ERSATZ: "", "uhrzeit": "04:00:00"},
        options={CONF_SENDER: ["ZDF"]},
    )
    eintrag.add_to_hass(hass)
    jetzt = time.time()
    hass_storage[f"{DOMAIN}.{eintrag.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{eintrag.entry_id}",
        "data": {
            "alle_sender": ["ZDF"],
            "programm": {
                "ZDF": [
                    [jetzt - 600, 253402387139.0, "Kaputt", "", "", ""],
                    [jetzt - 600, jetzt + 3000, "Gut", "", "", ""],
                ]
            },
            "stand": dt_util.utcnow().isoformat(),
        },
    }
    programm = Programm(hass, eintrag)
    with patch.object(Programm, "beim_start_laden", return_value=False):
        await programm.async_start()
    assert programm.bis is not None  # vorher ValueError
    assert programm.uebersicht(dt_util.now())[0]["titel"] == "Gut"
    assert programm.abfragen(None, ["ZDF"], None)["sender"][0]["laeuft"]["titel"] == "Gut"
    await programm.async_stop()
