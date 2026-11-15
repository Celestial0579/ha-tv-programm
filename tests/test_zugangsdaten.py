"""GHSA-3r2j-mp95-7p2c: Zugangsdaten aus der Quelladresse erscheinen nirgends (nur Attrappen)."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.tv_programm import epg
from custom_components.tv_programm.const import CONF_URL, CONF_URL_ERSATZ, DOMAIN
from custom_components.tv_programm.programm import Programm

GEHEIM = "geheim123"


def test_ohne_zugang() -> None:
    assert epg.ohne_zugang(f"http://nutzer:{GEHEIM}@tvh.local:9981/xmltv/channels?auth={GEHEIM}") == (
        "http://tvh.local:9981/xmltv/channels"
    )
    assert epg.ohne_zugang("https://[2001:db8::1]/epg.xml.gz") == "https://[2001:db8::1]/epg.xml.gz"
    assert epg.ohne_zugang("http://[kaputt/") == "(ungültige Adresse)"


@pytest.fixture
async def attrappe(aiohttp_server):
    async def schleife(request: web.Request) -> web.Response:
        raise web.HTTPFound(location=str(request.rel_url))

    async def fehlt(_request: web.Request) -> web.Response:
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/schleife", schleife)
    app.router.add_get("/fehlt", fehlt)
    return await aiohttp_server(app)


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize(
    "pfad",
    ["/schleife?token=" + GEHEIM, "/fehlt?token=" + GEHEIM],
)
async def test_fehlertexte_ohne_zugangsdaten(hass: HomeAssistant, attrappe, tmp_path: Path, pfad: str) -> None:
    url = f"http://nutzer:{GEHEIM}@127.0.0.1:{attrappe.port}{pfad}"
    with pytest.raises(epg.EpgFehler) as fehler:
        await epg.herunterladen(async_get_clientsession(hass), url, str(tmp_path / "a.xml"))
    assert GEHEIM not in str(fehler.value) and "nutzer" not in str(fehler.value)


async def test_ungueltige_adresse_ohne_zugangsdaten(hass: HomeAssistant, tmp_path: Path) -> None:
    with pytest.raises(epg.EpgFehler) as fehler:
        await epg.herunterladen(async_get_clientsession(hass), f"http://nutzer:{GEHEIM}@[kaputt/x", str(tmp_path / "a"))
    assert GEHEIM not in str(fehler.value)


async def test_protokoll_ohne_zugangsdaten(hass: HomeAssistant, tmp_path: Path, monkeypatch, caplog) -> None:
    url = f"http://nutzer:{GEHEIM}@tvh.local:9981/xmltv?auth={GEHEIM}"
    eintrag = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_URL: url, CONF_URL_ERSATZ: ""}, options={CONF_URL: url})
    eintrag.add_to_hass(hass)
    programm = Programm(hass, eintrag)

    async def laden(_sitzung, _url, _ziel, *_):
        return 5_000_000

    monkeypatch.setattr(epg, "herunterladen", laden)
    monkeypatch.setattr(epg, "auswerten", lambda *_: {"alle_sender": ["A"], "programm": {"A": []}})
    with caplog.at_level(logging.INFO):
        await programm._quelle(url, str(tmp_path / "a"), ["A"])
    assert "tvh.local:9981/xmltv" in caplog.text
    assert GEHEIM not in caplog.text and "nutzer" not in caplog.text


async def test_unique_id_ohne_adresse(hass: HomeAssistant) -> None:
    url = f"http://nutzer:{GEHEIM}@tvh.local/xmltv"
    alt = MockConfigEntry(domain=DOMAIN, version=2, unique_id=url, data={CONF_URL: url, CONF_URL_ERSATZ: ""})
    alt.add_to_hass(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Programm, "async_start", lambda self: _nichts())
        await hass.config_entries.async_setup(alt.entry_id)
        await hass.async_block_till_done()
    assert alt.unique_id == DOMAIN


async def _nichts() -> None:
    return None
