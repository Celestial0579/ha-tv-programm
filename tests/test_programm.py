"""Quellenwahl, Ladeentscheidung und Abfragen mit nachgebauten Quellen."""

from __future__ import annotations

from datetime import timedelta
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.tv_programm import epg
from custom_components.tv_programm.const import CONF_SENDER, CONF_URL, CONF_URL_ERSATZ, DOMAIN
from custom_components.tv_programm.programm import Programm

HAUPT, ERSATZ = "https://haupt.example/epg.xml", "https://ersatz.example/epg.xml"


def _eintrag(hass: HomeAssistant, sender: list[str]) -> MockConfigEntry:
    eintrag = MockConfigEntry(
        domain=DOMAIN, version=2, data={CONF_URL: HAUPT, CONF_URL_ERSATZ: ERSATZ, "uhrzeit": "04:00:00"},
        options={CONF_URL: HAUPT, CONF_URL_ERSATZ: ERSATZ, CONF_SENDER: sender},
    )
    eintrag.add_to_hass(hass)
    return eintrag


def _sendung(start: float, titel: str) -> list[Any]:
    return [start, start + 3600, titel, "", "", ""]


async def test_ersatzquelle_nur_fuer_fehlende_sender(hass: HomeAssistant) -> None:
    programm = Programm(hass, _eintrag(hass, ["ZDF", "NDR Fernsehen"]))
    jetzt = time.time()
    antworten = {
        HAUPT: {"alle_sender": ["ZDF", "NDR Fernsehen"], "programm": {"ZDF": [_sendung(jetzt, "Krimi")]}},
        ERSATZ: {"alle_sender": ["NDR FERNSEHEN"], "programm": {"NDR FERNSEHEN": [_sendung(jetzt, "Nordmagazin")]}},
    }
    gefragt: list[tuple[str, list[str]]] = []

    async def quelle(url: str, _datei: str, auswahl: list[str]) -> dict[str, Any]:
        gefragt.append((url, auswahl))
        return antworten[url]

    with patch.object(programm, "_quelle", side_effect=quelle):
        await programm._laden()
    assert gefragt == [(HAUPT, ["ZDF", "NDR Fernsehen"]), (ERSATZ, ["NDR Fernsehen"])]
    # Andere Schreibweise in der Ersatzquelle: trotzdem gewählt und abfragbar.
    assert programm.gewaehlt() == ["ZDF", "NDR FERNSEHEN"]
    assert [z["sender"] for z in programm.uebersicht(dt_util.now())] == ["ZDF", "NDR FERNSEHEN"]
    assert programm.abfragen(None, ["NDR-Fernsehen"], None)["sender"][0]["laeuft"]["titel"] == "Nordmagazin"


async def test_beide_quellen_fallen_aus(hass: HomeAssistant) -> None:
    programm = Programm(hass, _eintrag(hass, ["ZDF"]))
    with patch.object(programm, "_quelle", AsyncMock(side_effect=epg.EpgFehler("HTTP 503"))):
        await programm._laden()
    assert programm.fehler == "Hauptquelle: HTTP 503; Ersatzquelle: HTTP 503"
    gespeichert = await programm._store.async_load()
    assert time.time() - gespeichert["versuch"] < 60
    # Kein sofortiger neuer Versuch beim nächsten Start.
    assert programm.beim_start_laden(gespeichert) is False


async def test_ladeentscheidung_beim_start(hass: HomeAssistant) -> None:
    programm = Programm(hass, _eintrag(hass, ["ZDF", "ARD alpha"]))
    programm.stand = dt_util.utcnow()
    frisch = {"quellen_fp": programm._quellen_fp, "wunsch": ["ZDF", "ARD alpha"], "versuch": time.time()}
    # „ARD alpha“ hat kein Programm: das allein ist kein Grund zu laden.
    assert programm.beim_start_laden(frisch) is False
    assert programm.beim_start_laden({**frisch, "wunsch": ["ZDF"]}) is True  # neu gewünscht
    assert programm.beim_start_laden({**frisch, "quellen_fp": "alt"}) is True
    programm.stand = dt_util.utcnow() - timedelta(hours=21)
    assert programm.beim_start_laden({**frisch, "versuch": time.time() - 7200}) is True


async def test_abfragen_mit_suche_und_spitznamen(hass: HomeAssistant) -> None:
    programm = Programm(hass, _eintrag(hass, ["Das Erste", "ZDF"]))
    jetzt = time.time()
    programm.alle_sender = ["Das Erste", "ZDF"]
    programm.programm = {
        "Das Erste": [_sendung(jetzt - 600, "Tagesschau"), _sendung(jetzt + 3000, "Tatort")],
        "ZDF": [_sendung(jetzt + 7200, "Tatort: Wiederholung")],
    }
    assert programm.abfragen(None, ["im Ersten"], None)["sender"][0]["sender"] == "Das Erste"
    treffer = programm.abfragen(None, None, "tatort")["treffer"]
    assert [t["sender"] for t in treffer] == ["Das Erste", "ZDF"]
    with pytest.raises(HomeAssistantError):
        programm.abfragen(None, ["Gibtsnicht"], None)


async def test_optionen_speichern_waehrend_des_ladens(hass: HomeAssistant, tmp_path: Any) -> None:
    """#18: Beim Neuladen bricht der alte Ladelauf ab; nie zwei Downloads zugleich, die neue Instanz hat Programm."""
    import asyncio
    import contextlib
    import glob
    import os

    hass.config.config_dir = str(tmp_path)  # nicht ins testing_config des Pakets schreiben
    os.makedirs(hass.config.path(".storage"), exist_ok=True)
    eintrag = _eintrag(hass, ["ZDF"])
    laufend = 0
    hoechstens = 0
    aufrufe = 0
    freigabe = asyncio.Event()

    async def herunterladen(_sitzung: Any, _url: str, ziel: str, *_: Any) -> int:
        nonlocal laufend, hoechstens, aufrufe
        aufrufe += 1
        laufend += 1
        hoechstens = max(hoechstens, laufend)
        try:
            with open(ziel + ".teil", "w", encoding="utf-8") as datei:
                datei.write("<tv/>")
            await freigabe.wait()
            os.replace(ziel + ".teil", ziel)
        except asyncio.CancelledError:
            os.remove(ziel + ".teil")
            raise
        finally:
            laufend -= 1
        return 5

    @contextlib.asynccontextmanager
    async def sitzung(_url: str):
        yield None

    def auswerten(datei: str, _auswahl: list[str] | None) -> dict[str, Any]:
        assert os.path.exists(datei)
        return {"alle_sender": ["ZDF"], "programm": {"ZDF": [_sendung(time.time(), "Krimi")]}}

    with (
        patch.object(epg, "herunterladen", side_effect=herunterladen),
        patch.object(epg, "eigene_sitzung", sitzung),
        patch.object(epg, "auswerten", side_effect=auswerten),
    ):
        assert await hass.config_entries.async_setup(eintrag.entry_id)
        await asyncio.sleep(0)
        assert aufrufe == 1  # erster Download läuft und wartet
        alt = eintrag.runtime_data
        hass.config_entries.async_update_entry(eintrag, options={**eintrag.options, CONF_SENDER: ["ZDF", "ARD"]})
        await hass.async_block_till_done(wait_background_tasks=False)
        for _ in range(20):
            if aufrufe == 2:
                break
            await asyncio.sleep(0)
        assert eintrag.runtime_data is not alt
        assert aufrufe == 2 and laufend == 1  # alter Lauf beendet, nur der neue lädt
        freigabe.set()
        await hass.async_block_till_done(wait_background_tasks=True)

    neu = eintrag.runtime_data
    assert hoechstens == 1
    assert neu.programm and neu.fehler is None
    assert not glob.glob(hass.config.path(".storage", f"{DOMAIN}.{eintrag.entry_id}.*.xml*"))
    assert await hass.config_entries.async_unload(eintrag.entry_id)
