"""Weiterleitungen der Quelle werden selbst verfolgt und geprüft (GHSA-m7cx-3q6v-c42v)."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from aiohttp import web
import pytest
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.tv_programm import epg

OEFFENTLICH = ipaddress.ip_address("93.184.216.34")  # nur als Antwort der Attrappe, nie verbunden


class Bewacht:
    """Sitzung, die nur zur Testquelle verbindet: Ein Fehler im Code erreicht nie das echte Netz."""

    def __init__(self, echt, erlaubt: set[str]) -> None:
        self._echt = echt
        self._erlaubt = erlaubt
        self.rechner: list[str] = []

    def get(self, url, **kwargs):
        rechner = URL(url).host
        self.rechner.append(rechner)
        assert rechner in self._erlaubt, f"Verbindung zu {rechner} versucht"
        assert kwargs.get("allow_redirects") is False
        return self._echt.get(url, **kwargs)


@pytest.fixture
async def quelle(aiohttp_server):
    aufrufe: list[str] = []

    async def klein(request: web.Request) -> web.Response:
        aufrufe.append(request.path)
        return web.Response(body=b"<tv/>")

    async def weiter(request: web.Request) -> web.Response:
        aufrufe.append(request.path)
        raise web.HTTPFound(request.query["ziel"])

    async def kreis(request: web.Request) -> web.Response:
        aufrufe.append(request.path)
        raise web.HTTPFound("/kreis")

    app = web.Application()
    app.router.add_get("/klein", klein)
    app.router.add_get("/weiter", weiter)
    app.router.add_get("/kreis", kreis)
    server = await aiohttp_server(app)
    server.aufrufe = aufrufe
    return server


@pytest.fixture
def oeffentlich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Testquelle auf 127.0.0.1 gilt als öffentlicher Server; Namen werden nicht wirklich aufgelöst."""

    async def adressen(rechner: str, port: int):
        if rechner == "127.0.0.1":
            return [OEFFENTLICH]
        return [ipaddress.ip_address(rechner)]

    monkeypatch.setattr(epg, "_adressen", adressen)


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
async def test_weiterleitung_innerhalb_der_quelle(hass: HomeAssistant, quelle, tmp_path: Path) -> None:
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    ziel = str(tmp_path / "epg.xml")
    assert await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/weiter?ziel=/klein", ziel) == 5
    assert quelle.aufrufe == ["/weiter", "/klein"]


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
@pytest.mark.parametrize(
    "ziel",
    [
        "http://192.168.178.1/relay/0?turn=on",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/",
        "http://localhost:8123/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:192.168.1.1]/",
        "http://224.0.0.251/",
        "http://0.0.0.0/",
    ],
)
async def test_weiterleitung_ins_lokale_netz_wird_abgelehnt(
    hass: HomeAssistant, quelle, tmp_path: Path, ziel: str
) -> None:
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    datei = tmp_path / "epg.xml"
    with pytest.raises(epg.EpgFehler, match="lokalen Netz") as fehler:
        await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/weiter?ziel={ziel}", str(datei))
    assert quelle.aufrufe == ["/weiter"]
    assert sitzung.rechner == ["127.0.0.1"]
    assert URL(ziel).host not in str(fehler.value)  # nie die Adresse im Fehlertext
    assert not datei.exists() and not Path(str(datei) + ".teil").exists()


@pytest.mark.usefixtures("socket_enabled", "oeffentlich")
async def test_zu_viele_weiterleitungen(hass: HomeAssistant, quelle, tmp_path: Path) -> None:
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    with pytest.raises(epg.EpgFehler, match="Weiterleitungen"):
        await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/kreis", str(tmp_path / "epg.xml"))
    assert len(quelle.aufrufe) == epg.MAX_WEITERLEITUNGEN + 1


@pytest.mark.usefixtures("socket_enabled")
async def test_quelle_im_heimnetz_darf_im_heimnetz_weiterleiten(hass: HomeAssistant, quelle, tmp_path: Path) -> None:
    """Eigener Tvheadend: 127.0.0.1 ist lokal, eine Weiterleitung ins Heimnetz bleibt erlaubt."""
    sitzung = Bewacht(async_get_clientsession(hass), {"127.0.0.1"})
    ziel = str(tmp_path / "epg.xml")
    weiter = f"http://127.0.0.1:{quelle.port}/klein"
    assert await epg.herunterladen(sitzung, f"http://127.0.0.1:{quelle.port}/weiter?ziel={weiter}", ziel) == 5
    assert await epg._nur_lokal("192.168.1.5", 9981) is True
    assert await epg._weiterleitung_abgelehnt(URL("http://192.168.1.5:9981/a"), URL("http://192.168.1.6/b"), True) is None


async def test_kein_wechsel_von_https_auf_http() -> None:
    grund = await epg._weiterleitung_abgelehnt(URL("https://quelle.example/a"), URL("http://quelle.example/b"), True)
    assert grund == "von https auf http"
    assert await epg._weiterleitung_abgelehnt(URL("http://quelle.example/a"), URL("ftp://quelle.example/b"), True)


@pytest.mark.parametrize(
    ("adresse", "lokal"),
    [
        ("127.0.0.1", True), ("10.1.2.3", True), ("172.16.0.1", True), ("192.168.0.1", True),
        ("169.254.169.254", True), ("100.64.0.1", True), ("224.0.0.1", True), ("0.0.0.0", True),
        ("::1", True), ("fd00::1", True), ("fe80::1", True), ("::ffff:10.0.0.1", True), ("ff02::1", True),
        ("93.184.216.34", False), ("2a00:1450:4001::1", False),
    ],
)
def test_nicht_oeffentlich(adresse: str, lokal: bool) -> None:
    assert epg._nicht_oeffentlich(ipaddress.ip_address(adresse)) is lokal


async def test_unaufloesbar(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scheitert(_rechner: str, _port: int):
        raise OSError("keine Auflösung")

    monkeypatch.setattr(epg, "_adressen", scheitert)
    assert await epg._lokal("quelle.example", 80) is True  # Ziel: im Zweifel ablehnen
    assert await epg._nur_lokal("quelle.example", 80) is False  # Start: im Zweifel öffentlich, also streng


async def test_ungueltige_adresse_ist_epgfehler(hass: HomeAssistant, tmp_path: Path) -> None:
    with pytest.raises(epg.EpgFehler):
        await epg.herunterladen(async_get_clientsession(hass), "ftp://quelle.example/epg.xml", str(tmp_path / "x"))
